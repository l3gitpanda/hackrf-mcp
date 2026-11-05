"""Spectrum occupancy (occupancy.py): hackrf_sweep's numbers, the parser,
occupancy.db, the readers, the slot scheduler in the live service, and the
web API."""

import base64
import hashlib
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from hackrf_mcp import agent, cli, occupancy, server, settings
from hackrf_mcp.live_service import LiveService, start_occupancy
from hackrf_mcp.live_spectrum import DB_MIN, DB_STEP
from hackrf_mcp.occupancy import DAY, HOUR, OccupancyWriter, Plan
from hackrf_mcp.web import alerts
from hackrf_mcp.web import app as web_app

from test_live_service import Clock, FakePipeline

FAKE_SWEEP = Path(__file__).parent.parent / "tools" / "fake_hackrf_sweep.py"
# A small plan: 24-64 MHz at 1 MHz, so 2 tuning steps, 8 lines of 5 bins.
SMALL = Plan(lo_mhz=24, hi_mhz=64, bin_width_hz=1_000_000, passes=2, lna_gain_db=24, vga_gain_db=40)
T0 = 1_793_923_200.0  # 2026-11-06 00:00:00 UTC, a Friday


def csv(plan: Plan, passes: list[np.ndarray], partial_lines: int = 0) -> str:
    """hackrf_sweep's CSV for `passes` (each float[bins] in dB, its own
    scale), lines in its order: per 20 MHz step, f, f+10, f+5, f+15 MHz."""
    out = []
    per = plan.per_line
    for p, values in enumerate([*passes, *([passes[0]] if partial_lines else [])]):
        n = 0
        for s in range(plan.steps):
            f = plan.lo_hz + s * 20_000_000
            for lo in (f, f + 10_000_000, f + 5_000_000, f + 15_000_000):
                if p == len(passes) and n >= partial_lines:
                    break
                k = (lo - plan.lo_hz) // 5_000_000
                vals = values[k * per : (k + 1) * per]
                out.append(
                    f"2026-11-05, 23:15:18.29, {lo}, {lo + 5_000_000}, {plan.bin_hz:.2f}, {plan.fft}, "
                    + ", ".join(f"{v:.2f}" for v in vals)
                )
                n += 1
    return "\n".join(out) + "\n"


def measured(plan: Plan, mean_db, peak_db=None, passes=None) -> occupancy.Measured:
    mean = np.broadcast_to(np.asarray(mean_db, dtype=np.float32), (plan.bins,)).copy()
    peak = mean + 10 if peak_db is None else np.broadcast_to(np.asarray(peak_db, dtype=np.float32), (plan.bins,)).copy()
    return occupancy.Measured(passes or plan.passes, 0, 0, mean, peak)


# --- hackrf_sweep's numbers -------------------------------------------------


def test_the_plan_matches_hackrf_sweep_on_the_station():
    # Measured 2026-11-05 through the agent: -w 100000 gave 98039.22 Hz bins,
    # num_samples 204, 51 values a line, 24 to 1704 MHz.
    p = Plan()
    assert p.fft == 204 and p.per_line == 51
    assert p.bin_hz == pytest.approx(98039.2157, abs=1e-3)
    assert p.top_mhz == 1704 and p.lines == 336 and p.bins == 17_136
    assert p.args() == ["-f", "24:1700", "-w", "100000", "-l", "24", "-g", "40", "-N", "20"]
    req = p.agent_request()
    assert req["tool"] == "sweep" and req["one_shot"] is False and req["num_sweeps"] == 20 and req["amp"] is False
    assert Plan(passes=1).args()[-1] == "-1" and Plan(passes=1).agent_request()["one_shot"] is True
    # The agent accepts what the plan asks for.
    assert agent.check_run(req, agent.AgentSettings())[0] == "hackrf_sweep"
    assert [occupancy.fft_size(w) for w in (1_000_000, 250_000, 5_000_000, 2445)] == [20, 84, 4, 8180]
    assert "24-1704 MHz, 20 passes at 98.0 kHz, LNA 24 dB, VGA 40 dB, amp off" == p.describe()


