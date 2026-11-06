"""Day plans (live_day.py): parts of the day on a local clock. Expected
instants come from datetime in the plan's zone, never typed by hand."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hackrf_mcp import live_day
from hackrf_mcp.live_profiles import Spec, SpecError

ZONE = "Europe/Berlin"
TZ = ZoneInfo(ZONE)
A, B, C, D = Spec("a1", (1,)), Spec("a1", (2,)), Spec("a1", (3,)), Spec("b1")
DAY_STEPS = [(A, 5), (B, 5), (C, 5), (D, 45)]
NIGHT_STEPS = [(A, 10), (B, 10), (C, 10), (D, 30)]


def at(*args, tz=TZ, fold=0) -> float:
    return datetime(*args, tzinfo=tz, fold=fold).timestamp()


def plan(zone=ZONE, day=DAY_STEPS, night=NIGHT_STEPS):
    return live_day.make(zone, [("night", 23 * 60, night), ("day", 6 * 60, day)])


def test_parts_are_kept_in_order_of_start_and_checked():
    d = plan()
    assert [p.name for p in d.parts] == ["day", "night"]
    assert [p.start for p in d.parts] == ["06:00", "23:00"]
    with pytest.raises(SpecError, match="at least two parts"):
        live_day.make(ZONE, [("all", 0, DAY_STEPS)])
    with pytest.raises(SpecError, match="both start at 06:00"):
        live_day.make(ZONE, [("x", 360, DAY_STEPS), ("y", 360, NIGHT_STEPS)])
    with pytest.raises(SpecError, match="name of its own"):
        live_day.make(ZONE, [("x", 360, DAY_STEPS), ("x", 1380, NIGHT_STEPS)])
    with pytest.raises(SpecError, match="at least one step"):
        live_day.make(ZONE, [("x", 360, DAY_STEPS), ("y", 1380, [])])
    with pytest.raises(SpecError, match="unknown time zone 'Mars/Olympus'"):
        live_day.make("Mars/Olympus", [("x", 360, DAY_STEPS), ("y", 1380, NIGHT_STEPS)])
    with pytest.raises(SpecError, match="unknown time zone"):
        live_day.zone("../../etc/passwd")
    # A part with no name is called by its start.
    assert live_day.make(ZONE, [("", 360, DAY_STEPS), (None, 1380, NIGHT_STEPS)]).parts[1].name == "23:00"


@pytest.mark.parametrize("text,minutes", [("00:00", 0), ("06:00", 360), ("23:59", 1439), (" 7:05 ", 425)])
def test_start_times_parse(text, minutes):
    assert live_day.parse_start(text) == minutes
    assert live_day.fmt_start(minutes) == text.strip().zfill(5)


@pytest.mark.parametrize("text", ["24:00", "12:60", "12", "noon", "12:5", "-1:00", ""])
def test_bad_start_times_are_refused(text):
    with pytest.raises(SpecError, match="like 23:00"):
        live_day.parse_start(text)


def test_each_part_starts_at_its_first_step_and_cuts_its_last_cycle():
    d = plan()
    # Day part: a1@1 at 06:00, then each hour from there.
    spec, until, span = live_day.target(d, at(2026, 6, 10, 6, 0))
    assert spec == A and until == at(2026, 6, 10, 6, 5) and d.parts[span.index].name == "day"
    assert span.since == at(2026, 6, 10, 6, 0) and span.until == at(2026, 6, 10, 23, 0)
    spec, until, _ = live_day.target(d, at(2026, 6, 10, 14, 7, 30))
    assert spec == B and until == at(2026, 6, 10, 14, 10)
    spec, until, _ = live_day.target(d, at(2026, 6, 10, 22, 59))
    assert spec == D and until == at(2026, 6, 10, 23, 0)
    # Night part: starts at its first step at 23:00, runs past midnight.
    spec, until, span = live_day.target(d, at(2026, 6, 10, 23, 0))
    assert spec == A and until == at(2026, 6, 10, 23, 10) and d.parts[span.index].name == "night"
    spec, until, span = live_day.target(d, at(2026, 6, 11, 0, 25))
    assert spec == C and until == at(2026, 6, 11, 0, 30)
    assert span.since == at(2026, 6, 10, 23, 0) and span.until == at(2026, 6, 11, 6, 0)
    # A cycle that doesn't fit is cut short by the next part.
    odd = live_day.make(ZONE, [("x", 360, [(A, 7), (D, 7)]), ("y", 370, [(B, 1)])])
    spec, until, _ = live_day.target(odd, at(2026, 6, 10, 6, 8))
    assert spec == D and until == at(2026, 6, 10, 6, 10)
    # A single-step part runs its one profile until the next part.
    spec, until, _ = live_day.target(odd, at(2026, 6, 10, 12, 0))
    assert spec == B and until == at(2026, 6, 11, 6, 0)


def test_spring_forward_shortens_the_part_that_spans_it():
    # Berlin: 2026-03-29 02:00 CET becomes 03:00 CEST.
    d = plan()
    span = live_day.span_at(d, at(2026, 3, 29, 1, 0))
    assert span.since == at(2026, 3, 28, 23, 0) and span.until == at(2026, 3, 29, 6, 0)
    assert span.until - span.since == 6 * 3600
    # The night still steps on the elapsed clock from 23:00: 03:30 local is 3.5 h in.
    spec, until, _ = live_day.target(d, at(2026, 3, 29, 3, 30))
    assert spec == D and until == at(2026, 3, 29, 4, 0)
    t = live_day.today(d, at(2026, 3, 29, 12, 0))
    assert t["to"] - t["from"] == 23 * 3600
    assert t["share"]["b1"] == pytest.approx((17 * 0.75 + 6 * 0.5) / 23)


def test_a_start_in_the_spring_gap_takes_effect_an_hour_later_by_the_clock():
    d = live_day.make(ZONE, [("x", 2 * 60 + 30, [(A, 1)]), ("y", 12 * 60, [(B, 1)])])
    span = live_day.span_at(d, at(2026, 3, 29, 12, 0) - 1)
    assert span.since == at(2026, 3, 29, 3, 30)  # 02:30 never happened; 03:30 CEST is the same instant as 02:30 CET would have been


def test_fall_back_lengthens_the_part_and_an_ambiguous_start_takes_its_first_occurrence():
    # Berlin: 2026-10-25 03:00 CEST becomes 02:00 CET.
    d = plan()
    span = live_day.span_at(d, at(2026, 10, 25, 1, 0))
    assert span.until - span.since == 8 * 3600
    t = live_day.today(d, at(2026, 10, 25, 12, 0))
    assert t["to"] - t["from"] == 25 * 3600
    amb = live_day.make(ZONE, [("x", 2 * 60 + 30, [(A, 1)]), ("y", 12 * 60, [(B, 1)])])
    span = live_day.span_at(amb, at(2026, 10, 25, 12, 0) - 1)
    assert span.since == at(2026, 10, 25, 2, 30, fold=0)
    assert span.since < at(2026, 10, 25, 2, 30, fold=1)


def test_a_zone_without_daylight_saving_and_utc():
    d = plan(zone="UTC")
    spec, until, span = live_day.target(d, at(2026, 3, 29, 23, 5, tz=ZoneInfo("UTC")))
    assert spec == A and d.parts[span.index].name == "night"


def test_todays_strip_and_shares():
    d = plan()
    now = at(2026, 6, 10, 12, 0)
    t = live_day.today(d, now)
    assert t["from"] == at(2026, 6, 10, 0, 0) and t["to"] == at(2026, 6, 11, 0, 0)
    assert [(s["name"], s["from"], s["to"]) for s in t["segments"]] == [
        ("night", at(2026, 6, 10, 0, 0), at(2026, 6, 10, 6, 0)),
        ("day", at(2026, 6, 10, 6, 0), at(2026, 6, 10, 23, 0)),
        ("night", at(2026, 6, 10, 23, 0), at(2026, 6, 11, 0, 0)),
    ]
    # 17 h at 45/60 and 7 h at 30/60.
    assert t["share"] == {"b1": pytest.approx(16.25 / 24), "a1": pytest.approx(7.75 / 24)}
    assert list(t["share"]) == ["b1", "a1"]  # largest first
    # Against today's single rotation (45 of every 60 minutes).
    assert live_day.share(live_day.make(ZONE, [("x", 0, DAY_STEPS), ("y", 12 * 60, DAY_STEPS)]), t["from"], t["to"]) == {
        "b1": pytest.approx(0.75),
        "a1": pytest.approx(0.25),
    }


def test_steps_tile_the_window():
    d = plan()
    t0, t1 = at(2026, 6, 10, 0, 0), at(2026, 6, 11, 0, 0)
    got = list(live_day.steps(d, t0, t1))
    assert got[0][1] == t0 and got[-1][2] == t1
    for (_, _, b), (_, a, _) in zip(got, got[1:]):
        assert a == b


def test_json_round_trip_and_status_form():
    d = plan()
    again = live_day.from_json(live_day.to_json(d))
    assert again == d and again.tz.key == ZONE
    st = live_day.status_json(d)
    assert st["zone"] == ZONE
    assert st["parts"][1] == {
        "name": "night",
        "start": "23:00",
        "schedule": [{**s.to_json(), "minutes": float(m)} for s, m in NIGHT_STEPS],
    }
    assert live_day.describe(d, live_day.span_at(d, at(2026, 6, 10, 23, 30))) == f"night: 23:00-06:00 {ZONE}"
