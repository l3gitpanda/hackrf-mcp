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
# What the split station's worker appends to the events it imports from the
# agent's machine, whose own live service decodes while no worker is around
# (live_import.py): "<detail> (fallback on the agent's machine)".
FALLBACK_NOTE = "fallback on the agent's machine"
_COVERAGE_EVENTS = ("start", "stop", "service-start", "service-stop", "error")


def is_fallback(detail: str | None) -> bool:
    """Whether an event came from the agent's machine (its fallback)."""
    return detail is not None and (detail == FALLBACK_NOTE or detail.endswith(f" ({FALLBACK_NOTE})"))


def coverage(conn: sqlite3.Connection, since: float, until: float) -> list[dict]:
    """What the receiver was doing over [since, until], from the events
    table: {"state", "profile", "from", "to"} in time order, where state is
    "running" (with its profile), or a gap: "paused" (lent to a device
    tool), "error" (a profile failed and is being retried), "idle"
    (stopped on request) or "down" (the service wasn't running). Switches
    (a stop straight followed by the next start) leave no gap.

    On a split station the agent's machine decodes by itself while no
    worker is around, and the worker imports its events (marked
    FALLBACK_NOTE). Those are followed on a lane of their own: the station
    is running when either lane is (the worker's first), and a segment the
    fallback covered says so with "fallback": True. Otherwise the fallback's
    own pauses (it is paused whenever the worker has the HackRF), stamped
    a moment after the worker's start, would read as the worker pausing."""
    fb_where = "(detail = ? OR detail LIKE ?)"
    fb_args = (FALLBACK_NOTE, f"% ({FALLBACK_NOTE})")
    events = "event IN (" + ",".join("?" * len(_COVERAGE_EVENTS)) + ")"
    lanes = {}
    for lane, cond in (("worker", f"NOT {fb_where} OR detail IS NULL"), ("fallback", fb_where)):
        ev = conn.execute(
            f"SELECT ts, event, profile, detail FROM events WHERE ts < ? AND {events} AND ({cond}) "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (since, *_COVERAGE_EVENTS, *fb_args),
        ).fetchone()
        lanes[lane] = _after(ev[1], ev[2], ev[3], "down") if ev is not None else ("down", None)
    rows = conn.execute(
        f"SELECT ts, event, profile, detail FROM events WHERE ts >= ? AND ts <= ? AND {events} ORDER BY ts, id",
        (since, until, *_COVERAGE_EVENTS),
    ).fetchall()

    def combined() -> tuple[str, str | None, bool]:
        w_state, w_profile = lanes["worker"]
        f_state, f_profile = lanes["fallback"]
        if w_state == "running":
            return "running", w_profile, False
        if f_state == "running":
            return "running", f_profile, True
        if f_state == "switching":
            return "switching", None, True
        return w_state, w_profile, False

    out: list[dict] = []
    t = since
    state = combined()
    for ts, event, ev_profile, detail in rows:
        lane = "fallback" if is_fallback(detail) else "worker"
        cur_state = lanes[lane][0]
        if event == "error" and cur_state == "running":
            continue  # an error while running is followed by its stop event
        lanes[lane] = _after(event, ev_profile, detail, cur_state)
        new = combined()
        if new == state:
            continue
        if ts > t:
            out.append(_segment(state, t, ts))
        t, state = ts, new
    if until > t:
        out.append(_segment(state, t, until))
    # A switch's stop and start are milliseconds apart: no gap to show.
    merged: list[dict] = []
    for seg in out:
        if seg["state"] == "switching":
            continue
        last = merged[-1] if merged else None
        if last and (last["state"], last["profile"], last.get("fallback")) == (seg["state"], seg["profile"], seg.get("fallback")):
            last["to"] = seg["to"]
        else:
            merged.append(seg)
    return merged


def _segment(state: tuple[str, str | None, bool], a: float, b: float) -> dict:
    s, profile, fallback = state
    seg = {"state": s, "profile": profile if s == "running" else None, "from": a, "to": b}
    if fallback and s == "running":
        seg["fallback"] = True
    return seg


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


