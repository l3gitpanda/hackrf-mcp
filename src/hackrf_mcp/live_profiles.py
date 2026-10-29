"""Live decoding profiles: what the live service (live_service.py) runs.

A profile tunes the HackRF to one band and runs the decoders for it,
turning their output into log rows (live_log.Row). One profile runs at a
time. Two kinds of input:

- AIS-catcher opens the HackRF itself (its native input).
- Every other profile reads `hackrf_transfer -r -` (int8 I/Q on stdout)
  and feeds the decoder program on its stdin: dump1090 and dumpvdl2 take
  the samples as cu8 (a byte flip); acarsdec and multimon-ng take audio,
  which iq.FFTChannelizer cuts out of the stream for every channel at once.

ADS-B and AIS position reports are summarised per aircraft or ship per
minute rather than logged frame by frame, so the log stays small.

Pager (FLEX/POCSAG) message text is dropped before anything is stored
unless the operator has set HACKRF_LIVE_PAGER_CONTENT; see redaction in
MultimonParser.
"""

from __future__ import annotations

import collections
import json
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal as scipy_signal

from hackrf_mcp import iq
from hackrf_mcp.live_log import Row
from hackrf_mcp.settings import LiveSettings


class SpecError(ValueError):
    """The profile can't run as asked; the message says what to change."""


# --- Profiles --------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    name: str
    summary: str
    programs: tuple[str, ...]  # any one of these, in preference order
    sample_rate_hz: int
    center_hz: int | None = None  # fixed tuning; None = chosen from the channels
    default_freqs_hz: tuple[int, ...] = ()
    needs_freqs: bool = False
    # LNA 32 / VGA 40 decoded every profile tried on the QNAP's antenna
    # (24/30 left FLEX on 931.75 MHz too weak to decode). HACKRF_LIVE_*_GAIN
    # and the start call override them.
    lna_gain_db: int = 32
    vga_gain_db: int = 40


# multimon-ng reads 22,050 S/s audio; 2,205,000 S/s from the HackRF divides
# down to it exactly.
MULTIMON_RATE = 22_050
MULTIMON_INPUT_RATE = 2_205_000
ACARS_RATE = 12_500
VDL2_SYMBOL_RATE = 105_000

PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile(
            "adsb",
            "ADS-B / Mode S aircraft on 1090 MHz (dump1090); one row per aircraft per minute",
            ("dump1090-mutability", "dump1090"),
            2_400_000,
            1_090_000_000,
            (1_090_000_000,),
        ),
        Profile(
            "ais",
            "AIS ships, base stations and aids to navigation on 161.975/162.025 MHz (AIS-catcher); "
            "position reports as one row per ship per minute, other messages one row each",
            ("AIS-catcher",),
            0,  # AIS-catcher picks its own rate
            162_000_000,
            (161_975_000, 162_025_000),
        ),
        Profile(
            "acars",
            "ACARS aircraft datalink, 130.8 MHz +-0.8 MHz (acarsdec)",
            ("acarsdec",),
            2_000_000,
            130_800_000,
            (130_025_000, 130_425_000, 130_450_000, 131_125_000, 131_550_000),
        ),
        Profile(
            "vdl2",
            "VDL Mode 2 aircraft datalink, 136.65-136.975 MHz (dumpvdl2)",
            ("dumpvdl2",),
            2_100_000,
            # Between two 25 kHz channels, so none sits on the DC spike.
            136_812_500,
            (
                136_650_000,
                136_700_000,
                136_725_000,
                136_775_000,
                136_800_000,
                136_825_000,
                136_875_000,
                136_900_000,
                136_975_000,
            ),
        ),
        Profile(
            "flex",
            "FLEX pagers on the channels given (multimon-ng); metadata only unless pager content is on",
            ("multimon-ng",),
            MULTIMON_INPUT_RATE,
            needs_freqs=True,
        ),
        Profile(
            "pocsag",
            "POCSAG pagers on the channels given (multimon-ng); metadata only unless pager content is on",
            ("multimon-ng",),
            MULTIMON_INPUT_RATE,
            needs_freqs=True,
        ),
        Profile(
            "aprs",
            "APRS packet radio, US 144.390 MHz (multimon-ng)",
            ("multimon-ng",),
            MULTIMON_INPUT_RATE,
            default_freqs_hz=(144_390_000,),
        ),
        Profile(
            "eas",
            "EAS/SAME headers on NOAA Weather Radio, 162.400-162.550 MHz (multimon-ng)",
            ("multimon-ng",),
            MULTIMON_INPUT_RATE,
            default_freqs_hz=(162_400_000, 162_425_000, 162_450_000, 162_475_000, 162_500_000, 162_525_000, 162_550_000),
        ),
    )
}

PROFILE_NAMES = tuple(PROFILES)


def find_program(profile: Profile) -> str | None:
    for name in profile.programs:
        if path := shutil.which(name):
            return path
    return None


