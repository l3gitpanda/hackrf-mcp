"""Meshtastic mesh radios, and LoRa in general: capture decoders and a live
profile, with the LoRa receiver built into this package
(hackrf_mcp.lora_phy), so no program is needed.

What a Meshtastic packet is on the air, as the firmware sends it (the
numbers checked against its source and protobuf definitions; nothing here is
generated or copied from them):

- LoRa with sync word 0x2B, a 16-symbol preamble, explicit header and CRC,
  at one of the modem presets below. A preset's default channel is named
  after it ("LongFast"), and its frequency slot in a region is the djb2 hash
  of that name modulo the number of slots that fit (US 902-928 MHz:
  LongFast at 906.875 MHz).
- A 16-byte header, little-endian: to, from, packet id (32 bits each), flags
  (hop limit in bits 0-2, want-ack 3, via MQTT 4, hop start 5-7), the
  channel hash (the XOR of the channel name's bytes and of its key's), and
  the low bytes of the next hop's and the relaying node's numbers.
- Then the payload, AES-CTR with the channel key, the nonce being the
  packet id (64-bit little-endian) then the sender (32-bit) then zeros. The
  default channel's key is published in the firmware, so every node (and
  this decoder) reads it; only it is tried. Packets on other channels, and
  direct messages encrypted to one node's key, stay encrypted.
- The plaintext is a protobuf "Data" message: the port (what the app is:
  text, position, node info, telemetry, ...) and that app's own message.

What is shown follows the operator's choice for this station: text sent to
everyone on the default channel is shown, like an AIS ship's name; a direct
message's text is not, even when it could be read. Positions are shown as
the node sent them.
"""

from __future__ import annotations

import collections
import json
import struct
import threading
import time
from collections import Counter
from dataclasses import dataclass

from hackrf_mcp import decoders, iq, live_profiles, lora_phy
from hackrf_mcp.decoders import DecodeError, Decoder, Job, Result
from hackrf_mcp.live_log import Row
from hackrf_mcp.live_profiles import Pipeline, PositionThrottle, Profile, View

# --- Presets and slots -----------------------------------------------------


@dataclass(frozen=True)
class Preset:
    name: str  # the default channel's name, hashed for its slot
    bw_hz: int
    sf: int
    cr: int  # parity bits: code rate 4/(4 + cr)


PRESETS = (
    Preset("LongFast", 250_000, 11, 1),
    Preset("LongSlow", 125_000, 12, 4),
    Preset("LongMod", 125_000, 11, 4),
    Preset("LongTurbo", 500_000, 11, 4),
    Preset("MediumFast", 250_000, 9, 1),
    Preset("MediumSlow", 250_000, 10, 1),
    Preset("ShortFast", 250_000, 7, 1),
    Preset("ShortSlow", 250_000, 8, 1),
    Preset("ShortTurbo", 500_000, 7, 1),
)
BY_RADIO = {(p.bw_hz, p.sf): p for p in PRESETS}
US_HZ = (902_000_000, 928_000_000)
SYNC_WORD = 0x2B
# The default channel key ("AQ==" in the apps, an index into this).
DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
BROADCAST = 0xFFFFFFFF


def djb2(text: str) -> int:
    h = 5381
    for c in text.encode():
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


def slot_hz(preset: Preset, band: tuple[int, int] = US_HZ) -> int:
    """The preset's default frequency slot in a band."""
    count = (band[1] - band[0]) // preset.bw_hz
    return band[0] + preset.bw_hz // 2 + (djb2(preset.name) % count) * preset.bw_hz


def channel_hash(name: str, key: bytes) -> int:
    h = 0
    for b in name.encode() + key:
        h ^= b
    return h


# --- Packets ---------------------------------------------------------------


@dataclass
class Packet:
    to: int
    sender: int
    id: int
    hop_limit: int
    want_ack: bool
    via_mqtt: bool
    hop_start: int
    channel: int
    next_hop: int
    relay: int
    body: bytes  # still encrypted


def parse_packet(raw: bytes) -> Packet | None:
    if len(raw) < 16:
        return None
    to, sender, pid, flags, channel, next_hop, relay = struct.unpack_from("<IIIBBBB", raw)
    return Packet(
        to, sender, pid, flags & 7, bool(flags & 8), bool(flags & 16), flags >> 5, channel, next_hop, relay, raw[16:]
    )


def decrypt(pkt: Packet, key: bytes = DEFAULT_KEY) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    nonce = struct.pack("<QI", pkt.id, pkt.sender) + bytes(4)
    d = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    return d.update(pkt.body) + d.finalize()


