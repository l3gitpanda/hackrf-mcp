"""ADS-B / Mode S aircraft on 1090 MHz, with dump1090: offline decoding
of captures (hackrf_decode) and the live `adsb` profile, which logs one
row per aircraft per minute."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from hackrf_mcp import decoders, iq, live_profiles
from hackrf_mcp.decoders import AutoGain, Capture, DecodeError, Decoder, Result, nothing, sc16
from hackrf_mcp.live_log import Row
from hackrf_mcp.live_profiles import MinuteAggregator, Pipeline, Proc, Profile


_ADSB_FIELDS = {
    "Ident": "callsign",
    "Squawk": "squawk",
    "Altitude": "altitude",
    "Speed": "speed",
    "Heading": "heading",
    "Category": "category",
}


def parse_dump1090(text: str) -> tuple[dict[str, dict], int]:
    """Aggregate dump1090's verbose message blocks per ICAO address.
    Returns (aircraft, messages).

    dump1090 prints only messages it accepted. Its "CRC:" line is not a
    pass/fail flag: for address/parity replies (DF0/4/5/16/20/21) it is the
    aircraft's address, and for DF11 the interrogator ID, so it is ignored."""
    aircraft: dict[str, dict] = {}
    good = 0
    for block in re.split(r"\n\s*\n", text):
        if "ICAO Address:" not in block:
            continue
        icao_m = re.search(r"ICAO Address:\s*([0-9A-Fa-f]{6})", block)
        if not icao_m:
            continue
        good += 1
        icao = icao_m.group(1).upper()
        a = aircraft.setdefault(icao, {"messages": 0, "positions": 0})
        a["messages"] += 1
        if rssi := re.search(r"RSSI:\s*(-?[\d.]+)", block):
            a["rssi"] = max(a.get("rssi", -999.0), float(rssi.group(1)))
        for label, key in _ADSB_FIELDS.items():
            if fm := re.search(rf"^\s*{label}:\s*(.+?)\s*$", block, re.M):
                a[key] = fm.group(1)
        decoding = re.search(r"CPR decoding:\s*(\w+)", block)
        lat = re.search(r"CPR latitude:\s*(-?\d+\.\d+)", block)
        lon = re.search(r"CPR longitude:\s*(-?\d+\.\d+)", block)
        if decoding and decoding.group(1) in ("global", "local") and lat and lon:
            a["lat"], a["lon"] = float(lat.group(1)), float(lon.group(1))
            a["positions"] += 1
    return aircraft, good