def available() -> dict[str, str | None]:
    return {name: find_program(p) for name, p in PROFILES.items()}


@dataclass(frozen=True)
class Spec:
    """What the caller asked for. Gains of None mean the profile's."""

    profile: str
    freqs_hz: tuple[int, ...] = ()
    lna_gain_db: int | None = None
    vga_gain_db: int | None = None
    amp: bool = False

    def to_json(self) -> dict:
        return {
            "profile": self.profile,
            "freqs_hz": list(self.freqs_hz),
            "lna_gain_db": self.lna_gain_db,
            "vga_gain_db": self.vga_gain_db,
            "amp": self.amp,
        }

    @classmethod
    def from_json(cls, d: dict) -> Spec:
        return cls(
            profile=str(d["profile"]),
            freqs_hz=tuple(int(f) for f in d.get("freqs_hz") or ()),
            lna_gain_db=None if d.get("lna_gain_db") is None else int(d["lna_gain_db"]),
            vga_gain_db=None if d.get("vga_gain_db") is None else int(d["vga_gain_db"]),
            amp=bool(d.get("amp", False)),
        )


@dataclass(frozen=True)
class Tuning:
    """A Spec resolved against its profile: exactly what will run."""

    spec: Spec
    profile: Profile
    program: str
    center_hz: int
    sample_rate_hz: int
    freqs_hz: tuple[int, ...]
    lna_gain_db: int
    vga_gain_db: int
    amp: bool

    def describe(self) -> str:
        chans = ", ".join(f"{f / 1e6:.4f}" for f in self.freqs_hz)
        rate = f" at {self.sample_rate_hz / 1e6:g} MS/s" if self.sample_rate_hz else ""
        return (
            f"{self.profile.name}: tuned {self.center_hz / 1e6:.4f} MHz{rate}, channels {chans} MHz, "
            f"LNA {self.lna_gain_db} dB, VGA {self.vga_gain_db} dB, amp {'on' if self.amp else 'off'}"
        )


def usable_hz(sample_rate_hz: int) -> float:
    # The HackRF's anti-alias filter rolls off in the outer 10% of the band.
    return 0.4 * sample_rate_hz


def choose_center(freqs: tuple[int, ...], sample_rate_hz: int, margin_hz: int = 10_000, avoid_hz: int = 20_000) -> int:
    """A tuning that covers every channel (with `margin_hz` to spare) and
    keeps each at least `avoid_hz` off the DC spike at the center."""
    lo, hi = min(freqs), max(freqs)
    mid = (lo + hi) // 2
    reach = usable_hz(sample_rate_hz) - margin_hz
    for k in range(0, 129):
        for sign in (1, -1) if k else (1,):
            c = mid + sign * k * 12_500
            c -= c % 25
            if all(abs(f - c) <= reach for f in freqs) and all(abs(f - c) >= avoid_hz for f in freqs):
                return c
    raise SpecError(
        f"the channels span {(hi - lo) / 1e6:.3f} MHz; at most {2 * reach / 1e6:.3f} MHz fits one tuning. "
        "Start fewer channels, or channels closer together."
    )


def resolve(spec: Spec, settings: LiveSettings, program_finder: Callable[[Profile], str | None] = find_program) -> Tuning:
    profile = PROFILES.get(spec.profile)
    if profile is None:
        raise SpecError(f"unknown profile {spec.profile!r}; choose one of {', '.join(PROFILE_NAMES)}")
    program = program_finder(profile)
    if program is None:
        raise SpecError(f"{profile.name} needs {' or '.join(profile.programs)}, which is not installed on this server.")
    freqs = tuple(spec.freqs_hz) or profile.default_freqs_hz
    if profile.needs_freqs and not freqs:
        raise SpecError(
            f"{profile.name} needs frequencies_hz: the pager channel(s) to decode, e.g. [931750000]."
        )
    if profile.name in ("adsb", "ais") and spec.freqs_hz and tuple(spec.freqs_hz) != profile.default_freqs_hz:
        raise SpecError(f"{profile.name} has fixed frequencies; leave frequencies_hz out.")
    rate = profile.sample_rate_hz
    if profile.center_hz is not None:
        center = profile.center_hz
        if spec.freqs_hz and rate:
            outside = [f for f in freqs if abs(f - center) > usable_hz(rate) - 10_000]
            if outside:
                raise SpecError(
                    f"{profile.name} is tuned to {center / 1e6:.4f} MHz and covers "
                    f"{(center - usable_hz(rate)) / 1e6:.3f}-{(center + usable_hz(rate)) / 1e6:.3f} MHz; "
                    f"outside it: {', '.join(f'{f / 1e6:.4f}' for f in outside)} MHz."
                )
    else:
        center = choose_center(freqs, rate)
    lna = spec.lna_gain_db if spec.lna_gain_db is not None else settings.lna_gain_db
    vga = spec.vga_gain_db if spec.vga_gain_db is not None else settings.vga_gain_db
    lna = profile.lna_gain_db if lna is None else lna
    vga = profile.vga_gain_db if vga is None else vga
    if not (0 <= lna <= 40 and lna % 8 == 0):
        raise SpecError(f"LNA gain must be 0-40 dB in 8 dB steps, not {lna}")
    if not (0 <= vga <= 62 and vga % 2 == 0):
        raise SpecError(f"VGA gain must be 0-62 dB in 2 dB steps, not {vga}")
    return Tuning(spec, profile, program, center, rate, tuple(freqs), lna, vga, bool(spec.amp))


