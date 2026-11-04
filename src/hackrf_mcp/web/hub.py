"""Relays the live service's feed socket (live_feed.py) to websockets.

One connection to the feed serves every browser. FFT lines are passed on
as they arrive, unparsed; rows go through the redaction the app gives (pager
text, unless the operator allows it) before any browser sees them. Each
browser has a bounded queue; one that falls behind loses messages and is
told how many, so a slow phone can't hold up the rest. The hub reconnects
when the live service restarts.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

# Messages a browser may fall behind by: about a minute of FFT rows (ten a
# second) and the decoded rows among them.
CLIENT_QUEUE = 640
# Longest line accepted from the feed (an FFT row is about 1.5 KB; a row's
# raw text is capped at 4000 characters).
MAX_LINE = 1 << 20


class Client:
    def __init__(self, fft: bool = True):
        self.q: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.fft = fft
        self.dropped = 0

    def offer(self, text: str) -> None:
        try:
            self.q.put_nowait(text)
        except asyncio.QueueFull:
            self.dropped += 1


class FeedHub:
    def __init__(self, path: Path, redact_row: Callable[[dict], dict], retry_s: float = 2.0):
        self.path = Path(path)
        self.redact_row = redact_row
        self.retry_s = retry_s
        self.clients: set[Client] = set()
        self.connected = False
        self.last_message_at: float | None = None
        self.messages = 0
        self.error: str | None = None
        self._task: asyncio.Task | None = None

    def subscribe(self, fft: bool = True) -> Client:
        c = Client(fft)
        self.clients.add(c)
        return c

    def unsubscribe(self, c: Client) -> None:
        self.clients.discard(c)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

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
            "clients": len(self.clients),
            "messages": self.messages,
            "last_message_at": self.last_message_at,
            "error": self.error,
        }

    def dispatch(self, line: str) -> None:
        """Pass one feed line on to every browser."""
        is_fft = line.startswith('{"type":"fft"')
        if line.startswith('{"type":"row"'):
            try:
                line = json.dumps(self.redact_row(json.loads(line)), separators=(",", ":"))
            except ValueError:
                return
        self.messages += 1
        self.last_message_at = time.time()
        for c in list(self.clients):
            if is_fft and not c.fft:
                continue
            c.offer(line)

    async def run(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(str(self.path), limit=MAX_LINE)
            except (OSError, ValueError) as exc:
                self.connected = False
                self.error = f"live service feed not reachable: {exc}"
                await asyncio.sleep(self.retry_s)
                continue
            self.connected, self.error = True, None
            try:
                while True:
                    raw = await reader.readline()
                    if not raw:
                        break
                    self.dispatch(raw.decode(errors="replace").rstrip("\n"))
            except (OSError, ValueError, asyncio.LimitOverrunError) as exc:
                self.error = f"feed read failed: {exc}"
            finally:
                self.connected = False
                writer.close()
            await asyncio.sleep(self.retry_s)
