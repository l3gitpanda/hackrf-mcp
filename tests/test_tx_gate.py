import asyncio
from dataclasses import replace

import numpy as np
import pytest

from hackrf_mcp import iq, server, settings, storage


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def _fake_run(binary, args, timeout=None):
        calls.append((binary, args, timeout))
        return "ok"

    monkeypatch.setattr(server, "run", _fake_run)
    return calls


def _write_iq(path, samples):
    interleaved = np.empty(samples.size * 2, dtype=np.int8)
    interleaved[0::2] = np.clip(np.real(samples) * 127, -127, 127).astype(np.int8)
    interleaved[1::2] = np.clip(np.imag(samples) * 127, -127, 127).astype(np.int8)
    interleaved.tofile(path)


def _tone(offset_hz, sample_rate_hz=2_000_000, seconds=0.2):
    t = np.arange(int(sample_rate_hz * seconds)) / sample_rate_hz
    return 0.8 * np.exp(2j * np.pi * offset_hz * t)


def _noise(sample_rate_hz=10_000_000, seconds=0.05):
    rng = np.random.default_rng(0)
    n = int(sample_rate_hz * seconds)
    return 0.3 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def _set(isolated_settings, **changes):
    settings.configure(replace(isolated_settings, **changes))


def test_tx_refused_when_mode_off(fake_run, tmp_path):
    _write_iq(tmp_path / "tone.iq", _tone(50e3))

    result = server.hackrf_transfer(direction="tx", file_path="tone.iq", frequency_hz=915_000_000, sample_rate_hz=2_000_000)

    assert result.startswith("REFUSED")
    assert fake_run == []
    [entry] = storage.recent_tx()
    assert entry["allowed"] is False
    assert entry["tx_mode"] == "off"


def test_tx_allowed_inside_band_and_audited(fake_run, tmp_path, isolated_settings):
    _set(isolated_settings, tx_mode="allowed-bands")
    _write_iq(tmp_path / "tone.iq", _tone(50e3))

    result = server.hackrf_transfer(direction="tx", file_path="tone.iq", frequency_hz=915_000_000, sample_rate_hz=2_000_000)

    assert result.startswith("Transmit allowed")
    assert fake_run[0][0] == "hackrf_transfer"
    [entry] = storage.recent_tx()
    assert entry["allowed"] is True
    assert entry["result"] == "ran"
    assert entry["file"] == "tone.iq"
    assert len(entry["file_sha256"]) == 64
    low, high = entry["span_hz"]
    assert low <= 915e6 <= high
    assert high - low < 200e3  # measured narrow span, not the 2 MHz sample rate


def test_tx_refused_when_wideband_signal_crosses_band_edge(fake_run, tmp_path, isolated_settings):
    _set(isolated_settings, tx_mode="allowed-bands")
    _write_iq(tmp_path / "noise.iq", _noise())

    result = server.hackrf_transfer(direction="tx", file_path="noise.iq", frequency_hz=927_000_000, sample_rate_hz=10_000_000)

    assert result.startswith("REFUSED")
    assert fake_run == []


def test_tx_refused_outside_every_band(fake_run, tmp_path, isolated_settings):
    _set(isolated_settings, tx_mode="allowed-bands")
    _write_iq(tmp_path / "tone.iq", _tone(50e3))

    result = server.hackrf_transfer(direction="tx", file_path="tone.iq", frequency_hz=1_090_000_000, sample_rate_hz=2_000_000)

    assert result.startswith("REFUSED")
    assert fake_run == []


def test_tx_timeout_is_capped(fake_run, tmp_path, isolated_settings):
    _set(isolated_settings, tx_mode="unrestricted", max_tx_seconds=5)
    _write_iq(tmp_path / "tone.iq", _tone(50e3))

    server.hackrf_transfer(direction="tx", file_path="tone.iq", frequency_hz=100_000_000, repeat_tx=True, timeout_seconds=600)

    assert fake_run[0][2] == 5


def test_tx_refuses_file_outside_capture_dir(fake_run, isolated_settings):
    _set(isolated_settings, tx_mode="unrestricted")

    result = server.hackrf_transfer(direction="tx", file_path="/etc/hostname", frequency_hz=100_000_000)

    assert result.startswith("REFUSED")
    assert fake_run == []


def test_rx_over_duration_limit_is_refused(fake_run, isolated_settings):
    _set(isolated_settings, max_rx_seconds=10)

    result = server.hackrf_transfer(direction="rx", file_path="long.iq", frequency_hz=100_000_000, duration_seconds=30)

    assert result.startswith("REFUSED")
    assert fake_run == []


def test_rx_over_size_limit_is_refused(fake_run, isolated_settings):
    _set(isolated_settings, max_capture_mb=10)

    result = server.hackrf_transfer(
        direction="rx", file_path="big.iq", frequency_hz=100_000_000, sample_rate_hz=20_000_000, duration_seconds=1
    )

    assert result.startswith("REFUSED")
    assert fake_run == []


def test_device_calls_are_refused_while_device_is_busy(fake_run):
    server._device_lock.acquire()
    try:
        result = server.hackrf_info()
    finally:
        server._device_lock.release()

    assert result == server.DEVICE_BUSY
    assert fake_run == []


def test_offline_tools_refuse_paths_outside_capture_dir():
    result = server.hackrf_iq_info(iq_file_path="/etc/hostname", sample_rate_hz=1_000_000)

    assert result.startswith("REFUSED")


def test_delete_capture(tmp_path):
    (tmp_path / "old.iq").write_bytes(b"x" * 100)

    result = server.hackrf_delete_capture("old.iq")

    assert result.startswith("Deleted old.iq")
    assert not (tmp_path / "old.iq").exists()


def test_delete_capture_refuses_audit_log():
    storage.audit_tx({"allowed": False})

    result = server.hackrf_delete_capture("tx-audit.jsonl")

    assert result.startswith("REFUSED")


def test_server_status_reports_mode_and_bands(isolated_settings):
    _set(isolated_settings, tx_mode="allowed-bands")

    status = server.hackrf_server_status()

    assert "Transmit mode: allowed-bands" in status
    assert "915 MHz ISM" in status
    assert "frequency only" in status


def test_list_captures_tool(tmp_path):
    (tmp_path / "a.iq").write_bytes(b"x" * 10)

    result = server.hackrf_list_captures()

    assert "a.iq" in result


@pytest.mark.parametrize("name", ["hackrf_server_status", "hackrf_list_captures", "hackrf_delete_capture"])
def test_new_tools_are_registered(name):
    tools = asyncio.run(server.mcp.list_tools())

    assert name in {t.name for t in tools}


def test_power_bandwidth_of_a_tone_is_narrow(tmp_path):
    _write_iq(tmp_path / "tone.iq", _tone(100e3))

    freqs, power = iq.file_power_spectrum(tmp_path / "tone.iq", 2_000_000)
    low, high = iq.power_bandwidth(freqs, power)

    assert low < 100e3 < high
    assert high - low < 20e3


def test_power_bandwidth_of_noise_is_wide(tmp_path):
    _write_iq(tmp_path / "noise.iq", _noise())

    freqs, power = iq.file_power_spectrum(tmp_path / "noise.iq", 10_000_000)
    low, high = iq.power_bandwidth(freqs, power)

    assert high - low > 9e6


def test_file_power_spectrum_of_empty_file(tmp_path):
    (tmp_path / "empty.iq").write_bytes(b"")

    freqs, power = iq.file_power_spectrum(tmp_path / "empty.iq", 2_000_000)

    assert freqs.size == 0
    assert iq.power_bandwidth(freqs, power) is None