def node_id(num: int) -> str:
    return f"!{num:08x}"


# --- Protobuf wire format --------------------------------------------------


class ProtoError(ValueError):
    pass


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ProtoError("truncated varint")
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, i


def fields(buf: bytes) -> dict[int, list]:
    """A message's fields by number: varints and fixed-width values as
    ints, length-delimited ones as bytes. Raises ProtoError if it isn't a
    well-formed message."""
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if num == 0:
            raise ProtoError("field number 0")
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = int.from_bytes(buf[i : i + 8], "little"), i + 8
        elif wire == 2:
            size, i = _varint(buf, i)
            value, i = bytes(buf[i : i + size]), i + size
        elif wire == 5:
            value, i = int.from_bytes(buf[i : i + 4], "little"), i + 4
        else:
            raise ProtoError(f"wire type {wire}")
        if i > len(buf):
            raise ProtoError("truncated field")
        out.setdefault(num, []).append(value)
    return out


def _one(f: dict, num: int, default=None):
    v = f.get(num)
    return v[-1] if v else default


def _sfixed32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def _int32(v: int) -> int:
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >> 63 else v


def _float(v: int) -> float:
    return struct.unpack("<f", (v & 0xFFFFFFFF).to_bytes(4, "little"))[0]


def _text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


def _packed_fixed32(values: list) -> list[int]:
    out = []
    for v in values:
        if isinstance(v, bytes):
            out += [int.from_bytes(v[i : i + 4], "little") for i in range(0, len(v) - 3, 4)]
        else:
            out.append(v)
    return out


# --- What the apps carry ---------------------------------------------------

PORTS = {
    0: "UNKNOWN", 1: "TEXT", 2: "REMOTE_HARDWARE", 3: "POSITION", 4: "NODEINFO", 5: "ROUTING", 6: "ADMIN",
    7: "TEXT_COMPRESSED", 8: "WAYPOINT", 9: "AUDIO", 10: "DETECTION_SENSOR", 11: "ALERT", 12: "KEY_VERIFICATION",
    32: "REPLY", 33: "IP_TUNNEL", 34: "PAXCOUNTER", 35: "STORE_FORWARD_PLUSPLUS", 36: "NODE_STATUS",
    37: "MESH_BEACON", 64: "SERIAL", 65: "STORE_FORWARD", 66: "RANGE_TEST", 67: "TELEMETRY", 68: "ZPS",
    69: "SIMULATOR", 70: "TRACEROUTE", 71: "NEIGHBORINFO", 72: "ATAK_PLUGIN", 73: "MAP_REPORT",
    74: "POWERSTRESS", 76: "RETICULUM_TUNNEL", 77: "CAYENNE", 256: "PRIVATE", 257: "ATAK_FORWARDER",
}
HW_MODELS = {
    0: "UNSET", 1: "TLORA_V2", 2: "TLORA_V1", 3: "TLORA_V2_1_1P6", 4: "TBEAM", 5: "HELTEC_V2_0", 6: "TBEAM_V0P7",
    7: "T_ECHO", 8: "TLORA_V1_1P3", 9: "RAK4631", 10: "HELTEC_V2_1", 11: "HELTEC_V1", 12: "LILYGO_TBEAM_S3_CORE",
    13: "RAK11200", 14: "NANO_G1", 15: "TLORA_V2_1_1P8", 16: "TLORA_T3_S3", 17: "NANO_G1_EXPLORER",
    18: "NANO_G2_ULTRA", 19: "LORA_TYPE", 20: "WIPHONE", 31: "STATION_G2", 33: "T_ECHO_PLUS", 37: "PORTDUINO",
    43: "HELTEC_V3", 48: "HELTEC_WIRELESS_TRACKER", 58: "HELTEC_WIRELESS_TRACKER_V1_0", 81: "SEEED_XIAO_S3",
    95: "SEEED_SOLAR_NODE", 99: "SEEED_WIO_TRACKER_L1", 255: "PRIVATE_HW",
}
ROLES = {
    0: "CLIENT", 1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER_CLIENT", 4: "REPEATER", 5: "TRACKER", 6: "SENSOR",
    7: "TAK", 8: "CLIENT_HIDDEN", 9: "LOST_AND_FOUND", 10: "TAK_TRACKER", 11: "ROUTER_LATE", 12: "CLIENT_BASE",
}


@dataclass
class Message:
    port: int
    summary: str  # one line, as shown
    info: dict  # the fields worth keeping (node names, a position, battery)


