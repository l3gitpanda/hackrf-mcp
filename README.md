# hackrf-mcp

[![CI](https://github.com/thehappydinoa/hackrf-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/thehappydinoa/hackrf-mcp/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io/) server that wraps the
[hackrf_tools](https://github.com/greatscottgadgets/hackrf) command-line
utilities, letting AI assistants inspect and control a connected
[HackRF](https://greatscottgadgets.com/hackrf/) software-defined radio.

Built with [FastMCP](https://gofastmcp.com/) and designed for use with
Cursor, Claude Desktop, Claude Code, and any other MCP client.

## Features

- **Device tools** — query device info, sweep spectrum, capture/transmit IQ,
  configure clocking, Opera Cake antenna switching, and bias-tee power.
- **Offline IQ analysis** — demodulate captures to WAV, render spectrograms,
  summarize signal metrics, and detect RF bursts (numpy/scipy/matplotlib).
- **Safe defaults** — bounded timeouts, truncated sweep output, sample caps on
  large IQ files, and destructive firmware/CPLD tools disabled by default.

## Requirements

| Requirement | Notes |
|---|---|
| HackRF device | Connected via USB |
| [hackrf_tools](https://github.com/greatscottgadgets/hackrf) | `hackrf_info`, `hackrf_sweep`, `hackrf_transfer`, etc. on `PATH` or in a directory you configure |
| Python 3.10+ | Managed via [uv](https://docs.astral.sh/uv/) (recommended) or pip |

### Installing hackrf_tools

Pre-built binaries and build instructions are in the
[HackRF repository](https://github.com/greatscottgadgets/hackrf). Platform
packages are also available (e.g. `hackrf` on Homebrew, `hackrf` on many
Linux distros). After install, confirm with:

```bash
hackrf_info
```

## Install

Clone the repository and sync dependencies with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/thehappydinoa/hackrf-mcp.git
cd hackrf-mcp
uv sync
```

Or install into an isolated tool environment:

```bash
uv tool install git+https://github.com/thehappydinoa/hackrf-mcp.git
```

## Run

Start the MCP server over stdio:

```bash
uv run hackrf-mcp
```

If `hackrf_*` binaries are not on your `PATH`, point the server at the folder
containing them:

```bash
uv run hackrf-mcp --bin-dir /path/to/hackrf-tools
# or
HACKRF_TOOLS_DIR=/path/to/hackrf-tools uv run hackrf-mcp
```

`--bin-dir` is checked first, then `PATH`.

## MCP client setup

Example configs live in [`examples/`](examples/). Replace `/absolute/path/to/...`
with your actual paths.

### Cursor

Copy [`examples/cursor.mcp.json`](examples/cursor.mcp.json) into your Cursor
MCP config (project `.cursor/mcp.json` or global MCP settings) and edit the
paths:

```json
{
  "mcpServers": {
    "hackrf": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/hackrf-mcp",
        "hackrf-mcp"
      ],
      "env": {
        "HACKRF_TOOLS_DIR": "/absolute/path/to/hackrf-tools"
      }
    }
  }
}
```

### Claude Code

From the project directory:

```bash
claude mcp add hackrf -- uv run --directory /absolute/path/to/hackrf-mcp hackrf-mcp
```

Or use the included [`.mcp.json`](.mcp.json) when working inside a cloned copy
of this repo (uses `${CLAUDE_PROJECT_DIR}` as the working directory).

### Claude Desktop

Add to `claude_desktop_config.json` (see
[`examples/claude-desktop.config.json`](examples/claude-desktop.config.json)):

```json
{
  "mcpServers": {
    "hackrf": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/hackrf-mcp",
        "hackrf-mcp",
        "--bin-dir",
        "/absolute/path/to/hackrf-tools"
      ]
    }
  }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `hackrf_info` | Show connected device info (board ID, firmware, serial). |
| `hackrf_sweep` | Sweep a frequency range and report power levels per bin. |
| `hackrf_transfer` | Receive IQ samples to a file, or transmit from a file. |
| `hackrf_clock` | Read/configure CLKIN/CLKOUT and Pro P1/P2 connectors. |
| `hackrf_operacake` | List/configure Opera Cake antenna switch boards. |
| `hackrf_biast` | Enable/disable/configure bias-tee antenna power. |
| `hackrf_iq_to_wav` | Demodulate a raw IQ capture (NFM/WFM/AM/USB/LSB) to WAV (optionally returned as playable audio). |
| `hackrf_iq_spectrogram` | Render a spectrogram PNG from a raw IQ capture. |
| `hackrf_iq_info` | Summarize an IQ capture: duration, power levels, occupied bandwidth. |
| `hackrf_iq_detect_bursts` | Detect bursts of elevated power (keyfobs, packet preambles, etc.) in an IQ capture. |

`hackrf_iq_*` tools are **offline** — they read capture files written by
`hackrf_transfer` and never touch the device, so they can run while the HackRF
is busy with another operation.

`hackrf_sweep` accepts an optional `output_image_path` to render the sweep as
a PNG alongside the CSV text.

The lower-level/destructive tools (`hackrf_debug`, `hackrf_cpldjtag`,
`hackrf_spiflash`) are implemented but **disabled by default** (commented out
in `server.py`) because misuse can brick the device. Uncomment them in
`src/hackrf_mcp/server.py` if you need register access, CPLD programming, or
firmware flashing.

## Typical workflow

1. **`hackrf_info`** — confirm the device is connected.
2. **`hackrf_sweep`** — scan a band to find active signals.
3. **`hackrf_transfer`** (rx) — capture IQ at a chosen center frequency.
4. **`hackrf_iq_spectrogram`** / **`hackrf_iq_to_wav`** / **`hackrf_iq_info`** —
   analyze the capture offline.

For NFM/WFM demodulation, capture with a small LO offset (~25–50 kHz) from the
target signal and pass the real frequency as `target_freq_hz` to avoid the
HackRF DC spike.

## Safety notes

- **Transmitting (`hackrf_transfer` with `direction="tx"`)** radiates RF. Only
  transmit on frequencies and at power levels you are authorized to use in your
  jurisdiction (e.g. FCC Part 97/15 in the US). Prefer a dummy load or shielded
  enclosure when testing.
- **Bias-tee (`hackrf_biast`)** applies DC power to the antenna port and can
  damage equipment not designed for it.
- **`amp_enable`** adds ~11–14 dB gain and can overdrive the receiver front end
  when combined with bias-tee power or strong nearby signals.
- **Gain settings**: start conservative (RF amp off, LNA ~16 dB, VGA ~16 dB)
  and increase gradually. See
  [setting_gain](https://hackrf.readthedocs.io/en/latest/setting_gain.html).
- **Firmware/CPLD tools** (`hackrf_spiflash`, `hackrf_cpldjtag`, `hackrf_debug`)
  can leave the device in a bad state. Recovery is usually via DFU mode — see
  [updating_firmware](https://hackrf.readthedocs.io/en/latest/updating_firmware.html).

## Development

```bash
uv sync
uv run pytest
```

Tests mock subprocess/`hackrf_mcp.cli.run`, so no HackRF hardware is required.
See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT — see [LICENSE](LICENSE).
