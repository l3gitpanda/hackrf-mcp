"""hackrf-live: keeps one decoding profile running on the HackRF and writes
what it decodes to the rolling log (live_log.py).

It runs as its own service (deploy/hackrf-live.service) beside the MCP
server, with its own memory and CPU limits. It is always running; "stopped"
means idle, holding no device. The profile asked for is saved, so after a
restart or reboot the service picks it up again.

Control is a Unix socket (LiveSettings.socket_path) taking one JSON request
per connection, answered with one JSON line:

  {"cmd": "status"}
  {"cmd": "start", "profile": "adsb", "freqs_hz": [...], "lna_gain_db": 32, ...}
  {"cmd": "start", "schedule": [{"profile": "adsb", "minutes": 10}, ...]}
  {"cmd": "start", "day": {"zone": "Europe/Berlin", "parts": [{"name": "night", "start": "23:00",
                            "schedule": [{"profile": ..., "minutes": 10}, ...]}, ...]}}
  {"cmd": "stop"}
  {"cmd": "pause", "owner": "hackrf_transfer", "lease_s": 90}
  {"cmd": "resume", "token": "..."}
  {"cmd": "capture-plan", "from_hz": ..., "to_hz": ..., "decoders": [...], "seconds": 30, ...}
  {"cmd": "capture", ...the same...}
  {"cmd": "capture-list"}
  {"cmd": "capture-decode", "id": "...", "decoders": [...]}
  {"cmd": "capture-delete", "id": "..."}

pause releases the HackRF (the reply comes after the decoder has exited) and
holds it free until resume or until the lease runs out, whichever is first,
so a client that dies mid-call can't leave decoding stopped. The MCP server
pauses the service around every device tool call (see live_client.py).

With HACKRF_LIVE_OCCUPANCY on, the service also takes the HackRF itself
every few minutes for an occupancy sweep (occupancy.py), with the same kind
of hold, and hands it back to whatever should be running.

The capture-* commands are the web UI's captures (captures.py): the service
records a range into the capture directory's web/ with the same kind of
hold, then decodes it. While one records, another client's pause is refused
(with how long is left) rather than taking the HackRF from under it.

`python -m hackrf_mcp.live_service --replay FILE ...` runs one profile over a
capture file instead of the HackRF and prints what was logged.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import secrets
import socket
import socketserver
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from hackrf_mcp import captures, cli, listen, live_day, live_feed, live_log, live_profiles, live_spectrum, occupancy, procstats, settings
from hackrf_mcp import protocols  # noqa: F401  (registers every live profile)
from hackrf_mcp.live_log import LiveLogWriter, Position, Row
from hackrf_mcp.live_profiles import Context, FileSource, HackrfSource, Spec, SpecError, Tuning
from hackrf_mcp.live_spectrum import SpectrumRecorder, SpectrumWriter

MIB = 1024 * 1024
# Retry delays after a profile fails, in seconds; the last one repeats.
BACKOFF_S = (2, 5, 10, 30, 60)
# No samples for this long counts as a failure.
STALL_S = 15
# Longest pause lease a client may ask for.
MAX_LEASE_S = 3600
# A watch lends the receiver for this long unless asked otherwise, and at most
# for WATCH_MAX_MIN; then the rotation (or profile) resumes by itself.
WATCH_DEFAULT_MIN = 15
WATCH_MAX_MIN = 60


def _now_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class LogPump(threading.Thread):
    """The only user of the database connections: inserts queued rows,
    events and spectrum rows about once a second and prunes the log and the
    spectrum history once a minute."""

    def __init__(
        self,
        writer: LiveLogWriter,
        flush_s: float = 1.0,
        prune_s: float = 60.0,
        spectrum_writer: SpectrumWriter | None = None,
    ):
        super().__init__(daemon=True, name="log-pump")
        self.writer = writer
        self.spectrum_writer = spectrum_writer
        self.q: queue.Queue = queue.Queue(maxsize=100_000)
        self.flush_s = flush_s
        self.prune_s = prune_s
        self.dropped_queue = 0
        self._halt = threading.Event()
        self._next_prune = 0.0

    def row(self, row: Row) -> None:
        try:
            self.q.put_nowait(("row", row))
        except queue.Full:
            self.dropped_queue += 1

    def event(self, event: str, profile: str | None, detail: str | None, ts: float | None = None) -> None:
        try:
            self.q.put_nowait(("event", (time.time() if ts is None else ts, event, profile, detail)))
        except queue.Full:
            self.dropped_queue += 1

    def after_write(self, fn) -> None:
        """Call fn on the pump's thread once everything queued before it is
        written; not if the write raises. Rows the writer drops for low disk
        space count as written, as the service's own rows do."""
        self.q.put(("call", fn))

    def position(self, position: Position) -> None:
        try:
            self.q.put_nowait(("position", position))
        except queue.Full:
            self.dropped_queue += 1

    def spectrum(self, item: tuple) -> None:
        """Queue a SpectrumRecorder item for spectrum.db."""
        if self.spectrum_writer is None:
            return
        try:
            self.q.put_nowait(("spectrum", item))
        except queue.Full:
            self.dropped_queue += 1

    def drain(self) -> None:
        rows, events, spectra, positions, calls = [], [], [], [], []
        while True:
            try:
                kind, item = self.q.get_nowait()
            except queue.Empty:
                break
            {"row": rows, "event": events, "spectrum": spectra, "position": positions, "call": calls}[kind].append(item)
        try:
            for ts, event, profile, detail in events:
                self.writer.event(event, profile, detail, ts=ts)
            self.writer.write(rows)
            self.writer.write_positions(positions)
            for fn in calls:
                fn()
        finally:
            # The spectrum history must not hold up the log, nor the log it.
            if spectra and self.spectrum_writer is not None:
                self.spectrum_writer.apply(spectra)

    def run(self) -> None:
        while not self._halt.wait(self.flush_s):
            try:
                self.drain()
                now = time.time()
                if now >= self._next_prune:
                    self._next_prune = now + self.prune_s
                    self.writer.prune(now)
                    if self.spectrum_writer is not None:
                        self.spectrum_writer.prune(now)
            except Exception as exc:
                print(f"hackrf-live: log or spectrum write failed: {exc!r}", file=sys.stderr, flush=True)
        self.drain()

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10)


