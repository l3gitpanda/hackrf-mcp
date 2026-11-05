"""Spectrum occupancy: what is on the air across a wide range, and when.

Every LiveSettings.occupancy_interval_min minutes, on the UTC clock (with
15, at :00, :15, :30 and :45), the live service lends the HackRF to
hackrf_sweep for a few seconds: `passes` sweeps of the range at the bin
width, with fixed gains and the amplifier off, so slots compare over time.
It takes the receiver the way a device tool does (a pause with a lease, so a
slot that dies can't leave decoding stopped), and the profile or rotation
resumes as soon as the sweep ends; the slot is stored after that.

hackrf_sweep measures each bin from a single FFT frame per pass (204
samples, 10 us, at a 100 kHz bin), so one pass sees steady carriers but
catches bursts only by luck. Each slot keeps, per bin, the mean of its
passes (in linear power), which shows how busy a bin is, and the peak, the
strongest pass, which catches more of the bursts.

occupancy.db, beside the log, with limits of its own (LiveSettings.occupancy_*):

  slots  one row per slot: when it was due (slot_ts) and when the sweep
         began (ts), Unix seconds UTC; how long it held the HackRF; the
         grid (lo_hz, bin_hz, bins: bin i covers lo_hz + i * bin_hz to the
         next); the gains; how many complete passes were averaged; `mean`
         and `peak`, one byte per bin. Kept occupancy_max_age_days.
  daily  one row per UTC day and grid, rolled up from that day's slots once
         the day is over: the mean of the means (linear power) and the max
         of the peaks, with the number of slots. Kept occupancy_daily_days.

A byte b is db_min + b * db_step dBFS, spectrum.db's scale (-130 dBFS and
0.5 dB). hackrf_sweep's levels are shifted by its window's coherent gain so
that, as in spectrum.db, a full-scale tone reads 0 dBFS. Byte 0 means
nothing was measured there.

The readers (grid, profile, unusual, cell) serve the web UI's Occupancy
screen. Hour-of-day and day-of-week views are worked out from the slots on
request. A slot is unusual where its mean is threshold_db or more above the
bin's median at the same UTC hour over the 7 whole days before the slot's
day.
"""

from __future__ import annotations

import math
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from hackrf_mcp.live_spectrum import DB_MIN, DB_STEP, quantise

MIB = 1024 * 1024
DAY = 86400
HOUR = 3600
# hackrf_sweep's fixed sample rate, tuning step and the width of each CSV line.
SWEEP_RATE_HZ = 20_000_000
TUNE_STEP_HZ = 20_000_000
LINE_HZ = 5_000_000
# What the live service calls the holder of the receiver during a slot.
OWNER = "occupancy sweep"
# Readers' limits.
MAX_TBUCKETS = 2000
MAX_FBUCKETS = 8000
MAX_CELLS = 2_000_000
MAX_PROFILE_DAYS = 92
MAX_UNUSUAL_DAYS = 2
BASELINE_DAYS = 7
# Linear power of each byte, for averaging without dequantising.
_LIN = np.power(10.0, (DB_MIN + np.arange(256) * DB_STEP) / 10.0)


