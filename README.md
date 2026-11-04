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

FLEX and POCSAG message text is not shown, or saved with `output_path`,
unless the server operator sets `HACKRF_MCP_PAGER_CONTENT=true`: by default a
page shows only its time, rate, frame, capcode and type (see
[Live decoding](#live-decoding), which follows the same setting).

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
| `ais` | `AIS-catcher` | 162.0 MHz, 2 MS/s | one row per ship, base station or aid to navigation per minute for position reports; one row per other message (static/voyage data, safety text) |
| `acars` | `acarsdec` | 130.8 MHz, 2 MS/s: 130.025–131.550 MHz | one row per message |
| `vdl2` | `dumpvdl2` | 136.8125 MHz, 2.1 MS/s: 136.650–136.975 MHz | one row per frame |
| `flex`, `pocsag` | `multimon-ng` | the channels given, 2.205 MS/s | one row per page: time, channel, capcode, message type (see below) |
| `aprs` | `multimon-ng` | 144.390 MHz, 2.205 MS/s | one row per packet |
| `eas` | `multimon-ng` | the seven NOAA Weather Radio channels, 162.400–162.550 MHz | one row per SAME header |

The service reads `hackrf_transfer -r -` and streams the samples to the
decoder: AIS-catcher takes them as they are (cs8, on its stdin since
milestone 2 of the web UI plan; before, it opened the HackRF itself),
dump1090 and dumpvdl2 converted to cu8;
for acarsdec and multimon-ng one shared FFT per block cuts every channel
out at once (`iq.FFTChannelizer`), which costs about what a single
per-channel filter would. Measured on a 2-core Celeron J4025: ADS-B about
15% of one core in total, ACARS (five channels) about 35%, VDL2 (nine
channels) about 45%, FLEX about 35%, AIS 12% (measured on 2026-10-29,
before the spectrum history, whose FFTs add about 8% of a core, and while
AIS-catcher still opened the HackRF itself). `hackrf_live_status` warns
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
takes `HACKRF_MCP_PAGER_CONTENT=true` in the environment both services read
(the older name `HACKRF_LIVE_PAGER_CONTENT` also works); it applies to
`hackrf_decode` as well, and no tool can change it.

**The log.** `$HACKRF_LIVE_DIR/live.db` (WAL mode; the MCP server opens it
read-only). Each row has its time (and end time, for per-minute rows),
profile, kind, receiver, frequency, signal level, subject (ICAO address,
MMSI, capcode, ...), label (callsign, flight, ship name), the decoded fields
as JSON, and the decoder's raw text. Once a minute rows older than the age
limit, then the oldest rows beyond the size limit, are deleted and the
space given back to the disk; nothing is written while free disk is below
`HACKRF_MCP_MIN_FREE_MB`. Starts, stops, pauses and errors go to an
`events` table, so gaps in coverage can be seen. ADS-B and AIS also write a
`positions` table for map tracks finer than the per-minute rows: time,
subject, latitude, longitude, altitude, speed and track, at most one per
aircraft or ship every 10 s, and one every 5 minutes while it stands still
(a fixed AIS station, a moored ship). Positions go with the rows when the
log is pruned.

**Spectrum history.** Beside the log, `$HACKRF_LIVE_DIR/spectrum.db` keeps
what the band looked like while each profile ran, for a waterfall of any
past period ([`live_spectrum.py`](src/hackrf_mcp/live_spectrum.py)). Every
block of samples a profile reads from the HackRF also goes through 512-point
FFTs, and each second of samples becomes one row of two values per bin: the
mean power and the peak (the strongest single FFT frame, so short bursts
show). Values are one byte each, in 0.5 dB steps from -130 to -2.5 dBFS. A
run of a profile (a start, a switch, a resume after a pause) is a segment
recording the profile, tuning, gains and scale. Second rows are also rolled
up into one row per clock minute, kept longer, for day and week views.
Mean and peak take about 1.4 KB a second on disk (SQLite pages included), so about 120 MB a day while a profile runs all day. `hackrf_live_status` shows its size
and limits.

| Variable | Default | |
|---|---|---|
| `HACKRF_LIVE_DIR` | `~/.local/state/hackrf-mcp/live` | log, control socket and saved profile; set it for both services |
| `HACKRF_LIVE_MAX_AGE_DAYS` | `7` | rows older than this are deleted |
| `HACKRF_LIVE_MAX_DB_MB` | `256` | the oldest rows beyond this are deleted |
| `HACKRF_MCP_PAGER_CONTENT` | `false` | store and show pager message text (live log and `hackrf_decode`) |
| `HACKRF_LIVE_RECEIVER` | none | `LAT,LON`, so dump1090 can resolve more positions |
| `HACKRF_LIVE_LNA_GAIN`, `HACKRF_LIVE_VGA_GAIN` | 32, 40 | default gains (a start call can set its own) |
| `HACKRF_LIVE_SPECTRUM` | `true` | record the spectrum history |
| `HACKRF_LIVE_SPECTRUM_MAX_MB` | `1024` | the oldest second rows (then minute rows) beyond this are deleted |
| `HACKRF_LIVE_SPECTRUM_MAX_AGE_DAYS` | `7` | second rows older than this are deleted |
| `HACKRF_LIVE_SPECTRUM_MINUTE_DAYS` | `90` | minute rows older than this are deleted |
| `HACKRF_LIVE_SPECTRUM_DUTY` | `1` | share of the samples transformed (0-1]; below 1 the peak can miss short bursts |

The unit limits the service to `MemoryMax=512M` and 1.5 cores
(`CPUQuota=150%`), and gives way under contention (`CPUWeight=50`,
`Nice=10`). The profile last started is saved and resumed after a restart
or reboot. To check a profile against a recording instead of the radio:

```bash
python -m hackrf_mcp.live_service --replay capture.cs8 --profile acars \
    --rate 2000000 --center 130800000 --db /tmp/replay.db --fast
```

A replay also records the spectrum (to `--spectrum-db`, by default
`replay-spectrum.db` beside `--db`) and prints the noise floor and the
strongest bins, to check a recording against a known signal.

## Web UI

`hackrf-web` (`python -m hackrf_mcp.web`, [`deploy/hackrf-web.service`](deploy/hackrf-web.service))
serves a browser UI for live decoding; the screens are being built
milestone by milestone ([TODO.md](TODO.md)). The **Live** screen shows the
running profile and its tuning, a waterfall and spectrum of its band
(levelled to the noise floor, with peak hold, prefilled from the spectrum
history), the decoded feed with per-profile filters, the rotation and an
editor for it, start, switch and stop, and a health strip (input rate and
power, channel levels, messages a minute, CPU and memory of both services,
log and spectrum sizes). The **Log** screen (`/log`) searches the log by
time range, subject, free text, frequency and profiles (the search lives in
the address, so it can be bookmarked, and Live's feed links rows to it),
shows a timeline of detections per profile with the receiver's coverage
under it (which profile ran, and hatched gaps for paused, error, stopped and
service down), pages through the results, opens a row with all its fields,
its raw text and the waterfall of the minute around it, and exports CSV.
The **Map** screen (`/map`) draws the targets of every profile whose
protocol module declares a map view (ADS-B aircraft, AIS ships) from the
`positions` table: live, with arrows turned to the heading, labels, tracks
of the last 10 minutes to 3 hours and faded markers after 5 minutes without
a fix; or in History, for any range up to 7 days, with a scrubber over the
receiver's coverage and playback (long ranges come back thinned to one fix
per target every 20 s to an hour, and the screen says so). A selected
target shows its fields, its altitude over the track and a link to its rows
in the Log. The map is [Leaflet](https://leafletjs.com/) 1.9.4 with a
lat/lon grid and range rings 25 to 200 km around `HACKRF_LIVE_RECEIVER`
when it is set. Nothing is loaded from other sites when it is viewed.

**Base map.** Streets, coast and places come from an extract of
OpenStreetMap vector tiles that the operator makes once and keeps on the
station, never in the repository (it says where the station is):

```bash
uv run --with pmtiles python -m hackrf_mcp.web.basemap \
    --from-log /var/lib/hackrf-mcp/live/live.db \
    --out /var/lib/hackrf-mcp/web/basemap.pmtiles
```

It reads only what it needs from the newest [Protomaps](https://protomaps.com/)
daily build with HTTP range requests: the wide area (the positions in the
log, or `--around LAT,LON`, or `--wide W,S,E,N`) down to zoom 11 for towns,
main roads and coast, and the core where most targets are down to zoom 14
for streets. For a coastal metro area that is about 86 MiB and 15 seconds;
`--dry-run` reports the size first. hackrf-web serves the file in byte
ranges at `/api/basemap.pmtiles` and the browser draws it with the vendored
[protomaps-leaflet](https://github.com/protomaps/protomaps-leaflet) 5.1.0
(BSD-3-Clause). Map data © OpenStreetMap contributors (ODbL), shown on the
map. Without the file the map has the grid alone.

The **History** screen (`/history`) shows the past in signal form. The
spectrum history is recorded per tuning, so a period is one rotation step:
a day's steps sit on a lane (hatched where the receiver was paused or
failing), and the chosen step shows its recorded waterfall with every row
decoded during it marked at its time and frequency, the gaps in coverage,
the decoded rows with their fields, each channel's level against the noise
floor, and the rows logged over time. Rows link to the Log, and those of
map-capable profiles to the Map at that moment (`/map?at=…&profile=…&subject=…`);
the Log's detail panel links back to History.

**Band plan.** Every waterfall (Live, History and the Log's thumbnail) has a
strip along the top naming the bands it shows, coloured by kind of service
(broadcast, aviation, marine, amateur, land mobile and public safety,
mobile broadband, satellite, navigation, radar, unlicensed, federal,
weather, science). Overlapping bands sit in lanes, broadest on top, and
pointing at the strip (or tapping it) lists every band at that frequency
with its FCC rule part. The plan is data, `web/static/bandplan-us.json`: a
practical United States plan of about 135 bands compiled from FCC rules,
with coarse edges where the real structure is intricate. Under the plan's
bands the readout gives the **official allocation** at that frequency, Non-Federal and
Federal, with the FCC rule parts: the United States part of the FCC's Online
Table of Frequency Allocations (47 CFR 2.106, revised 2022-07-01; the eCFR
prints it only as images), parsed from the FCC's PDF by
`tools/fcc_us_table.py` and compacted by `tools/fcc_web_json.py` into
`web/static/fcc-us-allocations.json`. Live's **Bands** button hides the
strip, and History has a view toggle.

**Bands.** The **Bands** screen (`/bands`) searches the band plan and the
official allocations as you type: words (marine, weather, radio astronomy),
an FCC rule part (part 97), a frequency (162.4, 1.09 GHz, 535 kHz) or a
range (88-108), with chips for the kinds of service. The search is kept in
the address. Each band's **Watch** fills in the Live screen's Watch a range
(taking the receiver is still a click there).

**Watching a range.** The Live screen's **Watch a range** takes any range
the HackRF receives (1 MHz-6 GHz), up to 10 MHz at once: type from and to
in MHz, and it tunes there with the waterfall only, nothing decoded.
**Lower** and **Higher**, or dragging the waterfall sideways, scroll by
half the width. The receiver is lent, not given: after the chosen time
(15 minutes by default, at most 60, **+15 min** to extend) the live service
goes back to the rotation by itself, page open or not, and a restart does
the same. Device tools pause a watch as they pause decoding. What was
watched is recorded in the spectrum history as profile `watch`.

**Summaries.** The **Summaries** screen (`/summaries`) totals a period
(the last hour, 6 or 24 hours, today, 7 days, or from-to in UTC) from the
log as it is, with no storage of its own: when each profile was on the air
and the gaps between; per profile, how many different subjects it heard
(aircraft, ships, capcodes ...; AIS by kind), its decoded messages and the
20 busiest subjects with their fields, each linking to its rows in the Log;
messages per time bucket with the time on the air beneath; and a heat map
of where the profiles with positions were, one profile at a time, counting
different targets (or fixes) per cell on the same base map as the Map, the
cells finer as you zoom in. ADS-B and AIS rows are per-minute summaries, so
a row counts as its `messages` field; any other row is one message. Only
the fields the screen shows are read out of the log, so pager text never
reaches it.

It never opens the HackRF. It reads the live log and the spectrum history, relays the live
service's feed socket (`feed.sock`: decoded rows, events, positions and
about four FFT rows a second) to browsers over a websocket, and starts,
switches and stops profiles over the control socket, as the MCP tools do.

| Route | |
|---|---|
| `GET /api/registry` | profiles and decoders from the registries, with each profile's views (map, table, timeline), the receiver's position if set, and the base map's zooms and bounds, if there is one |
| `GET /api/status` | the live service's status, and the memory and CPU of `hackrf-live` and `hackrf-web` |
| `GET /api/log`, `/api/summary`, `/api/events` | the log, as `hackrf_live_search` and `hackrf_live_summary` read it; `/api/log` takes several `profiles` and pages with `before_id` |
| `GET /api/log/histogram`, `/api/coverage` | matching rows per time bucket and profile; what the receiver was doing (running which profile, paused, error, stopped, down), rebuilt from the events |
| `GET /api/summaries` | for a range, per profile: decoded messages per bucket, subjects heard (per kind) and the busiest of them, with the coverage, from one reading of the log |
| `GET /api/positions` | map-track points; when a range holds more than `limit`, one fix per target per `step` seconds (the response gives `step`) |
| `GET /api/positions/grid` | positions counted per `cell`-degree cell (fixes and different targets), optionally inside a `bbox`, coarsened when there are more than `max_cells` |
| `GET /api/basemap.pmtiles` | the operator's base map, in byte ranges, or 404 when there is none |
| `GET /api/spectrum/segments`, `/api/spectrum?segment=` | spectrum history, downsampled to the buckets asked for |
| `POST /api/live/start`, `/api/live/stop` | the same requests as `hackrf_live_start` / `hackrf_live_stop` |
| `WS /ws/live` | everything as it happens, and status every few seconds |

**Access.** Every route but the sign-in page needs a token: an
`Authorization: Bearer` header, or a cookie a browser gets by signing in
once with a token (HttpOnly, SameSite=Strict, signed with a key in
`/var/lib/hackrf-mcp/web`, 30 days). Changes and websockets must come from
the page's own origin. Tokens come from `HACKRF_WEB_TOKEN_FILE` and
`HACKRF_WEB_TOKEN_SHA256`; removing one ends its sessions. There is no TLS:
limit the port with a firewall and never publish it. Pager text is removed
on read, in the API and on the websocket, unless `HACKRF_MCP_PAGER_CONTENT`
is on, even from rows logged while it was. Protocol modules say how their
rows are shown (`views` on `Profile`; a table and a timeline when they
declare none), so a new decoder appears without changes to the UI.

The UI is plain ES modules and CSS in `src/hackrf_mcp/web/static/` (no
build step, nothing loaded from other sites). To work on it without a
HackRF, `uv run python tools/webui_demo.py` runs the real service, feed,
log, spectrum history and backend on simulated samples with made-up rows,
and prints a sign-in token for `http://127.0.0.1:18766/`.

## Split station: hackrf-agent

The HackRF can sit on one machine while the processing runs on another,
like a game panel and its nodes: **hackrf-agent** (`python -m
hackrf_mcp.agent`, [`deploy/hackrf-agent.service`](deploy/hackrf-agent.service))
runs where the HackRF is plugged in, owns it, and streams its raw samples;
the live service on the worker reads them instead of opening a local
device when `HACKRF_LIVE_AGENT=host:port` and
`HACKRF_LIVE_AGENT_TOKEN_FILE` are set. Decoders, spectrum history, the
log and the web UI then all run on the worker. The agent needs only
Python's standard library and `hackrf_transfer`.

- **What crosses the network:** int8 I/Q at 2 bytes a sample, so 38 Mbit/s
  at 2.4 MS/s and 160 Mbit/s for a 10 MHz watch. Use a wired LAN.
- **Protocol:** one JSON request line, then frames (samples, or a JSON
  notice: started, input power once a second, why a stream ended). A new
  stream preempts the old one; the agent stops `hackrf_transfer` and waits
  for it before opening the device again. Details are at the top of
  `agent.py`.
- **Access:** `HACKRF_AGENT_ALLOW` (addresses or networks) and a token
  (`HACKRF_AGENT_TOKEN_FILE` or `HACKRF_AGENT_TOKEN_SHA256`). Requests are
  typed fields, never a command line, and receive only. There is no TLS
  and the samples carry everything the radio hears, so allow the worker's
  address alone, in the agent and in the firewall.
- **Trying it without hardware:** `tools/webui_demo.py --agent` runs an
  agent over loopback with `tools/fake_hackrf_transfer.py`.

**The worker image.** The `Dockerfile` builds the worker: Ubuntu 24.04 with
dump1090-mutability, multimon-ng and rtl_433 from Ubuntu, AIS-catcher
0.66, acarsdec 3.7 and dumpvdl2 2.6.0 (with libacars 2.2) built from
source, and the app from `uv.lock`, running as uid 1000.
`python -m hackrf_mcp.selfcheck` lists every decoder and fails if one is
missing or a library doesn't resolve. `deploy/worker/compose.yaml` runs it
as three services (live, web, MCP) sharing one `/data` volume, with their
own CPU and memory limits; `deploy/worker/worker.env.example` holds the
settings. `.forgejo/workflows/image.yaml` builds it on a Forgejo mirror of
this fork, runs the self-check, and pushes it to the registry.

**Device tools on a split station.** With `HACKRF_LIVE_AGENT` set, the MCP
server's `hackrf_info`, `hackrf_sweep` and receive `hackrf_transfer` run
through the agent: the live service is paused as usual, the agent runs the
tool (a "run" request with typed fields; it builds the command itself),
and a sweep's data or a capture's samples come back over the connection
and are written to the capture directory on the worker, where
`hackrf_decode` and the other file tools read them. The agent keeps its
own limits: `HACKRF_AGENT_MAX_CAPTURE_MB` (2048 by default) and bias-tee
power locked unless `HACKRF_AGENT_ALLOW_BIAS_TEE` is on. Bias-tee
control, the clock and the Opera Cake are refused on a split station: each
would need its own typed request on the agent. `hackrf_server_status` says
where the HackRF is.

**Transmitting on a split station.** `hackrf_transfer` with
`direction="tx"` first passes the MCP server's own transmit policy and
audit log, as on a single machine. The file is then sent to the agent,
which checks it again with its own settings before `hackrf_transfer -t`
runs:

- **Its settings:** `HACKRF_AGENT_TX_MODE` (off by default),
  `HACKRF_AGENT_TX_BANDS`, `HACKRF_AGENT_TX_EXTRA_RANGES`,
  `HACKRF_AGENT_MAX_TX_SECONDS` and `HACKRF_AGENT_MAX_TX_MB`.
- **Its check:** the file's occupied bandwidth is measured with numpy alone
  (SciPy would not fit the agent's memory limit), the way the MCP server
  measures it, so a narrow signal can use a band narrower than the 2 MS/s
  minimum sample rate.
- **Both must allow it,** and the agent's refusal wins. If the server allows
  a transmission and the agent refuses it, the two configurations differ.
- **Every attempt** is in both audit logs. The agent's is
  `HACKRF_AGENT_TX_LOG`, `/var/lib/hackrf-agent/tx-audit.jsonl` under the
  unit file.
- **The upload is two-phase:** the agent answers a checked request with
  "ready", takes exactly the announced bytes, and checks their SHA-256
  before anything else.

**Fallback.** The machine with the HackRF can keep its own hackrf-live
(local device, its own saved rotation) for when the worker is away. With
`HACKRF_AGENT_FALLBACK_LIVE_DIR` set to that service's `HACKRF_LIVE_DIR`,
the agent holds it paused, with the live service's pause leases, while a
worker is around: streaming, running a tool, or sending the heartbeat its
live service sends every 30 s. After `HACKRF_AGENT_FALLBACK_AFTER_S` (120)
without one, the agent lets go and that machine decodes on its own. The
lease is three minutes and renewed every minute, so if the agent itself
stops, the local service resumes by itself. When the worker is back, its
first stream takes the HackRF over again (the agent pauses the local
service before opening the device), and after each heartbeat the worker
imports what was logged meanwhile: messages, positions, and the local
service's start, stop and error events, each with its own time and marked
as the fallback's. The worker keeps the last ids it imported in
`agent-import.json` in its live directory; the first contact records where
the local log stands and imports nothing, since the log copied over when
the station was split holds the rest. Imported rows reach the log, the
history and the MCP search, not the live view, and pager rows get the
worker's redaction on top of what was stored. The spectrum recorded during
a fallback stays on that machine. The local hackrf-live must not have
`HACKRF_LIVE_AGENT` set: it is the one process there that opens the
HackRF itself.

## Adding a decoder

Each protocol lives in one module in
[`src/hackrf_mcp/protocols/`](src/hackrf_mcp/protocols/), which registers what
it provides when it is imported: an offline decoder for `hackrf_decode`, a
live profile for `hackrf_live_start`, or both. The package imports every
module in it, so a new file there is all it takes; the tools' parameter
choices and descriptions, `hackrf_server_status` and `hackrf_live_summary`
come from the registries. `adsb.py` (both kinds, dump1090 on cu8 samples),
`acars.py` (audio channels for a WAV-reading program) and `multimon.py`
(several protocols from one program) are the ones to copy from.

A minimal offline decoder:

```python
# src/hackrf_mcp/protocols/mydecoder.py (an example)
from hackrf_mcp import decoders, iq
from hackrf_mcp.decoders import Decoder, Job, Result, nothing


def run(job: Job) -> Result:
    # job.cap is the capture (path, rate, center), job.program the binary,
    # job.freqs the channels, job.workdir a scratch directory.
    stdin = iq.iter_cu8_chunks(job.cap.path, max_samples=job.cap.max_samples)
    out = decoders.run_program([job.program, "-"], stdin, job.timeout, job.workdir)
    lines = [line for line in out.output.splitlines() if line.startswith("MSG")]
    if not lines:
        return nothing(job.name, out, [])
    return Result(f"{job.name}: {len(lines)} messages.", [], lines, out.output, [])


decoders.register(
    Decoder(
        "mydecoder",  # the name hackrf_decode takes
        ("my-decoder-program",),  # found on PATH; the first one present runs
        "What it decodes",
        "How to capture for it",
        run,
        help="  mydecoder - what it decodes; capture at ... S/s.",
    )
)
```

A live profile registers a `live_profiles.Profile` the same way, with a
`Pipeline` subclass whose `start()` opens a source (`self.ctx.make_source`,
which is `hackrf_transfer -r -`, or the decoder's own device input like
AIS-catcher), feeds the decoder process (`Proc`), and turns its output into
`live_log.Row`s with `self.ctx.emit`. `ChannelPipeline` already does the
channelising for programs that take audio per channel: subclass it and
supply `decoder_args()`, `convert()` and `on_line()`. Give the profile
`summary_kinds` and `summary_fields` so `hackrf_live_summary` can group
its rows. Call `decoders.run_program` and `decoders.find_program` through
the module (not `from ... import`), so the tests' mocks apply.

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
