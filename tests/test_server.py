import os

import pytest

from hackrf_mcp import server


@pytest.fixture
def fake_run(monkeypatch):
    """Capture calls to hackrf_mcp.server.run and return a canned result."""

    calls = []

    def _fake_run(binary, args, timeout=None):
        calls.append((binary, args, timeout))
        return "ok"

    monkeypatch.setattr(server, "run", _fake_run)
    return calls


@pytest.fixture
def bias_tee_allowed(isolated_settings):
    from dataclasses import replace

    from hackrf_mcp import settings

    settings.configure(replace(isolated_settings, allow_bias_tee=True))


def test_hackrf_info_no_args(fake_run):
    result = server.hackrf_info()

    assert result == "ok"
    assert fake_run == [("hackrf_info", [], 15)]


def test_hackrf_info_with_device_serial(fake_run):
    server.hackrf_info(device_serial="ABC123")

    assert fake_run == [("hackrf_info", ["-d", "ABC123"], 15)]


def test_hackrf_sweep_builds_args_and_reads_output_file(monkeypatch):
    captured = {}

    def _fake_run(binary, args, timeout=None):
        captured["binary"] = binary
        captured["args"] = args
        captured["timeout"] = timeout
        out_path = args[args.index("-r") + 1]
        with open(out_path, "w") as f:
            f.write("2024-01-01,00:00:00,0,6000000,1000000,1,-50.0\n")
        return "sweep log"

    monkeypatch.setattr(server, "run", _fake_run)

    result = server.hackrf_sweep(
        freq_min_mhz=0,
        freq_max_mhz=6000,
        amp_enable=True,
        device_serial="ABC123",
        timeout_seconds=5,
    )

    assert captured["binary"] == "hackrf_sweep"
    assert "-f" in captured["args"] and "0:6000" in captured["args"]
    assert "-a" in captured["args"] and "1" in captured["args"]
    assert "-d" in captured["args"] and "ABC123" in captured["args"]
    assert "-1" in captured["args"]  # one_shot defaults to True
    assert captured["timeout"] == 5

    assert "sweep log" in result
    assert "-50.0" in result

    # temp file should be cleaned up afterwards
    out_path = captured["args"][captured["args"].index("-r") + 1]
    assert not os.path.exists(out_path)


def test_hackrf_sweep_with_output_image(monkeypatch, tmp_path):
    from fastmcp.utilities.types import Image

    def _fake_run(binary, args, timeout=None):
        out_path = args[args.index("-r") + 1]
        with open(out_path, "w") as f:
            f.write("2024-01-01, 00:00:00, 0, 2000000, 1000000, 1, -50.0, -10.0\n")
        return "sweep log"

    monkeypatch.setattr(server, "run", _fake_run)

    img_path = tmp_path / "sweep.png"
    result = server.hackrf_sweep(timeout_seconds=5, output_image_path=str(img_path))

    assert len(result) == 2
    assert "Wrote" in result[0]
    assert "MHz" in result[0]
    assert isinstance(result[1], Image)
    assert img_path.exists()


def test_hackrf_sweep_truncates_large_output(monkeypatch):
    def _fake_run(binary, args, timeout=None):
        out_path = args[args.index("-r") + 1]
        with open(out_path, "w") as f:
            f.write("x" * 60_000)
        return "log"

    monkeypatch.setattr(server, "run", _fake_run)

    result = server.hackrf_sweep(timeout_seconds=5)

    assert "(output truncated)" in result


def test_hackrf_transfer_rx_defaults_to_five_second_capture(fake_run, tmp_path):
    server.hackrf_transfer(
        direction="rx",
        file_path="out.iq",
        frequency_hz=2_400_000_000,
        sample_rate_hz=10_000_000,
    )

    binary, args, timeout = fake_run[0]
    assert binary == "hackrf_transfer"
    assert args[:2] == ["-r", str(tmp_path / "out.iq")]
    assert "-n" in args
    assert args[args.index("-n") + 1] == str(5 * 10_000_000)
    assert "-f" in args and "2400000000" in args
    assert "-s" in args and "10000000" in args


