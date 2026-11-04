"""Meshtastic (and plain LoRa) capture decoding: the slots and channel
hashes, the packet header, the default-channel decryption, the protobuf
reader, and whole captures through hackrf_decode. The packets here are
built by a small encoder written from the field numbers, encrypted as the
firmware describes (AES-CTR, nonce = packet id, sender, zeros), and sent
through the test LoRa transmitter."""

import json
import struct

import numpy as np
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lora_tx import encode, modulate

from hackrf_mcp import decoders, iq, lora_phy, selfcheck, server
from hackrf_mcp.protocols import meshtastic as M

# --- A Meshtastic packet builder ------------------------------------------


def varint(v: int) -> bytes:
    v &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def fv(num, v):  # varint field
    return varint(num << 3) + varint(v)


def fb(num, b):  # length-delimited field
    return varint(num << 3 | 2) + varint(len(b)) + b


def f32(num, v):  # fixed32 / sfixed32 / float field
    return varint(num << 3 | 5) + struct.pack("<I", v & 0xFFFFFFFF)


def ffloat(num, x):
    return varint(num << 3 | 5) + struct.pack("<f", x)


def data(port: int, payload: bytes) -> bytes:
    return fv(1, port) + fb(2, payload)


def packet(sender, pid, body_plain, to=0xFFFFFFFF, channel=0x08, hop_limit=3, hop_start=3, key=M.DEFAULT_KEY):
    flags = hop_limit | (hop_start << 5)
    head = struct.pack("<IIIBBBB", to, sender, pid, flags, channel, 0, sender & 0xFF)
    nonce = pid.to_bytes(8, "little") + sender.to_bytes(4, "little") + bytes(4)
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return head + enc.update(body_plain) + enc.finalize()


SENDER = 0xA1B2C3D4
NODEINFO = data(4, fb(1, b"!a1b2c3d4") + fb(2, b"Test Node") + fb(3, b"TST") + fv(5, 43) + fv(7, 0))
POSITION = data(3, f32(1, 371234567) + f32(2, -1221234567) + fv(3, 42) + fv(23, 32))
TEXT = data(1, "hello mesh".encode())
TELEMETRY = data(67, fb(2, fv(1, 87) + ffloat(2, 4.05) + ffloat(3, 3.5)) + f32(1, 1_700_000_000))


# --- Pieces ----------------------------------------------------------------


def test_default_slots_and_channel_hashes():
    slot = {p.name: M.slot_hz(p) for p in M.PRESETS}
    assert slot["LongFast"] == 906_875_000
    assert slot["MediumFast"] == 913_125_000
    assert slot["LongTurbo"] == 908_750_000
    assert all(902_000_000 < f < 928_000_000 for f in slot.values())
    assert M.channel_hash("LongFast", M.DEFAULT_KEY) == 0x08
    assert M.channel_hash("MediumFast", M.DEFAULT_KEY) == 0x1F
    assert decoders.DECODERS["meshtastic"].default_freqs_hz == tuple(sorted(set(slot.values())))


def test_header_fields():
    raw = struct.pack("<IIIBBBB", 0x11223344, SENDER, 0xDEADBEEF, 0b011_1_1_010, 0x08, 0x55, 0xD4) + b"x"
    p = M.parse_packet(raw)
    assert (p.to, p.sender, p.id, p.hop_limit, p.want_ack, p.via_mqtt, p.hop_start) == (
        0x11223344, SENDER, 0xDEADBEEF, 2, True, True, 3
    )
    assert (p.channel, p.next_hop, p.relay, p.body) == (0x08, 0x55, 0xD4, b"x")
    assert M.parse_packet(b"short") is None


def test_default_channel_packets_decrypt():
    got = M.decode_packet(packet(SENDER, 0x1234, NODEINFO), M.BY_RADIO[(250_000, 11)])
    assert got.readable and got.port == 4
    assert got.message.info["long_name"] == "Test Node" and got.message.info["hw_model"] == "HELTEC_V3"
    # Another channel (its hash differs) stays encrypted, and so does the
    # default channel's hash under another key: it decrypts to nonsense.
    assert not M.decode_packet(packet(SENDER, 0x1234, NODEINFO, channel=0x2A), M.BY_RADIO[(250_000, 11)]).readable
    other = packet(SENDER, 0x1234, NODEINFO, key=bytes(16))
    assert M.decode_packet(other, M.BY_RADIO[(250_000, 11)]).port != 4


