"""Alerts: rules over the live log, the hits they make, and a webhook.

hackrf-web keeps them in a SQLite database of its own, `alerts.db` in its
state directory; the live log (live.db) is only read. A background thread
reads the rows the live service has logged since it last looked (by row
id, so rows logged while hackrf-web was down are checked when it comes
back) and records a hit for every row a rule matches, at most one per rule
and subject every `repeat_s` seconds. Rows are checked as they are logged:
ADS-B and AIS log one row per aircraft or ship a minute, so their hits come
up to a minute after the signal.

A rule is a set of conditions, all of which must hold, over a row's
subject, label, kind, frequency, level and the profile's summary and table
fields (from its protocol module): "is any of" (case-insensitive), "equals",
"matches" (a wildcard pattern: * and ?), "is greater than", "is less than".
Pager text is a field to match on, and goes into a hit and the webhook
like any other matched field, only when the operator allows pager content
(HACKRF_MCP_PAGER_CONTENT); with it off, a hit records the rule instead. A
row's raw text is never a field (its fields hold the same).

Some fields aren't in the row and are worked out as it is checked (see
Derived):
  aircraft_type and the other aircraft_* fields, from the aircraft
    database (web/aircraftdb.py), for profiles whose subjects are ICAO
    addresses (subject_type "icao24"): the class (military, law
    enforcement, government, commercial, private, unknown), registration,
    make and model, kind, registrant type, owner, and plane-alert-db's
    operator, category and tags. They are the database's as it is when
    the row is checked.
  distance_km, from the receiver (HACKRF_LIVE_RECEIVER) to the row's
    position, for profiles with a map view. A hit shows the distance;
    the webhook gets the condition instead ("distance_km is less than
    10"), because distances to a known aircraft at known times would let
    anyone with a public flight archive work out where the receiver is.

A hit is shown in the UI until acknowledged and, if the rule asks and the
operator has set one up (HACKRF_WEB_ALERT_WEBHOOK_FILE), posted to a
webhook: JSON, Discord or ntfy. Hits for rows logged more than
WEBHOOK_MAX_AGE_S ago (a catch-up after an outage) are recorded but not
posted.
"""

from __future__ import annotations

import fnmatch
import json
import math
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hackrf_mcp import live_log, live_profiles
from hackrf_mcp.web import aircraftdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    profiles TEXT NOT NULL,
    conditions TEXT NOT NULL,
    webhook INTEGER NOT NULL DEFAULT 0,
    repeat_s INTEGER NOT NULL DEFAULT 600,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hits (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    recorded REAL NOT NULL,
    rule_id INTEGER,
    rule_name TEXT NOT NULL,
    profile TEXT NOT NULL,
    kind TEXT,
    subject TEXT,
    label TEXT,
    freq_hz INTEGER,
    detail TEXT NOT NULL,
    row_id INTEGER,
    acked REAL,
    webhook TEXT
);
CREATE INDEX IF NOT EXISTS hits_acked ON hits (acked, id);
CREATE INDEX IF NOT EXISTS hits_rule ON hits (rule_id, ts);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# The row columns a rule may test, besides the profile's own fields.
BASE_FIELDS = ("subject", "label", "kind", "freq_hz", "level_db")
NUMERIC_BASE = ("freq_hz", "level_db")
OPS = {
    "any_of": "is any of",
    "equals": "equals",
    "matches": "matches",
    "gt": "is greater than",
    "lt": "is less than",
}
MAX_RULES = 200
MAX_CONDITIONS = 8
MAX_VALUES = 50
MAX_HITS = 20_000
# A webhook isn't sent for a row logged longer ago than this.
WEBHOOK_MAX_AGE_S = 1800
# The UI's list of subjects with an unacknowledged hit, at most this long.
ACTIVE_MAX = 50
POLL_S = 2.0
BATCH = 2000

# The derived aircraft fields, and the key of aircraftdb.describe() each is.
AIRCRAFT_FIELDS = {
    "aircraft_type": "class",
    "aircraft_registration": "registration",
    "aircraft_manufacturer": "manufacturer",
    "aircraft_model": "model",
    "aircraft_kind": "kind",
    "aircraft_registrant": "registrant",
    "aircraft_owner": "owner",
    "aircraft_operator": "operator",
    "aircraft_category": "category",
    "aircraft_tags": "tags",
}
DISTANCE = "distance_km"
DERIVED_FIELDS = (*AIRCRAFT_FIELDS, DISTANCE)
# Derived fields whose values the webhook doesn't carry (see the module).
NOT_POSTED = (DISTANCE,)
CLASS_NAMES = tuple(c[0] for c in aircraftdb.CLASSES)


