"""Offline protocol decoding: the core behind hackrf_decode.

A decoder turns a capture written by hackrf_transfer (8-bit signed I/Q) into
the input its decoder program expects, runs the program, and summarises
what it printed. Conversion happens in chunks (see iq.channelize), and the
converted stream goes to the program's stdin wherever it can read one, so
nothing is ever held in memory or on disk at full length.

The decoders themselves live in hackrf_mcp/protocols/, one module per
protocol family, and each registers itself here with register(). This
module holds what they share: the Job they are given, running a program,
the Capture description, and helpers for channelising and formatting.

The programs are found on PATH; none of them is a Python dependency. Which
ones are installed is reported by available().
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
from scipy import signal as scipy_signal

from hackrf_mcp import iq


class DecodeError(ValueError):
    """The capture can't be decoded as asked (wrong rate, frequency outside
    the capture, decoder missing). The message says what to change."""


@dataclass(frozen=True)
class Job:
    """One decode request, as a decoder's run() receives it."""

    name: str
    cap: Capture
    program: str
    timeout: float
    workdir: Path
    freqs: list[int]
    receiver: tuple[float, float] | None = None
    # Whether pager (FLEX/POCSAG) message text may be shown; see
    # settings.LiveSettings.pager_content.
    pager_content: bool = False


@dataclass(frozen=True)
class Decoder:
    name: str
    programs: tuple[str, ...]  # any one of these will do, in preference order
    summary: str
    recipe: str  # how to capture for it
    run: Callable[[Job], Result]
    default_freqs_hz: tuple[int, ...] = ()
    # This decoder's lines in hackrf_decode's description (several decoders
    # may share one entry; the others leave it empty).
    help: str = ""


DECODERS: dict[str, Decoder] = {}


def register(decoder: Decoder) -> Decoder:
    """Add a decoder to hackrf_decode. Called by the protocol modules."""
    if decoder.name in DECODERS:
        raise ValueError(f"decoder {decoder.name!r} is registered twice")
    DECODERS[decoder.name] = decoder
    return decoder


def load() -> None:
    """Import every protocol module, so the registry is complete."""
    import hackrf_mcp.protocols  # noqa: F401


def names() -> tuple[str, ...]:
    load()
    return tuple(DECODERS)


def tool_help() -> str:
    """The per-decoder lines of hackrf_decode's description."""
    load()
    return "\n".join(d.help.rstrip() for d in DECODERS.values() if d.help)


def find_program(decoder: Decoder) -> str | None:
    for name in decoder.programs:
        if path := shutil.which(name):
            return path
    return None


def available() -> dict[str, str | None]:
    """Map each decoder name to the program that would run it, or None."""
    load()
    return {name: find_program(d) for name, d in DECODERS.items()}


# --- Running a program -----------------------------------------------------


@dataclass
class RunResult:
    output: str
    returncode: int | None
    timed_out: bool
    seconds: float


def run_program(
    args: list[str],
    stdin_chunks: Iterable[bytes] | None,
    timeout: float,
    workdir: Path,
) -> RunResult:
    """Run a decoder, feeding it `stdin_chunks` (if any), and return what it
    printed. Output goes through a file so a chatty program can't block on a
    full pipe while we are still writing its input."""
    started = time.monotonic()
    deadline = started + timeout
    out_path = workdir / "decoder.out"
    timed_out = False
    with open(out_path, "wb") as out:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_chunks is not None else subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=workdir,
        )
        try:
            if stdin_chunks is not None:
                try:
                    for chunk in stdin_chunks:
                        if time.monotonic() > deadline:
                            timed_out = True
                            break
                        proc.stdin.write(chunk)
                except BrokenPipeError:
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
            if not timed_out:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    return RunResult(
        out_path.read_text(errors="replace"),
        proc.returncode,
        timed_out,
        time.monotonic() - started,
    )


# --- Shared helpers --------------------------------------------------------


@dataclass(frozen=True)
class Capture:
    path: Path
    sample_rate_hz: int
    center_freq_hz: int
    max_samples: int | None

    @property
    def low_hz(self) -> float:
        return self.center_freq_hz - self.sample_rate_hz / 2

    @property
    def high_hz(self) -> float:
        return self.center_freq_hz + self.sample_rate_hz / 2

    def covers(self, freq_hz: float, margin_hz: float = 0.0) -> bool:
        # Keep clear of the outer 10% of the band, where the HackRF's
        # anti-alias filter rolls off.
        usable = 0.4 * self.sample_rate_hz
        return abs(freq_hz - self.center_freq_hz) + margin_hz <= usable


def channels_in_capture(cap: Capture, freqs: Iterable[int], margin_hz: float, what: str) -> list[int]:
    freqs = list(freqs)
    inside = [f for f in freqs if cap.covers(f, margin_hz)]
    if not inside:
        listed = ", ".join(f"{f / 1e6:.4f}" for f in freqs) or "(none given)"
        raise DecodeError(
            f"none of the {what} frequencies ({listed} MHz) fall inside the usable part of this "
            f"capture ({(cap.center_freq_hz - 0.4 * cap.sample_rate_hz) / 1e6:.4f}-"
            f"{(cap.center_freq_hz + 0.4 * cap.sample_rate_hz) / 1e6:.4f} MHz). Pass frequencies_hz, "
            "or recapture centered nearer them."
        )
    return inside


