"""Run the web UI against a simulated live service, for developing and
screenshotting the screens without a HackRF.

    uv run python tools/webui_demo.py [--port 18766]

Everything behind the UI is the real code (LiveService, its control and
feed sockets, the log, the spectrum history, hackrf-web) except two parts:
the sample source replays generated noise with bursts on each profile's
channels, and the pipelines emit made-up rows (subjects start with DEMO) in
place of decoders. Every profile counts as installed. Its data lives in --dir (default
/tmp/hackrf-webui-demo), emptied at each start. Prints a sign-in token.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

import numpy as np

from hackrf_mcp import decoders, live_feed, live_profiles, settings
from hackrf_mcp.live_log import LiveLogWriter, Position, Row
from hackrf_mcp.live_profiles import FileSource, Pipeline
from hackrf_mcp.live_service import ControlServer, LiveService, LogPump
from hackrf_mcp.live_spectrum import SpectrumRecorder, SpectrumWriter

HOME = (33.86, -118.28)


class LoopSource(FileSource):
    """A capture played in a loop, in real time."""

    def _pump(self, consumer):
        try:
            with open(self.path, "rb") as f:
                while not self._stop.is_set():
                    buf = f.read(1 << 18)
                    if not buf:
                        f.seek(0)
                        continue
                    self._count(len(buf))
                    self._deliver(consumer, buf)
                    due = self.started + self.bytes_read / (2 * self.sample_rate_hz)
                    delay = due - time.monotonic()
                    if delay > 0:
                        self._stop.wait(delay)
        finally:
            self.finished.set()


def synth(path: Path, tuning, seconds: float = 4.0) -> None:
    """Noise, a couple of steady carriers, and bursts on the tuned channels."""
    rate = tuning.sample_rate_hz
    n = int(rate * seconds)
    rng = np.random.default_rng(abs(hash((tuning.profile.name, tuning.center_hz))) % 2**32)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.02
    t = np.arange(n, dtype=np.float32) / rate
    for off in (-0.31 * rate, 0.27 * rate):
        x += (0.05 * np.exp(2j * np.pi * off * t)).astype(np.complex64)
    for f in tuning.freqs_hz or (tuning.center_hz,):
        off = f - tuning.center_hz
        for _ in range(int(seconds * 3)):
            start = rng.integers(0, n - rate // 20)
            length = int(rng.integers(rate // 400, rate // 25))
            seg = slice(start, start + length)
            if off == 0:  # a wideband pulse train, like ADS-B on its centre
                x[seg] += (0.3 * (rng.standard_normal(length) + 1j * rng.standard_normal(length))).astype(np.complex64)
            else:
                x[seg] += (0.4 * np.exp(2j * np.pi * off * t[seg])).astype(np.complex64)
    out = np.empty(2 * n, dtype=np.int8)
    out[0::2] = np.clip(np.rint(x.real * 127), -127, 127)
    out[1::2] = np.clip(np.rint(x.imag * 127), -127, 127)
    out.tofile(path)


class DemoPipeline(Pipeline):
    """Reads the simulated source and makes up rows like the profile's."""

    def start(self) -> None:
        self.source = self.ctx.make_source(self.tuning)
        self.source.start(lambda buf: None)
        self.started = time.time()
        self._thread(self._rows, "demo-rows")

    def _rows(self) -> None:
        p = self.tuning.profile
        kind = p.summary_kinds[0] if p.summary_kinds else p.name
        has_map = any(v.name == "map" for v in p.ui_views())
        subjects = [f"DEMO{i:02d}" for i in range(8)]
        pos = {s: [HOME[0] + random.uniform(-0.3, 0.3), HOME[1] + random.uniform(-0.3, 0.3)] for s in subjects}
        rnd = random.Random(p.name)
        while not self._stop.wait(rnd.uniform(0.4, 2.5)):
            s = rnd.choice(subjects)
            freq = rnd.choice(self.tuning.freqs_hz or (self.tuning.center_hz,))
            # Numbers where the real decoders put numbers, words elsewhere.
            numeric = {"altitude", "altitude_min", "altitude_max", "speed", "track", "course", "heading", "messages", "positions", "duration"}
            fields = {
                k: (rnd.choice([7, 3500, 12000, 36000]) if k in numeric else rnd.choice(["DEMO", "UNDER WAY", "TEST"]))
                for k in p.summary_fields
                if k not in ("lat", "lon")
            }
            if has_map:
                pos[s][0] += rnd.uniform(-0.01, 0.01)
                pos[s][1] += rnd.uniform(-0.01, 0.01)
                fields.update(lat=round(pos[s][0], 5), lon=round(pos[s][1], 5))
                self.ctx.emit_position(Position(time.time(), p.name, s, pos[s][0], pos[s][1], 30000.0, 250.0, 90.0))
            self.ctx.emit(
                Row(
                    ts=time.time(),
                    profile=p.name,
                    kind=kind,
                    freq_hz=freq,
                    level_db=round(rnd.uniform(-40, -10), 1),
                    subject=s,
                    label=f"DEMO {s[-2:]}",
                    fields=fields,
                    raw=f"demo {p.name} {s} (simulated, not decoded)",
                )
            )
            self.ctx.count(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--token-file", help="write the sign-in token here instead of printing it")
    parser.add_argument("--dir", default="/tmp/hackrf-webui-demo", help="scratch directory, emptied at start")
    args = parser.parse_args()

    found = lambda _: "/bin/true"  # noqa: E731
    live_profiles.find_program = found
    decoders.find_program = found
    # uvicorn ends the process with the signal that stopped it, so cleaning
    # up afterwards can't be relied on: reuse one directory instead.
    tmp = Path(args.dir)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    live = settings.LiveSettings(live_dir=tmp / "live", min_free_mb=0)
    settings.configure_live(live)
    live.live_dir.mkdir(parents=True)
    samples = tmp / "samples"
    samples.mkdir()

    def make_source(tuning):
        path = samples / f"{tuning.profile.name}-{tuning.center_hz}.cs8"
        if not path.exists():
            synth(path, tuning)
        return LoopSource(path, tuning.sample_rate_hz, realtime=True)

    sw = SpectrumWriter(live.spectrum_db_path, 86400, 86400, 1 << 30, 0)
    pump = LogPump(LiveLogWriter(live.db_path, 86400, 1 << 30, 0), spectrum_writer=sw)
    pump.start()
    rec = SpectrumRecorder(pump.spectrum)
    svc = LiveService(live, pump, make_pipeline=DemoPipeline, make_source=make_source, program_finder=found, spectrum=rec)
    svc.attach_feed(live_feed.FeedServer(live.feed_socket_path).start())
    control = ControlServer(live.socket_path, svc)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    threading.Thread(target=svc.serve_forever, daemon=True).start()
    svc.handle({"cmd": "start", "schedule": [{"profile": "adsb", "minutes": 2}, {"profile": "ais", "minutes": 1}]})

    token = secrets.token_urlsafe(24)
    if args.token_file:
        Path(args.token_file).write_text(token)
    else:
        print(f"sign-in token: {token}", flush=True)
    import uvicorn

    from hackrf_mcp.web.app import create_app

    app = create_app(live, settings.WebSettings(), [hashlib.sha256(token.encode()).digest()], secrets.token_bytes(32))
    print(f"demo UI on http://{args.host}:{args.port}/ (data in {tmp})", file=sys.stderr, flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    svc.shutdown.set()


if __name__ == "__main__":
    main()