def position_fields(profile: live_profiles.Profile) -> tuple[str, str] | None:
    """The fields holding a row's latitude and longitude, from the profile's
    map view; None for a profile without one."""
    for v in profile.ui_views():
        if v.name == "map":
            roles = dict(v.roles)
            if "lat" in roles and "lon" in roles:
                return roles["lat"], roles["lon"]
    return None


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


class Derived:
    """The fields a rule can test that aren't in the row (see the module):
    the aircraft fields when there is an aircraft database (`aircraft`, an
    AircraftDB; it classifies by address and callsign even before its file
    is built), and distance_km when the receiver's position is known
    (`receiver`, (lat, lon)). The position is used here and nowhere else:
    no field, hit or message holds it."""

    def __init__(self, aircraft: aircraftdb.AircraftDB | None = None, receiver: tuple[float, float] | None = None):
        self.aircraft = aircraft
        self.receiver = (float(receiver[0]), float(receiver[1])) if receiver else None

    def fields(self, profile: live_profiles.Profile) -> list[str]:
        out = []
        if self.aircraft is not None and profile.subject_type == "icao24":
            out += list(AIRCRAFT_FIELDS)
        if self.receiver is not None and position_fields(profile):
            out.append(DISTANCE)
        return out

    def choices(self) -> dict[str, list[dict]]:
        """The values a field takes, for the rule editor: the aircraft
        classes, in the registry's order."""
        if self.aircraft is None:
            return {}
        return {
            "aircraft_type": [{"value": n, "label": label, "color": c} for n, label, _short, c, _notable in aircraftdb.CLASSES]
        }

    def add(self, items: list[tuple[dict, dict]], wanted: set[str], cache: dict | None = None) -> None:
        """Adds the `wanted` derived fields to each (row, fields) pair's
        fields, in place, with one database lookup for all of them. A
        profile's own field of the same name is left as it is. `cache`
        keeps lookups across calls (a preview's chunks)."""
        want_aircraft = self.aircraft is not None and any(f in AIRCRAFT_FIELDS for f in wanted)
        want_distance = self.receiver is not None and DISTANCE in wanted
        if not (want_aircraft or want_distance):
            return
        cache = {} if cache is None else cache
        if want_aircraft:
            asked = {}
            for row, fields in items:
                key = _aircraft_key(row, fields)
                if key is not None and key not in cache:
                    asked[key] = None
            if asked:
                found = self.aircraft.lookup(list(asked))
                for icao, callsign in asked:
                    cache[(icao, callsign)] = found.get(icao)
        for row, fields in items:
            p = live_profiles.PROFILES.get(row.get("profile"))
            if p is None:
                continue
            if want_aircraft and p.subject_type == "icao24":
                info = cache.get(_aircraft_key(row, fields))
                if info:
                    for name, key in AIRCRAFT_FIELDS.items():
                        if name in wanted and info.get(key) not in (None, "", []):
                            fields.setdefault(name, info[key])
            if want_distance and (pos := position_fields(p)):
                try:
                    here = (float(fields[pos[0]]), float(fields[pos[1]]))
                except (KeyError, TypeError, ValueError):
                    continue
                if -90 <= here[0] <= 90 and -180 <= here[1] <= 180:
                    fields.setdefault(DISTANCE, round(distance_km(self.receiver, here), 1))

    def aircraft_of(self, hits: list[dict]) -> dict[str, dict]:
        """{icao: aircraftdb.describe(...)} for hits on aircraft, for the
        webhook to say what each one is."""
        if self.aircraft is None:
            return {}
        wanted = []
        for h in hits:
            p = live_profiles.PROFILES.get(h.get("profile"))
            if p is not None and p.subject_type == "icao24" and h.get("subject"):
                wanted.append((h["subject"], h.get("label")))
        return self.aircraft.lookup(wanted) if wanted else {}


def _aircraft_key(row: dict, fields: dict) -> tuple[str, str | None] | None:
    p = live_profiles.PROFILES.get(row.get("profile"))
    if p is None or p.subject_type != "icao24":
        return None
    icao = aircraftdb.normalise_icao(str(row.get("subject") or ""))
    if icao is None:
        return None
    callsign = fields.get("callsign") or row.get("label")
    return icao, (str(callsign).strip().upper() or None) if callsign else None