class Rate:
    """Counts over a sliding window."""

    def __init__(self, window_s: float = 600):
        self.window_s = window_s
        self.items: collections.deque[tuple[float, int]] = collections.deque()
        self.total = 0
        self.lock = threading.Lock()

    def add(self, n: int, now: float | None = None) -> None:
        if n <= 0:
            return
        now = time.time() if now is None else now
        with self.lock:
            self.items.append((now, n))
            self.total += n
            while self.items and now - self.items[0][0] > self.window_s:
                self.items.popleft()

    def over(self, seconds: float, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.lock:
            return sum(n for t, n in self.items if now - t <= seconds)


class LiveService:
    def __init__(
        self,
        live: settings.LiveSettings,
        pump: LogPump | None,
        *,
        make_pipeline=live_profiles.make_pipeline,
        make_source=None,
        program_finder=live_profiles.find_program,
        run_dir: Path | None = None,
        clock=time.time,
        spectrum: SpectrumRecorder | None = None,
    ):
        self.live = live
        self.pump = pump
        # Spectrum history: every source a pipeline makes gets a tap, and
        # each run of a pipeline is one segment of it.
        self.spectrum = spectrum
        self.make_pipeline = make_pipeline
        self.program_finder = program_finder
        self.clock = clock
        self.run_dir = run_dir or Path(os.environ.get("RUNTIME_DIRECTORY") or live.live_dir / "run")
        self._make_source = make_source or self._hackrf_source
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.shutdown = threading.Event()
        self.started_at = clock()
        self.agent_link = None  # the split station's live_import.AgentLink, if any
        self.occupancy: occupancy.Scheduler | None = None
        self.captures: captures.Recorder | None = None
        # Listening (listen.py): {"channels", "until", "since", "owns",
        # "center_hz", "sample_rate_hz"} while a listen runs. "owns" is True
        # when the listen lent itself the receiver (a watch of the listen
        # profile, which ends with it); otherwise it rides whatever the
        # receiver is tuned to, and ends when that moves elsewhere. Not
        # saved. The tap turning the current source's samples into audio is
        # self.audio; its frames go to self.audio_out, its clips to
        # self.clips.
        self.listen: dict | None = None
        self.listen_last: dict | None = None
        self.audio: listen.AudioTap | None = None
        self._audio_source: live_profiles.Source | None = None
        self.audio_out: listen.AudioServer | None = None
        self.clips: listen.ClipStore | None = None
        self.clips_written = 0
        self.clips_skipped = 0
        self.audio_threaded = True  # False: the tap works in the source's thread (tests)

        # {"spec": Spec}, {"schedule": [(Spec, minutes)], "anchor": t} or
        # {"day": live_day.Day} (parts of the day on a local clock).
        self.desired: dict | None = None
        self._part_seen: tuple[int, float] | None = None  # the day plan's part last logged
        self.hold: dict | None = None  # {"owner", "until", "token", "since", "stopped"}
        # A watch in progress: {"spec", "until", "since"}. Not saved: after a
        # restart the rotation is back, so the receiver is never left parked.
        self.watch: dict | None = None
        self.pipeline: live_profiles.Pipeline | None = None
        self.tuning: Tuning | None = None
        self.running_spec: Spec | None = None
        self.running_since: float | None = None
        self.failures = 0
        self.restarts = 0
        self.retry_at = 0.0
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_stderr: list[str] = []
        self.behind = False
        self._last_bytes = (0, 0.0)
        self.decoded = Rate()
        self.rows = Rate()
        self.last_row_at: float | None = None
        self.per_profile_rows: collections.Counter = collections.Counter()
        self.positions_total = 0
        # Besides the log, anything that wants every row as it is decoded
        # can subscribe with add_sink(); attach_feed() does, for the web UI.
        self.sinks: list = []
        self.feed: live_feed.FeedServer | None = None

    # --- plumbing -----------------------------------------------------

    def _hackrf_source(self, tuning: Tuning) -> live_profiles.Source:
        if self.live.agent is not None:
            # Split station: the HackRF is on another machine, behind a
            # hackrf-agent; this one never opens a device itself.
            return live_profiles.RemoteSource(
                self.live.agent,
                self.live.agent_token_file.read_text().strip(),
                tuning.center_hz,
                tuning.sample_rate_hz,
                tuning.lna_gain_db,
                tuning.vga_gain_db,
                tuning.amp,
            )
        return HackrfSource(
            cli._resolve_binary("hackrf_transfer"),
            tuning.center_hz,
            tuning.sample_rate_hz,
            tuning.lna_gain_db,
            tuning.vga_gain_db,
            tuning.amp,
        )

    def _emit(self, row: Row) -> None:
        now = self.clock()
        self.rows.add(1, now)
        self.last_row_at = now
        self.per_profile_rows[row.profile] += 1
        if self.pump is not None:
            self.pump.row(row)
        for sink in self.sinks:
            try:
                sink(row)
            except Exception as exc:  # a broken consumer must not stop decoding
                print(f"hackrf-live: row sink failed: {exc!r}", file=sys.stderr, flush=True)

    def _emit_position(self, position: Position) -> None:
        self.positions_total += 1
        if self.pump is not None:
            self.pump.position(position)
        if self.feed is not None:
            self.feed.publish(live_feed.position_message(position))

    def attach_feed(self, feed: live_feed.FeedServer) -> None:
        """Publish rows, events, positions and live spectrum rows on `feed`."""
        self.feed = feed
        self.add_sink(lambda row: feed.publish(live_feed.row_message(row)))
        if self.spectrum is not None:
            self.spectrum.add_live_sink(lambda seg, row: feed.publish(live_feed.fft_message(seg, row)))

    def add_sink(self, sink) -> None:
        """Call sink(row) for every row as it is decoded, from the decoder's
        thread; it must return quickly."""
        self.sinks.append(sink)

    def _event(self, event: str, profile: str | None = None, detail: str | None = None) -> None:
        print(f"hackrf-live: {event}" + (f" [{profile}]" if profile else "") + (f": {detail}" if detail else ""),
              file=sys.stderr, flush=True)
        if self.pump is not None:
            self.pump.event(event, profile, detail)
        if self.feed is not None:
            self.feed.publish(live_feed.event_message(time.time(), event, profile, detail))

    def _source(self, tuning: Tuning) -> live_profiles.Source:
        source = self._make_source(tuning)
        if self.spectrum is not None:
            source.tap = self.spectrum.tap_for(tuning, source.sample_rate_hz)
        self._attach_audio(source, tuning)
        return source

    # --- listening (under self.lock; the tap takes no lock of its own) ----

    def _attach_audio(self, source: live_profiles.Source | None, tuning: Tuning | None) -> None:
        """Put a tap for the listen on `source`, if a listen runs and its
        channels are inside this tuning."""
        if self.listen is None or source is None or tuning is None:
            return
        channels = self.listen["channels"]
        if not listen.fits(tuning.center_hz, source.sample_rate_hz, channels):
            return
        self._close_audio()
        out = self.audio_out
        try:
            tap = listen.AudioTap(
                tuning.center_hz,
                source.sample_rate_hz,
                channels,
                send=out.send if out is not None else None,
                has_listeners=out.has_clients if out is not None else (lambda: False),
                on_clip=(lambda *a, chs=channels: self._on_clip(chs, *a)) if self.clips is not None else None,
                clip_max_s=self.live.clip_max_s,
                clock=self.clock,
                threaded=self.audio_threaded,
            )
        except (listen.ListenError, ValueError) as exc:
            self._event("error", live_profiles.LISTEN.name, f"could not listen: {exc}")
            return
        source.taps["audio"] = tap
        self.audio = tap
        self._audio_source = source

    def _close_audio(self) -> None:
        if self.audio is None:
            return
        if self._audio_source is not None:
            self._audio_source.taps.pop("audio", None)
        self.audio.close()
        self.audio = None
        self._audio_source = None

    def _on_clip(self, channels, i, ts, ts_end, data, rate, peak_db, continued) -> None:
        """One transmission recorded on an airband channel (from the tap's
        own thread): the file into the clip store, and a row for the log."""
        store = self.clips
        if store is None or not store.disk_ok():
            self.clips_skipped += 1
            return
        c = channels[i]
        wav = listen.ulaw_wav(data, int(round(rate)))
        store.prune(need_bytes=len(wav))
        cid = store.new_id(ts)
        store.write(cid, wav)
        self.clips_written += 1
        fields = {"clip": cid, "seconds": round(ts_end - ts, 1), "mode": c.mode, "bytes": len(wav)}
        if continued:
            fields["continued"] = True
        self._emit(
            Row(
                ts=ts,
                ts_end=ts_end,
                profile=live_profiles.LISTEN.name,
                kind="clip",
                fields=fields,
                freq_hz=c.freq_hz,
                level_db=round(peak_db, 1),
                subject=listen.mhz(c.freq_hz),
                label=c.label,
            )
        )

    def _end_listen(self, why: str) -> None:
        if self.listen is None:
            return
        owns = self.listen["owns"]
        self._close_audio()
        self.listen = None
        self.listen_last = {"ended": self.clock(), "why": why}
        self._event("listen-end", live_profiles.LISTEN.name, why)
        if owns and self.watch is not None and self.watch["spec"].profile == live_profiles.LISTEN.name:
            self._end_watch(why)

    def _listen_tuned(self, now: float) -> tuple[int, int, str] | None:
        """Where the receiver is (or, during a watch, will be) tuned:
        (center, rate, profile)."""
        if self.watch is not None and now < self.watch["until"]:
            w = self.watch["spec"]
            return (w.center_hz, w.sample_rate_hz, w.profile)
        if self.tuning is not None and self.tuning.sample_rate_hz:
            return (self.tuning.center_hz, self.tuning.sample_rate_hz, self.tuning.profile.name)
        return None

    def _listen_plan(self, channels, now: float) -> dict:
        tuned = self._listen_tuned(now)
        rides = tuned is not None and listen.fits(tuned[0], tuned[1], channels)
        if rides:
            center, rate = tuned[0], tuned[1]
        else:
            center, rate = listen.window_center(channels), listen.WINDOW_RATE_HZ
        if self.watch is not None and now < self.watch["until"]:
            running = self.watch["spec"].profile
        elif self.hold is not None and self.hold.get("stopped"):
            running = self.hold["stopped"]
        else:
            running = self.running_spec.profile if self.running_spec else None
        return {
            "retune": not rides,
            "center_hz": center,
            "sample_rate_hz": rate,
            # What runs now: a retune stops it until the listen ends.
            "running": running,
            "riding": tuned[2] if rides else None,
            "until": self.watch["until"] if rides and tuned[2] in live_profiles.SPECIAL else None,
            "channels": [c.to_json() for c in channels],
            "default_minutes": WATCH_DEFAULT_MIN,
            "max_minutes": WATCH_MAX_MIN,
            "held_by": self.hold["owner"] if self.hold is not None and now < self.hold["until"] else None,
            "next_slot": self.occupancy.next_at if self.occupancy is not None else None,
        }

    def _cmd_listen(self, req: dict, plan_only: bool = False) -> dict:
        """Listen to up to listen.MAX_CHANNELS channels. Inside the band the
        receiver is tuned to (a decoder's, or a watch's), it costs no air
        time and lasts until that band moves; anywhere else it lends the
        receiver to a listen.WINDOW_RATE_HZ window around them, like a watch,
        for `minutes` (default WATCH_DEFAULT_MIN), and only with "retune"
        set (the client asked first). Sent again with the same channels it
        only moves their squelch (and, given minutes, the time limit)."""
        if not self.live.listen:
            return {"ok": False, "error": "listening is off on this station (HACKRF_LIVE_LISTEN)"}
        channels = listen.parse_channels(req.get("channels"))
        minutes = req.get("minutes")
        if minutes is not None:
            minutes = float(minutes)
            if not 1 <= minutes <= WATCH_MAX_MIN:
                raise listen.ListenError(f"a listen lasts 1-{WATCH_MAX_MIN} minutes, not {minutes:g}")
        with self.lock:
            now = self.clock()
            plan = self._listen_plan(channels, now)
            if plan_only:
                return {"ok": True, **plan}
            if plan["retune"] and not req.get("retune"):
                return {
                    "ok": False,
                    "retune_needed": True,
                    **plan,
                    "error": "those channels are outside the band the receiver is on: listening there retunes it "
                    + (f"and stops {plan['running']} " if plan["running"] else "")
                    + "until the listen ends; send retune to go ahead",
                }
            prev = self.listen
            if plan["retune"]:
                spec = Spec(
                    profile=live_profiles.LISTEN.name,
                    center_hz=plan["center_hz"],
                    sample_rate_hz=listen.WINDOW_RATE_HZ,
                    lna_gain_db=None if req.get("lna_gain_db") is None else int(req["lna_gain_db"]),
                    vga_gain_db=None if req.get("vga_gain_db") is None else int(req["vga_gain_db"]),
                )
                self.resolve(spec)
                keep = prev is not None and prev["owns"] and minutes is None
                until = prev["until"] if keep else now + 60 * (minutes or WATCH_DEFAULT_MIN)
                since = self.watch["since"] if self.watch is not None else now
                self.watch = {"spec": spec, "until": until, "since": since}
                self.failures = 0
                self.retry_at = 0.0
                self.last_error = None
                owns = True
            elif plan["until"] is not None:
                # Inside a watch: its time limit is the listen's. A listen's
                # own window gets the new limit, if one is given.
                owns = self.watch["spec"].profile == live_profiles.LISTEN.name
                if minutes is not None and owns:
                    self.watch["until"] = now + 60 * minutes
                until = self.watch["until"]
                if minutes is not None and not owns:
                    until = min(until, now + 60 * minutes)
            else:
                owns = False
                keep = prev is not None and not prev["owns"] and minutes is None
                until = prev["until"] if keep else now + 60 * (minutes or WATCH_DEFAULT_MIN)
            changed = prev is None or not listen.same_channels(prev["channels"], channels)
            self.listen = {
                "channels": channels,
                "until": until,
                "since": prev["since"] if prev is not None else now,
                "owns": owns,
                "center_hz": plan["center_hz"],
                "sample_rate_hz": plan["sample_rate_hz"],
            }
            if changed or plan["retune"]:
                self._close_audio()
                if not plan["retune"] and self.pipeline is not None:
                    self._attach_audio(self.pipeline.source, self.tuning)
                where = (
                    f"a {listen.WINDOW_RATE_HZ / 1e6:g} MHz window at {plan['center_hz'] / 1e6:.4f} MHz"
                    if plan["retune"]
                    else f"inside {plan['riding']}"
                )
                self._event(
                    "listen",
                    live_profiles.LISTEN.name,
                    "; ".join(c.describe() for c in channels) + f", {where}, {max(0.0, until - now) / 60:.0f} min",
                )
            elif self.audio is not None:
                self.audio.set_squelch([c.squelch_db for c in channels])
        self.supervise()
        return {"ok": True, **self.status()}

    def _cmd_listen_stop(self) -> dict:
        with self.lock:
            self._end_listen("stopped on request")
        self.supervise()
        return {"ok": True, **self.status()}

    def _cmd_clip_delete(self, req: dict) -> dict:
        if self.clips is None:
            return {"ok": False, "error": "clips are off on this station (HACKRF_LIVE_CLIPS)"}
        cid = listen.ClipStore.check_id(str(req.get("id")))
        freed = self.clips.delete(cid)
        if freed:
            self._event("clip-delete", live_profiles.LISTEN.name, cid)
        return {"ok": True, "id": cid, "freed_bytes": freed}

    def listen_status(self) -> dict:
        """What listening is doing, for status (call under self.lock)."""
        now = self.clock()
        active = None
        if self.listen is not None:
            lis = self.listen
            if self.audio is not None:
                channels = self.audio.status()
                health = self.audio.health()
            else:
                channels = [c.to_json() for c in lis["channels"]]
                health = None
            active = {
                "channels": channels,
                "since": lis["since"],
                "until": lis["until"],
                "left_s": max(0.0, lis["until"] - now),
                "owns": lis["owns"],
                "center_hz": lis["center_hz"],
                "sample_rate_hz": lis["sample_rate_hz"],
                # "playing" while the tap has samples; "waiting" while the
                # receiver is lent elsewhere for a moment (a capture).
                "state": "playing" if self.audio is not None else "waiting",
                "health": health,
            }
        return {
            "enabled": self.live.listen,
            "clips_enabled": self.clips is not None,
            "active": active,
            "last": self.listen_last,
            "clips_written": self.clips_written,
            "clips_skipped": self.clips_skipped,
            "listeners": self.audio_out.status()["clients"] if self.audio_out is not None else 0,
        }

    def _context(self) -> Context:
        return Context(
            settings=self.live,
            emit=self._emit,
            count=lambda n: self.decoded.add(n),
            run_dir=self.run_dir,
            make_source=self._source,
            emit_position=self._emit_position,
        )

    def resolve(self, spec: Spec) -> Tuning:
        return live_profiles.resolve(spec, self.live, self.program_finder)

    # --- desired state ------------------------------------------------

    def _target(self, now: float) -> tuple[Spec | None, float | None]:
        """The spec that should be running now, and when that changes."""
        if self.watch is not None and now < self.watch["until"]:
            return self.watch["spec"], self.watch["until"]
        if self.desired is None:
            return None, None
        if "spec" in self.desired:
            return self.desired["spec"], None
        if "day" in self.desired:
            spec, until, _ = live_day.target(self.desired["day"], now)
            return spec, until
        entries = self.desired["schedule"]
        cycle = sum(m for _, m in entries) * 60
        pos = (now - self.desired["anchor"]) % cycle
        start = 0.0
        for spec, minutes in entries:
            end = start + minutes * 60
            if pos < end:
                return spec, now + (end - pos)
            start = end
        return entries[-1][0], None

    def _save(self) -> None:
        state = {"desired": None}
        if self.desired is not None:
            if "spec" in self.desired:
                state["desired"] = {"spec": self.desired["spec"].to_json()}
            elif "day" in self.desired:
                state["desired"] = {"day": live_day.to_json(self.desired["day"])}
            else:
                state["desired"] = {
                    "schedule": [{"spec": s.to_json(), "minutes": m} for s, m in self.desired["schedule"]],
                    "anchor": self.desired["anchor"],
                }
        path = self.live.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)

    def load(self) -> None:
        try:
            state = json.loads(self.live.state_path.read_text())
        except (OSError, ValueError):
            return
        d = state.get("desired")
        try:
            if d and "spec" in d:
                self.desired = {"spec": Spec.from_json(d["spec"])}
            elif d and "day" in d:
                self.desired = {"day": live_day.from_json(d["day"])}
            elif d and "schedule" in d:
                self.desired = {
                    "schedule": [(Spec.from_json(e["spec"]), float(e["minutes"])) for e in d["schedule"]],
                    "anchor": float(d.get("anchor", self.clock())),
                }
        except (KeyError, TypeError, ValueError) as exc:
            self._event("error", None, f"saved state unreadable, starting idle: {exc}")
            self.desired = None
        if self.desired is not None:
            self._event("resume-saved", None, json.dumps(state["desired"])[:300])

    # --- supervision --------------------------------------------------

    def _stop_pipeline(self, why: str) -> None:
        if self.pipeline is None:
            return
        self._close_audio()
        name = self.running_spec.profile if self.running_spec else None
        p = self.pipeline
        self.pipeline = None
        try:
            p.stop()
        except Exception as exc:
            self._event("error", name, f"while stopping: {exc!r}")
        if self.spectrum is not None:
            self.spectrum.end()
        self.last_stderr = p.stderr_tail()
        self._event("stop", name, why)
        self.running_spec = None
        self.running_since = None
        self.tuning = None
        self.behind = False

    def _fail(self, now: float, profile: str | None, error: str) -> None:
        self.failures += 1
        self.restarts += 1
        delay = BACKOFF_S[min(self.failures - 1, len(BACKOFF_S) - 1)]
        self.retry_at = now + delay
        self.last_error = error
        self.last_error_at = now
        self._event("error", profile, f"{error}; retrying in {delay} s")

    def supervise(self) -> None:
        with self.lock:
            now = self.clock()
            if self.hold is not None and now >= self.hold["until"]:
                self._event("resume", None, f"pause by {self.hold['owner']} expired")
                self.hold = None
            if self.listen is not None and now >= self.listen["until"]:
                self._end_listen("time limit reached")
            if self.watch is not None and now >= self.watch["until"]:
                self._event("watch-end", self.watch["spec"].profile, "time limit reached")
                self.watch = None
            self._log_part(now)
            target, _ = (None, None) if self.hold else self._target(now)
            p = self.pipeline
            if p is not None:
                failure = p.failure()
                if failure is None:
                    failure = self._check_stall(now)
                if failure is not None:
                    self.last_stderr = p.stderr_tail()
                    profile = self.running_spec.profile if self.running_spec else None
                    self._stop_pipeline("failed")
                    self._fail(now, profile, failure)
                elif target != self.running_spec:
                    self._stop_pipeline("switching" if target else "stopped")
                else:
                    self._check_health(now)
                    if self.failures and now - (self.running_since or now) > 60:
                        self.failures = 0
            if self.pipeline is None and target is not None and now >= self.retry_at:
                self._start(target, now)
            # A listen riding a band ends when the receiver moves elsewhere
            # (a capture borrowing it for a moment leaves no tuning, so the
            # listen waits for it instead).
            t = self.tuning
            if self.listen is not None and self.pipeline is not None and t is not None:
                if not listen.fits(t.center_hz, t.sample_rate_hz, self.listen["channels"]):
                    self._end_listen(f"the receiver moved to {t.profile.name}")

    def _log_part(self, now: float) -> None:
        """A "part" event when a day plan's part begins (or the plan is
        set or loaded), whatever has the receiver at that moment."""
        day = self.desired.get("day") if self.desired else None
        if day is None:
            self._part_seen = None
            return
        span = live_day.span_at(day, now)
        if self._part_seen != (span.index, span.since):
            self._part_seen = (span.index, span.since)
            self._event("part", None, live_day.describe(day, span))

    def _check_stall(self, now: float) -> str | None:
        src = self.pipeline.source if self.pipeline else None
        if src is None:
            return None
        n, t = self._last_bytes
        if src.bytes_read != n:
            self._last_bytes = (src.bytes_read, now)
            return None
        if now - t > STALL_S and now - (self.running_since or now) > STALL_S:
            return f"no samples from the HackRF for {STALL_S} s"
        return None

    def _check_health(self, now: float) -> None:
        h = self.pipeline.health() if self.pipeline else {}
        ratio = h.get("input_ratio")
        behind = ratio is not None and ratio < 0.9
        if behind != self.behind:
            self.behind = behind
            self._event(
                "falling-behind" if behind else "keeping-up",
                self.running_spec.profile if self.running_spec else None,
                f"input at {ratio:.0%} of the sample rate" if ratio is not None else None,
            )

    def _start(self, spec: Spec, now: float) -> None:
        try:
            tuning = self.resolve(spec)
        except SpecError as exc:
            self._fail(now, spec.profile, str(exc))
            self.retry_at = now + 60
            return
        try:
            pipeline = self.make_pipeline(tuning, self._context())
            pipeline.start()
        except Exception as exc:
            self._fail(now, spec.profile, f"could not start: {exc}")
            try:
                pipeline.stop()
            except Exception:
                pass
            if self.spectrum is not None:
                self.spectrum.end()
            return
        self.pipeline = pipeline
        self.tuning = tuning
        self.running_spec = spec
        self.running_since = now
        self._last_bytes = (0, now)
        self._event("start", spec.profile, tuning.describe())

    def serve_forever(self, interval_s: float = 0.5) -> None:
        while not self.shutdown.is_set():
            try:
                self.supervise()
            except Exception as exc:
                self._event("error", None, f"supervisor: {exc!r}")
            self.wake.wait(interval_s)
            self.wake.clear()
        with self.lock:
            self._stop_pipeline("service stopping")

    # --- requests -----------------------------------------------------

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        try:
            if cmd == "status":
                return {"ok": True, **self.status()}
            if cmd == "start":
                return self._cmd_start(req)
            if cmd == "stop":
                return self._cmd_stop()
            if cmd == "watch":
                return self._cmd_watch(req)
            if cmd == "watch-stop":
                return self._cmd_watch_stop()
            if cmd == "pause":
                return self._cmd_pause(req)
            if cmd == "resume":
                return self._cmd_resume(req)
            if cmd in ("capture-plan", "capture", "capture-list", "capture-decode", "capture-delete"):
                return self._cmd_capture(cmd, req)
            if cmd == "listen-plan":
                return self._cmd_listen(req, plan_only=True)
            if cmd == "listen":
                return self._cmd_listen(req)
            if cmd == "listen-stop":
                return self._cmd_listen_stop()
            if cmd == "clip-delete":
                return self._cmd_clip_delete(req)
        except (SpecError, captures.PlanError, listen.ListenError) as exc:
            return {"ok": False, "error": str(exc)}
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"bad request: {exc}"}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    @staticmethod
    def _spec_from(req: dict) -> Spec:
        return Spec(
            profile=str(req["profile"]),
            freqs_hz=tuple(int(f) for f in req.get("freqs_hz") or ()),
            lna_gain_db=None if req.get("lna_gain_db") is None else int(req["lna_gain_db"]),
            vga_gain_db=None if req.get("vga_gain_db") is None else int(req["vga_gain_db"]),
            amp=bool(req.get("amp", False)),
        )

    def _decoder_spec(self, req: dict) -> Spec:
        spec = self._spec_from(req)
        if spec.profile == live_profiles.WATCH.name:
            raise SpecError("a watch is started with the watch command, for a limited time, not with start")
        if spec.profile in live_profiles.SPECIAL:
            raise SpecError(f"{spec.profile} is started with its own command, for a limited time, not with start")
        return spec

    def _end_watch(self, why: str) -> None:
        if self.watch is not None:
            self._event("watch-end", self.watch["spec"].profile, why)
            self.watch = None

    def _cmd_watch(self, req: dict) -> dict:
        """Lend the receiver to a watch of one range: the waterfall only,
        for `minutes` (default WATCH_DEFAULT_MIN), then back to what was
        running. A watch already in progress is retuned and keeps its time
        limit unless `minutes` is given."""
        spec = Spec(
            profile=live_profiles.WATCH.name,
            center_hz=int(req["center_hz"]),
            sample_rate_hz=int(req.get("sample_rate_hz") or live_profiles.WATCH_RATES_HZ[-1]),
            lna_gain_db=None if req.get("lna_gain_db") is None else int(req["lna_gain_db"]),
            vga_gain_db=None if req.get("vga_gain_db") is None else int(req["vga_gain_db"]),
            amp=bool(req.get("amp", False)),
        )
        self.resolve(spec)
        minutes = req.get("minutes")
        if minutes is not None and not 1 <= float(minutes) <= WATCH_MAX_MIN:
            raise SpecError(f"a watch lasts 1-{WATCH_MAX_MIN} minutes, not {float(minutes):g}")
        with self.lock:
            now = self.clock()
            if self.watch is None or minutes is not None:
                until = now + 60 * float(minutes if minutes is not None else WATCH_DEFAULT_MIN)
            else:
                until = self.watch["until"]
            since = self.watch["since"] if self.watch is not None else now
            self.watch = {"spec": spec, "until": until, "since": since}
            self.failures = 0
            self.retry_at = 0.0
            self.last_error = None
            self._event("request", None, "watch " + json.dumps(req, default=str)[:300])
        self.supervise()
        return {"ok": True, **self.status()}

    def _cmd_watch_stop(self) -> dict:
        with self.lock:
            self._end_watch("stopped on request")
        self.supervise()
        return {"ok": True, **self.status()}

    def _entries(self, schedule: list) -> list[tuple[Spec, float]]:
        entries = []
        for e in schedule:
            minutes = float(e["minutes"])
            if not 1 <= minutes <= 24 * 60:
                raise SpecError(f"each schedule entry needs 1-1440 minutes, not {minutes:g}")
            spec = self._decoder_spec(e)
            self.resolve(spec)  # refuse a bad entry now, not when its turn comes
            entries.append((spec, minutes))
        return entries

    def _cmd_start(self, req: dict) -> dict:
        now = self.clock()
        if sum(1 for k in ("profile", "schedule", "day") if req.get(k)) > 1:
            raise SpecError("give one of profile, schedule or day")
        if req.get("day"):
            d = req["day"]
            parts = []
            for p in d["parts"]:
                start = live_day.parse_start(p["start"])
                parts.append((p.get("name"), start, self._entries(p.get("schedule") or [])))
            desired = {"day": live_day.make(d["zone"], parts)}
        elif req.get("schedule"):
            entries = self._entries(req["schedule"])
            if len(entries) < 2:
                raise SpecError("a schedule needs at least two entries; start a single profile instead")
            desired = {"schedule": entries, "anchor": now}
        else:
            spec = self._decoder_spec(req)
            self.resolve(spec)
            desired = {"spec": spec}
        with self.lock:
            self._end_watch("a profile was started")
            self.desired = desired
            self._part_seen = None
            self.failures = 0
            self.retry_at = 0.0
            self.last_error = None
            self._save()
            self._event("request", None, "start " + json.dumps(req, default=str)[:300])
        self.supervise()
        return {"ok": True, **self.status()}

    def _cmd_stop(self) -> dict:
        with self.lock:
            self._end_listen("stopped on request")
            self._end_watch("stopped on request")
            self.desired = None
            self._save()
            self._stop_pipeline("stopped on request")
        return {"ok": True, **self.status()}

    def _cmd_pause(self, req: dict, log: bool = True) -> dict:
        owner = str(req.get("owner") or "a client")[:80]
        lease = min(max(float(req.get("lease_s", 120)), 1.0), MAX_LEASE_S)
        with self.lock:
            now = self.clock()
            if self.hold is not None and self.hold["owner"] == captures.OWNER and owner != captures.OWNER and now < self.hold["until"]:
                # The capture's agent run has the HackRF: replacing its hold
                # would only restart decoding against a busy device. (Its
                # time left comes from the hold, not the recorder: the
                # recorder's lock is never taken under this one.)
                left = max(0.0, self.hold["until"] - captures.LEASE_MARGIN_S - now)
                return {
                    "ok": False,
                    "busy": True,
                    "error": f"the HackRF is recording a capture from the web UI for about {left:.0f} s more; try again then",
                }
            was = self.running_spec.profile if self.running_spec else None
            since = now
            renewal = self.hold is not None and self.hold["owner"] == owner
            if self.hold is not None:
                # Another holder's lease is replaced; the device is already free.
                was = was or self.hold.get("stopped")
                since = self.hold["since"]
            token = secrets.token_hex(8)
            self.hold = {"owner": owner, "until": now + lease, "token": token, "since": since, "stopped": was}
            self._stop_pipeline(f"paused for {owner}")
            if was and not renewal and log:
                self._event("pause", was, f"for {owner}, lease {lease:.0f} s")
        return {"ok": True, "token": token, "paused_profile": was, "lease_s": lease}

    def _cmd_resume(self, req: dict, log: bool = True) -> dict:
        with self.lock:
            hold = self.hold
            if hold is None or hold["token"] != req.get("token"):
                return {"ok": True, "resumed": False}
            self.hold = None
            if hold.get("stopped") and log:
                self._event("resume", hold["stopped"], f"after {hold['owner']} ({self.clock() - hold['since']:.0f} s)")
        self.wake.set()
        return {"ok": True, "resumed": True}

    # --- occupancy sweeps -----------------------------------------------

    def occupancy_take(self, lease_s: float) -> dict:
        """Take the HackRF for an occupancy sweep: {"state": "ok", "token",
        "paused_profile"} once the decoder has let go; {"state": "watch"}
        while a watch has it (the slot is skipped); {"state": "busy",
        "owner"} while a device tool holds it (the slot may wait). Checked
        and taken under one lock, so a device tool's pause can't be
        replaced. The slot's own events say what happened, so the hold
        logs no pause or resume of its own."""
        with self.lock:
            now = self.clock()
            if self.listen is not None:
                return {"state": "listen"}
            if self.watch is not None and now < self.watch["until"]:
                return {"state": "watch"}
            if self.hold is not None and now < self.hold["until"]:
                return {"state": "busy", "owner": self.hold["owner"]}
            r = self._cmd_pause({"owner": occupancy.OWNER, "lease_s": lease_s}, log=False)
        return {"state": "ok", "token": r["token"], "paused_profile": r["paused_profile"]}

    def occupancy_give(self, token: str) -> bool:
        """Hand the HackRF back: the supervisor starts whatever should be
        running now (the profile, the rotation's current entry, a watch
        asked for meanwhile). False if the hold had already gone (its lease
        ran out, or a device tool's pause replaced it)."""
        return self._cmd_resume({"token": token}, log=False)["resumed"]

    # --- captures from the web UI ----------------------------------------

    def capture_take(self, lease_s: float) -> dict:
        """Take the HackRF for a capture: {"state": "ok", "token",
        "paused_profile"} once the decoder (or a watch, whose time keeps
        running) has let go; {"state": "busy", "owner"} while a device tool
        or an occupancy sweep holds it. Checked and taken under one lock. The
        capture's own events say what happened, so the hold logs no pause or
        resume of its own."""
        with self.lock:
            now = self.clock()
            if self.hold is not None and now < self.hold["until"]:
                return {"state": "busy", "owner": self.hold["owner"]}
            r = self._cmd_pause({"owner": captures.OWNER, "lease_s": lease_s}, log=False)
        return {"state": "ok", "token": r["token"], "paused_profile": r["paused_profile"]}

    def capture_give(self, token: str) -> bool:
        """Hand the HackRF back after a capture; False if the hold had gone."""
        return self._cmd_resume({"token": token}, log=False)["resumed"]

    def capture_context(self) -> dict:
        """What a capture now would interrupt, for its plan: the profile
        running (or a watch), who holds the receiver, the next occupancy
        slot, and the running profile's channels and band, for the
        decoder of the same name."""
        with self.lock:
            now = self.clock()
            watching = self.watch is not None and now < self.watch["until"]
            held = self.hold["owner"] if self.hold is not None and now < self.hold["until"] else None
            if held is not None and self.hold.get("stopped"):
                running = self.hold["stopped"]
            else:
                running = self.running_spec.profile if self.running_spec else None
            known = {}
            band = None
            if self.tuning is not None:
                if self.tuning.freqs_hz:
                    known[self.tuning.profile.name] = tuple(self.tuning.freqs_hz)
                if self.tuning.sample_rate_hz:
                    half = self.tuning.sample_rate_hz / 2
                    band = {"profile": self.tuning.profile.name, "lo_hz": self.tuning.center_hz - half, "hi_hz": self.tuning.center_hz + half}
        slot = self.occupancy.next_at if self.occupancy is not None else None
        return {
            "running": None if running in live_profiles.SPECIAL else running,
            "watch": watching,
            "listen": self.listen is not None,
            "held_by": held,
            "next_slot": slot,
            "known": known,
            "band": band,
        }

    def _cmd_capture(self, cmd: str, req: dict) -> dict:
        if self.captures is None:
            return {"ok": False, "error": "captures from the web UI are off on this station (HACKRF_LIVE_CAPTURES)"}
        c = self.captures
        if cmd == "capture-plan":
            return {"ok": True, **c.plan_request(req)}
        if cmd == "capture":
            self._event("request", None, "capture " + json.dumps(req, default=str)[:300])
            return {"ok": True, "capture": c.start(req)}
        if cmd == "capture-list":
            return {"ok": True, "captures": c.list(int(req.get("limit") or 200)), "status": c.status()}
        if cmd == "capture-decode":
            return {"ok": True, "capture": c.decode(str(req["id"]), list(req.get("decoders") or ()))}
        return {"ok": True, "freed_bytes": c.delete(str(req["id"]))}

    # --- status ---------------------------------------------------------

    def status(self) -> dict:
        now = self.clock()
        locked = self.lock.acquire(timeout=2)
        try:
            target, next_switch = self._target(now)
            if self.hold is not None:
                state = "paused"
            elif self.pipeline is not None:
                state = "running"
            elif target is not None:
                state = "waiting" if self.last_error else "starting"
            else:
                state = "idle"
            desired = None
            day = None
            if self.desired is not None:
                if "spec" in self.desired:
                    desired = {"profile": self.desired["spec"].to_json()}
                elif "day" in self.desired:
                    plan = self.desired["day"]
                    desired = {"day": live_day.status_json(plan)}
                    span = live_day.span_at(plan, now)
                    nxt = (span.index + 1) % len(plan.parts)
                    day = {
                        "zone": plan.zone,
                        "part": {"index": span.index, "name": plan.parts[span.index].name, "start": plan.parts[span.index].start, "since": span.since, "until": span.until},
                        "next_part": {"index": nxt, "name": plan.parts[nxt].name, "start": plan.parts[nxt].start, "at": span.until},
                        "today": live_day.today(plan, now),
                    }
                else:
                    desired = {
                        "schedule": [{**s.to_json(), "minutes": m} for s, m in self.desired["schedule"]],
                        "anchor": self.desired["anchor"],
                    }
            health = self.pipeline.health() if self.pipeline is not None else {}
            watch = None
            if self.watch is not None:
                w = self.watch["spec"]
                watch = {
                    "profile": w.profile,
                    "center_hz": w.center_hz,
                    "sample_rate_hz": w.sample_rate_hz,
                    "since": self.watch["since"],
                    "until": self.watch["until"],
                    "left_s": max(0.0, self.watch["until"] - now),
                }
            out = {
                "state": state,
                "watch": watch,
                "profile": self.running_spec.profile if self.running_spec else None,
                "tuning": self.tuning.describe() if self.tuning else None,
                "tuning_detail": None
                if self.tuning is None
                else {
                    "profile": self.tuning.profile.name,
                    "center_hz": self.tuning.center_hz,
                    "sample_rate_hz": self.tuning.sample_rate_hz,
                    "freqs_hz": list(self.tuning.freqs_hz),
                    "lna_gain_db": self.tuning.lna_gain_db,
                    "vga_gain_db": self.tuning.vga_gain_db,
                    "amp": self.tuning.amp,
                },
                "running_since": self.running_since,
                "desired": desired,
                "day": day,
                "target": target.profile if target else None,
                "next_switch": next_switch,
                "hold": None
                if self.hold is None
                else {k: self.hold[k] for k in ("owner", "until", "since", "stopped")},
                "decoded_1m": self.decoded.over(60, now),
                "decoded_10m": self.decoded.over(600, now),
                "decoded_total": self.decoded.total,
                "rows_10m": self.rows.over(600, now),
                "rows_total": self.rows.total,
                "rows_by_profile": dict(self.per_profile_rows),
                "positions_total": self.positions_total,
                "last_row_at": self.last_row_at,
                "health": health,
                "falling_behind": self.behind,
                "restarts": self.restarts,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "retry_at": self.retry_at if self.pipeline is None and self.last_error else None,
                "stderr_tail": (self.pipeline.stderr_tail() if self.pipeline else self.last_stderr)[-6:],
                "service_started": self.started_at,
                "pid": os.getpid(),
                # This process's cgroup (its container, or its systemd unit):
                # hackrf-web shows it when it can't read the unit itself.
                "process": procstats.own(),
                "now": now,
                "listen": self.listen_status(),
            }
        finally:
            if locked:
                self.lock.release()
        log = {
            "path": str(self.live.db_path),
            "max_db_mb": self.live.max_db_mb,
            "max_age_days": self.live.max_age_days,
            "min_free_mb": self.live.min_free_mb,
            "pager_content": self.live.pager_content,
        }
        if self.pump is not None:
            w = self.pump.writer
            log.update(
                file_bytes=w.file_bytes(),
                rows_written=w.stats.rows_written,
                rows_dropped=w.stats.rows_dropped + self.pump.dropped_queue,
                positions_written=w.stats.positions_written,
                positions_dropped=w.stats.positions_dropped,
                low_disk=w.stats.low_disk,
                last_prune={
                    "deleted_age": w.stats.last_prune.deleted_age,
                    "deleted_size": w.stats.last_prune.deleted_size,
                },
            )
        out["log"] = log
        out["spectrum"] = self.spectrum_status()
        out["occupancy"] = self.occupancy.status() if self.occupancy is not None else {"enabled": False}
        out["captures"] = self.captures.status() if self.captures is not None else {"enabled": False}
        if self.clips is not None:
            out["listen"]["clips"] = self.clips.status()
        out["listen"]["presets"] = self.live.listen_preset_list()
        out["feed"] = self.feed.status() if self.feed is not None else None
        out["agent"] = self.agent_link.status() if self.agent_link is not None else None
        return out

    def spectrum_status(self) -> dict:
        sp: dict = {"enabled": self.spectrum is not None}
        if self.spectrum is None:
            return sp
        sp.update(
            path=str(self.live.spectrum_db_path),
            max_mb=self.live.spectrum_max_mb,
            max_age_days=self.live.spectrum_max_age_days,
            minute_days=self.live.spectrum_minute_days,
            **self.spectrum.status(),
        )
        w = self.pump.spectrum_writer if self.pump is not None else None
        if w is not None:
            sp.update(
                path=str(w.path),
                file_bytes=w.file_bytes(),
                rows_written=w.stats.rows_written,
                rows_dropped=w.stats.rows_dropped,
                low_disk=w.stats.low_disk,
            )
        return sp


