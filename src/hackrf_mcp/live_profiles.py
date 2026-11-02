"""Live decoding: the core that the live service (live_service.py) runs.

A profile tunes the HackRF to one band and runs the decoders for it,
turning their output into log rows (live_log.Row). One profile runs at a
time. Profiles live in hackrf_mcp/protocols/, beside the offline decoders
for the same protocol, and register themselves here with register(). This
module holds what they share:

- Profile, Spec and Tuning, and resolve(), which turns a request into an
  exact tuning (center, rate, channels, gains) or refuses it.
- Proc, a decoder child process with its output read line by line.
- Sources: HackrfSource (`hackrf_transfer -r -`, int8 I/Q on a pipe) and
  FileSource (a capture replayed as if it were the HackRF).
- Pipeline, the base of every profile's pipeline, and ChannelPipeline for
  decoders that take audio channels cut out by iq.FFTChannelizer.
- MinuteAggregator, for profiles that log one row per subject per minute.
"""

from __future__ import annotations

import collections
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
    pipeline: type[Pipeline]
    sample_rate_hz: int  # 0: the decoder opens the HackRF and picks its own
    center_hz: int | None = None  # fixed tuning; None = chosen from the channels
    default_freqs_hz: tuple[int, ...] = ()
    needs_freqs: bool = False  # the caller must give the channels
    fixed_freqs: bool = False  # the caller may not change the channels
    # LNA 32 / VGA 40 decoded every profile tried on the QNAP's antenna
    # (24/30 left FLEX on 931.75 MHz too weak to decode). HACKRF_LIVE_*_GAIN
    # and the start call override them.
    lna_gain_db: int = 32
    vga_gain_db: int = 40
    # This profile's lines in hackrf_live_start's description.
    help: str = ""
    # The row kinds hackrf_live_summary groups for this profile, and the
    # fields it shows for each subject.
    summary_kinds: tuple[str, ...] = ()
    summary_fields: tuple[str, ...] = ()


PROFILES: dict[str, Profile] = {}


def register(profile: Profile) -> Profile:
    """Add a profile to the live service. Called by the protocol modules."""
    if profile.name in PROFILES:
        raise ValueError(f"live profile {profile.name!r} is registered twice")
    PROFILES[profile.name] = profile
    return profile


def load() -> None:
    """Import every protocol module, so the registry is complete."""
    import hackrf_mcp.protocols  # noqa: F401


def names() -> tuple[str, ...]:
    load()
    return tuple(PROFILES)


def tool_help() -> str:
    """The per-profile lines of hackrf_live_start's description."""
    load()
    return "\n".join(p.help.rstrip() for p in PROFILES.values() if p.help)


def find_program(profile: Profile) -> str | None:
    for name in profile.programs:
        if path := shutil.which(name):
            return path
    return None


def available() -> dict[str, str | None]:
    load()
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
    load()
    profile = PROFILES.get(spec.profile)
    if profile is None:
        raise SpecError(f"unknown profile {spec.profile!r}; choose one of {', '.join(PROFILES)}")
    program = program_finder(profile)
    if program is None:
        raise SpecError(f"{profile.name} needs {' or '.join(profile.programs)}, which is not installed on this server.")
    freqs = tuple(spec.freqs_hz) or profile.default_freqs_hz
    if profile.needs_freqs and not freqs:
        raise SpecError(
            f"{profile.name} needs frequencies_hz: the pager channel(s) to decode, e.g. [931750000]."
        )
    if profile.fixed_freqs and spec.freqs_hz and tuple(spec.freqs_hz) != profile.default_freqs_hz:
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


def line_buffered(args: list[str]) -> list[str]:
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
        # Called with every block before the consumer (the live service's
        # spectrum history, live_spectrum.SpectrumTap). Its errors are
        # counted, never raised: it must not stop decoding.
        self.tap: Callable[[bytes], None] | None = None
        self.tap_errors = 0
        self.tap_error: str | None = None

    def _deliver(self, consumer: Callable[[bytes], None], buf: bytes) -> None:
        if self.tap is not None:
            try:
                self.tap(buf)
            except Exception as exc:
                self.tap_errors += 1
                self.tap_error = repr(exc)
        consumer(buf)

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
        h = {"bytes": self.bytes_read, "input_ratio": self.input_ratio()}
        if self.tap_errors:
            h["spectrum_tap_errors"] = self.tap_errors
            h["spectrum_tap_error"] = self.tap_error
        return h


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
                self._deliver(consumer, buf)
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
                    self._deliver(consumer, buf)
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


class ChannelPipeline(Pipeline):
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
                line_buffered(self.decoder_args(f)),
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


def make_pipeline(tuning: Tuning, ctx: Context) -> Pipeline:
    return tuning.profile.pipeline(tuning, ctx)
