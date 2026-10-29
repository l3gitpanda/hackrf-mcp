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
  {"cmd": "stop"}
  {"cmd": "pause", "owner": "hackrf_transfer", "lease_s": 90}
  {"cmd": "resume", "token": "..."}

pause releases the HackRF (the reply comes after the decoder has exited) and
holds it free until resume or until the lease runs out, whichever is first,
so a client that dies mid-call can't leave decoding stopped. The MCP server
pauses the service around every device tool call (see live_client.py).

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

from hackrf_mcp import cli, live_log, live_profiles, settings
from hackrf_mcp.live_log import LiveLogWriter, Row
from hackrf_mcp.live_profiles import Context, FileSource, HackrfSource, Spec, SpecError, Tuning

MIB = 1024 * 1024
# Retry delays after a profile fails, in seconds; the last one repeats.
BACKOFF_S = (2, 5, 10, 30, 60)
# No samples for this long counts as a failure.
STALL_S = 15
# Longest pause lease a client may ask for.
MAX_LEASE_S = 3600


def _now_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class LogPump(threading.Thread):
    """The only user of the database connection: inserts queued rows and
    events about once a second and prunes the log once a minute."""

    def __init__(self, writer: LiveLogWriter, flush_s: float = 1.0, prune_s: float = 60.0):
        super().__init__(daemon=True, name="log-pump")
        self.writer = writer
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

    def event(self, event: str, profile: str | None, detail: str | None) -> None:
        try:
            self.q.put_nowait(("event", (time.time(), event, profile, detail)))
        except queue.Full:
            self.dropped_queue += 1

    def drain(self) -> None:
        rows, events = [], []
        while True:
            try:
                kind, item = self.q.get_nowait()
            except queue.Empty:
                break
            (rows if kind == "row" else events).append(item)
        for ts, event, profile, detail in events:
            self.writer.event(event, profile, detail, ts=ts)
        self.writer.write(rows)

    def run(self) -> None:
        while not self._halt.wait(self.flush_s):
            try:
                self.drain()
                now = time.time()
                if now >= self._next_prune:
                    self._next_prune = now + self.prune_s
                    self.writer.prune(now)
            except Exception as exc:
                print(f"hackrf-live: log write failed: {exc!r}", file=sys.stderr, flush=True)
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
    ):
        self.live = live
        self.pump = pump
        self.make_pipeline = make_pipeline
        self.program_finder = program_finder
        self.clock = clock
        self.run_dir = run_dir or Path(os.environ.get("RUNTIME_DIRECTORY") or live.live_dir / "run")
        self._make_source = make_source or self._hackrf_source
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.shutdown = threading.Event()
        self.started_at = clock()

        self.desired: dict | None = None  # {"spec": Spec} or {"schedule": [(Spec, minutes)], "anchor": t}
        self.hold: dict | None = None  # {"owner", "until", "token", "since", "stopped"}
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

    # --- plumbing -----------------------------------------------------

    def _hackrf_source(self, tuning: Tuning) -> live_profiles.Source:
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

    def _event(self, event: str, profile: str | None = None, detail: str | None = None) -> None:
        print(f"hackrf-live: {event}" + (f" [{profile}]" if profile else "") + (f": {detail}" if detail else ""),
              file=sys.stderr, flush=True)
        if self.pump is not None:
            self.pump.event(event, profile, detail)

    def _context(self) -> Context:
        return Context(
            settings=self.live,
            emit=self._emit,
            count=lambda n: self.decoded.add(n),
            run_dir=self.run_dir,
            make_source=self._make_source,
        )

    def resolve(self, spec: Spec) -> Tuning:
        return live_profiles.resolve(spec, self.live, self.program_finder)

    # --- desired state ------------------------------------------------

    def _target(self, now: float) -> tuple[Spec | None, float | None]:
        """The spec that should be running now, and when that changes."""
        if self.desired is None:
            return None, None
        if "spec" in self.desired:
            return self.desired["spec"], None
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
        name = self.running_spec.profile if self.running_spec else None
        p = self.pipeline
        self.pipeline = None
        try:
            p.stop()
        except Exception as exc:
            self._event("error", name, f"while stopping: {exc!r}")
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
            if cmd == "pause":
                return self._cmd_pause(req)
            if cmd == "resume":
                return self._cmd_resume(req)
        except SpecError as exc:
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

    def _cmd_start(self, req: dict) -> dict:
        now = self.clock()
        if req.get("schedule"):
            entries = []
            for e in req["schedule"]:
                minutes = float(e["minutes"])
                if not 1 <= minutes <= 24 * 60:
                    raise SpecError(f"each schedule entry needs 1-1440 minutes, not {minutes:g}")
                spec = self._spec_from(e)
                self.resolve(spec)  # refuse a bad entry now, not when its turn comes
                entries.append((spec, minutes))
            if len(entries) < 2:
                raise SpecError("a schedule needs at least two entries; start a single profile instead")
            desired = {"schedule": entries, "anchor": now}
        else:
            spec = self._spec_from(req)
            self.resolve(spec)
            desired = {"spec": spec}
        with self.lock:
            self.desired = desired
            self.failures = 0
            self.retry_at = 0.0
            self.last_error = None
            self._save()
            self._event("request", None, "start " + json.dumps(req, default=str)[:300])
        self.supervise()
        return {"ok": True, **self.status()}

    def _cmd_stop(self) -> dict:
        with self.lock:
            self.desired = None
            self._save()
            self._stop_pipeline("stopped on request")
        return {"ok": True, **self.status()}

    def _cmd_pause(self, req: dict) -> dict:
        owner = str(req.get("owner") or "a client")[:80]
        lease = min(max(float(req.get("lease_s", 120)), 1.0), MAX_LEASE_S)
        with self.lock:
            now = self.clock()
            was = self.running_spec.profile if self.running_spec else None
            since = now
            if self.hold is not None:
                # Another holder's lease is replaced; the device is already free.
                was = was or self.hold.get("stopped")
                since = self.hold["since"]
            token = secrets.token_hex(8)
            self.hold = {"owner": owner, "until": now + lease, "token": token, "since": since, "stopped": was}
            self._stop_pipeline(f"paused for {owner}")
            if was:
                self._event("pause", was, f"for {owner}, lease {lease:.0f} s")
        return {"ok": True, "token": token, "paused_profile": was, "lease_s": lease}

    def _cmd_resume(self, req: dict) -> dict:
        with self.lock:
            hold = self.hold
            if hold is None or hold["token"] != req.get("token"):
                return {"ok": True, "resumed": False}
            self.hold = None
            if hold.get("stopped"):
                self._event("resume", hold["stopped"], f"after {hold['owner']} ({self.clock() - hold['since']:.0f} s)")
        self.wake.set()
        return {"ok": True, "resumed": True}

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
            if self.desired is not None:
                if "spec" in self.desired:
                    desired = {"profile": self.desired["spec"].to_json()}
                else:
                    desired = {
                        "schedule": [{**s.to_json(), "minutes": m} for s, m in self.desired["schedule"]],
                        "anchor": self.desired["anchor"],
                    }
            health = self.pipeline.health() if self.pipeline is not None else {}
            out = {
                "state": state,
                "profile": self.running_spec.profile if self.running_spec else None,
                "tuning": self.tuning.describe() if self.tuning else None,
                "running_since": self.running_since,
                "desired": desired,
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
                "now": now,
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
                low_disk=w.stats.low_disk,
                last_prune={
                    "deleted_age": w.stats.last_prune.deleted_age,
                    "deleted_size": w.stats.last_prune.deleted_size,
                },
            )
        out["log"] = log
        return out


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
    pump = LogPump(writer)
    pump.start()
    service = LiveService(live, pump)
    service._event("service-start", None, f"pid {os.getpid()}, pager content {'on' if live.pager_content else 'off'}")
    service.load()
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
        service._event("service-stop", None, None)
        pump.stop()
        writer.close()


