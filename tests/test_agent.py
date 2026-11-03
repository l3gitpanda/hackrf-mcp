"""hackrf-agent and RemoteSource over loopback TCP, with a fake
hackrf_transfer that writes a known byte pattern, prints power lines like
the real one, and exits cleanly on SIGINT."""

import hashlib
import json
import socket
import sys
import threading
import time

import pytest

from hackrf_mcp import agent, settings
from hackrf_mcp.live_profiles import RemoteSource
from hackrf_mcp.live_service import LiveService

TOKEN = "t" * 40
FAKE = """\
import os, signal, sys, time
signal.signal(signal.SIGINT, lambda *a: (open(os.environ.get("FAKE_LOG", "/dev/null"), "a").write("sigint\\n"), os._exit(0)))
if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("hackrf_open() failed: Resource busy (-1000)\\n"); sys.exit(1)
block = bytes(range(256)) * 256
t = 0
while True:
    sys.stdout.buffer.write(block); sys.stdout.buffer.flush()
    t += 1
    if t % 5 == 0:
        sys.stderr.write("2.0 MiB / 1.000 sec = 2.0 MiB/second, average power -27.5 dBfs, 0 bytes free in buffer, 0 underruns\\n")
        sys.stderr.flush()
    time.sleep(0.02)
"""


@pytest.fixture
def fake(tmp_path):
    path = tmp_path / "hackrf_transfer"
    path.write_text(f"#!{sys.executable}\n" + FAKE)
    path.chmod(0o755)
    return path


@pytest.fixture
def server(fake):
    s = agent.AgentSettings(host="127.0.0.1", port=0, token_sha256=(hashlib.sha256(TOKEN.encode()).hexdigest(),), binary=str(fake))
    s.digests = s.load_digests()
    srv = agent.AgentServer(s)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    with srv.agent.lock:
        cur = srv.agent.current
    if cur is not None:
        cur.finish("test over")
        cur.stop_child()
    srv.server_close()


def addr(srv):
    return srv.server_address[:2]


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_samples_and_power_reach_the_remote_source(server):
    got = bytearray()
    src = RemoteSource(addr(server), TOKEN, 98_000_000, 10_000_000, 32, 40, False)
    src.start(got.extend)
    assert wait_for(lambda: len(got) >= 3 * 65536 and src.level_dbfs() is not None)
    assert bytes(got[:512]) == bytes(range(256)) * 2  # the bytes as hackrf_transfer wrote them
    assert src.level_dbfs() == -27.5 and src.failure() is None
    st = agent.status(addr(server), TOKEN)
    assert st["streaming"] and st["stream"]["center_hz"] == 98_000_000 and st["stream"]["sample_rate_hz"] == 10_000_000
    assert st["last_client_seen"] is not None
    child = server.agent.current.proc
    src.stop()
    # Closing the connection stops hackrf_transfer and frees the device.
    assert wait_for(lambda: child.poll() is not None and not agent.status(addr(server), TOKEN)["streaming"])
    assert src.failure() is None  # a stop asked for is no failure


def test_a_new_stream_preempts_the_old_and_says_why(server):
    a = RemoteSource(addr(server), TOKEN, 1_090_000_000, 2_400_000, 32, 40, False)
    a.start(lambda b: None)
    assert wait_for(lambda: server.agent.current is not None)
    first = server.agent.current.proc
    b = RemoteSource(addr(server), TOKEN, 162_000_000, 2_000_000, 32, 40, False)
    got = bytearray()
    b.start(got.extend)
    assert wait_for(lambda: a.failure() is not None and len(got) > 0)
    assert "preempted by a new stream" in a.failure()
    assert first.poll() is not None  # the old hackrf_transfer exited before the new one ran
    assert server.agent.current.tuning["center_hz"] == 162_000_000
    a.stop()
    b.stop()


