# hackrf-live UI mockup

Clickable mockup of **hackrf-live**, the planned home SDR station web UI: one HackRF
runs one decoding profile at a time (or a rotation) and writes everything it decodes
to a searchable log. Mockup only: no backend, sample data.

Live canvas (private until shared): https://claude.ai/artifact/7w4vBFC1oVbWzp2YjfjEsx

## What's here

`project/` is the source of the Claude design canvas, one `.dc.html` file per artboard.
The files need the canvas runtime (`support.js`, served by the canvas), so they do not
open on their own in a browser. To republish, publish this folder's `project/` files to
the canvas URL above with this folder as the root.

| File | Screen |
| --- | --- |
| `Main.dc.html` | 1 · Live: waterfall + spectrum, control bar, rotation timeline and editor, live feed, health strip. Its dashed "Mockup" menu switches service states. |
| `Map.dc.html` | 2 · Map: LA basin, aircraft and ships with tracks, live/history scrubber, layers, target side panel, emergency squawks. |
| `Log.dc.html` | 3 · Log explorer: search, detection timeline with coverage gaps, results table, detail drawer with waterfall thumbnail. |
| `History.dc.html` | 4 · History: recorded waterfall per rotation step with clickable message markers, decoded list, channel levels, message rate. |
| `Summaries.dc.html` | 5 · Summaries: coverage today, aircraft, ships, pager capcodes (metadata only), ACARS/VDL2, EAS, 24 h rate per profile. |
| `Alerts.dc.html` | 6 · Alerts: rules, rule editor with preview, hit history. |
| `Plugins.dc.html` | 7 · Plugins: decoders and panels, plus a simulated install showing where a new decoder and panel appear. |
| `States.dc.html` | Not running, idle, paused (countdown), retry after error, falling behind, log near cap, empty search, nothing heard (flat vs decoder error). |
| `Components.dc.html` | Component sheet: colours, type, chips, buttons, waterfall panel, feed items, map targets and card, timeline, rotation strip, level bars, banners. |
| `LiveTablet.dc.html` | Live at tablet width (834 px). |
| `AppBar.dc.html`, `Waterfall.dc.html` | Shared parts imported by the screens. |
| `hl.css` | Shared styles and design tokens. |
| `canvas.json` | Canvas layout and notes. |

## Design rules

- Chrome is greyscale; colour is data. Each profile has one colour (from its manifest, or
  the next free palette slot) used in the feed, map, timelines and charts.
- Frequencies, ICAO addresses, MMSIs, capcodes, callsigns and times are IBM Plex Mono.
- The waterfall palette stays near black at the noise floor and stays dark in the light theme.
- Pagers are metadata only: capcode, type, time, channel; text shows as `[content not logged]`.
- Any capture pauses decoding and shows "Paused by <tool>, resumes automatically".

## How a new decoder or panel slots in

1. A decoder ships a manifest: program and args, tuning (center, rate, channels with an
   emission hint such as burst, pulse, fsk, afsk, msk, carrier), the kinds it emits, which
   field is the subject and which the label, typed fields, supported views, colour or `auto`.
2. The service merges manifests into one registry (`/api/registry`). Profile pickers, feed
   filters, log facets, timeline legends, map layers (only kinds that declare `map`), alert
   fields and summary cards all render from it; no screen lists profiles itself.
3. Unknown kinds use the generic row: subject, label, fields table, raw text.
4. A panel declares a slot (`live.health`, `map.side`, `log.drawer`, `summaries.card`), a
   query (kinds or profiles, fields, window) and a built-in renderer. Screens render whatever
   is registered for their slots.

Example manifests are on the Plugins screen (`ism433` via `rtl_433`, and a `squawk-board` panel).

## Sample data assumptions

- Rotation: ADS-B 10 min, AIS 10, VDL2 5, FLEX 5, started 20:30 UTC; mock "now" is 21:14:07 UTC.
- N4127P (A4F2C3) squawking 7700 was added to show emergency highlighting.
- Squawks for UAL817/SWA3162, ship types and all raw decoder text are invented.
- APRS is shown as "missing program" (direwolf not installed).
- The map basemap is a hand-drawn simplified coastline, not real tiles.