def _position(f: dict) -> dict:
    out: dict = {}
    lat, lon = _one(f, 1), _one(f, 2)
    if lat is not None and lon is not None and (lat or lon):
        out["lat"], out["lon"] = _sfixed32(lat) * 1e-7, _sfixed32(lon) * 1e-7
    for num, key in ((3, "alt_m"), (19, "sats"), (23, "precision_bits")):
        v = _one(f, num)
        if v is not None:
            out[key] = _int32(v) if num == 3 else v
    v = _one(f, 15)
    if v is not None:
        out["speed_m_s"] = v
    v = _one(f, 4) or _one(f, 7)
    if v:
        out["time"] = v
    return out


def _telemetry(f: dict) -> dict:
    out: dict = {}
    dev = _one(f, 2)
    if isinstance(dev, bytes):
        d = fields(dev)
        for num, key, conv in (
            (1, "battery_pct", int),
            (2, "voltage", _float),
            (3, "channel_util_pct", _float),
            (4, "air_util_tx_pct", _float),
            (5, "uptime_s", int),
        ):
            v = _one(d, num)
            if v is not None:
                out[key] = conv(v)
    env = _one(f, 3)
    if isinstance(env, bytes):
        e = fields(env)
        for num, key in ((1, "temperature_c"), (2, "humidity_pct"), (3, "pressure_hpa")):
            v = _one(e, num)
            if v is not None:
                out[key] = _float(v)
    stats = _one(f, 6)
    if isinstance(stats, bytes):
        s = fields(stats)
        for num, key, conv in (
            (2, "channel_util_pct", _float),
            (3, "air_util_tx_pct", _float),
            (7, "nodes_online", int),
            (8, "nodes_total", int),
        ):
            v = _one(s, num)
            if v is not None:
                out[key] = conv(v)
    return out


def _fmt(info: dict) -> str:
    parts = []
    for key, value in info.items():
        if isinstance(value, float):
            value = f"{value:.7f}".rstrip("0").rstrip(".") if key in ("lat", "lon") else f"{value:.2f}".rstrip("0").rstrip(".")
        parts.append(f"{key}={value}")
    return " ".join(parts)


def describe(port: int, payload: bytes, broadcast: bool) -> Message:
    """What one app message says, in a line and as fields."""
    name = PORTS.get(port, f"PORT_{port}")
    try:
        if port in (1, 66):  # text, range test
            text = payload.decode("utf-8", "replace")
            if not broadcast:
                return Message(port, f"{name} direct message, {len(payload)} bytes (not shown)", {})
            return Message(port, f"{name} {text!r}", {"text": text})
        if port == 3:
            info = _position(fields(payload))
            return Message(port, f"{name} {_fmt(info) or '(no fix)'}", info)
        if port == 4:
            f = fields(payload)
            info = {
                "id": _text(_one(f, 1)),
                "long_name": _text(_one(f, 2)),
                "short_name": _text(_one(f, 3)),
                "hw_model": HW_MODELS.get(_one(f, 5, 0), str(_one(f, 5, 0))),
                "role": ROLES.get(_one(f, 7, 0), str(_one(f, 7, 0))),
            }
            if _one(f, 6):
                info["licensed"] = True
            if _one(f, 8):
                info["public_key"] = True
            label = f"{info['short_name']!r} {info['long_name']!r} {info['hw_model']} {info['role']}"
            return Message(port, f"{name} {info['id']} {label}", info)
        if port == 67:
            info = _telemetry(fields(payload))
            return Message(port, f"{name} {_fmt(info) or '(other metrics)'}", info)
        if port == 5:
            err = _one(fields(payload), 3)
            return Message(port, f"{name} {'ack' if not err else f'error {err}'}", {})
        if port == 70:
            f = fields(payload)
            hops = [node_id(v) for v in _packed_fixed32(f.get(1, []))]
            back = [node_id(v) for v in _packed_fixed32(f.get(3, []))]
            info = {"route": hops, "route_back": back}
            return Message(port, f"{name} route {' > '.join(hops) or '(direct)'}" + (f" back {' > '.join(back)}" if back else ""), info)
        if port == 71:
            f = fields(payload)
            neighbours = [fields(n) for n in f.get(4, []) if isinstance(n, bytes)]
            return Message(port, f"{name} {len(neighbours)} neighbours", {"neighbours": len(neighbours)})
        if port == 8:
            f = fields(payload)
            info = {}
            lat, lon = _one(f, 2), _one(f, 3)
            if lat is not None and lon is not None:
                info["lat"], info["lon"] = _sfixed32(lat) * 1e-7, _sfixed32(lon) * 1e-7
            if broadcast:
                info["name"] = _text(_one(f, 6))
            return Message(port, f"{name} {_fmt(info)}", info)
    except ProtoError as exc:
        return Message(port, f"{name} ({len(payload)} bytes, unreadable: {exc})", {})
    return Message(port, f"{name} ({len(payload)} bytes)", {})


