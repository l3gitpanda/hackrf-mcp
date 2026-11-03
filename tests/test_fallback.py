"""The split station's fallback: the agent holds its machine's own live
service paused while a worker is around and lets go when none is; the worker
imports what that service logged meanwhile."""

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from hackrf_mcp import agent, live_client, live_import, settings
from hackrf_mcp.live_log import LiveLogWriter, Position, Row
from hackrf_mcp.live_profiles import RemoteSource
from hackrf_mcp.live_service import ControlServer, LiveService, LogPump
from test_agent import FAKE, INFO, SWEEP, TOKEN, addr, wait_for
from test_live_service import Clock, FakePipeline


@pytest.fixture
def bins(tmp_path):
    """The fake hackrf_transfer, hackrf_info and hackrf_sweep of test_agent."""
    d = tmp_path / "bin"
    d.mkdir()
    for name, body in (("hackrf_transfer", FAKE), ("hackrf_info", INFO), ("hackrf_sweep", SWEEP)):
        (d / name).write_text(f"#!{sys.executable}\n" + body)
        (d / name).chmod(0o755)
    return d


def _agent_server(bins, fallback_dir=None):
    s = agent.AgentSettings(
        host="127.0.0.1",
        port=0,
        token_sha256=(hashlib.sha256(TOKEN.encode()).hexdigest(),),
        binary=str(bins / "hackrf_transfer"),
        tools_dir=str(bins),
        fallback_live_dir=fallback_dir,
    )
    s.digests = s.load_digests()
    srv = agent.AgentServer(s)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _close(srv):
    srv.shutdown()
    with srv.agent.lock:
        cur = srv.agent.current
    if cur is not None:
        cur.finish("test over")
        cur.stop_child()
    srv.server_close()


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # short relative paths for the control socket
    return tmp_path


@pytest.fixture
def local(here):
    """The agent machine's own live service, decoding adsb, on a test clock."""
    pipes, events = [], []

    def make(tuning, ctx):
        p = FakePipeline(tuning, ctx)
        pipes.append(p)
        return p

    live = settings.LiveSettings(live_dir=Path("qnap"), min_free_mb=0)
    live.live_dir.mkdir()
    svc = LiveService(live, None, make_pipeline=make, program_finder=lambda p: "/bin/true", clock=Clock())
    record = svc._event
    svc._event = lambda event, profile=None, detail=None: (events.append((event, detail)), record(event, profile, detail))
    svc.pipes, svc.events = pipes, events
    svc.handle({"cmd": "start", "profile": "adsb"})
    ctl = ControlServer(live.socket_path, svc)
    threading.Thread(target=ctl.serve_forever, daemon=True).start()
    yield svc
    ctl.shutdown()
    ctl.server_close()


@pytest.fixture
def fb(bins, local):
    srv = _agent_server(bins, Path("qnap"))
    yield srv
    _close(srv)


def test_a_stream_holds_the_local_service_before_it_opens_the_hackrf(fb, local):
    assert local.status()["state"] == "running"
    got = bytearray()
    src = RemoteSource(addr(fb), TOKEN, 98_000_000, 2_000_000, 32, 40, False)
    src.start(got.extend)
    try:
        assert wait_for(lambda: len(got) > 0)
        assert local.hold is not None and local.hold["owner"] == agent.FALLBACK_OWNER
        assert local.pipes[0].stopped
        st = agent.status(addr(fb), TOKEN)["fallback"]
        assert st["held"] and st["lease_until"] > st["held_since"]
    finally:
        src.stop()


def test_a_heartbeat_keeps_the_local_service_held_and_its_absence_lets_go(fb, local):
    f = fb.agent.fallback
    f.after_s = 0.3
    f.tick()  # no worker yet
    assert local.hold is None and local.status()["state"] == "running"
    agent.status(addr(fb), TOKEN, heartbeat=True)
    f.tick()
    assert local.hold is not None and fb.agent.last_heartbeat is not None
    local.supervise()
    assert local.status()["state"] == "paused"
    time.sleep(0.4)
    f.tick()  # no heartbeat for longer than after_s: decode locally again
    assert local.hold is None and not agent.status(addr(fb), TOKEN)["fallback"]["held"]
    local.supervise()
    assert local.status()["state"] == "running" and len(local.pipes) == 2
    assert [e for e, _ in local.events].count("resume") == 1


def test_a_plain_status_request_is_not_a_heartbeat(fb, local):
    agent.status(addr(fb), TOKEN)
    fb.agent.fallback.tick()
    assert fb.agent.last_heartbeat is None and local.hold is None