# --- Child processes -------------------------------------------------------


def _line_buffered(args: list[str]) -> list[str]:
    """Run a decoder with its stdout line-buffered, so a message reaches the
    log when it is decoded rather than when 4 KiB of output has piled up."""
    stdbuf = shutil.which("stdbuf")
    return [stdbuf, "-oL", *args] if stdbuf else args


class Proc:
    """A child process. Its stderr is kept as a short tail for error
    messages; its stdout, if `on_line` is given, is read line by line."""

    def __init__(
        self,
        name: str,
        args: list[str],
        *,
        stdin: bool = False,
        on_line: Callable[[str], None] | None = None,
        stdout_bytes: bool = False,
        cwd: Path | None = None,
    ):
        self.name = name
        self.args = args
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE if (on_line or stdout_bytes) else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
        self.threads = [threading.Thread(target=self._drain_stderr, daemon=True, name=f"{name}-stderr")]
        if on_line is not None:
            self.threads.append(
                threading.Thread(target=self._read_lines, args=(on_line,), daemon=True, name=f"{name}-stdout")
            )
        for t in self.threads:
            t.start()

    def _drain_stderr(self) -> None:
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if line:
                self.stderr_tail.append(line)

    def _read_lines(self, on_line: Callable[[str], None]) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                on_line(raw.decode(errors="replace").rstrip("\r\n"))
            except Exception as exc:  # a parser bug must not stop the stream
                self.stderr_tail.append(f"(parser error: {exc!r})")

    def write(self, data: bytes) -> bool:
        try:
            self.proc.stdin.write(data)
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def poll(self) -> int | None:
        return self.proc.poll()

    def stop(self, sig: int = signal.SIGTERM, grace_s: float = 3.0) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(sig)
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            except ProcessLookupError:
                pass
        for t in self.threads:
            t.join(timeout=2)

    def failure(self) -> str | None:
        code = self.proc.poll()
        if code is None:
            return None
        tail = " | ".join(list(self.stderr_tail)[-4:])
        return f"{self.name} exited with code {code}" + (f": {tail}" if tail else "")


# --- Sample sources --------------------------------------------------------

READ_BYTES = 1 << 18  # 131072 samples: 55-65 ms at the rates used here

_POWER_RE = re.compile(r"average power\s+(-?[\d.]+)\s*dBfs")


class Source:
    """A stream of int8 I/Q bytes handed to a consumer in blocks."""

    sample_rate_hz: int

    def __init__(self) -> None:
        self.bytes_read = 0
        self.started = 0.0
        self.error: str | None = None
        self.finished = threading.Event()
        self._recent: collections.deque[tuple[float, int]] = collections.deque(maxlen=200)

    def start(self, consumer: Callable[[bytes], None]) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def _count(self, n: int) -> None:
        self.bytes_read += n
        self._recent.append((time.monotonic(), self.bytes_read))

    def input_ratio(self, window_s: float = 10.0) -> float | None:
        """Bytes received over the last window as a fraction of what the
        sample rate should deliver; well under 1 means samples are lost."""
        now = time.monotonic()
        if not self._recent or now - self.started < window_s + 2:
            return None
        old = [(t, n) for t, n in self._recent if now - t >= window_s]
        t0, n0 = old[-1] if old else self._recent[0]
        t1, n1 = self._recent[-1]
        if t1 <= t0:
            return None
        return (n1 - n0) / (t1 - t0) / (2 * self.sample_rate_hz)

    def health(self) -> dict:
        return {"bytes": self.bytes_read, "input_ratio": self.input_ratio()}


