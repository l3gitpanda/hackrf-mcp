"""RDS on broadcast FM, with redsea: offline decoding only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np

from hackrf_mcp import decoders, iq
from hackrf_mcp.decoders import (
    Capture,
    DecodeError,
    Decoder,
    Result,
    channels_in_capture,
    close_to_dc,
    json_lines,
    nothing,
    s16,
)


def decode_rds(cap: Capture, program: str, timeout: float, workdir: Path, freqs: list[int]) -> Result:
    if len(freqs) != 1:
        raise DecodeError("rds needs exactly one station frequency in frequencies_hz.")
    f = channels_in_capture(cap, freqs, 100_000, "FM station")[0]
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
            yield s16(audio, 0.6 * 32767 / (2 * np.pi * 75_000 / rate))

    run = decoders.run_program([program, "-r", str(rate)], mpx(), timeout, workdir)
    events = json_lines(run.output)
    notes = [close_to_dc(cap, f, 100_000).strip()] if close_to_dc(cap, f, 100_000) else []
    if not events:
        return nothing("rds", run, notes)
    info: dict[str, object] = {}
    for e in events:
        for key in ("pi", "ps", "prog_type", "radiotext", "partial_radiotext", "callsign", "tp", "ta"):
            if e.get(key) not in (None, ""):
                info[key] = e[key]
    table = [f"  {k}={v!r}" for k, v in info.items()]
    return Result(f"rds: {len(events)} groups from {f / 1e6:.1f} MHz.", table, [], run.output, notes)


decoders.register(
    Decoder(
        "rds",
        ("redsea",),
        "RDS on broadcast FM (station name, radiotext, programme type)",
        "Tune 300-500 kHz off the station at 2 MS/s and pass the station frequency; 10-30 s.",
        lambda job: decode_rds(job.cap, job.program, job.timeout, job.workdir, job.freqs),
        help="""  rds   - FM broadcast RDS; pass the station in frequencies_hz and
          capture 300-500 kHz off it at 2,000,000 S/s.""",
    )
)
