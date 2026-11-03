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

ADS-B and AIS also write `positions`, for map tracks finer than the
per-minute rows: at most one per aircraft or ship every 10 s, and while it
stands still one every 5 minutes (live_profiles.PositionThrottle), with
time, profile, subject, lat, lon, altitude (feet, ADS-B), speed (knots)
and track (degrees: ADS-B track, AIS course over ground). They are pruned
with the messages.
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
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    profile TEXT NOT NULL,
    subject TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt REAL,
    speed REAL,
    track REAL
);
CREATE INDEX IF NOT EXISTS positions_ts ON positions (ts);
CREATE INDEX IF NOT EXISTS positions_subject ON positions (subject, ts);
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


@dataclass
class Position:
    ts: float
    profile: str
    subject: str
    lat: float
    lon: float
    alt: float | None = None
    speed: float | None = None
    track: float | None = None


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
    positions_written: int = 0
    positions_dropped: int = 0
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

    def write_positions(self, positions: list[Position], now: float | None = None) -> int:
        """Insert positions in one transaction, unless free disk space is
        under the floor. Returns how many were written."""
        if not positions:
            return 0
        now = time.time() if now is None else now
        if not self._disk_ok(now):
            self.stats.positions_dropped += len(positions)
            return 0
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO positions (ts, profile, subject, lat, lon, alt, speed, track) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(p.ts, p.profile, p.subject, p.lat, p.lon, p.alt, p.speed, p.track) for p in positions],
            )
        self.stats.positions_written += len(positions)
        return len(positions)

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
            self.conn.execute("DELETE FROM positions WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.execute(
                "DELETE FROM events WHERE id <= (SELECT max(id) FROM events) - ?", (MAX_EVENTS,)
            )
        # Each pass removes a tenth of the rows, oldest first, and the
        # positions older than the oldest row left; the used size only falls
        # once the freed pages are counted, so it's re-read.
        for _ in range(50):
            if self.used_bytes() <= self.max_bytes:
                break
            count = self.conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            positions = self.conn.execute("SELECT count(*) FROM positions").fetchone()[0]
            if count == 0 and positions == 0:
                break
            with self.conn:
                self.conn.execute("BEGIN")
                if count:
                    result.deleted_size += self.conn.execute(
                        "DELETE FROM messages WHERE id IN (SELECT id FROM messages ORDER BY id LIMIT ?)",
                        (max(1, count // 10),),
                    ).rowcount
                oldest = self.conn.execute("SELECT min(ts) FROM messages").fetchone()[0]
                if oldest is not None:
                    self.conn.execute("DELETE FROM positions WHERE ts < ?", (oldest,))
                elif positions:
                    self.conn.execute(
                        "DELETE FROM positions WHERE id IN (SELECT id FROM positions ORDER BY ts LIMIT ?)",
                        (max(1, positions // 10),),
                    )
        # The pragma frees pages as its result is stepped through, so read it all.
        self.conn.execute("PRAGMA incremental_vacuum").fetchall()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result.bytes_after = self.file_bytes()
        self.stats.last_prune = result
        return result


# --- Reading ---------------------------------------------------------------


def parse_when(value: str | float | None, now: float) -> float | None:
    """A time for the live tools and the web API: "30m", "2h", "1d" or "90s" ago; an ISO 8601
    time (UTC unless it carries an offset); or Unix seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if v[-1:] in units and v[:-1].replace(".", "", 1).isdigit():
        return now - float(v[:-1]) * units[v[-1]]
    if v.replace(".", "", 1).isdigit():
        return float(v)
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"can't read the time {value!r}: use e.g. 30m, 2h, 1d, or 2026-10-29T21:00:00Z"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()



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


def _where(
    since: float | None = None,
    until: float | None = None,
    profile: str | None = None,
    kind: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    freq_hz: int | None = None,
    freq_tolerance_hz: int = 5_000,
    profiles: tuple[str, ...] | None = None,
) -> tuple[list[str], list]:
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
    if profiles:
        where.append(f"profile IN ({', '.join('?' * len(profiles))})")
        args += list(profiles)
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
    return where, args


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
    profiles: tuple[str, ...] | None = None,
    before_id: int | None = None,
) -> list[sqlite3.Row]:
    """Rows matching every filter given, newest first. `subject` matches
    the subject or the label exactly (case-insensitive), so an ICAO address,
    MMSI, capcode, callsign, flight or ship name all work; `text` is a
    substring of the raw text, the fields JSON, the subject or the label.
    `profiles` keeps any of several profiles; `before_id` continues a
    previous page after its last row."""
    where, args = _where(since, until, profile, kind, subject, text, freq_hz, freq_tolerance_hz, profiles)
    if before_id is not None:
        where.append("(ts, id) < (SELECT ts, id FROM messages WHERE id = ?)")
        args.append(int(before_id))
    sql = "SELECT * FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    return conn.execute(sql, args).fetchall()


def histogram(
    conn: sqlite3.Connection,
    since: float,
    until: float,
    buckets: int = 96,
    **filters,
) -> dict:
    """How many rows match per time bucket and profile, for a timeline:
    {"bucket_s": width, "counts": [{profile: n, ...} per bucket]}. A row
    counts in the bucket of its start. Takes search()'s filters."""
    buckets = max(1, min(int(buckets), 2000))
    width = max((until - since) / buckets, 1e-9)
    where, args = _where(since=None, until=until, **filters)
    where.append("ts >= ?")
    args.append(since)
    sql = (
        "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, profile, count(*) FROM messages WHERE "
        + " AND ".join(where)
        + " GROUP BY b, profile"
    )
    counts: list[dict] = [{} for _ in range(buckets)]
    for b, profile, n in conn.execute(sql, [since, width, *args]):
        counts[min(max(int(b), 0), buckets - 1)][profile] = n
    return {"since": since, "until": until, "bucket_s": width, "counts": counts}


# How a stop event's detail begins, and the gap it opens.
_GAP_OF_STOP = (("paused", "paused"), ("failed", "error"), ("service stopping", "down"), ("switching", "switching"))


def coverage(conn: sqlite3.Connection, since: float, until: float) -> list[dict]:
    """What the receiver was doing over [since, until], from the events
    table: {"state", "profile", "from", "to"} in time order, where state is
    "running" (with its profile), or a gap: "paused" (lent to a device
    tool), "error" (a profile failed and is being retried), "idle"
    (stopped on request) or "down" (the service wasn't running). Switches
    (a stop straight followed by the next start) leave no gap."""
    start_ev = conn.execute(
        "SELECT ts, event, profile, detail FROM events WHERE ts < ? AND event IN "
        "('start', 'stop', 'service-start', 'service-stop') ORDER BY ts DESC, id DESC LIMIT 1",
        (since,),
    ).fetchone()
    state, profile = "down", None
    if start_ev is not None:
        state, profile = _after(start_ev[1], start_ev[2], start_ev[3], "down")
    out: list[dict] = []
    t = since
    rows = conn.execute(
        "SELECT ts, event, profile, detail FROM events WHERE ts >= ? AND ts <= ? AND event IN "
        "('start', 'stop', 'service-start', 'service-stop', 'error') ORDER BY ts, id",
        (since, until),
    ).fetchall()
    for ts, event, ev_profile, detail in rows:
        new_state, new_profile = _after(event, ev_profile, detail, state)
        if event == "error" and state == "running":
            continue  # an error while running is followed by its stop event
        if (new_state, new_profile) == (state, profile):
            continue
        if ts > t:
            out.append({"state": state, "profile": profile, "from": t, "to": ts})
        t, state, profile = ts, new_state, new_profile
    if until > t:
        out.append({"state": state, "profile": profile, "from": t, "to": until})
    # A switch's stop and start are milliseconds apart: no gap to show.
    merged: list[dict] = []
    for seg in out:
        if seg["state"] == "switching":
            continue
        if merged and merged[-1]["state"] == seg["state"] and merged[-1]["profile"] == seg["profile"]:
            merged[-1]["to"] = seg["to"]
        else:
            merged.append(seg)
    return merged


def _after(event: str, profile: str | None, detail: str | None, state: str) -> tuple[str, str | None]:
    """The state an event leaves the receiver in."""
    if event == "start":
        return "running", profile
    if event == "stop":
        d = (detail or "").lower()
        for prefix, gap in _GAP_OF_STOP:
            if d.startswith(prefix):
                return gap, None
        return "idle", None
    if event == "service-stop":
        return "down", None
    if event == "service-start":
        return "idle", None
    if event == "error":
        return "error", None
    return state, None


def positions(
    conn: sqlite3.Connection,
    since: float | None = None,
    until: float | None = None,
    profile: str | None = None,
    subject: str | None = None,
    limit: int = 10_000,
    step: float = 0,
) -> list[sqlite3.Row]:
    """Positions in [since, until], oldest first: tracks for a map.

    With `step` (seconds), at most one fix per target in each `step`-long
    slice of time, the last one logged in it: the same tracks, thinner, for
    ranges too long to send every fix.
    """
    where, args = _position_filter(since, until, profile, subject)
    cond = " WHERE " + " AND ".join(where) if where else ""
    if step and step > 0:
        sql = (
            "SELECT p.* FROM positions p JOIN (SELECT max(id) AS id FROM positions"
            + cond
            # The slice first: rows arrive in time order, so the grouping
            # B-tree is appended to rather than shuffled (a third faster).
            + " GROUP BY CAST(ts / ? AS INTEGER), subject, profile) k ON p.id = k.id ORDER BY p.ts, p.id LIMIT ?"
        )
        args.append(float(step))
    else:
        sql = "SELECT * FROM positions" + cond + " ORDER BY ts, id LIMIT ?"
    args.append(max(1, int(limit)))
    return conn.execute(sql, args).fetchall()


def count_positions(
    conn: sqlite3.Connection,
    since: float | None = None,
    until: float | None = None,
    profile: str | None = None,
    subject: str | None = None,
) -> int:
    """How many positions `positions()` would return without a limit or step."""
    where, args = _position_filter(since, until, profile, subject)
    cond = " WHERE " + " AND ".join(where) if where else ""
    return conn.execute("SELECT count(*) FROM positions" + cond, args).fetchone()[0]


def _position_filter(since, until, profile, subject) -> tuple[list[str], list]:
    where, args = [], []
    if since is not None:
        where.append("ts >= ?")
        args.append(since)
    if until is not None:
        where.append("ts <= ?")
        args.append(until)
    if profile:
        where.append("profile = ?")
        args.append(profile)
    if subject:
        where.append("subject = ? COLLATE NOCASE")
        args.append(normalise_subject(subject))
    return where, args


def events(
    conn: sqlite3.Connection, since: float | None = None, limit: int = 20, until: float | None = None
) -> list[sqlite3.Row]:
    """Events newest first, optionally only those in [since, until]."""
    where, args = [], []
    if since is not None:
        where.append("ts >= ?")
        args.append(since)
    if until is not None:
        where.append("ts <= ?")
        args.append(until)
    sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    return conn.execute(sql, args).fetchall()


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
            # Only numbers are summed or compared; a value of another type
            # (a decoder's odd output) is kept as the latest instead.
            prev = s["fields"].get(k)
            numeric = isinstance(v, (int, float)) and not isinstance(v, bool)
            prev_numeric = isinstance(prev, (int, float)) and not isinstance(prev, bool)
            if k in ("messages", "positions"):
                s["fields"][k] = (prev if prev_numeric else 0) + (v if numeric else 0)
            elif k in ("altitude_max", "altitude_min") and numeric and prev_numeric:
                s["fields"][k] = max(prev, v) if k == "altitude_max" else min(prev, v)
            else:
                s["fields"][k] = v
    out = sorted(by_subject.values(), key=lambda s: -s["last"])
    return out[: max(1, int(limit))]