class HackrfSource(Source):
    """`hackrf_transfer -r -`: the HackRF's samples on a pipe. Stopping it
    sends SIGINT, which makes hackrf_transfer close the device cleanly, and
    waits for it to exit, so the USB device is free when stop() returns."""

    def __init__(self, binary: str, center_hz: int, sample_rate_hz: int, lna: int, vga: int, amp: bool):
        super().__init__()
        self.sample_rate_hz = sample_rate_hz
        self.args = [
            binary,
            "-r",
            "-",
            "-f",
            str(center_hz),
            "-s",
            str(sample_rate_hz),
            "-l",
            str(lna),
            "-g",
            str(vga),
            "-a",
            "1" if amp else "0",
        ]
        self.proc: Proc | None = None
        self.thread: threading.Thread | None = None
        self._stopping = False

    def start(self, consumer: Callable[[bytes], None]) -> None:
        self.started = time.monotonic()
        self.proc = Proc("hackrf_transfer", self.args, stdout_bytes=True)
        self.thread = threading.Thread(target=self._pump, args=(consumer,), daemon=True, name="hackrf-source")
        self.thread.start()

    def _pump(self, consumer: Callable[[bytes], None]) -> None:
        out = self.proc.proc.stdout
        try:
            while True:
                buf = out.read(READ_BYTES)
                if not buf:
                    break
                if len(buf) % 2:
                    buf = buf[:-1]
                self._count(len(buf))
                consumer(buf)
        except Exception as exc:
            if not self._stopping:
                self.error = f"stream stopped: {exc!r}"
        finally:
            self.finished.set()

    def level_dbfs(self) -> float | None:
        """hackrf_transfer's own once-a-second average input power."""
        if self.proc is None:
            return None
        for line in reversed(self.proc.stderr_tail):
            if m := _POWER_RE.search(line):
                return float(m.group(1))
        return None

    def stop(self) -> None:
        self._stopping = True
        if self.proc is not None:
            self.proc.stop(signal.SIGINT, grace_s=5.0)
        if self.thread is not None:
            self.thread.join(timeout=5)

    def failure(self) -> str | None:
        if self.error:
            return self.error
        if self.proc is not None and not self._stopping:
            return self.proc.failure()
        return None

    def health(self) -> dict:
        h = super().health()
        h["input_power_dbfs"] = self.level_dbfs()
        return h


class FileSource(Source):
    """A capture file played back as if it were the HackRF, optionally in
    real time; for replaying real recordings through a profile."""

    def __init__(self, path: Path, sample_rate_hz: int, realtime: bool = True):
        super().__init__()
        self.path = Path(path)
        self.sample_rate_hz = sample_rate_hz
        self.realtime = realtime
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, consumer: Callable[[bytes], None]) -> None:
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._pump, args=(consumer,), daemon=True, name="file-source")
        self.thread.start()

    def _pump(self, consumer: Callable[[bytes], None]) -> None:
        try:
            with open(self.path, "rb") as f:
                while not self._stop.is_set():
                    buf = f.read(READ_BYTES)
                    if not buf:
                        break
                    if len(buf) % 2:
                        buf = buf[:-1]
                    self._count(len(buf))
                    consumer(buf)
                    if self.realtime:
                        due = self.started + self.bytes_read / (2 * self.sample_rate_hz)
                        delay = due - time.monotonic()
                        if delay > 0:
                            self._stop.wait(delay)
        except Exception as exc:
            self.error = f"replay stopped: {exc!r}"
        finally:
            self.finished.set()

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def failure(self) -> str | None:
        return self.error


# --- Per-minute aggregation ------------------------------------------------