NO_DERIVED = Derived()


def allowed_fields(profile: live_profiles.Profile, pager_content: bool, derived: Derived = NO_DERIVED) -> list[str]:
    """What a rule on this profile may test: the row's columns and the
    profile's summary and table fields, without positions; never raw text,
    and pager text only when the operator allows pager content; then the
    derived fields the profile has."""
    out = list(BASE_FIELDS)
    fields = list(profile.summary_fields)
    for v in profile.ui_views():
        if v.name == "table":
            fields += list(v.fields)
    for f in fields:
        if f in ("lat", "lon", "raw") or f in out:
            continue
        out.append(f)
    if profile.redact is not None and pager_content and "message" not in out:
        out.append("message")
    out += [f for f in derived.fields(profile) if f not in out]
    return out


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    values: tuple[str, ...]

    def to_json(self) -> dict:
        return {"field": self.field, "op": self.op, "values": list(self.values)}

    def text(self) -> str:
        return f"{self.field} {OPS[self.op]} {', '.join(self.values)}"


@dataclass(frozen=True)
class Rule:
    id: int | None
    name: str
    enabled: bool
    profiles: tuple[str, ...]
    conditions: tuple[Condition, ...]
    webhook: bool
    repeat_s: int

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "profiles": list(self.profiles),
            "conditions": [c.to_json() for c in self.conditions],
            "webhook": self.webhook,
            "repeat_s": self.repeat_s,
            "text": " and ".join(c.text() for c in self.conditions),
        }


def _class_values(op: str, values: tuple[str, ...]) -> tuple[str, ...]:
    """aircraft_type's values as class names: a class's label ("Law
    enforcement") is taken for its name; anything else is refused, except
    in a pattern."""
    if op == "matches":
        return values
    by_text = {}
    for n, label, short, _c, _notable in aircraftdb.CLASSES:
        for t in (n, label, short, n.replace("_", " ")):
            by_text[t.casefold()] = n
    out = []
    for v in values:
        n = by_text.get(v.casefold())
        if n is None:
            raise ValueError(f"aircraft_type is one of {', '.join(CLASS_NAMES)}, not {v!r}")
        out.append(n)
    return tuple(dict.fromkeys(out))


def parse_rule(body: dict, pager_content: bool, rule_id: int | None = None, derived: Derived = NO_DERIVED) -> Rule:
    """A rule from the UI's JSON, checked: known profiles, fields those
    profiles allow, known operators, numbers where a comparison needs one."""
    if not isinstance(body, dict):
        raise ValueError("send a JSON object")
    live_profiles.load()
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("a rule needs a name of at most 80 characters")
    profiles = tuple(dict.fromkeys(str(p) for p in body.get("profiles") or []))
    for p in profiles:
        if p not in live_profiles.PROFILES:
            raise ValueError(f"unknown profile {p!r}")
    chosen = [live_profiles.PROFILES[p] for p in profiles] or list(live_profiles.PROFILES.values())
    allowed = set()
    for p in chosen:
        allowed.update(allowed_fields(p, pager_content, derived))
    raw_conditions = body.get("conditions") or []
    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise ValueError("a rule needs at least one condition")
    if len(raw_conditions) > MAX_CONDITIONS:
        raise ValueError(f"at most {MAX_CONDITIONS} conditions")
    conditions = []
    for c in raw_conditions:
        if not isinstance(c, dict):
            raise ValueError("each condition is an object with field, op and values")
        field = str(c.get("field") or "")
        op = str(c.get("op") or "")
        if field not in allowed:
            raise ValueError(f"{field!r} is not a field these profiles log (or may be matched on)")
        if op not in OPS:
            raise ValueError(f"unknown operator {op!r}")
        values = c.get("values")
        if isinstance(values, str):
            values = values.replace("\n", ",").split(",")
        values = tuple(v for v in (str(x).strip() for x in values or []) if v)
        if not values:
            raise ValueError(f"the condition on {field} needs a value")
        if len(values) > MAX_VALUES or any(len(v) > 200 for v in values):
            raise ValueError(f"at most {MAX_VALUES} values of up to 200 characters")
        if op in ("equals", "gt", "lt") and len(values) != 1:
            raise ValueError(f"{OPS[op]} takes one value")
        if field == DISTANCE and op not in ("gt", "lt"):
            raise ValueError(f"{DISTANCE} takes {OPS['gt']} or {OPS['lt']}")
        if op in ("gt", "lt"):
            try:
                float(values[0])
            except ValueError as exc:
                raise ValueError(f"{field} {OPS[op]} needs a number") from exc
        if field == "aircraft_type":
            values = _class_values(op, values)
        conditions.append(Condition(field, op, values))
    try:
        repeat_s = int(body.get("repeat_s", 600))
    except (TypeError, ValueError) as exc:
        raise ValueError("repeat_s is a number of seconds") from exc
    if not 0 <= repeat_s <= 7 * 86400:
        raise ValueError("repeat_s is between 0 and a week")
    return Rule(
        rule_id,
        name,
        bool(body.get("enabled", True)),
        profiles,
        tuple(conditions),
        bool(body.get("webhook", False)),
        repeat_s,
    )


