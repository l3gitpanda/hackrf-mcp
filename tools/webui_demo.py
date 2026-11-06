"""Run the web UI against a simulated live service, for developing and
screenshotting the screens without a HackRF.

    uv run python tools/webui_demo.py [--port 18766]

Everything behind the UI is the real code (LiveService, its control and
feed sockets, the log, the spectrum history, hackrf-web) except two parts:
the sample source replays generated noise with bursts on each profile's
channels, and the pipelines emit made-up rows (subjects start with DEMO) in
place of decoders. Every profile counts as installed. Its data lives in --dir (default
/tmp/hackrf-webui-demo), emptied at each start. Prints a sign-in token.

Occupancy sweeps run every --occupancy-min minutes through
tools/fake_hackrf_sweep.py (through the agent with --agent), over
--occupancy-days of made-up earlier slots.
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
from dataclasses import replace
from pathlib import Path

import numpy as np

from hackrf_mcp import cli, decoders, listen, live_feed, live_profiles, occupancy, settings
from hackrf_mcp.live_log import LiveLogWriter, Position, Row
from hackrf_mcp.live_profiles import FileSource, Pipeline
from hackrf_mcp.live_service import ControlServer, LiveService, LogPump, start_captures, start_occupancy
from hackrf_mcp.live_spectrum import SpectrumRecorder, SpectrumWriter

HOME = (33.86, -118.28)


def demo_aircraft(i: int) -> tuple[str, str]:
    """A made-up ICAO address and callsign for demo aircraft i, each type of
    aircraft in turn (military, law enforcement, government, commercial
    twice, private, unknown), to match demo_aircraft_db."""
    kind = i % 7
    addr = 0xAE0000 + i if kind == 0 else 0xA0D000 + i
    callsign = (f"RCH{100 + i}", f"N{100 + i}SD", f"N{100 + i}SU", f"DMO{100 + i}", f"DMO{200 + i}", f"N{100 + i}PV", "")[kind]
    return f"{addr:06X}", callsign


def subject_and_label(p, s: str) -> tuple[str, str | None]:
    """A profile whose subjects are aircraft addresses gets demo aircraft;
    the others keep DEMOnn."""
    if p.subject_type == "icao24":
        icao, callsign = demo_aircraft(int(s[-2:]))
        return icao, callsign or None
    return s, f"DEMO {s[-2:]}"


def demo_aircraft_db(path: Path) -> None:
    """An aircraft database for the demo aircraft alone, all made up, so the
    Map's types show without downloading the real ones."""
    import sqlite3

    from hackrf_mcp.web import aircraftdb

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(aircraftdb.SCHEMA)
    for i in range(40):
        icao, _ = demo_aircraft(i)
        kind = i % 7
        if kind == 0:
            conn.execute(
                "INSERT INTO aircraft (icao, pa_class, pa_operator, pa_type, pa_category) VALUES (?, 'Mil', ?, ?, ?)",
                (icao, "Example Air Force", "C-17A Globemaster III", "Example Air Force"),
            )
            continue
        faa = {
            1: (f"N{100 + i}SD", "AIRBUS HELICOPTERS", "AS350B3", "6", "5", "EXAMPLE COUNTY SHERIFFS OFFICE"),
            2: (f"N{100 + i}SU", "CESSNA", "172S", "4", "5", "EXAMPLE STATE UNIVERSITY"),
            3: (f"N{300 + i}DM", "BOEING", "737-8", "5", "3", "DEMO AIRLINES INC"),
            4: (f"N{300 + i}DM", "AIRBUS", "A321-271NX", "5", "3", "DEMO AIRLINES INC"),
            5: (f"N{100 + i}PV", "PIPER", "PA-28-181", "4", "1", "A. N. EXAMPLE"),
        }.get(kind)
        if faa:
            conn.execute(
                "INSERT INTO aircraft (icao, faa, registration, manufacturer, model, type_aircraft, registrant_type, owner) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                (icao, *faa),
            )
    conn.execute("INSERT INTO meta VALUES ('built_at', ?)", (str(time.time()),))
    conn.commit()
    conn.close()


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
    if tuning.profile.name == "listen":
        # Something to hear where a listen of one channel puts it (25 kHz
        # below the centre): an AM call, on air 2 s of the 4 s loop, and a
        # steady NFM tone 300 kHz above the centre, as the fake hackrf_transfer has.
        import fake_hackrf_transfer as fake  # beside this file

        tt = np.arange(n, dtype=np.float64) / rate
        x += (0.03 * np.exp(1j * (2 * np.pi * fake.NFM_OFFSET_HZ * tt + 2.0 * np.sin(2 * np.pi * fake.NFM_TONE_HZ * tt)))).astype(np.complex64)
        am = 0.05 * (1 + 0.8 * np.sin(2 * np.pi * fake.AM_TONE_HZ * tt)) * np.exp(2j * np.pi * fake.AM_OFFSET_HZ * tt)
        x += (am * ((tt % 4.0 >= 1.0) & (tt % 4.0 < 3.0))).astype(np.complex64)
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
        # Reported positions (ADS-C, CPDLC) come from demo_datalink instead.
        has_map = any(v.name == "map" for v in p.ui_views()) and not p.positions_reported
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
            subject, label = subject_and_label(p, s)
            if has_map:
                pos[s][0] += rnd.uniform(-0.01, 0.01)
                pos[s][1] += rnd.uniform(-0.01, 0.01)
                fields.update(lat=round(pos[s][0], 5), lon=round(pos[s][1], 5))
                self.ctx.emit_position(Position(time.time(), p.name, subject, pos[s][0], pos[s][1], 30000.0, 250.0, 90.0))
            self.ctx.emit(
                Row(
                    ts=time.time(),
                    profile=p.name,
                    kind=kind,
                    freq_hz=freq,
                    level_db=round(rnd.uniform(-40, -10), 1),
                    subject=subject,
                    label=label,
                    fields=fields,
                    raw=f"demo {p.name} {s} (simulated, not decoded)",
                )
            )
            self.ctx.count(1)