@dataclass
class Received:
    packet: Packet
    preset: str | None
    readable: bool  # decrypted with the default key into a Data message
    port: int | None = None
    message: Message | None = None


def decode_packet(raw: bytes, preset: Preset | None) -> Received | None:
    """A LoRa payload as a Meshtastic packet, decrypted when it is on the
    preset's default channel. None if it is too short to be one."""
    pkt = parse_packet(raw)
    if pkt is None:
        return None
    out = Received(pkt, preset.name if preset else None, False)
    if preset is None or pkt.channel != channel_hash(preset.name, DEFAULT_KEY) or not pkt.body:
        return out
    try:
        data = fields(decrypt(pkt))
        port = _one(data, 1)
        payload = _one(data, 2, b"")
        if not isinstance(port, int) or port > 511 or not isinstance(payload, bytes):
            return out
    except ProtoError:
        return out
    out.readable = True
    out.port = port
    out.message = describe(port, payload, pkt.to == BROADCAST)
    return out


# --- The capture decoders --------------------------------------------------


def _channels(job: Job, presets: tuple[Preset, ...] | None) -> list[lora_phy.Channel]:
    """The channels to search: each frequency at every bandwidth the
    presets use (or 125, 250 and 500 kHz at every spreading factor, for
    plain LoRa) that the capture holds and its rate can give."""
    cap = job.cap
    if presets is None:
        radios = {bw: tuple(range(7, 13)) for bw in (125_000, 250_000, 500_000)}
    else:
        radios = {}
        for p in presets:
            radios.setdefault(p.bw_hz, set()).add(p.sf)
        radios = {bw: tuple(sorted(sfs)) for bw, sfs in radios.items()}
    usable = {bw: sfs for bw, sfs in radios.items() if lora_phy.rate_fits(cap.sample_rate_hz, bw)}
    if not usable:
        raise DecodeError(
            f"{job.name} needs a sample rate that is a multiple of 2 MS/s (2,000,000 S/s holds a slot); "
            f"this capture is {cap.sample_rate_hz} S/s."
        )
    out = []
    for f in job.freqs:
        for bw, sfs in sorted(usable.items()):
            if cap.covers(f, bw / 2):
                out.append(lora_phy.Channel(float(f), float(bw), sfs))
    if not out:
        listed = ", ".join(f"{f / 1e6:.4f}" for f in job.freqs) or "(none given)"
        raise DecodeError(
            f"none of the frequencies ({listed} MHz) fit inside the usable part of this capture "
            f"({(cap.center_freq_hz - 0.4 * cap.sample_rate_hz) / 1e6:.4f}-"
            f"{(cap.center_freq_hz + 0.4 * cap.sample_rate_hz) / 1e6:.4f} MHz). Pass frequencies_hz, "
            "or recapture centred nearer them."
        )
    return out


def _slot_channels(job: Job) -> list[lora_phy.Channel]:
    """On the default slots, each preset at its own slot only."""
    cap = job.cap
    out = [
        lora_phy.Channel(float(slot_hz(p)), float(p.bw_hz), (p.sf,))
        for p in PRESETS
        if cap.covers(slot_hz(p), p.bw_hz / 2) and lora_phy.rate_fits(cap.sample_rate_hz, p.bw_hz)
    ]
    if not out:
        _channels(job, PRESETS)  # says why: the rate, or no slot inside the capture
        raise DecodeError(
            "no preset's default slot fits inside the usable part of this capture with its bandwidth; "
            "pass frequencies_hz, or recapture centred nearer a slot (LongFast 906.875 MHz)."
        )
    return out


def _scan(job: Job, channels: list[lora_phy.Channel]) -> tuple[list[lora_phy.Found], float, bool]:
    """(frames, seconds of the capture read, whether the timeout cut it)."""
    deadline = time.monotonic() + job.timeout
    read = [0]

    def chunks():
        for c in iq.iter_iq_chunks(job.cap.path, max_samples=job.cap.max_samples):
            read[0] += c.size
            yield c

    found = lora_phy.scan(chunks(), job.cap.sample_rate_hz, job.cap.center_freq_hz, channels, deadline)
    total = iq.iq_file_samples(job.cap.path)
    if job.cap.max_samples is not None:
        total = min(total, job.cap.max_samples)
    return found, read[0] / job.cap.sample_rate_hz, read[0] < total


