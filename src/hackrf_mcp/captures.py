"""Captures from the web UI: a range picked on a waterfall, recorded by the
live service into the capture directory's web/ and decoded there.

hackrf-web never touches the HackRF or the hackrf-agent: it asks the live
service over its control socket (capture-plan, capture, capture-list,
capture-decode, capture-delete; see live_service.py). A capture

1. is planned by plan(): the rate and centre the chosen decoders need, read
   off their registrations (decoders.Decoder's capture fields), so a capture
   planned here is one those decoders take;
2. waits up to capture_wait_s if a device tool or an occupancy sweep has the
   receiver, then takes it from whatever is running (a profile, a
   rotation's entry, or a watch, whose time keeps running), with the same
   kind of hold an occupancy sweep takes;
3. is recorded through the hackrf-agent on a split station (its capture
   tool), else by hackrf_transfer here, and the receiver is given back
   whatever happens;
4. is decoded after the receiver is back, one job at a time, in a child
   process at low priority: each decoder's headline, table, messages and
   notes (never the decoder program's raw output), and a spectrogram
   picture.

Each capture is up to three files in web/: <id>.cs8, the I/Q, which the UI
never serves; <id>.json, what was asked, what happened and the results; and
<id>.png, the picture. They are kept capture_keep_days, or until web/ passes
capture_keep_mb, oldest first.

`python -m hackrf_mcp.captures --decode-job` is the child: it reads one job
as JSON on stdin and prints the results as one JSON line.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from hackrf_mcp import decoders

MIB = 1024 * 1024
# Who holds the receiver while a capture records: the stop event's detail is
# "paused for web capture", which the log's coverage shows as a pause.
OWNER = "web capture"
SUBDIR = "web"
# What the HackRF tunes and samples at (the hackrf-agent checks the same).
MIN_HZ, MAX_HZ = 1_000_000, 6_000_000_000
MIN_RATE, MAX_RATE = 2_000_000, 20_000_000
# Rates a capture with no decoder may have: the narrowest that holds the range.
RATES_HZ = (2_000_000, 4_000_000, 5_000_000, 8_000_000, 10_000_000, 16_000_000, 20_000_000)
# A decoder is suggested when one of its usual frequencies is this close to
# the range picked.
SUGGEST_MARGIN_HZ = 250_000
# With no decoder, a range narrower than this is tuned DEFAULT_OFFSET_HZ
# below, off the DC spike; a wider one is centred on.
NARROW_HZ = 200_000
DEFAULT_OFFSET_HZ = 250_000
# How far inside the usable band a channel must be.
CHANNEL_MARGIN_HZ = 25_000
# The decoder's own channel margins, where they are wider than that.
DECODER_MARGIN_HZ = {"rds": 100_000, "meshtastic": 250_000, "lora": 250_000}
DECODE_TIMEOUT_S = 240.0
PICTURE_TIMEOUT_S = 120.0
# The receiver is held this much longer than the capture lasts, for the
# agent to open the device and the file to arrive; the lease frees it if
# this service dies first.
LEASE_MARGIN_S = 90.0
# Results kept per decoder.
MAX_LINES = 500
MAX_LINE = 400
MAX_NOTES = 40
ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")
# Where a decoder's notes turn into its program's own output (decoders.nothing).
RAW_MARK = "--- decoder output (tail) ---"


class PlanError(ValueError):
    """The capture can't be planned as asked; the message says why."""


def usable_hz(rate: int) -> float:
    # The HackRF's anti-alias filter rolls off in the outer 10% of the band.
    return 0.4 * rate


def mhz(hz: float) -> str:
    return f"{hz / 1e6:.4f}".rstrip("0").rstrip(".") + " MHz"


def rate_text(rate: int) -> str:
    return f"{rate / 1e6:g} MS/s" if rate % 1000 == 0 else f"{rate:,} S/s"