def decode_adsb(cap: Capture, program: str, timeout: float, workdir: Path, receiver: tuple | None) -> Result:
    rate = cap.sample_rate_hz
    target = 1_090_000_000
    if not cap.covers(target):
        raise DecodeError("1090 MHz is outside this capture; capture at 1090 MHz.")
    shift = target - cap.center_freq_hz
    notes = []
    if rate == 2_400_000 and shift == 0:
        stdin = iq.iter_cu8_chunks(cap.path, max_samples=cap.max_samples)
        fmt, out_rate = "UC8", rate
    elif rate % 2_400_000 == 0:
        out_rate = 2_400_000
        gain = AutoGain(0.5)
        stdin = (
            sc16(gain(c))
            for c in iq.channelize(cap.path, rate, shift, rate // out_rate, cap.max_samples, bandwidth=0.9)
        )
        fmt = "SC16"
        notes.append(
            f"Converted {rate / 1e6:g} MS/s to {out_rate / 1e6:g} MS/s for the decoder; a capture made "
            "at 2.4 MS/s directly decodes best."
        )
    else:
        # On 2026-10-29 the QNAP's HackRF gave dump1090 nothing at 2.0 MS/s,
        # recorded directly or decimated from 8 MS/s, while 2.4 MS/s a
        # minute later decoded 746 messages from 12 aircraft.
        raise DecodeError(
            f"ADS-B needs a capture at 2,400,000 S/s or a multiple of it; this one is {rate} S/s "
            "(2.0 MS/s decodes nothing with the HackRF). Recapture at 1090 MHz, 2.4 MS/s."
        )
    args = [program, "--ifile", "-", "--iformat", fmt, "--stats", "--oversample"]
    if receiver:
        args += ["--lat", str(receiver[0]), "--lon", str(receiver[1])]
    run = decoders.run_program(args, stdin, timeout, workdir)
    aircraft, good = parse_dump1090(run.output)
    if not aircraft:
        return nothing("adsb", run, notes)
    with_pos = sum(1 for a in aircraft.values() if a["positions"])
    table = []
    for icao, a in sorted(aircraft.items(), key=lambda kv: -kv[1]["messages"]):
        parts = [icao]
        for key in ("callsign", "squawk", "altitude", "speed", "heading", "category"):
            if key in a:
                parts.append(f"{key}={a[key]}")
        if "lat" in a:
            parts.append(f"pos={a['lat']:.4f},{a['lon']:.4f}")
        if "rssi" in a:
            parts.append(f"rssi={a['rssi']:.1f}dBFS")
        parts.append(f"msgs={a['messages']}")
        table.append("  " + " ".join(parts))
    return Result(
        f"adsb: {len(aircraft)} aircraft, {good} messages decoded, {with_pos} aircraft with a "
        f"position ({run.seconds:.0f} s to decode).",
        table,
        [],
        run.output,
        notes,
    )


# --- Live ------------------------------------------------------------------


class AdsbAggregator(MinuteAggregator):
    """Per-aircraft summaries from dump1090's aircraft.json, polled every
    few seconds: callsign, squawk, altitude range, last position, speed,
    track, strongest RSSI and how many messages arrived that minute."""

    def __init__(self, emit: Callable[[Row], None], poll_s: float = 5.0):
        super().__init__(emit)
        self.poll_s = poll_s
        self.prev_messages: dict[str, int] = {}
        self.total_messages: int | None = None

    def update(self, now: float, snapshot: dict) -> int:
        """Fold in one aircraft.json; returns messages decoded since the last."""
        self._roll(now)
        new_total = int(snapshot.get("messages", 0) or 0)
        # dump1090 starts with the pipeline, so its first total is all new.
        decoded = new_total if self.total_messages is None else max(0, new_total - self.total_messages)
        self.total_messages = new_total
        seen_now = set()
        minute_start = (self.minute or 0) * 60
        for a in snapshot.get("aircraft", []):
            icao = str(a.get("hex", "")).strip().upper()
            if not icao:
                continue
            seen_now.add(icao)
            msgs = int(a.get("messages", 0) or 0)
            prev = self.prev_messages.get(icao)
            delta = msgs if prev is None or msgs < prev else msgs - prev
            self.prev_messages[icao] = msgs
            seen = float(a.get("seen", 1e9))
            if seen > self.poll_s + 1:
                continue  # not heard since the last poll
            heard = max(minute_start, now - seen)
            e = self.entries.get(icao)
            if e is None:
                e = self.entries[icao] = {"first": heard, "last": heard, "messages": 0, "positions": 0}
            e["first"] = min(e["first"], heard)
            e["last"] = max(e["last"], heard)
            e["messages"] += max(delta, 0)
            if flight := str(a.get("flight", "")).strip():
                e["callsign"] = flight
            for key in ("squawk", "category", "speed", "track", "vert_rate"):
                if a.get(key) not in (None, ""):
                    e[key] = a[key]
            alt = a.get("altitude")
            if isinstance(alt, (int, float)):
                e["altitude"] = int(alt)
                e["altitude_min"] = min(e.get("altitude_min", int(alt)), int(alt))
                e["altitude_max"] = max(e.get("altitude_max", int(alt)), int(alt))
            elif alt == "ground":
                e["on_ground"] = True
            if a.get("lat") is not None and a.get("lon") is not None and float(a.get("seen_pos", 1e9)) <= self.poll_s + 1:
                e["lat"], e["lon"] = round(float(a["lat"]), 5), round(float(a["lon"]), 5)
                e["positions"] += 1
            if a.get("rssi") is not None:
                e["rssi"] = max(e.get("rssi", -1e9), float(a["rssi"]))
        for icao in list(self.prev_messages):
            if icao not in seen_now:
                del self.prev_messages[icao]
        return decoded

    def row(self, icao: str, e: dict) -> Row:
        fields = {"icao": icao}
        for key in (
            "callsign",
            "squawk",
            "altitude",
            "altitude_min",
            "altitude_max",
            "on_ground",
            "speed",
            "track",
            "vert_rate",
            "lat",
            "lon",
            "category",
            "messages",
            "positions",
        ):
            if key in e:
                fields[key] = e[key]
        parts = [icao]
        if "callsign" in e:
            parts.append(e["callsign"])
        if "squawk" in e:
            parts.append(f"sq {e['squawk']}")
        if "altitude_min" in e:
            lo, hi = e["altitude_min"], e["altitude_max"]
            parts.append(f"alt {lo}" if lo == hi else f"alt {lo}-{hi}")
        elif e.get("on_ground"):
            parts.append("on ground")
        if "lat" in e:
            parts.append(f"pos {e['lat']:.4f},{e['lon']:.4f}")
        if "speed" in e:
            parts.append(f"spd {e['speed']}")
        if "track" in e:
            parts.append(f"trk {e['track']}")
        parts.append(f"msgs {e['messages']}")
        if "rssi" in e:
            parts.append(f"rssi {e['rssi']:.1f}")
        return Row(
            ts=e["first"],
            ts_end=e["last"],
            profile="adsb",
            kind="aircraft",
            freq_hz=1_090_000_000,
            level_db=e.get("rssi"),
            subject=icao,
            label=e.get("callsign"),
            fields=fields,
            raw=" ".join(parts),
        )


class AdsbPipeline(Pipeline):
    def start(self) -> None:
        t = self.tuning
        json_dir = self.ctx.run_dir / "adsb"
        if json_dir.exists():
            for f in json_dir.iterdir():
                if f.is_file():
                    f.unlink()
        json_dir.mkdir(parents=True, exist_ok=True)
        args = [t.program, "--ifile", "-", "--iformat", "UC8", "--quiet", "--write-json", str(json_dir), "--write-json-every", "1"]
        if t.sample_rate_hz == 2_400_000:
            args.append("--oversample")
        if self.ctx.settings.receiver:
            args += ["--lat", str(self.ctx.settings.receiver[0]), "--lon", str(self.ctx.settings.receiver[1])]
        decoder = Proc("dump1090", args, stdin=True)
        self.procs.append(decoder)
        self.agg = AdsbAggregator(self.ctx.emit)
        self.json_path = json_dir / "aircraft.json"
        self.source = self.ctx.make_source(t)
        self.started = time.time()

        def feed(buf: bytes) -> None:
            if not decoder.write(iq.cs8_to_cu8(buf)):
                raise BrokenPipeError("dump1090 stopped reading")

        self.source.start(feed)
        self._thread(self._poll, "adsb-poll")

    def _poll(self) -> None:
        while not self._stop.wait(self.agg.poll_s):
            self._read()

    def _read(self) -> None:
        try:
            text = self.json_path.read_text()
            snapshot = json.loads(text)
        except (OSError, ValueError):
            return
        self.ctx.count(self.agg.update(time.time(), snapshot))

    def finish(self) -> None:
        if hasattr(self, "agg"):
            self._read()
            self.agg.flush()


decoders.register(
    Decoder(
        "adsb",
        ("dump1090-mutability", "dump1090"),
        "ADS-B / Mode S aircraft transponders (1090 MHz)",
        "1090 MHz at 2.4 MS/s; 10-60 s. Other multiples of 2.4 MS/s are decimated.",
        lambda job: decode_adsb(job.cap, job.program, job.timeout, job.workdir, job.receiver),
        (1_090_000_000,),
        help="""  adsb  - aircraft, 1090 MHz at 2,400,000 S/s (2,000,000 decodes nothing).
          Optional receiver_lat/receiver_lon resolve more positions.""",
    )
)

live_profiles.register(
    Profile(
        "adsb",
        "ADS-B / Mode S aircraft on 1090 MHz (dump1090); one row per aircraft per minute",
        ("dump1090-mutability", "dump1090"),
        AdsbPipeline,
        2_400_000,
        1_090_000_000,
        (1_090_000_000,),
        fixed_freqs=True,
        help="""  adsb   - aircraft, 1090 MHz at 2.4 MS/s (dump1090); one log row per
           aircraft per minute: callsign, squawk, altitude range, last
           position, speed, track, messages, strongest RSSI.""",
        summary_kinds=("aircraft",),
        summary_fields=("callsign", "squawk", "altitude_min", "altitude_max", "lat", "lon", "speed", "messages"),
    )
)