def _frame_json(f: lora_phy.Found, extra: dict | None = None) -> str:
    fr = f.frame
    row = {
        "t": round(f.seconds, 4),
        "freq_hz": f.channel.freq_hz,
        "bw_hz": f.channel.bw_hz,
        "sf": fr.sf,
        "sync_word": fr.sync_word,
        "cfo_hz": round(fr.cfo_hz),
        "snr_db": round(fr.snr_db, 1),
        "length": fr.header.length if fr.header else None,
        "cr": fr.header.cr if fr.header else None,
        "crc_ok": fr.crc_ok,
    }
    row.update(extra or {})
    return json.dumps(row)


def _where(found_on: Counter) -> str:
    return ", ".join(f"{name} {freq / 1e6:.4f} MHz: {n}" for (name, freq), n in sorted(found_on.items()))


def run_meshtastic(job: Job) -> Result:
    on_slots = tuple(sorted(job.freqs)) == SLOTS_HZ
    channels = _slot_channels(job) if on_slots else _channels(job, PRESETS)
    found, seconds, cut = _scan(job, channels)
    notes = [f"(stopped at the timeout after {seconds:.1f} s of the capture)"] if cut else []
    raw, messages = [], []
    nodes: dict[int, dict] = {}
    on: Counter = Counter()
    failed = other = 0
    other_words: Counter = Counter()
    for f in found:
        fr = f.frame
        if fr.header is None:
            continue
        if fr.sync_word != SYNC_WORD:
            other += 1
            other_words[fr.sync_word] += 1
            raw.append(_frame_json(f))
            continue
        preset = BY_RADIO.get((int(f.channel.bw_hz), fr.sf))
        if fr.crc_ok is False:
            failed += 1
            raw.append(_frame_json(f, {"preset": preset.name if preset else None}))
            continue
        d = decode_packet(fr.payload, preset)
        if d is None:
            failed += 1
            continue
        p = d.packet
        name = preset.name if preset else f"SF{fr.sf}/{f.channel.bw_hz / 1e3:g}k"
        on[(name, f.channel.freq_hz)] += 1
        node = nodes.setdefault(p.sender, {"packets": 0})
        node["packets"] += 1
        node["last_s"] = f.seconds
        node["snr_db"] = fr.snr_db
        what = d.message.summary if d.message else f"encrypted (channel {p.channel:#04x}, {len(p.body)} bytes)"
        if d.message:
            if d.message.port == 4:
                node.update({k: v for k, v in d.message.info.items() if k in ("long_name", "short_name", "hw_model", "role")})
            elif d.message.port == 3 and "lat" in d.message.info:
                node["position"] = (d.message.info["lat"], d.message.info["lon"])
            elif d.message.port == 67:
                node.update({k: v for k, v in d.message.info.items() if k in ("battery_pct", "voltage")})
        to = "all" if p.to == BROADCAST else node_id(p.to)
        hops = f"hops {p.hop_start - p.hop_limit}/{p.hop_start}" if p.hop_start else f"hop limit {p.hop_limit}"
        flags = "".join([" want-ack" if p.want_ack else "", " via-MQTT" if p.via_mqtt else ""])
        relay = f" relayed by ..{p.relay:02x}" if p.relay and p.relay != p.sender & 0xFF else ""
        messages.append(
            f"  +{f.seconds:8.3f}s {name} {f.channel.freq_hz / 1e6:.4f} MHz {node_id(p.sender)} -> {to} "
            f"id {p.id:08x} {hops}{flags}{relay} SNR {fr.snr_db:.1f} dB: {what}"
        )
        raw.append(
            _frame_json(
                f,
                {
                    "preset": name,
                    "from": node_id(p.sender),
                    "to": "all" if p.to == BROADCAST else node_id(p.to),
                    "id": p.id,
                    "channel": p.channel,
                    "hop_limit": p.hop_limit,
                    "hop_start": p.hop_start,
                    "port": PORTS.get(d.port, d.port) if d.readable else None,
                    "info": d.message.info if d.message else None,
                },
            )
        )
    extra = []
    if failed:
        extra.append(f"{failed} more Meshtastic frame(s) failed their CRC")
    if other:
        words = ", ".join(f"{w:#04x}" + (" LoRaWAN" if w == 0x34 else "") for w in sorted(other_words))
        extra.append(f"{other} LoRa frame(s) with another sync word ({words}; try decoder lora)")
    if on_slots:
        searched = ", ".join(f"{BY_RADIO[(int(c.bw_hz), c.sfs[0])].name} {c.freq_hz / 1e6:.4f} MHz" for c in channels)
    else:
        searched = ", ".join(sorted({f"{c.freq_hz / 1e6:.4f}" for c in channels})) + " MHz (every preset that fits)"
    if not messages:
        headline = f"meshtastic: nothing decoded on {searched}"
        headline += (" (" + "; ".join(extra) + ")") if extra else ""
        notes.append(
            "Meshtastic nodes are quiet between their periodic reports; a longer capture, or a message sent "
            "from a node meanwhile, gives the decoder something to find."
        )
        return Result(headline + ".", [], [], "\n".join(raw), notes)
    headline = f"meshtastic: {len(messages)} packet(s) from {len(nodes)} node(s) ({_where(on)})"
    headline += (". " + "; ".join(extra)) if extra else ""
    table = []
    for num, n in sorted(nodes.items(), key=lambda kv: -kv[1]["packets"]):
        label = " ".join(
            x
            for x in (
                repr(n["short_name"]) if n.get("short_name") else "",
                repr(n["long_name"]) if n.get("long_name") else "",
                n.get("hw_model", ""),
                n.get("role", ""),
            )
            if x
        )
        extra_bits = []
        if "position" in n:
            extra_bits.append(f"position {n['position'][0]:.5f},{n['position'][1]:.5f}")
        if "battery_pct" in n:
            extra_bits.append(f"battery {n['battery_pct']}%")
        if "voltage" in n:
            extra_bits.append(f"{n['voltage']:.2f} V")
        table.append(
            f"  {node_id(num)} {label + ' ' if label else ''}{n['packets']} packet(s), last at +{n['last_s']:.1f}s, "
            f"SNR {n['snr_db']:.1f} dB" + (", " + ", ".join(extra_bits) if extra_bits else "")
        )
    return Result(headline + ".", table, messages, "\n".join(raw), notes)


