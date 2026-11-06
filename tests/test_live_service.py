import threading
from pathlib import Path

import pytest

from hackrf_mcp import live_client, server, settings
from hackrf_mcp.live_log import LiveLogWriter, Row
from hackrf_mcp.live_service import ControlServer, LiveService


class FakePipeline:
    def __init__(self, tuning, ctx):
        self.tuning = tuning
        self.ctx = ctx
        self.source = None
        self.running = False
        self.stopped = False
        self.fail = None

    def start(self):
        self.running = True

    def stop(self):
        self.running = False
        self.stopped = True

    def failure(self):
        return self.fail

    def health(self):
        return {}

    def stderr_tail(self):
        return []


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def svc(tmp_path):
    pipes = []

    def make(tuning, ctx):
        p = FakePipeline(tuning, ctx)
        pipes.append(p)
        return p

    clock = Clock()
    live = settings.LiveSettings(live_dir=tmp_path / "live")
    s = LiveService(live, None, make_pipeline=make, program_finder=lambda p: "/bin/true", clock=clock)
    s.pipes = pipes
    s.test_clock = clock
    return s


def test_start_switch_and_stop(svc):
    r = svc.handle({"cmd": "start", "profile": "adsb"})
    assert r["ok"] and r["state"] == "running" and r["profile"] == "adsb"
    r = svc.handle({"cmd": "start", "profile": "flex", "freqs_hz": [931_750_000]})
    assert r["profile"] == "flex"
    assert r["tuning_detail"]["freqs_hz"] == [931_750_000] and r["tuning_detail"]["sample_rate_hz"] > 0
    assert svc.pipes[0].stopped and svc.pipes[1].running
    r = svc.handle({"cmd": "stop"})
    assert r["state"] == "idle" and svc.pipes[1].stopped


def test_bad_requests_are_refused_with_a_reason(svc):
    r = svc.handle({"cmd": "start", "profile": "flex"})
    assert not r["ok"] and "frequencies_hz" in r["error"]
    assert not svc.handle({"cmd": "start", "profile": "nope"})["ok"]
    assert not svc.handle({"cmd": "frobnicate"})["ok"]
    assert svc.pipeline is None


def test_the_profile_survives_a_restart(svc, tmp_path):
    svc.handle({"cmd": "start", "profile": "ais"})
    again = LiveService(svc.live, None, make_pipeline=FakePipeline, program_finder=lambda p: "/bin/true")
    again.load()
    again.supervise()
    assert again.running_spec.profile == "ais"


def test_pause_releases_the_device_and_resume_restarts(svc):
    svc.handle({"cmd": "start", "profile": "adsb"})
    first = svc.pipes[0]
    r = svc.handle({"cmd": "pause", "owner": "hackrf_transfer", "lease_s": 60})
    assert r["paused_profile"] == "adsb" and first.stopped
    svc.supervise()
    assert svc.pipeline is None and svc.status()["state"] == "paused"
    assert svc.handle({"cmd": "resume", "token": "wrong"})["resumed"] is False
    assert svc.handle({"cmd": "resume", "token": r["token"]})["resumed"] is True
    svc.supervise()
    assert svc.status()["state"] == "running" and len(svc.pipes) == 2


def test_a_pause_lease_runs_out_by_itself(svc):
    svc.handle({"cmd": "start", "profile": "adsb"})
    svc.handle({"cmd": "pause", "owner": "x", "lease_s": 10})
    svc.test_clock.t += 5
    svc.supervise()
    assert svc.pipeline is None
    svc.test_clock.t += 6
    svc.supervise()
    assert svc.status()["state"] == "running"


def test_pause_while_idle_holds_off_a_start(svc):
    r = svc.handle({"cmd": "pause", "owner": "x", "lease_s": 60})
    assert r["paused_profile"] is None
    svc.handle({"cmd": "start", "profile": "adsb"})
    assert svc.pipeline is None and svc.status()["state"] == "paused"
    svc.handle({"cmd": "resume", "token": r["token"]})
    svc.supervise()
    assert svc.status()["state"] == "running"


