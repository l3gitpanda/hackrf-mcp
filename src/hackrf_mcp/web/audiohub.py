"""Relays the live service's audio socket (listen.AudioServer) to browsers'
/ws/audio websockets, one binary message per frame, as they come.

The hub connects only while at least one browser listens (the live service
skips building audio frames nobody takes), and lets go when the last one
leaves. Each browser has a bounded queue; one that falls behind loses frames
and is sent a DROPPED frame saying how many, so a slow phone can't hold up
the rest. The browser gets audio and levels only: the live service never
puts samples of the radio on this socket.
"""

from __future__ import annotations

import asyncio
import struct
import time
from pathlib import Path

from hackrf_mcp import listen

# Frames a browser may fall behind by: about five seconds of four channels'
# audio and meters.
CLIENT_QUEUE = 256
# Longest frame accepted from the socket (a frame of 8 kHz audio is a few KB).
MAX_FRAME = 1 << 20


class AudioClient:
    def __init__(self) -> None:
        self.q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.dropped = 0

    def offer(self, data: bytes) -> None:
        try:
            self.q.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped += 1


class AudioHub:
    def __init__(self, path: Path, retry_s: float = 2.0):
        self.path = Path(path)
        self.retry_s = retry_s
        self.clients: set[AudioClient] = set()
        self.connected = False
        self.frames = 0
        self.last_frame_at: float | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._wanted: asyncio.Event | None = None
        self._writer: asyncio.StreamWriter | None = None

    def _event(self) -> asyncio.Event:
        if self._wanted is None:
            self._wanted = asyncio.Event()
        return self._wanted

    def subscribe(self) -> AudioClient:
        c = AudioClient()
        self.clients.add(c)
        self._event().set()
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run())
        return c

    def unsubscribe(self, c: AudioClient) -> None:
        self.clients.discard(c)
        if not self.clients:
            self._event().clear()
            if self._writer is not None:
                self._writer.close()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "listeners": len(self.clients),
            "frames": self.frames,
            "last_frame_at": self.last_frame_at,
            "error": self.error,
        }

    def dispatch(self, data: bytes) -> None:
        self.frames += 1
        self.last_frame_at = time.time()
        for c in list(self.clients):
            c.offer(data)

    async def run(self) -> None:
        wanted = self._event()
        while True:
            await wanted.wait()
            try:
                reader, writer = await asyncio.open_unix_connection(str(self.path))
            except (OSError, ValueError) as exc:
                self.connected = False
                self.error = f"live service audio not reachable: {exc}"
                await asyncio.sleep(self.retry_s)
                continue
            self._writer = writer
            self.connected, self.error = True, None
            try:
                while self.clients:
                    head = await reader.readexactly(4)
                    (n,) = struct.unpack("<I", head)
                    if n > MAX_FRAME or n < listen.FRAME.size:
                        raise ValueError(f"an audio frame of {n} bytes")
                    self.dispatch(await reader.readexactly(n))
            except asyncio.IncompleteReadError:
                pass  # closed: the live service restarted, or the last browser left
            except (OSError, ValueError) as exc:
                self.error = f"audio read failed: {exc}"
            finally:
                self.connected = False
                self._writer = None
                writer.close()
            if self.clients:
                await asyncio.sleep(self.retry_s)
