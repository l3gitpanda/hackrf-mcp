"""Transmitting through the hackrf-agent: its own policy and audit log, the
two-phase upload, its limits, and the MCP server's hackrf_transfer on a
split station. The fake hackrf_transfer (test_agent.FAKE) notes every file
it is given to transmit in FAKE_TX_LOG."""

import hashlib
import json
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from hackrf_mcp import agent, iq, settings
from test_agent import FAKE, INFO, SWEEP, TOKEN, addr


@pytest.fixture
def bins(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    for name, body in (("hackrf_transfer", FAKE), ("hackrf_info", INFO), ("hackrf_sweep", SWEEP)):
        (d / name).write_text(f"#!{sys.executable}\n" + body)
        (d / name).chmod(0o755)
    return d


@pytest.fixture
def make_agent(bins, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_TX_LOG", str(tmp_path / "sent.jsonl"))
    servers = []

    def make(**kw):
        s = agent.AgentSettings(
            host="127.0.0.1",
            port=0,
            token_sha256=(hashlib.sha256(TOKEN.encode()).hexdigest(),),
            binary=str(bins / "hackrf_transfer"),
            tools_dir=str(bins),
            tx_log=tmp_path / "agent-tx.jsonl",
            **kw,
        )
        s.check_tx_settings()
        s.digests = s.load_digests()
        srv = agent.AgentServer(s)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def tone(path: Path, offset_hz: float, rate: int = 2_000_000, n: int = 1 << 18) -> Path:
    t = np.arange(n) / rate
    data = np.empty(2 * n, np.int8)
    data[0::2] = np.round(100 * np.cos(2 * np.pi * offset_hz * t))
    data[1::2] = np.round(100 * np.sin(2 * np.pi * offset_hz * t))
    path.write_bytes(data.tobytes())
    return path


def send(srv, path: Path, frequency_hz: int, **fields) -> dict:
    data = path.read_bytes()
    req = {
        "tool": "transmit",
        "frequency_hz": frequency_hz,
        "sample_rate_hz": 2_000_000,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **fields,
    }
    return agent.run_tool(addr(srv), TOKEN, req, lambda b: None, wait_s=30, upload=path)


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_narrow_signal_inside_an_ism_band_is_sent_and_audited(make_agent, tmp_path):
    srv = make_agent(tx_mode="allowed-bands")
    f = tone(tmp_path / "tone.iq", 100_000)
    done = send(srv, f, 915_000_000, txvga_gain_db=20)
    assert done["exit_code"] == 0, done["stderr"]
    assert "915 MHz ISM" in done["allowed_reason"] and done["measured"]
    (sent,) = lines(tmp_path / "sent.jsonl")
    assert sent["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()  # the same bytes reached hackrf_transfer
    assert sent["args"][2:] == ["-f", "915000000", "-s", "2000000", "-x", "20"]
    (rec,) = lines(tmp_path / "agent-tx.jsonl")
    assert rec["allowed"] and rec["result"] == "ran" and rec["measured"] and rec["client"] == "127.0.0.1"
    assert not list(Path(tempfile.gettempdir()).glob("hackrf-tx-*"))  # the upload was removed


def test_measuring_lets_a_narrow_signal_into_a_band_narrower_than_the_sample_rate(make_agent, tmp_path, monkeypatch):
    srv = make_agent(tx_mode="allowed-bands")
    f = tone(tmp_path / "tone.iq", 2_000)  # 13.56 MHz ISM is 14 kHz wide; 2 MS/s is the least the HackRF takes
    assert "13.56 MHz ISM" in send(srv, f, 13_560_000)["allowed_reason"]
    # Without the measurement the whole 2 MHz counts, and that can't fit.
    monkeypatch.setattr(agent, "measure_occupied", lambda *a, **k: None)
    with pytest.raises(agent.AgentRefused, match="assumed full sample-rate span"):
        send(srv, f, 13_560_000)
    assert len(lines(tmp_path / "sent.jsonl")) == 1


def test_the_agents_measurement_agrees_with_the_mcp_servers(tmp_path):
    rng = np.random.default_rng(1)
    for name, offset in (("tone", 300_000), ("tone-neg", -450_000)):
        f = tone(tmp_path / f"{name}.iq", offset)
        ours = agent.measure_occupied(f, 2_000_000)
        theirs = iq.power_bandwidth(*iq.file_power_spectrum(f, 2_000_000))
        bin_hz = 2_000_000 / 4096
        assert abs(ours[0] - theirs[0]) <= 2 * bin_hz and abs(ours[1] - theirs[1]) <= 2 * bin_hz
    # Noise filling the band: both see nearly all of it.
    noise = tmp_path / "noise.iq"
    noise.write_bytes(np.clip(rng.normal(0, 30, 2 << 18), -127, 127).astype(np.int8).tobytes())
    lo, hi = agent.measure_occupied(noise, 2_000_000)
    assert lo < -950_000 and hi > 950_000


def test_out_of_band_is_refused_after_the_upload_and_nothing_is_sent(make_agent, tmp_path):
    srv = make_agent(tx_mode="allowed-bands")
    with pytest.raises(agent.AgentRefused, match="not inside any allowed band"):
        send(srv, tone(tmp_path / "tone.iq", 100_000), 433_920_000)
    assert lines(tmp_path / "sent.jsonl") == []
    (rec,) = lines(tmp_path / "agent-tx.jsonl")
    assert rec["allowed"] is False and rec["frequency_hz"] == 433_920_000 and rec["span_hz"]


def test_with_transmit_off_the_agent_refuses_before_taking_the_file(make_agent, tmp_path, monkeypatch):
    srv = make_agent()  # tx_mode off, the default
    got_ready = []
    real = agent.read_frame

    def spy(sock):
        frame = real(sock)
        if frame and frame[0] == agent.NOTICE and json.loads(frame[1]).get("type") == "ready":
            got_ready.append(1)
        return frame

    monkeypatch.setattr(agent, "read_frame", spy)
    with pytest.raises(agent.AgentRefused, match="disabled on this agent"):
        send(srv, tone(tmp_path / "tone.iq", 100_000), 915_000_000)
    assert got_ready == [] and lines(tmp_path / "sent.jsonl") == []
    assert lines(tmp_path / "agent-tx.jsonl")[0]["reason"].startswith("transmit is disabled")


def test_a_file_that_does_not_match_its_digest_is_refused(make_agent, tmp_path):
    srv = make_agent(tx_mode="allowed-bands")
    f = tone(tmp_path / "tone.iq", 100_000)
    with pytest.raises(agent.AgentRefused, match="does not match its sha256"):
        send(srv, f, 915_000_000, sha256="0" * 64)
    assert lines(tmp_path / "sent.jsonl") == []


def test_the_agent_keeps_its_own_limits(make_agent, tmp_path):
    srv = make_agent(tx_mode="unrestricted", max_tx_mb=1)
    f = tone(tmp_path / "tone.iq", 100_000)

    def refused(frequency_hz=915_000_000, **fields):
        with pytest.raises(agent.AgentRefused) as exc:
            send(srv, f, frequency_hz, **fields)
        return str(exc.value)

    assert "1 MB limit" in refused(bytes=4 << 20)
    assert "whole I/Q pairs" in refused(bytes=3)
    assert "txvga_gain_db must be 0-47" in refused(txvga_gain_db=48)
    assert "bias-tee power is locked" in refused(antenna_enable=True)
    assert "outside" in refused(frequency_hz=7_000_000_000)
    assert lines(tmp_path / "sent.jsonl") == []
    assert len(lines(tmp_path / "agent-tx.jsonl")) == 5  # every attempt is audited


def test_repeat_runs_until_the_agents_own_time_limit(make_agent, tmp_path):
    srv = make_agent(tx_mode="unrestricted", max_tx_seconds=1)
    done = send(srv, tone(tmp_path / "tone.iq", 100_000), 915_000_000, repeat=True, timeout_s=300)
    assert done["timed_out"]
    assert "-R" in lines(tmp_path / "sent.jsonl")[0]["args"]
    assert lines(tmp_path / "agent-tx.jsonl")[0]["result"] == "timed out"


def test_transmit_settings_from_the_environment(tmp_path):
    base = {"HACKRF_AGENT_TOKEN_SHA256": "0" * 64}
    assert agent.AgentSettings.from_env(base).tx_mode == "off"
    with pytest.raises(ValueError, match="audit log"):
        agent.AgentSettings.from_env({**base, "HACKRF_AGENT_TX_MODE": "allowed-bands"})
    s = agent.AgentSettings.from_env(
        {**base, "HACKRF_AGENT_TX_MODE": "allowed-bands", "HACKRF_AGENT_TX_BANDS": "ism,amateur", "STATE_DIRECTORY": str(tmp_path)}
    )
    assert s.tx_log == tmp_path / "tx-audit.jsonl" and s.tx_band_sets == ("ism", "amateur")
    with pytest.raises(ValueError, match="unknown band set"):
        agent.AgentSettings.from_env({**base, "HACKRF_AGENT_TX_MODE": "allowed-bands", "HACKRF_AGENT_TX_BANDS": "cb", "STATE_DIRECTORY": str(tmp_path)})
    with pytest.raises(ValueError, match="tx_mode must be one of"):
        agent.AgentSettings.from_env({**base, "HACKRF_AGENT_TX_MODE": "loud"})


# --- the MCP server on a split station ---------------------------------------


@pytest.fixture
def split_tx(make_agent, tmp_path, isolated_settings):
    def setup(**agent_kw):
        srv = make_agent(**agent_kw)
        tok = tmp_path / "agent-token"
        tok.write_text(TOKEN)
        settings.configure(replace(settings.get(), tx_mode="allowed-bands"))
        settings.configure_live(settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0, agent=addr(srv), agent_token_file=tok))
        return srv

    return setup


def test_the_mcp_server_transmits_through_the_agent(split_tx, tmp_path):
    from hackrf_mcp import server as mcp

    split_tx(tx_mode="allowed-bands")
    tone(tmp_path / "tone.iq", 100_000)
    out = mcp.hackrf_transfer(direction="tx", file_path="tone.iq", frequency_hz=915_000_000, sample_rate_hz=2_000_000)
    assert out.startswith("Transmit allowed: fits inside 915 MHz ISM") and "the hackrf-agent allowed it too" in out
    assert "streaming terminated" in out
    (ours,) = lines(tmp_path / "tx-audit.jsonl")
    (theirs,) = lines(tmp_path / "agent-tx.jsonl")
    assert ours["result"] == "ran" and "915 MHz ISM" in ours["agent_reason"]
    assert theirs["result"] == "ran" and theirs["file_sha256"] == ours["file_sha256"]
    status = mcp.hackrf_server_status()
    assert "the agent's: allowed-bands, bands ism, at most 60 s" in status


def test_the_agents_refusal_wins_and_both_sides_audit_it(split_tx, tmp_path):
    from hackrf_mcp import server as mcp

    split_tx()  # the agent's transmit mode is off
    tone(tmp_path / "tone.iq", 100_000)
    out = mcp.hackrf_transfer(direction="tx", file_path="tone.iq", frequency_hz=915_000_000, sample_rate_hz=2_000_000)
    assert out == "REFUSED: the hackrf-agent refused: transmit is disabled on this agent (HACKRF_AGENT_TX_MODE=off)"
    assert lines(tmp_path / "tx-audit.jsonl")[0]["result"] == "refused-agent"
    assert lines(tmp_path / "agent-tx.jsonl")[0]["allowed"] is False
    assert lines(tmp_path / "sent.jsonl") == []