def test_a_failed_profile_is_retried_with_backoff(svc):
    svc.handle({"cmd": "start", "profile": "adsb"})
    svc.pipes[0].fail = "hackrf_transfer exited with code 1: resource busy"
    svc.supervise()
    st = svc.status()
    assert st["state"] == "waiting" and "resource busy" in st["last_error"]
    svc.supervise()
    assert len(svc.pipes) == 1  # not before the retry time
    svc.test_clock.t += 3
    svc.supervise()
    assert len(svc.pipes) == 2 and svc.status()["state"] == "running"


def test_a_schedule_rotates_profiles(svc):
    r = svc.handle({"cmd": "start", "schedule": [{"profile": "adsb", "minutes": 1}, {"profile": "ais", "minutes": 2}]})
    assert r["profile"] == "adsb" and r["next_switch"] == pytest.approx(svc.test_clock.t + 60)
    svc.test_clock.t += 61
    svc.supervise()
    assert svc.running_spec.profile == "ais"
    svc.test_clock.t += 120
    svc.supervise()
    assert svc.running_spec.profile == "adsb"
    assert not svc.handle({"cmd": "start", "schedule": [{"profile": "adsb", "minutes": 1}]})["ok"]


def test_a_watch_lends_the_receiver_and_the_service_takes_it_back(svc):
    svc.handle({"cmd": "start", "schedule": [{"profile": "adsb", "minutes": 10}, {"profile": "ais", "minutes": 10}]})
    r = svc.handle({"cmd": "watch", "center_hz": 98_000_000, "sample_rate_hz": 10_000_000, "minutes": 5})
    assert r["ok"] and r["state"] == "running" and r["profile"] == "watch"
    assert r["watch"]["center_hz"] == 98_000_000 and r["watch"]["left_s"] == pytest.approx(300)
    assert r["tuning_detail"]["center_hz"] == 98_000_000 and r["tuning_detail"]["sample_rate_hz"] == 10_000_000
    assert "93.000-103.000 MHz" in r["tuning"]
    assert svc.pipes[0].stopped and svc.pipes[1].tuning.profile.spectrum_only
    # Scrolling retunes and keeps the time limit.
    svc.test_clock.t += 120
    r = svc.handle({"cmd": "watch", "center_hz": 103_000_000, "sample_rate_hz": 10_000_000})
    assert r["watch"]["left_s"] == pytest.approx(180) and svc.pipes[2].tuning.center_hz == 103_000_000
    # The limit runs out in the service, page or no page: the rotation is back.
    svc.test_clock.t += 181
    svc.supervise()
    st = svc.status()
    assert st["watch"] is None and st["profile"] == "adsb" and svc.pipes[2].stopped


def test_a_split_station_worker_watches_with_no_hackrf_tools(tmp_path, monkeypatch):
    """The worker of a split station has no hackrf_transfer (its agent runs
    it), so a watch must not ask for one. The real program lookup here, not
    the fixture's stub, which is how this went unnoticed."""
    from hackrf_mcp.live_profiles import RemoteSource

    monkeypatch.setenv("PATH", str(tmp_path))  # nothing installed
    tok = tmp_path / "agent-token"
    tok.write_text("t" * 40)
    live = settings.LiveSettings.from_env({
        "HACKRF_LIVE_DIR": str(tmp_path / "live"),
        "HACKRF_LIVE_AGENT": "192.168.3.99:8767",
        "HACKRF_LIVE_AGENT_TOKEN_FILE": str(tok),
    })
    pipes = []
    s = LiveService(live, None, make_pipeline=lambda t, c: pipes.append(FakePipeline(t, c)) or pipes[-1], clock=Clock())
    r = s.handle({"cmd": "watch", "center_hz": 915_000_000, "sample_rate_hz": 10_000_000, "minutes": 5})
    assert r["ok"], r
    assert r["profile"] == "watch" and pipes[-1].tuning.center_hz == 915_000_000
    # Its samples come from the agent, as every profile's do there.
    src = pipes[-1].ctx.make_source(pipes[-1].tuning)
    assert isinstance(src, RemoteSource) and src.request["center_hz"] == 915_000_000