# How many decoded messages a row stands for: ADS-B and AIS rows are
# per-minute summaries carrying a `messages` count; any other row is one.
MESSAGES_SQL = (
    "CASE WHEN json_valid(fields) THEN coalesce(CAST(json_extract(fields, '$.messages') AS INTEGER), 1) ELSE 1 END"
)
# A row's identity, as summarise() keys it.
SUBJECT_SQL = "coalesce(nullif(subject, ''), nullif(label, ''), '?')"
# The fields summarise() folds over a subject's rows rather than keeping the latest.
_FOLDED = {"messages": "sum", "positions": "sum", "altitude_max": "max", "altitude_min": "min"}


def summaries(
    conn: sqlite3.Connection,
    since: float,
    until: float,
    profiles: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
    buckets: int = 96,
    top: int = 20,
) -> dict:
    """What each profile heard over [since, until], for the Summaries screen,
    in one pass over the log so the numbers agree with each other:

      rate      decoded messages per time bucket, a row counted in the
                bucket of its start (clamped to the range)
      rows / messages   their totals (messages = the sum of the buckets)
      subjects  how many different subjects (aircraft, ships, capcodes ...)
                among the profile's summary kinds, and `by_kind` per kind
      top       the busiest of them by messages: rows, messages, first and
                last time, strongest level, kinds, channels, labels and the
                latest value of each of the given fields (counts summed,
                altitude extremes kept)

    `profiles` maps a profile to (summary_kinds, fields). A row belongs to
    the range as in summarise(): it ends at or after `since` and starts at
    or before `until`. The pass reads each row's JSON once; the details are
    read for the top subjects only, through the subject and label indexes."""
    buckets = max(1, min(int(buckets), 2000))
    width = max((until - since) / buckets, 1e-9)
    window = "ts >= ? AND coalesce(ts_end, ts) >= ? AND ts <= ?"
    wargs = [since - ROW_SPAN_S, since, until]
    out: dict[str, dict] = {
        name: {"rows": 0, "messages": 0, "rate": [0] * buckets, "subjects": 0, "by_kind": {}, "top": []}
        for name in profiles
    }
    subjects: dict[str, dict[str, list]] = {name: {} for name in profiles}
    by_kind: dict[str, dict[str, set]] = {name: {} for name in profiles}
    sql = (
        f"SELECT profile, kind, {SUBJECT_SQL} AS k, CAST((max(ts, ?) - ?) / ? AS INTEGER) AS b, "
        f"count(*), sum({MESSAGES_SQL}), min(ts), max(coalesce(ts_end, ts)), max(level_db) "
        f"FROM messages WHERE {window} GROUP BY profile, k, kind, b"
    )
    for profile, kind, key, b, rows, msgs, first, last, level in conn.execute(sql, [since, since, width, *wargs]):
        p = out.get(profile)
        if p is None:
            continue
        p["rate"][min(max(int(b), 0), buckets - 1)] += msgs
        p["rows"] += rows
        p["messages"] += msgs
        kinds = profiles[profile][0]
        if kinds and kind not in kinds:
            continue
        by_kind[profile].setdefault(kind, set()).add(key)
        e = subjects[profile].get(key)
        if e is None:
            subjects[profile][key] = [rows, msgs, first, last, level, {kind}]
        else:
            e[0] += rows
            e[1] += msgs
            e[2] = min(e[2], first)
            e[3] = max(e[3], last)
            if level is not None and (e[4] is None or level > e[4]):
                e[4] = level
            e[5].add(kind)
    for name, (kinds, fields) in profiles.items():
        p = out[name]
        p["subjects"] = len(subjects[name])
        p["by_kind"] = {kind: len(keys) for kind, keys in sorted(by_kind[name].items())}
        busiest = sorted(subjects[name].items(), key=lambda kv: (-kv[1][1], -kv[1][3]))[: max(1, int(top))]
        for key, (rows, msgs, first, last, level, seen) in busiest:
            entry = {
                "subject": key,
                "rows": rows,
                "messages": msgs,
                "first": first,
                "last": last,
                "max_level_db": level,
                "kinds": sorted(seen),
                "freqs": [],
                "labels": [],
                "fields": {},
            }
            _subject_details(conn, name, entry, fields, window, wargs, kinds)
            p["top"].append(entry)
    return {"since": since, "until": until, "bucket_s": width, "profiles": out}