def backfill(db_path: Path, hours: float, now: float) -> None:
    """Fills the log with a made-up past, for the screens that read ranges
    (Log, Map history, Summaries): a rotation of the first three profiles
    with positions (or not) in 10, 10 and 5 minute slots, busier by day,
    with the service down for 40 minutes and a pause on the way. Rows are
    per-minute summaries with a message count, as ADS-B and AIS log them;
    pages carry no text."""
    w = LiveLogWriter(db_path, (hours + 2) * 3600, 1 << 30, 0)
    rnd = random.Random(7)
    names = list(live_profiles.PROFILES)[:3]
    lengths = (600, 600, 300)
    t = now - hours * 3600
    down = (now - hours * 3600 * 0.4, now - hours * 3600 * 0.4 + 2400)
    # Straight tracks across the area, each subject at its own speed and
    # height, some far enough out for the receiver range to have an edge.
    tracks = {
        f"DEMO{i:02d}": (
            HOME[0] + rnd.uniform(-2.2, 2.2) * (0.3 if i % 3 else 1),
            HOME[1] + rnd.uniform(-2.8, 2.8) * (0.3 if i % 3 else 1),
            rnd.uniform(-1, 1),
            rnd.uniform(-1, 1),
            float(rnd.choice([3000, 6500, 12000, 18000, 24000, 31000, 36000, 39000])),
        )
        for i in range(40)
    }
    w.event("service-start", ts=t)
    i = 0
    while t < now - 60:
        name, length = names[i % 3], lengths[i % 3]
        i += 1
        if down[0] <= t < down[1]:
            w.event("service-stop", ts=t)
            t = down[1]
            w.event("service-start", ts=t)
            continue
        p = live_profiles.PROFILES[name]
        kind = p.summary_kinds[0] if p.summary_kinds else name
        has_map = any(v.name == "map" for v in p.ui_views()) and not p.positions_reported
        end = min(t + length, now - 60)
        w.event("start", name, f"{name}: demo backfill", ts=t)
        rows, fixes = [], []
        hour = time.gmtime(t).tm_hour
        busy = 4 + int(14 * max(0.0, np.sin((hour - 6) / 24 * 2 * np.pi)))
        heard = [s for s in tracks if rnd.random() < busy / len(tracks) * 2][:busy]
        for m in np.arange(t, end - 30, 60.0):
            for s in heard:
                if rnd.random() < 0.25:
                    continue
                lat0, lon0, dlat, dlon, alt = tracks[s]
                subject, label = subject_and_label(p, s)
                k = ((m / 3600) % 6) / 6
                lat, lon = lat0 + dlat * 0.4 * k, lon0 + dlon * 0.4 * k
                fields = {"messages": rnd.randint(5, 400) if has_map else 1, "type": "DEMO"}
                if has_map:
                    fields.update(lat=round(lat, 5), lon=round(lon, 5), altitude=int(alt), speed=rnd.choice([12, 250, 430]))
                    for j in range(0, 60, 20):
                        fixes.append(Position(float(m + j), name, subject, lat + dlat * 0.0007 * j, lon + dlon * 0.0007 * j, alt, 250.0, 90.0))
                # Fainter with distance, as a real receiver hears them.
                km = 111.2 * float(np.hypot(lat - HOME[0], (lon - HOME[1]) * np.cos(np.radians(HOME[0]))))
                level = round(-6 - 0.09 * km + rnd.gauss(0, 3), 1) if has_map else round(rnd.uniform(-40, -10), 1)
                rows.append(
                    Row(
                        ts=float(m), ts_end=float(m + 50), profile=name, kind=kind, freq_hz=p.default_freqs_hz[0] if p.default_freqs_hz else None,
                        level_db=level, subject=subject, label=label, fields=fields,
                        raw=f"demo {name} {s} (simulated, not decoded)",
                    )
                )  # fmt: skip
        w.write(rows, now=now)
        w.write_positions(fixes, now=now)
        if i == 7:
            w.event("pause", name, "for hackrf_info, lease 45 s", ts=t + 200)
            w.event("stop", name, "paused for hackrf_info", ts=t + 200)
            w.event("start", name, f"{name}: demo backfill", ts=t + 230)
        w.event("stop", name, "switching", ts=end)
        t = end
    w.close()