def test_stop_start_and_watch_stop_end_a_watch(svc):
    svc.handle({"cmd": "start", "profile": "adsb"})
    svc.handle({"cmd": "watch", "center_hz": 162_000_000, "sample_rate_hz": 2_000_000})
    assert svc.status()["profile"] == "watch"
    r = svc.handle({"cmd": "watch-stop"})
    assert r["watch"] is None and r["profile"] == "adsb"
    svc.handle({"cmd": "watch", "center_hz": 162_000_000})
    r = svc.handle({"cmd": "start", "profile": "ais"})
    assert r["watch"] is None and r["profile"] == "ais"
    svc.handle({"cmd": "watch", "center_hz": 162_000_000})
    r = svc.handle({"cmd": "stop"})
    assert r["watch"] is None and r["state"] == "idle"


def test_bad_watches_are_refused_with_a_reason(svc):
    assert "outside" in svc.handle({"cmd": "watch", "center_hz": 2_000_000, "sample_rate_hz": 10_000_000})["error"]
    assert "outside" in svc.handle({"cmd": "watch", "center_hz": 5_999_000_000, "sample_rate_hz": 10_000_000})["error"]
    assert "wide" in svc.handle({"cmd": "watch", "center_hz": 100_000_000, "sample_rate_hz": 3_000_000})["error"]
    assert "minutes" in svc.handle({"cmd": "watch", "center_hz": 100_000_000, "minutes": 90})["error"]
    assert not svc.handle({"cmd": "watch"})["ok"]
    assert "watch command" in svc.handle({"cmd": "start", "profile": "watch"})["error"]
    rot = [{"profile": "watch", "minutes": 1}, {"profile": "adsb", "minutes": 1}]
    assert "watch command" in svc.handle({"cmd": "start", "schedule": rot})["error"]
    assert svc.pipeline is None and svc.watch is None


def test_a_device_tool_pauses_a_watch_and_a_restart_forgets_it(svc):
    svc.handle({"cmd": "start", "profile": "adsb"})
    svc.handle({"cmd": "watch", "center_hz": 98_000_000})
    p = svc.handle({"cmd": "pause", "owner": "hackrf_sweep", "lease_s": 30})
    assert p["paused_profile"] == "watch"
    svc.handle({"cmd": "resume", "token": p["token"]})
    svc.supervise()
    assert svc.status()["profile"] == "watch"
    again = LiveService(svc.live, None, make_pipeline=FakePipeline, program_finder=lambda p: "/bin/true")
    again.load()
    again.supervise()
    assert again.watch is None and again.running_spec.profile == "adsb"


def test_the_mcp_status_says_when_a_watch_has_the_receiver(svc):
    svc.handle({"cmd": "start", "schedule": [{"profile": "adsb", "minutes": 10}, {"profile": "ais", "minutes": 10}]})
    st = svc.handle({"cmd": "watch", "center_hz": 98_000_000, "minutes": 5})
    text = server._format_live_status({**st, "now": svc.test_clock.t})
    assert "Watching 93.000-103.000 MHz" in text and "decoding resumes" in text


def test_rows_are_counted(svc):
    svc._emit(Row(svc.test_clock.t, "adsb", "aircraft", {}))
    st = svc.status()
    assert st["rows_10m"] == 1 and st["rows_by_profile"] == {"adsb": 1}


# --- The socket, the client and the MCP server --------------------------------


@pytest.fixture
def served(svc, tmp_path, monkeypatch):
    # Unix socket paths are limited to ~108 bytes; use a short relative one.
    monkeypatch.chdir(tmp_path)
    live = settings.LiveSettings(live_dir=Path("live"))
    Path("live").mkdir()
    settings.configure_live(live)
    svc.live = live
    srv = ControlServer(live.socket_path, svc)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield svc
    srv.shutdown()
    srv.server_close()


def test_client_round_trip(served):
    assert live_client.request({"cmd": "status"})["state"] == "idle"
    with pytest.raises(live_client.LiveError, match="frequencies_hz"):
        live_client.request({"cmd": "start", "profile": "pocsag"})


def test_client_without_a_service():
    with pytest.raises(live_client.Unavailable):
        live_client.request({"cmd": "status"})


def test_device_tools_pause_and_resume_live_decoding(served, monkeypatch):
    calls = []

    def fake_run(binary, args, timeout=None):
        calls.append(served.status()["state"])
        return "Found HackRF"

    monkeypatch.setattr(server, "run", fake_run)
    served.handle({"cmd": "start", "profile": "adsb"})
    out = server.hackrf_info()
    assert calls == ["paused"]
    assert out.startswith("Found HackRF") and "Live decoding of adsb was paused" in out
    served.supervise()
    assert served.status()["state"] == "running"


