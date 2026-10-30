"""The live decoder's rolling log: a SQLite database of decoded messages.

The live service (live_service.py) is the only writer. The MCP server opens
the file read-only to search and summarise it. WAL mode lets the two run at
once; auto_vacuum=INCREMENTAL lets pruning actually shrink the file.

Each row is one decoded message, or for ADS-B and AIS position reports one
aircraft or ship per minute (see live_profiles.py), with:
  ts / ts_end   first and last time (Unix seconds, UTC) the row covers
  profile       the profile that decoded it (adsb, ais, acars, ...)
  kind          what it is (aircraft, vessel, acars, vdl2, page, aprs, eas, ...)
  device        the receiver (always "hackrf" for now)
  freq_hz       the channel it was heard on
  level_db      signal level as the decoder reports it (dBFS, or dB)
  subject       the normalised identity searched for exactly: ICAO address,
                MMSI, pager capcode, APRS source, aircraft registration ...
  label         a secondary name: callsign, flight number, ship name ...
  fields        the decoded fields, as JSON
  raw           the decoder's text for it
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    ts_end REAL,
    profile TEXT NOT NULL,
    kind TEXT NOT NULL,
    device TEXT NOT NULL DEFAULT 'hackrf',
    freq_hz INTEGER,
    level_db REAL,
    subject TEXT,
    label TEXT,
    fields TEXT NOT NULL,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_profile_ts ON messages (profile, ts);
CREATE INDEX IF NOT EXISTS messages_subject ON messages (subject COLLATE NOCASE, ts);
CREATE INDEX IF NOT EXISTS messages_label ON messages (label COLLATE NOCASE, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    profile TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""

# The longest stretch one row covers (a per-minute summary), in seconds.
ROW_SPAN_S = 120
# The raw text kept per row; a decoder line longer than this is cut.
MAX_RAW_CHARS = 4000
# The events table is small, but bounded anyway.
MAX_EVENTS = 20_000


@dataclass
class Row:
    ts: float
    profile: str
    kind: str
    fields: dict
    raw: str | None = None
    freq_hz: int | None = None
    level_db: float | None = None
    subject: str | None = None
    label: str | None = None
    ts_end: float | None = None
    device: str = "hackrf"


def _clean_level(level: float | None) -> float | None:
    if level is None:
        return None
    try:
        level = float(level)
    except (TypeError, ValueError):
        return None
    return round(level, 1) if level == level and abs(level) != float("inf") else None


@dataclass
class PruneResult:
    deleted_age: int = 0
    deleted_size: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


@dataclass
class WriterStats:
    rows_written: int = 0
    rows_dropped: int = 0
    low_disk: bool = False
    last_prune: PruneResult = field(default_factory=PruneResult)


class LiveLogWriter:
    """The write side, used by the live service from one thread."""

    def __init__(self, path: Path, max_age_s: float, max_bytes: int, min_free_bytes: int):
        self.path = Path(path)
        self.max_age_s = max_age_s
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.stats = WriterStats()
        self._last_disk_check = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        if new:
            # Must be set before the first table exists to take effect.
            self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        # Keep the WAL file from staying large after a big prune.
        self.conn.execute(f"PRAGMA journal_size_limit = {16 * MIB}")
        self.conn.executescript(SCHEMA)
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

    def write(self, rows: list[Row], now: float | None = None) -> int:
        """Insert rows in one transaction. Returns how many were written;
        none are while free disk space is under the floor."""
        if not rows:
            return 0
        now = time.time() if now is None else now
        if not self._disk_ok(now):
            self.stats.rows_dropped += len(rows)
            return 0
        values = [
            (
                r.ts,
                r.ts_end,
                r.profile,
                r.kind,
                r.device,
                r.freq_hz,
                _clean_level(r.level_db),
                r.subject,
                r.label,
                json.dumps(r.fields, separators=(",", ":"), default=str),
                None if r.raw is None else r.raw[:MAX_RAW_CHARS],
            )
            for r in rows
        ]
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO messages (ts, ts_end, profile, kind, device, freq_hz, level_db, subject, label, fields, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        self.stats.rows_written += len(rows)
        return len(rows)

    def event(self, event: str, profile: str | None = None, detail: str | None = None, ts: float | None = None) -> None:
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "INSERT INTO events (ts, event, profile, detail) VALUES (?, ?, ?, ?)",
                (time.time() if ts is None else ts, event, profile, detail),
            )

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

    def prune(self, now: float | None = None) -> PruneResult:
        """Delete rows older than the age limit, then the oldest rows until
        the data fits the size limit, then give the space back to the disk."""
        now = time.time() if now is None else now
        result = PruneResult(bytes_before=self.file_bytes())
        cutoff = now - self.max_age_s
        with self.conn:
            self.conn.execute("BEGIN")
            result.deleted_age = self.conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount
            self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.execute(
                "DELETE FROM events WHERE id <= (SELECT max(id) FROM events) - ?", (MAX_EVENTS,)
            )
        # Each pass removes a tenth of the rows, oldest first; the used size
        # only falls once the freed pages are counted, so it's re-read.
        for _ in range(50):
            if self.used_bytes() <= self.max_bytes:
                break
            count = self.conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            if count == 0:
                break
            n = max(1, count // 10)
            with self.conn:
                self.conn.execute("BEGIN")
                result.deleted_size += self.conn.execute(
                    "DELETE FROM messages WHERE id IN (SELECT id FROM messages ORDER BY id LIMIT ?)", (n,)
                ).rowcount
        # The pragma frees pages as its result is stepped through, so read it all.
        self.conn.execute("PRAGMA incremental_vacuum").fetchall()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result.bytes_after = self.file_bytes()
        self.stats.last_prune = result
        return result


# --- Reading ---------------------------------------------------------------


class LogNotFound(FileNotFoundError):
    pass


def open_reader(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise LogNotFound(f"no live log at {path} yet: the live service has not written one")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def normalise_subject(value: str) -> str:
    """What a subject is stored as: ICAO addresses and callsigns upper case,
    capcodes and MMSIs without leading zeros."""
    v = value.strip()
    if v.isdigit():
        return str(int(v))
    return v.upper()


def search(
    conn: sqlite3.Connection,
    since: float | None = None,
    until: float | None = None,
    profile: str | None = None,
    kind: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    freq_hz: int | None = None,
    freq_tolerance_hz: int = 5_000,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Rows matching every filter given, newest first. `subject` matches
    the subject or the label exactly (case-insensitive), so an ICAO address,
    MMSI, capcode, callsign, flight or ship name all work; `text` is a
    substring of the raw text, the fields JSON, the subject or the label."""
    where, args = [], []
    if since is not None:
        # A row covers at most a minute (ts..ts_end), so the ts index can
        # narrow the scan before the exact test.
        where.append("ts >= ? AND coalesce(ts_end, ts) >= ?")
        args += [since - ROW_SPAN_S, since]
    if until is not None:
        where.append("ts <= ?")
        args.append(until)
    if profile:
        where.append("profile = ?")
        args.append(profile)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if subject:
        where.append("(subject = ? COLLATE NOCASE OR label = ? COLLATE NOCASE)")
        args += [normalise_subject(subject), subject.strip()]
    if text:
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            "(raw LIKE ? ESCAPE '\\' OR fields LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' OR label LIKE ? ESCAPE '\\')"
        )
        args += [pattern] * 4
    if freq_hz is not None:
        where.append("freq_hz BETWEEN ? AND ?")
        args += [freq_hz - freq_tolerance_hz, freq_hz + freq_tolerance_hz]
    sql = "SELECT * FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    return conn.execute(sql, args).fetchall()


