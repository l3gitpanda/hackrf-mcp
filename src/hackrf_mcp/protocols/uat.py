"""UAT aircraft on 978 MHz (US general aviation), with dump978-fa:
offline decoding only."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from hackrf_mcp import decoders, iq
from hackrf_mcp.decoders import Capture, DecodeError, Decoder, Result, json_lines, nothing


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
    run = decoders.run_program([program, "--stdin", "--format", "CS8", "--json-stdout"], stdin, timeout, workdir)
    events = json_lines(run.output)
    if not events:
        return nothing("uat", run, [])
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


decoders.register(
    Decoder(
        "uat",
        ("dump978-fa",),
        "UAT aircraft transponders, US general aviation (978 MHz)",
        "978 MHz at 2,083,333 S/s; 10-60 s.",
        lambda job: decode_uat(job.cap, job.program, job.timeout, job.workdir),
        (978_000_000,),
        help="  uat   - US general-aviation aircraft, 978 MHz at 2,083,333 S/s.",
    )
)
