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
- **AIS spectrum.** AIS-catcher opens the HackRF itself, so there is no
  stream to take a spectrum from. Either feed it from `hackrf_transfer` (it
  reads CS8 on stdin, which `decode_ais` already uses) and accept the Python
  in the loop, or show no waterfall while AIS runs.
- **Tracks.** ADS-B and AIS rows are per minute, with the last position in
  the minute. A map with smooth tracks wants positions more often, e.g.
  every 10 s in a separate positions table, or the per-minute row extended
  with the minute's positions.
- **The live waterfall's source.** Only `hackrf-live` holds samples, so the
  UI's backend talks to it (a second socket command, or a sink that
  publishes FFT rows), not to `hackrf-mcp`.

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
5. **Live screen**, 6. **Log screen**, 7. **Map** (Leaflet over
   `positions`; tar1090 is GPL and live-ADS-B-only, so at most a link),
   then History, Summaries, Alerts and Plugins.

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
- **Frontend:** plain modules, with a map library (Leaflet with OSM tiles),
  a canvas waterfall (the OpenWebRX and WebSDR approach: append rows to a
  scrolling canvas) and a table/timeline view.
- **Extension points:** panels registered like protocols. A protocol module
  can name the views its rows support (map, table, waterfall, timeline) and
  the fields they plot. A new decoder should then show up in the UI without
  UI changes, and a new panel should be one file.
- **Reuse before building:** tar1090 for the ADS-B map (it reads the JSON
  dump1090 already writes), AIS-catcher's map for ships, OpenWebRX's
  waterfall code for ideas (AGPL; mind the licence before copying).
