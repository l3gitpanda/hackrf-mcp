"""A day plan for the live service: the day split into parts on a local
clock, each part with its own rotation (or a single profile).

A part starts at a local wall-clock time ("HH:MM" in the plan's IANA time
zone) and runs until the next part starts, every day; the last part runs
past midnight into the first. At its start a part begins at its first step,
so the same steps run at the same clock times every day; its last cycle is
cut short by the next part.

Local times follow the zone's daylight saving rules. A start that falls in
the hour a spring change skips takes effect at the instant the clock would
have shown it before the change (02:30 becomes 03:30); a start in the hour
an autumn change repeats takes its first occurrence. So the part that spans
the change runs an hour shorter or longer that day.

Pure functions only: no locks, no clock of its own, nothing about any one
profile. The live service (live_service.py) keeps the plan in its desired
state and asks target() what should run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hackrf_mcp.live_profiles import Spec, SpecError

MAX_PARTS = 24
MAX_NAME = 24


@dataclass(frozen=True)
class Part:
    name: str
    start_min: int  # minutes after local midnight
    schedule: tuple[tuple[Spec, float], ...]  # (spec, minutes); one entry runs all part long

    @property
    def start(self) -> str:
        return fmt_start(self.start_min)


@dataclass(frozen=True)
class Day:
    zone: str
    parts: tuple[Part, ...]  # in order of start
    tz: ZoneInfo = field(compare=False, repr=False, default=None)


@dataclass(frozen=True)
class Span:
    """One occurrence of a part: index into Day.parts, and when it runs."""

    index: int
    since: float
    until: float


def parse_start(text: str) -> int:
    t = str(text).strip()
    try:
        hh, mm = t.split(":")
        h, m = int(hh), int(mm)
    except ValueError:
        raise SpecError(f"a part starts at a time like 23:00, not {t!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59 and len(mm) == 2):
        raise SpecError(f"a part starts at a time like 23:00, not {t!r}")
    return h * 60 + m


def fmt_start(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def zone(name: str) -> ZoneInfo:
    key = str(name).strip()
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise SpecError(f"unknown time zone {key!r} (an IANA name like Europe/Berlin or UTC)") from None


def make(zone_name: str, parts: list[tuple[str, int, list[tuple[Spec, float]]]]) -> Day:
    """A checked plan from (name, start minutes, schedule) triples."""
    tz = zone(zone_name)
    if len(parts) < 2:
        raise SpecError("a day plan needs at least two parts; start a rotation or a single profile instead")
    if len(parts) > MAX_PARTS:
        raise SpecError(f"a day plan has at most {MAX_PARTS} parts")
    out = []
    for name, start, schedule in parts:
        name = str(name or "").strip() or fmt_start(start)
        if len(name) > MAX_NAME:
            raise SpecError(f"a part's name is at most {MAX_NAME} characters: {name[:40]!r}")
        if not schedule:
            raise SpecError(f"part {name!r} needs at least one step")
        out.append(Part(name=name, start_min=int(start), schedule=tuple((s, float(m)) for s, m in schedule)))
    out.sort(key=lambda p: p.start_min)
    for a, b in zip(out, out[1:]):
        if a.start_min == b.start_min:
            raise SpecError(f"parts {a.name!r} and {b.name!r} both start at {a.start}")
    names = [p.name for p in out]
    if len(set(names)) != len(names):
        raise SpecError("each part needs a name of its own")
    return Day(zone=tz.key, parts=tuple(out), tz=tz)


# --- JSON ------------------------------------------------------------------


def to_json(day: Day) -> dict:
    """The saved form."""
    return {
        "zone": day.zone,
        "parts": [
            {"name": p.name, "start": p.start, "schedule": [{"spec": s.to_json(), "minutes": m} for s, m in p.schedule]}
            for p in day.parts
        ],
    }


def from_json(d: dict) -> Day:
    return make(
        d["zone"],
        [
            (p.get("name"), parse_start(p["start"]), [(Spec.from_json(e["spec"]), float(e["minutes"])) for e in p["schedule"]])
            for p in d["parts"]
        ],
    )


def status_json(day: Day) -> dict:
    """The form status() reports: entries flattened like a rotation's."""
    return {
        "zone": day.zone,
        "parts": [
            {"name": p.name, "start": p.start, "schedule": [{**s.to_json(), "minutes": m} for s, m in p.schedule]}
            for p in day.parts
        ],
    }


# --- time --------------------------------------------------------------------


