"""hackrf-agent and RemoteSource over loopback TCP, with a fake
hackrf_transfer that writes a known byte pattern, prints power lines like
the real one, and exits cleanly on SIGINT."""

import hashlib
from pathlib import Path
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
args = sys.argv[1:]
if "-n" in args:
    # A capture: exactly 2 * n bytes, then hackrf_transfer's summary, then exit.
    left = 2 * int(args[args.index("-n") + 1])
    while left:
        chunk = block[: min(left, len(block))]
        sys.stdout.buffer.write(chunk); left -= len(chunk)
    sys.stdout.buffer.flush()
    sys.stderr.write("Exiting... hackrf_is_streaming() result: streaming terminated (-1004)\\n")
    sys.exit(0)
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


INFO = """\
import sys
print("hackrf_info version: 2026.01.3")
print("Found HackRF")
print("Serial number: 0000000000000000a06063c8234e925f")
"""

SWEEP = """\
import sys, time
if "-f" in sys.argv and sys.argv[sys.argv.index("-f") + 1] == "1:2":
    time.sleep(60)  # hangs, for the timeout test
for lo in range(int(sys.argv[sys.argv.index("-f") + 1].split(":")[0]), 3):
    print(f"2026-11-03, 21:00:00.0, {lo * 1000000}, {(lo + 1) * 1000000}, 1000000.00, 20, -60.1, -55.2")
sys.stderr.write("Sweeping from 0 MHz to 3 MHz\\n")
"""


@pytest.fixture
def tools(tmp_path, fake):
    d = tmp_path / "tools"
    d.mkdir()
    for name, body in (("hackrf_info", INFO), ("hackrf_sweep", SWEEP)):
        (d / name).write_text(f"#!{sys.executable}\n" + body)
        (d / name).chmod(0o755)
    return d


@pytest.fixture
def server(fake, tools):
    s = agent.AgentSettings(
        host="127.0.0.1", port=0, token_sha256=(hashlib.sha256(TOKEN.encode()).hexdigest(),), binary=str(fake), tools_dir=str(tools)
    )
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


# --- running tools through the agent (W3) ---------------------------------


def test_run_info_sweep_and_a_capture(server):
    out = bytearray()
    done = agent.run_tool(addr(server), TOKEN, {"tool": "info"}, out.extend)
    assert done["exit_code"] == 0 and "Found HackRF" in out.decode()
    out = bytearray()
    done = agent.run_tool(addr(server), TOKEN, {"tool": "sweep", "freq_min_mhz": 0, "freq_max_mhz": 3, "one_shot": True}, out.extend)
    assert done["exit_code"] == 0 and out.decode().count("\n") == 3 and "Sweeping" in done["stderr"]
    out = bytearray()
    req = {"tool": "capture", "frequency_hz": 98_000_000, "sample_rate_hz": 2_000_000, "num_samples": 300_000}
    done = agent.run_tool(addr(server), TOKEN, req, out.extend)
    assert done["exit_code"] == 0 and len(out) == 600_000 and done["bytes"] == 600_000
    assert bytes(out[:256]) == bytes(range(256))
    assert agent.status(addr(server), TOKEN)["tool"] is None


def test_a_run_preempts_a_stream_and_says_so(server):
    src = RemoteSource(addr(server), TOKEN, 1_090_000_000, 2_400_000, 32, 40, False)
    src.start(lambda b: None)
    assert wait_for(lambda: server.agent.current is not None)
    agent.run_tool(addr(server), TOKEN, {"tool": "info"}, lambda b: None)
    assert wait_for(lambda: src.failure() is not None)
    assert "preempted by hackrf_info" in src.failure()
    src.stop()


def test_a_tool_that_hangs_is_stopped_at_its_timeout(server):
    t0 = time.monotonic()
    done = agent.run_tool(addr(server), TOKEN, {"tool": "sweep", "freq_min_mhz": 1, "freq_max_mhz": 2, "timeout_s": 1}, lambda b: None)
    assert done["timed_out"] and time.monotonic() - t0 < 10


def test_runs_are_typed_and_the_agent_keeps_its_own_limits(server):
    def refused(req):
        with pytest.raises(agent.AgentRefused) as e:
            agent.run_tool(addr(server), TOKEN, req, lambda b: None)
        return str(e.value)

    assert "unknown tool" in refused({"tool": "transmit"})
    assert "unknown tool" in refused({"tool": "clock"})
    assert "device_serial" in refused({"tool": "info", "device_serial": "; rm -rf /"})
    assert "bias-tee power is locked" in refused({"tool": "capture", "frequency_hz": 98_000_000, "sample_rate_hz": 2_000_000, "num_samples": 10, "antenna_enable": True})
    assert "MB limit" in refused({"tool": "capture", "frequency_hz": 98_000_000, "sample_rate_hz": 2_000_000, "num_samples": 2_000_000_000})
    assert "0-7250" in refused({"tool": "sweep", "freq_min_mhz": 100, "freq_max_mhz": 99})
    assert "timeout_s" in refused({"tool": "sweep", "freq_min_mhz": 0, "freq_max_mhz": 3, "timeout_s": 5000})
    with pytest.raises(agent.AgentRefused, match="not authorised"):
        agent.run_tool(addr(server), "wrong", {"tool": "info"}, lambda b: None)