def _values_of(row: dict, fields: dict, field: str) -> list:
    v = row.get(field) if field in BASE_FIELDS else fields.get(field)
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _holds(c: Condition, values: list) -> bool:
    if not values:
        return False
    if c.op in ("gt", "lt"):
        limit = float(c.values[0])
        for v in values:
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if (x > limit) if c.op == "gt" else (x < limit):
                return True
        return False
    texts = [str(v).casefold() for v in values]
    if c.op == "matches":
        pats = [p.casefold() for p in c.values]
        return any(fnmatch.fnmatchcase(t, p) for t in texts for p in pats)
    wanted = {w.casefold() for w in c.values}
    return any(t in wanted for t in texts)


def match(
    rule: Rule, row: dict, fields: dict, pager_content: bool = False, hide: tuple[str, ...] = ()
) -> list[str] | None:
    """The matched values, as "field=value", if every condition holds for
    the row; else None. Pager text, and the fields in `hide`, are given as
    the condition itself (pager text unless the operator allows pager
    content). Derived fields must already be in `fields` (Derived.add)."""
    if rule.profiles and row.get("profile") not in rule.profiles:
        return None
    out = []
    for c in rule.conditions:
        values = _values_of(row, fields, c.field)
        if not _holds(c, values):
            return None
        if (c.field == "message" and not pager_content) or c.field in hide:
            out.append(c.text())  # the rule, not the value it matched
        else:
            out.append(f"{c.field}={', '.join(str(v) for v in values)}")
    return out


def derived_wanted(rules: list[Rule]) -> set[str]:
    """The derived fields any of these rules tests."""
    return {c.field for r in rules for c in r.conditions if c.field in DERIVED_FIELDS}


def _prefilter(rule: Rule) -> tuple[str, list]:
    """SQL that keeps every row the rule could match and drops most of the
    rest, so a preview parses few rows. match() still decides."""
    where, args = [], []
    for c in rule.conditions:
        if c.field in DERIVED_FIELDS:
            continue  # not in the row: worked out as it is checked
        if c.field in ("subject", "label", "kind") and c.op in ("any_of", "equals"):
            where.append(f"lower({c.field}) IN ({', '.join('?' * len(c.values))})")
            args += [v.casefold() for v in c.values]
        elif c.field in NUMERIC_BASE and c.op in ("gt", "lt"):
            where.append(f"{c.field} {'>' if c.op == 'gt' else '<'} ?")
            args.append(float(c.values[0]))
        elif c.field not in BASE_FIELDS and c.op in ("any_of", "equals", "matches"):
            # The value's text must appear in the fields' JSON, where plain
            # ASCII is written as it is.
            parts = []
            for v in c.values:
                lit = max(v.replace("?", "*").split("*"), key=len) if c.op == "matches" else v
                if len(lit) < 2 or not lit.isascii() or any(ch in lit for ch in '"\\'):
                    parts = []
                    break
                parts.append(lit.casefold())
            if parts:
                where.append("(" + " OR ".join("instr(lower(fields), ?) > 0" for _ in parts) + ")")
                args += parts
    return (" AND " + " AND ".join(where) if where else ""), args


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def aircraft_text(info: dict | None) -> str:
    """'Military, 05-5140, Boeing C-17A, United States Air Force' from
    aircraftdb.describe(): class, registration, type, operator or owner."""
    if not info:
        return ""
    labels = {n: label for n, label, *_ in aircraftdb.CLASSES}
    model = " ".join(x for x in (info.get("manufacturer"), info.get("model")) if x) or info.get("type")
    parts = [labels.get(info.get("class"), info.get("class")), info.get("registration"), model]
    who = info.get("operator") or info.get("owner")
    parts.append(who)
    return ", ".join(str(p) for p in parts if p)


