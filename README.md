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
| `hackrf_decode_ism` | Decode ISM-band sensors, remotes and tire-pressure sensors in an IQ capture with rtl_433 (offline; needs `rtl_433`). |
| `hackrf_server_status` | Show the transmit mode and allowed bands, capture directory, limits and recent transmit attempts. |
| `hackrf_list_captures` | List files in the capture directory with sizes and free space. |
| `hackrf_delete_capture` | Delete one file from the capture directory. |

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

## Transmit policy, file limits and network mode

All of these are set by whoever runs the server, through `HACKRF_MCP_*`
environment variables or the matching CLI flags. No tool can change them.
`hackrf_server_status` reports the current values.

### Transmit modes

| `HACKRF_MCP_TX_MODE` | Behaviour |
|---|---|
| `off` (default) | Every transmission is refused. |
| `allowed-bands` | Transmits only if the signal fits inside one allowed band. The server measures the transmit file's occupied bandwidth (99% of its power, sampled across the whole file) and requires that span, plus the carrier at `frequency_hz` and a small margin, to sit inside one band. If the file can't be analyzed it assumes the full sample-rate bandwidth. |
| `unrestricted` | No band check. |

`allowed-bands` permits the band sets named in `HACKRF_MCP_TX_BANDS`
(default `ism`):

- `ism`: the US ISM bands at 13.56, 27.12, 40.68, 915, 2450 and 5800 MHz.
- `amateur`: the US amateur bands from 160 m to 5 cm, without the
  channelized 60 m band. Your license class may not cover all of them.

`HACKRF_MCP_TX_EXTRA_RANGES` adds ranges in MHz (`433.05-434.79,…`), and
`HACKRF_MCP_BANDS_FILE` points at a JSON file that adds or replaces sets:
`{"sets": {"name": [[low_mhz, high_mhz, "label"], …]}}`.

The band check is a **frequency check only**. Power limits, modulation and
emission rules, harmonics, and which bands your license allows are still
your responsibility.

Every transmit attempt, allowed or refused, is appended to a JSON-lines
audit log (`HACKRF_MCP_TX_LOG`, default
`~/.local/state/hackrf-mcp/tx-audit.jsonl`) with the time, frequency,
measured span, gains, decision and the file's SHA-256. Each transmission is
also stopped after `HACKRF_MCP_MAX_TX_SECONDS` (default 60), `repeat_tx`
included.

### Bias-tee lock

Bias-tee power puts DC on the antenna port, which can damage an antenna,
LNA or filter that isn't built for it. Anything that would turn it on
(`hackrf_biast` with `immediate_mode`/`*_mode="on"`, or `antenna_enable` on
`hackrf_transfer`) is refused unless `HACKRF_MCP_ALLOW_BIAS_TEE=true`.
Turning it off and resetting always work.

### Files and capture limits

Every file path a tool takes is relative to the capture directory
(`HACKRF_MCP_DATA_DIR`, default `~/hackrf-captures`). Paths that resolve
outside it, through `..`, an absolute path or a symlink, are refused. Captures
longer than `HACKRF_MCP_MAX_RX_SECONDS` (60), larger than
`HACKRF_MCP_MAX_CAPTURE_MB` (2048), or that would leave less than
`HACKRF_MCP_MIN_FREE_MB` (2048) free, are refused. Only one device call runs
at a time; a second gets a "HackRF is busy" reply.

### Network mode

```bash
openssl rand -hex 32 > ~/.config/hackrf-mcp.token && chmod 600 ~/.config/hackrf-mcp.token
hackrf-mcp --transport http --host 0.0.0.0 --port 8765 --token-file ~/.config/hackrf-mcp.token
```

This serves streamable HTTP at `http://<host>:8765/mcp`. Every request must
carry `Authorization: Bearer <token>`. The server refuses to start in HTTP mode
without a token. To give each client its own token without the server ever
holding it, list the tokens' SHA-256 hex digests in `HACKRF_MCP_TOKEN_SHA256`
(comma-separated). The token travels in cleartext, so on an untrusted network
put a TLS reverse proxy in front. For Claude Code:

```bash
claude mcp add --transport http hackrf http://<host>:8765/mcp --header "Authorization: Bearer <token>"
```

[`deploy/`](deploy/) has a hardened systemd unit and an environment-file
template for running it as a service.

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