def run_lora(job: Job) -> Result:
    if not job.freqs:
        raise DecodeError("lora needs the channel frequencies in frequencies_hz.")
    channels = _channels(job, None)
    found, seconds, cut = _scan(job, channels)
    notes = [f"(stopped at the timeout after {seconds:.1f} s of the capture)"] if cut else []
    good = [f for f in found if f.frame.header is not None]
    raw = [_frame_json(f, {"payload": f.frame.payload.hex()}) for f in good]
    if not good:
        return Result("lora: nothing decoded.", [], [], "", notes)
    lines = []
    for f in good:
        fr = f.frame
        crc = {True: "CRC ok", False: "CRC FAILED", None: "no CRC"}[fr.crc_ok]
        payload = fr.payload[:32].hex() + ("..." if len(fr.payload) > 32 else "")
        lines.append(
            f"  +{f.seconds:8.3f}s {f.channel.freq_hz / 1e6:.4f} MHz SF{fr.sf} {f.channel.bw_hz / 1e3:g} kHz "
            f"CR 4/{4 + fr.header.cr} sync {fr.sync_word:#04x} {fr.header.length} bytes {crc} "
            f"SNR {fr.snr_db:.1f} dB CFO {fr.cfo_hz / 1e3:+.1f} kHz: {payload}"
        )
    ok = sum(f.frame.crc_ok is not False for f in good)
    return Result(f"lora: {len(good)} frame(s), {ok} with a good CRC.", [], lines, "\n".join(raw), notes)



# --- The live profile ------------------------------------------------------

LIVE_RATE = 2_000_000
# Row kinds by app; anything else (and anything still encrypted) is "packet".
KINDS = {
    1: "text", 3: "position", 4: "nodeinfo", 5: "routing", 8: "waypoint", 66: "rangetest", 67: "telemetry",
    70: "traceroute", 71: "neighborinfo",
}


def live_channels(freqs: tuple[int, ...]) -> list[lora_phy.Channel]:
    """A preset's default slot is searched with that preset; any other
    frequency with every preset."""
    out = []
    for f in freqs:
        owners = [p for p in PRESETS if slot_hz(p) == f]
        if owners:
            out += [lora_phy.Channel(float(f), float(p.bw_hz), (p.sf,)) for p in owners]
            continue
        radios: dict[int, set[int]] = {}
        for p in PRESETS:
            radios.setdefault(p.bw_hz, set()).add(p.sf)
        out += [lora_phy.Channel(float(f), float(bw), tuple(sorted(sfs))) for bw, sfs in sorted(radios.items())]
    return out