def demo_datalink(svc, last_fix: dict, stop: threading.Event) -> None:
    """Positions demo aircraft report themselves, as ADS-C and CPDLC over
    VDL2 and ACARS would: whenever ADS-B has gone quiet (the rotation is on
    another profile), demo aircraft 3 reports over VDL2 by its address,
    aircraft 4 over ACARS by its registration (which the demo aircraft.db
    maps to its address), and a tail the database doesn't know over ACARS,
    each a little on from its last fix, 15 s after it."""
    done = {}
    while not stop.wait(5):
        now = time.time()
        for i, profile, how in ((3, "vdl2", "ADS-C"), (4, "acars", "CPDLC"), (5, "acars", "CPDLC")):
            icao, callsign = demo_aircraft(i)
            fix = last_fix.get(("adsb", icao))
            if fix is None or now - fix.ts < 20 or done.get(i) == fix.ts:
                continue
            done[i] = fix.ts
            # After its last fix, as on the station (one band at a time).
            at = fix.ts + 15
            lat, lon, alt = fix.lat + 0.03, fix.lon + 0.04, 35000.0
            if i == 3:
                subject, label = icao, callsign
                fields = {"src": icao, "src_type": "Aircraft", "dst": "10916A", "dst_type": "Ground station", "frame_type": "I"}
            elif i == 4:
                subject, label = f"N{300 + i}DM", callsign
                fields = {"flight": callsign, "label": "BA", "mode": "2"}
            else:
                subject, label = "N999ZZ", "DMO999"  # no such aircraft in the database
                lat, lon = fix.lat - 0.15, fix.lon - 0.1
                fields = {"flight": label, "label": "BA", "mode": "2"}
            fields.update(lat=round(lat, 5), lon=round(lon, 5), alt=int(alt), pos_source=how, pos_ts=round(at, 3))
            svc._emit_position(Position(at, profile, subject, lat, lon, alt))
            svc._emit(Row(ts=now, profile=profile, kind=profile, freq_hz=None, level_db=-20.0, subject=subject, label=label,
                          fields=fields, raw=f"demo {profile} {how} report (simulated, not decoded)"))  # fmt: skip