def test_device_tools_are_unchanged_without_the_service(monkeypatch):
    monkeypatch.setattr(server, "run", lambda binary, args, timeout=None: "Found HackRF")
    assert server.hackrf_info() == "Found HackRF"


def test_device_tools_refuse_when_the_service_will_not_let_go(monkeypatch):
    def stuck(owner, lease_s):
        raise live_client.LiveError("did not answer within 20 s")

    monkeypatch.setattr(live_client, "pause", stuck)
    monkeypatch.setattr(server, "run", lambda *a, **k: pytest.fail("must not open the device"))
    out = server.hackrf_info()
    assert out.startswith("REFUSED:") and "hackrf_live_stop" in out


def test_live_tools_through_the_socket(served):
    out = server.hackrf_live_start(profile="flex", frequencies_hz=[931_750_000])
    assert "RUNNING flex" in out and "not logged (metadata only)" in out
    assert "PAUSED" not in out
    assert "IDLE" in server.hackrf_live_stop()
    assert "Give a profile" in server.hackrf_live_start()
    assert "Not started" in server.hackrf_live_start(profile="flex")
    assert "running" not in server._live_one_line() and "idle" in server._live_one_line()


def test_live_status_without_the_service():
    assert server.hackrf_live_status() == server.LIVE_NOT_RUNNING
    assert "not running" in server.hackrf_server_status()


def test_live_search_and_summary_read_the_log(tmp_path):
    db = settings.get_live().db_path
    w = LiveLogWriter(db, 86400 * 365, 1 << 30, 0)
    w.write(
        [
            Row(1_000.0, "adsb", "aircraft", {"icao": "A1B2C3", "callsign": "UAL123", "messages": 9}, "A1B2C3 UAL123",
                1_090_000_000, -12.5, "A1B2C3", "UAL123", 1_030.0),
            Row(1_100.0, "flex", "page", {"capcodes": ["499030"], "type": "ALN"}, "FLEX|t|m|f|000499030|ALN|[content not logged]",
                931_750_000, -45.0, "499030", "ALN"),
        ]
    )
    w.close()
    out = server.hackrf_live_search(since="0", subject="ual123")
    assert "1 row(s)" in out and "adsb/aircraft A1B2C3 (UAL123)" in out and "-12.5 dB" in out
    assert "No rows" in server.hackrf_live_search(since="1h")
    out = server.hackrf_live_search(since="1970-01-01T00:00:00Z", profile="flex", subject="000499030")
    assert "content not logged" in out
    out = server.hackrf_live_summary("adsb", since="0")
    assert "1 aircraft" in out and "callsign=UAL123" in out and "messages=9" in out
    assert "Nothing logged" in server.hackrf_live_summary("ais", since="0")
    assert "can't read the time" in server.hackrf_live_search(since="yesterday-ish")


def test_live_search_without_a_log():
    assert "has not written one" in server.hackrf_live_search()


def test_parse_when():
    now = 10_000.0
    assert server.parse_when("30m", now) == now - 1800
    assert server.parse_when("2h", now) == now - 7200
    assert server.parse_when("1.5d", now) == now - 1.5 * 86400
    assert server.parse_when("1793300000", now) == 1793300000.0
    assert server.parse_when("1970-01-01T01:00:00Z", now) == 3600.0
    assert server.parse_when("1970-01-01T01:00:00", now) == 3600.0
    assert server.parse_when(None, now) is None


# --- day plans (live_day.py) -------------------------------------------------

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

DAY_ZONE = "America/Chicago"


def _local(*args) -> float:
    return datetime(*args, tzinfo=ZoneInfo(DAY_ZONE)).timestamp()


def _day_req(zone=DAY_ZONE):
    return {
        "cmd": "start",
        "day": {
            "zone": zone,
            "parts": [
                {"name": "day", "start": "06:00", "schedule": [{"profile": "ais", "minutes": 15}, {"profile": "adsb", "minutes": 45}]},
                {"name": "night", "start": "23:00", "schedule": [{"profile": "aprs", "minutes": 30}, {"profile": "adsb", "minutes": 30}]},
            ],
        },
    }


