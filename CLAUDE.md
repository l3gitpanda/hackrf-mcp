# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP (Model Context Protocol) server that exposes the `hackrf_tools` CLI
(from <https://github.com/greatscottgadgets/hackrf>) as MCP tools, so Claude can
inspect and control a HackRF SDR connected via USB. It's a local stdio server
built with FastMCP 3.x, managed with `uv`.

## Commands

```bash
uv sync              # install/sync dependencies into .venv
uv run hackrf-mcp    # run the MCP server over stdio
uv run pytest        # run the test suite
uv run pytest tests/test_server.py::test_hackrf_sweep_truncates_large_output  # single test
```

Tests live in `tests/` and mock `hackrf_mcp.server.run` / `hackrf_mcp.cli.run`
(and `subprocess`/`shutil.which`), so they run without a HackRF device or
`hackrf_tools` installed. There is no lint config or CI yet.

To sanity-check tool registration/annotations against a live FastMCP app:

```bash
uv run python -c "
import asyncio
from hackrf_mcp.server import mcp
async def main():
    for t in await mcp.list_tools():
        print(t.name, t.annotations)
asyncio.run(main())
"
```

## Architecture

- `src/hackrf_mcp/cli.py` — single `run(binary, args, timeout)` helper. Locates
  a `hackrf_tools` binary with `shutil.which`, runs it via `subprocess.run`
  (list args, no shell), and returns combined stdout/stderr (+ exit code on
  failure). All tools funnel through this — there is no direct libhackrf/USB
  access.
- `src/hackrf_mcp/server.py` — the FastMCP app (`mcp = FastMCP("hackrf", ...)`)
  and all tool definitions. Each tool is a thin wrapper that builds a CLI arg
  list for the corresponding `hackrf_*` binary and calls `run(...)`. `main()`
  calls `mcp.run()` (stdio transport) and is the `hackrf-mcp` console script
  entry point (see `pyproject.toml`).
- `src/hackrf_mcp/iq.py` — offline numpy/scipy/matplotlib helpers for IQ
  capture files written by `hackrf_transfer -r` (reading int8 IQ, mixing,
  filtering, demodulation, resampling, WAV output, spectrogram/spectrum
  computation, sweep CSV parsing, burst detection, and PNG rendering). Pure
  functions, no device/subprocess access. Used by the `hackrf_iq_to_wav`,
  `hackrf_iq_spectrogram`, `hackrf_iq_info`, `hackrf_iq_detect_bursts`, and
  `hackrf_sweep` (for `output_image_path`) tools in `server.py`.

- `src/hackrf_mcp/settings.py` — the `Settings` dataclass (transmit mode,
  band sets, capture directory, limits), read once in `main()` from
  `HACKRF_MCP_*` env vars and CLI flags. `settings.get()` returns it. No tool
  may change it; the transmit mode is the operator's decision.
- `src/hackrf_mcp/policy.py` — the built-in US band sets (`ism`, `amateur`)
  and `check_tx()`, which decides a transmission from the mode, the carrier
  and the measured occupied span (`iq.file_power_spectrum` +
  `iq.power_bandwidth`).
- `src/hackrf_mcp/storage.py` — `resolve()` confines every tool file path to
  the capture directory; capture size and free-space checks; capture
  listing; the JSON-lines transmit audit log.
- `src/hackrf_mcp/auth.py` — the bearer-token ASGI middleware for
  `--transport http`. HTTP mode refuses to start without a token.
- `src/hackrf_mcp/live_service.py` — `hackrf-live`, a separate process and
  systemd unit (`deploy/hackrf-live.service`) that keeps one decoding
  profile running and owns the HackRF meanwhile. A supervisor loop starts,
  switches, retries (with backoff) and pauses pipelines; a Unix-socket
  control server takes status/start/stop/pause/resume; `LogPump` is the only
  writer of the database. `--replay FILE` runs a profile over a capture.
- `src/hackrf_mcp/decoders.py` — the core of `hackrf_decode`: the `Decoder`
  registry (`register()`), the `Job` a decoder's `run()` gets, `Capture`,
  `run_program()`, `Result`, and the chunked conversion helpers protocol
  modules share (`fm_audio`, `sc16`, `s16`, `channels_in_capture`, ...).
- `src/hackrf_mcp/live_profiles.py` — the core of the live service: the
  `Profile` registry, `Spec`/`Tuning`/`resolve()`, `Proc`, the sources
  (`HackrfSource`, `FileSource`), `Pipeline`, `ChannelPipeline` (decoders
  that take audio channels cut out by `iq.FFTChannelizer`) and
  `MinuteAggregator`.
- `src/hackrf_mcp/protocols/` — one module per protocol family (`adsb`,
  `uat`, `ais`, `acars`, `vdl2`, `multimon`, `rds`). Each registers its
  offline decoder and/or live profile when imported, and holds that
  protocol's conversion, output parsing and pipeline. The package imports
  every module in it (the built-ins in `BUILTIN` order, then any other
  file), so adding a protocol is adding a module; see the README's
  "Adding a decoder". `multimon.py` holds `MultimonParser`, which does the
  pager redaction for both the live log and `hackrf_decode`.
- `src/hackrf_mcp/live_log.py` — the SQLite log: schema, writer (size/age
  pruning, free-space floor) and the read side the MCP tools use.
- `src/hackrf_mcp/live_spectrum.py` — the spectrum history beside the log
  (`spectrum.db`): `SpectrumTap` (512-point FFTs over every block a
  `Source` delivers, one mean+peak row per second of samples),
  `SpectrumRecorder` (a segment per pipeline run, the per-minute roll-up,
  live sinks), `SpectrumWriter` (written from `LogPump`'s thread; own size
  and age limits) and the read side. Sources call the tap through
  `Source._deliver()`, which counts its errors and never lets it stop
  decoding; protocol modules don't see it.
- `src/hackrf_mcp/live_client.py` — the MCP server's side of the socket.
  `_run_on_device()` pauses the live service around every device call.

### Rules for this fork's additions

- Tools that open the device call `_run_on_device()`, not `run()`, so a
  second concurrent call gets `DEVICE_BUSY` instead of fighting over USB.
- Every file path parameter goes through `storage.resolve()` (with
  `must_exist=True` for inputs) before use.
- Refusals are returned as strings starting with `REFUSED:` (`_refused()`).
- Anything that can turn bias-tee power on checks
  `settings.get().allow_bias_tee` first.
- Any new way to transmit must go through `policy.check_tx()` and
  `storage.audit_tx()`, like `hackrf_transfer`.
- `tests/conftest.py` gives every test its own capture directory and audit
  log, with the free-space floor off, and a live-service directory where
  nothing listens.
- Pager (FLEX/POCSAG) message text is dropped in `MultimonParser` before a
  row exists, and before `hackrf_decode` shows or saves anything, unless
  `LiveSettings.pager_content` (`HACKRF_MCP_PAGER_CONTENT`) is set; nothing
  else may store, print or log it (decoder stdout never goes to the
  journal).
- Protocol modules call `decoders.run_program`/`decoders.find_program`
  through the module, never `from hackrf_mcp.decoders import run_program`:
  the tests mock those attributes. A name registered twice is an error.
- The tools' decoder and profile choices (`DecoderName`, `ProfileName`) and
  their lists in `hackrf_decode`/`hackrf_live_start` come from the
  registries (`_fill_doc` replaces the `<<DECODERS>>`/`<<PROFILES>>` lines).
  Don't hard-code a list of protocols anywhere else.
- FastMCP uses only a docstring's first text section as the tool
  description: a standalone `Title:` line followed by an indented block
  starts a new section, and everything after it vanishes from what clients
  see. Introduce lists with a sentence instead;
  `test_no_tool_description_loses_docstring_text` catches it.
- The live service never transmits and never turns on bias-tee power.
- `iq.py` imports matplotlib only inside the PNG renderers, so the live
  service doesn't load it. Keep it that way.
- The spectrum tap runs in each source's thread on every block, so keep it
  cheap: it uses `scipy.fft` (several times faster than `numpy.fft` on
  complex64), and on the QNAP's Celeron it must stay a few percent of a
  core at `HACKRF_LIVE_SPECTRUM_DUTY=1`.
- The socket tests in `tests/test_live_service.py` need `AF_UNIX`; a
  sandbox that blocks it fails them with `PermissionError`.

### Single-device constraint

A HackRF only allows one process to hold its USB connection at a time. The
`FastMCP(... instructions=...)` string tells the model to serialize calls to
hackrf_* tools and explains "device busy"/"resource busy" errors.
`hackrf_sweep` and `hackrf_transfer` additionally note in their docstrings
that they hold the device for up to `timeout_seconds`. If you add a new tool
that opens the device, mention this constraint in its docstring too.

`hackrf_iq_to_wav`, `hackrf_iq_spectrogram`, `hackrf_iq_info`,
`hackrf_iq_detect_bursts`, `hackrf_decode`, `hackrf_decode_ism` and the
`hackrf_live_*` tools are the exception: they only read capture files or
the live log, or talk to the live service, and never open the device, so they can run anytime
(including while another `hackrf_*` tool holds the device). This is called out
explicitly in the `FastMCP(... instructions=...)` string.

### Tool design conventions (follow these for new tools)

- One MCP tool per `hackrf_*` CLI command (Pattern A — small surface, ~9
  commands total).
- Every tool sets `annotations=` to one of the shared `ToolAnnotations`
  constants defined near the top of `server.py`: `READ_ONLY` (read-only
  queries), `CONFIG` (writes config/state but not destructive), or
  `DANGEROUS` (`readOnlyHint=False, destructiveHint=True` — register writes,
  firmware/CPLD flashing).
- Long-running or potentially-unbounded operations (`hackrf_sweep`,
  `hackrf_transfer`) take a `timeout_seconds` param passed to `run()`, which
  relies on `subprocess.run`'s timeout to kill the process. `hackrf_sweep`
  also defaults `one_shot=True` and `hackrf_transfer` rx defaults to a 5s
  capture (via `duration_seconds` -> `num_samples`) so nothing runs forever by
  default.
- `hackrf_sweep` writes CLI output to a temp file (the tool requires `-r
  <file>`), reads it back, truncates to ~50k chars, and deletes the temp file.
- File tools never load a whole capture. They read it in chunks
  (`iq.iter_iq_chunks`, `iq.channelize`, `iq.stream_spectrogram`,
  `iq.stream_stats`, `iq.detect_bursts_file`), so memory stays bounded however
  long the capture is: a full-capture `scipy.signal.spectrogram` on 25 s at
  2 MS/s once took the server to 3.4 GB and the OOM killer. Only
  `hackrf_iq_to_wav` holds an array, after decimating to the channel, capped
  at `MAX_DEMOD_SAMPLES`. When a `max_duration_seconds` cap stops a tool
  early, its result says so (`_coverage()`); never truncate silently.
- `hackrf_decode` runs external decoder programs (`protocols/`). Each
  decoder converts the capture with the chunked helpers, streams it to the
  program's stdin where possible, refuses a capture at a rate the program
  can't use with a message saying what to recapture at, and summarises the
  output. Temporary files go under `<capture dir>/.decode-tmp` (the service
  unit's `ProtectSystem=strict` leaves little else writable). Tests mock
  `decoders.run_program` and `decoders.find_program`.
- **Output schema / mixed content gotcha**: do not annotate a tool's return
  type as `-> list`, `-> dict`, etc. if it can return non-JSON-serializable
  content like `Image`/`Audio`/`File`. FastMCP auto-generates an
  `output_schema` from such annotations; when `pydantic_core.to_jsonable_python`
  then fails on an `Image`/`Audio` in the result, `structured_content` ends up
  empty and the MCP client rejects the whole response with "outputSchema
  defined but no structured output returned" — even though the tool's actual
  work (e.g. writing the PNG/WAV) succeeded. Tools that may return
  `[text, Image(...)]` / `[text, Audio(...)]` (e.g. `hackrf_iq_spectrogram`,
  `hackrf_sweep` with `output_image_path`, `hackrf_iq_to_wav` with
  `return_audio`) should leave the return type unannotated.
- `hackrf_iq_to_wav`'s docstring tells callers to capture with an LO offset
  (~25-50kHz) and pass the real signal frequency as `target_freq_hz`, since a
  signal sitting on HackRF's baseband DC spike demodulates as a loud constant
  tone for nfm/wfm. The tool also appends a runtime warning to its result
  string when `target_freq_hz == center_freq_hz` for nfm/wfm. NFM demod
  additionally DC-blocks the discriminator output (`scipy.signal.butter`
  high-pass) and trims the first 50ms transient; `iq.write_wav` normalizes by
  the 99.5th percentile rather than raw peak for the same reason.
- Tool docstrings double as the MCP tool description shown to the model.
  Tools with real-world side effects (RF transmission, bias-tee power,
  amplifier gain, firmware/register writes) include a `SAFETY:` paragraph
  explaining the risk (regulatory issues for TX, DC bias damaging connected
  equipment, bricking risk for flashing). Keep this pattern when adding tools.

### Currently disabled tools

`hackrf_debug`, `hackrf_cpldjtag`, and `hackrf_spiflash` are fully implemented
at the bottom of `server.py` but commented out (and use the `DANGEROUS`
annotation) because they can write firmware/CPLD/registers and potentially
brick the device. Uncomment to enable; `DANGEROUS` is currently unused while
they're off.

## Safety notes (for any new tools touching RF/hardware state)

- TX (`hackrf_transfer` with `direction="tx"`) is subject to RF transmission
  regulations (e.g. FCC) — document that the user must be authorized for the
  frequency/power used.
- Bias-tee (`hackrf_biast`, or `antenna_enable` on `hackrf_transfer`) applies
  DC power to the antenna port and can damage connected equipment that isn't
  expecting it.
- `amp_enable` adds ~11-14dB gain and can overdrive the receiver front end
  when combined with bias-tee power or strong nearby signals.
- Firmware/CPLD writes (`hackrf_spiflash`, `hackrf_cpldjtag`) can brick the
  device; recovery is via DFU mode + `dfu-util` (see README "Safety notes" and
  <https://hackrf.readthedocs.io/en/latest/updating_firmware.html>).
