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