def events(conn: sqlite3.Connection, since: float | None = None, limit: int = 20) -> list[sqlite3.Row]:
    if since is None:
        return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return conn.execute(
        "SELECT * FROM events WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit)
    ).fetchall()


def overview(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT count(*), min(ts), max(coalesce(ts_end, ts)) FROM messages").fetchone()
    per_profile = conn.execute(
        "SELECT profile, count(*), max(coalesce(ts_end, ts)) FROM messages GROUP BY profile ORDER BY profile"
    ).fetchall()
    return {
        "rows": row[0],
        "oldest": row[1],
        "newest": row[2],
        "profiles": {p: {"rows": n, "newest": t} for p, n, t in per_profile},
    }


def summarise(
    conn: sqlite3.Connection,
    profile: str,
    kinds: tuple[str, ...],
    since: float,
    until: float | None = None,
    limit: int = 100,
) -> list[dict]:
    """One entry per subject a profile logged in the range, most recently
    heard first: first and last time, rows, the latest non-empty value of
    each field, and the strongest level. `kinds` is the profile's
    summary_kinds (live_profiles.Profile); other kinds are left out."""
    args: list = [profile, since - ROW_SPAN_S, since]
    sql = (
        "SELECT ts, coalesce(ts_end, ts) AS last, kind, freq_hz, level_db, subject, label, fields "
        "FROM messages WHERE profile = ? AND ts >= ? AND coalesce(ts_end, ts) >= ?"
    )
    if kinds:
        sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
        args += list(kinds)
    if until is not None:
        sql += " AND ts <= ?"
        args.append(until)
    sql += " ORDER BY ts"
    by_subject: dict[str, dict] = {}
    for r in conn.execute(sql, args):
        key = r["subject"] or r["label"] or "?"
        s = by_subject.get(key)
        if s is None:
            s = by_subject[key] = {
                "subject": key,
                "first": r["ts"],
                "last": r["last"],
                "rows": 0,
                "kinds": set(),
                "freqs": set(),
                "labels": [],
                "max_level_db": None,
                "fields": {},
            }
        s["rows"] += 1
        s["last"] = max(s["last"], r["last"])
        s["kinds"].add(r["kind"])
        if r["freq_hz"]:
            s["freqs"].add(r["freq_hz"])
        if r["label"] and r["label"] not in s["labels"]:
            s["labels"].append(r["label"])
        if r["level_db"] is not None and (s["max_level_db"] is None or r["level_db"] > s["max_level_db"]):
            s["max_level_db"] = r["level_db"]
        try:
            fields = json.loads(r["fields"])
        except ValueError:
            fields = {}
        for k, v in fields.items():
            if v in (None, "", [], {}):
                continue
            if k in ("messages", "positions"):
                s["fields"][k] = s["fields"].get(k, 0) + (v if isinstance(v, (int, float)) else 0)
            elif k in ("altitude_max",):
                s["fields"][k] = max(s["fields"].get(k, v), v)
            elif k in ("altitude_min",):
                s["fields"][k] = min(s["fields"].get(k, v), v)
            else:
                s["fields"][k] = v
    out = sorted(by_subject.values(), key=lambda s: -s["last"])
    return out[: max(1, int(limit))]