def test_a_day_plan_switches_parts_on_the_local_clock(svc):
    events = []
    svc._event = lambda e, p=None, d=None: events.append((e, p, d))
    svc.test_clock.t = _local(2026, 11, 6, 22, 50)
    r = svc.handle(_day_req())
    assert r["ok"], r
    # 22:50 is in the day part's adsb step, which the part cuts at 23:00.
    assert r["profile"] == "adsb" and r["next_switch"] == pytest.approx(_local(2026, 11, 6, 23, 0))
    assert r["day"]["part"]["name"] == "day" and r["day"]["next_part"] == {
        "index": 1, "name": "night", "start": "23:00", "at": pytest.approx(_local(2026, 11, 6, 23, 0)),
    }
    assert r["desired"]["day"]["zone"] == DAY_ZONE and [p["start"] for p in r["desired"]["day"]["parts"]] == ["06:00", "23:00"]
    assert ("part", None, f"day: 06:00-23:00 {DAY_ZONE}") in events
    # At 23:00 the night starts at its first step.
    svc.test_clock.t = _local(2026, 11, 6, 23, 0)
    svc.supervise()
    st = svc.status()
    assert st["profile"] == "aprs" and st["day"]["part"]["name"] == "night"
    assert st["next_switch"] == pytest.approx(_local(2026, 11, 6, 23, 30))
    assert ("part", None, f"night: 23:00-06:00 {DAY_ZONE}") in events
    assert [e for e in events if e[0] == "part"] == [("part", None, f"day: 06:00-23:00 {DAY_ZONE}"), ("part", None, f"night: 23:00-06:00 {DAY_ZONE}")]
    # The strip covers the local day; the night part shows at both ends.
    today = st["day"]["today"]
    assert today["from"] == pytest.approx(_local(2026, 11, 6, 0, 0))
    assert [s["name"] for s in today["segments"]] == ["night", "day", "night"]
    assert today["share"]["adsb"] == pytest.approx((17 * 45 / 60 + 7 * 0.5) / 24)
    # Past midnight it is still the night until 06:00, then the day at its first step.
    svc.test_clock.t = _local(2026, 11, 7, 5, 59)
    svc.supervise()
    assert svc.running_spec.profile == "adsb"
    svc.test_clock.t = _local(2026, 11, 7, 6, 0)
    svc.supervise()
    assert svc.running_spec.profile == "ais" and svc.status()["day"]["part"]["name"] == "day"


def test_a_hold_across_a_part_change_finishes_then_the_new_part_runs(svc):
    svc.test_clock.t = _local(2026, 11, 6, 22, 59)
    svc.handle(_day_req())
    r = svc.handle({"cmd": "pause", "owner": "hackrf_transfer", "lease_s": 120})
    svc.test_clock.t = _local(2026, 11, 6, 23, 0, 30)
    svc.supervise()
    assert svc.pipeline is None and svc.status()["state"] == "paused"
    assert svc.status()["day"]["part"]["name"] == "night"
    svc.handle({"cmd": "resume", "token": r["token"]})
    svc.supervise()
    assert svc.running_spec.profile == "aprs"


def test_a_watch_across_a_part_change_runs_to_its_own_end(svc):
    svc.test_clock.t = _local(2026, 11, 6, 22, 58)
    svc.handle(_day_req())
    svc.handle({"cmd": "watch", "center_hz": 98_000_000, "sample_rate_hz": 10_000_000, "minutes": 5})
    svc.test_clock.t = _local(2026, 11, 6, 23, 1)
    svc.supervise()
    st = svc.status()
    assert st["profile"] == "watch" and st["day"]["part"]["name"] == "night"
    assert st["next_switch"] == pytest.approx(_local(2026, 11, 6, 23, 3))
    svc.test_clock.t = _local(2026, 11, 6, 23, 3)
    svc.supervise()
    assert svc.running_spec.profile == "aprs"