def test_a_full_scale_tone_reads_0_dbfs():
    # hackrf_sweep: Hann window, int8/128, FFT scaled by 1/n, 10 log10 |X|^2.
    n = 204
    k = 17
    x = np.exp(2j * np.pi * k * np.arange(n) / n) * np.hanning(n)
    db = 10 * np.log10(np.abs(np.fft.fft(x) / n) ** 2)
    assert db[k] == pytest.approx(-6.06, abs=0.01)
    assert db[k] + occupancy.window_gain_db(n) == pytest.approx(0.0, abs=1e-6)


# --- parsing ----------------------------------------------------------------


def test_parse_averages_complete_passes_in_linear_power():
    p = SMALL
    a = np.full(p.bins, -60.0)
    b = np.full(p.bins, -50.0)
    b[7] = -20.0
    text = csv(p, [a, b], partial_lines=3) + "2026-11-05, 23:15:18.29, 24500000, 29500000, 1000000.00, 20, 1, 2, 3, 4, 5\n"
    m = occupancy.parse(text, p)
    g = occupancy.window_gain_db(p.fft)
    assert m.passes == 2 and m.partial == 1 and m.bad_lines == 1
    assert m.mean_db.shape == (p.bins,)
    assert m.mean_db[0] == pytest.approx(10 * math.log10((1e-6 + 1e-5) / 2) + g, abs=1e-4)
    assert m.peak_db[0] == pytest.approx(-50 + g, abs=1e-4)
    assert m.peak_db[7] == pytest.approx(-20 + g, abs=1e-4)


def test_parse_without_a_complete_pass_fails():
    with pytest.raises(occupancy.SweepError, match="no complete pass"):
        occupancy.parse(csv(SMALL, [np.zeros(SMALL.bins)])[:200], SMALL)
    with pytest.raises(occupancy.SweepError):
        occupancy.parse("", SMALL)


# --- occupancy.db -------------------------------------------------------------


@pytest.fixture
def writer(tmp_path):
    w = OccupancyWriter(tmp_path / "occupancy.db", 365 * DAY, 1826 * DAY, 1 << 30, 0)
    yield w
    w.close()


def reader(w: OccupancyWriter):
    return occupancy.open_reader(w.path)


def test_daily_rows_roll_up_finished_days(writer):
    p = SMALL
    writer.add(T0 + 600, T0 + 600, 4.0, p, measured(p, -80, -60))
    writer.add(T0 + 1500, T0 + 1500, 4.0, p, measured(p, -70, -40))
    writer.add(T0 + DAY + 600, T0 + DAY + 600, 4.0, p, measured(p, -90))
    assert writer.roll_up(T0 + DAY + 1000) == 0  # the first day only just ended
    assert writer.roll_up(T0 + DAY + 1801) == 1
    assert writer.roll_up(T0 + DAY + 1801) == 0
    with reader(writer) as conn:
        row = conn.execute("SELECT * FROM daily").fetchone()
    assert row["day"] == T0 and row["slots"] == 2 and row["bins"] == p.bins
    mean = DB_MIN + np.frombuffer(row["mean"], np.uint8) * DB_STEP
    peak = DB_MIN + np.frombuffer(row["peak"], np.uint8) * DB_STEP
    assert mean[0] == pytest.approx(10 * math.log10((1e-8 + 1e-7) / 2), abs=DB_STEP)
    assert peak[0] == -40


def test_prune_by_age_and_size(tmp_path, writer):
    p = SMALL
    for i in range(10):
        writer.add(T0 + i * 900, T0 + i * 900, 4.0, p, measured(p, -80))
    writer.max_age_s = 5 * 900 + 1
    r = writer.prune(T0 + 9 * 900)
    assert r.deleted_age == 4 and writer.stats.slots_kept == 6
    # Big slots and a small cap: the oldest go first.
    big = Plan(passes=1)
    w2 = OccupancyWriter(tmp_path / "big.db", 365 * DAY, 1826 * DAY, 200_000, 0)
    for i in range(10):
        w2.add(T0 + i * 900, T0 + i * 900, 4.0, big, measured(big, -80))
    r = w2.prune(T0 + 9 * 900)
    assert r.deleted_size > 0 and w2.used_bytes() <= 200_000
    oldest = w2.conn.execute("SELECT min(ts) FROM slots").fetchone()[0]
    assert oldest > T0
    w2.close()