# --- The store ------------------------------------------------------------------------


class AlertStore:
    """alerts.db: rules, hits and the watermark. One connection, behind a
    lock, shared by the engine's thread and the web handlers."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # rules

    def rules(self) -> list[Rule]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
        return [self._rule(r) for r in rows]

    def rule(self, rule_id: int) -> Rule | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
        return self._rule(r) if r else None

    @staticmethod
    def _rule(r) -> Rule:
        return Rule(
            r["id"],
            r["name"],
            bool(r["enabled"]),
            tuple(json.loads(r["profiles"])),
            tuple(Condition(c["field"], c["op"], tuple(c["values"])) for c in json.loads(r["conditions"])),
            bool(r["webhook"]),
            r["repeat_s"],
        )

    def save(self, rule: Rule) -> Rule:
        now = time.time()
        cols = (
            rule.name,
            int(rule.enabled),
            json.dumps(list(rule.profiles)),
            json.dumps([c.to_json() for c in rule.conditions]),
            int(rule.webhook),
            rule.repeat_s,
        )
        with self.lock:
            if rule.id is None:
                if self.conn.execute("SELECT count(*) FROM rules").fetchone()[0] >= MAX_RULES:
                    raise ValueError(f"at most {MAX_RULES} rules")
                cur = self.conn.execute(
                    "INSERT INTO rules (name, enabled, profiles, conditions, webhook, repeat_s, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (*cols, now, now),
                )
                rule_id = cur.lastrowid
            else:
                cur = self.conn.execute(
                    "UPDATE rules SET name = ?, enabled = ?, profiles = ?, conditions = ?, webhook = ?, repeat_s = ?, "
                    "updated = ? WHERE id = ?",
                    (*cols, now, rule.id),
                )
                if not cur.rowcount:
                    raise KeyError(rule.id)
                rule_id = rule.id
        return self.rule(rule_id)

    def delete(self, rule_id: int) -> bool:
        """Removes a rule; its hits stay, under the rule's name."""
        with self.lock, self.conn:
            self.conn.execute("BEGIN")
            n = self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount
            self.conn.execute("UPDATE hits SET rule_id = NULL WHERE rule_id = ?", (rule_id,))
        return bool(n)

    def rule_stats(self, since: float) -> dict[int, dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT rule_id, sum(ts >= ?), max(ts) FROM hits WHERE rule_id IS NOT NULL GROUP BY rule_id", (since,)
            ).fetchall()
        return {r[0]: {"hits": r[1] or 0, "last": r[2]} for r in rows}

    # hits

    def add_hits(self, hits: list[dict]) -> list[dict]:
        if not hits:
            return []
        with self.lock, self.conn:
            self.conn.execute("BEGIN")
            for h in hits:
                cur = self.conn.execute(
                    "INSERT INTO hits (ts, recorded, rule_id, rule_name, profile, kind, subject, label, freq_hz, detail, "
                    "row_id, webhook) VALUES (:ts, :recorded, :rule_id, :rule_name, :profile, :kind, :subject, :label, "
                    ":freq_hz, :detail, :row_id, :webhook)",
                    h,
                )
                h["id"] = cur.lastrowid
            # Bounded: the oldest acknowledged go first, then the oldest.
            n = self.conn.execute("SELECT count(*) FROM hits").fetchone()[0]
            if n > MAX_HITS:
                self.conn.execute(
                    "DELETE FROM hits WHERE id IN (SELECT id FROM hits ORDER BY acked IS NULL, id LIMIT ?)", (n - MAX_HITS,)
                )
        return hits

    def hits(self, limit: int = 100, before_id: int | None = None, unacked: bool = False) -> list[dict]:
        where, args = [], []
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        if unacked:
            where.append("acked IS NULL")
        sql = "SELECT * FROM hits" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, [*args, max(1, int(limit))])]

    def ack(self, ids: list[int] | None) -> int:
        """Acknowledges the given hits, or all of them (ids None)."""
        now = time.time()
        with self.lock:
            if ids is None:
                return self.conn.execute("UPDATE hits SET acked = ? WHERE acked IS NULL", (now,)).rowcount
            n = 0
            for i in ids:
                n += self.conn.execute("UPDATE hits SET acked = ? WHERE id = ? AND acked IS NULL", (now, int(i))).rowcount
            return n

    def set_webhook_status(self, hit_id: int, status: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE hits SET webhook = ? WHERE id = ?", (status[:200], hit_id))

    def last_hits(self, since: float) -> dict[tuple, float]:
        """(rule, profile, subject) -> the time of its last hit, for repeat limits."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT rule_id, profile, subject, max(ts) FROM hits WHERE ts >= ? AND rule_id IS NOT NULL "
                "GROUP BY rule_id, profile, subject",
                (since,),
            ).fetchall()
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    def summary(self) -> dict:
        with self.lock:
            unacked = self.conn.execute("SELECT count(*) FROM hits WHERE acked IS NULL").fetchone()[0]
            latest = self.conn.execute("SELECT * FROM hits WHERE acked IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            active = self.conn.execute(
                "SELECT profile, subject, max(id) AS id FROM hits WHERE acked IS NULL AND subject IS NOT NULL "
                "GROUP BY profile, subject ORDER BY id DESC LIMIT ?",
                (ACTIVE_MAX,),
            ).fetchall()
        return {
            "unacked": unacked,
            "latest": dict(latest) if latest else None,
            "active": [{"profile": r["profile"], "subject": r["subject"]} for r in active],
        }

    # the watermark

    def get_state(self, key: str) -> str | None:
        with self.lock:
            r = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def set_state(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))


# --- The webhook ----------------------------------------------------------------------


def check_webhook(url: str, fmt: str) -> None:
    """Refuses a webhook URL that isn't http(s), or an unknown format. The
    message never repeats the URL: it is a secret."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("the alert webhook (HACKRF_WEB_ALERT_WEBHOOK_FILE) must hold an http(s) URL")
    if fmt not in Webhook.FORMATS:
        raise ValueError(f"HACKRF_WEB_ALERT_WEBHOOK_FORMAT must be one of {', '.join(Webhook.FORMATS)}")


class Webhook:
    """Posts hits to the operator's webhook from a thread of its own: a
    bounded queue, a 10 s timeout and two retries. The URL is a secret: it
    is never logged or shown, and errors are reported without it."""

    FORMATS = ("json", "discord", "ntfy")

    def __init__(self, url: str, fmt: str, store: AlertStore, base_url: str | None = None, timeout: float = 10.0):
        check_webhook(url, fmt)
        self.url = url
        self.fmt = fmt
        self.store = store
        self.base_url = (base_url or "").rstrip("/") or None
        self.timeout = timeout
        self.q: queue.Queue = queue.Queue(maxsize=100)
        self.retry_wait_s = 5.0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="alert-webhook", daemon=True)
            self._thread.start()

    def submit(self, hit: dict) -> None:
        try:
            self.q.put_nowait(hit)
        except queue.Full:
            self.store.set_webhook_status(hit["id"], "dropped: too many at once")

    def _run(self) -> None:
        while True:
            hit = self.q.get()
            if hit is None:
                return
            self.store.set_webhook_status(hit["id"], self.send(hit))

    def link(self, hit: dict) -> str | None:
        if not self.base_url or not hit.get("subject"):
            return None
        q = urllib.parse.urlencode(
            {"subject": hit["subject"], "profiles": hit["profile"], "since": int(hit["ts"] - 600), "until": int(hit["ts"] + 600)}
        )
        return f"{self.base_url}/log?{q}"

    def request(self, hit: dict) -> urllib.request.Request:
        """The webhook's request for a hit. `aircraft` (aircraftdb.describe()
        of the subject, if it is an aircraft) says what it is."""
        who = hit.get("subject") or "?"
        if hit.get("label") and hit["label"] != who:
            who += f" ({hit['label']})"
        ac = aircraft_text(hit.get("aircraft"))
        text = f"{hit['rule_name']}: {hit['profile']} {who}{f' · {ac}' if ac else ''} · {hit['detail']} · {_iso(hit['ts'])}"
        link = self.link(hit)
        if self.fmt == "discord":
            # Labels arrive over the air: no mentions, whatever they say.
            body = {"content": f"hackrf alert · {text}" + (f"\n{link}" if link else ""), "allowed_mentions": {"parse": []}}
            return self._json(body)
        if self.fmt == "ntfy":
            title = f"hackrf: {hit['rule_name']}".encode("ascii", "replace").decode()[:120]
            headers = {"Title": title.replace("\r", " ").replace("\n", " "), "Content-Type": "text/plain; charset=utf-8"}
            if link:
                headers["Click"] = link
            return urllib.request.Request(self.url, data=text.encode(), headers=headers, method="POST")
        body = {
            "rule": hit["rule_name"],
            "profile": hit["profile"],
            "kind": hit.get("kind"),
            "subject": hit.get("subject"),
            "label": hit.get("label"),
            "freq_hz": hit.get("freq_hz"),
            "detail": hit["detail"],
            "time": _iso(hit["ts"]),
            "link": link,
        }
        if hit.get("aircraft"):
            body["aircraft"] = {k: v for k, v in hit["aircraft"].items() if k != "why"}
        return self._json(body)

    def _json(self, body: dict) -> urllib.request.Request:
        return urllib.request.Request(
            self.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )

    def send(self, hit: dict, attempts: int = 3) -> str:
        last = ""
        for i in range(attempts):
            try:
                with urllib.request.urlopen(self.request(hit), timeout=self.timeout) as r:
                    return f"sent ({r.status})"
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                reason = getattr(exc, "reason", exc)
                last = type(reason).__name__ if not isinstance(reason, str) else "connection failed"
            if i + 1 < attempts:
                time.sleep(self.retry_wait_s)
        return f"failed: {last}"


