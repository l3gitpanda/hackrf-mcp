"""Positions for map tracks (web UI milestone 3): the throttle, the ADS-B
and AIS aggregators, the log's positions table and the service's path."""

from types import SimpleNamespace

from hackrf_mcp import live_log, settings
from hackrf_mcp.live_log import LiveLogWriter, Position, Row
from hackrf_mcp.live_profiles import PositionThrottle
from hackrf_mcp.live_service import LiveService, LogPump
from hackrf_mcp.protocols import adsb, ais

MIB = 1024 * 1024


def _writer(tmp_path, **kw):
    args = {"max_age_s": 7 * 86400, "max_bytes": 256 * MIB, "min_free_bytes": 0}
    args.update(kw)
    return LiveLogWriter(tmp_path / "live.db", **args)


# --- The throttle ------------------------------------------------------------------


def test_one_position_per_ten_seconds_while_moving():
    kept = []
    t = PositionThrottle("adsb", kept.append)
    lat = 33.9
    for i in range(30):  # one a second, moving about 200 m a second
        t.offer("A1B2C3", 1000.0 + i, lat + i * 0.002, -118.4, alt=30000)
    assert [p.ts for p in kept] == [1000.0, 1010.0, 1020.0]
    assert kept[0] == Position(1000.0, "adsb", "A1B2C3", 33.9, -118.4, 30000, None, None)


def test_a_station_that_does_not_move_is_kept_every_five_minutes():
    kept = []
    t = PositionThrottle("ais", kept.append)
    for i in range(0, 1200, 10):  # a base station every 10 s for 20 minutes
        t.offer("3669139", 1000.0 + i, 33.37958, -118.41643)
    assert [p.ts - 1000 for p in kept] == [0, 300, 600, 900]


def test_a_climb_counts_as_moving():
    kept = []
    t = PositionThrottle("adsb", kept.append)
    t.offer("A1", 0.0, 33.9, -118.4, alt=1000)
    t.offer("A1", 15.0, 33.9, -118.4, alt=1050)  # under 100 ft: still
    t.offer("A1", 30.0, 33.9, -118.4, alt=1500)
    assert [p.alt for p in kept] == [1000, 1500]


def test_the_throttle_forgets_the_longest_unheard_subject():
    t = PositionThrottle("ais", lambda p: None, max_subjects=3)
    for i, s in enumerate(["a", "b", "c", "a", "d"]):
        t.offer(s, float(i * 100), 1.0, 1.0)
    assert list(t.last) == ["c", "a", "d"]


# --- The aggregators ----------------------------------------------------------------


def _aircraft(hexid, lat, lon, seen_pos=1.0, **kw):
    return {"hex": hexid, "lat": lat, "lon": lon, "seen_pos": seen_pos, "seen": 0.5, "messages": 10, **kw}


def test_adsb_positions_are_stamped_when_dump1090_heard_them():
    kept = []
    agg = adsb.AdsbAggregator(lambda row: None, emit_position=kept.append)
    base = 60 * 3000.0
    for k in range(5):  # aircraft.json every 5 s
        snap = {"messages": 100 * k, "aircraft": [
            _aircraft("a1b2c3", 33.9 + 0.01 * k, -118.4, seen_pos=2.0, altitude=12000 + 500 * k, speed=250, track=90.5),
            _aircraft("d4e5f6", 34.0, -118.0, seen_pos=99),  # position too old: not this poll's
        ]}
        agg.update(base + 5 * k, snap)
    assert [p.ts - base for p in kept] == [-2.0, 8.0, 18.0]
    assert {p.subject for p in kept} == {"A1B2C3"}
    assert kept[0].alt == 12000 and kept[0].speed == 250 and kept[0].track == 90.5
    plain = adsb.AdsbAggregator(lambda row: None)  # no position sink: nothing breaks
    plain.update(base, {"aircraft": [_aircraft("a1b2c3", 33.9, -118.4)]})


def _ais(mmsi, mtype, **kw):
    return {"class": "AIS", "mmsi": mmsi, "type": mtype, "channel": "A", "signalpower": -20.0, **kw}