def _minute(ts: float) -> int:
    return int(ts // 60)


class MinuteAggregator:
    """Collects one entry per subject per clock minute and emits them as
    rows when the minute changes (or on flush)."""

    def __init__(self, emit: Callable[[Row], None]):
        self.emit = emit
        self.minute: int | None = None
        self.entries: dict[str, dict] = {}

    def _roll(self, now: float) -> None:
        m = _minute(now)
        if self.minute is not None and m != self.minute:
            self.flush()
        self.minute = m

    def flush(self) -> None:
        for key, entry in self.entries.items():
            self.emit(self.row(key, entry))
        self.entries = {}

    def row(self, key: str, entry: dict) -> Row:
        raise NotImplementedError


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

    def __init__(self, emit: Callable[[Row], None]):
        super().__init__(emit)
        self.static: dict[str, dict] = {}

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


# --- Line parsers ----------------------------------------------------------


def parse_acars(line: str, freq_hz: int, now: float) -> Row | None:
    """One acarsdec -o 4 JSON line."""
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    tail = str(e.get("tail", "")).strip().lstrip(".")
    flight = str(e.get("flight", "")).strip()
    fields = {k: v for k, v in e.items() if k not in ("station_id", "app", "channel", "freq", "timestamp")}
    return Row(
        ts=now,
        profile="acars",
        kind="acars",
        freq_hz=freq_hz,
        level_db=e.get("level"),
        subject=tail.upper() or flight.upper() or None,
        label=flight or None,
        fields=fields,
        raw=line,
    )


def parse_vdl2(line: str, now: float) -> Row | None:
    """One dumpvdl2 decoded:json line."""
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    v = e.get("vdl2", e)
    avlc = v.get("avlc") or {}
    src, dst = avlc.get("src") or {}, avlc.get("dst") or {}
    acars = avlc.get("acars") or {}
    fields = {
        "src": src.get("addr"),
        "src_type": src.get("type"),
        "dst": dst.get("addr"),
        "dst_type": dst.get("type"),
        "frame_type": avlc.get("frame_type"),
    }
    if acars:
        fields["acars"] = {
            k: acars.get(k) for k in ("reg", "flight", "label", "blk_id", "mode", "msg_num", "msg_text") if acars.get(k)
        }
    # Name the aircraft whichever end of the link it is.
    aircraft = src if src.get("type") == "Aircraft" else dst if dst.get("type") == "Aircraft" else src
    reg = str(acars.get("reg", "")).strip().lstrip(".")
    return Row(
        ts=now,
        profile="vdl2",
        kind="vdl2",
        freq_hz=v.get("freq"),
        level_db=v.get("sig_level"),
        subject=str(aircraft.get("addr", "")).upper() or None,
        label=str(acars.get("flight", "")).strip() or reg or None,
        fields={k: x for k, x in fields.items() if x not in (None, "", {})},
        raw=line,
    )


_FLEX_OLD = re.compile(
    r"^FLEX:\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(\S+)\s+(\S+)\s+\[([\d ]+)\]\s+(\w+)\s?(.*)$"
)
_POCSAG = re.compile(
    r"^(POCSAG\d+):\s+Address:\s*(\d+)\s+Function:\s*(\d+)\s*(?:(Alpha|Numeric|Skyper):\s*(.*))?$"
)
_APRS = re.compile(r"^APRS:\s*([^>\s]+)>([^,:\s]+)((?:,[^:]*)?):(.*)$")
_AFSK = re.compile(r"^AFSK1200:\s*fm\s+(\S+)\s+to\s+(\S+)")
_EAS = re.compile(r"^EAS:\s*(ZCZC-[A-Z]{3}-[A-Z]{3}-[\d-]+\+\d{4}-\d{7}-[^-]{1,8}-?)")
_EAS_PARTS = re.compile(r"ZCZC-([A-Z]{3})-([A-Z]{3})-([\d-]+)\+(\d{4})-(\d{7})-([^-]{1,8})")

CONTENT_NOT_LOGGED = "[content not logged]"
_PAGER_PREFIXES = ("FLEX|", "FLEX:", "POCSAG")


class MultimonParser:
    """multimon-ng's output for one channel, as log rows.

    Pagers: a FLEX or POCSAG message is a header line (time, rate, frame,
    capcode, type) followed by its text, which may run onto further lines.
    Unless `pager_content` is set, the text and every continuation line are
    dropped here, before a row exists: the row's fields and raw text hold
    only the header. With it set, continuation lines are joined onto the
    message."""

    def __init__(self, profile: str, freq_hz: int, pager_content: bool, level: Callable[[], float | None] = lambda: None):
        self.profile = profile
        self.freq_hz = freq_hz
        self.pager_content = pager_content
        self.level = level
        self.pending: Row | None = None
        self.pending_since = 0.0

    def _row(self, now: float, kind: str, subject: str | None, label: str | None, fields: dict, raw: str) -> Row:
        return Row(
            ts=now,
            profile=self.profile,
            kind=kind,
            freq_hz=self.freq_hz,
            level_db=self.level(),
            subject=subject,
            label=label,
            fields=fields,
            raw=raw,
        )

    def _page(self, now: float, fields: dict, header: str, content: str) -> list[Row]:
        out = self.flush()
        capcode = fields["capcodes"][0] if fields.get("capcodes") else fields.get("capcode")
        if self.pager_content:
            fields["message"] = content
            raw = f"{header}{content}"
        else:
            raw = f"{header}{CONTENT_NOT_LOGGED}" if content else header.rstrip("| ").rstrip()
        row = self._row(now, "page", str(int(capcode)) if capcode else None, fields.get("type"), fields, raw)
        if self.pager_content:
            # Text may continue on the next lines; hold the row briefly.
            self.pending, self.pending_since = row, now
        else:
            out.append(row)
        return out

    def flush(self) -> list[Row]:
        if self.pending is None:
            return []
        row, self.pending = self.pending, None
        return [row]

    def tick(self, now: float, hold_s: float = 2.0) -> list[Row]:
        if self.pending is not None and now - self.pending_since >= hold_s:
            return self.flush()
        return []

    def feed(self, line: str, now: float) -> list[Row]:
        line = line.rstrip("\r\n")
        if not line.strip():
            return []
        if line.startswith("FLEX|"):
            parts = line.split("|", 6)
            if len(parts) >= 6:
                capcodes = [str(int(c)) for c in parts[4].split() if c.isdigit()]
                fields = {
                    "protocol": "FLEX",
                    "mode": parts[2],
                    "frame": parts[3],
                    "capcodes": capcodes,
                    "type": parts[5],
                }
                header = "|".join(parts[:6]) + "|"
                return self._page(now, fields, header, parts[6] if len(parts) > 6 else "")
        if m := _FLEX_OLD.match(line):
            capcodes = [str(int(c)) for c in m.group(4).split() if c.isdigit()]
            fields = {"protocol": "FLEX", "mode": m.group(2), "frame": m.group(3), "capcodes": capcodes, "type": m.group(5)}
            header = f"FLEX: {m.group(1)} {m.group(2)} {m.group(3)} [{m.group(4)}] {m.group(5)} "
            return self._page(now, fields, header, m.group(6))
        if m := _POCSAG.match(line):
            ctype = m.group(4) or "Tone"
            fields = {
                "protocol": m.group(1),
                "capcode": str(int(m.group(2))),
                "function": int(m.group(3)),
                "type": ctype,
            }
            header = f"{m.group(1)}: Address: {m.group(2)}  Function: {m.group(3)}"
            content = m.group(5) or ""
            if m.group(4):
                header += f"  {m.group(4)}: "
            return self._page(now, fields, header, content)
        if line.startswith(_PAGER_PREFIXES):
            # A pager line in a shape this parser doesn't know: with content
            # off nothing of it is kept, so no text can slip through.
            if self.pager_content:
                return self.flush() + [self._row(now, "page", None, None, {"unparsed": True}, line)]
            return self.flush()
        if self.profile in ("flex", "pocsag"):
            # A continuation of the last message's text.
            if self.pager_content and self.pending is not None:
                self.pending.fields["message"] = self.pending.fields.get("message", "") + "\n" + line
                self.pending.raw = (self.pending.raw or "") + "\n" + line
                self.pending_since = now
            return []
        if m := _APRS.match(line):
            fields = {"src": m.group(1), "dst": m.group(2), "path": m.group(3).lstrip(","), "payload": m.group(4)}
            return self.flush() + [self._row(now, "aprs", m.group(1).upper(), None, fields, line)]
        if m := _AFSK.match(line):
            fields = {"src": m.group(1), "dst": m.group(2)}
            return self.flush() + [self._row(now, "aprs", m.group(1).upper(), None, fields, line)]
        if line.startswith("EAS"):
            if m := _EAS.match(line):
                header = m.group(1)
                fields = {"header": header}
                if p := _EAS_PARTS.search(header):
                    fields.update(
                        originator=p.group(1),
                        event=p.group(2),
                        locations=[x for x in p.group(3).split("-") if x],
                        duration=p.group(4),
                        issued=p.group(5),
                        sender=p.group(6),
                    )
                return self.flush() + [self._row(now, "eas", fields.get("sender"), fields.get("event"), fields, line)]
            if line.startswith("EAS:") and "NNNN" in line:
                return self.flush() + [self._row(now, "eas", None, "NNNN", {"end_of_message": True}, line)]
            return []
        return []


# --- Pipelines -------------------------------------------------------------


@dataclass
class Context:
    """What a pipeline needs from the service."""

    settings: LiveSettings
    emit: Callable[[Row], None]
    count: Callable[[int], None]
    run_dir: Path
    make_source: Callable[[Tuning], Source]
    # Replays (live_service.py --replay) decode a file instead of the HackRF.
    replay_path: Path | None = None
    replay_rate: int = 0


def wav_header(rate: int) -> bytes:
    """A mono 16-bit WAV header with the length left open (0xFFFFFFFF), as
    for a stream; acarsdec reads it from a pipe."""
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, 2 * rate, 2, 16)
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)
    )


