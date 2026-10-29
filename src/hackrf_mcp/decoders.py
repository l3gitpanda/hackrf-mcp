"""Protocol decoders for IQ capture files, run as external programs.

Each decoder takes a capture written by hackrf_transfer (8-bit signed I/Q),
turns it into the input its decoder program expects, and summarises what
the program printed. Conversion happens in chunks (see iq.channelize), and
the converted stream goes to the program's stdin wherever it can read one,
so nothing is ever held in memory or on disk at full length.

The programs are found on PATH; none of them is a Python dependency. Which
ones are installed is reported by available().
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import wave
from collections.abc import Iterable, Iterator
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
class Decoder:
    name: str
    programs: tuple[str, ...]  # any one of these will do, in preference order
    summary: str
    recipe: str  # how to capture for it
    default_freqs_hz: tuple[int, ...] = ()


DECODERS: dict[str, Decoder] = {
    d.name: d
    for d in (
        Decoder(
            "adsb",
            ("dump1090-mutability", "dump1090"),
            "ADS-B / Mode S aircraft transponders (1090 MHz)",
            "1090 MHz at 2.4 MS/s (best) or 2.0 MS/s; 10-60 s. Other multiples of 2.4 MS/s are decimated.",
            (1_090_000_000,),
        ),
        Decoder(
            "uat",
            ("dump978-fa",),
            "UAT aircraft transponders, US general aviation (978 MHz)",
            "978 MHz at 2,083,333 S/s; 10-60 s.",
            (978_000_000,),
        ),
        Decoder(
            "ais",
            ("AIS-catcher",),
            "AIS ship transponders (161.975 and 162.025 MHz)",
            "162.0 MHz at 2 MS/s; 30-60 s. Any rate works.",
            (161_975_000, 162_025_000),
        ),
        Decoder(
            "acars",
            ("acarsdec",),
            "ACARS aircraft datalink (VHF AM, 129-131.6 MHz)",
            "130.8 MHz at 2 MS/s covers 130.025-131.550 MHz (4 MS/s at 130.35 MHz adds 129.125); the rate must be a multiple of 12.5 kS/s; 60 s.",
            (129_125_000, 130_025_000, 130_425_000, 130_450_000, 131_125_000, 131_550_000),
        ),
        Decoder(
            "vdl2",
            ("dumpvdl2",),
            "VDL Mode 2 aircraft datalink (136.6-137 MHz)",
            "136.8 MHz at 2,100,000 S/s (a multiple of 105 kS/s is required); 60 s.",
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
        Decoder(
            "pocsag",
            ("multimon-ng",),
            "POCSAG pagers (512/1200/2400 baud)",
            "Tune 25-50 kHz off the pager frequency at 2 MS/s and pass the frequency; 60 s.",
        ),
        Decoder(
            "flex",
            ("multimon-ng",),
            "FLEX pagers (US 929-932 MHz)",
            "Tune 25-50 kHz off the pager frequency at 2 MS/s and pass the frequency; 60 s.",
        ),
        Decoder(
            "aprs",
            ("multimon-ng",),
            "APRS packet radio (AFSK 1200, US 144.390 MHz)",
            "144.35 MHz at 2 MS/s; 60 s. Stations beacon every few minutes.",
            (144_390_000,),
        ),
        Decoder(
            "eas",
            ("multimon-ng",),
            "EAS/SAME alert headers, e.g. NOAA Weather Radio (162.400-162.550 MHz)",
            "162.475 MHz at 2 MS/s covers all seven channels; headers are sent only with alerts and tests.",
            (162_400_000, 162_425_000, 162_450_000, 162_475_000, 162_500_000, 162_525_000, 162_550_000),
        ),
        Decoder(
            "dtmf",
            ("multimon-ng",),
            "DTMF tones on an FM voice channel",
            "Tune 25-50 kHz off the channel at 2 MS/s and pass the frequency.",
        ),
        Decoder(
            "rds",
            ("redsea",),
            "RDS on broadcast FM (station name, radiotext, programme type)",
            "Tune 300-500 kHz off the station at 2 MS/s and pass the station frequency; 10-30 s.",
        ),
    )
}

DECODER_NAMES = tuple(DECODERS)


def find_program(decoder: Decoder) -> str | None:
    for name in decoder.programs:
        if path := shutil.which(name):
            return path
    return None


def available() -> dict[str, str | None]:
    """Map each decoder name to the program that would run it, or None."""
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


def _channels_in_capture(cap: Capture, freqs: Iterable[int], margin_hz: float, what: str) -> list[int]:
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


def _close_to_dc(cap: Capture, freq_hz: float, min_offset_hz: float) -> str:
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


def _sc16(x: np.ndarray) -> bytes:
    out = np.empty(2 * x.size, dtype=np.float32)
    out[0::2] = x.real
    out[1::2] = x.imag
    return np.clip(np.rint(out * 32767), -32768, 32767).astype("<i2").tobytes()


def _fm_audio(
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


def _resample(audio: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    frac = Fraction(out_rate, in_rate)
    if frac == 1:
        return audio
    return scipy_signal.resample_poly(audio, frac.numerator, frac.denominator).astype(np.float32)


def _dc_block(audio: np.ndarray, rate: int, cutoff_hz: float = 20.0) -> np.ndarray:
    if audio.size < 16:
        return audio
    sos = scipy_signal.butter(2, cutoff_hz, btype="high", fs=rate, output="sos")
    return scipy_signal.sosfilt(sos, audio).astype(np.float32)


def _s16(audio: np.ndarray, scale: float) -> bytes:
    return np.clip(np.rint(audio * scale), -32768, 32767).astype("<i2").tobytes()


def _json_lines(text: str) -> list[dict]:
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


def _tail(text: str, n: int = 1500) -> str:
    return text[-n:].strip()


def _nothing(name: str, run: RunResult, notes: list[str]) -> Result:
    extra = " (stopped at the timeout)" if run.timed_out else ""
    return Result(
        f"{name}: nothing decoded{extra}.",
        [],
        [],
        run.output,
        notes + ["--- decoder output (tail) ---", _tail(run.output)],
    )


# --- ADS-B -----------------------------------------------------------------

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
    if rate in (2_400_000, 2_000_000) and shift == 0:
        stdin = iq.iter_cu8_chunks(cap.path, max_samples=cap.max_samples)
        fmt, out_rate = "UC8", rate
    elif rate % 2_400_000 == 0 or rate % 2_000_000 == 0:
        out_rate = 2_400_000 if rate % 2_400_000 == 0 else 2_000_000
        gain = AutoGain(0.5)
        stdin = (
            _sc16(gain(c))
            for c in iq.channelize(cap.path, rate, shift, rate // out_rate, cap.max_samples, bandwidth=0.9)
        )
        fmt = "SC16"
        notes.append(
            f"Converted {rate / 1e6:g} MS/s to {out_rate / 1e6:g} MS/s for the decoder; a capture made "
            "at 2.4 MS/s directly decodes best."
        )
    else:
        raise DecodeError(
            f"ADS-B needs a capture at 2,400,000 S/s (best) or 2,000,000 S/s, or a multiple of either; "
            f"this one is {rate} S/s. Recapture at 1090 MHz, 2.4 MS/s."
        )
    if out_rate == 2_000_000:
        notes.append("If little decodes at 2.0 MS/s, recapture at 2.4 MS/s, which dump1090 demodulates better.")
    args = [program, "--ifile", "-", "--iformat", fmt, "--stats"]
    if out_rate == 2_400_000:
        args.append("--oversample")
    if receiver:
        args += ["--lat", str(receiver[0]), "--lon", str(receiver[1])]
    run = run_program(args, stdin, timeout, workdir)
    aircraft, good = parse_dump1090(run.output)
    if not aircraft:
        return _nothing("adsb", run, notes)
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


# --- UAT -------------------------------------------------------------------


def decode_uat(cap: Capture, program: str, timeout: float, workdir: Path) -> Result:
    if abs(cap.sample_rate_hz - 2_083_333) > 1000 or cap.center_freq_hz != 978_000_000:
        raise DecodeError(
            f"UAT needs a capture centered on 978 MHz at 2,083,333 S/s; this one is "
            f"{cap.center_freq_hz} Hz at {cap.sample_rate_hz} S/s."
        )
    total = iq.iq_file_samples(cap.path)
    n = total if cap.max_samples is None else min(total, cap.max_samples)
    raw = np.memmap(cap.path, dtype=np.uint8, mode="r", shape=(2 * n,)) if n else np.zeros(0, np.uint8)
    stdin = (raw[i : i + (1 << 21)].tobytes() for i in range(0, raw.size, 1 << 21))
    run = run_program([program, "--stdin", "--format", "CS8", "--json-stdout"], stdin, timeout, workdir)
    events = _json_lines(run.output)
    if not events:
        return _nothing("uat", run, [])
    by_addr: dict[str, dict] = {}
    for e in events:
        addr = str(e.get("address", "?"))
        a = by_addr.setdefault(addr, {"messages": 0})
        a["messages"] += 1
        for key in ("callsign", "pressure_altitude", "geometric_altitude", "ground_speed", "true_track"):
            if key in e:
                a[key] = e[key]
        if "position" in e and isinstance(e["position"], dict):
            a["lat"], a["lon"] = e["position"].get("lat"), e["position"].get("lon")
    table = [
        "  " + addr + " " + " ".join(f"{k}={v}" for k, v in a.items() if k != "messages") + f" msgs={a['messages']}"
        for addr, a in sorted(by_addr.items(), key=lambda kv: -kv[1]["messages"])
    ]
    return Result(f"uat: {len(by_addr)} aircraft, {len(events)} messages.", table, [], run.output, [])


# --- AIS -------------------------------------------------------------------


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
    run = run_program(args, stdin, timeout, workdir)
    events = [e for e in _json_lines(run.output) if e.get("class") == "AIS"]
    if not events:
        return _nothing("ais", run, [])
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


# --- ACARS -----------------------------------------------------------------

ACARS_RATE = 12_500


def decode_acars(cap: Capture, program: str, timeout: float, workdir: Path, freqs: list[int]) -> Result:
    if cap.sample_rate_hz % ACARS_RATE:
        raise DecodeError(
            f"ACARS needs a sample rate that is a multiple of {ACARS_RATE} S/s (e.g. 2,000,000); "
            f"this capture is {cap.sample_rate_hz} S/s."
        )
    chans = _channels_in_capture(cap, freqs, 10_000, "ACARS")
    dec = cap.sample_rate_hz // ACARS_RATE
    started = time.monotonic()
    messages: list[str] = []
    raw_parts = []
    notes = [f"Channels decoded: {', '.join(f'{f / 1e6:.3f}' for f in chans)} MHz."]
    for f in chans:
        notes[-1] += _close_to_dc(cap, f, 10_000)
        env_parts = [
            np.abs(c)
            for c in iq.channelize(
                cap.path, cap.sample_rate_hz, f - cap.center_freq_hz, dec, cap.max_samples, bandwidth=0.8
            )
        ]
        env = np.concatenate(env_parts) if env_parts else np.zeros(0, np.float32)
        audio = _dc_block(env.astype(np.float32), ACARS_RATE, 100.0)
        ref = float(np.percentile(np.abs(audio), 99.9)) if audio.size else 0.0
        wav_path = workdir / f"acars-{f}.wav"
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(ACARS_RATE)
            w.writeframes(_s16(audio, 16000 / ref if ref > 0 else 1.0))
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            notes.append("Stopped at the timeout before every channel was decoded.")
            break
        run = run_program([program, "-o", "4", "-f", str(wav_path)], None, remaining, workdir)
        raw_parts.append(run.output)
        for e in _json_lines(run.output):
            e.setdefault("freq", f / 1e6)
            text = str(e.get("text", "")).replace("\n", " ").replace("\r", " ")
            if len(text) > 160:
                text = text[:160] + "..."
            messages.append(
                f"  {f / 1e6:.3f} MHz flight={e.get('flight', '')} tail={e.get('tail', '')} "
                f"label={e.get('label', '')} mode={e.get('mode', '')} text={text!r}"
            )
        wav_path.unlink(missing_ok=True)
    raw = "\n".join(raw_parts)
    if not messages:
        return Result("acars: nothing decoded.", [], [], raw, notes + ["--- decoder output (tail) ---", _tail(raw)])
    return Result(f"acars: {len(messages)} messages.", [], messages, raw, notes)


# --- VDL2 ------------------------------------------------------------------

VDL2_SYMBOL_RATE = 105_000


def decode_vdl2(cap: Capture, program: str, timeout: float, workdir: Path, freqs: list[int]) -> Result:
    chans = _channels_in_capture(cap, freqs, 15_000, "VDL2")
    span = max(chans) - min(chans) + 2 * 25_000
    rate = cap.sample_rate_hz
    choices = [
        d for d in range(1, rate // VDL2_SYMBOL_RATE + 1) if rate % d == 0 and (rate // d) % VDL2_SYMBOL_RATE == 0
    ]
    # dumpvdl2's usual setting is 1.05 MS/s (oversample 10); go no lower.
    choices = [d for d in choices if rate // d >= max(span / 0.8, 10 * VDL2_SYMBOL_RATE)]
    if not choices:
        raise DecodeError(
            f"VDL2 needs a sample rate that is a multiple of {VDL2_SYMBOL_RATE} S/s and wide enough for "
            f"the channels; this capture is {rate} S/s. Recapture at 136.8 MHz, 2,100,000 S/s."
        )
    dec = max(choices)
    out_rate = rate // dec
    mid = (max(chans) + min(chans)) // 2
    gain = AutoGain(0.5)
    stdin = (
        _sc16(gain(c))
        for c in iq.channelize(cap.path, rate, mid - cap.center_freq_hz, dec, cap.max_samples, bandwidth=0.9)
    )
    args = [
        program,
        "--iq-file",
        "-",
        "--sample-format",
        # dumpvdl2 2.6's --help says S16LE, but its parser only takes S16_LE.
        "S16_LE",
        "--oversample",
        str(out_rate // VDL2_SYMBOL_RATE),
        "--centerfreq",
        str(mid),
        "--output",
        "decoded:json:file:path=-",
        *[str(f) for f in chans],
    ]
    run = run_program(args, stdin, timeout, workdir)
    notes = [f"Channels decoded: {', '.join(f'{f / 1e6:.3f}' for f in chans)} MHz."]
    events = _json_lines(run.output)
    if not events:
        # Builds without JSON support print text; show it.
        text_msgs = [b.strip() for b in re.split(r"\n\s*\n", run.output) if "[" in b and "]" in b]
        if text_msgs:
            return Result(f"vdl2: {len(text_msgs)} messages.", [], text_msgs, run.output, notes)
        return _nothing("vdl2", run, notes)
    messages = []
    for e in events:
        v = e.get("vdl2", e)
        src = (v.get("avlc") or {}).get("src", {}) if isinstance(v, dict) else {}
        acars = ((v.get("avlc") or {}).get("acars") or {}) if isinstance(v, dict) else {}
        line = f"  {v.get('freq', 0) / 1e6:.3f} MHz from {src.get('addr', '?')} ({src.get('type', '?')})"
        if acars:
            text = str(acars.get("msg_text", "")).replace("\n", " ")[:160]
            line += f" acars flight={acars.get('flight', '')} reg={acars.get('reg', '')} label={acars.get('label', '')} text={text!r}"
        messages.append(line)
    return Result(f"vdl2: {len(events)} frames.", [], messages, run.output, notes)


# --- multimon-ng (pagers, APRS, EAS, DTMF) --------------------------------

MULTIMON_RATE = 22_050
MULTIMON_MODES = {
    "pocsag": ["POCSAG512", "POCSAG1200", "POCSAG2400"],
    "flex": ["FLEX"],
    "aprs": ["AFSK1200"],
    "eas": ["EAS"],
    "dtmf": ["DTMF"],
}


def decode_multimon(
    cap: Capture, program: str, timeout: float, workdir: Path, name: str, freqs: list[int]
) -> Result:
    if not freqs:
        raise DecodeError(f"{name} needs frequencies_hz: the channel(s) to decode.")
    chans = _channels_in_capture(cap, freqs, 8_000, name)
    started = time.monotonic()
    messages: list[str] = []
    raw_parts = []
    notes = []
    for f in chans:
        note = _close_to_dc(cap, f, 10_000)
        if note:
            notes.append(note.strip())
        audio, rate = _fm_audio(cap, f, 16_000, 40_000)
        audio = _dc_block(audio, rate)
        audio = _resample(audio, rate, MULTIMON_RATE)
        # Fixed scaling, so a quiet channel stays quiet: +-5 kHz deviation
        # (narrowband FM's full swing) maps to half of full scale.
        scale = 0.5 * 32767 / (2 * np.pi * 5_000 / rate)
        args = [program, "-t", "raw", "-q", "-c"]
        for mode in MULTIMON_MODES[name]:
            args += ["-a", mode]
        if name == "aprs":
            args.append("-A")
        if name == "pocsag":
            args.append("-e")
        args.append("-")
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            notes.append("Stopped at the timeout before every channel was decoded.")
            break
        pcm = _s16(audio, scale)
        run = run_program(args, (pcm[i : i + (1 << 20)] for i in range(0, len(pcm), 1 << 20)), remaining, workdir)
        raw_parts.append(run.output)
        for line in run.output.splitlines():
            line = line.strip()
            if not line or line.startswith(("multimon-ng", "(C)", "Enabled demodulators", "Available")):
                continue
            messages.append(f"  {f / 1e6:.4f} MHz {line}")
    raw = "\n".join(raw_parts)
    notes.insert(0, f"Channels decoded: {', '.join(f'{f / 1e6:.4f}' for f in chans)} MHz.")
    if not messages:
        return Result(f"{name}: nothing decoded.", [], [], raw, notes)
    return Result(f"{name}: {len(messages)} lines decoded.", [], messages, raw, notes)


# --- RDS -------------------------------------------------------------------


def decode_rds(cap: Capture, program: str, timeout: float, workdir: Path, freqs: list[int]) -> Result:
    if len(freqs) != 1:
        raise DecodeError("rds needs exactly one station frequency in frequencies_hz.")
    f = _channels_in_capture(cap, freqs, 100_000, "FM station")[0]
    dec = iq.pick_decimation(cap.sample_rate_hz, 171_000)
    rate = cap.sample_rate_hz // dec
    if rate > 400_000 or rate < 171_000:
        raise DecodeError(
            f"rds needs a sample rate with an integer divisor between 171 and 400 kS/s (e.g. 2 MS/s); "
            f"this capture is {cap.sample_rate_hz} S/s."
        )
    bw = min(0.95, 200_000 / rate)

    def mpx() -> Iterator[bytes]:
        prev = None
        for chunk in iq.channelize(
            cap.path, cap.sample_rate_hz, f - cap.center_freq_hz, dec, cap.max_samples, bandwidth=bw
        ):
            audio, prev = iq.fm_discriminate(chunk, prev)
            # +-75 kHz deviation maps to about 60% of full scale.
            yield _s16(audio, 0.6 * 32767 / (2 * np.pi * 75_000 / rate))

    run = run_program([program, "-r", str(rate)], mpx(), timeout, workdir)
    events = _json_lines(run.output)
    notes = [_close_to_dc(cap, f, 100_000).strip()] if _close_to_dc(cap, f, 100_000) else []
    if not events:
        return _nothing("rds", run, notes)
    info: dict[str, object] = {}
    for e in events:
        for key in ("pi", "ps", "prog_type", "radiotext", "partial_radiotext", "callsign", "tp", "ta"):
            if e.get(key) not in (None, ""):
                info[key] = e[key]
    table = [f"  {k}={v!r}" for k, v in info.items()]
    return Result(f"rds: {len(events)} groups from {f / 1e6:.1f} MHz.", table, [], run.output, notes)


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
) -> Result:
    if name not in DECODERS:
        raise DecodeError(f"unknown decoder {name!r}; choose one of {', '.join(DECODER_NAMES)}")
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
        workdir = Path(tmp)
        if name == "adsb":
            return decode_adsb(cap, program, timeout, workdir, receiver)
        if name == "uat":
            return decode_uat(cap, program, timeout, workdir)
        if name == "ais":
            return decode_ais(cap, program, timeout, workdir)
        if name == "acars":
            return decode_acars(cap, program, timeout, workdir, freqs)
        if name == "vdl2":
            return decode_vdl2(cap, program, timeout, workdir, freqs)
        if name == "rds":
            return decode_rds(cap, program, timeout, workdir, freqs)
        return decode_multimon(cap, program, timeout, workdir, name, freqs)
