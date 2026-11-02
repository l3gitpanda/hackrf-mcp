"""AIS ships, base stations and aids to navigation on 161.975/162.025 MHz,
with AIS-catcher: offline decoding and the live `ais` profile, in which
AIS-catcher reads hackrf_transfer's samples on its stdin, like every other
live decoder, so AIS gets the spectrum history and the stall and input-rate
checks too."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from hackrf_mcp import decoders, iq, live_profiles
from hackrf_mcp.decoders import Capture, DecodeError, Decoder, Result, json_lines, nothing
from hackrf_mcp.live_log import Row
from hackrf_mcp.live_profiles import MinuteAggregator, Pipeline, PositionThrottle, Proc, Profile, View, line_buffered


def decode_ais(cap: Capture, program: str, timeout: float, workdir: Path) -> Result:
    center = 162_000_000
    if not (cap.covers(161_975_000) and cap.covers(162_025_000)):
        raise DecodeError("AIS channels 161.975/162.025 MHz are outside this capture; capture at 162.0 MHz.")
    args = [program, "-o", "5", "-s", str(cap.sample_rate_hz)]
    whole_file = cap.max_samples is None or cap.max_samples >= iq.iq_file_samples(cap.path)
    if cap.center_freq_hz == center and whole_file:
        args += ["-r", "CS8", str(cap.path)]
        stdin = None
    else:
        args += ["-r", "CS8", "."]
        mixer_chunks = iq.channelize(
            cap.path, cap.sample_rate_hz, center - cap.center_freq_hz, 1, cap.max_samples
        )
        stdin = (iq.complex_to_cs8(c, 128.0) for c in mixer_chunks)
    run = decoders.run_program(args, stdin, timeout, workdir)
    events = [e for e in json_lines(run.output) if e.get("class") == "AIS"]
    if not events:
        return nothing("ais", run, [])
    ships: dict[int, dict] = {}
    for e in events:
        s = ships.setdefault(e.get("mmsi", 0), {"messages": 0, "types": set()})
        s["messages"] += 1
        s["types"].add(e.get("type"))
        for key in ("shipname", "name", "callsign", "shiptype_text", "aid_type_text", "status_text", "destination"):
            if e.get(key):
                s[key] = str(e[key]).strip()
        if e.get("lat") is not None and e.get("lon") is not None and abs(e["lat"]) <= 90:
            s["lat"], s["lon"] = e["lat"], e["lon"]
        if e.get("speed") is not None and e["speed"] < 102.3:
            s["speed"] = e["speed"]
        if "signalpower" in e:
            s["power"] = max(s.get("power", -999.0), e["signalpower"])
    table = []
    for mmsi, s in sorted(ships.items(), key=lambda kv: -kv[1]["messages"]):
        parts = [f"MMSI {mmsi}"]
        for key in ("shipname", "name", "callsign", "shiptype_text", "aid_type_text", "status_text", "destination"):
            if key in s:
                parts.append(f"{key}={s[key]!r}")
        if "lat" in s:
            parts.append(f"pos={s['lat']:.5f},{s['lon']:.5f}")
        if "speed" in s:
            parts.append(f"speed={s['speed']}kn")
        if "power" in s:
            parts.append(f"power={s['power']:.1f}dB")
        parts.append(f"types={sorted(t for t in s['types'] if t is not None)} msgs={s['messages']}")
        table.append("  " + " ".join(parts))
    return Result(
        f"ais: {len(ships)} stations, {len(events)} messages ({run.seconds:.0f} s to decode).",
        table,
        [],
        run.output,
        [],
    )


# --- Live ------------------------------------------------------------------

AIS_POSITION_TYPES = {1, 2, 3, 4, 9, 11, 18, 19, 21, 27}
AIS_KINDS = {4: "base-station", 11: "base-station", 21: "aid-to-navigation", 9: "sar-aircraft"}
AIS_CHANNEL_HZ = {"A": 161_975_000, "B": 162_025_000}
_AIS_DROP = {"class", "device", "version", "driver", "hardware", "scaled", "nmea", "rxtime", "ppm", "signalpower"}


class AisAggregator(MinuteAggregator):
    """AIS-catcher's JSON messages: position reports (and base stations,
    aids to navigation) become one row per MMSI per minute; anything else
    (static and voyage data, safety text, binary messages) a row each.
    Names and callsigns from static messages are carried onto the
    position summaries."""

    def __init__(self, emit: Callable[[Row], None], emit_position=None):
        super().__init__(emit)
        self.static: dict[str, dict] = {}
        # Positions for map tracks, at most one per station every 10 s, and
        # one every 5 minutes from a station that doesn't move.
        self.positions = PositionThrottle("ais", emit_position) if emit_position else None

    def _remember_static(self, key: str, e: dict) -> None:
        s = self.static.setdefault(key, {})
        for src, dst in (
            ("shipname", "name"),
            ("name", "name"),
            ("callsign", "callsign"),
            ("shiptype_text", "shiptype"),
            ("destination", "destination"),
            ("aid_type_text", "aid_type"),
        ):
            v = e.get(src)
            if isinstance(v, str) and v.strip(" @"):
                s[dst] = v.strip(" @")
        if len(self.static) > 5000:
            self.static.pop(next(iter(self.static)))

    def message(self, now: float, e: dict) -> None:
        if e.get("class") != "AIS" or not e.get("mmsi"):
            return
        self._roll(now)
        key = str(int(e["mmsi"]))
        self._remember_static(key, e)
        mtype = e.get("type")
        freq = AIS_CHANNEL_HZ.get(str(e.get("channel", "")))
        level = e.get("signalpower")
        if mtype not in AIS_POSITION_TYPES:
            static = self.static.get(key, {})
            fields = {k: v for k, v in e.items() if k not in _AIS_DROP}
            self.emit(
                Row(
                    ts=now,
                    profile="ais",
                    kind="ais",
                    freq_hz=freq,
                    level_db=level,
                    subject=key,
                    label=static.get("name"),
                    fields=fields,
                    raw=" ".join(e.get("nmea") or []) or json.dumps(fields)[:500],
                )
            )
            return
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = {"first": now, "last": now, "messages": 0, "types": set(), "freqs": set()}
        entry["last"] = now
        entry["messages"] += 1
        entry["types"].add(mtype)
        entry.setdefault("kind", AIS_KINDS.get(mtype, "vessel"))
        if freq:
            entry["freqs"].add(freq)
        lat, lon = e.get("lat"), e.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and abs(lat) <= 90 and abs(lon) <= 180:
            entry["lat"], entry["lon"] = round(lat, 5), round(lon, 5)
        if isinstance(e.get("speed"), (int, float)) and e["speed"] < 102.3:
            entry["speed"] = round(e["speed"], 1)
        if isinstance(e.get("course"), (int, float)) and e["course"] < 360:
            entry["course"] = round(e["course"], 1)
        if isinstance(e.get("heading"), (int, float)) and e["heading"] < 360:
            entry["heading"] = e["heading"]
        if e.get("status_text"):
            entry["status"] = e["status_text"]
        if isinstance(level, (int, float)):
            entry["level"] = max(entry.get("level", -1e9), level)
        if self.positions is not None and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            if abs(lat) <= 90 and abs(lon) <= 180:
                speed, course = e.get("speed"), e.get("course")
                self.positions.offer(
                    key,
                    now,
                    round(lat, 5),
                    round(lon, 5),
                    speed=round(speed, 1) if isinstance(speed, (int, float)) and speed < 102.3 else None,
                    track=round(course, 1) if isinstance(course, (int, float)) and course < 360 else None,
                )

    def row(self, key: str, e: dict) -> Row:
        static = self.static.get(key, {})
        fields = {"mmsi": int(key), **static}
        for k in ("lat", "lon", "speed", "course", "heading", "status", "messages"):
            if k in e:
                fields[k] = e[k]
        fields["types"] = sorted(t for t in e["types"] if t is not None)
        parts = [f"MMSI {key}"]
        if static.get("name"):
            parts.append(repr(static["name"]))
        if "lat" in e:
            parts.append(f"pos {e['lat']:.5f},{e['lon']:.5f}")
        if "speed" in e:
            parts.append(f"sog {e['speed']}")
        if "course" in e:
            parts.append(f"cog {e['course']}")
        parts.append(f"types {fields['types']} msgs {e['messages']}")
        return Row(
            ts=e["first"],
            ts_end=e["last"],
            profile="ais",
            kind=e.get("kind", "vessel"),
            freq_hz=min(e["freqs"]) if len(e["freqs"]) == 1 else 162_000_000,
            level_db=e.get("level"),
            subject=key,
            label=static.get("name"),
            fields=fields,
            raw=" ".join(parts),
        )


# The live profile's tuning: midway between the two channels, 25 kHz from
# each, so neither sits on the HackRF's DC spike. AIS-catcher assumes its
# input is centred here.
AIS_CENTER_HZ = 162_000_000
# 2 MS/s is the lowest rate the HackRF filters well, and AIS-catcher decodes
# it (hackrf_decode's AIS results were at 2 MS/s).
AIS_LIVE_RATE = 2_000_000


class AisPipeline(Pipeline):
    """hackrf_transfer's int8 samples, passed unchanged to AIS-catcher's
    stdin as CS8 (`-r CS8 .`, the input hackrf_decode already uses)."""

    def start(self) -> None:
        t = self.tuning
        if t.center_hz != AIS_CENTER_HZ:
            raise ValueError(
                f"AIS-catcher needs its samples centred on {AIS_CENTER_HZ / 1e6:.1f} MHz, "
                f"not {t.center_hz / 1e6:.4f} MHz"
            )
        self.agg = AisAggregator(self.ctx.emit, emit_position=self.ctx.emit_position)
        self._lock = threading.Lock()
        args = [t.program, "-r", "CS8", ".", "-s", str(t.sample_rate_hz), "-o", "5"]
        self.decoder = Proc("AIS-catcher", line_buffered(args), stdin=True, on_line=self._line)
        self.procs.append(self.decoder)
        self.source = self.ctx.make_source(t)
        self.started = time.time()

        def feed(buf: bytes) -> None:
            if not self.decoder.write(buf):
                raise BrokenPipeError("AIS-catcher stopped reading")

        self.source.start(feed)
        self._thread(self._tick, "ais-tick")

    def _line(self, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        if e.get("class") != "AIS":
            return
        with self._lock:
            self.agg.message(time.time(), e)
        self.ctx.count(1)

    def _tick(self) -> None:
        # Close each minute's summaries even when no message arrives.
        while not self._stop.wait(5):
            with self._lock:
                self.agg._roll(time.time())

    def finish(self) -> None:
        if hasattr(self, "agg"):
            with self._lock:
                self.agg.flush()


decoders.register(
    Decoder(
        "ais",
        ("AIS-catcher",),
        "AIS ship transponders (161.975 and 162.025 MHz)",
        "162.0 MHz at 2 MS/s; 30-60 s. Any rate works.",
        lambda job: decode_ais(job.cap, job.program, job.timeout, job.workdir),
        (161_975_000, 162_025_000),
        help="  ais   - ships, 162.0 MHz at 2,000,000 S/s (any rate).",
    )
)

live_profiles.register(
    Profile(
        "ais",
        "AIS ships, base stations and aids to navigation on 161.975/162.025 MHz (AIS-catcher); "
        "position reports as one row per ship per minute, other messages one row each",
        ("AIS-catcher",),
        AisPipeline,
        AIS_LIVE_RATE,
        AIS_CENTER_HZ,
        (161_975_000, 162_025_000),
        fixed_freqs=True,
        help="""  ais    - ships, base stations and aids to navigation, 161.975 and
           162.025 MHz at 2 MS/s (AIS-catcher); position reports one row
           per ship per minute, other AIS messages one row each.""",
        summary_kinds=("vessel", "base-station", "aid-to-navigation", "sar-aircraft", "ais"),
        summary_fields=("name", "callsign", "shiptype", "aid_type", "destination", "status", "lat", "lon", "speed", "messages"),
        views=(
            View(
                "map",
                ("vessel", "base-station", "aid-to-navigation", "sar-aircraft"),
                ("name", "callsign", "shiptype", "aid_type", "destination", "status", "speed", "course"),
                (("lat", "lat"), ("lon", "lon"), ("heading", "course"), ("speed", "speed"), ("label", "name")),
            ),
            View("table", (), ("name", "callsign", "shiptype", "aid_type", "destination", "status", "speed", "messages")),
            View("timeline"),
        ),
    )
)