def _subject_details(conn, profile, entry, fields, window, wargs, kinds) -> None:
    """Channels, labels, the folded fields (summed counts, altitude extremes)
    and the latest non-empty value of the other `fields` for one subject in
    the window, read through the subject index (or the label index, for a
    subject known only by its label)."""
    key = entry["subject"]
    kind_sql = f" AND kind IN ({', '.join('?' * len(kinds))})" if kinds else ""
    # Field names come from the protocol modules; only plain names go into SQL.
    folded = [(f, _FOLDED[f]) for f in fields if f in _FOLDED and f != "messages" and f.isidentifier()]
    fold_sql = "".join(f", {fn}(CASE WHEN json_valid(fields) THEN json_extract(fields, '$.{f}') END)" for f, fn in folded)
    for col in ("subject", "label"):
        # The column's own index finds the rows; the key expression keeps
        # only those summaries() counted under this key.
        where = (
            f"{col} = ? COLLATE NOCASE AND profile = ? AND {window}{kind_sql} AND {SUBJECT_SQL} = ?"
        )
        args = [key, profile, *wargs, *kinds, key]
        agg = conn.execute(f"SELECT count(*), group_concat(DISTINCT freq_hz){fold_sql} FROM messages WHERE {where}", args).fetchone()
        if agg[0]:
            break
    else:
        return
    agg = tuple(agg)
    entry["freqs"] = sorted(int(f) for f in (agg[1] or "").split(",") if f)
    entry["fields"] = {f: v for (f, _), v in zip(folded, agg[2:]) if v is not None}
    missing = [f for f in fields if f not in _FOLDED]
    for r in conn.execute(f"SELECT label, fields FROM messages WHERE {where} ORDER BY ts DESC LIMIT 60", args):
        if r["label"] and r["label"] not in entry["labels"]:
            entry["labels"].append(r["label"])
        if not missing:
            continue
        try:
            values = json.loads(r["fields"])
        except ValueError:
            continue
        for f in list(missing):
            v = values.get(f)
            if v not in (None, "", [], {}):
                entry["fields"][f] = v
                missing.remove(f)


# The finest heat-map cell, in degrees (about 220 m of latitude).
GRID_MIN_DEG = 0.002


def position_grid(
    conn: sqlite3.Connection,
    since: float,
    until: float,
    profile: str | None = None,
    cell_deg: float = 0.05,
    bbox: tuple[float, float, float, float] | None = None,
    max_cells: int = 20_000,
) -> dict:
    """Positions over [since, until] counted on a grid of `cell_deg`-degree
    cells, for a heat map: per profile, [ix, iy, fixes, targets] per cell,
    where the cell spans lon ix*cell-180 .. +cell and lat iy*cell-90 .. +cell,
    fixes is the positions logged in it and targets how many different
    subjects they came from. `bbox` (west, south, east, north) keeps only
    that area. With more than `max_cells` cells the grid is coarsened
    (doubling the cell) until it fits; the cell used is returned."""
    cell = max(GRID_MIN_DEG, float(cell_deg))
    where, args = _position_filter(since, until, profile, None)
    if bbox is not None:
        w, s, e, n = bbox
        # A box across the antimeridian has its west edge east of its east edge.
        where += ["lon >= ? AND lon <= ?" if w <= e else "(lon >= ? OR lon <= ?)", "lat >= ?", "lat <= ?"]
        args += [w, e, s, n]
    cond = " AND ".join(where)
    fixes, targets = conn.execute(
        f"SELECT count(*), count(DISTINCT profile || '|' || subject) FROM positions WHERE {cond}", args
    ).fetchone()
    while True:
        sql = (
            "SELECT profile, CAST((lon + 180) / ? AS INTEGER) AS x, CAST((lat + 90) / ? AS INTEGER) AS y, "
            f"count(*), count(DISTINCT subject) FROM positions WHERE {cond} GROUP BY profile, x, y LIMIT ?"
        )
        rows = conn.execute(sql, [cell, cell, *args, max_cells + 1]).fetchall()
        if len(rows) <= max_cells or cell >= 10:
            break
        cell *= 2
    cells: dict[str, list] = {}
    for p, x, y, n, t in rows[:max_cells]:
        cells.setdefault(p, []).append([x, y, n, t])
    return {"cell_deg": cell, "fixes": fixes, "targets": targets, "cells": cells, "truncated": len(rows) > max_cells}


