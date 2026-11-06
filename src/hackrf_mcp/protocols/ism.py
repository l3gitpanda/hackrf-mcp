"""ISM-band devices through rtl_433: weather stations, temperature and
humidity sensors, tyre-pressure sensors, remotes, doorbells, utility meters
and the rest of its 200+ protocols.

Live: the "ism" profile cuts one channel out of the HackRF's 2 MS/s stream
for each frequency given (433.92 MHz by default), ISM_RATE wide, or WIDE_RATE
in the 902-928 MHz band where devices spread wider, tuned well clear of the
DC spike, and feeds it to rtl_433 as 16-bit I/Q on stdin (the channel is
far quieter than the 2 MHz it came from; 8 bits would round a weak device
away). Every message
rtl_433 decodes is a row with every field it gave (its own clock aside); a
message repeated within DEDUPE_S (most devices send each reading two or
three times) is one row. The subject is the device: its model, id and
channel.

Offline: the "ism" decoder runs rtl_433 over a capture as hackrf_decode_ism
does, for hackrf_decode and the web UI's captures.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from hackrf_mcp import decoders, live_profiles
from hackrf_mcp.decoders import Capture, Decoder, Result
from hackrf_mcp.live_log import Row
from hackrf_mcp.live_profiles import ChannelPipeline, Profile, View

ISM_RATE = 250_000
WIDE_RATE = 1_000_000
WIDE_BAND_HZ = (902_000_000, 928_000_000)
INPUT_RATE = 2_000_000
# Two readings of one device within this are the same message sent again.
DEDUPE_S = 3.0
# rtl_433's fields that say how it was heard rather than what was sent:
# kept on the row, but not compared when repeats are dropped.
RECEPTION = ("rssi", "snr", "noise", "freq", "freq1", "freq2", "mod", "protocol", "mic")
KIND = "ism"


def rtl_433_args(program: str, rate: int, freq_hz: int, source: str = "cs8:-") -> list[str]:
    return [
        program, "-r", source, "-s", str(rate), "-f", str(freq_hz),
        "-F", "json", "-M", "level", "-M", "protocol",
    ]  # fmt: skip


def subject_of(e: dict) -> str:
    """The device: rtl_433's model, then its id and channel when it gave them."""
    parts = [str(e.get("model") or "unknown")]
    if e.get("id") not in (None, ""):
        parts.append(str(e["id"]))
    if e.get("channel") not in (None, ""):
        parts.append(f"ch{e['channel']}")
    return " ".join(parts)[:120]


def row_of(e: dict, freq_hz: int, ts: float) -> Row:
    fields = {k: v for k, v in e.items() if k != "time"}
    heard = e.get("freq")
    try:
        heard_hz = int(round(float(heard) * 1e6)) if heard is not None else freq_hz
    except (TypeError, ValueError):
        heard_hz = freq_hz
    level = e.get("rssi")
    return Row(
        ts=ts,
        profile="ism",
        kind=KIND,
        fields=fields,
        raw=json.dumps(e, sort_keys=True)[:4000],
        freq_hz=heard_hz,
        level_db=float(level) if isinstance(level, (int, float)) else None,
        subject=subject_of(e),
        label=str(e["model"]) if e.get("model") else None,
    )


class Dedupe:
    """Drops a device's message when the same one came within DEDUPE_S."""

    def __init__(self, window_s: float = DEDUPE_S):
        self.window_s = window_s
        self.last: dict[str, tuple[float, str]] = {}

    def fresh(self, e: dict, ts: float) -> bool:
        subject = subject_of(e)
        sent = json.dumps({k: v for k, v in e.items() if k not in RECEPTION and k != "time"}, sort_keys=True, default=str)
        prev = self.last.get(subject)
        self.last[subject] = (ts, sent)
        if len(self.last) > 5000:  # a long run hearing many devices
            cutoff = ts - self.window_s
            self.last = {k: v for k, v in self.last.items() if v[0] >= cutoff}
        return not (prev and prev[1] == sent and ts - prev[0] <= self.window_s)