def test_what_each_port_shows():
    pos = M.describe(3, M.fields(POSITION)[2][0])
    assert pos.info["lat"] == pytest.approx(37.1234567) and pos.info["lon"] == pytest.approx(-122.1234567)
    assert pos.info["alt_m"] == 42 and pos.info["precision_bits"] == 32
    tele = M.fields(TELEMETRY)
    t = M.describe(67, tele[2][0])
    assert t.info["battery_pct"] == 87 and t.info["voltage"] == pytest.approx(4.05)
    assert M.describe(1, b"hi all").summary == "TEXT 'hi all'"
    waypoint = M.describe(8, f32(2, 371234567) + f32(3, -1221234567) + fb(6, b"Camp") + fb(7, b"by the lake"))
    assert (waypoint.info["name"], waypoint.info["description"]) == ("Camp", "by the lake")
    route = M.describe(70, fb(1, struct.pack("<II", 0x11111111, 0x22222222)))
    assert route.info["route"] == ["!11111111", "!22222222"]
    assert M.describe(5, fv(3, 0)).summary == "ROUTING ack"
    assert "unreadable" in M.describe(3, b"\x0a\xff").summary


def test_protobuf_reader():
    f = M.fields(fv(1, 300) + fb(2, b"ab") + f32(3, 7) + varint(4 << 3 | 1) + bytes(8))
    assert f == {1: [300], 2: [b"ab"], 3: [7], 4: [0]}
    for bad in (b"\x08", b"\x12\x05ab", b"\x00\x01", b"\x0b"):
        with pytest.raises(M.ProtoError):
            M.fields(bad)


# --- Whole captures --------------------------------------------------------


def _capture(tmp_path, center, frames, seconds, fs=2_000_000, name="m.cs8"):
    """frames: (freq, preset, LoRa payload, start s, cfo Hz[, sync word])."""
    total = int(seconds * fs)
    x = np.zeros(total, np.complex64)
    for freq, preset, raw, start, cfo, *rest in frames:
        sync = rest[0] if rest else 0x2B
        os = fs // preset.bw_hz
        syms = encode(raw, preset.sf, preset.cr, ldro=lora_phy.ldro_default(preset.sf, preset.bw_hz))
        y = modulate(syms, preset.sf, bw_hz=preset.bw_hz, cfo_hz=cfo, os=os, lead=start * preset.bw_hz + 0.3,
                     tail=0, amplitude=0.05, sync_word=sync, fc_hz=freq)[:total]
        x[: y.size] += y * np.exp(2j * np.pi * (freq - center) / fs * np.arange(y.size)).astype(np.complex64)
    rng = np.random.default_rng(5)
    x += (0.01 * (rng.standard_normal(total) + 1j * rng.standard_normal(total))).astype(np.complex64)
    path = tmp_path / name
    path.write_bytes(iq.complex_to_cs8(x, 127 / 0.15))
    return path


MEDIUM = M.BY_RADIO[(250_000, 9)]
LONG = M.BY_RADIO[(250_000, 11)]
MEDIUM_HZ = 913_125_000


def test_a_capture_decodes_through_hackrf_decode(tmp_path):
    frames = [
        (MEDIUM_HZ, MEDIUM, packet(SENDER, 0x101, NODEINFO, channel=0x1F), 0.02, 4200),
        (MEDIUM_HZ, MEDIUM, packet(SENDER, 0x102, POSITION, channel=0x1F), 0.20, 4200),
        (MEDIUM_HZ, MEDIUM, packet(SENDER, 0x103, TEXT, channel=0x1F), 0.40, 4200),
        (MEDIUM_HZ, MEDIUM, packet(0x0BADF00D, 0x104, TEXT, to=SENDER, channel=0x1F), 0.60, -3000),
        (MEDIUM_HZ, MEDIUM, packet(SENDER, 0x105, TELEMETRY, channel=0x1F), 0.80, 4200),
        (MEDIUM_HZ, MEDIUM, b"\x40" + bytes(range(20)), 1.00, 1000, 0x34),  # LoRaWAN, not Meshtastic
    ]
    path = _capture(tmp_path, MEDIUM_HZ - 300_000, frames, 1.25)
    out = server.hackrf_decode("meshtastic", str(path), 2_000_000, MEDIUM_HZ - 300_000, output_path="m.json")
    assert out.startswith("meshtastic: 5 packet(s) from 2 node(s) (MediumFast 913.1250 MHz: 5)")
    assert "1 LoRa frame(s) with another sync word (0x34 LoRaWAN; try decoder lora)" in out
    assert "!a1b2c3d4 'TST' 'Test Node' HELTEC_V3 CLIENT 4 packet(s)" in out
    assert "position 37.12346,-122.12346" in out and "battery 87%, 4.05 V" in out
    assert "!a1b2c3d4 -> all id 00000103 hops 0/3" in out and "TEXT 'hello mesh'" in out
    # Every text it can read is shown, a direct message's too.
    assert "!0badf00d -> !a1b2c3d4" in out and out.count("TEXT 'hello mesh'") == 2
    rows = [json.loads(line) for line in (tmp_path / "m.json").read_text().splitlines()]
    assert [r.get("port") for r in rows] == ["NODEINFO", "POSITION", "TEXT", "TEXT", "TELEMETRY", None]
    assert rows[1]["info"]["lat"] == pytest.approx(37.1234567)