def test_renewing_the_hold_logs_one_pause_not_one_a_minute(fb, local, monkeypatch):
    monkeypatch.setattr(agent, "FALLBACK_RENEW_S", 0.0)
    agent.status(addr(fb), TOKEN, heartbeat=True)
    tokens = set()
    for _ in range(3):
        fb.agent.fallback.tick()
        tokens.add(local.hold["token"])
    assert len(tokens) == 3  # each renewal is a fresh lease
    assert [e for e, _ in local.events].count("pause") == 1


def test_if_the_agent_dies_the_local_service_resumes_within_the_lease(fb, local):
    agent.status(addr(fb), TOKEN, heartbeat=True)
    fb.agent.fallback.tick()
    local.supervise()
    assert local.status()["state"] == "paused"
    # No more renewals: the agent is gone. The lease runs out by itself.
    local.clock.t += agent.FALLBACK_LEASE_S - 1
    local.supervise()
    assert local.status()["state"] == "paused"
    local.clock.t += 2
    local.supervise()
    assert local.status()["state"] == "running"
    assert ("resume", f"pause by {agent.FALLBACK_OWNER} expired") in local.events


def test_a_local_service_that_will_not_let_go_refuses_the_stream(fb, local, monkeypatch):
    def refuse(req, timeout=20.0, path=None):
        raise live_client.LiveError("the live decoder service did not answer within 20 s")

    monkeypatch.setattr(agent.live_client, "request", refuse)
    src = RemoteSource(addr(fb), TOKEN, 98_000_000, 2_000_000, 32, 40, False)
    src.start(lambda b: None)
    try:
        assert wait_for(lambda: src.failure() is not None)
        assert "own live service did not let go" in src.failure()
        assert fb.agent.current is None  # hackrf_transfer never started
    finally:
        src.stop()


def test_with_no_local_service_running_there_is_nothing_to_hold(bins, here):
    srv = _agent_server(bins, Path("absent"))
    try:
        got = bytearray()
        src = RemoteSource(addr(srv), TOKEN, 98_000_000, 2_000_000, 32, 40, False)
        src.start(got.extend)
        assert wait_for(lambda: len(got) > 0)
        assert agent.status(addr(srv), TOKEN)["fallback"]["held"] is False
        src.stop()
    finally:
        _close(srv)


# --- export and import --------------------------------------------------------


def _log(path: Path, t0: float, n: int, events=("start",)):
    w = LiveLogWriter(path, 30 * 86400, 1 << 30, 0)
    w.write([Row(t0 + i, "adsb", "aircraft", {"alt": 1000 + i}, subject=f"a{i}") for i in range(n)])
    w.write([Row(t0, "flex", "page", {"capcode": "1", "message": "MEET AT 3"}, "FLEX|h|MEET AT 3", subject="1")])
    w.write_positions([Position(t0 + 0.5, "adsb", "a0", 33.1, -117.2, alt=1000.0)])
    for e in events:
        w.event(e, "adsb", "for a test", ts=t0 + 0.25)
    w.close()


class FakePump:
    def __init__(self):
        self.rows, self.positions, self.events = [], [], []

    def row(self, r):
        self.rows.append(r)

    def position(self, p):
        self.positions.append(p)

    def event(self, event, profile, detail, ts=None):
        self.events.append((event, profile, detail, ts))

    def after_write(self, fn):
        fn()


@pytest.fixture
def exporter(bins, here):
    Path("qnap").mkdir()
    srv = _agent_server(bins, Path("qnap"))
    tok = here / "agent-token"
    tok.write_text(TOKEN + "\n")
    yield srv, tok
    _close(srv)


def _link(srv, tok, pump, pager_content=False):
    return live_import.AgentLink(pump, addr(srv), tok, Path("agent-import.json"), pager_content)


def test_first_contact_records_the_ids_and_imports_nothing_then_new_rows_come_across(exporter):
    srv, tok = exporter
    _log(Path("qnap/live.db"), 1_000.0, 3)
    pump = FakePump()
    link = _link(srv, tok, pump)
    link.round()
    assert pump.rows == [] and srv.agent.last_heartbeat is not None
    assert json.loads(Path("agent-import.json").read_text())["since"] == {"messages": 4, "positions": 1, "events": 1}
    _log(Path("qnap/live.db"), 2_000.0, 2, events=("pause", "error", "resume", "service-start"))
    link.round()
    assert [r.subject for r in pump.rows] == ["a0", "a1", "1"]
    assert [p.ts for p in pump.positions] == [2_000.5]
    # Only the service's own events, marked as the fallback's.
    assert [(e, d) for e, _, d, _ in pump.events] == [
        ("error", f"for a test ({live_import.FALLBACK_NOTE})"),
        ("service-start", f"for a test ({live_import.FALLBACK_NOTE})"),
    ]
    assert link.status()["since"] == {"messages": 7, "positions": 2, "events": 5}
    assert link.status()["imported"] == {"messages": 3, "positions": 1, "events": 2}
    link.round()  # nothing new
    assert len(pump.rows) == 3