def parse(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    return e if isinstance(e, dict) and e.get("model") else None


# --- Live ------------------------------------------------------------------


class IsmPipeline(ChannelPipeline):
    """One rtl_433 per frequency, each fed its channel as 16-bit I/Q."""

    def __init__(self, tuning, ctx):
        super().__init__(tuning, ctx)
        wide = any(WIDE_BAND_HZ[0] <= f <= WIDE_BAND_HZ[1] for f in tuning.freqs_hz)
        self.out_rate = WIDE_RATE if wide else ISM_RATE
        self.bandwidth_hz = 0.7 * self.out_rate
        self.dedupe = Dedupe()
        self._dlock = threading.Lock()

    def decoder_args(self, freq_hz: int) -> list[str]:
        return rtl_433_args(self.tuning.program, self.out_rate, freq_hz, "cs16:-")

    def convert(self, i: int, y: np.ndarray) -> bytes:
        # The HackRF's full scale stays full scale; the extra bits keep what
        # the channel filter took out of the noise.
        return decoders.sc16(y)

    def on_line(self, i: int, freq_hz: int, line: str) -> None:
        e = parse(line)
        if e is None:
            return
        now = time.time()
        self.ctx.count(1)
        with self._dlock:
            fresh = self.dedupe.fresh(e, now)
        if fresh:
            self.ctx.emit(row_of(e, freq_hz, now))


live_profiles.register(
    Profile(
        "ism",
        "ISM-band devices (rtl_433): weather stations, sensors, tyre-pressure sensors, remotes, meters; every field logged",
        ("rtl_433",),
        IsmPipeline,
        INPUT_RATE,
        default_freqs_hz=(433_920_000,),
        offer_freqs=True,
        help="""  ism    - ISM-band devices through rtl_433 (weather stations,
           sensors, tyre-pressure sensors, remotes, utility meters) on the
           frequency in frequencies_hz: 433.92 MHz by default, 315 MHz for
           US tyre sensors and remotes, 915 MHz for the 902-928 MHz band.
           One frequency a run (a rotation can visit each); every field
           rtl_433 decodes is logged.""",
        summary_kinds=(KIND,),
        summary_fields=("model", "id", "channel", "temperature_C", "temperature_F", "humidity", "battery_ok"),
        views=(
            View(
                "table",
                (KIND,),
                (
                    "model", "id", "channel", "temperature_C", "temperature_F", "humidity", "battery_ok",
                    "pressure_kPa", "pressure_PSI", "wind_avg_km_h", "rain_mm", "consumption_data",
                ),
            ),  # fmt: skip
            View("timeline", (KIND,)),
        ),
        # Wide enough that a 902-928 MHz channel (WIDE_RATE) and the DC
        # spike stay apart in one 2 MS/s tuning: the channel ends up about
        # 370 kHz from the centre.
        channel_bw_hz=700_000,
    )
)


# --- Offline ---------------------------------------------------------------


def decode_ism(cap: Capture, program: str, timeout: float, workdir: Path, freqs: list[int]) -> Result:
    """rtl_433 over the whole capture, at its own rate (its decoders are
    frequency-tolerant): every message, and a line per device."""

    def chunks() -> Iterator[bytes]:
        left = None if cap.max_samples is None else 2 * cap.max_samples
        with open(cap.path, "rb") as f:
            while left is None or left > 0:
                buf = f.read(1 << 20 if left is None else min(1 << 20, left))
                if not buf:
                    return
                if left is not None:
                    left -= len(buf)
                yield buf

    run = decoders.run_program(rtl_433_args(program, cap.sample_rate_hz, cap.center_freq_hz), chunks(), timeout, workdir)
    events = [e for e in (parse(line) for line in run.output.splitlines()) if e is not None]
    if not events:
        return decoders.nothing("ism", run, [])
    devices: dict[str, list[dict]] = {}
    for e in events:
        devices.setdefault(subject_of(e), []).append(e)

    def brief(e: dict) -> str:
        return " ".join(f"{k}={v}" for k, v in e.items() if k not in ("time", "model", "id", "channel") and k not in RECEPTION)

    table = [f"  {s}: {len(es)} message(s); last {brief(es[-1])}"[:300] for s, es in sorted(devices.items())]
    messages = [f"  {subject_of(e)} {brief(e)}"[:300] for e in events]
    return Result(f"ism: {len(events)} messages from {len(devices)} device(s).", table, messages, run.output, [])


decoders.register(
    Decoder(
        "ism",
        ("rtl_433",),
        "ISM-band devices (rtl_433): weather stations, sensors, tyre-pressure sensors, remotes, meters",
        "On the band's usual frequency at 2 MS/s: 433.92, 315 or 915 MHz; a minute or more, as most send every 15-60 s.",
        lambda job: decode_ism(job.cap, job.program, job.timeout, job.workdir, job.freqs),
        (315_000_000, 433_920_000, 915_000_000),
        help="""  ism   - ISM-band devices through rtl_433 (weather stations,
          sensors, tyre-pressure sensors, remotes, meters): capture at
          2,000,000 S/s on or near 433.92, 315 or 915 MHz.""",
        rate_ok=lambda r: 250_000 <= r <= 10_000_000,
        tune_offset_hz=300_000,
    )
)