def test_low_disk_drops_a_slot(tmp_path):
    w = OccupancyWriter(tmp_path / "o.db", DAY, DAY, 1 << 30, 1 << 62)
    assert w.add(T0, T0, 4.0, SMALL, measured(SMALL, -80)) is None
    assert w.stats.dropped == 1 and w.stats.low_disk
    w.close()


# --- reading ----------------------------------------------------------------------


def test_grid_buckets_time_and_frequency(writer):
    p = SMALL
    for i in range(4):
        mean = np.full(p.bins, -90.0)
        mean[10:15] = -50.0 + i
        writer.add(T0 + i * 900, T0 + i * 900, 4.0, p, measured(p, mean, mean + 5))
    with reader(writer) as conn:
        g = occupancy.grid(conn, T0, T0 + HOUR, tbuckets=4, fbuckets=8)
        assert g["tier"] == "slots" and g["rows"] == 4 and len(g["ts"]) == 4
        assert g["fbuckets"] == 8 and g["bins_per_bucket"] == 5 and g["bucket_hz"] == 5e6 and g["f_lo_hz"] == 24e6
        mean = np.frombuffer(g["mean"], np.uint8).reshape(4, 8)
        peak = np.frombuffer(g["peak"], np.uint8).reshape(4, 8)
        # Bucket 2 is bins 10-14: the carrier.
        assert DB_MIN + mean[3, 2] * DB_STEP == pytest.approx(-47, abs=DB_STEP)
        assert DB_MIN + peak[3, 2] * DB_STEP == pytest.approx(-42, abs=DB_STEP)
        assert DB_MIN + mean[0, 0] * DB_STEP == pytest.approx(-90, abs=DB_STEP)
        # Two slots a bucket; a frequency range of its own.
        g2 = occupancy.grid(conn, T0, T0 + HOUR, fmin_hz=34e6, fmax_hz=39e6, tbuckets=2, fbuckets=100)
        assert g2["counts"] == [2, 2] and g2["fbuckets"] == 5 and g2["f_lo_hz"] == 34e6
        with pytest.raises(ValueError):
            occupancy.grid(conn, T0, T0 + HOUR, fmin_hz=5e9, fmax_hz=6e9)


def test_grid_uses_daily_rows_for_wide_buckets_and_only_the_newest_grid(writer):
    p = SMALL
    other = Plan(lo_mhz=24, hi_mhz=64, bin_width_hz=250_000, passes=2)
    for d in range(3):
        writer.add(T0 + d * DAY + 600, T0 + d * DAY + 600, 4.0, p, measured(p, -80 + d))
    writer.add(T0 + 2 * DAY + 900, T0 + 2 * DAY + 900, 4.0, other, measured(other, -60))
    writer.roll_up(T0 + 4 * DAY)
    with reader(writer) as conn:
        g = occupancy.grid(conn, T0, T0 + 3 * DAY, tbuckets=3, fbuckets=4)
        assert g["tier"] == "daily" and g["rows"] == 3
        # The newest grid in the range is the 250 kHz one.
        g = occupancy.grid(conn, T0, T0 + 3 * DAY, tbuckets=300, fbuckets=4)
        assert g["tier"] == "slots" and g["grid"]["bin_hz"] == other.bin_hz and g["other_grid"] == 3
        empty = occupancy.grid(conn, T0 - 10 * DAY, T0 - 9 * DAY)
        assert empty["rows"] == 0 and empty["grid"] is None