def replay(live: settings.LiveSettings, args: argparse.Namespace) -> int:
    """Run one profile over a capture file and print what was logged."""
    path = Path(args.replay)
    spec = Spec(profile=args.profile, freqs_hz=tuple(args.freqs or ()))
    tuning = live_profiles.resolve(spec, live)
    if tuning.profile.name != "ais":
        tuning = replace(tuning, center_hz=args.center, sample_rate_hz=args.rate)
        reach = live_profiles.usable_hz(args.rate)
        outside = [f for f in tuning.freqs_hz if abs(f - args.center) > reach]
        if outside:
            print(f"channels outside the capture: {outside}", file=sys.stderr)
            return 2
    db = Path(args.db)
    writer = LiveLogWriter(db, 365 * 86400, 1 << 40, 0)
    pump = LogPump(writer)
    pump.start()
    realtime = not args.fast or tuning.profile.name == "adsb"
    service = LiveService(
        live,
        pump,
        make_source=lambda t: FileSource(path, args.rate, realtime=realtime),
        run_dir=Path(args.run_dir) if args.run_dir else db.parent / "replay-run",
    )
    ctx = service._context()
    ctx.replay_path = path
    ctx.replay_rate = args.rate
    pipeline = live_profiles.make_pipeline(tuning, ctx)
    started = time.monotonic()
    pipeline.start()
    print(f"replaying {path} as {tuning.describe()}", flush=True)
    while not pipeline.finished():
        if f := pipeline.failure():
            print(f"failed: {f}", file=sys.stderr)
            break
        time.sleep(0.5)
    time.sleep(args.drain)
    pipeline.stop()
    pump.stop()
    took = time.monotonic() - started
    conn = live_log.open_reader(db)
    counts = conn.execute("SELECT kind, count(*) FROM messages GROUP BY kind").fetchall()
    print(f"done in {took:.1f} s; decoded {service.decoded.total} messages; rows: "
          + (", ".join(f"{k} {n}" for k, n in counts) or "none"))
    for r in live_log.search(conn, limit=args.show):
        print(f"  {_now_iso(r['ts'])} {r['kind']} {r['subject']} {r['label'] or ''} "
              f"{(r['freq_hz'] or 0) / 1e6:.4f} MHz level {r['level_db']} | {(r['raw'] or '')[:160]}")
    for line in pipeline.stderr_tail():
        print(f"  stderr {line}")
    writer.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Continuous HackRF decoding into a rolling log.")
    parser.add_argument("--bin-dir", default=os.environ.get("HACKRF_TOOLS_DIR"))
    parser.add_argument("--replay", help="decode this capture file instead of the HackRF, then exit")
    parser.add_argument("--profile", choices=live_profiles.PROFILE_NAMES)
    parser.add_argument("--rate", type=int, help="the capture's sample rate (replay)")
    parser.add_argument("--center", type=int, help="the capture's center frequency (replay)")
    parser.add_argument("--freqs", type=int, nargs="*", help="channels to decode (replay)")
    parser.add_argument("--db", help="database to write (replay)")
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
    if args.replay:
        if not (args.profile and args.rate and (args.center or args.profile == "ais") and args.db):
            parser.error("--replay needs --profile, --rate, --center and --db")
        sys.exit(replay(live, args))
    serve(live)


if __name__ == "__main__":
    main()