# --- Control socket -----------------------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(65536)
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            resp = self.server.service.handle(req)
        except ValueError as exc:
            resp = {"ok": False, "error": f"bad request: {exc}"}
        self.wfile.write((json.dumps(resp, default=str) + "\n").encode())


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, service: LiveService):
        self.service = service
        path = Path(path)
        if path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
            except OSError:
                path.unlink()
            else:
                raise RuntimeError(f"another hackrf-live is already listening on {path}")
            finally:
                probe.close()
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o660)


# --- Entry points ---------------------------------------------------------------


def serve(live: settings.LiveSettings) -> None:
    live.live_dir.mkdir(parents=True, exist_ok=True)
    writer = LiveLogWriter(
        live.db_path, live.max_age_days * 86400, int(live.max_db_mb * MIB), int(live.min_free_mb * MIB)
    )
    spectrum_writer = None
    if live.spectrum:
        spectrum_writer = SpectrumWriter(
            live.spectrum_db_path,
            live.spectrum_max_age_days * 86400,
            live.spectrum_minute_days * 86400,
            int(live.spectrum_max_mb * MIB),
            int(live.min_free_mb * MIB),
        )
    pump = LogPump(writer, spectrum_writer=spectrum_writer)
    pump.start()
    recorder = SpectrumRecorder(pump.spectrum, duty=live.spectrum_duty) if spectrum_writer else None
    service = LiveService(live, pump, spectrum=recorder)
    feed = live_feed.FeedServer(live.feed_socket_path).start()
    service.attach_feed(feed)
    audio_out = None
    if live.listen:
        audio_out = service.audio_out = listen.AudioServer(live.audio_socket_path).start()
        if live.clips:
            service.clips = listen.ClipStore(live.clips_dir, live.clip_keep_days, live.clip_keep_mb, live.min_free_mb)
    service._event(
        "service-start",
        None,
        f"pid {os.getpid()}, pager content {'on' if live.pager_content else 'off'}, spectrum history "
        + (f"on (duty {live.spectrum_duty:g})" if recorder else "off")
        + ", occupancy sweeps "
        + (f"every {live.occupancy_interval_min} min" if live.occupancy else "off")
        + f", captures from the web UI {'on' if live.captures else 'off'}"
        + f", listening {'on' if live.listen else 'off'}"
        + (f" (clips {'on' if live.clips else 'off'})" if live.listen else ""),
    )
    link = None
    if live.agent is not None:
        # Split station: heartbeats to the hackrf-agent, and what its machine
        # logged while no worker was around (see live_import.py).
        from hackrf_mcp import live_import

        link = live_import.AgentLink(
            pump, live.agent, live.agent_token_file, live.live_dir / "agent-import.json", live.pager_content
        )
        service.agent_link = link
        link.start()
    service.load()
    sched = start_occupancy(service, live) if live.occupancy else None
    recorder = start_captures(service, live) if live.captures else None
    server = ControlServer(live.socket_path, service)
    threading.Thread(target=server.serve_forever, daemon=True, name="control").start()

    import signal

    def _term(signum, frame):
        service.shutdown.set()
        service.wake.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        service.serve_forever()
    finally:
        server.shutdown()
        server.server_close()
        try:
            live.socket_path.unlink()
        except OSError:
            pass
        if link is not None:
            link.stop()
        if sched is not None and sched.stop():
            sched.writer.close()
        if recorder is not None:
            recorder.stop()
        service._event("service-stop", None, None)
        feed.close()
        if audio_out is not None:
            audio_out.close()
        pump.stop()
        writer.close()
        if spectrum_writer is not None:
            spectrum_writer.close()


