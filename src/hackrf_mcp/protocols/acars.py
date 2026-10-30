"""ACARS aircraft datalink (VHF AM, 129-131.6 MHz), with acarsdec:
offline decoding and the live `acars` profile."""

from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import numpy as np
from scipy import signal as scipy_signal

from hackrf_mcp import decoders, iq, live_profiles
from hackrf_mcp.decoders import (
    Capture,
    DecodeError,
    Decoder,
    Result,
    channels_in_capture,
    close_to_dc,
    dc_block,
    json_lines,
    s16,
    tail,
)
from hackrf_mcp.live_log import Row
from hackrf_mcp.live_profiles import ChannelPipeline, Profile, wav_header


ACARS_RATE = 12_500


def decode_acars(cap: Capture, program: str, timeout: float, workdir: Path, freqs: list[int]) -> Result:
    if cap.sample_rate_hz % ACARS_RATE:
        raise DecodeError(
            f"ACARS needs a sample rate that is a multiple of {ACARS_RATE} S/s (e.g. 2,000,000); "
            f"this capture is {cap.sample_rate_hz} S/s."
        )
    chans = channels_in_capture(cap, freqs, 10_000, "ACARS")
    dec = cap.sample_rate_hz // ACARS_RATE
    started = time.monotonic()
    messages: list[str] = []
    raw_parts = []
    notes = [f"Channels decoded: {', '.join(f'{f / 1e6:.3f}' for f in chans)} MHz."]
    for f in chans:
        notes[-1] += close_to_dc(cap, f, 10_000)
        env_parts = [
            np.abs(c)
            for c in iq.channelize(
                cap.path, cap.sample_rate_hz, f - cap.center_freq_hz, dec, cap.max_samples, bandwidth=0.8
            )
        ]
        env = np.concatenate(env_parts) if env_parts else np.zeros(0, np.float32)
        audio = dc_block(env.astype(np.float32), ACARS_RATE, 100.0)
        ref = float(np.percentile(np.abs(audio), 99.9)) if audio.size else 0.0
        wav_path = workdir / f"acars-{f}.wav"
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(ACARS_RATE)
            w.writeframes(s16(audio, 16000 / ref if ref > 0 else 1.0))
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            notes.append("Stopped at the timeout before every channel was decoded.")
            break
        run = decoders.run_program([program, "-o", "4", "-f", str(wav_path)], None, remaining, workdir)
        raw_parts.append(run.output)
        for e in json_lines(run.output):
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
        return Result("acars: nothing decoded.", [], [], raw, notes + ["--- decoder output (tail) ---", tail(raw)])
    return Result(f"acars: {len(messages)} messages.", [], messages, raw, notes)


# --- Live ------------------------------------------------------------------


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


class AcarsPipeline(ChannelPipeline):
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
        return s16(out, 16_000.0)

    def on_line(self, i: int, freq_hz: int, line: str) -> None:
        row = parse_acars(line, freq_hz, time.time())
        if row is not None:
            self.ctx.emit(row)
            self.ctx.count(1)


decoders.register(
    Decoder(
        "acars",
        ("acarsdec",),
        "ACARS aircraft datalink (VHF AM, 129-131.6 MHz)",
        "130.8 MHz at 2 MS/s covers 130.025-131.550 MHz (4 MS/s at 130.35 MHz adds 129.125); the rate must be a multiple of 12.5 kS/s; 60 s.",
        lambda job: decode_acars(job.cap, job.program, job.timeout, job.workdir, job.freqs),
        (129_125_000, 130_025_000, 130_425_000, 130_450_000, 131_125_000, 131_550_000),
        help="""  acars - aircraft datalink, 130.8 MHz at 2,000,000 S/s covers the
          US channels 130.025-131.550 MHz (4,000,000 S/s at 130.35 MHz
          adds 129.125).""",
    )
)

live_profiles.register(
    Profile(
        "acars",
        "ACARS aircraft datalink, 130.8 MHz +-0.8 MHz (acarsdec)",
        ("acarsdec",),
        AcarsPipeline,
        2_000_000,
        130_800_000,
        (130_025_000, 130_425_000, 130_450_000, 131_125_000, 131_550_000),
        help="  acars  - aircraft datalink, the US channels 130.025-131.550 MHz.",
        summary_kinds=("acars",),
        summary_fields=("flight", "label", "mode"),
    )
)
