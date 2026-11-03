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
   Requests are typed fields, never a command line: the agent only ever
   runs `hackrf_transfer -r -` (receive) with checked values.
2. The agent answers in frames: a 4-byte big-endian payload length, a type
   byte, then the payload. Type 1 is samples (int8 I/Q, as hackrf_transfer
   writes them); type 2 is a notice, one JSON object:
       {"type": "started", "center_hz", "sample_rate_hz", ...}
       {"type": "power", "dbfs": -27.5}       about once a second
       {"type": "end", "reason": "..."}       the stream is over
       {"type": "error", "error": "..."}      refused, then closed
       {"type": "status", ...}                the answer to "status"
3. A stream runs until the client closes the connection or the agent ends
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
        with self.start_lock:
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
        s.send(notice({"type": "started", **tuning}))
        s.pump()

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