def packet_fields(f: lora_phy.Found, rec: Received) -> tuple[str, dict, str]:
    """(row kind, fields, one-line summary) of a packet."""
    p, fr = rec.packet, f.frame
    preset = rec.preset or f"SF{fr.sf}/{f.channel.bw_hz / 1e3:g}k"
    fields: dict = {
        "from": node_id(p.sender),
        "to": "all" if p.to == BROADCAST else node_id(p.to),
        "id": f"{p.id:08x}",
        "hops": p.hop_start - p.hop_limit if p.hop_start else None,
        "hop_start": p.hop_start or None,
        "channel": f"{p.channel:#04x}",
        "preset": preset,
        "snr_db": round(fr.snr_db, 1),
        "port": PORTS.get(rec.port, f"PORT_{rec.port}") if rec.readable else None,
        "encrypted": not rec.readable,
    }
    if rec.message:
        for key, value in rec.message.info.items():
            fields[key] = " > ".join(value) if isinstance(value, list) else value
        what = rec.message.summary
    else:
        what = f"encrypted (channel {p.channel:#04x}, {len(p.body)} bytes)"
    kind = KINDS.get(rec.port, "packet") if rec.readable else "packet"
    return kind, fields, f"{node_id(p.sender)} -> {fields['to']} {preset}: {what}"


class MeshtasticPipeline(Pipeline):
    """The built-in LoRa receiver on the live stream. The source's thread
    only queues the blocks (dropping, and counting, what the decoder can't
    keep up with, so the HackRF is never held up); a worker thread runs the
    scanner, which reports each frame a few seconds after it ends."""

    QUEUE_SECONDS = 10.0

    def start(self) -> None:
        t = self.tuning
        self.rate = t.sample_rate_hz
        self.scanner = lora_phy.Scanner(self.rate, t.center_hz, live_channels(t.freqs_hz), self._found)
        self.throttle = PositionThrottle(t.profile.name, self.ctx.emit_position)
        self.names: dict[int, str] = {}
        self.cv = threading.Condition()
        self.queue: collections.deque[tuple[bytes, float]] = collections.deque()
        self.queued = 0  # bytes
        self.max_bytes = int(self.QUEUE_SECONDS * 2 * self.rate)
        # (samples fed once this block is in, when it arrived), to date frames.
        self.arrivals: collections.deque[tuple[int, float]] = collections.deque(maxlen=8192)
        self.stats = {"packets": 0, "crc_failed": 0, "other_lora": 0, "dropped_blocks": 0}
        self.error: str | None = None
        self.source = self.ctx.make_source(t)
        # A file (a replay) can wait for the decoder; the HackRF can't.
        self.blocking = isinstance(self.source, live_profiles.FileSource)
        self.started = time.time()
        self._thread(self._work, "meshtastic-decode")
        self.source.start(self._feed)

    def _feed(self, buf: bytes) -> None:
        with self.cv:
            while self.blocking and self.queued + len(buf) > self.max_bytes and not self._stop.is_set():
                self.cv.wait(0.2)
            if self.queued + len(buf) > self.max_bytes:
                self.stats["dropped_blocks"] += 1
                return
            self.queue.append((buf, time.time()))
            self.queued += len(buf)
            self.cv.notify_all()

    def _work(self) -> None:
        try:
            while True:
                with self.cv:
                    while not self.queue and not self._stop.is_set():
                        self.cv.wait(0.2)
                    if not self.queue:
                        return  # stopped, and nothing left
                    buf, at = self.queue.popleft()
                    self.queued -= len(buf)
                    self.cv.notify_all()
                x = iq.cs8_to_complex(buf)
                self.arrivals.append((self.scanner.samples + x.size, at))
                self.scanner.feed(x)
        except Exception as exc:  # reported by failure(); the service restarts the profile
            self.error = f"meshtastic decoder: {exc!r}"

    def _ts(self, seconds: float) -> float:
        """When a frame was on the air: live, from when its samples arrived;
        in a replay (read as fast as it decodes), from where it is in the
        file."""
        if self.blocking:
            return self.started + seconds
        s = seconds * self.rate
        for end, at in self.arrivals:
            if end >= s:
                return at - (end - s) / self.rate
        return time.time()

    def _found(self, f: lora_phy.Found) -> None:
        fr = f.frame
        if fr.header is None:
            return
        if fr.sync_word != SYNC_WORD:
            self.stats["other_lora"] += 1
            return
        rec = decode_packet(fr.payload, BY_RADIO.get((int(f.channel.bw_hz), fr.sf))) if fr.crc_ok is not False else None
        if rec is None:
            self.stats["crc_failed"] += 1
            return
        ts = self._ts(f.seconds)
        kind, fields, line = packet_fields(f, rec)
        sender = rec.packet.sender
        if kind == "nodeinfo" and (fields.get("long_name") or fields.get("short_name")):
            self.names[sender] = fields.get("long_name") or fields.get("short_name")
        if sender in self.names:
            fields["name"] = self.names[sender]
        subject = node_id(sender)
        self.ctx.emit(
            Row(ts, self.tuning.profile.name, kind, fields, raw=line, freq_hz=int(f.channel.freq_hz),
                subject=subject, label=self.names.get(sender))
        )
        self.ctx.count(1)
        self.stats["packets"] += 1
        if kind == "position" and "lat" in fields:
            self.throttle.offer(subject, ts, fields["lat"], fields["lon"])

    def finish(self) -> None:
        # The frames still in the scanner's buffers, once the worker is done.
        if self.error is None and not any(t.is_alive() for t in self._threads):
            try:
                self.scanner.finish()
            except Exception as exc:
                self.error = f"meshtastic decoder: {exc!r}"

    def finished(self) -> bool:
        with self.cv:
            idle = not self.queue
        return super().finished() and idle

    def failure(self) -> str | None:
        return self.error or super().failure()

    def health(self) -> dict:
        h = super().health()
        if hasattr(self, "scanner"):
            h.update(self.stats)
            h["queue_s"] = round(self.queued / (2 * self.rate), 1)
            levels = {}
            for bw, tracks in self.scanner.groups.items():
                for i, t in enumerate(tracks):
                    p = self.scanner.splitters[bw].power_db[i]
                    key = f"{t.channel.freq_hz / 1e6:.4f}"
                    if p != float("-inf") and key not in levels:
                        levels[key] = round(p, 1)
            h["channel_levels_dbfs"] = levels
        return h