def test_profile_folds_by_hour_and_weekday(writer):
    p = SMALL
    for d in range(3):
        writer.add(T0 + d * DAY + 1 * HOUR, T0 + d * DAY + 1 * HOUR, 4.0, p, measured(p, -80))
        writer.add(T0 + d * DAY + 14 * HOUR, T0 + d * DAY + 14 * HOUR, 4.0, p, measured(p, -60))
    with reader(writer) as conn:
        h = occupancy.profile(conn, "hour", T0, T0 + 3 * DAY, fbuckets=1)
        assert h["counts"][1] == 3 and h["counts"][14] == 3 and sum(h["counts"]) == 6
        mean = np.frombuffer(h["mean"], np.uint8)
        assert DB_MIN + mean[14] * DB_STEP == -60 and DB_MIN + mean[1] * DB_STEP == -80 and mean[5] == 0
        w = occupancy.profile(conn, "weekday", T0, T0 + 3 * DAY)
        assert w["counts"][4] == 2 and w["counts"][5] == 2 and w["counts"][6] == 2  # Fri, Sat, Sun
        with pytest.raises(ValueError):
            occupancy.profile(conn, "hour", T0, T0 + 200 * DAY)
        with pytest.raises(ValueError):
            occupancy.profile(conn, "month", T0, T0 + DAY)
    assert occupancy.utc_weekday(T0) == 4 and occupancy.utc_hour(T0 + 14 * HOUR + 5) == 14


def test_unusual_compares_with_the_same_hour_over_the_week_before(writer):
    p = SMALL
    for d in range(7):
        for q in range(4):
            writer.add(T0 + d * DAY + 12 * HOUR + q * 900, T0 + d * DAY + 12 * HOUR + q * 900, 4.0, p, measured(p, -80))
    new = np.full(p.bins, -80.0)
    new[20:23] = -60.0
    new[24] = -65.0  # one bin's gap: the same range
    day8 = T0 + 7 * DAY
    writer.add(day8 + 12 * HOUR, day8 + 12 * HOUR, 4.0, p, measured(p, new))
    writer.add(day8 + 13 * HOUR, day8 + 13 * HOUR, 4.0, p, measured(p, new))  # no history at 13:00
    with reader(writer) as conn:
        u = occupancy.unusual(conn, day8, day8 + DAY)
        assert u["slots"] == 2 and u["no_baseline"] == 1 and len(u["items"]) == 1
        it = u["items"][0]
        assert it["lo_hz"] == 44e6 and it["hi_hz"] == 49e6 and it["bins"] == 5
        assert it["excess_db"] == 20 and it["usual_db"] == -80 and it["level_db"] == -60
        assert occupancy.unusual(conn, day8, day8 + DAY, threshold_db=25)["items"] == []
        with pytest.raises(ValueError):
            occupancy.unusual(conn, day8, day8 + 5 * DAY)
        c = occupancy.cell(conn, day8 + 12 * HOUR + 100, 44e6, 47e6)
        assert c["slot"]["ts"] == day8 + 12 * HOUR and c["bins"] == 3
        assert c["mean_db"] == pytest.approx(-60, abs=0.01) and c["usual_db"] == pytest.approx(-80, abs=0.01)
        assert occupancy.cell(conn, T0 - 5 * DAY, 44e6, 47e6) is None
        assert len(occupancy.slot_list(conn, day8, day8 + DAY)) == 2


# --- the scheduler in the live service -------------------------------------------


@pytest.fixture
def svc(tmp_path):
    pipes = []

    def make(tuning, ctx):
        pipe = FakePipeline(tuning, ctx)
        pipes.append(pipe)
        return pipe

    clock = Clock(T0 + 100)
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0, occupancy=True)
    s = LiveService(live, None, make_pipeline=make, program_finder=lambda p: "/bin/true", clock=clock)
    s.pipes, s.test_clock, s.events = pipes, clock, []
    s._event = lambda event, profile=None, detail=None: s.events.append((event, profile, detail))
    s.handle({"cmd": "start", "profile": "adsb"})
    return s


def scheduler(svc, writer, run_sweep, plan=SMALL, alerts_db=None, **kw):
    sched = occupancy.Scheduler(svc, plan, writer, 900, 600, 600, alerts_db, run_sweep, clock=svc.test_clock, poll_s=0.001, **kw)
    svc.occupancy = sched
    return sched