def _s16(x: np.ndarray, scale: float) -> bytes:
    return np.clip(np.rint(x * scale), -32768, 32767).astype("<i2").tobytes()


class Pipeline:
    """One running profile: a source, its decoder processes, and whatever
    turns their output into rows. start() and stop() are called from the
    service's supervisor; stop() releases the HackRF before it returns."""

    def __init__(self, tuning: Tuning, ctx: Context):
        self.tuning = tuning
        self.ctx = ctx
        self.source: Source | None = None
        self.procs: list[Proc] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started = 0.0

    def start(self) -> None:
        raise NotImplementedError

    def _thread(self, target: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=target, daemon=True, name=name)
        self._threads.append(t)
        t.start()

    def stop(self) -> None:
        self._stop.set()
        # The source first: that frees the device.
        if self.source is not None:
            self.source.stop()
        for p in self.procs:
            p.stop()
        for t in self._threads:
            t.join(timeout=6)
        self.finish()

    def finish(self) -> None:
        """Emit whatever is still being aggregated."""

    def failure(self) -> str | None:
        if self.source is not None:
            if f := getattr(self.source, "failure", lambda: None)():
                return f
            if self.source.finished.is_set() and self.ctx.replay_path is None:
                return "the sample stream ended"
        for p in self.procs:
            if f := p.failure():
                return f
        return None

    def finished(self) -> bool:
        """For replays: the input has been used up."""
        return self.source is not None and self.source.finished.is_set()

    def health(self) -> dict:
        h = self.source.health() if self.source is not None else {}
        return h

    def stderr_tail(self) -> list[str]:
        lines = []
        for p in self.procs:
            lines += [f"{p.name}: {x}" for x in list(p.stderr_tail)[-3:]]
        return lines


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