def test_a_day_plan_survives_a_restart_and_the_old_forms_still_load(svc, tmp_path):
    svc.test_clock.t = _local(2026, 11, 6, 23, 10)
    svc.handle(_day_req())
    again = LiveService(svc.live, None, make_pipeline=FakePipeline, program_finder=lambda p: "/bin/true", clock=svc.test_clock)
    again.load()
    again.supervise()
    assert again.running_spec.profile == "aprs" and again.status()["day"]["part"]["name"] == "night"
    # A rotation saved before day plans existed keeps its anchor and phase.
    import json
    svc.live.state_path.write_text(json.dumps({"desired": {
        "schedule": [{"spec": {"profile": "adsb"}, "minutes": 10}, {"spec": {"profile": "ais"}, "minutes": 10}],
        "anchor": svc.test_clock.t - 15 * 60,
    }}))
    old = LiveService(svc.live, None, make_pipeline=FakePipeline, program_finder=lambda p: "/bin/true", clock=svc.test_clock)
    old.load()
    old.supervise()
    st = old.status()
    assert st["profile"] == "ais" and st["day"] is None and st["desired"]["anchor"] == svc.test_clock.t - 15 * 60
    assert st["next_switch"] == pytest.approx(svc.test_clock.t + 5 * 60)


def test_a_saved_plan_whose_zone_is_gone_says_which(svc):
    import json
    events = []
    svc.handle(_day_req())
    state = json.loads(svc.live.state_path.read_text())
    state["desired"]["day"]["zone"] = "Gone/Away"
    svc.live.state_path.write_text(json.dumps(state))
    again = LiveService(svc.live, None, make_pipeline=FakePipeline, program_finder=lambda p: "/bin/true")
    again._event = lambda e, p=None, d=None: events.append((e, d))
    again.load()
    assert again.desired is None
    assert any(e == "error" and "Gone/Away" in d for e, d in events)


def test_bad_day_plans_are_refused_with_a_reason(svc):
    def refused(req):
        r = svc.handle(req)
        assert not r["ok"], r
        return r["error"]

    assert "unknown time zone" in refused(_day_req(zone="Nowhere/Land"))
    bad = _day_req()
    bad["day"]["parts"][1]["start"] = "25:00"
    assert "like 23:00" in refused(bad)
    bad = _day_req()
    bad["day"]["parts"][1]["schedule"][0]["minutes"] = 0
    assert "1-1440" in refused(bad)
    bad = _day_req()
    bad["day"]["parts"][1]["schedule"] = [{"profile": "flex", "minutes": 10}]
    assert "frequencies_hz" in refused(bad)
    bad = _day_req()
    bad["day"]["parts"] = bad["day"]["parts"][:1]
    assert "at least two parts" in refused(bad)
    bad = _day_req()
    bad["profile"] = "adsb"
    assert "one of profile, schedule or day" in refused(bad)
    assert svc.desired is None and svc.pipeline is None


def test_a_single_profile_part_runs_all_part_long(svc):
    req = _day_req()
    req["day"]["parts"][1]["schedule"] = [{"profile": "aprs", "minutes": 60}]
    svc.test_clock.t = _local(2026, 11, 6, 23, 0)
    r = svc.handle(req)
    assert r["profile"] == "aprs" and r["next_switch"] == pytest.approx(_local(2026, 11, 7, 6, 0))


def test_a_day_plan_through_the_mcp_tool(served):
    served.test_clock.t = _local(2026, 11, 6, 23, 5)
    E = server.LiveScheduleEntry
    day = server.LiveDay(
        zone=DAY_ZONE,
        parts=[
            server.LiveDayPart(name="day", start="06:00", schedule=[E(profile="ais", minutes=15), E(profile="adsb", minutes=45)]),
            server.LiveDayPart(name="night", start="23:00", schedule=[E(profile="aprs", minutes=30, frequencies_hz=[144_390_000]), E(profile="adsb", minutes=30)]),
        ],
    )
    out = server.hackrf_live_start(day=day)
    assert "RUNNING aprs" in out
    assert f"Day plan ({DAY_ZONE} time, every day): day from 06:00 (ais 15 min -> adsb 45 min, repeating); night from 23:00 (aprs 144.39 30 min -> adsb 30 min, repeating)." in out
    assert "Now: night until " in out and "(06:00 local), then day" in out
    assert "Today by the plan: adsb 68%, ais 18%, aprs 15% of the day." in out
    assert "Give one of profile, schedule or day" in server.hackrf_live_start(profile="adsb", day=day)
    bad = day.model_copy(update={"zone": "Nowhere/Land"})
    assert "Not started" in server.hackrf_live_start(day=bad) and "unknown time zone" in server.hackrf_live_start(day=bad)