def test_ais_positions_from_position_reports_only():
    kept, rows = [], []
    agg = ais.AisAggregator(rows.append, emit_position=kept.append)
    base = 60 * 3000.0
    agg.message(base, _ais(367000001, 5, shipname="KILLEEN"))  # static: no position
    agg.message(base + 1, _ais(367000001, 1, lat=33.5, lon=-118.0, speed=3.7, course=267.0))
    agg.message(base + 4, _ais(367000001, 1, lat=33.501, lon=-118.0, speed=3.7, course=267.0))
    agg.message(base + 12, _ais(367000001, 1, lat=33.502, lon=-118.0, speed=102.3, course=360))
    agg.message(base + 13, _ais(316033831, 18, lat=91, lon=181))  # "not available"
    assert [(p.subject, p.ts - base) for p in kept] == [("367000001", 1.0), ("367000001", 12.0)]
    assert kept[0].speed == 3.7 and kept[0].track == 267.0 and kept[0].alt is None
    assert kept[1].speed is None and kept[1].track is None


# --- The log ------------------------------------------------------------------------


def test_positions_are_written_read_and_pruned_with_the_log(tmp_path):
    w = _writer(tmp_path, max_age_s=100)
    w.write([Row(ts=950.0, profile="adsb", kind="aircraft", fields={}, subject="A1")])
    n = w.write_positions(
        [
            Position(800.0, "adsb", "A1", 33.9, -118.4, 30000.0, 250.0, 90.0),
            Position(960.0, "adsb", "A1", 33.95, -118.4, 30500.0, 250.0, 90.0),
            Position(970.0, "ais", "367000001", 33.5, -118.0, None, 3.7, 267.0),
        ]
    )
    assert n == 3
    conn = live_log.open_reader(w.path)
    assert [r["ts"] for r in live_log.positions(conn)] == [800.0, 960.0, 970.0]
    assert [r["ts"] for r in live_log.positions(conn, since=900, profile="adsb")] == [960.0]
    assert [r["subject"] for r in live_log.positions(conn, subject="0367000001")] == ["367000001"]
    w.prune(now=1000.0)  # older than 100 s goes, with the messages
    assert [r["ts"] for r in live_log.positions(conn)] == [960.0, 970.0]


def test_size_pruning_takes_positions_older_than_the_oldest_row_left(tmp_path):
    w = _writer(tmp_path, max_bytes=1)
    w.write([Row(ts=float(t), profile="adsb", kind="aircraft", fields={}, subject="A1") for t in range(100)])
    w.write_positions([Position(float(t), "adsb", "A1", 33.9, -118.4) for t in range(0, 100, 5)])
    w.prune(now=200.0)
    conn = live_log.open_reader(w.path)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert live_log.positions(conn) == []


def test_positions_are_dropped_while_disk_is_short(tmp_path, monkeypatch):
    w = _writer(tmp_path, min_free_bytes=1 << 40)
    monkeypatch.setattr(live_log.shutil, "disk_usage", lambda p: SimpleNamespace(free=1 << 20))
    assert w.write_positions([Position(1.0, "adsb", "A1", 1.0, 1.0)]) == 0
    assert w.stats.positions_dropped == 1


# --- The service ----------------------------------------------------------------------


class PositionPipeline:
    def __init__(self, tuning, ctx):
        self.tuning, self.ctx, self.source = tuning, ctx, None

    def start(self):
        self.ctx.emit_position(Position(1000.0, "adsb", "A1B2C3", 33.9, -118.4, 30000.0))

    def stop(self):
        pass

    def failure(self):
        return None

    def health(self):
        return {}

    def stderr_tail(self):
        return []


def test_the_service_logs_positions_its_pipelines_emit(tmp_path):
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    pump = LogPump(LiveLogWriter(live.db_path, 86400 * 365 * 100, 1 << 30, 0))
    svc = LiveService(live, pump, make_pipeline=PositionPipeline, program_finder=lambda p: "/bin/true")
    svc.handle({"cmd": "start", "profile": "adsb"})
    st = svc.status()
    pump.drain()
    assert st["positions_total"] == 1 and pump.writer.stats.positions_written == 1
    assert svc.status()["log"]["positions_written"] == 1
    [p] = live_log.positions(live_log.open_reader(live.db_path))
    assert (p["subject"], p["lat"], p["alt"]) == ("A1B2C3", 33.9, 30000.0)