def test_hackrf_transfer_rx_with_explicit_num_samples(fake_run):
    server.hackrf_transfer(
        direction="rx",
        file_path="out.iq",
        frequency_hz=100_000_000,
        num_samples=12345,
    )

    _, args, _ = fake_run[0]
    assert args[args.index("-n") + 1] == "12345"


def test_hackrf_transfer_tx_with_repeat(fake_run, tmp_path, isolated_settings):
    from dataclasses import replace

    from hackrf_mcp import settings

    settings.configure(replace(isolated_settings, tx_mode="unrestricted"))
    (tmp_path / "in.iq").write_bytes(b"\x10\x00" * 1024)

    server.hackrf_transfer(
        direction="tx",
        file_path="in.iq",
        frequency_hz=100_000_000,
        repeat_tx=True,
    )

    _, args, _ = fake_run[0]
    assert args[:2] == ["-t", str(tmp_path / "in.iq")]
    assert "-R" in args
    assert "-n" not in args


def test_hackrf_transfer_gain_and_power_flags(fake_run, bias_tee_allowed):
    server.hackrf_transfer(
        direction="rx",
        file_path="out.iq",
        frequency_hz=100_000_000,
        lna_gain_db=16,
        vga_gain_db=20,
        amp_enable=True,
        antenna_enable=True,
        device_serial="SERIAL",
    )

    _, args, _ = fake_run[0]
    assert "-l" in args and "16" in args
    assert "-g" in args and "20" in args
    assert "-a" in args and "1" in args
    assert "-p" in args and "1" in args
    assert "-d" in args and "SERIAL" in args


def test_hackrf_clock_read_all(fake_run):
    server.hackrf_clock(read_all=True, device_serial="SERIAL")

    assert fake_run == [("hackrf_clock", ["-a", "-d", "SERIAL"], 15)]


def test_hackrf_clock_configure_p1_p2(fake_run):
    server.hackrf_clock(clkout_enable=True, p1_signal="clkin", p2_signal="clkout")

    _, args, _ = fake_run[0]
    assert "-o" in args and "1" in args
    assert "-1" in args and "clkin" in args
    assert "-2" in args and "clkout" in args


def test_hackrf_operacake_frequency_mode(fake_run):
    server.hackrf_operacake(
        mode="frequency",
        port_a0="A1",
        frequency_ranges=["A1:0:1000", "A2:1000:6000"],
    )

    _, args, _ = fake_run[0]
    assert "-m" in args and "frequency" in args
    assert "-a" in args and "A1" in args
    assert args.count("-f") == 2
    assert "A1:0:1000" in args
    assert "A2:1000:6000" in args


def test_hackrf_operacake_list_boards(fake_run):
    server.hackrf_operacake(list_boards=True)

    assert fake_run == [("hackrf_operacake", ["-l"], 15)]


def test_hackrf_biast_immediate_enable(fake_run, bias_tee_allowed):
    server.hackrf_biast(immediate_mode=True)

    assert fake_run == [("hackrf_biast", ["-b", "1"], 15)]


def test_hackrf_biast_reset_and_modes(fake_run, bias_tee_allowed):
    server.hackrf_biast(reset=True, rx_mode="on", tx_mode="off", off_mode="leave")

    _, args, _ = fake_run[0]
    assert args[0] == "-R"
    assert "-r" in args and "on" in args
    assert "-t" in args and "off" in args
    assert "-o" in args and "leave" in args


@pytest.mark.parametrize(
    "name",
    ["hackrf_info", "hackrf_sweep", "hackrf_transfer", "hackrf_clock", "hackrf_operacake", "hackrf_biast"],
)
def test_active_tools_are_registered(name):
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    tool_names = {t.name for t in tools}

    assert name in tool_names


def test_disabled_tools_are_not_registered():
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    tool_names = {t.name for t in tools}

    assert "hackrf_debug" not in tool_names
    assert "hackrf_cpldjtag" not in tool_names
    assert "hackrf_spiflash" not in tool_names
