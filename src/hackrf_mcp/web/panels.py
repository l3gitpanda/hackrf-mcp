"""Panels: small views of the live log that the operator adds as files.

A panel is a TOML file in the `panels` folder of hackrf-web's state
directory. It is data, not code: it names a slot on a screen, the profiles
whose rows it reads, an optional filter (the same conditions as an alert
rule), how to group the rows, the fields to show and a renderer. The UI
can't create or change them. Bad files are reported in the Plugins screen's
catalogue and left out; they never stop hackrf-web.

    [panel]
    title = "Squawk board"
    slot = "summaries.card"
    render = "table"            # table, list or stat

    [query]
    profiles = ["adsb"]
    kinds = ["aircraft"]        # optional: the profiles' summary kinds
    fields = ["squawk", "callsign"]
    group_by = "subject"        # or one of the fields
    sort = "last"               # last, rows or messages
    limit = 20
    window = "1h"               # optional: else the screen's own period
    filter = [{ field = "squawk", op = "any_of", values = ["7500", "7600", "7700"] }]

Fields are those an alert rule may test (web/alerts.allowed_fields), never
a row's raw text (its fields hold the same). Pager text (`message`) may be
filtered on, shown and grouped by only when the operator allows pager
content; with it off, a panel that asks for it is refused. Panels are read
again at every page load, so turning the setting off takes effect at once.
A panel reads at most 7 days.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from hackrf_mcp import live_log, live_profiles
from hackrf_mcp.web import alerts

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - the image runs 3.12
    tomllib = None

# Where a panel can go, and what it is there.
SLOTS = {"summaries.card": "a card on the Summaries screen, beside the built-in ones"}
RENDERS = ("table", "list", "stat")
SORTS = ("last", "rows", "messages")
MAX_FILES = 50
MAX_FILE_BYTES = 64 * 1024
MAX_LIMIT = 100
MAX_WINDOW_S = 7 * 86400
MAX_SCAN = 300_000
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_WINDOW = re.compile(r"^(\d+)\s*([mhd])$")


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    slot: str
    render: str
    profiles: tuple[str, ...]
    kinds: tuple[str, ...]
    fields: tuple[str, ...]
    group_by: str
    sort: str
    limit: int
    window_s: float | None
    rule: alerts.Rule | None
    file: str

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "slot": self.slot,
            "render": self.render,
            "profiles": list(self.profiles),
            "kinds": list(self.kinds),
            "fields": list(self.fields),
            "group_by": self.group_by,
            "sort": self.sort,
            "limit": self.limit,
            "window_s": self.window_s,
            "filter": self.rule.to_json()["text"] if self.rule else None,
            "file": self.file,
        }


@dataclass
class Loaded:
    panels: list[Panel] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _window(value) -> float | None:
    if value is None:
        return None
    m = _WINDOW.match(str(value).strip())
    if not m:
        raise ValueError('window is like "30m", "6h" or "7d"')
    s = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if not 0 < s <= MAX_WINDOW_S:
        raise ValueError("window is at most 7 days")
    return float(s)


def parse(doc: dict, name: str, pager_content: bool) -> Panel:
    """A panel from a parsed TOML document, checked against the registry."""
    live_profiles.load()
    p = doc.get("panel")
    q = doc.get("query")
    if not isinstance(p, dict) or not isinstance(q, dict):
        raise ValueError("a panel file needs a [panel] and a [query] table")
    pid = str(p.get("id") or name).lower()
    if not _ID.match(pid):
        raise ValueError("id is lowercase letters, digits, - and _")
    title = str(p.get("title") or "").strip()
    if not title or len(title) > 80:
        raise ValueError("title is 1-80 characters")
    slot = str(p.get("slot") or "")
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of: {', '.join(SLOTS)}")
    render = str(p.get("render") or "table")
    if render not in RENDERS:
        raise ValueError(f"render must be one of: {', '.join(RENDERS)}")
    profiles = tuple(dict.fromkeys(str(x) for x in q.get("profiles") or []))
    if not profiles:
        raise ValueError("query.profiles names at least one profile")
    for name_ in profiles:
        if name_ not in live_profiles.PROFILES or live_profiles.PROFILES[name_].spectrum_only:
            raise ValueError(f"unknown profile {name_!r}")
    allowed: set[str] = set()
    default_kinds: list[str] = []
    for name_ in profiles:
        prof = live_profiles.PROFILES[name_]
        allowed.update(alerts.allowed_fields(prof, pager_content))
        default_kinds += [k for k in prof.summary_kinds if k not in default_kinds]
    kinds = tuple(str(k) for k in q.get("kinds") or default_kinds)
    fields = tuple(dict.fromkeys(str(f) for f in q.get("fields") or []))
    if len(fields) > 8:
        raise ValueError("at most 8 fields")
    for f in fields:
        if f == "message" and not pager_content:
            raise ValueError("pager text is shown only when the operator allows pager content")
        if f not in allowed:
            raise ValueError(f"{f!r} is not a field these profiles log (or may be shown)")
    group_by = str(q.get("group_by") or "subject")
    if group_by != "subject" and group_by not in allowed:
        raise ValueError(f"group_by is subject or a field these profiles log, not {group_by!r}")
    sort = str(q.get("sort") or "last")
    if sort not in SORTS:
        raise ValueError(f"sort must be one of: {', '.join(SORTS)}")
    try:
        limit = int(q.get("limit", 20))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit is a number") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit is 1-{MAX_LIMIT}")
    rule = None
    if q.get("filter"):
        # The same checks and matching as an alert rule.
        rule = alerts.parse_rule({"name": title, "profiles": list(profiles), "conditions": q["filter"]}, pager_content)
    return Panel(pid, title, slot, render, profiles, kinds, fields, group_by, sort, limit, _window(q.get("window")), rule, name)


def load(folder: Path, pager_content: bool) -> Loaded:
    """Every panel file in `folder` (*.toml, at most MAX_FILES), and an
    error per file that isn't a valid panel."""
    out = Loaded()
    if not folder.is_dir():
        return out
    if tomllib is None:
        out.errors.append({"file": str(folder), "error": "panels need Python 3.11 or later"})
        return out
    seen: set[str] = set()
    for f in sorted(folder.glob("*.toml"))[:MAX_FILES]:
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("the file is over 64 KiB")
            panel = parse(tomllib.loads(f.read_text()), f.stem, pager_content)
            if panel.id in seen:
                raise ValueError(f"another file already has id {panel.id!r}")
            seen.add(panel.id)
            out.panels.append(panel)
        except (ValueError, OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            out.errors.append({"file": f.name, "error": str(exc)})
    return out


def run(conn: sqlite3.Connection, panel: Panel, since: float, until: float) -> dict:
    """The panel's rows over [since, until] (its own window instead, if it
    has one, ending at `until`): one per group, newest values of its fields,
    sorted and cut to its limit; `total` is how many groups there were."""
    if panel.window_s is not None:
        since = until - panel.window_s
    since = max(since, until - MAX_WINDOW_S)
    where = f"ts >= ? AND coalesce(ts_end, ts) >= ? AND ts <= ? AND profile IN ({', '.join('?' * len(panel.profiles))})"
    args: list = [since - live_log.ROW_SPAN_S, since, until, *panel.profiles]
    if panel.kinds:
        where += f" AND kind IN ({', '.join('?' * len(panel.kinds))})"
        args += list(panel.kinds)
    if panel.rule is not None:
        extra, extra_args = alerts._prefilter(panel.rule)
        where += extra
        args += extra_args
    groups: dict[str, dict] = {}
    matched = scanned = 0
    sql = (
        "SELECT id, ts, coalesce(ts_end, ts) AS last, profile, kind, freq_hz, level_db, subject, label, fields "
        f"FROM messages WHERE {where} ORDER BY ts DESC, id DESC LIMIT ?"
    )
    for r in conn.execute(sql, [*args, MAX_SCAN]):
        scanned += 1
        row = dict(r)
        try:
            fields = json.loads(row["fields"] or "{}")
        except ValueError:
            fields = {}
        if panel.rule is not None and alerts.match(panel.rule, row, fields) is None:
            continue
        matched += 1
        if panel.group_by == "subject":
            key = row["subject"] or row["label"] or "?"
        else:
            vals = alerts._values_of(row, fields, panel.group_by)
            if not vals:
                continue
            key = ", ".join(str(v) for v in vals)
        g = groups.get(key)
        msgs = fields.get("messages")
        msgs = msgs if isinstance(msgs, int) and not isinstance(msgs, bool) else 1
        if g is None:
            # Rows come newest first: the first seen holds the latest values.
            g = groups[key] = {
                "key": key,
                "profile": row["profile"],
                "subject": row["subject"],
                "label": row["label"],
                "rows": 0,
                "messages": 0,
                "first": row["ts"],
                "last": row["last"],
                "fields": {},
            }
        g["rows"] += 1
        g["messages"] += msgs
        g["first"] = min(g["first"], row["ts"])
        g["last"] = max(g["last"], row["last"])
        if not g["label"] and row["label"]:
            g["label"] = row["label"]
        for f in panel.fields:
            if f in g["fields"]:
                continue
            v = _value(row, fields, f)
            if v not in (None, "", [], {}):
                g["fields"][f] = v
    order = {"last": lambda g: -g["last"], "rows": lambda g: (-g["rows"], -g["last"]), "messages": lambda g: (-g["messages"], -g["last"])}
    rows = sorted(groups.values(), key=order[panel.sort])
    return {
        "since": since,
        "until": until,
        "total": len(groups),
        "matched": matched,
        "scanned": scanned,
        "truncated": scanned >= MAX_SCAN,
        "rows": rows[: panel.limit],
    }


def _value(row: dict, fields: dict, f: str):
    if f in alerts.BASE_FIELDS:
        return row.get(f)
    return fields.get(f)
