import json
from dataclasses import replace

import pytest

from hackrf_mcp import server, settings
from hackrf_mcp.settings import Settings, parse_bool


@pytest.fixture
def fake_run(monkeypatch):
    calls = []
    output = {"text": "ok"}

    def _fake_run(binary, args, timeout=None):
        calls.append((binary, args, timeout))
        return output["text"]

    monkeypatch.setattr(server, "run", _fake_run)
    return calls, output


@pytest.mark.parametrize(
    "kwargs",
    [{"immediate_mode": True}, {"rx_mode": "on"}, {"tx_mode": "on"}, {"off_mode": "on"}],
)
def test_bias_tee_power_on_is_refused_while_locked(fake_run, kwargs):
    calls, _ = fake_run

    result = server.hackrf_biast(**kwargs)

    assert result.startswith("REFUSED")
    assert calls == []


@pytest.mark.parametrize(
    "kwargs",
    [{"immediate_mode": False}, {"rx_mode": "off", "tx_mode": "leave"}, {"reset": True}],
)
def test_bias_tee_power_off_works_while_locked(fake_run, kwargs):
    calls, _ = fake_run

    server.hackrf_biast(**kwargs)

    assert calls[0][0] == "hackrf_biast"


def test_antenna_enable_is_refused_while_locked(fake_run):
    calls, _ = fake_run

    result = server.hackrf_transfer(direction="rx", file_path="x.iq", frequency_hz=100_000_000, antenna_enable=True)

    assert result.startswith("REFUSED")
    assert calls == []


def test_bias_tee_allowed_by_setting(fake_run, isolated_settings):
    calls, _ = fake_run
    settings.configure(replace(isolated_settings, allow_bias_tee=True))

    server.hackrf_biast(immediate_mode=True)

    assert calls == [("hackrf_biast", ["-b", "1"], 15)]


def test_allow_bias_tee_from_env():
    assert Settings.from_env({"HACKRF_MCP_ALLOW_BIAS_TEE": "yes"}).allow_bias_tee is True
    assert Settings.from_env({}).allow_bias_tee is False
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_status_reports_bias_tee_lock():
    assert "Bias-tee power: locked" in server.hackrf_server_status()


def test_decode_ism_builds_rtl433_args_and_summarizes(fake_run, tmp_path):
    calls, output = fake_run
    (tmp_path / "cap.iq").write_bytes(b"\x00" * 64)
    events = [
        {"model": "Acurite-Tower", "id": 1, "temperature_C": 20.1},
        {"model": "Acurite-Tower", "id": 1, "temperature_C": 20.2},
        {"model": "Schrader", "id": "ABC"},
    ]
    output["text"] = "\n".join(json.dumps(e) for e in events) + "\n--- stderr ---\nrtl_433 version 23.11"

    result = server.hackrf_decode_ism("cap.iq", sample_rate_hz=2_000_000, center_freq_hz=433_920_000, protocols=[40])

    binary, args, _ = calls[0]
    assert binary == "rtl_433"
    assert args[:2] == ["-r", f"cs8:{tmp_path / 'cap.iq'}"]
    assert ["-s", "2000000"] == args[2:4]
    assert "-R" in args and "40" in args
    assert "json" in args
    assert result.startswith("rtl_433 decoded 3 message(s)")
    assert "Acurite-Tower x2" in result
    assert "Schrader x1" in result


def test_decode_ism_reports_when_nothing_decoded(fake_run, tmp_path):
    _, output = fake_run
    (tmp_path / "cap.iq").write_bytes(b"\x00" * 64)
    output["text"] = "--- stderr ---\nTest mode active."

    result = server.hackrf_decode_ism("cap.iq", sample_rate_hz=2_000_000, center_freq_hz=433_920_000)

    assert result.startswith("rtl_433 decoded nothing")


def test_decode_ism_refuses_path_outside_capture_dir(fake_run):
    calls, _ = fake_run

    result = server.hackrf_decode_ism("/etc/hostname", sample_rate_hz=2_000_000, center_freq_hz=433_920_000)

    assert result.startswith("REFUSED")
    assert calls == []
