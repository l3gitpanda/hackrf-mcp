# TODO

## Web UI

**Goal.** A browser interface to the HackRF and everything it has decoded,
as extensible as [OpenWebRX](https://github.com/jketterl/openwebrx): new
views should drop in the way new decoders drop into `protocols/`. Wanted:

- **Live:** a waterfall and spectrum of whatever band the live service is on,
  and the current profile, with controls to start, switch, rotate and stop
  profiles (the same calls `hackrf_live_start`/`hackrf_live_stop` make).
- **Maps:** ADS-B aircraft and AIS ships on a map, live and for any past time
  range, with tracks; aircraft and ship lists with their details.
- **Decoded logs:** everything in the live log, browsable and searchable by
  time, profile, ICAO/MMSI/capcode/callsign or text (what
  `hackrf_live_search` and `hackrf_live_summary` do), with a timeline of
  detections and of coverage gaps (the `events` table: starts, stops,
  pauses, errors).
- **History in signal form:** for any past period, a waterfall view next to
  the decoded messages from it, so a detection can be seen as it looked on
  the air. Other views worth having: per-channel level over time, message
  rate per profile, a heat map of positions.
- **Detections:** the same log as now, plus alerts or highlights the user
  defines (a squawk of 7500/7600/7700, an MMSI or callsign of interest, an
  EAS event for a given location code).

### What the current design already gives it

- **The live log** (`live_log.py`): SQLite, WAL mode, readable while the
  service writes, with time, profile, kind, frequency, level, subject, label,
  fields and raw text per row, and an `events` table.
- **Row sinks** (`LiveService.add_sink`): a live feed of every row as it is
  decoded, for a websocket.
- **The registries** (`decoders.DECODERS`, `live_profiles.PROFILES`): what
  exists, with `summary_kinds`/`summary_fields` per profile, so a UI can list
  and group them without hard-coding protocols.
- **dump1090's `aircraft.json`,** written every second to
  `/run/hackrf-live/adsb/` while ADS-B runs: the format tar1090 and
  dump1090's own SkyAware map read, so a ready-made live ADS-B map is close
  to free.
- **AIS-catcher's built-in web server** (`-N <port>`): a live ship map and
  statistics, also nearly free while AIS runs.

### What has to be added first, because it shapes the data

- **Spectrum history.** Done in milestone 1: `live_spectrum.py` records
  mean and peak power in 512 bins per second of samples, and per minute,
  in `spectrum.db` with its own limits (`HACKRF_LIVE_SPECTRUM_*`); see the
  README's "Spectrum history".
- **AIS spectrum.** Done in milestone 2: AIS-catcher now reads
  `hackrf_transfer`'s samples on stdin at 2 MS/s like the other decoders,
  so AIS has a spectrum history, input-rate and stall checks too.
- **Tracks.** Done in milestone 3: a `positions` table in `live.db`, one
  position per aircraft or ship at most every 10 s (every 5 minutes while
  it stands still), pruned with the log; `live_log.positions()` reads it.
- **The live waterfall's source.** Done in milestone 4: `hackrf-live`
  publishes rows, events, positions and FFT rows on `feed.sock`
  (`live_feed.py`), which `hackrf-web` relays to browsers.

### Milestones (agreed 2026-11-01)

One branch and one PR against `homelab` each; deployed to the QNAP only
after the owner approves.

1. **Spectrum history** (`webui-1-spectrum`): the tap, `spectrum.db`, limits,
   status. Mean and peak per bin (the owner chose both over mean only).
2. **AIS through `hackrf_transfer`**: CS8 at 2 MS/s on AIS-catcher's stdin
   (`-r CS8 . -s 2000000`, as `decode_ais` does), so AIS gets spectrum and
   the stall/input checks. Compare messages and stations per 10-minute
   slot with earlier AIS slots before keeping it.
3. **Positions**: a `positions` table in `live.db` (time, profile, subject,
   lat, lon, altitude, speed, track), one per subject at most every 10 s,
   pruned with the log. Not inside the per-minute rows: `positions` there
   is a count that `summarise()` adds up, and `hackrf_live_search` prints
   those fields.
4. **Backend**: a third unit, `hackrf-web` (Starlette/uvicorn, already in
   the lock file), with its own limits. hackrf-live publishes rows, events
   and live FFT rows on a second Unix socket (`feed.sock`, bounded queue per
   subscriber, slow ones dropped); control goes over `control.sock` as the
   MCP tools do. JSON API over the log, events, spectrum and positions;
   websockets for the feed and FFT rows; `/api/registry` from `PROFILES`,
   `DECODERS` and `available()`, with views declared by protocol modules
   (a `views` field on `Profile`). Pager text stripped on read as well.
   Port 8766 behind ufw (source IPs to confirm with the owner), its own token
   (digest), the browser exchanging it once for an HttpOnly SameSite=Strict
   cookie; websockets check the Origin.
5. **Live screen** (done: `web/static/`, checked in headless Chromium
   against `tools/webui_demo.py`), 6. **Log screen** (done: `/log`,
   `log.js`, `/api/log/histogram`, `/api/coverage`), 7. **Map** (done:
   `/map`, `map.js`, `maputil.js`, vendored Leaflet over `positions`, live
   and History with a scrubber and thinning for long ranges; the base map an
   OpenStreetMap extract kept on the station, `web/basemap.py`, drawn by
   vendored protomaps-leaflet. tar1090 was not used: it is GPL, installs
   its own web server as root, shows only live ADS-B, and ADS-B here runs
   half of a rotation, so it would be empty half the time), 8. **History**
   (done: `/history`, `history.js`, `historyutil.js`, from existing routes:
   spectrum segments as rotation steps, the downsampled waterfall, the log
   and its histogram, coverage for gaps; channel levels are computed in the
   browser from the waterfall's peak rows at the profile's default
   channels, since segments don't record a run's custom frequencies),
   9. **Band plan strip** (done: `bandstrip.js`, `bandplan-us.json`, on
   Live, History and the Log's thumbnail), 10. **Official allocations**
   (done: the FCC Table of Frequency Allocations, 47 CFR 2.106, revised
   2022-07-01, parsed from the FCC's PDF by `tools/fcc_us_table.py` into
   `fcc-us-allocations.json`, shown under the strip's pointer readout; to
   refresh it, download a newer PDF and run the two tools), 11.
   **Watch a range** (done, below), 12. **Bands** (done: `/bands`,
   `bands.js`, `bandsearch.js`: search the plan and the official table by
   word, part, frequency or range, and open a band in Watch a range),
   14. **Summaries** (done: `/summaries`, `summaries.js`, `summutil.js`,
   `/api/summaries` and `/api/positions/grid`, scope agreed with the owner
   on 2026-11-03: for a chosen period, per profile, the subjects heard and
   the busiest, the message rate over time, the time on the air from
   coverage, and a heat map of positions; from data already logged, no new
   storage), 15. **Alerts** (agreed with the owner on 2026-11-04: in the UI
   plus a webhook; done: `web/alerts.py` with rules, hits and a watermark
   in hackrf-web's own `alerts.db`, `/alerts`, `alerts.js`, `alertutil.js`,
   the app bar's count and the banner on every screen, the webhook from a
   URL file, and subjects with an unacknowledged hit highlighted: a red
   ring on the Map, a red edge and an "alert" tag in the Live feed and the
   Log), 17. **Plugins** (agreed with the owner on 2026-11-04:
   the catalogue plus panel cards; decoders stay Python modules in the
   reviewed image; done: `/plugins`, `plugins.js`, `panelutil.js`,
   `web/panels.py` reading data-only TOML panel files from the web state
   directory's `panels` folder, `/api/plugins`, `/api/panels`, panel cards
   on Summaries). More slots (the Map's side, the Log's drawer, Live's
   health) when a panel wants one.

### Watching a custom range (milestone 11)

Agreed with the owner on 2026-11-03: tune and watch any range up to about
10 MHz wide, scrolling left and right retunes, decoding pauses meanwhile.

- **The receiver is lent, not given.** A watch takes over from the
  rotation until it is stopped or its time limit runs out (default 15
  minutes), then the rotation resumes by itself. The limit lives in the
  live service, not the page: closing the tab must not leave the receiver
  parked.
- **Device tools** pause a watch like they pause a profile; it resumes
  afterwards. Its time limit keeps counting during the pause.
- **Status and the app bar** say so: "Watching 88–98 MHz, 11 min left".
  Coverage and History need a state for it.
- **Recorded:** a watch writes to the spectrum history like a profile,
  as profile `watch`. Every retune is a new segment (History shows each
  as a step); the page retunes once per click or drag, not continuously.
- **Done:** the live service's `watch` / `watch-stop` commands and the
  `WATCH` profile (`WatchPipeline`, not registered with the decoders, so
  the MCP tools are unchanged; `start` refuses it), `POST /api/live/watch`
  and `/api/live/watch/stop`, `hackrf_live_status` saying so, and the
  Live screen's range form, scroll buttons, waterfall drag, +15 min and
  stop. Widths 2, 5, 8 and 10 MHz; 1-60 minutes, 15 by default; a watch
  is not saved, so a restart brings the rotation back.
- **CPU:** measure the tap at 10 MS/s offline first (a synthetic file,
  paced, not a tight loop), apply the ~3x factor the QNAP showed for
  milestone 1, and confirm on the box. Start at 512 bins (19.5 kHz at
  10 MS/s); `segments.bins` already allows more.
- **Same rules:** receive only (`hackrf_transfer -r`), the same Gate and
  Origin checks as start and stop. The Map's History mode could add a per-minute roll-up of
   positions: thinning a week of fixes reads all of them (about a second
   on a desktop, several on the QNAP). On real data the Map's labels piled
   up over a busy approach at the zoom that fits the whole area; done
   (milestone 16): labels that would overlap a more important one are left
   out (the selected target first, then those with an alert, the moving,
   the newest), shown again when the pointer is on the marker.

### Split station (milestone 13, agreed 2026-11-03)

The owner wants the processing off the box with the radio, Pelican
panel/wings style, so CPU stops being the limit: a thin **hackrf-agent**
next to the HackRF, and a **worker** container on a bigger machine in the
owner's Docker stack doing everything else.

- **Decided:** the agent owns the HackRF and streams raw I/Q; the worker
  runs the decoders, spectrum, log, web UI and MCP server, with its data
  in a Docker volume; when no worker is connected the agent's machine
  decodes locally (today's setup) and the worker imports what it logged.
  The web and MCP ports move to the worker at cutover.
- **W1 (done):** `agent.py` (framed protocol, token and address allowlist,
  one stream at a time with preemption, typed receive-only requests,
  status with `last_client_seen`), `RemoteSource`, `HACKRF_LIVE_AGENT`,
  `deploy/hackrf-agent.service`, `tools/fake_hackrf_transfer.py` and
  `webui_demo.py --agent`. Not deployed.
- **W2:** the worker image (`Dockerfile`: Ubuntu 24.04, dump1090-mutability,
  multimon-ng and rtl_433 packaged; AIS-catcher 0.66, acarsdec 3.7 and
  dumpvdl2 2.6.0 with libacars 2.2 from source, the versions the original
  station ran), `hackrf_mcp.selfcheck`, `deploy/worker/compose.yaml` (live,
  web and MCP services sharing `/data`, each with CPU and memory limits),
  and `.forgejo/workflows/image.yaml` (built on a Forgejo pull mirror of
  this fork, self-checked, pushed as `:sha-<12>` and `:homelab`). The
  owner sets up the mirror and its `PACKAGERUNNER_TOKEN` secret. The uat
  (dump978) and rds (redsea) capture decoders stay out, as on the
  original station.
- **W3 (done):** the MCP server's `hackrf_info`, `hackrf_sweep` and receive
  captures through the agent: a typed "run" request (info, sweep, capture)
  whose output comes back in DATA frames and is written to the worker's
  capture directory. The agent enforces its own capture-size cap and
  bias-tee lock, holds the device for the tool's run (a stream is
  preempted with a reason), and stops a tool at its timeout or when its
  client goes. Transmitting, bias-tee control, clock and Opera Cake stay
  refused on a split station; transmitting through the agent needs its
  own approval, since the transmit policy and its audit log would move
  to the agent, the only thing touching the device.
- **W4 (done):** fallback. The QNAP keeps its hackrf-live; the agent
  (`HACKRF_AGENT_FALLBACK_LIVE_DIR`) holds it paused with renewed
  three-minute pause leases while a worker streams, runs a tool or sends
  its 30 s heartbeat, and lets go after 2 minutes without one (or by
  itself within the lease if the agent dies). Every stream or tool pauses
  it before opening the HackRF. The worker's live service imports what was
  logged meanwhile through the agent's "export" (messages, positions,
  start/stop/error events), with a watermark in `agent-import.json`;
  fallback spectrum stays on the QNAP. The QNAP's hackrf-live keeps no
  `HACKRF_LIVE_AGENT`.
- **W6 (done, ahead of W5 at the owner's request):** transmitting through
  the agent. The MCP server's policy and audit log first, then a two-phase
  upload ("ready", exact bytes, SHA-256) and the agent's own policy
  (`HACKRF_AGENT_TX_*`, off by default) with the file's bandwidth measured
  in numpy alone, and its own audit log in the unit's StateDirectory. Both
  must allow a transmission. Bias-tee control, clock and Opera Cake stay
  refused on a split station.
- **W5:** cutover, with the owner's approval: the agent's port allowed to
  the worker only, the data copied to the worker's volume, the web and MCP
  addresses moved. Order matters for the log, since the worker's first
  contact records the QNAP's ids and imports nothing: stop the QNAP's
  hackrf-live, copy live.db (and spectrum.db), start the agent and the
  worker so its first contact sees the ids the copy has, then start the
  QNAP's hackrf-live again as the fallback. Rows logged on the QNAP between
  the copy and the first contact would reach neither side; a worker started
  before the copy would import rows the copy also has.

### Next features (chosen by the owner on 2026-11-04)

Every milestone above is built. These are next, each its own pull request
against `homelab`, deployed only with the owner's approval:

- **Receiver range map (done):** how far the antenna hears in each
  direction, from the positions table and `HACKRF_LIVE_RECEIVER` (the
  farthest fix per bearing, level against distance), to judge the antenna
  and any move. The Map's *Receiver range* layer; see the README.
- **Aircraft details from an offline database:** registration, type,
  operator and a military or government flag per ICAO address, from a
  downloadable aircraft database kept on the worker, for Alerts' fields and
  Summaries' names. Which database, its licence and how it is refreshed
  are to be agreed first.
- **ADS-C and CPDLC positions from datalink:** libacars already unpacks
  them in the ACARS and VDL2 decoders; put those positions on the Map, so
  aircraft beyond ADS-B range show too.
- **Capture from the waterfall:** drag across a signal on Live or History
  to record it into the capture directory (through the agent, pausing live
  decoding as a device tool does), then run a decoder on it.
- **Spectrum occupancy over time:** short scheduled sweeps (for example
  24-1700 MHz hourly, about a second of the radio each) building a heat map
  of what is on the air when, shown with the band plan.
- **Meshtastic / LoRa:** a decoder for mesh nodes (LoRa chirp demodulation
  of the US LongFast and other presets), with their positions on the Map.
  The capture decoders `meshtastic` and `lora` are in (#35); the live
  profile `meshtastic` follows. Still to do: hearing a real node. The
  capture directory's Meshtastic captures hold no LoRa at all, so the
  Meshtastic layer (header, AES-CTR, protobuf) is proven on synthetic
  packets only; the owner will key a node during a capture.
- **ISM sensors as a live profile:** rtl_433 continuously on 433.92 or
  915 MHz. It hears the neighbours' weather stations, tyre sensors and
  meters, so what it logs and shows by default is the owner's decision
  before anything is built.
- **Listening in the browser:** live audio demodulated on the worker
  (airband AM 118-137 MHz, NOAA Weather Radio, FM) streamed to the page,
  and voice clips recorded with squelch into the Log. Audio only: the
  browser still never gets I/Q samples.

Decided against for now: HTTPS for the LAN UI, feeding community networks
(no ADS-B or AIS aggregators: the station's data and position stay home),
and a second receiver (an RTL-SDR for ADS-B around the clock).

### Constraints

- **The QNAP:** 2 Celeron cores, one of them taken by Kodi, and 3.7 GiB of
  RAM. FFTs for the waterfall belong in the live service, which already
  channelises with one FFT per block; the browser should get rows, not
  samples. The UI backend gets its own memory and CPU limits like the other
  units.
- **Exposure:** LAN only, behind the QNAP's ufw like port 8765, and behind a
  token or the same bearer-token scheme as the MCP. No public URL.
- **Privacy:** the browser sees exactly what the log holds. Pager text stays
  hidden unless `HACKRF_MCP_PAGER_CONTENT` is on.
- **One device:** the UI controls the live service and never opens the
  HackRF itself; ad-hoc captures go through the same pause-and-resume path
  as the MCP's device tools.

### Building it

- **Backend:** a small async web app (e.g. Starlette, already a dependency
  through FastMCP) serving the log over a JSON API and the live feed over a
  websocket, as a third unit or inside `hackrf-mcp`'s HTTP server on another
  path.
- **Frontend:** plain modules, with a map library (Leaflet, with vector
  tiles from a local extract),
  a canvas waterfall (the OpenWebRX and WebSDR approach: append rows to a
  scrolling canvas) and a table/timeline view.
- **Extension points:** panels registered like protocols. A protocol module
  can name the views its rows support (map, table, waterfall, timeline) and
  the fields they plot. A new decoder should then show up in the UI without
  UI changes, and a new panel should be one file.
- **Reuse before building:** tar1090 for the ADS-B map (it reads the JSON
  dump1090 already writes), AIS-catcher's map for ships, OpenWebRX's
  waterfall code for ideas (AGPL; mind the licence before copying).