def good_sweep(plan, seen=None, svc=None):
    def run(p):
        if seen is not None:
            seen.append({"pipeline": svc.pipeline, "hold": dict(svc.hold) if svc.hold else None})
        return csv(p, [np.full(p.bins, -70.0)] * p.passes), {"exit_code": 0, "stderr": "", "timed_out": False}

    return run


def test_a_slot_lends_the_receiver_stores_the_sweep_and_gives_it_back(svc, writer):
    seen = []
    sched = scheduler(svc, writer, good_sweep(SMALL, seen, svc))
    assert sched.slot(T0) == "stored"
    # While sweeping: no decoder, the hold the slot's.
    assert seen[0]["pipeline"] is None and seen[0]["hold"]["owner"] == occupancy.OWNER
    assert svc.pipes[0].stopped and svc.hold is None
    svc.supervise()
    assert svc.status()["state"] == "running" and len(svc.pipes) == 2
    names = [e[0] for e in svc.events]
    assert "occupancy-done" in names and "pause" not in names and "resume" not in names
    done = next(e for e in svc.events if e[0] == "occupancy-done")[2]
    assert "2 passes" in done and "adsb paused" in done and "24-64 MHz" in done
    st = svc.status()["occupancy"]
    assert st["enabled"] and st["stored"] == 1 and st["last"]["result"] == "stored" and st["slots_kept"] == 1
    assert st["range_mhz"] == [24, 64] and st["lna_gain_db"] == 24 and st["amp"] is False
    with reader(writer) as conn:
        row = conn.execute("SELECT * FROM slots").fetchone()
    assert row["slot_ts"] == T0 and row["passes"] == 2 and row["lna_gain_db"] == 24 and row["amp"] == 0


def test_the_mcp_status_describes_occupancy_sweeps(svc, writer):
    sched = scheduler(svc, writer, good_sweep(SMALL))
    sched.next_at = T0 + 900
    sched.slot(T0)
    text = server._format_live_status(svc.status())
    assert "Occupancy sweeps: every 15 min on the UTC clock, 24-64 MHz, 2 passes at 1000.0 kHz, LNA 24 dB" in text
    assert "next 2026-11-06 00:15:00Z; 1 stored, 0 skipped and 0 failed" in text and "1 slots (kept 365 days)" in text
    svc.occupancy = None
    assert "Occupancy sweeps: off (HACKRF_LIVE_OCCUPANCY)." in server._format_live_status(svc.status())


def test_a_watch_skips_the_slot(svc, writer):
    svc.handle({"cmd": "watch", "center_hz": 162_000_000})
    calls = []
    sched = scheduler(svc, writer, lambda p: calls.append(p))
    assert sched.slot(T0) == "skipped" and not calls
    assert svc.events[-1] == ("occupancy-skip", None, "a watch has the receiver")
    assert svc.watch is not None and svc.hold is None


class LentFirst:
    """The service, with a device tool holding the receiver for the first
    `n` tries, each `step_s` of the clock apart."""

    def __init__(self, svc, n, step_s):
        self.svc, self.n, self.step_s = svc, n, step_s
        self.hold = svc.handle({"cmd": "pause", "owner": "hackrf_transfer", "lease_s": 3600})

    def occupancy_take(self, lease_s):
        if self.n == 0:
            self.svc.handle({"cmd": "resume", "token": self.hold["token"]})
        self.n -= 1
        self.svc.test_clock.t += self.step_s
        return self.svc.occupancy_take(lease_s)

    def __getattr__(self, name):
        return getattr(self.svc, name)


def test_a_slot_waits_for_a_device_tool_then_runs_late(svc, writer):
    lent = LentFirst(svc, 3, 40.0)
    sched = scheduler(lent, writer, good_sweep(SMALL))
    sched.service = lent
    assert sched.slot(T0) == "stored"
    done = next(e for e in svc.events if e[0] == "occupancy-done")[2]
    assert "min late (the receiver was lent to hackrf_transfer)" in done


def test_a_slot_lent_out_past_its_wait_is_skipped(svc, writer):
    lent = LentFirst(svc, 100, 200.0)
    sched = scheduler(lent, writer, lambda p: pytest.fail("swept while lent"))
    sched.service = lent
    assert sched.slot(T0 + 100) == "skipped"
    assert svc.events[-1][2] == "the receiver was lent to hackrf_transfer past 10 min"
    assert svc.hold["owner"] == "hackrf_transfer"  # the tool's hold untouched


