"""The live service's feed: everything it decodes, as it happens, for the
web UI's backend (hackrf_mcp.web).

hackrf-live listens on a second Unix socket (LiveSettings.feed_socket_path)
beside the control socket. A client connects and receives JSON lines until
it disconnects; it sends nothing. Each line is one message:

  {"type": "row", ...}       a decoded row, as it is logged (live_log.Row)
  {"type": "event", ...}     a start, stop, pause, error ... (the events table)
  {"type": "position", ...}  a position kept for map tracks (live_log.Position)
  {"type": "fft", ...}       about four times a second while a profile reads
                             the HackRF: mean and peak power per bin since the
                             last one, quantised like the spectrum history
                             (base64 of one byte per bin), with its segment
  {"type": "dropped", "n": N}  N messages were dropped for this client

The feed never blocks the service: each client has a bounded queue, and
while a client is that far behind, new messages for it are dropped; it is
told how many it missed. Pager text never reaches the feed unless the operator
turned it on: rows are redacted when they are parsed, before they exist.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import socket
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from hackrf_mcp.live_log import Position, Row
from hackrf_mcp.live_spectrum import SpectrumRow, quantise

# Messages a client may fall behind by before newer ones are dropped: about
# a minute of FFT rows plus a burst of decoded rows.
QUEUE_MESSAGES = 512


def row_message(row: Row) -> dict:
    return {"type": "row", **asdict(row)}


def position_message(position: Position) -> dict:
    return {"type": "position", **asdict(position)}


def event_message(ts: float, event: str, profile: str | None, detail: str | None) -> dict:
    return {"type": "event", "ts": ts, "event": event, "profile": profile, "detail": detail}


def fft_message(segment: dict, row: SpectrumRow) -> dict:
    db_min, db_step = segment["db_min"], segment["db_step"]
    return {
        "type": "fft",
        "ts": row.ts,
        "seconds": row.seconds,
        "frames": row.frames,
        "profile": segment["profile"],
        "center_hz": segment["center_hz"],
        "sample_rate_hz": segment["sample_rate_hz"],
        "bins": segment["bins"],
        "db_min": db_min,
        "db_step": db_step,
        "mean": base64.b64encode(quantise(row.mean_db, db_min, db_step)).decode(),
        "peak": base64.b64encode(quantise(row.peak_db, db_min, db_step)).decode(),
    }


class _Client:
    def __init__(self, conn: socket.socket, maxsize: int):
        self.conn = conn
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._lock = threading.Lock()
        self.closed = threading.Event()

    def offer(self, line: bytes) -> None:
        try:
            self.q.put_nowait(line)
        except queue.Full:
            with self._lock:
                self.dropped += 1

    def run(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    line = self.q.get(timeout=0.5)
                except queue.Empty:
                    line = None
                # Report a gap as soon as there is room, not only when the
                # next message comes: the feed may have gone quiet.
                with self._lock:
                    n, self.dropped = self.dropped, 0
                if n:
                    self.conn.sendall((json.dumps({"type": "dropped", "n": n}) + "\n").encode())
                if line is not None:
                    self.conn.sendall(line)
        except OSError:
            pass
        finally:
            self.closed.set()
            try:
                self.conn.close()
            except OSError:
                pass


class FeedServer:
    """Accepts feed clients on a Unix socket and copies every published
    message to each of them. publish() is called from decoder and source
    threads, so it only encodes once and queues; each client has a thread
    that writes to it."""

    def __init__(self, path: Path, maxsize: int = QUEUE_MESSAGES):
        self.path = Path(path)
        self.maxsize = maxsize
        self.clients: list[_Client] = []
        self.lock = threading.Lock()
        self.published = 0
        self._halt = threading.Event()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.path))
            except OSError:
                self.path.unlink()
            else:
                raise RuntimeError(f"another hackrf-live feed is already listening on {self.path}")
            finally:
                probe.close()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        os.chmod(self.path, 0o660)
        self.sock.listen(8)
        self.sock.settimeout(1.0)
        self.thread = threading.Thread(target=self._accept, daemon=True, name="feed-accept")

    def start(self) -> FeedServer:
        self.thread.start()
        return self

    def _accept(self) -> None:
        while not self._halt.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(10.0)  # a client that stops reading is dropped
            client = _Client(conn, self.maxsize)
            with self.lock:
                self.clients.append(client)
            threading.Thread(target=client.run, daemon=True, name="feed-client").start()

    def publish(self, message: dict) -> None:
        with self.lock:
            self.clients = [c for c in self.clients if not c.closed.is_set()]
            if not self.clients:
                return
            clients = list(self.clients)
        try:
            line = (json.dumps(message, separators=(",", ":"), default=str) + "\n").encode()
        except (TypeError, ValueError) as exc:
            print(f"hackrf-live: feed message not encodable: {exc!r}", file=sys.stderr, flush=True)
            return
        self.published += 1
        for c in clients:
            c.offer(line)

    def status(self) -> dict:
        with self.lock:
            live = [c for c in self.clients if not c.closed.is_set()]
        return {"path": str(self.path), "clients": len(live), "published": self.published}

    def close(self) -> None:
        self._halt.set()
        try:
            self.sock.close()
        except OSError:
            pass
        with self.lock:
            for c in self.clients:
                c.closed.set()
        self.thread.join(timeout=3)
        try:
            self.path.unlink()
        except OSError:
            pass


def read_feed(path: Path, timeout: float = 5.0):
    """Connect to the feed now, and return an iterator over its messages as
    dicts (for tests and debugging)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))
    return _messages(sock)


def _messages(sock: socket.socket):
    buf = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield json.loads(line)
    finally:
        sock.close()

