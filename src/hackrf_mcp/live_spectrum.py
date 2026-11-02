"""Spectrum history: what the live service's band looked like, second by
second, kept beside the decoded log so a detection can be seen as it was on
the air.

Every profile that reads the HackRF through a live_profiles.Source (all of
them except AIS, whose decoder opens the device itself) has its sample
blocks passed to a SpectrumTap before the decoders get them. The tap runs
512-point FFTs (Hann window) over the blocks and, for each second of
samples, keeps two values per bin: the mean power over the second and the
peak, the strongest single FFT frame. The mean shows steady carriers and
the noise floor; the peak shows bursts too short to lift a one-second mean
(an ADS-B squitter, an AIS slot, a pager burst).

Rows go to spectrum.db beside live.db: its own file, with its own limits
(LiveSettings.spectrum_*), written by the live service's LogPump thread
like the log.

  segments  one per run of a profile (a start, a switch, a resume after a
            pause): profile, tuning, gains, and the scale its rows use
  spectra   tier 1: one row per second of samples; tier 60: one per clock
            minute, rolled up from the seconds (mean of the means in linear
            power, max of the peaks), kept longer for day and week views.
            Each has ts (Unix seconds, UTC, when its first samples arrived),
            its segment, how many FFT frames it holds, and `mean` and
            `peak`, one byte per bin.

A byte b is db_min + b * db_step dBFS (each segment records its own; today
-130 dBFS and 0.5 dB, so -130 to -2.5 dBFS), where 0 dBFS is a full-scale
tone. Bins are in frequency order: bin i is centred on
center_hz + (i - bins / 2) * sample_rate_hz / bins. The HackRF's DC spike
sits on bin bins / 2.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import scipy.fft

MIB = 1024 * 1024

BINS = 512
DB_MIN = -130.0
DB_STEP = 0.5
TIER_SECOND = 1
TIER_MINUTE = 60
# How often the live view (SpectrumRecorder.add_live_sink) gets a row.
LIVE_ROW_S = 0.25


def quantise(db: np.ndarray, db_min: float = DB_MIN, db_step: float = DB_STEP) -> bytes:
    """dB values as one byte each, clipped to the scale's 256 steps."""
    q = np.rint((np.asarray(db, dtype=np.float64) - db_min) / db_step)
    return np.clip(np.nan_to_num(q, nan=0.0, neginf=0.0, posinf=255.0), 0, 255).astype(np.uint8).tobytes()


def dequantise(blob: bytes, db_min: float = DB_MIN, db_step: float = DB_STEP) -> np.ndarray:
    return (db_min + np.frombuffer(blob, dtype=np.uint8).astype(np.float32) * db_step).astype(np.float32)