def test_a_recent_alert_skips_the_slot(svc, writer, tmp_path):
    db = tmp_path / "web" / "alerts.db"
    store = alerts.AlertStore(db)
    hit = {"ts": svc.test_clock.t - 180, "recorded": svc.test_clock.t - 120, "rule_id": 1, "rule_name": "r",
           "profile": "adsb", "kind": "aircraft", "subject": "A1", "label": None, "freq_hz": None,
           "detail": "{}", "row_id": 1, "webhook": None}  # fmt: skip
    store.add_hits([hit])
    store.close()
    assert occupancy.last_alert_ts(db) == svc.test_clock.t - 180
    sched = scheduler(svc, writer, lambda p: pytest.fail("swept after an alert"), alerts_db=db)
    assert sched.slot(T0) == "skipped" and svc.events[-1][2] == "an alert hit 3 min ago"
    svc.test_clock.t += 600
    sched.run_sweep = good_sweep(SMALL)
    assert sched.slot(T0 + 900) == "stored"
    assert occupancy.last_alert_ts(tmp_path / "nope.db") is None


def test_a_failed_sweep_still_gives_the_receiver_back(svc, writer):
    def boom(p):
        raise ConnectionError("the hackrf-agent closed the connection")

    sched = scheduler(svc, writer, boom)
    assert sched.slot(T0) == "error"
    assert svc.hold is None and "the sweep failed: the hackrf-agent closed" in svc.events[-1][2]
    svc.supervise()
    assert svc.status()["state"] == "running"
    sched.run_sweep = lambda p: ("", {"exit_code": 1, "stderr": "hackrf_open() failed: not found\n", "timed_out": False})
    assert sched.slot(T0 + 900) == "error"
    assert "no complete pass" in svc.events[-1][2] and "(exit code 1): hackrf_open() failed" in svc.events[-1][2]
    assert svc.status()["occupancy"]["errors"] == 2


def test_a_device_tool_that_pauses_during_a_slot_keeps_the_receiver(svc, writer):
    tool = {}

    def run(p):
        tool.update(svc.handle({"cmd": "pause", "owner": "hackrf_transfer", "lease_s": 60}))
        return good_sweep(p)(p)

    sched = scheduler(svc, writer, run)
    assert sched.slot(T0) == "stored"
    assert svc.hold is not None and svc.hold["owner"] == "hackrf_transfer"
    svc.supervise()
    assert svc.pipeline is None
    svc.handle({"cmd": "resume", "token": tool["token"]})
    svc.supervise()
    assert svc.status()["state"] == "running"


def test_low_disk_skips_the_slot_without_taking_the_receiver(svc, tmp_path):
    w = OccupancyWriter(tmp_path / "o.db", DAY, DAY, 1 << 30, 1 << 62)
    sched = scheduler(svc, w, lambda p: pytest.fail("swept with no room"))
    assert sched.slot(T0) == "skipped" and svc.pipeline is not None
    w.close()


def test_slots_follow_the_utc_clock():
    assert occupancy.next_slot(T0 + 1, 900) == T0 + 900
    assert occupancy.next_slot(T0, 900) == T0 + 900
    assert occupancy.next_slot(T0 + 899.9, 900) == T0 + 900


def test_the_schedule_runs_by_itself_and_stops(tmp_path):
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0, occupancy=True)
    s = LiveService(live, None, make_pipeline=FakePipeline, program_finder=lambda p: "/bin/true")
    w = OccupancyWriter(tmp_path / "o.db", DAY, DAY, 1 << 30, 0)
    ran = threading.Event()

    def run(p):
        ran.set()
        return good_sweep(p)(p)

    sched = occupancy.Scheduler(s, SMALL, w, 0.2, 0.1, 0, None, run, poll_s=0.01)
    s.occupancy = sched
    sched.start()
    assert ran.wait(5)
    assert sched.stop()
    assert sched.counts["stored"] >= 1
    w.close()


