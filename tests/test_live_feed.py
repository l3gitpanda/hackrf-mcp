"""The live service's feed socket (web UI milestone 4). These tests need
AF_UNIX, like the control-socket tests."""

import base64
import hashlib
import json
import shutil
import socket
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from starlette.testclient import TestClient

from hackrf_mcp import live_feed, settings
from hackrf_mcp.live_feed import FeedServer, read_feed
from hackrf_mcp.live_log import Position, Row
from hackrf_mcp.live_service import LiveService
from hackrf_mcp.live_spectrum import BINS, SpectrumRecorder, SpectrumRow
from hackrf_mcp.protocols.multimon import CONTENT_NOT_LOGGED
from hackrf_mcp.web import app as web_app


@pytest.fixture
def sock_dir():
    # AF_UNIX paths are limited to about 100 bytes, which pytest's tmp_path
    # can exceed; use a short directory and remove it afterwards.
    d = Path(tempfile.mkdtemp(prefix="feed", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _take(gen, n):
    return [next(gen) for _ in range(n)]


def test_published_messages_reach_every_client(sock_dir):
    d = sock_dir
    feed = FeedServer(d / "feed.sock").start()
    try:
        feed.publish({"type": "event", "event": "nobody listening"})  # dropped: no clients yet
        a, b = read_feed(d / "feed.sock"), read_feed(d / "feed.sock")
        deadline = time.monotonic() + 5
        while feed.status()["clients"] < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        row = Row(ts=1.0, profile="adsb", kind="aircraft", fields={"icao": "A1"}, subject="A1")
        feed.publish(live_feed.row_message(row))
        feed.publish(live_feed.position_message(Position(2.0, "adsb", "A1", 33.9, -118.4)))
        feed.publish(live_feed.event_message(3.0, "start", "adsb", "tuned"))
        for gen in (a, b):
            got = _take(gen, 3)
            assert [m["type"] for m in got] == ["row", "position", "event"]
            assert got[0]["subject"] == "A1" and got[1]["lat"] == 33.9 and got[2]["detail"] == "tuned"
        assert feed.status()["clients"] == 2
    finally:
        feed.close()
    assert not (d / "feed.sock").exists()


def test_a_client_that_stops_reading_loses_messages_not_the_service(sock_dir):
    d = sock_dir
    feed = FeedServer(d / "feed.sock", maxsize=4).start()
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(d / "feed.sock"))
        deadline = time.monotonic() + 5
        while feed.status()["clients"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        big = "x" * 200_000
        t0 = time.monotonic()
        for i in range(40):  # far more than the socket buffer and the queue hold
            feed.publish({"type": "event", "n": i, "pad": big})
        assert time.monotonic() - t0 < 2  # publish never waited for the client
        s.settimeout(5)
        buf, delivered, dropped = b"", 0, 0
        deadline = time.monotonic() + 10
        while delivered + dropped < 40 and time.monotonic() < deadline:
            buf += s.recv(1 << 20)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                m = json.loads(line)
                if m["type"] == "dropped":
                    dropped += m["n"]
                else:
                    delivered += 1
        s.close()
        # Every message was either delivered or reported as dropped.
        assert dropped > 0 and delivered + dropped == 40
    finally:
        feed.close()


def test_fft_messages_carry_the_quantised_spectrum_and_its_segment():
    seg = {"profile": "adsb", "center_hz": 1_090_000_000, "sample_rate_hz": 2_400_000, "bins": BINS, "db_min": -130.0, "db_step": 0.5}
    mean = np.full(BINS, -80.0, dtype=np.float32)
    msg = live_feed.fft_message(seg, SpectrumRow(5.0, 0.25, 1170, mean, mean + 10))
    assert msg["type"] == "fft" and msg["center_hz"] == 1_090_000_000 and msg["bins"] == BINS
    raw = np.frombuffer(base64.b64decode(msg["mean"]), np.uint8)
    assert raw.size == BINS and -130 + 0.5 * raw[0] == -80.0


class RowPipeline:
    def __init__(self, tuning, ctx):
        self.tuning, self.ctx, self.source = tuning, ctx, None

    def start(self):
        self.ctx.emit(Row(ts=1.0, profile=self.tuning.profile.name, kind="aircraft", fields={}, subject="A1"))
        self.ctx.emit_position(Position(1.0, "adsb", "A1", 33.9, -118.4))

    def stop(self):
        pass

    def failure(self):
        return None

    def health(self):
        return {}

    def stderr_tail(self):
        return []


def test_the_service_publishes_rows_events_positions_and_fft(sock_dir):
    d = sock_dir
    live = settings.LiveSettings(live_dir=d)
    rec = SpectrumRecorder(lambda item: None)
    svc = LiveService(live, None, make_pipeline=RowPipeline, program_finder=lambda p: "/bin/true", spectrum=rec)
    feed = FeedServer(live.feed_socket_path).start()
    svc.attach_feed(feed)
    try:
        gen = read_feed(live.feed_socket_path)
        deadline = time.monotonic() + 5
        while feed.status()["clients"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        svc.handle({"cmd": "start", "profile": "adsb"})
        tuning = SimpleNamespace(profile=SimpleNamespace(name="adsb"), center_hz=1_090_000_000, sample_rate_hz=2_400_000,
                                 lna_gain_db=32, vga_gain_db=40, amp=False)
        tap = rec.tap_for(tuning)
        tap(np.zeros(2 * 2_400_000, dtype=np.int8).tobytes())  # a second of samples: four live rows
        got = []
        while not any(m["type"] == "fft" for m in got):
            got.append(next(gen))
        types = {m["type"] for m in got}
        assert {"event", "row", "position", "fft"} <= types
        assert svc.status()["feed"]["clients"] == 1
    finally:
        feed.close()


def test_feed_to_browser_end_to_end_with_pager_text_removed(sock_dir):
    d = sock_dir
    live = settings.LiveSettings(live_dir=d)
    feed = FeedServer(live.feed_socket_path).start()
    token = "correct-horse-battery-staple-42"
    gate = web_app.create_app(live, settings.WebSettings(), [hashlib.sha256(token.encode()).digest()], b"k" * 32)
    try:
        with TestClient(gate) as c:
            with c.websocket_connect("/ws/live", headers={"Authorization": f"Bearer {token}"}) as ws:
                assert json.loads(ws.receive_text())["type"] == "hello"
                deadline = time.monotonic() + 10
                while not gate.hub.connected and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert gate.hub.connected
                page = Row(ts=1.0, profile="flex", kind="page", subject="1", fields={"message": "SECRET"}, raw="FLEX|h|SECRET")
                feed.publish(live_feed.row_message(page))
                while True:
                    m = json.loads(ws.receive_text())
                    if m["type"] == "row":
                        break
                assert m["raw"] == "FLEX|h|" + CONTENT_NOT_LOGGED and "message" not in m["fields"]
    finally:
        feed.close()
