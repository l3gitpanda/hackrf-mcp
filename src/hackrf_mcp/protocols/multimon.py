"""Pagers (FLEX, POCSAG), APRS, EAS/SAME and DTMF, with multimon-ng: offline
decoding and the live `flex`, `pocsag`, `aprs` and `eas` profiles.

Pager message text is personal. Unless the operator sets
HACKRF_MCP_PAGER_CONTENT, MultimonParser drops it, and every continuation
line it runs onto, before anything is stored or shown: the live log keeps
only each page's header, and hackrf_decode shows and saves only that too.
"""

from __future__ import annotations

import re
import threading
import time
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
    fm_audio,
    resample,
    s16,
)
from hackrf_mcp.live_log import Row
from hackrf_mcp.live_profiles import ChannelPipeline, Profile


MULTIMON_RATE = 22_050
# 2,205,000 S/s from the HackRF divides down to 22,050 exactly.
MULTIMON_INPUT_RATE = 2_205_000
MULTIMON_MODES = {
    "pocsag": ["POCSAG512", "POCSAG1200", "POCSAG2400"],
    "flex": ["FLEX"],
    "aprs": ["AFSK1200"],
    "eas": ["EAS"],
    "dtmf": ["DTMF"],
}
PAGER_DECODERS = ("pocsag", "flex")
NOAA_CHANNELS = (162_400_000, 162_425_000, 162_450_000, 162_475_000, 162_500_000, 162_525_000, 162_550_000)


# --- Parsing multimon-ng's output ------------------------------------------


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


# --- Offline ---------------------------------------------------------------


def decode_multimon(
    cap: Capture,
    program: str,
    timeout: float,
    workdir: Path,
    name: str,
    freqs: list[int],
    pager_content: bool = False,
) -> Result:
    if not freqs:
        raise DecodeError(f"{name} needs frequencies_hz: the channel(s) to decode.")
    chans = channels_in_capture(cap, freqs, 8_000, name)
    started = time.monotonic()
    messages: list[str] = []
    raw_parts = []
    notes = []
    redact = name in PAGER_DECODERS and not pager_content
    if redact:
        notes.append(
            "Pager message text is not shown (HACKRF_MCP_PAGER_CONTENT is off): "
            "only each page's time, rate, frame, capcode and type."
        )
    for f in chans:
        note = close_to_dc(cap, f, 10_000)
        if note:
            notes.append(note.strip())
        audio, rate = fm_audio(cap, f, 16_000, 40_000)
        audio = dc_block(audio, rate)
        audio = resample(audio, rate, MULTIMON_RATE)
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
        pcm = s16(audio, scale)
        run = decoders.run_program(args, (pcm[i : i + (1 << 20)] for i in range(0, len(pcm), 1 << 20)), remaining, workdir)
        if redact:
            # Through the same parser the live log uses: only each page's
            # header survives, in the result and in the saved output alike.
            parser = MultimonParser(name, f, pager_content=False)
            rows = [r for line in run.output.splitlines() for r in parser.feed(line, 0.0)] + parser.flush()
            raw_parts.append("\n".join(r.raw or "" for r in rows))
            messages += [f"  {f / 1e6:.4f} MHz {r.raw}" for r in rows]
            continue
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


# --- Live ------------------------------------------------------------------


class MultimonPipeline(ChannelPipeline):
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
        return s16(audio, 0.5 * 32767 / (2 * np.pi * 5_000 / MULTIMON_RATE))

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


def _offline(name: str, summary: str, recipe: str, default_freqs: tuple[int, ...] = (), help: str = "") -> None:
    decoders.register(
        Decoder(
            name,
            ("multimon-ng",),
            summary,
            recipe,
            lambda job: decode_multimon(
                job.cap, job.program, job.timeout, job.workdir, job.name, job.freqs, job.pager_content
            ),
            default_freqs,
            help=help,
        )
    )


_offline(
    "pocsag",
    "POCSAG pagers (512/1200/2400 baud)",
    "Tune 25-50 kHz off the pager frequency at 2 MS/s and pass the frequency; 60 s.",
    help="""  pocsag, flex - pagers; pass the pager frequency in frequencies_hz and
          capture 25-50 kHz off it, at 2,000,000 S/s. Message text is shown
          only if the server operator allows it.""",
)
_offline(
    "flex",
    "FLEX pagers (US 929-932 MHz)",
    "Tune 25-50 kHz off the pager frequency at 2 MS/s and pass the frequency; 60 s.",
)
_offline(
    "aprs",
    "APRS packet radio (AFSK 1200, US 144.390 MHz)",
    "144.35 MHz at 2 MS/s; 60 s. Stations beacon every few minutes.",
    (144_390_000,),
    help="  aprs  - packet radio, US 144.390 MHz; capture at 144.35 MHz.",
)
_offline(
    "eas",
    "EAS/SAME alert headers, e.g. NOAA Weather Radio (162.400-162.550 MHz)",
    "162.475 MHz at 2 MS/s covers all seven channels; headers are sent only with alerts and tests.",
    NOAA_CHANNELS,
    help="""  eas   - EAS/SAME headers (NOAA Weather Radio 162.400-162.550 MHz;
          capture at 162.475 MHz). Headers come only with alerts/tests.""",
)
_offline(
    "dtmf",
    "DTMF tones on an FM voice channel",
    "Tune 25-50 kHz off the channel at 2 MS/s and pass the frequency.",
    help="  dtmf  - DTMF on an FM channel given in frequencies_hz.",
)

live_profiles.register(
    Profile(
        "flex",
        "FLEX pagers on the channels given (multimon-ng); metadata only unless pager content is on",
        ("multimon-ng",),
        MultimonPipeline,
        MULTIMON_INPUT_RATE,
        needs_freqs=True,
        help="""  flex, pocsag - pagers on the channels in frequencies_hz (required,
           e.g. [931750000]); several channels must fit within about
           1.7 MHz. Only time, channel, capcode and message type are
           logged unless the server operator has turned on pager
           content logging (hackrf_live_status says which).""",
        summary_kinds=("page",),
        summary_fields=("type", "mode"),
    )
)
live_profiles.register(
    Profile(
        "pocsag",
        "POCSAG pagers on the channels given (multimon-ng); metadata only unless pager content is on",
        ("multimon-ng",),
        MultimonPipeline,
        MULTIMON_INPUT_RATE,
        needs_freqs=True,
        summary_kinds=("page",),
        summary_fields=("type", "function", "protocol"),
    )
)
live_profiles.register(
    Profile(
        "aprs",
        "APRS packet radio, US 144.390 MHz (multimon-ng)",
        ("multimon-ng",),
        MultimonPipeline,
        MULTIMON_INPUT_RATE,
        default_freqs_hz=(144_390_000,),
        help="  aprs   - APRS packet radio, US 144.390 MHz.",
        summary_kinds=("aprs",),
        summary_fields=("dst", "path"),
    )
)
live_profiles.register(
    Profile(
        "eas",
        "EAS/SAME headers on NOAA Weather Radio, 162.400-162.550 MHz (multimon-ng)",
        ("multimon-ng",),
        MultimonPipeline,
        MULTIMON_INPUT_RATE,
        default_freqs_hz=NOAA_CHANNELS,
        help="""  eas    - EAS/SAME alert headers on the seven NOAA Weather Radio
           channels, 162.400-162.550 MHz (they carry headers only
           during alerts and weekly tests).""",
        summary_kinds=("eas",),
        summary_fields=("event", "originator", "locations", "duration"),
    )
)