def test_status_without_occupancy(tmp_path):
    s = LiveService(settings.LiveSettings(live_dir=tmp_path / "live"), None, make_pipeline=FakePipeline)
    assert s.status()["occupancy"] == {"enabled": False}


# --- the real path: hackrf_sweep through an agent, or on this machine -------------


@pytest.fixture
def sweep_tools(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "hackrf_sweep").write_text(f"#!{sys.executable}\n" + FAKE_SWEEP.read_text())
    (d / "hackrf_sweep").chmod(0o755)
    return d


def test_sweeps_through_a_hackrf_agent(tmp_path, sweep_tools):
    token = "occupancy-test-token"
    (tmp_path / "agent-token").write_text(token)
    a = agent.AgentSettings(
        host="127.0.0.1", port=0, token_sha256=(hashlib.sha256(token.encode()).hexdigest(),), tools_dir=str(sweep_tools)
    )
    a.digests = a.load_digests()
    srv = agent.AgentServer(a)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        live = settings.LiveSettings(
            live_dir=tmp_path / "live", agent=srv.server_address[:2], agent_token_file=tmp_path / "agent-token"
        )
        plan = Plan(lo_mhz=88, hi_mhz=168, bin_width_hz=100_000, passes=3)
        text, done = occupancy.sweep_runner(live)(plan)
        assert done["exit_code"] == 0 and "Total sweeps: 3" in done["stderr"]
        m = occupancy.parse(text, plan)
        assert m.passes == 3 and m.partial == 0 and m.bad_lines == 0 and m.mean_db.size == plan.bins
        # The fake's weather carrier at 162.4 MHz stands well above its floor.
        f = plan.lo_hz + (np.arange(plan.bins) + 0.5) * plan.bin_hz
        assert m.mean_db[np.argmin(np.abs(f - 162.4e6))] > np.median(m.mean_db) + 20
    finally:
        srv.shutdown()
        srv.server_close()


def test_sweeps_on_this_machine(tmp_path, sweep_tools):
    cli.set_bin_dir(sweep_tools)
    try:
        live = settings.LiveSettings(live_dir=tmp_path / "live")
        text, info = occupancy.sweep_runner(live)(SMALL)
        assert info["exit_code"] == 0 and occupancy.parse(text, SMALL).passes == 2
    finally:
        cli.set_bin_dir(None)