def _at(day: Day, d: date, minutes: int) -> float:
    # fold=0: a time in a spring gap maps forward, an autumn repeat to its first.
    return datetime(d.year, d.month, d.day, minutes // 60, minutes % 60, tzinfo=day.tz).timestamp()


def _starts(day: Day, t0: float, t1: float) -> list[tuple[float, int]]:
    """Every part start from the local day before t0 to the day after t1,
    in time order. Two starts that land on one instant (a spring change can
    do that) leave only the later part, the earlier having no length."""
    d0 = datetime.fromtimestamp(t0, day.tz).date() - timedelta(days=1)
    d1 = datetime.fromtimestamp(t1, day.tz).date() + timedelta(days=1)
    out: list[tuple[float, int]] = []
    d = d0
    while d <= d1:
        for i, p in enumerate(day.parts):
            out.append((_at(day, d, p.start_min), i))
        d += timedelta(days=1)
    out.sort()
    kept: list[tuple[float, int]] = []
    for ts, i in out:
        if kept and kept[-1][0] == ts:
            kept[-1] = (ts, i)
        else:
            kept.append((ts, i))
    return kept


def spans(day: Day, t0: float, t1: float) -> list[Span]:
    """The parts' occurrences that overlap [t0, t1), unclipped."""
    starts = _starts(day, t0, t1)
    out = []
    for (a, i), (b, _) in zip(starts, starts[1:]):
        if b > t0 and a < t1:
            out.append(Span(i, a, b))
    return out


def span_at(day: Day, now: float) -> Span:
    for s in spans(day, now, now + 1e-6):
        if s.since <= now < s.until:
            return s
    raise AssertionError("a day plan always has a part running")  # pragma: no cover


def local_day(day: Day, now: float) -> tuple[float, float]:
    """Local midnight before now and the next one (23 or 25 h apart on the
    days the clocks change)."""
    d = datetime.fromtimestamp(now, day.tz).date()
    return _at(day, d, 0), _at(day, d + timedelta(days=1), 0)


def target(day: Day, now: float) -> tuple[Spec, float, Span]:
    """The step that should run now, when it ends (the step's end or the
    part's, whichever is first), and the part's span."""
    span = span_at(day, now)
    entries = day.parts[span.index].schedule
    if len(entries) == 1:
        return entries[0][0], span.until, span
    cycle = sum(m for _, m in entries) * 60
    pos = (now - span.since) % cycle
    end = 0.0
    for spec, minutes in entries:
        end += minutes * 60
        if pos < end:
            return spec, min(now + (end - pos), span.until), span
    return entries[-1][0], span.until, span  # pragma: no cover (float edge)


def steps(day: Day, t0: float, t1: float):
    """(spec, from, to) for every step that runs in [t0, t1), clipped to it."""
    for span in spans(day, t0, t1):
        entries = day.parts[span.index].schedule
        if len(entries) == 1:
            yield entries[0][0], max(span.since, t0), min(span.until, t1)
            continue
        t = span.since
        cycle = sum(m for _, m in entries) * 60
        if t < t0:  # skip whole cycles before the window
            t += ((t0 - t) // cycle) * cycle
        while t < min(span.until, t1):
            for spec, minutes in entries:
                a, b = t, min(t + minutes * 60, span.until)
                if b > t0 and a < t1:
                    yield spec, max(a, t0), min(b, t1)
                t += minutes * 60
                if t >= span.until:
                    break


def share(day: Day, t0: float, t1: float) -> dict[str, float]:
    """Each profile's fraction of [t0, t1) by the plan (holds, sweeps and
    captures not counted)."""
    total = t1 - t0
    out: dict[str, float] = {}
    if total <= 0:
        return out
    for spec, a, b in steps(day, t0, t1):
        out[spec.profile] = out.get(spec.profile, 0.0) + (b - a)
    return {k: v / total for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def today(day: Day, now: float) -> dict:
    """The local day containing now, for the web UI's strip: its bounds,
    each part's stretch of it (the part that spans midnight appears at both
    ends) and each profile's share of it."""
    t0, t1 = local_day(day, now)
    segs = [
        {"index": s.index, "name": day.parts[s.index].name, "start": day.parts[s.index].start, "from": max(s.since, t0), "to": min(s.until, t1)}
        for s in spans(day, t0, t1)
    ]
    return {"from": t0, "to": t1, "segments": segs, "share": share(day, t0, t1)}


def describe(day: Day, span: Span) -> str:
    """One line for the log when a part begins: its name and local hours."""
    p = day.parts[span.index]
    nxt = day.parts[(span.index + 1) % len(day.parts)]
    return f"{p.name}: {p.start}-{nxt.start} {day.zone}"
