"""hackrf-agent: the thin half of a split station. It runs where the HackRF
is plugged in, owns the device, and streams its raw samples to a worker on
another machine, which does everything else (decoders, spectrum, log, web
UI). Standard library only, so the machine with the radio needs nothing but
Python and hackrf_tools.

    python -m hackrf_mcp.agent        (deploy/hackrf-agent.service)

Protocol, over one TCP connection per request:

1. The client sends one line of JSON (at most 4 KiB, ending in "\\n"):
       {"token": "...", "cmd": "stream", "center_hz": 98000000,
        "sample_rate_hz": 10000000, "lna_gain_db": 32, "vga_gain_db": 40,
        "amp": false}
   or  {"token": "...", "cmd": "status"}
   or  {"token": "...", "cmd": "run", "tool": "info" | "sweep" | "capture", ...}
   Requests are typed fields, never a command line: the agent builds the
   command itself from checked values, and only ever receives (the tools
   are hackrf_transfer -r, hackrf_sweep and hackrf_info).
2. The agent answers in frames: a 4-byte big-endian payload length, a type
   byte, then the payload. Type 1 is samples (int8 I/Q, as hackrf_transfer
   writes them); type 2 is a notice, one JSON object:
       {"type": "started", "center_hz", "sample_rate_hz", ...}
       {"type": "power", "dbfs": -27.5}       about once a second
       {"type": "end", "reason": "..."}       the stream is over
       {"type": "error", "error": "..."}      refused, then closed
       {"type": "status", ...}                the answer to "status"
       {"type": "done", "exit_code", "stderr", "timed_out", "bytes"}
                                              a "run" finished
   Type 3 is a tool's output (a sweep's CSV, a capture's samples,
   hackrf_info's text), sent as it is written, before "done".
3. A "run" holds the HackRF until its tool exits (or its timeout): a
   stream in progress is ended first, and later requests wait their turn.
   A stream runs until the client closes the connection or the agent ends
   it with an "end" notice: a new stream preempts the old one (there is one
   HackRF), or hackrf_transfer exits. The agent stops hackrf_transfer with
   SIGINT and waits for it, so the device is free before the next one starts.

Only addresses in HACKRF_AGENT_ALLOW may connect, and every request needs a
token whose SHA-256 is in HACKRF_AGENT_TOKEN_SHA256 (or that is in
HACKRF_AGENT_TOKEN_FILE). There is no TLS: raw samples carry everything the
radio hears, so keep the agent on the LAN, allowed to the worker alone.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

FRAME = struct.Struct(">IB")
SAMPLES = 1
NOTICE = 2
DATA = 3
MAX_FRAME = 1 << 20
MAX_REQUEST = 4096
READ_BYTES = 1 << 18
# A client that stops reading (a frozen worker, a dead link that TCP still
# thinks is open) must not hold the agent: a send that can't finish in this
# long ends its stream.
SEND_TIMEOUT_S = 15
MIN_HZ, MAX_HZ = 1_000_000, 6_000_000_000
MIN_RATE, MAX_RATE = 2_000_000, 20_000_000
_POWER_RE = re.compile(r"average power\s+(-?[\d.]+)\s*dBfs")


def frame(kind: int, payload: bytes) -> bytes:
    return FRAME.pack(len(payload), kind) + payload


def notice(obj: dict) -> bytes:
    return frame(NOTICE, json.dumps(obj, separators=(",", ":")).encode())


def read_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """One frame from `sock`, or None at a clean end of the stream."""
    head = _read_exact(sock, FRAME.size)
    if head is None:
        return None
    n, kind = FRAME.unpack(head)
    if n > MAX_FRAME:
        raise ValueError(f"frame of {n} bytes is over the limit")
    body = _read_exact(sock, n)
    if body is None:
        raise ConnectionError("the stream ended inside a frame")
    return kind, body


def _read_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if not buf:
                return None
            raise ConnectionError("the stream ended inside a frame")
        buf += chunk
    return bytes(buf)


# --- settings ---------------------------------------------------------------


@dataclass
class AgentSettings:
    host: str = "127.0.0.1"
    port: int = 8767
    token_sha256: tuple[str, ...] = ()
    token_file: Path | None = None
    allow: tuple[str, ...] = ("127.0.0.1",)
    binary: str = "hackrf_transfer"
    # Where hackrf_info and hackrf_sweep are (default: on PATH).
    tools_dir: str | None = None
    # Limits the agent keeps whatever the worker asks: a capture's size, and
    # DC on the antenna port (off unless HACKRF_AGENT_ALLOW_BIAS_TEE).
    max_capture_mb: int = 2048
    allow_bias_tee: bool = False
    digests: list[bytes] = field(default_factory=list)

    @classmethod
    def from_env(cls, env=None) -> AgentSettings:
        env = os.environ if env is None else env
        s = cls(
            host=env.get("HACKRF_AGENT_HOST", "127.0.0.1"),
            port=int(env.get("HACKRF_AGENT_PORT", "8767")),
            token_sha256=tuple(t.strip().lower() for t in env.get("HACKRF_AGENT_TOKEN_SHA256", "").split(",") if t.strip()),
            token_file=Path(env["HACKRF_AGENT_TOKEN_FILE"]) if env.get("HACKRF_AGENT_TOKEN_FILE") else None,
            allow=tuple(a.strip() for a in env.get("HACKRF_AGENT_ALLOW", "127.0.0.1").split(",") if a.strip()),
            binary=env.get("HACKRF_AGENT_BINARY") or shutil.which("hackrf_transfer") or "hackrf_transfer",
            tools_dir=env.get("HACKRF_AGENT_TOOLS_DIR") or None,
            max_capture_mb=int(env.get("HACKRF_AGENT_MAX_CAPTURE_MB", "2048")),
            allow_bias_tee=env.get("HACKRF_AGENT_ALLOW_BIAS_TEE", "").strip().lower() in ("1", "true", "yes", "on"),
        )
        s.digests = s.load_digests()
        return s

    def load_digests(self) -> list[bytes]:
        out = []
        for d in self.token_sha256:
            if not re.fullmatch(r"[0-9a-f]{64}", d):
                raise ValueError("HACKRF_AGENT_TOKEN_SHA256 holds SHA-256 digests in hex")
            out.append(bytes.fromhex(d))
        if self.token_file is not None:
            token = self.token_file.read_text().strip()
            if len(token) < 32:
                raise ValueError(f"{self.token_file} holds a token shorter than 32 characters")
            out.append(hashlib.sha256(token.encode()).digest())
        if not out:
            raise ValueError("no token: set HACKRF_AGENT_TOKEN_SHA256 or HACKRF_AGENT_TOKEN_FILE")
        return out

    def program(self, name: str) -> str:
        if name == "hackrf_transfer":
            return self.binary
        if self.tools_dir:
            return os.path.join(self.tools_dir, name)
        return shutil.which(name) or name

    def allowed(self, peer: str) -> bool:
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(a, strict=False) for a in self.allow)


def check_stream(req: dict) -> dict:
    """The typed fields of a stream request, checked; ValueError otherwise."""
    center = int(req["center_hz"])
    rate = int(req["sample_rate_hz"])
    lna = int(req.get("lna_gain_db", 32))
    vga = int(req.get("vga_gain_db", 40))
    amp = req.get("amp", False)
    if not MIN_HZ <= center <= MAX_HZ:
        raise ValueError(f"center_hz {center} is outside {MIN_HZ}-{MAX_HZ}")
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ValueError(f"sample_rate_hz {rate} is outside {MIN_RATE}-{MAX_RATE}")
    if not (0 <= lna <= 40 and lna % 8 == 0):
        raise ValueError(f"lna_gain_db must be 0-40 in steps of 8, not {lna}")
    if not (0 <= vga <= 62 and vga % 2 == 0):
        raise ValueError(f"vga_gain_db must be 0-62 in steps of 2, not {vga}")
    if not isinstance(amp, bool):
        raise ValueError("amp must be true or false")
    return {"center_hz": center, "sample_rate_hz": rate, "lna_gain_db": lna, "vga_gain_db": vga, "amp": amp}


SERIAL_RE = re.compile(r"[0-9a-fA-F]{1,32}")
# The longest each tool may hold the HackRF (a worker clamps to these).
TOOL_TIMEOUT_S = {"info": 15.0, "sweep": 300.0, "capture": 900.0}


def _gain_args(req: dict) -> list[str]:
    out = []
    if req.get("lna_gain_db") is not None:
        lna = int(req["lna_gain_db"])
        if not (0 <= lna <= 40 and lna % 8 == 0):
            raise ValueError(f"lna_gain_db must be 0-40 in steps of 8, not {lna}")
        out += ["-l", str(lna)]
    if req.get("vga_gain_db") is not None:
        vga = int(req["vga_gain_db"])
        if not (0 <= vga <= 62 and vga % 2 == 0):
            raise ValueError(f"vga_gain_db must be 0-62 in steps of 2, not {vga}")
        out += ["-g", str(vga)]
    amp = req.get("amp", False)
    if not isinstance(amp, bool):
        raise ValueError("amp must be true or false")
    return out + (["-a", "1"] if amp else [])


def _timeout(req: dict, most: float) -> float:
    t = float(req.get("timeout_s", most))
    if not 1 <= t <= most:
        raise ValueError(f"timeout_s must be 1-{most:g}, not {t:g}")
    return t


def check_run(req: dict, s: AgentSettings) -> tuple[str, list[str], float, int]:
    """A "run" request as (tool name, command line, timeout, most output
    bytes), built by the agent from checked fields; ValueError otherwise."""
    tool = req.get("tool")
    serial = req.get("device_serial")
    if serial is not None and not (isinstance(serial, str) and SERIAL_RE.fullmatch(serial)):
        raise ValueError("device_serial is a hex serial number")
    dev = ["-d", serial] if serial else []
    if tool == "info":
        return "hackrf_info", [s.program("hackrf_info"), *dev], TOOL_TIMEOUT_S["info"], 1 << 20
    if tool == "sweep":
        lo, hi = int(req["freq_min_mhz"]), int(req["freq_max_mhz"])
        if not 0 <= lo < hi <= 7250:
            raise ValueError(f"sweep range {lo}-{hi} MHz must be within 0-7250 and low to high")
        args = [s.program("hackrf_sweep"), "-f", f"{lo}:{hi}"]
        if req.get("bin_width_hz") is not None:
            w = int(req["bin_width_hz"])
            if not 2445 <= w <= 5_000_000:
                raise ValueError(f"bin_width_hz must be 2445-5000000, not {w}")
            args += ["-w", str(w)]
        if req.get("one_shot", True):
            args += ["-1"]
        elif req.get("num_sweeps") is not None:
            n = int(req["num_sweeps"])
            if not 1 <= n <= 10_000:
                raise ValueError(f"num_sweeps must be 1-10000, not {n}")
            args += ["-N", str(n)]
        return "hackrf_sweep", [*args, *_gain_args(req), *dev], _timeout(req, TOOL_TIMEOUT_S["sweep"]), 64 << 20
    if tool == "capture":
        f, rate, n = int(req["frequency_hz"]), int(req["sample_rate_hz"]), int(req["num_samples"])
        if not MIN_HZ <= f <= MAX_HZ:
            raise ValueError(f"frequency_hz {f} is outside {MIN_HZ}-{MAX_HZ}")
        if not MIN_RATE <= rate <= MAX_RATE:
            raise ValueError(f"sample_rate_hz {rate} is outside {MIN_RATE}-{MAX_RATE}")
        if not 0 < 2 * n <= s.max_capture_mb << 20:
            raise ValueError(f"a capture of {n} samples is over this agent's {s.max_capture_mb} MB limit")
        antenna = req.get("antenna_enable", False)
        if antenna is not False and not s.allow_bias_tee:
            raise ValueError("bias-tee power is locked on this agent (HACKRF_AGENT_ALLOW_BIAS_TEE is off)")
        args = [s.binary, "-r", "-", "-n", str(n), "-f", str(f), "-s", str(rate), *_gain_args(req)]
        if antenna is True:
            args += ["-p", "1"]
        return "hackrf_transfer", [*args, *dev], _timeout(req, TOOL_TIMEOUT_S["capture"]), 2 * n + 4096
    raise ValueError(f"unknown tool {tool!r}; this agent runs info, sweep and capture")


def run_child(sock: socket.socket, args: list[str], timeout_s: float, limit: int) -> dict:
    """Run one tool, its stdout to `sock` as DATA frames; the "done" notice."""
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    err = bytearray()

    def drain() -> None:
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            err.extend(chunk)
            del err[: max(0, len(err) - 16384)]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    state = {"timed_out": False}

    def stop(why: str) -> None:
        if proc.poll() is None:
            state[why] = True
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)

    timer = threading.Timer(timeout_s, stop, args=("timed_out",))
    timer.start()
    sent = 0
    try:
        while True:
            buf = proc.stdout.read1(READ_BYTES) if hasattr(proc.stdout, "read1") else proc.stdout.read(READ_BYTES)
            if not buf:
                break
            sent += len(buf)
            if sent > limit:
                stop("over_limit")
                break
            sock.sendall(frame(DATA, buf))
    except OSError:
        stop("client_gone")  # nobody to send to: stop the tool and free the device
    finally:
        timer.cancel()
        code = proc.wait()
        t.join(timeout=2)
    return {
        "type": "done",
        "exit_code": code,
        "stderr": err.decode(errors="replace")[-8000:],
        "timed_out": state["timed_out"],
        "over_limit": state.get("over_limit", False),
        "bytes": sent,
    }


# --- the one stream ---------------------------------------------------------


class Stream:
    """hackrf_transfer for one client, its samples sent in frames."""

    def __init__(self, agent: Agent, sock: socket.socket, peer: str, tuning: dict):
        self.agent = agent
        self.sock = sock
        self.peer = peer
        self.tuning = tuning
        self.since = time.time()
        self.bytes = 0
        self.power: float | None = None
        self.send_lock = threading.Lock()
        self.ended = threading.Event()
        self.end_reason: str | None = None
        args = [
            agent.settings.binary, "-r", "-",
            "-f", str(tuning["center_hz"]), "-s", str(tuning["sample_rate_hz"]),
            "-l", str(tuning["lna_gain_db"]), "-g", str(tuning["vga_gain_db"]),
            "-a", "1" if tuning["amp"] else "0",
        ]  # fmt: skip
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._stderr, daemon=True, name="agent-stderr").start()

    def send(self, data: bytes, wait_s: float | None = None) -> bool:
        """Send `data` whole; False if the client is gone or stuck. With
        `wait_s`, give up at once if another send holds the line that long."""
        if not self.send_lock.acquire(timeout=-1 if wait_s is None else wait_s):
            return False
        try:
            self.sock.sendall(data)
            return True
        except OSError:
            return False
        finally:
            self.send_lock.release()

    def _stderr(self) -> None:
        last = 0.0
        for raw in self.proc.stderr:
            line = raw.decode(errors="replace").strip()
            self.stderr_tail = (self.stderr_tail + [line])[-20:]
            if m := _POWER_RE.search(line):
                self.power = float(m.group(1))
                if time.monotonic() - last >= 0.9:
                    last = time.monotonic()
                    self.send(notice({"type": "power", "dbfs": self.power}))

    def pump(self) -> None:
        """Send samples until the client goes, the agent ends the stream, or
        hackrf_transfer exits; then stop it and close the connection."""
        out = self.proc.stdout
        try:
            while not self.ended.is_set():
                buf = out.read1(READ_BYTES) if hasattr(out, "read1") else out.read(READ_BYTES)
                if not buf:
                    code = self.proc.wait(timeout=5)
                    tail = " ".join(self.stderr_tail[-3:])
                    self.finish(f"hackrf_transfer exited with code {code}" + (f": {tail}" if tail else ""))
                    break
                if len(buf) % 2:
                    buf = buf[:-1]
                self.bytes += len(buf)
                if not self.send(frame(SAMPLES, buf)):
                    self.finish(None)  # the client went away
                    break
        except Exception as exc:  # noqa: BLE001 - the stream ends whatever happened
            self.finish(f"agent error: {exc!r}")
        finally:
            self.stop_child()
            self.agent.forget(self)
            try:
                self.sock.close()
            except OSError:
                pass

    def finish(self, reason: str | None) -> None:
        """Mark the stream over and tell the client why (if it is still there)."""
        if self.ended.is_set():
            return
        self.end_reason = reason
        self.ended.set()
        if reason:
            # A client stuck mid-send doesn't hold this up for long.
            self.send(notice({"type": "end", "reason": reason}), wait_s=2)

    def stop_child(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)

    def status(self) -> dict:
        return {**self.tuning, "since": self.since, "bytes": self.bytes, "client": self.peer, "power_dbfs": self.power}


class Agent:
    def __init__(self, settings: AgentSettings):
        self.settings = settings
        # `lock` guards `current` and is only held briefly, so status always
        # answers; `start_lock` serialises handing the HackRF over.
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.current: Stream | None = None
        self.tool: dict | None = None  # the "run" holding the device, if any
        self.started = time.time()
        self.last_client_seen: float | None = None

    def authorised(self, token: object) -> bool:
        if not isinstance(token, str) or not token:
            return False
        digest = hashlib.sha256(token.encode()).digest()
        return any(hmac.compare_digest(digest, d) for d in self.settings.digests)

    def stream(self, sock: socket.socket, peer: str, req: dict) -> None:
        try:
            tuning = check_stream(req)
        except (KeyError, TypeError, ValueError) as exc:
            sock.sendall(notice({"type": "error", "error": f"bad request: {exc}"}))
            return
        # A tool holds the HackRF for its whole run: refuse at once rather
        # than queue, so a worker that keeps retrying doesn't pile up requests
        # that would preempt each other in turn when the tool ends.
        with self.lock:
            tool = self.tool
        if tool is not None or not self.start_lock.acquire(timeout=10):
            busy = f"running {tool['name']} for {tool['client']}" if tool else "busy"
            sock.sendall(notice({"type": "error", "error": f"the HackRF is {busy}; try again when it finishes"}))
            return
        try:
            with self.lock:
                old = self.current
            if old is not None:
                # One HackRF: the old stream ends, and its hackrf_transfer has
                # exited, before the new one opens the device.
                old.finish(f"preempted by a new stream from {peer}")
                old.stop_child()
            try:
                s = Stream(self, sock, peer, tuning)
            except OSError as exc:
                sock.sendall(notice({"type": "error", "error": f"could not run {self.settings.binary}: {exc}"}))
                return
            with self.lock:
                self.current = s
        finally:
            self.start_lock.release()
        s.send(notice({"type": "started", **tuning}))
        s.pump()

    def run(self, sock: socket.socket, peer: str, req: dict) -> None:
        try:
            name, args, timeout_s, limit = check_run(req, self.settings)
        except (KeyError, TypeError, ValueError) as exc:
            sock.sendall(notice({"type": "error", "error": f"bad request: {exc}"}))
            return
        with self.start_lock:
            with self.lock:
                old = self.current
            if old is not None:
                old.finish(f"preempted by {name} from {peer}")
                old.stop_child()
            with self.lock:
                self.tool = {"name": name, "since": time.time(), "client": peer}
            try:
                done = run_child(sock, args, timeout_s, limit)
            except OSError as exc:
                done = None
                try:
                    sock.sendall(notice({"type": "error", "error": f"could not run {name}: {exc}"}))
                except OSError:
                    pass
            finally:
                with self.lock:
                    self.tool = None
        if done is not None:
            try:
                sock.sendall(notice(done))
            except OSError:
                pass

    def forget(self, s: Stream) -> None:
        with self.lock:
            if self.current is s:
                self.current = None

    def status(self) -> dict:
        with self.lock:
            cur = self.current
        return {
            "type": "status",
            "streaming": cur is not None,
            "stream": cur.status() if cur else None,
            "tool": self.tool,
            "last_client_seen": self.last_client_seen,
            "agent_started": self.started,
            "binary": self.settings.binary,
        }


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        agent: Agent = self.server.agent  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        peer = self.client_address[0]
        if not agent.settings.allowed(peer):
            return  # not even an error frame for a stranger
        sock.settimeout(10)
        try:
            line = b""
            while not line.endswith(b"\n"):
                chunk = sock.recv(MAX_REQUEST + 1 - len(line))
                if not chunk:
                    return
                line += chunk
                if len(line) > MAX_REQUEST:
                    sock.sendall(notice({"type": "error", "error": "request too long"}))
                    return
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("not an object")
            except ValueError:
                sock.sendall(notice({"type": "error", "error": "send one JSON object on a line"}))
                return
            if not agent.authorised(req.get("token")):
                time.sleep(0.5)
                sock.sendall(notice({"type": "error", "error": "not authorised"}))
                return
            agent.last_client_seen = time.time()
            sock.settimeout(SEND_TIMEOUT_S)
            cmd = req.get("cmd")
            if cmd == "status":
                sock.sendall(notice(agent.status()))
            elif cmd == "stream":
                agent.stream(sock, peer, req)
            elif cmd == "run":
                agent.run(sock, peer, req)
            else:
                sock.sendall(notice({"type": "error", "error": f"unknown command {cmd!r}"}))
        except OSError:
            pass


class AgentServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: AgentSettings):
        self.agent = Agent(settings)
        super().__init__((settings.host, settings.port), _Handler)


# --- the client side --------------------------------------------------------


def request(address: tuple[str, int], token: str, req: dict, timeout: float = 5.0) -> socket.socket:
    """Connect to an agent and send one request; returns the connection,
    ready to read frames from."""
    sock = socket.create_connection(address, timeout=timeout)
    sock.sendall((json.dumps({**req, "token": token}) + "\n").encode())
    return sock


class AgentRefused(Exception):
    """The agent refused a request (an "error" notice)."""


def run_tool(address: tuple[str, int], token: str, req: dict, sink, wait_s: float = 1200.0) -> dict:
    """Ask the agent to run a tool; its output goes to `sink` as it comes,
    and the "done" notice is returned. The request may wait for the device
    while another tool runs, hence the long `wait_s`."""
    with request(address, token, {**req, "cmd": "run"}) as sock:
        sock.settimeout(wait_s)
        while True:
            got = read_frame(sock)
            if got is None:
                raise ConnectionError("the hackrf-agent closed the connection before the tool finished")
            kind, body = got
            if kind == DATA:
                sink(body)
            elif kind == NOTICE:
                msg = json.loads(body)
                if msg.get("type") == "done":
                    return msg
                if msg.get("type") == "error":
                    raise AgentRefused(msg["error"])


def status(address: tuple[str, int], token: str, timeout: float = 5.0) -> dict:
    with request(address, token, {"cmd": "status"}, timeout) as sock:
        got = read_frame(sock)
    if got is None or got[0] != NOTICE:
        raise ConnectionError("the agent closed the connection without an answer")
    msg = json.loads(got[1])
    if msg.get("type") == "error":
        raise PermissionError(msg["error"]) if msg["error"] == "not authorised" else ValueError(msg["error"])
    return msg


def main(argv: list[str] | None = None) -> int:
    settings = AgentSettings.from_env()
    server = AgentServer(settings)

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"hackrf-agent on {settings.host}:{settings.port}, allowing {', '.join(settings.allow)}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        with server.agent.lock:
            cur = server.agent.current
        if cur is not None:
            cur.finish("the agent is stopping")
            cur.stop_child()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
