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
- **Live decoding** — a companion service keeps ADS-B, AIS, ACARS, VDL2,
  pagers, APRS or EAS decoding into a rolling, searchable log, and steps
  aside whenever a device tool needs the HackRF.
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
| `hackrf_decode` | Decode a protocol in an IQ capture with an external decoder and summarise it: ADS-B, UAT, AIS, ACARS, VDL2, POCSAG, FLEX, APRS, EAS/SAME, DTMF, RDS (offline; see [Decoders](#decoders)). |
| `hackrf_live_start` / `hackrf_live_stop` | Start, switch or stop continuous decoding by the live service (see [Live decoding](#live-decoding)). |
| `hackrf_live_status` | What the live service is doing: profile, rates, input health, errors, log size. |
| `hackrf_live_search` | Search the live log by time, profile, ICAO/MMSI/capcode/callsign, text or frequency. |
| `hackrf_live_summary` | Summarise the live log by aircraft, ship, capcode or station over a time range. |
| `hackrf_server_status` | Show the transmit mode and allowed bands, capture directory, limits and recent transmit attempts. |
| `hackrf_list_captures` | List files in the capture directory with sizes and free space. |
| `hackrf_delete_capture` | Delete one file from the capture directory. |

`hackrf_iq_*` and `hackrf_decode*` tools are **offline** — they read capture
files written by `hackrf_transfer` and never touch the device, so they can run
while the HackRF is busy with another operation. They read the capture in
fixed-size chunks, so memory use doesn't grow with its length; when a
`max_duration_seconds` cap stops them early, the result says so.

### Decoders

`hackrf_decode` converts the capture to what each decoder program reads
(cu8/sc16 IQ, AM or FM audio, FM multiplex), streaming it to the program's
stdin where it can, and summarises the output per aircraft, ship or message.
The programs are found on `PATH`; `hackrf_server_status` lists which are
installed.

| Decoder | Program | Capture |
|---|---|---|
| `adsb` | `dump1090-mutability` (or `dump1090`) | 1090 MHz at 2.4 MS/s (2.0 MS/s decoded nothing in tests) |
| `uat` | `dump978-fa` | 978 MHz at 2,083,333 S/s |
| `ais` | `AIS-catcher` | 162.0 MHz, any rate (2 MS/s) |
| `acars` | `acarsdec` | 130.8 MHz at 2 MS/s (a multiple of 12.5 kS/s) |
| `vdl2` | `dumpvdl2` | 136.8 MHz at 2.1 MS/s (a multiple of 105 kS/s) |
| `pocsag`, `flex`, `aprs`, `eas`, `dtmf` | `multimon-ng` | the channel ± 25–50 kHz, 2 MS/s |
| `rds` | `redsea` | the station ± 300–500 kHz, 2 MS/s |

ISM-band devices go through `hackrf_decode_ism` (`rtl_433`).
[`deploy/build-decoders.sh`](deploy/build-decoders.sh) builds `redsea` and
`dump978-fa`, which Ubuntu doesn't package; the others come from the
distribution or their projects' releases.

`hackrf_sweep` accepts an optional `output_image_path` to render the sweep as
a PNG alongside the CSV text.

The lower-level/destructive tools (`hackrf_debug`, `hackrf_cpldjtag`,
`hackrf_spiflash`) are implemented but **disabled by default** (commented out
in `server.py`) because misuse can brick the device. Uncomment them in
`src/hackrf_mcp/server.py` if you need register access, CPLD programming, or
firmware flashing.

## Live decoding

A second service, `hackrf-live` ([`deploy/hackrf-live.service`](deploy/hackrf-live.service)),
keeps one **profile** decoding continuously and writes everything it decodes
to a rolling SQLite log. The MCP tools start, stop and switch it, which is
how an assistant moves the receiver to another band, and search the log.

| Profile | Decoder | Tuning | Logged as |
|---|---|---|---|
| `adsb` | `dump1090-mutability` | 1090 MHz, 2.4 MS/s | one row per aircraft per minute: callsign, squawk, altitude range, last position, speed, track, messages, strongest RSSI |
| `ais` | `AIS-catcher` (opens the HackRF itself) | 162.0 MHz | one row per ship, base station or aid to navigation per minute for position reports; one row per other message (static/voyage data, safety text) |
| `acars` | `acarsdec` | 130.8 MHz, 2 MS/s: 130.025–131.550 MHz | one row per message |
| `vdl2` | `dumpvdl2` | 136.8125 MHz, 2.1 MS/s: 136.650–136.975 MHz | one row per frame |
| `flex`, `pocsag` | `multimon-ng` | the channels given, 2.205 MS/s | one row per page: time, channel, capcode, message type (see below) |
| `aprs` | `multimon-ng` | 144.390 MHz, 2.205 MS/s | one row per packet |
| `eas` | `multimon-ng` | the seven NOAA Weather Radio channels, 162.400–162.550 MHz | one row per SAME header |

Except for AIS, the service reads `hackrf_transfer -r -` and streams the
samples to the decoder: dump1090 and dumpvdl2 take them as they are (cu8);
for acarsdec and multimon-ng one shared FFT per block cuts every channel
out at once (`iq.FFTChannelizer`), which costs about what a single
per-channel filter would. Measured on a 2-core Celeron J4025: ADS-B about
15% of one core in total, ACARS (five channels) about 35%, VDL2 (nine
channels) about 45%, FLEX about 35%, AIS 12%. `hackrf_live_status` warns
when samples arrive below the sample rate, which means some are being lost.

**Tools.** `hackrf_live_start` (a profile, or a `schedule` that rotates
through several, e.g. 10 min ADS-B then 10 min AIS), `hackrf_live_stop`,
`hackrf_live_status`, `hackrf_live_search` (time range, profile, an exact
ICAO address / MMSI / capcode / callsign / registration, free text, or
frequency) and `hackrf_live_summary` (e.g. aircraft in the last hour, ships
today, active capcodes).

**Device sharing.** While a profile runs, the live service holds the HackRF.
Every device tool still works: the MCP server asks the service to release
the device, runs the tool, and hands the device back; the tool's result
says that live decoding was paused and for how long. The release carries a
lease (the tool's timeout plus 30 s), so if the MCP server dies mid-call the
service resumes by itself. If the service doesn't release the device the
tool is refused with a message saying so. Without the service, the tools
behave exactly as before.

**Pager privacy.** By default only the time, channel, capcode and message
type of a FLEX or POCSAG page are stored: the message text, and any
continuation lines it runs onto, are dropped before a row exists, and the
stored raw line is the header with `[content not logged]`. Storing the text
takes `HACKRF_LIVE_PAGER_CONTENT=true` in the service's environment; no tool
can change it.

**The log.** `$HACKRF_LIVE_DIR/live.db` (WAL mode; the MCP server opens it
read-only). Each row has its time (and end time, for per-minute rows),
profile, kind, receiver, frequency, signal level, subject (ICAO address,
MMSI, capcode, ...), label (callsign, flight, ship name), the decoded fields
as JSON, and the decoder's raw text. Once a minute rows older than the age
limit, then the oldest rows beyond the size limit, are deleted and the
space given back to the disk; nothing is written while free disk is below
`HACKRF_MCP_MIN_FREE_MB`. Starts, stops, pauses and errors go to an
`events` table, so gaps in coverage can be seen.

| Variable | Default | |
|---|---|---|
| `HACKRF_LIVE_DIR` | `~/.local/state/hackrf-mcp/live` | log, control socket and saved profile; set it for both services |
| `HACKRF_LIVE_MAX_AGE_DAYS` | `7` | rows older than this are deleted |
| `HACKRF_LIVE_MAX_DB_MB` | `256` | the oldest rows beyond this are deleted |
| `HACKRF_LIVE_PAGER_CONTENT` | `false` | store pager message text |
| `HACKRF_LIVE_RECEIVER` | none | `LAT,LON`, so dump1090 can resolve more positions |
| `HACKRF_LIVE_LNA_GAIN`, `HACKRF_LIVE_VGA_GAIN` | 32, 40 | default gains (a start call can set its own) |

The unit limits the service to `MemoryMax=512M` and 1.5 cores
(`CPUQuota=150%`), and gives way under contention (`CPUWeight=50`,
`Nice=10`). The profile last started is saved and resumed after a restart
or reboot. To check a profile against a recording instead of the radio:

```bash
python -m hackrf_mcp.live_service --replay capture.cs8 --profile acars \
    --rate 2000000 --center 130800000 --db /tmp/replay.db --fast
```

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
4. **`hackrf_decode`** — decode a known protocol (aircraft, ships, pagers…),
   or **`hackrf_iq_spectrogram`** / **`hackrf_iq_to_wav`** / **`hackrf_iq_info`** —
   analyze the capture offline. The burst and spectrum tools measure power
   only: a signal much narrower than the capture (a 25 kHz AIS channel in
   2 MHz) may not show up in them while it still decodes.

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