def close_to_dc(cap: Capture, freq_hz: float, min_offset_hz: float) -> str:
    if abs(freq_hz - cap.center_freq_hz) < min_offset_hz:
        return (
            f" Note: {freq_hz / 1e6:.4f} MHz sits within {min_offset_hz / 1e3:.0f} kHz of the capture's "
            "center, on the HackRF's DC spike; if nothing decodes, recapture tuned 25-50 kHz away."
        )
    return ""


class AutoGain:
    """Scale complex samples so their level suits a fixed-point format:
    set once from the first chunk (its 99.9th-percentile magnitude maps to
    `target` of full scale), then held, so levels stay comparable."""

    def __init__(self, target: float = 0.7):
        self.target = target
        self.gain: float | None = None

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.gain is None:
            ref = float(np.percentile(np.abs(x), 99.9)) if x.size else 0.0
            self.gain = self.target / ref if ref > 0 else 1.0
        return x * np.float32(self.gain)


def sc16(x: np.ndarray) -> bytes:
    out = np.empty(2 * x.size, dtype=np.float32)
    out[0::2] = x.real
    out[1::2] = x.imag
    return np.clip(np.rint(out * 32767), -32768, 32767).astype("<i2").tobytes()


def fm_audio(
    cap: Capture,
    freq_hz: float,
    channel_bw_hz: float,
    min_rate_hz: float,
) -> tuple[np.ndarray, int]:
    """Narrowband FM demodulate one channel: returns (discriminator output
    in radians per sample, rate). Memory is bounded by the decimated rate."""
    dec = iq.pick_decimation(cap.sample_rate_hz, min_rate_hz)
    rate = cap.sample_rate_hz // dec
    bw = min(0.95, channel_bw_hz / rate)
    parts = []
    prev = None
    for chunk in iq.channelize(
        cap.path, cap.sample_rate_hz, freq_hz - cap.center_freq_hz, dec, cap.max_samples, bandwidth=bw
    ):
        audio, prev = iq.fm_discriminate(chunk, prev)
        parts.append(audio)
    audio = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return audio, rate


def resample(audio: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    frac = Fraction(out_rate, in_rate)
    if frac == 1:
        return audio
    return scipy_signal.resample_poly(audio, frac.numerator, frac.denominator).astype(np.float32)


def dc_block(audio: np.ndarray, rate: int, cutoff_hz: float = 20.0) -> np.ndarray:
    if audio.size < 16:
        return audio
    sos = scipy_signal.butter(2, cutoff_hz, btype="high", fs=rate, output="sos")
    return scipy_signal.sosfilt(sos, audio).astype(np.float32)


def s16(audio: np.ndarray, scale: float) -> bytes:
    return np.clip(np.rint(audio * scale), -32768, 32767).astype("<i2").tobytes()


def json_lines(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


@dataclass
class Result:
    """What a decoder produced: a headline, per-object lines (aircraft,
    ships, ...), individual messages, and the raw program output."""

    headline: str
    table: list[str]
    messages: list[str]
    raw: str
    notes: list[str]

    def format(self, max_events: int) -> str:
        lines = [self.headline]
        lines += self.notes
        if self.table:
            lines += self.table[:max_events]
            if len(self.table) > max_events:
                lines.append(f"... {len(self.table) - max_events} more not shown")
        if self.messages:
            shown = self.messages[:max_events]
            lines.append(f"--- messages ({len(self.messages)}, first {len(shown)}) ---")
            lines += shown
        return "\n".join(lines)


def tail(text: str, n: int = 1500) -> str:
    return text[-n:].strip()


def nothing(name: str, run: RunResult, notes: list[str]) -> Result:
    extra = " (stopped at the timeout)" if run.timed_out else ""
    return Result(
        f"{name}: nothing decoded{extra}.",
        [],
        [],
        run.output,
        notes + ["--- decoder output (tail) ---", tail(run.output)],
    )


# --- Entry point -----------------------------------------------------------


def decode(
    name: str,
    path: Path,
    sample_rate_hz: int,
    center_freq_hz: int,
    frequencies_hz: list[int] | None,
    max_samples: int | None,
    timeout: float,
    workroot: Path,
    receiver: tuple[float, float] | None = None,
    pager_content: bool = False,
) -> Result:
    load()
    if name not in DECODERS:
        raise DecodeError(f"unknown decoder {name!r}; choose one of {', '.join(DECODERS)}")
    decoder = DECODERS[name]
    program = find_program(decoder)
    if program is None:
        raise DecodeError(
            f"{name} needs {' or '.join(decoder.programs)}, which is not installed on this server."
        )
    cap = Capture(path, sample_rate_hz, center_freq_hz, max_samples)
    freqs = list(frequencies_hz) if frequencies_hz else list(decoder.default_freqs_hz)
    workroot.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="decode-", dir=workroot) as tmp:
        job = Job(name, cap, program, timeout, Path(tmp), freqs, receiver, pager_content)
        return decoder.run(job)