class AisPipeline(Pipeline):
    """AIS-catcher on the HackRF directly (or on a replayed file)."""

    def start(self) -> None:
        t = self.tuning
        self.agg = AisAggregator(self.ctx.emit)
        self._lock = threading.Lock()
        if self.ctx.replay_path is not None:
            rate = int(self.ctx.replay_rate)
            args = [t.program, "-r", "CS8", str(self.ctx.replay_path), "-s", str(rate), "-o", "5"]
        else:
            args = [
                t.program,
                "-d:0",
                "-gf",
                "LNA",
                str(t.lna_gain_db),
                "VGA",
                str(t.vga_gain_db),
                "PREAMP",
                "on" if t.amp else "off",
                "-o",
                "5",
            ]
        self.decoder = Proc("AIS-catcher", _line_buffered(args), on_line=self._line)
        self.procs.append(self.decoder)
        self.started = time.time()
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

    def stop(self) -> None:
        self._stop.set()
        # SIGINT lets AIS-catcher close the HackRF before it exits.
        self.decoder.stop(signal.SIGINT, grace_s=6.0)
        for t in self._threads:
            t.join(timeout=6)
        self.finish()

    def finish(self) -> None:
        with self._lock:
            self.agg.flush()

    def failure(self) -> str | None:
        if self.ctx.replay_path is not None and self.decoder.poll() == 0:
            return None
        return self.decoder.failure()

    def finished(self) -> bool:
        return self.decoder.poll() is not None

    def health(self) -> dict:
        return {}


class Vdl2Pipeline(Pipeline):
    """dumpvdl2 does its own channelising, from cu8 at the full rate."""

    def start(self) -> None:
        t = self.tuning
        oversample = t.sample_rate_hz // VDL2_SYMBOL_RATE
        args = [
            t.program,
            "--iq-file",
            "-",
            "--sample-format",
            "U8",
            "--oversample",
            str(oversample),
            "--centerfreq",
            str(t.center_hz),
            "--output",
            "decoded:json:file:path=-",
            *[str(f) for f in t.freqs_hz],
        ]
        decoder = Proc("dumpvdl2", _line_buffered(args), stdin=True, on_line=self._line)
        self.procs.append(decoder)
        self.source = self.ctx.make_source(t)
        self.started = time.time()

        def feed(buf: bytes) -> None:
            if not decoder.write(iq.cs8_to_cu8(buf)):
                raise BrokenPipeError("dumpvdl2 stopped reading")

        self.source.start(feed)

    def _line(self, line: str) -> None:
        row = parse_vdl2(line, time.time())
        if row is not None:
            self.ctx.emit(row)
            self.ctx.count(1)


class _ChannelPipeline(Pipeline):
    """Shared by acarsdec and multimon-ng: an FFTChannelizer cuts every
    channel out of the stream, `convert` turns each channel's samples into
    the decoder's audio, and each channel has its own decoder process."""

    out_rate: int
    bandwidth_hz: float

    def decoder_args(self, freq_hz: int) -> list[str]:
        raise NotImplementedError

    def convert(self, i: int, y: np.ndarray) -> bytes:
        raise NotImplementedError

    def preamble(self) -> bytes:
        return b""

    def on_line(self, i: int, freq_hz: int, line: str) -> None:
        raise NotImplementedError

    def start(self) -> None:
        t = self.tuning
        offsets = [f - t.center_hz for f in t.freqs_hz]
        self.channelizer = iq.FFTChannelizer(
            t.sample_rate_hz, offsets, t.sample_rate_hz // self.out_rate, self.bandwidth_hz
        )
        for i, f in enumerate(t.freqs_hz):
            p = Proc(
                f"{Path(t.program).name}@{f / 1e6:.4f}",
                _line_buffered(self.decoder_args(f)),
                stdin=True,
                on_line=lambda line, i=i, f=f: self.on_line(i, f, line),
            )
            self.procs.append(p)
            if pre := self.preamble():
                p.write(pre)
        self.source = self.ctx.make_source(t)
        self.started = time.time()

        def feed(buf: bytes) -> None:
            outs = self.channelizer(iq.cs8_to_complex(buf))
            for i, (p, y) in enumerate(zip(self.procs, outs)):
                if y.size and not p.write(self.convert(i, y)):
                    raise BrokenPipeError(f"{p.name} stopped reading")

        self.source.start(feed)

    def level(self, i: int) -> float | None:
        p = self.channelizer.power_db[i]
        return round(p, 1) if p != float("-inf") else None

    def health(self) -> dict:
        # Each channel's level over its last block: a channel well above the
        # others is busy; all alike means nothing is on the air.
        h = super().health()
        if hasattr(self, "channelizer"):
            h["channel_levels_dbfs"] = {f"{f / 1e6:.4f}": self.level(i) for i, f in enumerate(self.tuning.freqs_hz)}
        return h