def start_occupancy(service: LiveService, live: settings.LiveSettings, run_sweep=None) -> occupancy.Scheduler:
    """Open occupancy.db and start the slot schedule. Alert hits are read
    from hackrf-web's alerts.db, in its state directory (the same
    environment gives both services the same one)."""
    writer = occupancy.OccupancyWriter(
        live.occupancy_db_path,
        live.occupancy_max_age_days * 86400,
        live.occupancy_daily_days * 86400,
        int(live.occupancy_max_mb * MIB),
        int(live.min_free_mb * MIB),
    )
    writer.prune()
    try:
        web_state = settings.WebSettings.from_env().state_path(live)
    except ValueError:
        # hackrf-web's own settings are its business: one it would refuse
        # must not stop decoding here. Its default place, then.
        web_state = settings.WebSettings().state_path(live)
    alerts_db = web_state / "alerts.db"
    sched = occupancy.Scheduler(
        service,
        occupancy.Plan.from_live(live),
        writer,
        live.occupancy_interval_min * 60,
        live.occupancy_late_min * 60,
        live.occupancy_alert_quiet_min * 60,
        alerts_db,
        run_sweep or occupancy.sweep_runner(live),
    )
    service.occupancy = sched
    sched.start()
    return sched


def start_captures(
    service: LiveService,
    live: settings.LiveSettings,
    run_capture=None,
    run_decode=None,
    server: settings.Settings | None = None,
    available=None,
) -> captures.Recorder:
    """The web UI's captures, in the capture directory's web/, within the
    MCP server's own limits (the same environment gives both services the
    same ones): its longest capture, its size cap and its free-space floor."""
    s = server or settings.get()
    root = s.data_dir.expanduser()
    store = captures.Store(root / captures.SUBDIR)
    recorder = captures.Recorder(
        service,
        store,
        max_seconds=min(live.capture_max_s, s.max_rx_seconds),
        max_bytes=int(s.max_capture_mb * MIB),
        min_free_bytes=int(s.min_free_mb * MIB),
        keep_bytes=int(live.capture_keep_mb * MIB),
        keep_age_s=live.capture_keep_days * 86400,
        wait_s=live.capture_wait_s,
        run_capture=run_capture or captures.capture_runner(live),
        run_decode=run_decode or captures.decode_runner(live, root / ".decode-tmp"),
        default_gains=(
            live.lna_gain_db if live.lna_gain_db is not None else live_profiles.WATCH.lna_gain_db,
            live.vga_gain_db if live.vga_gain_db is not None else live_profiles.WATCH.vga_gain_db,
        ),
        available=available,
    )
    try:
        gone = recorder.prune_now()
    except OSError as exc:
        service._event("capture-error", None, f"pruning {store.root}: {exc}")
    else:
        if gone:
            service._event("capture-delete", None, f"{len(gone)} kept past {live.capture_keep_days:g} days or the size cap")
    service.captures = recorder
    return recorder


