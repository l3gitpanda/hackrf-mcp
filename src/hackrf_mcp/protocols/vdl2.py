"""VDL Mode 2 aircraft datalink (136.6-137 MHz), with dumpvdl2: offline
decoding and the live `vdl2` profile."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from hackrf_mcp import decoders, iq, live_profiles
from hackrf_mcp.decoders import (
    AutoGain,
    Capture,
    DecodeError,
    Decoder,
    Result,
    channels_in_capture,
    json_lines,
    nothing,
    sc16,
)
from hackrf_mcp.live_log import Row
from hackrf_mcp.live_profiles import Pipeline, Proc, Profile, line_buffered


VDL2_CHANNELS = (
    136_650_000,
    136_700_000,
    136_725_000,
    136_775_000,
    136_800_000,
    136_825_000,
    136_875_000,
    136_900_000,
    136_975_000,
)

VDL2_SYMBOL_RATE = 105_000


def decode_vdl2(cap: Capture, program: str, timeout: float, workdir: Path, freqs: list[int]) -> Result:
    chans = channels_in_capture(cap, freqs, 15_000, "VDL2")
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
        sc16(gain(c))
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
    run = decoders.run_program(args, stdin, timeout, workdir)
    notes = [f"Channels decoded: {', '.join(f'{f / 1e6:.3f}' for f in chans)} MHz."]
    events = json_lines(run.output)
    if not events:
        # Builds without JSON support print text; show it.
        text_msgs = [b.strip() for b in re.split(r"\n\s*\n", run.output) if "[" in b and "]" in b]
        if text_msgs:
            return Result(f"vdl2: {len(text_msgs)} messages.", [], text_msgs, run.output, notes)
        return nothing("vdl2", run, notes)
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


# --- Live ------------------------------------------------------------------


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
        decoder = Proc("dumpvdl2", line_buffered(args), stdin=True, on_line=self._line)
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


decoders.register(
    Decoder(
        "vdl2",
        ("dumpvdl2",),
        "VDL Mode 2 aircraft datalink (136.6-137 MHz)",
        "136.8 MHz at 2,100,000 S/s (a multiple of 105 kS/s is required); 60 s.",
        lambda job: decode_vdl2(job.cap, job.program, job.timeout, job.workdir, job.freqs),
        VDL2_CHANNELS,
        help="""  vdl2  - aircraft datalink, 136.8 MHz at 2,100,000 S/s (the rate must
          be a multiple of 105,000).""",
    )
)

live_profiles.register(
    Profile(
        "vdl2",
        "VDL Mode 2 aircraft datalink, 136.65-136.975 MHz (dumpvdl2)",
        ("dumpvdl2",),
        Vdl2Pipeline,
        2_100_000,
        # Between two 25 kHz channels, so none sits on the DC spike.
        136_812_500,
        VDL2_CHANNELS,
        help="  vdl2   - aircraft datalink, 136.650-136.975 MHz.",
        summary_kinds=("vdl2",),
        summary_fields=("src_type", "dst", "dst_type"),
    )
)