# --- How far the receiver hears ------------------------------------------------------

EARTH_KM = 6371.0088
# The range outline's altitude bands, in feet: (name, from, below).
RANGE_BANDS = (("low", None, 10_000), ("mid", 10_000, 25_000), ("high", 25_000, None))
# The receiver's height is not known, so the horizon test assumes a
# generous one, and allows 10% more for refraction beyond the usual.
HORIZON_RECEIVER_M = 300.0
HORIZON_SLACK = 1.1


def radio_horizon_km(alt_ft, receiver_m: float = HORIZON_RECEIVER_M):
    """How far a radio path reaches over a smooth earth (4/3 refraction)
    from a receiver `receiver_m` up to something at `alt_ft` feet. Works on
    numbers and numpy arrays."""
    import numpy as np

    return 4.12 * (np.sqrt(np.maximum(alt_ft, 0.0) * 0.3048) + np.sqrt(receiver_m))


def distance_bearing(lat0: float, lon0: float, lat, lon):
    """Great-circle distance (km) and initial bearing (degrees, 0 = north)
    from (lat0, lon0) to each (lat, lon): numpy arrays in, arrays out."""
    import numpy as np

    p0, l0 = np.radians(lat0), np.radians(lon0)
    p, dl = np.radians(lat), np.radians(lon) - l0
    a = np.sin((p - p0) / 2) ** 2 + np.cos(p0) * np.cos(p) * np.sin(dl / 2) ** 2
    km = 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    y = np.sin(dl) * np.cos(p)
    x = np.cos(p0) * np.sin(p) - np.sin(p0) * np.cos(p) * np.cos(dl)
    return km, (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0


def _beyond_horizon(km, alt):
    """True where a fix is farther than any radio path to its altitude
    allows: a decoding error. Fixes with no altitude are never judged."""
    import numpy as np

    known = np.isfinite(alt)
    limit = np.where(known, HORIZON_SLACK * radio_horizon_km(np.where(known, alt, 0.0)), np.inf)
    return km > limit


def receiver_range(
    conn: sqlite3.Connection,
    receiver: tuple[float, float],
    since: float,
    until: float,
    profile: str = "adsb",
    sector_deg: float = 5.0,
    bin_km: float = 25.0,
) -> dict:
    """How far the receiver at `receiver` (lat, lon) heard `profile` over
    [since, until], from the positions table: the farthest fix in each
    `sector_deg` sector of bearing (sector i spans i*sector_deg up to the
    next), for all fixes and, where fixes have an altitude, per altitude
    band (RANGE_BANDS); None where nothing was heard. Fixes farther than
    the radio horizon for their altitude are decoding errors and are left
    out (counted in beyond_horizon). Also the farthest fix of all, the
    median of the sectors' farthest, and the signal level against distance
    from the per-minute rows (their peak level and last position), as
    10th/50th/90th percentiles per `bin_km` of distance."""
    import numpy as np

    n = max(1, int(round(360.0 / sector_deg)))
    step = 360.0 / n
    where, args = _position_filter(since, until, profile, None)
    rows = conn.execute(f"SELECT subject, ts, lat, lon, alt FROM positions WHERE {' AND '.join(where)}", args).fetchall()
    out: dict = {
        "sector_deg": step,
        "fixes": 0,
        "targets": 0,
        "beyond_horizon": 0,
        "bands": [{"name": name, "from_ft": lo, "below_ft": hi} for name, lo, hi in RANGE_BANDS],
        "sectors": {},
        "farthest": None,
        "median_km": None,
        "levels": {"bin_km": bin_km, "bins": [], "rows": 0},
    }
    if rows:
        lat = np.array([r[2] for r in rows], dtype=float)
        lon = np.array([r[3] for r in rows], dtype=float)
        alt = np.array([np.nan if r[4] is None else r[4] for r in rows], dtype=float)
        km, brg = distance_bearing(receiver[0], receiver[1], lat, lon)
        bad = _beyond_horizon(km, alt)
        good = ~bad
        out["beyond_horizon"] = int(bad.sum())
        out["fixes"] = int(good.sum())
        out["targets"] = len({rows[i][0] for i in np.flatnonzero(good)})
        sector = (brg // step).astype(int) % n
        masks = {"all": good}
        if np.isfinite(alt[good]).any():
            for name, lo, hi in RANGE_BANDS:
                m = good & np.isfinite(alt)
                if lo is not None:
                    m &= alt >= lo
                if hi is not None:
                    m &= alt < hi
                masks[name] = m
        else:
            out["bands"] = []
        for name, m in masks.items():
            far = np.full(n, -1.0)
            np.maximum.at(far, sector[m], km[m])
            out["sectors"][name] = [None if v < 0 else round(float(v), 1) for v in far]
        heard = [v for v in out["sectors"]["all"] if v is not None]
        if heard:
            out["median_km"] = round(float(np.median(heard)), 1)
            i = int(np.flatnonzero(good)[np.argmax(km[good])])
            subject, ts = rows[i][0], rows[i][1]
            label = conn.execute(
                "SELECT label FROM messages WHERE profile = ? AND subject = ? AND label IS NOT NULL AND label != '' "
                "AND ts >= ? AND ts <= ? ORDER BY abs(ts - ?) LIMIT 1",
                (profile, subject, ts - 3600, ts + 3600, ts),
            ).fetchone()
            out["farthest"] = {
                "subject": subject,
                "label": label[0] if label else None,
                "ts": ts,
                "km": round(float(km[i]), 1),
                "bearing": round(float(brg[i]), 1),
                "alt": None if not np.isfinite(alt[i]) else float(alt[i]),
            }
    out["levels"] = _levels_by_distance(conn, receiver, since, until, profile, bin_km)
    return out


def _levels_by_distance(conn, receiver, since, until, profile, bin_km) -> dict:
    import numpy as np

    rows = conn.execute(
        "SELECT level_db, json_extract(fields, '$.lat'), json_extract(fields, '$.lon'), json_extract(fields, '$.altitude') "
        "FROM messages WHERE profile = ? AND ts >= ? AND ts <= ? AND level_db IS NOT NULL AND json_valid(fields) "
        "AND json_extract(fields, '$.lat') IS NOT NULL AND json_extract(fields, '$.lon') IS NOT NULL",
        (profile, since, until),
    ).fetchall()
    out = {"bin_km": bin_km, "bins": [], "rows": 0}
    if not rows:
        return out
    level = np.array([r[0] for r in rows], dtype=float)
    lat = np.array([r[1] for r in rows], dtype=float)
    lon = np.array([r[2] for r in rows], dtype=float)
    alt = np.array([r[3] if isinstance(r[3], (int, float)) else np.nan for r in rows], dtype=float)
    km, _ = distance_bearing(receiver[0], receiver[1], lat, lon)
    keep = ~_beyond_horizon(km, alt)
    km, level = km[keep], level[keep]
    out["rows"] = int(km.size)
    which = (km // bin_km).astype(int)
    for b in np.unique(which):
        lv = level[which == b]
        p10, p50, p90 = np.percentile(lv, [10, 50, 90])
        out["bins"].append([round(float(b * bin_km), 1), int(lv.size), round(float(p10), 1), round(float(p50), 1), round(float(p90), 1)])
    return out