def bin_freqs(center_hz: int, sample_rate_hz: int, bins: int = BINS) -> np.ndarray:
    """The centre frequency of each bin, in Hz."""
    return center_hz + (np.arange(bins) - bins // 2) * (sample_rate_hz / bins)


def _db(power: np.ndarray) -> np.ndarray:
    return (10.0 * np.log10(np.maximum(power, 1e-30))).astype(np.float32)


# --- The tap ---------------------------------------------------------------


@dataclass
class SpectrumRow:
    ts: float  # wall time when the row's first block arrived
    seconds: float  # sample time the row covers
    frames: int  # FFT frames averaged
    mean_db: np.ndarray  # float32[bins], frequency order
    peak_db: np.ndarray


class _Accumulator:
    def __init__(self, bins: int):
        self.bins = bins
        self.reset()

    def reset(self) -> None:
        self.ts: float | None = None
        self.sum = np.zeros(self.bins, dtype=np.float64)
        self.max = np.zeros(self.bins, dtype=np.float32)
        self.frames = 0
        self.samples = 0

    def add(self, power: np.ndarray) -> None:
        self.sum += power.sum(axis=0, dtype=np.float64)
        np.maximum(self.max, power.max(axis=0), out=self.max)
        self.frames += power.shape[0]

    def row(self, sample_rate_hz: int, scale: float) -> SpectrumRow:
        mean = np.fft.fftshift(self.sum / max(self.frames, 1) * scale)
        peak = np.fft.fftshift(self.max.astype(np.float64) * scale)
        return SpectrumRow(self.ts or 0.0, self.samples / sample_rate_hz, self.frames, _db(mean), _db(peak))


class SpectrumTap:
    """Called with every block of int8 I/Q bytes a Source delivers, from the
    source's thread. Calls on_row with one SpectrumRow per `row_s` of
    samples (counted, so rows average exactly row_s however blocks fall),
    and on_live, if given, with one per `live_s`.

    `duty` (0-1] is the share of each block's 512-sample frames that are
    transformed, spread evenly through the block; 1 transforms every
    sample, so the peak catches every burst."""

    def __init__(
        self,
        sample_rate_hz: int,
        on_row: Callable[[SpectrumRow], None],
        *,
        bins: int = BINS,
        duty: float = 1.0,
        row_s: float = 1.0,
        on_live: Callable[[SpectrumRow], None] | None = None,
        live_s: float = LIVE_ROW_S,
        clock: Callable[[], float] = time.time,
    ):
        if not 0 < duty <= 1:
            raise ValueError(f"duty must be in (0, 1], not {duty}")
        self.sample_rate_hz = int(sample_rate_hz)
        self.bins = bins
        self.duty = duty
        self.on_row = on_row
        self.on_live = on_live
        self.clock = clock
        window = np.hanning(bins).astype(np.float32)
        # int8 samples scaled to [-1, 1) and windowed in one multiply; a
        # full-scale tone on a bin centre then reads 0 dBFS.
        self._weights = np.repeat(window / 128.0, 2).astype(np.float32)
        self.scale = 1.0 / float(window.sum()) ** 2
        self.row_samples = sample_rate_hz * row_s
        self.live_samples = sample_rate_hz * live_s
        self._row = _Accumulator(bins)
        self._live = _Accumulator(bins)
        self._samples = 0
        self._next_row = self.row_samples
        self._next_live = self.live_samples
        self.rows = 0

    def __call__(self, buf: bytes) -> None:
        n = len(buf) // 2
        if n == 0:
            return
        now = self.clock()
        if self._row.ts is None:
            self._row.ts = now
        if self.on_live is not None and self._live.ts is None:
            self._live.ts = now
        frames = n // self.bins
        if frames:
            raw = np.frombuffer(buf, dtype=np.int8, count=frames * self.bins * 2).reshape(frames, 2 * self.bins)
            if self.duty < 1:
                k = max(1, int(round(frames * self.duty)))
                if k < frames:
                    raw = raw[np.linspace(0, frames - 1, k).round().astype(np.intp)]
            x = (raw * self._weights).view(np.complex64)
            # scipy's FFT is several times faster than numpy's on complex64.
            power = np.abs(scipy.fft.fft(x, axis=1, overwrite_x=True))
            power *= power
            self._row.add(power)
            if self.on_live is not None:
                self._live.add(power)
        self._row.samples += n
        self._live.samples += n
        self._samples += n
        if self._samples >= self._next_row:
            self._next_row += self.row_samples
            if self._row.frames:
                self.rows += 1
                self.on_row(self._row.row(self.sample_rate_hz, self.scale))
            self._row.reset()
        if self.on_live is not None and self._samples >= self._next_live:
            self._next_live += self.live_samples
            if self._live.frames:
                self.on_live(self._live.row(self.sample_rate_hz, self.scale))
            self._live.reset()


# --- The recorder: segments and the minute roll-up --------------------------


class SpectrumRecorder:
    """The live service's side: a tap per run of a profile (tap_for() when a
    source is made, end() when the pipeline stops), each run its own
    segment; second rows go to `put` as they come, minute rows when the
    clock minute or the segment ends. `put` takes the items SpectrumWriter
    .apply() understands (LogPump.spectrum queues them for it)."""

    def __init__(
        self,
        put: Callable[[tuple], None],
        *,
        bins: int = BINS,
        duty: float = 1.0,
        db_min: float = DB_MIN,
        db_step: float = DB_STEP,
        clock: Callable[[], float] = time.time,
    ):
        self.put = put
        self.bins = bins
        self.duty = duty
        self.db_min = db_min
        self.db_step = db_step
        self.clock = clock
        self.lock = threading.Lock()
        self.current: int | None = None
        self.segment: dict | None = None
        self.tap: SpectrumTap | None = None
        self._token = 0
        self._minute: int | None = None
        self._m_ts = 0.0
        self._m_sum = np.zeros(bins, dtype=np.float64)
        self._m_peak = np.full(bins, -np.inf, dtype=np.float32)
        self._m_frames = 0
        self.rows = 0
        self.last_row_at: float | None = None
        self.live_sinks: list[Callable[[dict, SpectrumRow], None]] = []

    def tap_for(self, tuning, sample_rate_hz: int | None = None) -> SpectrumTap:
        """A new segment for `tuning` (a live_profiles.Tuning) and its tap."""
        rate = int(sample_rate_hz or tuning.sample_rate_hz)
        with self.lock:
            self._end_locked(None)
            self._token += 1
            token = self._token
            self.segment = {
                "profile": tuning.profile.name,
                "center_hz": int(tuning.center_hz),
                "sample_rate_hz": rate,
                "bins": self.bins,
                "db_min": self.db_min,
                "db_step": self.db_step,
                "lna_gain_db": tuning.lna_gain_db,
                "vga_gain_db": tuning.vga_gain_db,
                "amp": bool(tuning.amp),
                "duty": self.duty,
                "ts_start": self.clock(),
            }
            self.current = token
            self.put(("segment", token, dict(self.segment)))
            self.tap = SpectrumTap(
                rate,
                lambda row, t=token: self._row(t, row),
                bins=self.bins,
                duty=self.duty,
                on_live=lambda row, t=token: self._live_row(t, row),
                clock=self.clock,
            )
            return self.tap

    def add_live_sink(self, sink: Callable[[dict, SpectrumRow], None]) -> None:
        """Call sink(segment, row) about four times a second while a segment
        runs, from the source's thread; it must return quickly."""
        self.live_sinks.append(sink)

    def _live_row(self, token: int, row: SpectrumRow) -> None:
        if not self.live_sinks or token != self.current or self.segment is None:
            return
        for sink in list(self.live_sinks):
            try:
                sink(self.segment, row)
            except Exception:
                pass  # a viewer must not stop the recording

    def _row(self, token: int, row: SpectrumRow) -> None:
        with self.lock:
            if token != self.current:
                return
            minute = int(row.ts // 60)
            if self._minute is not None and minute != self._minute:
                self._flush_minute(token)
            if self._minute is None:
                self._minute = minute
                self._m_ts = row.ts
            self._m_sum += np.power(10.0, row.mean_db.astype(np.float64) / 10.0) * row.frames
            np.maximum(self._m_peak, row.peak_db, out=self._m_peak)
            self._m_frames += row.frames
            self.put(
                (
                    "row",
                    token,
                    TIER_SECOND,
                    row.ts,
                    row.frames,
                    quantise(row.mean_db, self.db_min, self.db_step),
                    quantise(row.peak_db, self.db_min, self.db_step),
                )
            )
            self.rows += 1
            self.last_row_at = row.ts

    def _flush_minute(self, token: int) -> None:
        if self._m_frames:
            mean = _db(self._m_sum / self._m_frames)
            self.put(
                (
                    "row",
                    token,
                    TIER_MINUTE,
                    self._m_ts,
                    self._m_frames,
                    quantise(mean, self.db_min, self.db_step),
                    quantise(self._m_peak, self.db_min, self.db_step),
                )
            )
        self._minute = None
        self._m_sum[:] = 0.0
        self._m_peak[:] = -np.inf
        self._m_frames = 0

    def _end_locked(self, ts: float | None) -> None:
        if self.current is None:
            return
        self._flush_minute(self.current)
        self.put(("end", self.current, self.clock() if ts is None else ts))
        self.current = None
        self.segment = None
        self.tap = None

    def end(self, ts: float | None = None) -> None:
        """The segment's samples have stopped (the pipeline stopped)."""
        with self.lock:
            self._end_locked(ts)

    def status(self) -> dict:
        seg = self.segment
        return {
            "segment": None
            if seg is None
            else {k: seg[k] for k in ("profile", "center_hz", "sample_rate_hz", "ts_start")},
            "rows_recorded": self.rows,
            "last_row_at": self.last_row_at,
            "duty": self.duty,
            "bins": self.bins,
        }


# --- The database ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    ts_start REAL NOT NULL,
    ts_end REAL,
    profile TEXT NOT NULL,
    center_hz INTEGER NOT NULL,
    sample_rate_hz INTEGER NOT NULL,
    bins INTEGER NOT NULL,
    db_min REAL NOT NULL,
    db_step REAL NOT NULL,
    lna_gain_db INTEGER,
    vga_gain_db INTEGER,
    amp INTEGER,
    duty REAL
);
CREATE INDEX IF NOT EXISTS segments_ts ON segments (ts_start);
CREATE TABLE IF NOT EXISTS spectra (
    id INTEGER PRIMARY KEY,
    segment INTEGER NOT NULL,
    tier INTEGER NOT NULL,
    ts REAL NOT NULL,
    frames INTEGER NOT NULL,
    mean BLOB NOT NULL,
    peak BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS spectra_tier_ts ON spectra (tier, ts);
CREATE INDEX IF NOT EXISTS spectra_segment ON spectra (segment, tier, ts);
"""

_SEGMENT_COLUMNS = (
    "ts_start",
    "profile",
    "center_hz",
    "sample_rate_hz",
    "bins",
    "db_min",
    "db_step",
    "lna_gain_db",
    "vga_gain_db",
    "amp",
    "duty",
)


@dataclass
class SpectrumPrune:
    deleted_age: int = 0
    deleted_size: int = 0
    deleted_segments: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


@dataclass
class SpectrumStats:
    rows_written: int = 0
    rows_dropped: int = 0
    low_disk: bool = False
    last_prune: SpectrumPrune = field(default_factory=SpectrumPrune)


class SpectrumWriter:
    """The write side of spectrum.db, used from LogPump's thread only."""

    def __init__(
        self,
        path: Path,
        max_age_s: float,
        minute_age_s: float,
        max_bytes: int,
        min_free_bytes: int,
    ):
        self.path = Path(path)
        self.max_age_s = max_age_s
        self.minute_age_s = minute_age_s
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.stats = SpectrumStats()
        self._last_disk_check = 0.0
        self._ids: dict[int, int] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        if new:
            self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute(f"PRAGMA journal_size_limit = {16 * MIB}")
        self.conn.executescript(SCHEMA)
        # Segments a previous run left open (a crash, a kill) end at their
        # last row.
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "UPDATE segments SET ts_end = coalesce("
                "(SELECT max(ts) FROM spectra WHERE spectra.segment = segments.id), ts_start) "
                "WHERE ts_end IS NULL"
            )
        try:
            os.chmod(self.path, 0o640)
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()

    def _disk_ok(self, now: float) -> bool:
        if now - self._last_disk_check >= 30 or self.stats.low_disk:
            self._last_disk_check = now
            free = shutil.disk_usage(self.path.parent).free
            self.stats.low_disk = free < self.min_free_bytes
        return not self.stats.low_disk

    def apply(self, items: list[tuple], now: float | None = None) -> int:
        """Write SpectrumRecorder's items in one transaction: ("segment",
        token, meta), ("row", token, tier, ts, frames, mean, peak) and
        ("end", token, ts). Rows are dropped while free disk space is under
        the floor; segments are still recorded. Returns rows written."""
        if not items:
            return 0
        now = time.time() if now is None else now
        disk_ok = self._disk_ok(now)
        written = dropped = 0
        # Token-to-id changes take effect only if the transaction commits.
        ids = dict(self._ids)
        with self.conn:
            self.conn.execute("BEGIN")
            for item in items:
                kind, token = item[0], item[1]
                if kind == "segment":
                    meta = item[2]
                    cur = self.conn.execute(
                        f"INSERT INTO segments ({', '.join(_SEGMENT_COLUMNS)}) "
                        f"VALUES ({', '.join('?' * len(_SEGMENT_COLUMNS))})",
                        [int(meta[c]) if c == "amp" else meta[c] for c in _SEGMENT_COLUMNS],
                    )
                    ids[token] = cur.lastrowid
                elif kind == "row":
                    seg = ids.get(token)
                    if seg is None:
                        continue
                    if not disk_ok:
                        dropped += 1
                        continue
                    _, _, tier, ts, frames, mean, peak = item
                    self.conn.execute(
                        "INSERT INTO spectra (segment, tier, ts, frames, mean, peak) VALUES (?, ?, ?, ?, ?, ?)",
                        (seg, tier, ts, frames, mean, peak),
                    )
                    written += 1
                elif kind == "end":
                    seg = ids.pop(token, None)
                    if seg is not None:
                        self.conn.execute("UPDATE segments SET ts_end = ? WHERE id = ?", (item[2], seg))
        self._ids = ids
        self.stats.rows_written += written
        self.stats.rows_dropped += dropped
        return written

    def used_bytes(self) -> int:
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
        free = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
        return (pages - free) * page_size

    def file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def prune(self, now: float | None = None) -> SpectrumPrune:
        """Delete second rows older than max_age_s and minute rows older than
        minute_age_s; then the oldest second rows (or, when none are left,
        minute rows) until the data fits max_bytes; then segments with no
        rows left; then give the space back to the disk."""
        now = time.time() if now is None else now
        result = SpectrumPrune(bytes_before=self.file_bytes())
        with self.conn:
            self.conn.execute("BEGIN")
            result.deleted_age = self.conn.execute(
                "DELETE FROM spectra WHERE tier = ? AND ts < ?", (TIER_SECOND, now - self.max_age_s)
            ).rowcount
            result.deleted_age += self.conn.execute(
                "DELETE FROM spectra WHERE tier = ? AND ts < ?", (TIER_MINUTE, now - self.minute_age_s)
            ).rowcount
        for _ in range(50):
            if self.used_bytes() <= self.max_bytes:
                break
            for tier in (TIER_SECOND, TIER_MINUTE):
                count = self.conn.execute("SELECT count(*) FROM spectra WHERE tier = ?", (tier,)).fetchone()[0]
                if count:
                    break
            if count == 0:
                break
            n = max(1, count // 10)
            with self.conn:
                self.conn.execute("BEGIN")
                result.deleted_size += self.conn.execute(
                    "DELETE FROM spectra WHERE id IN (SELECT id FROM spectra WHERE tier = ? ORDER BY ts LIMIT ?)",
                    (tier, n),
                ).rowcount
        open_ids = tuple(self._ids.values())
        with self.conn:
            self.conn.execute("BEGIN")
            sql = (
                "DELETE FROM segments WHERE ts_end IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM spectra WHERE spectra.segment = segments.id)"
            )
            if open_ids:
                sql += f" AND id NOT IN ({', '.join('?' * len(open_ids))})"
            result.deleted_segments = self.conn.execute(sql, open_ids).rowcount
        self.conn.execute("PRAGMA incremental_vacuum").fetchall()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result.bytes_after = self.file_bytes()
        self.stats.last_prune = result
        return result


# --- Reading -----------------------------------------------------------------


class SpectrumNotFound(FileNotFoundError):
    pass


def open_reader(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise SpectrumNotFound(f"no spectrum history at {path} yet: the live service has not written one")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def segments(conn: sqlite3.Connection, since: float | None = None, until: float | None = None) -> list[dict]:
    """Segments overlapping [since, until], oldest first."""
    where, args = [], []
    if since is not None:
        where.append("coalesce(ts_end, 1e18) >= ?")
        args.append(since)
    if until is not None:
        where.append("ts_start <= ?")
        args.append(until)
    sql = "SELECT * FROM segments" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts_start"
    return [dict(r) for r in conn.execute(sql, args)]


def read(
    conn: sqlite3.Connection,
    since: float | None = None,
    until: float | None = None,
    tier: int = TIER_SECOND,
    segment: int | None = None,
    limit: int = 3600,
) -> list[dict]:
    """Rows of one tier in [since, until], oldest first, with `mean` and
    `peak` as float32 dB arrays on their segment's scale."""
    where, args = ["s.tier = ?"], [tier]
    if since is not None:
        where.append("s.ts >= ?")
        args.append(since)
    if until is not None:
        where.append("s.ts <= ?")
        args.append(until)
    if segment is not None:
        where.append("s.segment = ?")
        args.append(segment)
    sql = (
        "SELECT s.ts, s.segment, s.frames, s.mean, s.peak, g.db_min, g.db_step, g.center_hz, "
        "g.sample_rate_hz, g.profile FROM spectra s JOIN segments g ON g.id = s.segment "
        f"WHERE {' AND '.join(where)} ORDER BY s.ts LIMIT ?"
    )
    args.append(max(1, int(limit)))
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["mean"] = dequantise(r["mean"], r["db_min"], r["db_step"])
        d["peak"] = dequantise(r["peak"], r["db_min"], r["db_step"])
        out.append(d)
    return out