def test_hackrf_transfer_failing_ends_the_stream_with_its_message(server, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    src = RemoteSource(addr(server), TOKEN, 98_000_000, 10_000_000, 32, 40, False)
    src.start(lambda b: None)
    assert wait_for(lambda: src.failure() is not None)
    assert "exited with code 1" in src.failure() and "Resource busy" in src.failure()
    src.stop()


def raw(srv, line: bytes):
    with socket.create_connection(addr(srv), timeout=5) as s:
        s.sendall(line)
        frames = []
        while (f := agent.read_frame(s)) is not None:
            frames.append(f)
            if len(frames) > 3:
                break
    return frames


def test_requests_are_refused_without_a_token_or_with_bad_fields(server):
    [(kind, body)] = raw(server, json.dumps({"cmd": "stream", "token": "wrong", "center_hz": 98_000_000, "sample_rate_hz": 10_000_000}).encode() + b"\n")
    assert kind == agent.NOTICE and json.loads(body) == {"type": "error", "error": "not authorised"}
    assert server.agent.current is None and server.agent.last_client_seen is None
    for bad, why in [
        ({"center_hz": 500_000, "sample_rate_hz": 10_000_000}, "center_hz"),
        ({"center_hz": 98_000_000, "sample_rate_hz": 30_000_000}, "sample_rate_hz"),
        ({"center_hz": 98_000_000, "sample_rate_hz": 10_000_000, "lna_gain_db": 33}, "lna_gain_db"),
        ({"center_hz": 98_000_000}, "sample_rate_hz"),
    ]:
        [(kind, body)] = raw(server, json.dumps({"cmd": "stream", "token": TOKEN, **bad}).encode() + b"\n")
        assert why in json.loads(body)["error"]
    [(kind, body)] = raw(server, b"not json\n")
    assert "JSON" in json.loads(body)["error"]
    [(kind, body)] = raw(server, json.dumps({"cmd": "transmit", "token": TOKEN}).encode() + b"\n")
    assert "unknown command" in json.loads(body)["error"]
    with pytest.raises(PermissionError):
        agent.status(addr(server), "wrong")


def test_only_allowed_addresses_get_an_answer(fake):
    s = agent.AgentSettings(host="127.0.0.1", port=0, token_sha256=(hashlib.sha256(TOKEN.encode()).hexdigest(),), allow=("10.9.9.9",), binary=str(fake))
    s.digests = s.load_digests()
    srv = agent.AgentServer(s)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert raw(srv, json.dumps({"cmd": "status", "token": TOKEN}).encode() + b"\n") == []
    finally:
        srv.shutdown()
        srv.server_close()
    assert s.allowed("10.9.9.9") and not s.allowed("127.0.0.1") and not s.allowed("not an ip")
    assert agent.AgentSettings(allow=("192.168.0.0/22",)).allowed("192.168.3.125")


def test_agent_settings_from_the_environment(tmp_path):
    tok = tmp_path / "agent-token"
    tok.write_text(TOKEN + "\n")
    s = agent.AgentSettings.from_env({"HACKRF_AGENT_HOST": "0.0.0.0", "HACKRF_AGENT_PORT": "8767", "HACKRF_AGENT_TOKEN_FILE": str(tok), "HACKRF_AGENT_ALLOW": "192.168.3.125, 127.0.0.1"})
    assert (s.host, s.port, s.allow) == ("0.0.0.0", 8767, ("192.168.3.125", "127.0.0.1"))
    assert s.digests == [hashlib.sha256(TOKEN.encode()).digest()]
    with pytest.raises(ValueError):
        agent.AgentSettings.from_env({})
    with pytest.raises(ValueError):
        agent.AgentSettings.from_env({"HACKRF_AGENT_TOKEN_SHA256": "nothex"})


def test_the_live_service_takes_samples_from_an_agent_when_told_to(tmp_path):
    tok = tmp_path / "agent-token"
    tok.write_text(TOKEN)
    live = settings.LiveSettings.from_env({"HACKRF_LIVE_DIR": str(tmp_path / "live"), "HACKRF_LIVE_AGENT": "192.168.3.99:8767", "HACKRF_LIVE_AGENT_TOKEN_FILE": str(tok)})
    assert live.agent == ("192.168.3.99", 8767)
    svc = LiveService(live, None, program_finder=lambda p: "/bin/true")
    tuning = svc.resolve(svc._spec_from({"profile": "adsb"}))
    src = svc._hackrf_source(tuning)
    assert isinstance(src, RemoteSource)
    assert src.request["center_hz"] == 1_090_000_000 and src.token == TOKEN
    with pytest.raises(ValueError):
        settings.LiveSettings.from_env({"HACKRF_LIVE_AGENT": "192.168.3.99:8767"})
    with pytest.raises(ValueError):
        settings.LiveSettings.from_env({"HACKRF_LIVE_AGENT": "no-port", "HACKRF_LIVE_AGENT_TOKEN_FILE": str(tok)})


def test_a_worker_that_stops_reading_does_not_hold_up_the_next(server):
    # A frozen client: asks for a stream, then never reads, so the agent's
    # sends fill the socket buffers and block.
    stuck = socket.socket()
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stuck.connect(addr(server))
    stuck.sendall(json.dumps({"cmd": "stream", "token": TOKEN, "center_hz": 98_000_000, "sample_rate_hz": 10_000_000}).encode() + b"\n")
    time.sleep(3)
    t0 = time.monotonic()
    assert agent.status(addr(server), TOKEN)["streaming"]  # status still answers
    assert time.monotonic() - t0 < 2
    got = bytearray()
    b = RemoteSource(addr(server), TOKEN, 162_000_000, 2_000_000, 32, 40, False)
    b.start(got.extend)
    assert wait_for(lambda: len(got) > 0, timeout=8), "the next stream never started"
    assert server.agent.current.tuning["center_hz"] == 162_000_000
    b.stop()
    stuck.close()


def test_a_refusal_reaches_the_remote_source(server):
    src = RemoteSource(addr(server), "wrong", 98_000_000, 10_000_000, 32, 40, False)
    src.start(lambda b: None)
    assert wait_for(lambda: src.failure() is not None)
    assert "not authorised" in src.failure()
    src.stop()