# --- the MCP server's device tools on a split station ----------------------


@pytest.fixture
def split(server, tmp_path, isolated_settings):
    tok = tmp_path / "agent-token"
    tok.write_text(TOKEN)
    host, port = addr(server)
    settings.configure_live(settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0, agent=(host, port), agent_token_file=tok))
    return server


def test_device_tools_go_through_the_agent(split, tmp_path):
    from hackrf_mcp import server as mcp

    assert "Found HackRF" in mcp.hackrf_info()
    out = mcp.hackrf_sweep(freq_min_mhz=0, freq_max_mhz=3)
    assert "--- sweep data ---" in out and out.count("-60.1") == 3
    r = mcp.hackrf_transfer(direction="rx", file_path="cap.iq", frequency_hz=98_000_000, sample_rate_hz=2_000_000, num_samples=250_000)
    assert "streaming terminated" in r
    assert (tmp_path / "cap.iq").stat().st_size == 500_000  # written here, on the worker
    status = mcp.hackrf_server_status()
    assert "behind the hackrf-agent at 127.0.0.1" in status and "(idle)" in status


def test_what_the_agent_does_not_do_is_refused_on_a_split_station(split, tmp_path):
    from hackrf_mcp import server as mcp

    (tmp_path / "tone.iq").write_bytes(bytes(4096))
    assert mcp.hackrf_transfer(direction="tx", file_path="tone.iq", frequency_hz=433_920_000).startswith("REFUSED: this server's HackRF is on another machine")
    assert "clock settings" in mcp.hackrf_clock(read_all=True)
    assert "Opera Cake" in mcp.hackrf_operacake(list_boards=True)
    assert mcp.hackrf_biast(immediate_mode=False).startswith("REFUSED:")
    # A capture the agent refuses leaves no file behind.
    r = mcp.hackrf_transfer(direction="rx", file_path="big.iq", frequency_hz=98_000_000, sample_rate_hz=20_000_000, duration_seconds=1, lna_gain_db=33)
    assert r.startswith("REFUSED") and not (tmp_path / "big.iq").exists()


def test_a_stream_asked_for_while_a_tool_runs_is_refused_not_queued(server):
    threading.Thread(
        target=agent.run_tool,
        args=(addr(server), TOKEN, {"tool": "sweep", "freq_min_mhz": 1, "freq_max_mhz": 2, "timeout_s": 3}, lambda b: None),
        daemon=True,
    ).start()
    assert wait_for(lambda: agent.status(addr(server), TOKEN)["tool"] is not None)
    t0 = time.monotonic()
    src = RemoteSource(addr(server), TOKEN, 98_000_000, 2_000_000, 32, 40, False)
    src.start(lambda b: None)
    assert wait_for(lambda: src.failure() is not None, timeout=3)
    assert "running hackrf_sweep" in src.failure() and time.monotonic() - t0 < 3
    src.stop()
    # Once the tool is done, a stream starts as usual.
    assert wait_for(lambda: agent.status(addr(server), TOKEN)["tool"] is None, timeout=10)
    got = bytearray()
    again = RemoteSource(addr(server), TOKEN, 98_000_000, 2_000_000, 32, 40, False)
    again.start(got.extend)
    assert wait_for(lambda: len(got) > 0)
    again.stop()


def test_a_device_tool_pauses_a_live_service_streaming_from_the_agent(server, tmp_path, monkeypatch):
    """The cycle W3 exists for: the live service streams from the agent; an
    MCP device tool pauses it, runs through the agent, and it resumes."""
    from hackrf_mcp import server as mcp
    from hackrf_mcp.live_service import ControlServer

    monkeypatch.chdir(tmp_path)  # a short relative path for the control socket
    tok = tmp_path / "agent-token"
    tok.write_text(TOKEN)
    Path("live").mkdir()
    live = settings.LiveSettings(live_dir=Path("live"), min_free_mb=0, agent=addr(server), agent_token_file=tok)
    settings.configure_live(live)
    svc = LiveService(live, None, program_finder=lambda p: "/bin/true")
    events = []
    record = svc._event
    svc._event = lambda event, profile=None, detail=None: (events.append(event), record(event, profile, detail))
    ctl = ControlServer(live.socket_path, svc)
    threading.Thread(target=ctl.serve_forever, daemon=True).start()
    threading.Thread(target=svc.serve_forever, kwargs={"interval_s": 0.1}, daemon=True).start()
    try:
        assert svc.handle({"cmd": "watch", "center_hz": 98_000_000, "sample_rate_hz": 2_000_000})["state"] == "running"
        assert wait_for(lambda: svc.pipeline is not None and svc.pipeline.source.bytes_read > 0)
        first = svc.pipeline
        out = mcp.hackrf_info()
        assert "Found HackRF" in out and "Live decoding of watch was paused for this call" in out
        # It comes back with a fresh stream from the agent.
        assert wait_for(lambda: svc.pipeline is not None and svc.pipeline is not first and svc.pipeline.source.bytes_read > 0)
        assert "pause" in events and "resume" in events and "error" not in events
        assert events.count("start") == 2  # the watch, and the watch again after the tool
    finally:
        svc.shutdown.set()
        ctl.shutdown()
        ctl.server_close()
        with svc.lock:
            svc._stop_pipeline("test over")