@dataclass(frozen=True)
class Plan:
    """What a capture records: the range picked, where it is tuned, how,
    and which decoders run on it (each with the frequencies it is given,
    None for its own usual ones)."""

    lo_hz: int
    hi_hz: int
    center_hz: int
    sample_rate_hz: int
    seconds: float
    lna_gain_db: int
    vga_gain_db: int
    amp: bool = False
    decoders: tuple[tuple[str, tuple[int, ...] | None], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def num_samples(self) -> int:
        return int(round(self.sample_rate_hz * self.seconds))

    @property
    def bytes(self) -> int:
        # hackrf_transfer writes 2 bytes (8-bit I and Q) per sample.
        return 2 * self.num_samples

    def covers(self) -> tuple[float, float]:
        u = usable_hz(self.sample_rate_hz)
        return self.center_hz - u, self.center_hz + u

    def describe(self) -> str:
        return (
            f"{mhz(self.center_hz)} at {rate_text(self.sample_rate_hz)} for {self.seconds:g} s, "
            f"LNA {self.lna_gain_db} VGA {self.vga_gain_db}" + (" amp on" if self.amp else "")
        )

    def to_json(self) -> dict:
        lo, hi = self.covers()
        return {
            "lo_hz": self.lo_hz,
            "hi_hz": self.hi_hz,
            "center_hz": self.center_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "seconds": self.seconds,
            "num_samples": self.num_samples,
            "bytes": self.bytes,
            "covers_hz": [lo, hi],
            "lna_gain_db": self.lna_gain_db,
            "vga_gain_db": self.vga_gain_db,
            "amp": self.amp,
            "decoders": [{"name": n, "freqs_hz": None if f is None else list(f)} for n, f in self.decoders],
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, d: dict) -> Plan:
        return cls(
            lo_hz=int(d["lo_hz"]),
            hi_hz=int(d["hi_hz"]),
            center_hz=int(d["center_hz"]),
            sample_rate_hz=int(d["sample_rate_hz"]),
            seconds=float(d["seconds"]),
            lna_gain_db=int(d["lna_gain_db"]),
            vga_gain_db=int(d["vga_gain_db"]),
            amp=bool(d.get("amp", False)),
            decoders=tuple(
                (str(e["name"]), None if e.get("freqs_hz") is None else tuple(int(f) for f in e["freqs_hz"]))
                for e in d.get("decoders") or ()
            ),
            notes=tuple(d.get("notes") or ()),
        )


# --- Planning ------------------------------------------------------------------


def suggest(
    lo_hz: float, hi_hz: float, installed: set[str] | None = None, known: dict | None = None, band: dict | None = None
) -> list[str]:
    """Decoders with a usual frequency near the range picked: their own
    defaults, or (`known`, decoder name -> frequencies) the channels the
    running profile of the same name is set to; and the decoder named like
    the running profile when the range is inside the band it is tuned to
    (`band`: {"profile", "lo_hz", "hi_hz"}). Only installed ones, when
    `installed` is given."""
    decoders.load()
    known = known or {}
    out = []
    for d in decoders.DECODERS.values():
        if installed is not None and d.name not in installed:
            continue
        freqs = tuple(d.default_freqs_hz) + tuple(known.get(d.name) or ())
        near = any(lo_hz - SUGGEST_MARGIN_HZ <= f <= hi_hz + SUGGEST_MARGIN_HZ for f in freqs)
        tuned = band is not None and band.get("profile") == d.name and band["lo_hz"] <= lo_hz and hi_hz <= band["hi_hz"]
        if near or tuned:
            out.append(d.name)
    return out


def _channel_freqs(d: decoders.Decoder, lo: int, hi: int, known: dict) -> tuple[int, ...] | None:
    """The frequencies a decoder is given: None (its own usual channels)
    when it has them, else the running profile's channels inside the range
    picked, else the range's middle. RDS takes exactly one."""
    if d.default_freqs_hz:
        return None
    mid = (lo + hi) // 2
    if d.name == "rds":
        return (mid,)
    inside = tuple(int(f) for f in known.get(d.name) or () if lo - CHANNEL_MARGIN_HZ <= f <= hi + CHANNEL_MARGIN_HZ)
    return inside or (mid,)


def _channels_problem(decs, freqs, center: int, rate: int) -> str | None:
    """Why a capture at center/rate would not hold some decoder's channels."""
    u = usable_hz(rate)
    for d, f in zip(decs, freqs):
        margin = DECODER_MARGIN_HZ.get(d.name, CHANNEL_MARGIN_HZ)
        chans = d.default_freqs_hz if f is None else f
        if not any(abs(c - center) + margin <= u for c in chans):
            listed = ", ".join(mhz(c) for c in chans[:6]) + (" ..." if len(chans) > 6 else "")
            return (
                f"{d.name} decodes {listed}, and a capture holding the range picked "
                f"({mhz(center - u)} to {mhz(center + u)}) would cover none of it"
            )
    return None


def _center(lo: int, hi: int, rate: int, decs, freqs) -> tuple[int, list[str]]:
    u = usable_hz(rate)

    def fits(c: float) -> bool:
        return lo >= c - u and hi <= c + u and MIN_HZ <= c <= MAX_HZ

    pinned = {d.pinned_center_hz: d for d in decs if d.pinned_center_hz}
    if len(pinned) > 1:
        a, b = list(pinned.values())[:2]
        raise PlanError(
            f"{a.name} needs the capture centred on {mhz(a.pinned_center_hz)} and {b.name} on "
            f"{mhz(b.pinned_center_hz)}: tick one of them"
        )
    if pinned:
        c, d = next(iter(pinned.items()))
        if not fits(c):
            raise PlanError(
                f"{d.name} needs the capture centred on {mhz(c)}, which holds {mhz(c - u)} to {mhz(c + u)}; "
                "the range picked is outside that"
            )
        if p := _channels_problem(decs, freqs, c, rate):
            raise PlanError(p)
        return c, [f"Centred on {mhz(c)}, as {d.name} needs."]
    usual: dict[int, list[str]] = {}
    for d in decs:
        if d.usual_center_hz:
            usual.setdefault(d.usual_center_hz, []).append(d.name)
    for c, names in usual.items():
        if fits(c) and not _channels_problem(decs, freqs, c, rate):
            return c, [f"Centred on {mhz(c)}, where {' and '.join(names)} {'is' if len(names) == 1 else 'are'} usually recorded."]
    if decs:
        offset = max(d.tune_offset_hz for d in decs)
    else:
        offset = DEFAULT_OFFSET_HZ if hi - lo < NARROW_HZ else 0
    if offset:
        c = lo - offset
        if fits(c) and not _channels_problem(decs, freqs, c, rate):
            return c, [f"Tuned {offset / 1e3:g} kHz below the range picked, to keep it off the HackRF's DC spike."]
    c = (lo + hi) // 2
    if fits(c):
        if p := _channels_problem(decs, freqs, c, rate):
            raise PlanError(p)
        return c, [f"The HackRF's DC spike is at the centre, {mhz(c)}."]
    raise PlanError(
        f"{mhz(lo)} to {mhz(hi)} is wider than a capture at {rate_text(rate)} holds "
        f"({2 * u / 1e6:g} MHz usable)" + (": pick a narrower range, or untick the decoders" if decs else "")
    )


def plan(
    lo_hz: float,
    hi_hz: float,
    names: list[str] | tuple[str, ...],
    seconds: float,
    *,
    max_seconds: float,
    max_bytes: int,
    lna_gain_db: int | None = None,
    vga_gain_db: int | None = None,
    amp: bool = False,
    default_lna: int = 32,
    default_vga: int = 40,
    known: dict | None = None,
) -> Plan:
    """Plan a capture of [lo_hz, hi_hz] (equal for a single frequency) for
    the decoders named: at the rate they record at, centred where their
    recipe says if that holds the range, else tuned off the range by the
    most any of them asks (off the DC spike). With no decoder, the
    narrowest rate in RATES_HZ that holds the range. Raises PlanError with
    what to change."""
    decoders.load()
    known = known or {}
    lo, hi = int(min(lo_hz, hi_hz)), int(max(lo_hz, hi_hz))
    if lo < MIN_HZ or hi > MAX_HZ:
        raise PlanError(f"{mhz(lo)} to {mhz(hi)} is outside what the HackRF receives (1 MHz to 6 GHz)")
    seconds = float(seconds)
    if not 1 <= seconds <= max_seconds:
        raise PlanError(f"a capture lasts 1 to {max_seconds:g} s, not {seconds:g}")
    decs: list[decoders.Decoder] = []
    for n in names:
        if n not in decoders.DECODERS:
            raise PlanError(f"unknown decoder {n!r}")
        if decoders.DECODERS[n] not in decs:
            decs.append(decoders.DECODERS[n])
    freqs = [_channel_freqs(d, lo, hi, known) for d in decs]
    if decs:
        rates = sorted({d.capture_rate_hz for d in decs})
        candidates = [r for r in rates if all(d.rate_ok(r) for d in decs)]
        if not candidates:
            a = decs[0]
            b = next(d for d in decs if not a.rate_ok(d.capture_rate_hz) or not d.rate_ok(a.capture_rate_hz))
            raise PlanError(
                f"{a.name} records at {rate_text(a.capture_rate_hz)} and {b.name} at "
                f"{rate_text(b.capture_rate_hz)}, and no one rate suits both: tick one of them"
            )
    else:
        candidates = list(RATES_HZ)
    err: PlanError | None = None
    for rate in candidates:
        try:
            center, notes = _center(lo, hi, rate, decs, freqs)
        except PlanError as exc:
            err = exc
            continue
        break
    else:
        raise err  # type: ignore[misc]
    lna = default_lna if lna_gain_db is None else int(lna_gain_db)
    vga = default_vga if vga_gain_db is None else int(vga_gain_db)
    if not (0 <= lna <= 40 and lna % 8 == 0):
        raise PlanError(f"LNA gain is 0-40 dB in steps of 8, not {lna}")
    if not (0 <= vga <= 62 and vga % 2 == 0):
        raise PlanError(f"VGA gain is 0-62 dB in steps of 2, not {vga}")
    p = Plan(
        lo, hi, int(center), int(rate), seconds, lna, vga, bool(amp),
        tuple((d.name, f) for d, f in zip(decs, freqs)), tuple(notes),
    )  # fmt: skip
    if p.bytes > max_bytes:
        raise PlanError(
            f"{rate_text(rate)} for {seconds:g} s is {p.bytes / MIB:,.0f} MiB, over this station's "
            f"{max_bytes / MIB:,.0f} MiB for one capture: make it shorter"
        )
    return p


def fit(p: Plan, names: list[str], known: dict | None = None) -> tuple[tuple[str, tuple[int, ...] | None], ...]:
    """Decoders to run on a capture already made: each with the frequencies
    it is given. Raises PlanError for one the capture doesn't suit."""
    decoders.load()
    known = known or {}
    out = []
    for n in names:
        d = decoders.DECODERS.get(n)
        if d is None:
            raise PlanError(f"unknown decoder {n!r}")
        if not d.rate_ok(p.sample_rate_hz):
            raise PlanError(f"{n} needs {rate_text(d.capture_rate_hz)}; this capture is {rate_text(p.sample_rate_hz)}")
        if d.pinned_center_hz and d.pinned_center_hz != p.center_hz:
            raise PlanError(f"{n} needs a capture centred on {mhz(d.pinned_center_hz)}; this one is on {mhz(p.center_hz)}")
        f = _channel_freqs(d, p.lo_hz, p.hi_hz, known)
        if prob := _channels_problem([d], [f], p.center_hz, p.sample_rate_hz):
            raise PlanError(prob)
        out.append((n, f))
    return tuple(out)


# --- The files -------------------------------------------------------------------


class Store:
    """web/ in the capture directory: <id>.cs8, <id>.json, <id>.png."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def check_id(cid: str) -> str:
        if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
            raise ValueError(f"not a capture id: {cid!r}")
        return cid

    def new_id(self, now: float) -> str:
        while True:
            cid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(2)
            if not self.meta_path(cid).exists():
                return cid

    def iq_path(self, cid: str) -> Path:
        return self.root / f"{self.check_id(cid)}.cs8"

    def meta_path(self, cid: str) -> Path:
        return self.root / f"{self.check_id(cid)}.json"

    def picture_path(self, cid: str) -> Path:
        return self.root / f"{self.check_id(cid)}.png"

    def save(self, rec: dict) -> None:
        self.ensure()
        path = self.meta_path(rec["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=1, default=str))
        os.replace(tmp, path)

    def load(self, cid: str) -> dict | None:
        try:
            return json.loads(self.meta_path(cid).read_text())
        except (OSError, ValueError):
            return None

    def _groups(self) -> dict[str, list[Path]]:
        out: dict[str, list[Path]] = {}
        try:
            entries = list(self.root.iterdir())
        except FileNotFoundError:
            return out
        for p in entries:
            cid = p.name.split(".", 1)[0]
            if p.is_file() and ID_RE.fullmatch(cid):
                out.setdefault(cid, []).append(p)
        return out

    def records(self) -> list[dict]:
        out = []
        for cid in sorted(self._groups(), reverse=True):
            if (rec := self.load(cid)) is not None:
                out.append(rec)
        return out

    def used_bytes(self) -> int:
        return sum(_size(p) for files in self._groups().values() for p in files)

    def count(self) -> int:
        return sum(1 for files in self._groups().values() if any(p.suffix == ".json" for p in files))

    def delete(self, cid: str) -> int:
        freed = 0
        for p in self._groups().get(self.check_id(cid), []):
            freed += _size(p)
            p.unlink(missing_ok=True)
        return freed

    def prune(self, now: float, max_age_s: float, max_bytes: int, need_bytes: int = 0, keep: set[str] = frozenset()) -> list[str]:
        """Delete captures older than max_age_s, then the oldest until web/
        holds at most max_bytes with need_bytes more. Ids in `keep` (being
        recorded or decoded) stay. Returns the ids deleted."""
        groups = self._groups()
        gone = []
        used = sum(_size(p) for files in groups.values() for p in files)
        for cid in sorted(groups):  # the id starts with its UTC time
            if cid in keep:
                continue
            made = min((_mtime(p) for p in groups[cid]), default=now)
            if now - made > max_age_s or used + need_bytes > max_bytes:
                used -= self.delete(cid)
                gone.append(cid)
        return gone


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# --- Running a capture ----------------------------------------------------------


def capture_runner(live):
    """A function recording a Plan into a file and returning the agent's
    "done" (exit_code, stderr, timed_out): through the hackrf-agent on a
    split station, else hackrf_transfer here. Bias-tee power stays off."""
    if live.agent is not None:
        from hackrf_mcp import agent

        def run_agent(p: Plan, path: Path) -> dict:
            token = live.agent_token_file.read_text().strip()
            req = {
                "tool": "capture",
                "frequency_hz": p.center_hz,
                "sample_rate_hz": p.sample_rate_hz,
                "num_samples": p.num_samples,
                "lna_gain_db": p.lna_gain_db,
                "vga_gain_db": p.vga_gain_db,
                "amp": p.amp,
                "antenna_enable": False,
                "timeout_s": p.seconds + 30,
            }
            with open(path, "wb") as fh:
                return agent.run_tool(live.agent, token, req, fh.write, wait_s=p.seconds + 330)

        return run_agent

    from hackrf_mcp import cli

    def run_local(p: Plan, path: Path) -> dict:
        args = [
            cli._resolve_binary("hackrf_transfer"),
            "-r", str(path), "-n", str(p.num_samples), "-f", str(p.center_hz), "-s", str(p.sample_rate_hz),
            "-l", str(p.lna_gain_db), "-g", str(p.vga_gain_db), "-a", "1" if p.amp else "0",
        ]  # fmt: skip
        try:
            proc = subprocess.run(args, capture_output=True, timeout=p.seconds + 30)
        except subprocess.TimeoutExpired as exc:
            return {"timed_out": True, "stderr": (exc.stderr or b"").decode(errors="replace")[-2000:]}
        return {"exit_code": proc.returncode, "stderr": proc.stderr.decode(errors="replace")[-2000:], "timed_out": False}

    return run_local


def _failure(exc: Exception) -> str:
    from hackrf_mcp import agent

    if isinstance(exc, agent.AgentRefused):
        return f"the hackrf-agent refused: {exc}"
    if isinstance(exc, OSError):
        return f"could not reach the HackRF: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _free_bytes(path: Path) -> int:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def decode_runner(live, workroot: Path):
    """A function decoding a capture in a child process (this module's
    --decode-job, which lowers its own priority), returning its JSON
    results."""

    def run(path: Path, p: Plan, todo, picture: Path | None) -> dict:
        job = {
            "path": str(path),
            "sample_rate_hz": p.sample_rate_hz,
            "center_hz": p.center_hz,
            "decoders": [[n, None if f is None else list(f)] for n, f in todo],
            "picture": None if picture is None else str(picture),
            "timeout_s": DECODE_TIMEOUT_S,
            "workroot": str(workroot),
            "pager_content": bool(live.pager_content),
            "receiver": list(live.receiver) if live.receiver else None,
            "title": f"{p.describe()}",
        }
        limit = len(todo) * (DECODE_TIMEOUT_S + 30) + PICTURE_TIMEOUT_S + 60
        proc = subprocess.run(
            [sys.executable, "-m", "hackrf_mcp.captures", "--decode-job"],
            input=json.dumps(job).encode(),
            capture_output=True,
            timeout=limit,
        )
        lines = [ln for ln in proc.stdout.decode(errors="replace").splitlines() if ln.startswith("{")]
        if proc.returncode != 0 or not lines:
            tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
            raise RuntimeError(f"the decoding process failed: {tail[0][:300]}")
        return json.loads(lines[-1])

    return run


def _clip(s: object) -> str:
    s = str(s)
    return s if len(s) <= MAX_LINE else s[: MAX_LINE - 1] + "…"


def summarise(r: decoders.Result) -> dict:
    """What the UI shows of a decoder's Result: never its raw output."""
    notes = []
    for n in r.notes:
        if n.strip() == RAW_MARK:
            break
        if n.strip():
            notes.append(_clip(n.strip()))
    return {
        "headline": _clip(r.headline),
        "notes": notes[:MAX_NOTES],
        "table": [_clip(x) for x in r.table[:MAX_LINES]],
        "table_more": max(0, len(r.table) - MAX_LINES),
        "messages": [_clip(x) for x in r.messages[:MAX_LINES]],
        "messages_more": max(0, len(r.messages) - MAX_LINES),
    }


def run_job(job: dict) -> dict:
    """The child's work: each decoder in turn, then the picture."""
    path = Path(job["path"])
    receiver = tuple(job["receiver"]) if job.get("receiver") else None
    out = []
    for name, freqs in job["decoders"]:
        started = time.monotonic()
        entry = {"decoder": name}
        try:
            r = decoders.decode(
                name, path, int(job["sample_rate_hz"]), int(job["center_hz"]), freqs, None,
                float(job["timeout_s"]), Path(job["workroot"]), receiver, pager_content=bool(job["pager_content"]),
            )  # fmt: skip
            entry.update(state="done", **summarise(r))
        except decoders.DecodeError as exc:
            entry.update(state="failed", error=_clip(f"cannot decode: {exc}"))
        except Exception as exc:  # one broken decoder must not lose the others
            entry.update(state="failed", error=_clip(f"{type(exc).__name__}: {exc}"))
        entry["seconds"] = round(time.monotonic() - started, 1)
        out.append(entry)
    pic = {"picture": False}
    if job.get("picture"):
        try:
            from hackrf_mcp import iq

            target = Path(job["picture"])
            freqs_hz, times_s, power_db = iq.stream_spectrogram(path, int(job["sample_rate_hz"]), 1024, max_time_bins=600)
            if freqs_hz.size:
                tmp = target.with_name(target.stem + ".tmp.png")
                iq.render_spectrogram_png(tmp, freqs_hz, times_s, power_db, int(job["center_hz"]), job.get("title"))
                os.replace(tmp, target)
                pic = {"picture": True}
            else:
                pic = {"picture": False, "picture_error": "the capture holds no samples"}
        except Exception as exc:
            pic = {"picture": False, "picture_error": _clip(f"{type(exc).__name__}: {exc}")}
    return {"decodes": out, **pic}


# --- The recorder in the live service -----------------------------------------------


class Recorder:
    """Captures and their decoding, in the live service. `service` is the
    LiveService: capture_take(lease_s) takes the receiver (or says who has
    it), capture_give(token) hands it back, _event(...) logs, status-like
    reads come from capture_context(). None of these is ever called with
    this recorder's lock held, and the service never calls into the
    recorder under its own lock, so the two locks never nest."""

    def __init__(
        self,
        service,
        store: Store,
        *,
        max_seconds: float,
        max_bytes: int,
        min_free_bytes: int,
        keep_bytes: int,
        keep_age_s: float,
        wait_s: float,
        run_capture,
        run_decode,
        default_gains: tuple[int, int] = (32, 40),
        clock=time.time,
        poll_s: float = 1.0,
        spawn=None,
        available=None,
    ):
        self.service = service
        self.store = store
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.keep_bytes = keep_bytes
        self.keep_age_s = keep_age_s
        self.wait_s = wait_s
        self.run_capture = run_capture
        self.run_decode = run_decode
        self.default_gains = default_gains
        self.clock = clock
        self.poll_s = poll_s
        self.lock = threading.RLock()
        self.halt = threading.Event()
        # The capture waiting for or holding the receiver: {"id", "state",
        # "since", "plan", "started"}.
        self.current: dict | None = None
        self.decoding: str | None = None
        self.queue: deque = deque()
        self.counts = {"recorded": 0, "failed": 0, "decoded": 0}
        self._decoder_running = False
        self._last_prune = 0.0
        # Which decoders are installed (decoders.available()).
        self.available = available or decoders.available
        # How work is started: a thread each (tests run it inline).
        self._spawn = spawn or (lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True).start())

    # --- requests

    def _plan(self, req: dict, ctx: dict) -> tuple[Plan | None, str | None, list[str]]:
        lo = float(req["from_hz"])
        hi = float(req.get("to_hz") or lo)
        installed = {n for n, prog in self.available().items() if prog}
        known = ctx.get("known") or {}
        suggested = suggest(min(lo, hi), max(lo, hi), installed, known, ctx.get("band"))
        names = suggested if req.get("decoders") is None else [str(n) for n in req["decoders"]]
        default_lna, default_vga = self.default_gains
        try:
            p = plan(
                lo, hi, names, float(req.get("seconds") or 30), max_seconds=self.max_seconds, max_bytes=self.max_bytes,
                lna_gain_db=req.get("lna_gain_db"), vga_gain_db=req.get("vga_gain_db"), amp=bool(req.get("amp", False)),
                default_lna=default_lna, default_vga=default_vga, known=known,
            )  # fmt: skip
        except PlanError as exc:
            return None, str(exc), suggested
        missing = [n for n, _ in p.decoders if n not in installed]
        if missing:
            return None, f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not installed on this station", suggested
        return p, None, suggested

    def plan_request(self, req: dict) -> dict:
        """What a capture of the range would be, and what it would stop,
        for the UI's confirmation; nothing is taken or written."""
        ctx = self.service.capture_context()
        p, err, suggested = self._plan(req, ctx)
        busy = self._busy_text()
        out = {
            "plan": None if p is None else p.to_json(),
            "error": err or busy,
            "suggested": suggested,
            "decoders": [
                {"name": n, "installed": bool(prog), "summary": decoders.DECODERS[n].summary}
                for n, prog in self.available().items()
            ],
            "limits": self.limits(),
            "effects": [] if p is None else self._effects(p, ctx),
        }
        return out

    def _effects(self, p: Plan, ctx: dict) -> list[str]:
        """What the capture would stop or delay, in words."""
        now = self.clock()
        out = []
        away = p.seconds + 5
        if ctx.get("watch"):
            out.append(f"The watch pauses for about {away:.0f} s; its time keeps running.")
        elif ctx.get("running"):
            out.append(f"Decoding of {ctx['running']} stops for about {away:.0f} s.")
        if ctx.get("held_by"):
            out.append(f"The receiver is lent to {ctx['held_by']} now; the capture waits up to {self.wait_s:g} s for it.")
        slot = ctx.get("next_slot")
        if slot is not None and now - 5 <= slot <= now + away + (self.wait_s if ctx.get("held_by") else 0):
            when = time.strftime("%H:%M:%S", time.gmtime(slot))
            if ctx.get("watch"):
                out.append(f"The occupancy sweep due at {when} UTC is skipped, as it is during any watch.")
            else:
                out.append(f"The occupancy sweep due at {when} UTC waits until the capture ends.")
        if _free_bytes(self.store.root) - p.bytes < self.min_free_bytes:
            out.append("There is not enough free disk space for it now; older captures would have to go first.")
        return out

    def _busy_text(self) -> str | None:
        with self.lock:
            cur = self.current
        if cur is None:
            return None
        return f"a capture is already {'being recorded' if cur['state'] == 'recording' else 'waiting for the receiver'}; wait for it to finish"

    def start(self, req: dict) -> dict:
        """Start a capture: returns its record at once; it records on its
        own thread."""
        ctx = self.service.capture_context()
        p, err, _ = self._plan(req, ctx)
        if p is None:
            raise PlanError(err)
        now = self.clock()
        with self.lock:
            if busy := self._busy_text():
                raise PlanError(busy)
            self.store.ensure()
            keep = self._keep()
            self.store.prune(now, self.keep_age_s, self.keep_bytes, p.bytes, keep)
            free = shutil.disk_usage(self.store.root).free
            if free - p.bytes < self.min_free_bytes:
                raise PlanError(
                    f"the capture would be {p.bytes / MIB:,.0f} MiB but only {free / MIB:,.0f} MiB is free and "
                    f"{self.min_free_bytes / MIB:,.0f} MiB must stay free; delete old captures first"
                )
            cid = self.store.new_id(now)
            origin = str(req.get("origin") or "live")
            rec = {
                "id": cid,
                "created": now,
                "origin": origin if origin in ("live", "history", "occupancy") else "live",
                "plan": p.to_json(),
                "state": "waiting",
                "error": None,
                "note": None,
                "started": None,
                "ended": None,
                "held_s": None,
                "paused": None,
                "bytes": 0,
                "decodes": [{"decoder": n, "state": "queued"} for n, _ in p.decoders],
                "picture": False,
            }
            self.store.save(rec)
            self.current = {"id": cid, "state": "waiting", "since": now, "plan": p, "started": None}
        self._spawn(self._record, rec, p)
        return dict(rec)

    def decode(self, cid: str, names: list[str]) -> dict:
        """Run more decoders on a capture already recorded."""
        self.store.check_id(cid)
        # Asked before taking this lock: the service's lock is never taken under it.
        known = self.service.capture_context().get("known")
        with self.lock:
            rec = self.store.load(cid)
            if rec is None:
                raise PlanError(f"no capture {cid}")
            if rec["state"] in ("waiting", "recording") or not self.store.iq_path(cid).exists():
                raise PlanError("that capture has no recording to decode")
            p = Plan.from_json(rec["plan"])
            todo = fit(p, [str(n) for n in names], known)
            if not todo:
                raise PlanError("tick a decoder to run")
            installed = {n for n, prog in self.available().items() if prog}
            if missing := [n for n, _ in todo if n not in installed]:
                raise PlanError(f"{', '.join(missing)} is not installed on this station")
            done = {d["decoder"]: d for d in rec["decodes"]}
            for n, _ in todo:
                done[n] = {"decoder": n, "state": "queued"}
            rec["decodes"] = list(done.values())
            rec["state"] = "decoding"
            self.store.save(rec)
        self._enqueue(cid, todo, picture=False)
        return rec

    def delete(self, cid: str) -> int:
        self.store.check_id(cid)
        with self.lock:
            if cid in self._keep():
                raise PlanError("that capture is still being recorded or decoded")
            self.queue = deque(item for item in self.queue if item[0] != cid)
            freed = self.store.delete(cid)
        if freed:
            self.service._event("capture-delete", None, f"{cid}: {freed / MIB:,.0f} MiB")
        return freed

    def list(self, limit: int = 200) -> list[dict]:
        """The captures, newest first, each with `more`: the installed
        decoders not yet run on it that its rate and centre suit."""
        installed = [n for n, prog in self.available().items() if prog]
        out = []
        for rec in self.store.records()[:limit]:
            cid = rec["id"]
            rec["bytes"] = _size(self.store.iq_path(cid))
            rec["picture"] = self.store.picture_path(cid).exists()
            rec["more"] = []
            if rec["bytes"] and rec.get("state") in ("decoding", "done"):
                ran = {d["decoder"] for d in rec.get("decodes") or ()}
                p = Plan.from_json(rec["plan"])
                for n in installed:
                    if n in ran:
                        continue
                    try:
                        fit(p, [n])
                    except PlanError:
                        continue
                    rec["more"].append(n)
            out.append(rec)
        return out

    def limits(self) -> dict:
        return {
            "max_seconds": self.max_seconds,
            "max_mb": self.max_bytes / MIB,
            "keep_days": self.keep_age_s / 86400,
            "keep_mb": self.keep_bytes / MIB,
            "wait_s": self.wait_s,
        }

    def status(self) -> dict:
        now = self.clock()
        with self.lock:
            cur = dict(self.current) if self.current else None
            decoding, queued = self.decoding, len(self.queue)
            counts = dict(self.counts)
        current = None
        if cur is not None:
            p: Plan = cur["plan"]
            until = cur["started"] + p.seconds if cur["started"] else None
            current = {
                "id": cur["id"],
                "state": cur["state"],
                "since": cur["since"],
                "until": until,
                "left_s": None if until is None else max(0.0, until - now),
                "center_hz": p.center_hz,
                "sample_rate_hz": p.sample_rate_hz,
                "seconds": p.seconds,
            }
        return {
            "enabled": True,
            **self.limits(),
            "dir": str(self.store.root),
            "used_mb": self.store.used_bytes() / MIB,
            "count": self.store.count(),
            "current": current,
            "decoding": decoding,
            "queued": queued,
            **counts,
        }

    def stop(self) -> None:
        self.halt.set()

    # --- the work

    def _keep(self) -> set[str]:
        keep = {item[0] for item in self.queue}
        if self.current:
            keep.add(self.current["id"])
        if self.decoding:
            keep.add(self.decoding)
        return keep

    def _update(self, rec: dict, **changes) -> None:
        rec.update(changes)
        with self.lock:
            if self.current and self.current["id"] == rec["id"] and "state" in changes:
                self.current["state"] = changes["state"]
            self.store.save(rec)

    def _record(self, rec: dict, p: Plan) -> None:
        cid = rec["id"]
        path = self.store.iq_path(cid)
        try:
            took = self._take(rec, p)
            if took is None:
                return
            started = self.clock()
            with self.lock:
                if self.current:
                    self.current["started"] = started
            paused = took.get("paused_profile")
            self._update(rec, state="recording", started=started, paused=paused)
            self.service._event("capture-start", None, f"{cid}: {p.describe()}" + (f"; {paused} paused" if paused else ""))
            failed: str | None = None
            info: dict = {}
            try:
                info = self.run_capture(p, path) or {}
            except Exception as exc:
                failed = _clip(_failure(exc))
            finally:
                held = self.clock() - started
                self.service.capture_give(took["token"])
            # The receiver is back; the rest doesn't hold up decoding.
            got = _size(path)
            if failed is None and got == 0:
                why = (info.get("stderr") or "").strip().splitlines()[-1:] or [""]
                failed = "no samples arrived" + (" (timed out)" if info.get("timed_out") else "") + (f": {why[0][:200]}" if why[0] else "")
            what = f"{paused} paused {held:.1f} s" if paused else f"held {held:.1f} s"
            if failed is not None:
                path.unlink(missing_ok=True)
                self.counts["failed"] += 1
                self._update(rec, state="failed", error=failed, ended=self.clock(), held_s=round(held, 1), bytes=0,
                             decodes=[])  # fmt: skip
                self.service._event("capture-error", None, f"{cid}: {failed}; {what}")
                return
            note = None
            if got < p.bytes:
                note = f"The capture is short: {got // 2:,} of {p.num_samples:,} samples arrived."
            self.counts["recorded"] += 1
            self._update(rec, state="decoding", ended=self.clock(), held_s=round(held, 1), bytes=got, note=note)
            self.service._event("capture-done", None, f"{cid}: {got / MIB:,.0f} MiB in {held:.1f} s; {what}")
            with self.lock:
                self.current = None
            self._enqueue(cid, p.decoders, picture=True)
        except Exception as exc:  # a broken capture must not leave the recorder stuck
            self._update(rec, state="failed", error=_clip(f"{type(exc).__name__}: {exc}"))
            self.service._event("capture-error", None, f"{cid}: {exc!r}")
        finally:
            with self.lock:
                if self.current and self.current["id"] == cid:
                    self.current = None

    def _take(self, rec: dict, p: Plan) -> dict | None:
        """Take the receiver, waiting up to wait_s for a device tool or a
        sweep; None (the capture failed) if it stays lent out."""
        deadline = self.clock() + self.wait_s
        while True:
            took = self.service.capture_take(p.seconds + LEASE_MARGIN_S)
            if took["state"] == "ok":
                return took
            owner = took.get("owner") or "a device tool"
            if self.clock() >= deadline or self.halt.is_set():
                why = f"the receiver was lent to {owner} for over {self.wait_s:g} s; nothing was recorded"
                self.counts["failed"] += 1
                self._update(rec, state="failed", error=why, ended=self.clock(), decodes=[])
                self.service._event("capture-error", None, f"{rec['id']}: {why}")
                return None
            self.halt.wait(self.poll_s)

    def _enqueue(self, cid: str, todo, picture: bool) -> None:
        with self.lock:
            self.queue.append((cid, tuple(todo), picture))
            start = not self._decoder_running
            self._decoder_running = True
        if start:
            self._spawn(self._decode_loop)

    def _decode_loop(self) -> None:
        """Decode queued captures one at a time, then end; the next one
        queued starts it again."""
        while True:
            with self.lock:
                if not self.queue or self.halt.is_set():
                    self._decoder_running = False
                    return
                item = self.queue.popleft()
                self.decoding = item[0]
            try:
                self._decode_one(*item)
            except Exception as exc:
                self.service._event("capture-error", None, f"{item[0]}: decoding: {exc!r}")
            finally:
                with self.lock:
                    self.decoding = None
            self._maybe_prune()

    def _decode_one(self, cid: str, todo, picture: bool) -> None:
        rec = self.store.load(cid)
        if rec is None or not self.store.iq_path(cid).exists():
            return
        p = Plan.from_json(rec["plan"])
        names = [n for n, _ in todo]
        for d in rec["decodes"]:
            if d["decoder"] in names:
                d["state"] = "running"
        self._update(rec, state="decoding")
        started = time.monotonic()
        try:
            out = self.run_decode(self.store.iq_path(cid), p, todo, self.store.picture_path(cid) if picture else None)
        except Exception as exc:
            err = _clip(str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}")
            out = {"decodes": [{"decoder": n, "state": "failed", "error": err} for n in names]}
        results = {d["decoder"]: d for d in out.get("decodes") or ()}
        rec = self.store.load(cid) or rec
        rec["decodes"] = [results.get(d["decoder"], d) if d["decoder"] in names else d for d in rec["decodes"]]
        for d in rec["decodes"]:
            if d["state"] in ("queued", "running") and d["decoder"] in names:
                d.update(state="failed", error="the decoding process gave no result for it")
        if picture:
            rec["picture"] = bool(out.get("picture"))
            if out.get("picture_error"):
                rec["picture_error"] = out["picture_error"]
        busy = any(d["state"] in ("queued", "running") for d in rec["decodes"])
        rec["state"] = "decoding" if busy else "done"
        self.store.save(rec)
        self.counts["decoded"] += 1
        took = time.monotonic() - started
        ok = sum(1 for n in names if results.get(n, {}).get("state") == "done")
        self.service._event(
            "capture-decoded", None, f"{cid}: {ok} of {len(names)} decoder{'s' if len(names) != 1 else ''} ran in {took:.0f} s"
        )

    def _maybe_prune(self) -> None:
        now = self.clock()
        if now - self._last_prune < 600:
            return
        self._last_prune = now
        with self.lock:
            keep = self._keep()
        gone = self.store.prune(now, self.keep_age_s, self.keep_bytes, 0, keep)
        if gone:
            self.service._event("capture-delete", None, f"{len(gone)} kept past {self.keep_age_s / 86400:g} days or the size cap")

    def prune_now(self) -> list[str]:
        with self.lock:
            keep = self._keep()
        return self.store.prune(self.clock(), self.keep_age_s, self.keep_bytes, 0, keep)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["--decode-job"]:
        print("usage: python -m hackrf_mcp.captures --decode-job < job.json", file=sys.stderr)
        return 2
    try:
        os.nice(10)  # below the live decoders
    except OSError:
        pass
    job = json.loads(sys.stdin.read())
    print(json.dumps(run_job(job)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