SLOTS_HZ = tuple(sorted({slot_hz(p) for p in PRESETS}))

decoders.register(
    Decoder(
        "meshtastic",
        (),
        "Meshtastic mesh radios on 902-928 MHz (node names, positions, telemetry, text sent to everyone)",
        "Tune about 300 kHz below the preset's slot (LongFast 906.875 MHz: 906.575 MHz) at 2 MS/s; 60 s or more.",
        run_meshtastic,
        default_freqs_hz=SLOTS_HZ,
        help="""  meshtastic - Meshtastic mesh radios, every modem preset; the default
          slots (LongFast 906.875 MHz) unless frequencies_hz says otherwise;
          capture at a multiple of 2,000,000 S/s, 60 s or more.
  lora  - any LoRa (SF 7-12, 125/250/500 kHz) on the frequencies_hz given.""",
    )
)
decoders.register(
    Decoder(
        "lora",
        (),
        "LoRa frames of any kind (SF 7-12 at 125, 250 or 500 kHz) on given frequencies, as hex",
        "Capture at a multiple of 2 MS/s with the channel inside the capture; pass it in frequencies_hz.",
        run_lora,
    )
)

live_profiles.register(
    Profile(
        "meshtastic",
        "Meshtastic mesh radios (the built-in LoRa receiver), LongFast's US slot by default; "
        "one row per packet: node names, positions, telemetry, text sent to everyone",
        (),
        MeshtasticPipeline,
        LIVE_RATE,
        None,
        (slot_hz(PRESETS[0]),),
        channel_bw_hz=500_000,
        help="""  meshtastic - Meshtastic mesh radios at 2 MS/s: LongFast's US slot,
           906.875 MHz, unless frequencies_hz names others (a preset's
           slot decodes that preset; any other frequency, every preset);
           one row per packet, positions on the map.""",
        summary_kinds=tuple(dict.fromkeys(KINDS.values())) + ("packet",),
        summary_fields=(
            "name", "short_name", "hw_model", "role", "text", "lat", "lon", "alt_m", "battery_pct", "voltage",
            "temperature_c", "snr_db", "hops", "preset",
        ),
        views=(
            View(
                "map",
                ("position",),
                ("name", "alt_m", "sats", "precision_bits", "hops", "snr_db"),
                (("lat", "lat"), ("lon", "lon"), ("label", "name")),
            ),
            View("table", (), ("name", "port", "to", "text", "hw_model", "battery_pct", "hops", "snr_db", "preset")),
            View("timeline"),
        ),
    )
)