def test_longfast_at_its_default_slot(tmp_path):
    path = _capture(tmp_path, 906_575_000, [(906_875_000, LONG, packet(SENDER, 7, TEXT), 0.05, -9000)], 0.75)
    out = server.hackrf_decode("meshtastic", str(path), 2_000_000, 906_575_000)
    assert "(LongFast 906.8750 MHz: 1)" in out and "TEXT 'hello mesh'" in out


def test_nothing_on_the_air(tmp_path):
    path = _capture(tmp_path, 906_575_000, [], 0.3)
    out = server.hackrf_decode("meshtastic", str(path), 2_000_000, 906_575_000)
    assert out.startswith("meshtastic: nothing decoded on LongFast 906.8750 MHz.")
    # Frequencies given are tried with every preset that fits.
    out = server.hackrf_decode("meshtastic", str(path), 2_000_000, 906_575_000, frequencies_hz=[906_875_000])
    assert out.startswith("meshtastic: nothing decoded on 906.8750 MHz (every preset that fits).")


def test_a_failed_crc_is_counted_not_shown(tmp_path):
    raw = packet(SENDER, 9, TEXT, channel=0x1F)
    syms = encode(raw, MEDIUM.sf, MEDIUM.cr)
    syms[20] = (syms[20] + 9) % (1 << MEDIUM.sf)
    x = modulate(syms, MEDIUM.sf, bw_hz=250e3, os=8, lead=5000.5, amplitude=0.05, fc_hz=MEDIUM_HZ)
    x = x * np.exp(2j * np.pi * 300_000 / 2_000_000 * np.arange(x.size)).astype(np.complex64)
    path = tmp_path / "bad.cs8"
    path.write_bytes(iq.complex_to_cs8(x, 127 / 0.15))
    out = server.hackrf_decode("meshtastic", str(path), 2_000_000, MEDIUM_HZ - 300_000)
    assert "nothing decoded" in out and "1 more Meshtastic frame(s) failed their CRC" in out


def test_lora_shows_any_frame_as_hex(tmp_path):
    raw = b"\x40\x01\x02\x03\x04"
    path = _capture(tmp_path, MEDIUM_HZ - 300_000, [(MEDIUM_HZ, MEDIUM, raw, 0.05, 0, 0x34)], 0.3)
    out = server.hackrf_decode("lora", str(path), 2_000_000, MEDIUM_HZ - 300_000, frequencies_hz=[MEDIUM_HZ])
    assert out.startswith("lora: 1 frame(s), 1 with a good CRC.")
    assert "SF9 250 kHz CR 4/5 sync 0x34 5 bytes CRC ok" in out and raw.hex() in out
    out = server.hackrf_decode("lora", str(path), 2_000_000, MEDIUM_HZ - 300_000)
    assert "needs the channel frequencies" in out


def test_rates_and_frequencies_are_checked(tmp_path):
    path = tmp_path / "r.cs8"
    path.write_bytes(bytes(4000))
    assert "multiple of 2 MS/s" in server.hackrf_decode("meshtastic", str(path), 2_400_000, 906_875_000)
    out = server.hackrf_decode("meshtastic", str(path), 2_000_000, 924_000_000)  # no slot within reach
    assert "none of the frequencies (902.6875, 905.3125, 906.8750" in out
    out = server.hackrf_decode("meshtastic", str(path), 2_000_000, 924_000_000, frequencies_hz=[906_875_000])
    assert "none of the frequencies" in out


# --- Built in: no program --------------------------------------------------


def test_it_is_built_in(monkeypatch):
    for name in ("meshtastic", "lora"):
        assert decoders.find_program(decoders.DECODERS[name]) == decoders.BUILT_IN
        assert decoders.available()[name] == decoders.BUILT_IN
    assert "meshtastic (built-in)" in server.hackrf_server_status()
    monkeypatch.setattr(decoders, "available", lambda: {"meshtastic": decoders.BUILT_IN})
    monkeypatch.setattr(selfcheck, "OPTIONAL", set())
    monkeypatch.setattr(selfcheck.shutil, "which", lambda name: "/usr/bin/ldd")
    monkeypatch.setattr(selfcheck.subprocess, "run", lambda *a, **k: pytest.fail("ldd run on a built-in"))
    from hackrf_mcp import live_profiles

    monkeypatch.setattr(live_profiles, "available", lambda: {})
    lines, problems = selfcheck.check()
    assert problems == [] and "ok       decoder meshtastic: built-in" in lines