def start_catcher() -> int:
    """A local stand-in for an alert webhook: prints each POST it gets."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Catcher(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            print(f"webhook got: {body.decode(errors='replace')}", file=sys.stderr, flush=True)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Catcher)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def demo_decode_runner(live, workroot: Path):
    """The real decoding process for the built-in decoders and the picture;
    a made-up result for the rest, whose programs aren't installed here."""
    from hackrf_mcp import captures

    real = captures.decode_runner(live, workroot)

    def run(path, p, todo, picture):
        built_in = [(n, f) for n, f in todo if not decoders.DECODERS[n].programs]
        out = real(path, p, built_in, picture)
        made = {d["decoder"]: d for d in out["decodes"]}
        for n, f in todo:
            made.setdefault(n, {
                "decoder": n, "state": "done", "headline": f"{n}: 3 messages (made up by the demo).",
                "notes": ["The demo has no decoder programs: these lines are made up."],
                "table": [], "messages": [f"demo message {i} on {(f or [p.center_hz])[0] / 1e6:.4f} MHz" for i in (1, 2, 3)],
                "seconds": 0.1,
            })  # fmt: skip
        return {**out, "decodes": [made[n] for n, _ in todo]}

    return run


def demo_occupancy(path: Path, days: float, now: float) -> int:
    """Made-up occupancy slots every 15 minutes for `days` before now, from
    the air tools/fake_hackrf_sweep.py makes (so the demo's own sweeps look
    the same): its floor, steady carriers, bursts that come and go from pass
    to pass, the carrier on from 13:00 to 22:00 UTC, and one more that is
    new in the last six hours (for "new or unusual")."""
    import fake_hackrf_sweep as fake  # beside this file

    plan = occupancy.Plan()
    f = plan.lo_hz + (np.arange(plan.bins) + 0.5) * plan.bin_hz
    gain = occupancy.window_gain_db(plan.fft)

    def lin(carriers) -> np.ndarray:
        out = np.zeros(plan.bins)
        for c, w, lv in carriers:
            half = max(w * 500.0, plan.bin_hz / 2)
            out[np.abs(f - c * 1e6) <= half] += 10 ** ((lv + gain) / 10)
        return out

    floor = fake.FLOOR_DB + 6 * (f / 6e9) + gain
    steady = lin(fake.STEADY)
    daytime = lin(fake.DAYTIME)
    bursts = [(lin([(c, w, lv)]), chance) for c, w, lv, chance in fake.BURSTY]
    new = lin([(155.31, 25, -52)])
    rng = np.random.default_rng(7)
    w = occupancy.OccupancyWriter(path, 365 * 86400, 1826 * 86400, 1 << 31, 0)
    n = 0
    for t in np.arange(occupancy.next_slot(now - days * 86400, 900), now, 900.0):
        base = 10 ** ((floor + rng.normal(0, 1.0, plan.bins)) / 10) + steady
        if 13 <= occupancy.utc_hour(t) < 22:
            base = base + daytime
        if now - t < 6 * 3600:
            base = base + new
        mean, peak = base.copy(), base * 10 ** (np.abs(rng.normal(0, 2.5, plan.bins)) / 10)
        for carrier, chance in bursts:
            on = rng.binomial(20, chance)  # passes it was on in
            mean += carrier * on / 20
            peak += carrier if on else 0
        w.add(float(t), float(t) + 2, 4.2, plan, occupancy.Measured(20, 0, 0, (10 * np.log10(mean)).astype(np.float32), (10 * np.log10(peak)).astype(np.float32)))
        n += 1
    w.prune(now)
    w.close()
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--token-file", help="write the sign-in token here instead of printing it")
    parser.add_argument("--dir", default="/tmp/hackrf-webui-demo", help="scratch directory, emptied at start")
    parser.add_argument("--basemap", help="a base map extract to show (python -m hackrf_mcp.web.basemap)")
    parser.add_argument("--history", type=float, default=0, metavar="HOURS", help="fill the log with a made-up past this long")
    parser.add_argument(
        "--webhook-catcher", action="store_true",
        help="set up the alert webhook to a local receiver that prints what it gets (to stderr)",
    )
    parser.add_argument(
        "--agent", action="store_true",
        help="take samples from a hackrf-agent (run here, over loopback TCP) as a split station would",
    )
    parser.add_argument("--occupancy-days", type=float, default=8, help="days of made-up occupancy slots before now")
    parser.add_argument("--occupancy-min", type=int, default=2, help="minutes between the demo's own occupancy sweeps")
    args = parser.parse_args()

    found = lambda _: "/bin/true"  # noqa: E731
    live_profiles.find_program = found
    decoders.find_program = found
    # uvicorn ends the process with the signal that stopped it, so cleaning
    # up afterwards can't be relied on: reuse one directory instead.
    tmp = Path(args.dir)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    # hackrf_sweep for the occupancy sweeps: the stand-in, here or at the agent.
    tools = tmp / "tools"
    tools.mkdir()
    (tools / "hackrf_sweep").write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_hackrf_sweep.py").read_text())
    (tools / "hackrf_sweep").chmod(0o755)
    # hackrf_transfer for the web UI's captures, when there is no agent.
    (tools / "hackrf_transfer").write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_hackrf_transfer.py").read_text())
    (tools / "hackrf_transfer").chmod(0o755)
    # The demo's receiver sits at HOME, for range rings and the receiver range.
    live = settings.LiveSettings(live_dir=tmp / "live", min_free_mb=0, receiver=HOME)
    agent_server = None
    if args.agent:
        # The split station: an agent runs tools/fake_hackrf_transfer.py and
        # streams it over TCP; the live service reads it with RemoteSource.
        from hackrf_mcp import agent

        fake = tmp / "hackrf_transfer"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_hackrf_transfer.py").read_text())
        fake.chmod(0o755)
        token = secrets.token_urlsafe(32)
        (tmp / "agent-token").write_text(token)
        a = agent.AgentSettings(
            host="127.0.0.1", port=args.port + 1, token_file=tmp / "agent-token", binary=str(fake), tools_dir=str(tools)
        )
        a.digests = a.load_digests()
        agent_server = agent.AgentServer(a)
        threading.Thread(target=agent_server.serve_forever, daemon=True).start()
        live = settings.LiveSettings(
            live_dir=tmp / "live", min_free_mb=0, receiver=HOME, agent=("127.0.0.1", args.port + 1), agent_token_file=tmp / "agent-token"
        )
    settings.configure_live(live)
    live.live_dir.mkdir(parents=True)
    samples = tmp / "samples"
    samples.mkdir()

    def make_source(tuning):
        path = samples / f"{tuning.profile.name}-{tuning.center_hz}.cs8"
        if not path.exists():
            # A second of samples is plenty at a watch's 10 MS/s.
            synth(path, tuning, 4.0 if tuning.sample_rate_hz <= 2_400_000 else 1.0)
        return LoopSource(path, tuning.sample_rate_hz, realtime=True)

    keep_s = max(86400, (args.history + 2) * 3600)
    if args.history > 0:
        live_profiles.load()
        backfill(live.db_path, args.history, time.time())
    sw = SpectrumWriter(live.spectrum_db_path, 86400, 86400, 1 << 30, 0)
    pump = LogPump(LiveLogWriter(live.db_path, keep_s, 1 << 30, 0), spectrum_writer=sw)
    pump.start()
    rec = SpectrumRecorder(pump.spectrum)
    def make_pipeline(tuning, ctx):
        # A watch decodes nothing, so it runs its own pipeline on the simulated source.
        return tuning.profile.pipeline(tuning, ctx) if tuning.profile.spectrum_only else DemoPipeline(tuning, ctx)

    svc = LiveService(
        live, pump, make_pipeline=make_pipeline, make_source=None if args.agent else make_source,
        program_finder=found, spectrum=rec,
    )  # fmt: skip
    svc.attach_feed(live_feed.FeedServer(live.feed_socket_path).start())
    # Listening: the audio socket, and clips into tmp/live/clips.
    svc.audio_out = listen.AudioServer(live.audio_socket_path).start()
    svc.clips = listen.ClipStore(live.clips_dir, live.clip_keep_days, live.clip_keep_mb, 0)
    # Remember each aircraft's last fix, for the reports demo_datalink makes.
    last_fix: dict = {}
    emit_position = svc._emit_position

    def remember(p):
        last_fix[(p.profile, p.subject)] = p
        emit_position(p)

    svc._emit_position = remember
    threading.Thread(target=demo_datalink, args=(svc, last_fix, svc.shutdown), daemon=True).start()
    control = ControlServer(live.socket_path, svc)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    threading.Thread(target=svc.serve_forever, daemon=True).start()
    svc.handle({"cmd": "start", "schedule": [{"profile": "adsb", "minutes": 2}, {"profile": "ais", "minutes": 1}]})
    if args.occupancy_days > 0:
        n = demo_occupancy(live.occupancy_db_path, args.occupancy_days, time.time())
        print(f"occupancy: {n} made-up slots", file=sys.stderr, flush=True)
    if not args.agent:
        cli.set_bin_dir(tools)
    start_occupancy(
        svc,
        replace(live, occupancy=True, occupancy_interval_min=args.occupancy_min, occupancy_late_min=1, occupancy_passes=5,
                occupancy_alert_quiet_min=0),
    )
    # The web UI's captures, into tmp/captures/web, with made-up results
    # from every decoder that needs a program (none is installed here).
    settings.configure(settings.Settings(data_dir=tmp / "captures", min_free_mb=0))
    start_captures(
        svc, live, run_decode=demo_decode_runner(live, tmp / "captures" / ".decode-tmp"),
        available=lambda: {n: "demo" for n in decoders.names()},
    )  # fmt: skip

    if args.basemap:
        state = settings.WebSettings().state_path(live)
        state.mkdir(parents=True, exist_ok=True)
        (state / "basemap.pmtiles").symlink_to(Path(args.basemap).resolve())

    token = secrets.token_urlsafe(24)
    if args.token_file:
        Path(args.token_file).write_text(token)
    else:
        print(f"sign-in token: {token}", flush=True)
    import uvicorn

    from hackrf_mcp.web.app import create_app

    web = settings.WebSettings()
    demo_aircraft_db(web.state_path(live) / "aircraft.db")
    if args.webhook_catcher:
        hook = tmp / "webhook-url"
        hook.write_text(f"http://127.0.0.1:{start_catcher()}/demo-hook")
        web = settings.WebSettings(alert_webhook_file=hook, public_url=f"http://{args.host}:{args.port}")
    app = create_app(live, web, [hashlib.sha256(token.encode()).digest()], secrets.token_bytes(32))
    print(f"demo UI on http://{args.host}:{args.port}/ (data in {tmp})", file=sys.stderr, flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    svc.shutdown.set()


if __name__ == "__main__":
    main()