# --- The engine -----------------------------------------------------------------------


class AlertEngine:
    """Checks every row the live service logs against the enabled rules.
    `derived` works out the fields rules may test that aren't in the row."""

    def __init__(
        self,
        store: AlertStore,
        live_db: Path,
        pager_content: bool,
        webhook: Webhook | None = None,
        derived: Derived = NO_DERIVED,
    ):
        self.store = store
        self.live_db = Path(live_db)
        self.pager_content = pager_content
        self.webhook = webhook
        self.derived = derived
        self.rules: list[Rule] = []
        self.wanted: set[str] = set()
        self.last: dict[tuple, float] = {}
        self._summary: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.reload()

    def reload(self) -> None:
        """Re-reads the rules (after a change) and the summary."""
        self.rules = [r for r in self.store.rules() if r.enabled]
        self.wanted = derived_wanted(self.rules)
        longest = max([r.repeat_s for r in self.rules] or [0])
        self.last = self.store.last_hits(time.time() - longest - 3600)
        self.refresh()

    def refresh(self) -> None:
        self._summary = self.store.summary()

    def summary(self) -> dict:
        """For /api/status (pushed every few seconds): from memory, not the database."""
        return {**self._summary, "webhook": self.webhook is not None, "error": self.error}

    def start(self) -> None:
        if self.webhook is not None:
            self.webhook.start()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="alert-engine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(POLL_S):
            self.tick()

    def tick(self) -> None:
        """One look, as the thread takes it: whatever goes wrong is kept in
        `error` (shown in /api/status) and the next look tries again, so a
        surprise can't silently end the checking."""
        try:
            self.poll_once()
            self.error = None
        except live_log.LogNotFound:
            self.error = "the live log isn't there yet"
        except sqlite3.Error as exc:
            self.error = f"reading the live log: {exc}"
        except Exception as exc:  # noqa: BLE001 - the thread must outlive any one row
            self.error = f"checking rows: {type(exc).__name__}: {exc}"

    def _prepare(self, rows: list, wanted: set[str], cache: dict | None = None) -> list[tuple[dict, dict]]:
        """Rows as (row, fields), with the derived fields `wanted` added."""
        items = []
        for r in rows:
            row = dict(r)
            try:
                fields = json.loads(row.get("fields") or "{}")
            except ValueError:
                fields = {}
            items.append((row, fields if isinstance(fields, dict) else {}))
        self.derived.add(items, wanted, cache)
        return items

    def poll_once(self, now: float | None = None) -> list[dict]:
        """Checks the rows logged since the last look; returns the new hits."""
        now = time.time() if now is None else now
        with closing(live_log.open_reader(self.live_db)) as conn:
            top = conn.execute("SELECT max(id) FROM messages").fetchone()[0] or 0
            mark = self.store.get_state("watermark")
            if mark is None or int(mark) > top:
                # First start (old rows are the preview's business, not
                # alerts), or a live.db rebuilt with ids from 1 again.
                self.store.set_state("watermark", str(top))
                return []
            mark = int(mark)
            hits: list[dict] = []
            while mark < top:
                rows = conn.execute("SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (mark, BATCH)).fetchall()
                if not rows:
                    break
                if self.rules:
                    for row, fields in self._prepare(rows, self.wanted):
                        hits += self._check(row, fields, now)
                mark = rows[-1]["id"]
                self.store.set_state("watermark", str(mark))
        if hits:
            self.store.add_hits(hits)
            posted = [h for h in hits if h.get("_post")]
            aircraft = self.derived.aircraft_of(posted) if posted else {}
            for h in hits:
                post = h.pop("_post", False)
                detail = h.pop("_webhook_detail", None)
                if post:
                    out = {**h, "detail": detail or h["detail"]}
                    info = aircraft.get(str(h.get("subject") or "").upper())
                    if info:
                        out["aircraft"] = info
                    self.webhook.submit(out)
            self.refresh()
        return hits

    def _check(self, row: dict, fields: dict, now: float) -> list[dict]:
        out = []
        for rule in self.rules:
            detail = match(rule, row, fields, self.pager_content)
            if detail is None:
                continue
            key = (rule.id, row["profile"], row.get("subject"))
            if rule.repeat_s and key in self.last and row["ts"] - self.last[key] < rule.repeat_s:
                continue
            self.last[key] = row["ts"]
            post = rule.webhook and self.webhook is not None
            status = None
            if post and now - row["ts"] > WEBHOOK_MAX_AGE_S:
                post, status = False, "not sent: logged over 30 min before it was checked"
            elif post:
                status = "queued"
            elif rule.webhook:
                status = "not sent: no webhook set up"
            hit = {
                "ts": row["ts"],
                "recorded": now,
                "rule_id": rule.id,
                "rule_name": rule.name,
                "profile": row["profile"],
                "kind": row.get("kind"),
                "subject": row.get("subject"),
                "label": row.get("label"),
                "freq_hz": row.get("freq_hz"),
                "detail": " · ".join(detail),
                "row_id": row["id"],
                "webhook": status,
                "_post": post,
            }
            if post and any(c.field in NOT_POSTED for c in rule.conditions):
                hit["_webhook_detail"] = " · ".join(match(rule, row, fields, self.pager_content, hide=NOT_POSTED) or detail)
            out.append(hit)
        return out

    def preview(self, rule: Rule, since: float, until: float, limit: int = 50, max_rows: int = 400_000) -> dict:
        """What the rule would have matched over [since, until]: how many
        rows, and the newest `limit` of them (as hits would record them),
        ignoring the repeat limit."""
        where = "ts >= ? AND ts <= ?"
        args: list = [since, until]
        profiles = list(rule.profiles)
        wanted = derived_wanted([rule])
        if wanted:
            # Only the profiles that have every derived field the rule tests.
            names = profiles or list(live_profiles.PROFILES)
            profiles = [
                n for n in names if n in live_profiles.PROFILES and wanted <= set(self.derived.fields(live_profiles.PROFILES[n]))
            ]
            if not profiles:
                return {"since": since, "until": until, "matches": 0, "scanned": 0, "truncated": False, "samples": []}
        if profiles:
            where += f" AND profile IN ({', '.join('?' * len(profiles))})"
            args += profiles
        extra, extra_args = _prefilter(rule)
        n = 0
        scanned = 0
        samples: list[dict] = []
        cache: dict = {}
        with closing(live_log.open_reader(self.live_db)) as conn:
            sql = f"SELECT * FROM messages WHERE {where}{extra} ORDER BY ts DESC, id DESC LIMIT ?"
            cur = conn.execute(sql, [*args, *extra_args, max_rows])
            while rows := cur.fetchmany(BATCH):
                scanned += len(rows)
                for row, fields in self._prepare(rows, wanted, cache):
                    detail = match(rule, row, fields, self.pager_content)
                    if detail is None:
                        continue
                    n += 1
                    if len(samples) < limit:
                        samples.append(
                            {
                                "ts": row["ts"],
                                "profile": row["profile"],
                                "kind": row.get("kind"),
                                "subject": row.get("subject"),
                                "label": row.get("label"),
                                "freq_hz": row.get("freq_hz"),
                                "detail": " · ".join(detail),
                            }
                        )
        return {"since": since, "until": until, "matches": n, "scanned": scanned, "truncated": scanned >= max_rows, "samples": samples}