def replay(live: settings.LiveSettings, args: argparse.Namespace) -> int:
    """Run one profile over a capture file and print what was logged."""
    path = Path(args.replay)
    spec = Spec(profile=args.profile, freqs_hz=tuple(args.freqs or ()))
    tuning = live_profiles.resolve(spec, live)
    # A profile with a fixed tuning (adsb, ais) replays a capture made there.
    center = args.center or tuning.profile.center_hz
    if not center:
        print(f"--replay of {tuning.profile.name} needs --center, the capture's center frequency", file=sys.stderr)
        return 2
    tuning = replace(tuning, center_hz=center, sample_rate_hz=args.rate)
    reach = live_profiles.usable_hz(args.rate)
    outside = [f for f in tuning.freqs_hz if abs(f - center) > reach]
    if outside:
        print(f"channels outside the capture: {outside}", file=sys.stderr)
        return 2
    db = Path(args.db)
    writer = LiveLogWriter(db, 365 * 86400, 1 << 40, 0)
    spectrum_db = Path(args.spectrum_db) if args.spectrum_db else db.with_name(db.stem + "-spectrum.db")
    spectrum_writer = SpectrumWriter(spectrum_db, 365 * 86400, 365 * 86400, 1 << 40, 0)
    pump = LogPump(writer, spectrum_writer=spectrum_writer)
    pump.start()
    realtime = not args.fast or tuning.profile.name == "adsb"
    # Rows are stamped with the wall clock; a fast replay squeezes them together.
    recorder = SpectrumRecorder(pump.spectrum, duty=live.spectrum_duty)
    service = LiveService(
        live,
        pump,
        make_source=lambda t: FileSource(path, args.rate, realtime=realtime),
        run_dir=Path(args.run_dir) if args.run_dir else db.parent / "replay-run",
        spectrum=recorder,
    )
    ctx = service._context()
    ctx.replay_path = path
    ctx.replay_rate = args.rate
    pipeline = live_profiles.make_pipeline(tuning, ctx)
    started = time.monotonic()
    try:
        pipeline.start()
    except Exception as exc:
        print(f"could not start {tuning.profile.name}: {exc}", file=sys.stderr)
        pipeline.stop()
        recorder.end()
        pump.stop()
        return 2
    print(f"replaying {path} as {tuning.describe()}", flush=True)
    while not pipeline.finished():
        if f := pipeline.failure():
            print(f"failed: {f}", file=sys.stderr)
            break
        time.sleep(0.5)
    time.sleep(args.drain)
    pipeline.stop()
    recorder.end()
    pump.stop()
    took = time.monotonic() - started
    conn = live_log.open_reader(db)
    counts = conn.execute("SELECT kind, count(*) FROM messages GROUP BY kind").fetchall()
    tracked = conn.execute("SELECT count(*), count(DISTINCT subject) FROM positions").fetchone()
    print(f"done in {took:.1f} s; decoded {service.decoded.total} messages; rows: "
          + (", ".join(f"{k} {n}" for k, n in counts) or "none")
          + f"; positions: {tracked[0]} for {tracked[1]} subjects")
    for r in live_log.search(conn, limit=args.show):
        print(f"  {_now_iso(r['ts'])} {r['kind']} {r['subject']} {r['label'] or ''} "
              f"{(r['freq_hz'] or 0) / 1e6:.4f} MHz level {r['level_db']} | {(r['raw'] or '')[:160]}")
    for line in pipeline.stderr_tail():
        print(f"  stderr {line}")
    _print_spectrum(spectrum_db)
    writer.close()
    spectrum_writer.close()
    return 0