class AcarsPipeline(_ChannelPipeline):
    out_rate = ACARS_RATE
    bandwidth_hz = 10_000

    def start(self) -> None:
        sos = scipy_signal.butter(2, 100.0, btype="high", fs=ACARS_RATE, output="sos")
        self._sos = sos
        self._zi = [scipy_signal.sosfilt_zi(sos) * 0 for _ in self.tuning.freqs_hz]
        super().start()

    def decoder_args(self, freq_hz: int) -> list[str]:
        return [self.tuning.program, "-o", "4", "-f", "/dev/stdin"]

    def preamble(self) -> bytes:
        return wav_header(ACARS_RATE)

    def convert(self, i: int, y: np.ndarray) -> bytes:
        env = np.abs(y).astype(np.float32)
        out, self._zi[i] = scipy_signal.sosfilt(self._sos, env, zi=self._zi[i])
        # Full scale (|x| = 1) maps to half of int16's range.
        return _s16(out, 16_000.0)

    def on_line(self, i: int, freq_hz: int, line: str) -> None:
        row = parse_acars(line, freq_hz, time.time())
        if row is not None:
            self.ctx.emit(row)
            self.ctx.count(1)


MULTIMON_MODES = {
    "pocsag": ["POCSAG512", "POCSAG1200", "POCSAG2400"],
    "flex": ["FLEX"],
    "aprs": ["AFSK1200"],
    "eas": ["EAS"],
}


class MultimonPipeline(_ChannelPipeline):
    out_rate = MULTIMON_RATE
    bandwidth_hz = 16_000

    def start(self) -> None:
        self._sos = scipy_signal.butter(2, 20.0, btype="high", fs=MULTIMON_RATE, output="sos")
        self._zi = [scipy_signal.sosfilt_zi(self._sos) * 0 for _ in self.tuning.freqs_hz]
        self._prev: list[complex | None] = [None for _ in self.tuning.freqs_hz]
        self._plock = threading.Lock()
        self.parsers = [
            MultimonParser(
                self.tuning.profile.name,
                f,
                self.ctx.settings.pager_content,
                level=lambda i=i: self.level(i),
            )
            for i, f in enumerate(self.tuning.freqs_hz)
        ]
        super().start()
        self._thread(self._tick, "multimon-tick")

    def decoder_args(self, freq_hz: int) -> list[str]:
        args = [self.tuning.program, "-t", "raw", "-q", "-c"]
        for mode in MULTIMON_MODES[self.tuning.profile.name]:
            args += ["-a", mode]
        if self.tuning.profile.name == "aprs":
            args.append("-A")
        if self.tuning.profile.name == "pocsag":
            args.append("-e")
        return args + ["-"]

    def convert(self, i: int, y: np.ndarray) -> bytes:
        audio, self._prev[i] = iq.fm_discriminate(y, self._prev[i])
        audio, self._zi[i] = scipy_signal.sosfilt(self._sos, audio, zi=self._zi[i])
        # +-5 kHz deviation (narrowband FM's full swing) maps to half scale.
        return _s16(audio, 0.5 * 32767 / (2 * np.pi * 5_000 / MULTIMON_RATE))

    def _emit_all(self, rows: list[Row]) -> None:
        for row in rows:
            self.ctx.emit(row)

    def on_line(self, i: int, freq_hz: int, line: str) -> None:
        with self._plock:
            rows = self.parsers[i].feed(line, time.time())
        if rows:
            self._emit_all(rows)
        if line.startswith(("FLEX", "POCSAG", "APRS", "AFSK1200", "EAS:")):
            self.ctx.count(1)

    def _tick(self) -> None:
        while not self._stop.wait(1):
            with self._plock:
                rows = [r for p in self.parsers for r in p.tick(time.time())]
            self._emit_all(rows)

    def finish(self) -> None:
        if hasattr(self, "parsers"):
            with self._plock:
                rows = [r for p in self.parsers for r in p.flush()]
            self._emit_all(rows)


PIPELINES: dict[str, type[Pipeline]] = {
    "adsb": AdsbPipeline,
    "ais": AisPipeline,
    "vdl2": Vdl2Pipeline,
    "acars": AcarsPipeline,
    "flex": MultimonPipeline,
    "pocsag": MultimonPipeline,
    "aprs": MultimonPipeline,
    "eas": MultimonPipeline,
}


def make_pipeline(tuning: Tuning, ctx: Context) -> Pipeline:
    return PIPELINES[tuning.profile.name](tuning, ctx)