def test_page_text_logged_there_stays_hidden_here(exporter):
    srv, tok = exporter
    Path("agent-import.json").write_text(json.dumps({"since": {"messages": 0, "positions": 0, "events": 0}}))
    _log(Path("qnap/live.db"), 1_000.0, 0)
    pump = FakePump()
    _link(srv, tok, pump).round()
    (page,) = pump.rows
    assert "message" not in page.fields and "MEET" not in page.raw and page.fields["capcode"] == "1"
    shown = FakePump()
    Path("agent-import.json").write_text(json.dumps({"since": {"messages": 0, "positions": 0, "events": 0}}))
    _link(srv, tok, shown, pager_content=True).round()
    assert shown.rows[0].fields["message"] == "MEET AT 3"


def test_the_mark_is_saved_only_once_the_rows_are_written(exporter, tmp_path):
    srv, tok = exporter
    Path("agent-import.json").write_text(json.dumps({"since": {"messages": 0, "positions": 0, "events": 0}}))
    _log(Path("qnap/live.db"), 1_000.0, 2, events=("start", "pause"))
    writer = LiveLogWriter(Path("worker.db"), 30 * 86400, 1 << 30, 0)
    pump = LogPump(writer)
    link = _link(srv, tok, pump)
    link.round()
    assert json.loads(Path("agent-import.json").read_text())["since"]["messages"] == 0  # queued, not written
    pump.drain()
    assert json.loads(Path("agent-import.json").read_text())["since"] == {"messages": 3, "positions": 1, "events": 2}
    rows = writer.conn.execute("SELECT ts, subject FROM messages ORDER BY id").fetchall()
    assert rows == [(1_000.0, "a0"), (1_001.0, "a1"), (1_000.0, "1")]  # their own times
    evs = writer.conn.execute("SELECT ts, event, detail FROM events").fetchall()
    assert evs == [(1_000.25, "start", f"for a test ({live_import.FALLBACK_NOTE})")]
    writer.close()


def test_a_log_started_afresh_there_is_read_from_its_start(exporter):
    srv, tok = exporter
    Path("agent-import.json").write_text(json.dumps({"since": {"messages": 500, "positions": 9, "events": 70}}))
    _log(Path("qnap/live.db"), 1_000.0, 1)
    pump = FakePump()
    link = _link(srv, tok, pump)
    link.round()
    assert [r.subject for r in pump.rows] == ["a0", "1"]
    assert link.status()["since"] == {"messages": 2, "positions": 1, "events": 1}


def test_export_pages_and_says_there_is_more(exporter):
    srv, tok = exporter
    _log(Path("qnap/live.db"), 1_000.0, 5)
    rows, done = agent.export(addr(srv), TOKEN, {"messages": 1}, limit=2)
    assert [r["row"]["id"] for r in rows if r["table"] == "messages"] == [2, 3]
    assert done["more"] and done["last_ids"]["messages"] == 3 and done["max_ids"]["messages"] == 6


def test_an_agent_without_a_local_service_only_takes_the_heartbeat(bins, here):
    srv = _agent_server(bins)
    tok = here / "agent-token"
    tok.write_text(TOKEN)
    try:
        with pytest.raises(agent.AgentRefused, match="no local live service"):
            agent.export(addr(srv), TOKEN, None)
        pump = FakePump()
        link = _link(srv, tok, pump)
        link.round()
        assert srv.agent.last_heartbeat is not None and not Path("agent-import.json").exists()
        assert link.status()["fallback"] is None and link.status()["last_contact"] is not None
    finally:
        _close(srv)


def test_the_link_keeps_trying_an_agent_that_is_away(here):
    tok = here / "agent-token"
    tok.write_text(TOKEN)
    s = socket_free_port()
    link = live_import.AgentLink(FakePump(), ("127.0.0.1", s), tok, Path("agent-import.json"))
    link.start()
    try:
        assert wait_for(lambda: link.status()["error"] is not None)
    finally:
        link.stop()
    assert not link.is_alive()


def socket_free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