def test_start_occupancy_opens_the_database_and_reads_alerts_from_the_web_state(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKRF_WEB_STATE_DIR", raising=False)
    live = settings.LiveSettings(live_dir=tmp_path / "data" / "live", min_free_mb=0, occupancy=True)
    s = LiveService(live, None, make_pipeline=FakePipeline)
    sched = start_occupancy(s, live, run_sweep=good_sweep(SMALL))
    try:
        assert sched.alerts_db == tmp_path / "data" / "web" / "alerts.db"
        assert live.occupancy_db_path.exists() and sched.interval_s == 900 and sched.plan == Plan()
        assert s.status()["occupancy"]["interval_min"] == 15
    finally:
        assert sched.stop()
        sched.writer.close()


# --- settings -------------------------------------------------------------------


def test_occupancy_settings_from_the_environment():
    d = settings.LiveSettings.from_env({})
    assert d.occupancy is False and d.occupancy_interval_min == 15 and d.occupancy_range_mhz == (24, 1700)
    assert (d.occupancy_bin_hz, d.occupancy_passes, d.occupancy_lna_gain_db, d.occupancy_vga_gain_db) == (100_000, 20, 24, 40)
    assert (d.occupancy_max_age_days, d.occupancy_daily_days, d.occupancy_late_min, d.occupancy_alert_quiet_min) == (365, 1826, 10, 10)
    s = settings.LiveSettings.from_env(
        {
            "HACKRF_LIVE_OCCUPANCY": "on",
            "HACKRF_LIVE_OCCUPANCY_INTERVAL_MIN": "30",
            "HACKRF_LIVE_OCCUPANCY_RANGE_MHZ": "50-1100",
            "HACKRF_LIVE_OCCUPANCY_PASSES": "5",
            "HACKRF_LIVE_OCCUPANCY_ALERT_QUIET_MIN": "0",
        }
    )
    assert s.occupancy and s.occupancy_interval_min == 30 and s.occupancy_range_mhz == (50, 1100)
    assert s.occupancy_passes == 5 and s.occupancy_alert_quiet_min == 0
    for env, match in (
        ({"HACKRF_LIVE_OCCUPANCY_INTERVAL_MIN": "7"}, "divide a day"),
        ({"HACKRF_LIVE_OCCUPANCY_RANGE_MHZ": "1700-24"}, "low to high"),
        ({"HACKRF_LIVE_OCCUPANCY_RANGE_MHZ": "24"}, "LOW-HIGH"),
        ({"HACKRF_LIVE_OCCUPANCY_LNA_GAIN": "20"}, "steps of 8"),
        ({"HACKRF_LIVE_OCCUPANCY_LATE_MIN": "15"}, "under the interval"),
        ({"HACKRF_LIVE_OCCUPANCY_PASSES": "0"}, "1-200"),
    ):
        with pytest.raises(ValueError, match=match):
            settings.LiveSettings.from_env(env)


# --- the web API ------------------------------------------------------------------

TOKEN = "correct-horse-battery-staple-42"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def web(tmp_path):
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    gate = web_app.create_app(
        live, settings.WebSettings(), [hashlib.sha256(TOKEN.encode()).digest()], b"k" * 32,
        control=lambda req, timeout=20.0: {"ok": True, "state": "running"}, feed=False, own_stats=lambda: None,
    )  # fmt: skip
    return live, TestClient(gate)


def test_the_occupancy_api(web):
    live, c = web
    assert c.get("/api/occupancy", headers=BEARER).status_code == 404
    w = OccupancyWriter(live.occupancy_db_path, 365 * DAY, 1826 * DAY, 1 << 30, 0)
    for i in range(4):
        w.add(T0 + i * 900, T0 + i * 900, 4.0, SMALL, measured(SMALL, -80 + i))
    w.close()
    q = {"since": str(T0), "until": str(T0 + HOUR)}
    g = c.get("/api/occupancy", params={**q, "tbuckets": 4, "fbuckets": 8}, headers=BEARER).json()
    assert g["rows"] == 4 and len(base64.b64decode(g["mean"])) == 4 * 8 and g["grid"]["bins"] == SMALL.bins
    zoom = c.get("/api/occupancy", params={**q, "fmin_hz": "30e6", "fmax_hz": "40e6"}, headers=BEARER).json()
    assert zoom["f_lo_hz"] == 30e6 and zoom["fbuckets"] == 10
    assert c.get("/api/occupancy", params={**q, "fmin_hz": "x"}, headers=BEARER).status_code == 400
    p = c.get("/api/occupancy/profile", params={**q, "by": "hour", "fbuckets": 2}, headers=BEARER).json()
    assert p["counts"][0] == 4 and len(base64.b64decode(p["mean"])) == 24 * 2
    assert c.get("/api/occupancy/profile", params={**q, "by": "year"}, headers=BEARER).status_code == 400
    u = c.get("/api/occupancy/unusual", params=q, headers=BEARER).json()
    assert u["slots"] == 4 and u["no_baseline"] == 4 and u["items"] == []
    cell = c.get("/api/occupancy/cell", params={"ts": str(T0 + 950), "fmin_hz": "24e6", "fmax_hz": "29e6"}, headers=BEARER).json()
    assert cell["slot"]["ts"] == T0 + 900 and cell["bins"] == 5 and cell["usual_db"] is None
    assert c.get("/api/occupancy/cell", params={"ts": str(T0)}, headers=BEARER).status_code == 400
    assert c.get("/api/occupancy/cell", params={"ts": "0", "fmin_hz": "24e6", "fmax_hz": "29e6"}, headers=BEARER).status_code == 404
    s = c.get("/api/occupancy/slots", params=q, headers=BEARER).json()["slots"]
    assert [r["ts"] for r in s] == [T0 + i * 900 for i in range(4)] and "mean" not in s[0]
    assert c.get("/api/occupancy").status_code == 401