def _print_spectrum(path: Path) -> None:
    """What a replay recorded: rows, and the strongest bins of the mean."""
    try:
        conn = live_spectrum.open_reader(path)
    except live_spectrum.SpectrumNotFound:
        return
    rows = live_spectrum.read(conn, tier=live_spectrum.TIER_SECOND, limit=100_000)
    minutes = conn.execute("SELECT count(*) FROM spectra WHERE tier = ?", (live_spectrum.TIER_MINUTE,)).fetchone()[0]
    print(f"spectrum: {len(rows)} second rows, {minutes} minute rows in {path}")
    if not rows:
        return
    import numpy as np

    mean = np.mean([r["mean"] for r in rows], axis=0)
    peak = np.max([r["peak"] for r in rows], axis=0)
    freqs = live_spectrum.bin_freqs(rows[0]["center_hz"], rows[0]["sample_rate_hz"], mean.size)
    print(f"  noise floor (median bin) {np.median(mean):.1f} dBFS; strongest bins by mean:")
    for i in np.argsort(mean)[::-1][:5]:
        print(f"    {freqs[i] / 1e6:.4f} MHz mean {mean[i]:.1f} peak {peak[i]:.1f} dBFS")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Continuous HackRF decoding into a rolling log.")
    parser.add_argument("--bin-dir", default=os.environ.get("HACKRF_TOOLS_DIR"))
    parser.add_argument("--replay", help="decode this capture file instead of the HackRF, then exit")
    parser.add_argument("--profile", choices=live_profiles.names())
    parser.add_argument("--rate", type=int, help="the capture's sample rate (replay)")
    parser.add_argument("--center", type=int, help="the capture's center frequency (replay)")
    parser.add_argument("--freqs", type=int, nargs="*", help="channels to decode (replay)")
    parser.add_argument("--db", help="database to write (replay)")
    parser.add_argument("--spectrum-db", help="spectrum history to write (replay; default <db>-spectrum.db)")
    parser.add_argument("--run-dir", help="scratch directory for decoder files (replay)")
    parser.add_argument("--fast", action="store_true", help="replay as fast as possible (not ADS-B)")
    parser.add_argument("--drain", type=float, default=3.0, help="seconds to let decoders finish (replay)")
    parser.add_argument("--show", type=int, default=20, help="rows to print (replay)")
    args = parser.parse_args(argv)
    cli.set_bin_dir(args.bin_dir)
    try:
        live = settings.LiveSettings.from_env()
    except ValueError as exc:
        parser.error(f"invalid configuration: {exc}")
    settings.configure_live(live)
    try:
        # The MCP server's settings: the capture directory and its limits,
        # for the web UI's captures.
        settings.configure(settings.Settings.from_env())
    except ValueError as exc:
        print(f"hackrf-live: the MCP server's settings are invalid ({exc}); captures use the defaults", file=sys.stderr)
    if args.replay:
        if not (args.profile and args.rate and args.db):
            parser.error("--replay needs --profile, --rate and --db (and --center unless the profile has a fixed one)")
        sys.exit(replay(live, args))
    serve(live)


if __name__ == "__main__":
    main()