def fft_size(bin_width_hz: int) -> int:
    """hackrf_sweep's FFT size for a requested bin width: 20 MHz over the
    width, padded up to an odd multiple of four."""
    n = max(4, SWEEP_RATE_HZ // int(bin_width_hz))
    while (n + 4) % 8:
        n += 1
    return n


def window_gain_db(n: int) -> float:
    """dB to add to hackrf_sweep's levels so a full-scale tone reads 0 dBFS:
    it scales its FFT by 1/n, and a Hann window's sum is (n - 1) / 2."""
    return 20.0 * math.log10(n / float(np.hanning(n).sum()))


@dataclass(frozen=True)
class Plan:
    """What every slot sweeps, from the settings."""

    lo_mhz: int = 24
    hi_mhz: int = 1700
    bin_width_hz: int = 100_000
    passes: int = 20
    lna_gain_db: int = 24
    vga_gain_db: int = 40

    @classmethod
    def from_live(cls, live) -> Plan:
        lo, hi = live.occupancy_range_mhz
        return cls(lo, hi, live.occupancy_bin_hz, live.occupancy_passes, live.occupancy_lna_gain_db, live.occupancy_vga_gain_db)

    @property
    def fft(self) -> int:
        return fft_size(self.bin_width_hz)

    @property
    def bin_hz(self) -> float:
        return SWEEP_RATE_HZ / self.fft

    @property
    def per_line(self) -> int:
        return self.fft // 4

    @property
    def steps(self) -> int:
        # hackrf_sweep rounds the range up to whole 20 MHz tuning steps.
        return 1 + (self.hi_mhz - self.lo_mhz - 1) // (TUNE_STEP_HZ // 1_000_000)

    @property
    def top_mhz(self) -> int:
        return self.lo_mhz + self.steps * TUNE_STEP_HZ // 1_000_000

    @property
    def lines(self) -> int:
        return self.steps * (TUNE_STEP_HZ // LINE_HZ)

    @property
    def bins(self) -> int:
        return self.lines * self.per_line

    @property
    def lo_hz(self) -> int:
        return self.lo_mhz * 1_000_000

    @property
    def timeout_s(self) -> float:
        # About 0.21 s a pass on the station; plenty over that.
        return min(300.0, 30.0 + 1.5 * self.passes)

    def describe(self) -> str:
        return (
            f"{self.lo_mhz}-{self.top_mhz} MHz, {self.passes} pass{'es' if self.passes != 1 else ''} "
            f"at {self.bin_hz / 1e3:.1f} kHz, LNA {self.lna_gain_db} dB, VGA {self.vga_gain_db} dB, amp off"
        )

    def args(self) -> list[str]:
        """hackrf_sweep's command line (after the program), CSV to stdout."""
        out = ["-f", f"{self.lo_mhz}:{self.hi_mhz}", "-w", str(self.bin_width_hz)]
        out += ["-l", str(self.lna_gain_db), "-g", str(self.vga_gain_db)]
        return out + (["-1"] if self.passes == 1 else ["-N", str(self.passes)])

    def agent_request(self) -> dict:
        """A hackrf-agent "run" of its sweep tool (agent.check_run)."""
        return {
            "tool": "sweep",
            "freq_min_mhz": self.lo_mhz,
            "freq_max_mhz": self.hi_mhz,
            "bin_width_hz": self.bin_width_hz,
            "one_shot": self.passes == 1,
            "num_sweeps": None if self.passes == 1 else self.passes,
            "lna_gain_db": self.lna_gain_db,
            "vga_gain_db": self.vga_gain_db,
            "amp": False,
            "timeout_s": self.timeout_s,
        }


# --- Parsing ------------------------------------------------------------------


class SweepError(RuntimeError):
    pass


@dataclass
class Measured:
    """One slot's sweep, reduced."""

    passes: int  # complete passes averaged
    partial: int  # passes cut short (left out)
    bad_lines: int  # lines off the plan's grid (left out)
    mean_db: np.ndarray  # float32[bins], dBFS
    peak_db: np.ndarray


def parse(text: str, plan: Plan) -> Measured:
    """hackrf_sweep's CSV for `plan`, reduced to the mean (linear power)
    and the peak of each bin over its complete passes. Lines are placed by
    their hz_low, so their order within a pass doesn't matter; a line
    whose place is already filled starts the next pass. SweepError if no
    pass is complete."""
    lo = plan.lo_hz
    per, lines = plan.per_line, plan.lines
    cur = np.full((lines, per), np.nan, dtype=np.float32)
    filled = np.zeros(lines, dtype=bool)
    total = np.zeros((lines, per), dtype=np.float64)
    peak = np.full((lines, per), -np.inf, dtype=np.float32)
    complete = partial = bad = 0

    def finish() -> None:
        nonlocal complete, partial
        if filled.all():
            np.add(total, np.power(10.0, cur.astype(np.float64) / 10.0), out=total)
            np.maximum(peak, cur, out=peak)
            complete += 1
        elif filled.any():
            partial += 1

    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            hz_low = int(float(parts[2]))
            values = np.array(parts[6:], dtype=np.float32)
        except ValueError:
            continue
        off = hz_low - lo
        k = off // LINE_HZ
        if off % LINE_HZ or not 0 <= k < lines or values.size != per:
            bad += 1
            continue
        if filled[k]:
            finish()
            filled[:] = False
        cur[k] = values
        filled[k] = True
    finish()
    if complete == 0:
        raise SweepError(f"no complete pass in hackrf_sweep's output ({partial} partial, {bad} lines off the grid)")
    gain = window_gain_db(plan.fft)
    mean = (10.0 * np.log10(np.maximum(total / complete, 1e-30)) + gain).astype(np.float32).ravel()
    return Measured(complete, partial, bad, mean, (peak + gain).astype(np.float32).ravel())


# --- Writing ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY,
    slot_ts REAL NOT NULL,
    ts REAL NOT NULL,
    seconds REAL NOT NULL,
    lo_hz INTEGER NOT NULL,
    bin_hz REAL NOT NULL,
    bins INTEGER NOT NULL,
    passes INTEGER NOT NULL,
    db_min REAL NOT NULL,
    db_step REAL NOT NULL,
    lna_gain_db INTEGER,
    vga_gain_db INTEGER,
    amp INTEGER,
    mean BLOB NOT NULL,
    peak BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS slots_ts ON slots (ts, lo_hz, bin_hz, bins);
CREATE TABLE IF NOT EXISTS daily (
    id INTEGER PRIMARY KEY,
    day REAL NOT NULL,
    lo_hz INTEGER NOT NULL,
    bin_hz REAL NOT NULL,
    bins INTEGER NOT NULL,
    slots INTEGER NOT NULL,
    db_min REAL NOT NULL,
    db_step REAL NOT NULL,
    mean BLOB NOT NULL,
    peak BLOB NOT NULL,
    UNIQUE (day, lo_hz, bin_hz, bins)
);
"""


@dataclass
class Prune:
    deleted_age: int = 0
    deleted_size: int = 0
    deleted_daily: int = 0
    rolled_up: int = 0
    bytes_after: int = 0


@dataclass
class WriterStats:
    written: int = 0
    dropped: int = 0
    low_disk: bool = False
    slots_kept: int = 0
    days_kept: int = 0
    last_prune: Prune = field(default_factory=Prune)


class OccupancyWriter:
    """The write side of occupancy.db. Used from the scheduler's thread only
    (file_bytes and stats may be read from others)."""

    def __init__(self, path: Path, max_age_s: float, daily_age_s: float, max_bytes: int, min_free_bytes: int):
        self.path = Path(path)
        self.max_age_s = max_age_s
        self.daily_age_s = daily_age_s
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.stats = WriterStats()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        if new:
            self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o640)
        except OSError:
            pass
        self._count()

    def close(self) -> None:
        self.conn.close()

    def _count(self) -> None:
        self.stats.slots_kept = self.conn.execute("SELECT count(*) FROM slots").fetchone()[0]
        self.stats.days_kept = self.conn.execute("SELECT count(*) FROM daily").fetchone()[0]

    def disk_ok(self) -> bool:
        self.stats.low_disk = shutil.disk_usage(self.path.parent).free < self.min_free_bytes
        return not self.stats.low_disk

    def add(self, slot_ts: float, ts: float, seconds: float, plan: Plan, m: Measured) -> int | None:
        """Store one slot; None (and counted as dropped) while free disk
        space is under the floor."""
        if not self.disk_ok():
            self.stats.dropped += 1
            return None
        cur = self.conn.execute(
            "INSERT INTO slots (slot_ts, ts, seconds, lo_hz, bin_hz, bins, passes, db_min, db_step, "
            "lna_gain_db, vga_gain_db, amp, mean, peak) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                slot_ts, ts, seconds, plan.lo_hz, plan.bin_hz, plan.bins, m.passes, DB_MIN, DB_STEP,
                plan.lna_gain_db, plan.vga_gain_db, quantise(m.mean_db), quantise(m.peak_db),
            ),
        )  # fmt: skip
        self.stats.written += 1
        self.stats.slots_kept += 1
        return cur.lastrowid

    def roll_up(self, now: float | None = None) -> int:
        """Add the daily row of every UTC day that is over (by half an hour,
        so its last slot is in) and has slots but no row yet."""
        now = time.time() if now is None else now
        cutoff = math.floor((now - 1800) / DAY) * DAY
        todo = self.conn.execute(
            "SELECT DISTINCT CAST(ts / 86400 AS INTEGER) * 86400 AS day, lo_hz, bin_hz, bins FROM slots WHERE ts < ? "
            "EXCEPT SELECT CAST(day AS INTEGER), lo_hz, bin_hz, bins FROM daily",
            (cutoff,),
        ).fetchall()
        for day, lo_hz, bin_hz, bins in todo:
            total = np.zeros(bins, dtype=np.float64)
            peak = np.zeros(bins, dtype=np.uint8)
            n = 0
            for mean_b, peak_b in self.conn.execute(
                "SELECT mean, peak FROM slots WHERE ts >= ? AND ts < ? AND lo_hz = ? AND bin_hz = ? AND bins = ?",
                (day, day + DAY, lo_hz, bin_hz, bins),
            ):
                total += _LIN[np.frombuffer(mean_b, dtype=np.uint8)]
                np.maximum(peak, np.frombuffer(peak_b, dtype=np.uint8), out=peak)
                n += 1
            if not n:
                continue
            mean_db = 10.0 * np.log10(total / n)
            self.conn.execute(
                "INSERT OR IGNORE INTO daily (day, lo_hz, bin_hz, bins, slots, db_min, db_step, mean, peak) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (float(day), lo_hz, bin_hz, bins, n, DB_MIN, DB_STEP, quantise(mean_db), peak.tobytes()),
            )
        return len(todo)

    def used_bytes(self) -> int:
        page = self.conn.execute("PRAGMA page_size").fetchone()[0]
        pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
        free = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
        return (pages - free) * page

    def file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def prune(self, now: float | None = None) -> Prune:
        """Roll up finished days, delete slots and daily rows past their
        age, then the oldest slots (or, when none are left, daily rows)
        until the data fits max_bytes; then give the space back."""
        now = time.time() if now is None else now
        result = Prune(rolled_up=self.roll_up(now))
        with self.conn:
            self.conn.execute("BEGIN")
            result.deleted_age = self.conn.execute("DELETE FROM slots WHERE ts < ?", (now - self.max_age_s,)).rowcount
            result.deleted_daily = self.conn.execute(
                "DELETE FROM daily WHERE day < ?", (now - self.daily_age_s,)
            ).rowcount
        for _ in range(50):
            if self.used_bytes() <= self.max_bytes:
                break
            for table, col in (("slots", "ts"), ("daily", "day")):
                count = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if count:
                    break
            if not count:
                break
            n = max(1, count // 10)
            result.deleted_size += self.conn.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY {col} LIMIT ?)", (n,)
            ).rowcount
        self.conn.execute("PRAGMA incremental_vacuum").fetchall()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result.bytes_after = self.file_bytes()
        self.stats.last_prune = result
        self._count()
        return result


# --- Reading ------------------------------------------------------------------


class OccupancyNotFound(FileNotFoundError):
    pass


def open_reader(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise OccupancyNotFound(
            f"no occupancy history at {path} yet: occupancy sweeps are off (HACKRF_LIVE_OCCUPANCY) or none has run"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def utc_hour(ts: float) -> int:
    return int((ts % DAY) // HOUR)


def utc_weekday(ts: float) -> int:
    """0 is Monday (1970-01-01 was a Thursday)."""
    return (int(ts // DAY) + 3) % 7


@dataclass(frozen=True)
class _Grid:
    lo_hz: int
    bin_hz: float
    bins: int

    def where(self, alias: str = "") -> str:
        p = f"{alias}." if alias else ""
        return f"{p}lo_hz = ? AND {p}bin_hz = ? AND {p}bins = ?"

    def args(self) -> tuple:
        return (self.lo_hz, self.bin_hz, self.bins)

    def to_json(self) -> dict:
        return {"lo_hz": self.lo_hz, "bin_hz": self.bin_hz, "bins": self.bins, "hi_hz": self.lo_hz + self.bins * self.bin_hz}


def _newest_grid(conn, table: str, since: float, until: float) -> _Grid | None:
    col = "ts" if table == "slots" else "day"
    row = conn.execute(
        f"SELECT lo_hz, bin_hz, bins FROM {table} WHERE {col} >= ? AND {col} < ? ORDER BY {col} DESC LIMIT 1",
        (since, until),
    ).fetchone()
    return _Grid(*row) if row else None


def _freq_plan(g: _Grid, fmin_hz: float | None, fmax_hz: float | None, fbuckets: int):
    """Bins a:b of grid g covering [fmin, fmax], in buckets of `group`
    whole bins (the last may be short): (a, b, group, starts, counts)."""
    fmin = g.lo_hz if fmin_hz is None else float(fmin_hz)
    fmax = g.lo_hz + g.bins * g.bin_hz if fmax_hz is None else float(fmax_hz)
    if fmax <= fmin:
        raise ValueError("fmax_hz must be above fmin_hz")
    a = int(np.clip(math.floor((fmin - g.lo_hz) / g.bin_hz), 0, g.bins))
    b = int(np.clip(math.ceil((fmax - g.lo_hz) / g.bin_hz), 0, g.bins))
    if b <= a:
        raise ValueError(f"no occupancy bins between {fmin / 1e6:g} and {fmax / 1e6:g} MHz")
    fb = max(1, min(int(fbuckets), MAX_FBUCKETS))
    group = math.ceil((b - a) / fb)
    starts = np.arange(0, b - a, group)
    counts = np.diff(np.append(starts, b - a))
    return a, b, group, starts, counts


def _reduce(mean_b: list[bytes], peak_b: list[bytes], bins: int, a: int, b: int, starts, counts):
    """Rows of bytes to per-bucket linear mean power and peak bytes."""
    n = len(mean_b)
    means = np.frombuffer(b"".join(mean_b), dtype=np.uint8).reshape(n, bins)[:, a:b]
    peaks = np.frombuffer(b"".join(peak_b), dtype=np.uint8).reshape(n, bins)[:, a:b]
    lin = np.add.reduceat(_LIN[means], starts, axis=1) / counts
    return lin, np.maximum.reduceat(peaks, starts, axis=1)


def _freq_json(g: _Grid, a: int, group: int, nb: int) -> dict:
    return {
        "grid": g.to_json(),
        "f_lo_hz": g.lo_hz + a * g.bin_hz,
        "bucket_hz": group * g.bin_hz,
        "fbuckets": nb,
        "bins_per_bucket": group,
    }


def _to_bytes(lin: np.ndarray) -> bytes:
    return quantise(10.0 * np.log10(np.maximum(lin, 1e-30)))


def grid(
    conn: sqlite3.Connection,
    since: float,
    until: float,
    fmin_hz: float | None = None,
    fmax_hz: float | None = None,
    tbuckets: int = 600,
    fbuckets: int = 1200,
    tier: str | None = None,
    chunk: int = 64,
) -> dict:
    """The heat map over [since, until): at most tbuckets time buckets by
    fbuckets frequency buckets of whole bins, each the mean (linear power)
    of its slots' means and the highest peak. Daily rows are used when a
    bucket is a day or wider, or when no slot is left in the range. Only
    the newest grid in the range is used; slots on another (settings
    changed) are counted in other_grid. Empty time buckets are left out."""
    if until <= since:
        raise ValueError("since must be before until")
    tb = max(1, min(int(tbuckets), MAX_TBUCKETS))
    width = (until - since) / tb
    if tier is None:
        tier = "daily" if width >= DAY else "slots"
        if tier == "slots" and _newest_grid(conn, "slots", since, until) is None:
            tier = "daily"
    if tier not in ("slots", "daily"):
        raise ValueError("tier is slots or daily")
    table, col = ("slots", "ts") if tier == "slots" else ("daily", "day")
    lo = since - (DAY if tier == "daily" else 0)  # a day row begins at midnight
    g = _newest_grid(conn, table, lo, until)
    out = {"tier": tier, "since": since, "until": until, "bucket_s": width, "db_min": DB_MIN, "db_step": DB_STEP}
    if g is None:
        return {**out, "ts": [], "rows": 0, "other_grid": 0, "grid": None, "mean": b"", "peak": b""}
    a, b, group, starts, counts = _freq_plan(g, fmin_hz, fmax_hz, fbuckets)
    nb = len(starts)
    if tb * nb > MAX_CELLS:
        raise ValueError(f"{tb} x {nb} buckets is over {MAX_CELLS:,} cells; ask for fewer")
    total = np.zeros((tb, nb), dtype=np.float64)
    peak = np.zeros((tb, nb), dtype=np.uint8)
    count = np.zeros(tb, dtype=np.int64)
    cur = conn.execute(
        f"SELECT {col}, mean, peak FROM {table} WHERE {col} >= ? AND {col} < ? AND {g.where()} ORDER BY {col}",
        (lo, until, *g.args()),
    )
    while rows := cur.fetchmany(chunk):
        ts = np.array([r[0] for r in rows], dtype=np.float64)
        idx = np.clip(((ts - since) / width).astype(np.int64), 0, tb - 1)
        lin, pk = _reduce([r[1] for r in rows], [r[2] for r in rows], g.bins, a, b, starts, counts)
        np.add.at(total, idx, lin)
        np.maximum.at(peak, idx, pk)
        np.add.at(count, idx, 1)
    other = conn.execute(
        f"SELECT count(*) FROM {table} WHERE {col} >= ? AND {col} < ? AND NOT ({g.where()})", (lo, until, *g.args())
    ).fetchone()[0]
    keep = np.nonzero(count)[0]
    return {
        **out,
        **_freq_json(g, a, group, nb),
        "ts": [float(since + i * width) for i in keep],
        "counts": [int(c) for c in count[keep]],
        "rows": int(count.sum()),
        "other_grid": int(other),
        "mean": _to_bytes(total[keep] / count[keep][:, None]) if keep.size else b"",
        "peak": peak[keep].tobytes(),
    }


def profile(
    conn: sqlite3.Connection,
    by: str,
    since: float,
    until: float,
    fmin_hz: float | None = None,
    fmax_hz: float | None = None,
    fbuckets: int = 1200,
    chunk: int = 64,
) -> dict:
    """The slots over [since, until) folded by UTC hour of day (24 rows)
    or day of week (7, Monday first): per row, the mean of the means
    (linear power) and the highest peak, and how many slots it holds."""
    if by not in ("hour", "weekday"):
        raise ValueError("by is hour or weekday")
    if until <= since:
        raise ValueError("since must be before until")
    if until - since > MAX_PROFILE_DAYS * DAY:
        raise ValueError(f"a profile covers at most {MAX_PROFILE_DAYS} days")
    n_rows = 24 if by == "hour" else 7
    key = utc_hour if by == "hour" else utc_weekday
    out = {"by": by, "since": since, "until": until, "db_min": DB_MIN, "db_step": DB_STEP}
    g = _newest_grid(conn, "slots", since, until)
    if g is None:
        return {**out, "grid": None, "counts": [0] * n_rows, "rows": 0, "mean": b"", "peak": b""}
    a, b, group, starts, counts = _freq_plan(g, fmin_hz, fmax_hz, fbuckets)
    nb = len(starts)
    total = np.zeros((n_rows, nb), dtype=np.float64)
    peak = np.zeros((n_rows, nb), dtype=np.uint8)
    count = np.zeros(n_rows, dtype=np.int64)
    cur = conn.execute(
        f"SELECT ts, mean, peak FROM slots WHERE ts >= ? AND ts < ? AND {g.where()} ORDER BY ts", (since, until, *g.args())
    )
    while rows := cur.fetchmany(chunk):
        idx = np.array([key(r[0]) for r in rows], dtype=np.int64)
        lin, pk = _reduce([r[1] for r in rows], [r[2] for r in rows], g.bins, a, b, starts, counts)
        np.add.at(total, idx, lin)
        np.maximum.at(peak, idx, pk)
        np.add.at(count, idx, 1)
    # Rows without slots come out as zero bytes (nothing measured).
    mean = total / np.maximum(count, 1)[:, None]
    return {
        **out,
        **_freq_json(g, a, group, nb),
        "counts": [int(c) for c in count],
        "rows": int(count.sum()),
        "mean": _to_bytes(mean),
        "peak": peak.tobytes(),
    }


def _baseline(conn, g: _Grid, ts: float, min_slots: int, cache: dict) -> np.ndarray | None:
    """Per bin, the median byte of the slots at ts's UTC hour on each of
    the BASELINE_DAYS whole days before ts's day; None with fewer than
    min_slots of them."""
    day = math.floor(ts / DAY) * DAY
    hour = utc_hour(ts)
    if (day, hour) in cache:
        return cache[(day, hour)]
    rows = []
    for k in range(1, BASELINE_DAYS + 1):
        start = day - k * DAY + hour * HOUR
        rows += [
            r[0]
            for r in conn.execute(
                f"SELECT mean FROM slots WHERE ts >= ? AND ts < ? AND {g.where()}", (start, start + HOUR, *g.args())
            )
        ]
    base = None
    if len(rows) >= min_slots:
        stack = np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(len(rows), g.bins)
        base = np.median(stack, axis=0)
    cache[(day, hour)] = base
    return base


def unusual(
    conn: sqlite3.Connection,
    since: float,
    until: float,
    threshold_db: float = 10.0,
    min_slots: int = 3,
    per_slot: int = 20,
    limit: int = 300,
) -> dict:
    """Ranges in the slots of [since, until) whose mean is threshold_db or
    more above the bin's usual level (_baseline), newest slot first and
    the biggest excess first within a slot. Neighbouring flagged bins (one
    bin's gap allowed) make one range."""
    if until <= since:
        raise ValueError("since must be before until")
    if until - since > MAX_UNUSUAL_DAYS * DAY:
        raise ValueError(f"unusual looks at most {MAX_UNUSUAL_DAYS} days at a time")
    out = {"since": since, "until": until, "threshold_db": threshold_db, "baseline_days": BASELINE_DAYS}
    g = _newest_grid(conn, "slots", since, until)
    if g is None:
        return {**out, "grid": None, "slots": 0, "no_baseline": 0, "items": []}
    cache: dict = {}
    items, slots, missing = [], 0, 0
    steps = threshold_db / DB_STEP
    for sid, ts, mean_b in conn.execute(
        f"SELECT id, ts, mean FROM slots WHERE ts >= ? AND ts < ? AND {g.where()} ORDER BY ts DESC",
        (since, until, *g.args()),
    ).fetchall():
        slots += 1
        base = _baseline(conn, g, ts, min_slots, cache)
        if base is None:
            missing += 1
            continue
        mean = np.frombuffer(mean_b, dtype=np.uint8).astype(np.float64)
        excess = mean - base
        hot = np.flatnonzero((excess >= steps) & (mean > 0))
        if not hot.size:
            continue
        breaks = np.flatnonzero(np.diff(hot) > 2) + 1
        found = []
        for run in np.split(hot, breaks):
            j = run[np.argmax(excess[run])]
            found.append(
                {
                    "slot": int(sid),
                    "ts": float(ts),
                    "lo_hz": g.lo_hz + int(run[0]) * g.bin_hz,
                    "hi_hz": g.lo_hz + int(run[-1] + 1) * g.bin_hz,
                    "freq_hz": g.lo_hz + (int(j) + 0.5) * g.bin_hz,
                    "level_db": DB_MIN + float(mean[j]) * DB_STEP,
                    "usual_db": DB_MIN + float(base[j]) * DB_STEP,
                    "excess_db": float(excess[j]) * DB_STEP,
                    "bins": int(run[-1] - run[0] + 1),
                }
            )
        found.sort(key=lambda x: -x["excess_db"])
        items += found[:per_slot]
        if len(items) >= limit:
            break
    return {**out, "grid": g.to_json(), "slots": slots, "no_baseline": missing, "items": items[:limit]}


def cell(conn: sqlite3.Connection, ts: float, fmin_hz: float, fmax_hz: float, min_slots: int = 3) -> dict | None:
    """The slot nearest ts and its level over [fmin, fmax]: the mean
    (linear power) of its bins' means, their highest peak, and the same
    range's usual level (_baseline). None when there is no slot within a
    day of ts."""
    row = conn.execute(
        "SELECT * FROM slots WHERE ts >= ? AND ts < ? ORDER BY abs(ts - ?) LIMIT 1", (ts - DAY, ts + DAY, ts)
    ).fetchone()
    if row is None:
        return None
    g = _Grid(row["lo_hz"], row["bin_hz"], row["bins"])
    a, b, _, _, _ = _freq_plan(g, fmin_hz, fmax_hz, 1)
    mean = np.frombuffer(row["mean"], dtype=np.uint8)[a:b]
    peak = np.frombuffer(row["peak"], dtype=np.uint8)[a:b]
    base = _baseline(conn, g, row["ts"], min_slots, {})
    usual = None
    if base is not None:
        usual = float(10.0 * np.log10(np.mean(_LIN[np.rint(base[a:b]).astype(np.uint8)])))
    return {
        "slot": {k: row[k] for k in ("id", "slot_ts", "ts", "seconds", "passes", "lna_gain_db", "vga_gain_db")},
        "grid": g.to_json(),
        "lo_hz": g.lo_hz + a * g.bin_hz,
        "hi_hz": g.lo_hz + b * g.bin_hz,
        "bins": b - a,
        "mean_db": float(10.0 * np.log10(np.mean(_LIN[mean]))),
        "peak_db": DB_MIN + float(peak.max()) * DB_STEP,
        "usual_db": usual,
        "baseline_days": BASELINE_DAYS,
    }


def slot_list(conn: sqlite3.Connection, since: float, until: float, limit: int = 5000) -> list[dict]:
    """The slots in [since, until), oldest first, without their data."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, slot_ts, ts, seconds, lo_hz, bin_hz, bins, passes, lna_gain_db, vga_gain_db "
            "FROM slots WHERE ts >= ? AND ts < ? ORDER BY ts LIMIT ?",
            (since, until, max(1, int(limit))),
        )
    ]


# --- Alerts -------------------------------------------------------------------


def last_alert_ts(path: Path | None) -> float | None:
    """When the latest alert hit's row was logged, from hackrf-web's
    alerts.db (web/alerts.py's hits table), read-only; None without one."""
    if path is None or not Path(path).exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as conn:
            row = conn.execute("SELECT max(ts) FROM (SELECT ts FROM hits ORDER BY id DESC LIMIT 100)").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


# --- Running the sweep -----------------------------------------------------------


def sweep_runner(live):
    """A function running a Plan's sweep, returning (CSV text, info): info
    as the hackrf-agent's "done" (exit_code, stderr, timed_out). Through
    the agent on a split station, else hackrf_sweep on this machine."""
    if live.agent is not None:
        from hackrf_mcp import agent

        def run_agent(plan: Plan) -> tuple[str, dict]:
            buf = bytearray()
            token = live.agent_token_file.read_text().strip()
            done = agent.run_tool(live.agent, token, plan.agent_request(), buf.extend, wait_s=plan.timeout_s + 330)
            return buf.decode(errors="replace"), done

        return run_agent

    from hackrf_mcp import cli

    def run_local(plan: Plan) -> tuple[str, dict]:
        try:
            proc = subprocess.run(
                [cli._resolve_binary("hackrf_sweep"), *plan.args()], capture_output=True, timeout=plan.timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            return (exc.stdout or b"").decode(errors="replace"), {
                "timed_out": True,
                "stderr": (exc.stderr or b"").decode(errors="replace")[-2000:],
            }
        return proc.stdout.decode(errors="replace"), {
            "exit_code": proc.returncode,
            "stderr": proc.stderr.decode(errors="replace")[-2000:],
            "timed_out": False,
        }

    return run_local


# --- The scheduler ------------------------------------------------------------------


def next_slot(now: float, interval_s: float) -> float:
    """The first slot strictly after now."""
    return (math.floor(now / interval_s) + 1) * interval_s


class Scheduler(threading.Thread):
    """Runs a slot every interval_s on the UTC clock. `service` is the
    LiveService: occupancy_take(lease_s) takes the receiver (or says why
    not), occupancy_give(token) hands it back, _event(...) logs."""

    def __init__(
        self,
        service,
        plan: Plan,
        writer: OccupancyWriter,
        interval_s: float,
        late_s: float,
        alert_quiet_s: float,
        alerts_db: Path | None,
        run_sweep,
        clock=time.time,
        poll_s: float = 5.0,
    ):
        super().__init__(daemon=True, name="occupancy")
        self.service = service
        self.plan = plan
        self.writer = writer
        self.interval_s = interval_s
        self.late_s = late_s
        self.alert_quiet_s = alert_quiet_s
        self.alerts_db = alerts_db
        self.run_sweep = run_sweep
        self.clock = clock
        self.poll_s = poll_s
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.next_at: float | None = None
        self.running_since: float | None = None
        self.last: dict | None = None
        self.counts = {"stored": 0, "skipped": 0, "errors": 0}

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Stop the schedule; True once the thread has ended. A sweep in
        progress isn't waited for (a container gets 10 s to stop): the
        thread is a daemon, and the lease hands the receiver back."""
        self.halt.set()
        self.join(timeout=timeout_s)
        return not self.is_alive()

    def run(self) -> None:
        due = next_slot(self.clock(), self.interval_s)
        while not self.halt.is_set():
            self.next_at = due
            now = self.clock()
            if now < due:
                self.halt.wait(min(due - now, 30.0))
                continue
            try:
                self.slot(due)
            except Exception as exc:  # a broken slot must not end the schedule
                self._finish(due, "error", f"{exc!r}")
            due = next_slot(max(self.clock(), due), self.interval_s)

    def _finish(self, slot_ts: float, result: str, detail: str, **extra) -> str:
        key = {"stored": "stored", "skipped": "skipped"}.get(result, "errors")
        with self.lock:
            self.counts[key] += 1
            self.last = {"slot_ts": slot_ts, "result": result, "detail": detail, "at": self.clock(), **extra}
        event = {"stored": "occupancy-done", "skipped": "occupancy-skip"}.get(result, "occupancy-error")
        self.service._event(event, None, detail)
        return result

    def slot(self, slot_ts: float) -> str:
        """One slot: stored, skipped or error."""
        if self.alert_quiet_s > 0:
            hit = last_alert_ts(self.alerts_db)
            if hit is not None and 0 <= self.clock() - hit < self.alert_quiet_s:
                ago = (self.clock() - hit) / 60
                return self._finish(slot_ts, "skipped", f"an alert hit {ago:.0f} min ago")
        if not self.writer.disk_ok():
            return self._finish(slot_ts, "skipped", "free disk space is under the floor")
        lease = self.plan.timeout_s + 30
        waited_for = None
        while True:
            took = self.service.occupancy_take(lease)
            if took["state"] == "ok":
                break
            if took["state"] == "watch":
                return self._finish(slot_ts, "skipped", "a watch has the receiver")
            waited_for = took.get("owner") or "a device tool"
            if self.clock() >= slot_ts + self.late_s or self.halt.is_set():
                return self._finish(
                    slot_ts, "skipped", f"the receiver was lent to {waited_for} past {self.late_s / 60:g} min"
                )
            self.halt.wait(self.poll_s)
        started = self.clock()
        late = started - slot_ts
        note = f", {late / 60:.0f} min late (the receiver was lent to {waited_for})" if waited_for and late >= 60 else ""
        self.running_since = started
        failed: Exception | None = None
        text, info = "", {}
        try:
            text, info = self.run_sweep(self.plan)
        except Exception as exc:
            failed = exc
        finally:
            held = self.clock() - started
            self.service.occupancy_give(took["token"])
            self.running_since = None
        # The receiver is back; the rest doesn't hold up decoding.
        paused = took.get("paused_profile")
        what = self.plan.describe() + note + (f"; {paused} paused {held:.1f} s" if paused else "")
        if failed is not None:
            return self._finish(slot_ts, "error", f"{what}: the sweep failed: {failed}", seconds=held)
        try:
            m = parse(text, self.plan)
        except SweepError as exc:
            why = (info.get("stderr") or "").strip().splitlines()[-1:] or [""]
            extra = " (timed out)" if info.get("timed_out") else f" (exit code {info.get('exit_code')})" if info.get("exit_code") else ""
            return self._finish(slot_ts, "error", f"{what}: {exc}{extra}{': ' + why[0] if why[0] else ''}", seconds=held)
        sid = self.writer.add(slot_ts, started, held, self.plan, m)
        if sid is None:
            return self._finish(slot_ts, "skipped", f"{what}: measured but not stored, free disk space is under the floor", seconds=held)
        try:
            self.writer.prune(self.clock())
        except sqlite3.Error as exc:
            self.service._event("occupancy-error", None, f"pruning occupancy.db failed: {exc}")
        cut = f", {m.partial} partial left out" if m.partial else ""
        return self._finish(
            slot_ts,
            "stored",
            f"{what}: {m.passes} passes in {held:.1f} s, {self.plan.bins:,} bins{cut}",
            seconds=held,
            passes=m.passes,
            slot_id=sid,
        )

    def status(self) -> dict:
        p = self.plan
        with self.lock:
            last = dict(self.last) if self.last else None
            counts = dict(self.counts)
        w = self.writer
        return {
            "enabled": True,
            "interval_min": self.interval_s / 60,
            "range_mhz": [p.lo_mhz, p.top_mhz],
            "bin_hz": p.bin_hz,
            "bins": p.bins,
            "passes": p.passes,
            "lna_gain_db": p.lna_gain_db,
            "vga_gain_db": p.vga_gain_db,
            "amp": False,
            "late_min": self.late_s / 60,
            "alert_quiet_min": self.alert_quiet_s / 60,
            "next_slot": self.next_at,
            "sweeping_since": self.running_since,
            "last": last,
            **counts,
            "path": str(w.path),
            "file_bytes": w.file_bytes(),
            "max_mb": w.max_bytes / MIB,
            "max_age_days": w.max_age_s / DAY,
            "daily_days": w.daily_age_s / DAY,
            "slots_kept": w.stats.slots_kept,
            "days_kept": w.stats.days_kept,
            "low_disk": w.stats.low_disk,
        }
