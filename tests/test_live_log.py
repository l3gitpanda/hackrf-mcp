import json

from hackrf_mcp import live_log
from hackrf_mcp.live_log import LiveLogWriter, Row

MIB = 1024 * 1024


def _writer(tmp_path, **kw):
    args = {"max_age_s": 7 * 86400, "max_bytes": 256 * MIB, "min_free_bytes": 0}
    args.update(kw)
    return LiveLogWriter(tmp_path / "live.db", **args)


def _aircraft(ts, icao, callsign=None, alt=30000, msgs=10, level=-20.0):
    return Row(
        ts=ts,
        ts_end=ts + 30,
        profile="adsb",
        kind="aircraft",
        freq_hz=1_090_000_000,
        level_db=level,
        subject=icao,
        label=callsign,
        fields={"icao": icao, "callsign": callsign, "altitude_min": alt, "altitude_max": alt + 500, "messages": msgs},
        raw=f"{icao} {callsign or ''} alt {alt}",
    )


def test_write_search_by_subject_label_text_and_frequency(tmp_path):
    w = _writer(tmp_path)
    w.write(
        [
            _aircraft(1000, "A1B2C3", "UAL123"),
            _aircraft(1060, "A1B2C3", "UAL123"),
            _aircraft(1060, "ABCDEF", "SWA9"),
            Row(1100, "flex", "page", {"capcodes": ["1234567"], "type": "ALN"}, "FLEX|...|ALN|", 931_750_000, -45.0, "1234567", "ALN"),
        ]
    )
    c = live_log.open_reader(tmp_path / "live.db")
    assert [r["ts"] for r in live_log.search(c, subject="a1b2c3")] == [1060, 1000]
    assert len(live_log.search(c, subject="ual123")) == 2  # matches the label too
    # capcodes match with or without leading zeros
    assert len(live_log.search(c, subject="001234567")) == 1
    assert len(live_log.search(c, text="SWA")) == 1
    assert len(live_log.search(c, text="%")) == 0  # LIKE wildcards are literal
    assert len(live_log.search(c, freq_hz=931_752_000)) == 1
    assert len(live_log.search(c, profile="adsb", since=1050)) == 2
    assert len(live_log.search(c, until=1010)) == 1
    # a minute row still matches a range that starts inside it
    assert len(live_log.search(c, subject="A1B2C3", since=1020, until=1040)) == 1


def test_prune_by_age_and_events(tmp_path):
    w = _writer(tmp_path, max_age_s=3600)
    w.write([_aircraft(1000, "A1B2C3"), _aircraft(9000, "A1B2C3")])
    w.event("start", "adsb", "x", ts=1000)
    result = w.prune(now=10_000)
    assert result.deleted_age == 1
    c = live_log.open_reader(tmp_path / "live.db")
    assert [r["ts"] for r in live_log.search(c)] == [9000]
    assert live_log.events(c) == []


def test_prune_by_size_deletes_oldest_and_shrinks_the_file(tmp_path):
    w = _writer(tmp_path, max_bytes=1 * MIB)
    pad = "x" * 1000
    rows = [Row(float(i), "aprs", "aprs", {"payload": pad}, pad, 144_390_000, None, f"N{i}") for i in range(6000)]
    for i in range(0, len(rows), 1000):
        w.write(rows[i : i + 1000])
    before = w.file_bytes()
    result = w.prune(now=6000)
    assert result.deleted_size > 0
    assert w.used_bytes() <= 1 * MIB
    assert w.file_bytes() < before / 2  # auto_vacuum gave the space back
    c = live_log.open_reader(tmp_path / "live.db")
    oldest = c.execute("SELECT min(ts) FROM messages").fetchone()[0]
    assert oldest > 0  # the oldest rows went first


def test_rows_are_dropped_below_the_free_space_floor(tmp_path):
    w = _writer(tmp_path, min_free_bytes=1 << 62)
    assert w.write([_aircraft(1000, "A1B2C3")]) == 0
    assert w.stats.rows_dropped == 1 and w.stats.low_disk


def test_summarise_groups_by_subject(tmp_path):
    w = _writer(tmp_path)
    w.write(
        [
            _aircraft(1000, "A1B2C3", "UAL123", alt=10000, msgs=5, level=-30),
            _aircraft(1060, "A1B2C3", "UAL123", alt=12000, msgs=7, level=-10),
            _aircraft(1060, "ABCDEF", None, msgs=1),
        ]
    )
    c = live_log.open_reader(tmp_path / "live.db")
    s = live_log.summarise(c, "adsb", ("aircraft",), since=0)
    assert [e["subject"] for e in s] == ["A1B2C3", "ABCDEF"] or [e["subject"] for e in s] == ["ABCDEF", "A1B2C3"]
    a = next(e for e in s if e["subject"] == "A1B2C3")
    assert a["rows"] == 2 and a["labels"] == ["UAL123"] and a["max_level_db"] == -10
    assert a["fields"]["messages"] == 12
    assert a["fields"]["altitude_min"] == 10000 and a["fields"]["altitude_max"] == 12500


def test_raw_is_capped_and_fields_are_json(tmp_path):
    w = _writer(tmp_path)
    w.write([Row(1, "vdl2", "vdl2", {"a": 1}, "y" * 10_000)])
    c = live_log.open_reader(tmp_path / "live.db")
    r = live_log.search(c)[0]
    assert len(r["raw"]) == live_log.MAX_RAW_CHARS
    assert json.loads(r["fields"]) == {"a": 1}


def test_open_reader_without_a_log(tmp_path):
    try:
        live_log.open_reader(tmp_path / "missing.db")
    except live_log.LogNotFound as exc:
        assert "has not written one" in str(exc)
    else:
        raise AssertionError("expected LogNotFound")


def test_events_between_two_times(tmp_path):
    w = _writer(tmp_path)
    for t in (10.0, 20.0, 30.0):
        w.event("start", "adsb", None, ts=t)
    conn = live_log.open_reader(w.path)
    assert [r["ts"] for r in live_log.events(conn, since=15, until=25)] == [20.0]
    assert [r["ts"] for r in live_log.events(conn, limit=2)] == [30.0, 20.0]


def test_search_pages_backwards_and_takes_several_profiles(tmp_path):
    w = _writer(tmp_path)
    w.write([Row(ts=float(t), profile=("adsb", "ais", "flex")[t % 3], kind="x", fields={}, subject=f"S{t}") for t in range(10)])
    conn = live_log.open_reader(w.path)
    first = live_log.search(conn, limit=4)
    assert [r["ts"] for r in first] == [9.0, 8.0, 7.0, 6.0]
    nxt = live_log.search(conn, limit=4, before_id=first[-1]["id"])
    assert [r["ts"] for r in nxt] == [5.0, 4.0, 3.0, 2.0]
    two = live_log.search(conn, profiles=("adsb", "flex"), limit=20)
    assert {r["profile"] for r in two} == {"adsb", "flex"} and len(two) == 7


def test_histogram_counts_rows_per_bucket_and_profile(tmp_path):
    w = _writer(tmp_path)
    w.write([Row(ts=float(t), profile="adsb" if t < 50 else "ais", kind="x", fields={}, subject="S") for t in range(100)])
    conn = live_log.open_reader(w.path)
    out = live_log.histogram(conn, 0.0, 100.0, buckets=4)
    assert out["bucket_s"] == 25.0
    assert out["counts"] == [{"adsb": 25}, {"adsb": 25}, {"ais": 25}, {"ais": 25}]
    only = live_log.histogram(conn, 0.0, 100.0, buckets=2, profiles=("ais",))
    assert only["counts"] == [{}, {"ais": 50}]


def test_coverage_rebuilds_what_the_receiver_was_doing(tmp_path):
    w = _writer(tmp_path)
    ev = [
        (5.0, "service-start", None, "pid 1"),
        (10.0, "start", "adsb", "tuned"),
        (100.0, "stop", "adsb", "switching"),
        (100.001, "start", "ais", "tuned"),
        (200.0, "stop", "ais", "paused for hackrf_transfer"),
        (230.0, "start", "ais", "tuned"),
        (300.0, "stop", "ais", "failed"),
        (300.0, "error", "ais", "hackrf_transfer exited; retrying in 2 s"),
        (305.0, "start", "ais", "tuned"),
        (400.0, "stop", "ais", "stopped on request"),
        (450.0, "service-stop", None, None),
    ]
    for ts, e, p, d in ev:
        w.event(e, p, d, ts=ts)
    conn = live_log.open_reader(w.path)
    cov = live_log.coverage(conn, 0.0, 500.0)
    assert [(c["state"], c["profile"], c["from"], c["to"]) for c in cov] == [
        ("down", None, 0.0, 5.0),
        ("idle", None, 5.0, 10.0),
        ("running", "adsb", 10.0, 100.0),
        ("running", "ais", 100.001, 200.0),
        ("paused", None, 200.0, 230.0),
        ("running", "ais", 230.0, 300.0),
        ("error", None, 300.0, 305.0),
        ("running", "ais", 305.0, 400.0),
        ("idle", None, 400.0, 450.0),
        ("down", None, 450.0, 500.0),
    ]
    # A range that starts mid-run knows what was running from before it.
    assert live_log.coverage(conn, 150.0, 180.0) == [{"state": "running", "profile": "ais", "from": 150.0, "to": 180.0}]


def test_coverage_follows_the_fallback_on_a_lane_of_its_own(tmp_path):
    """The worker's outage of 2026-11-06 00:33-00:41Z as the station logged
    it: while the worker was down the agent's machine decoded AIS, then
    ADS-B, and its imported stop ("paused for hackrf-agent") came 9 ms after
    the worker's own start. That read as 227 s paused; it was decoding."""
    fb = f" ({live_log.FALLBACK_NOTE})"
    w = _writer(tmp_path)
    ev = [
        (1000.0, "start", "adsb", "tuned"),
        (1209.579, "stop", "adsb", "service stopping"),
        (1210.022, "service-stop", None, None),
        (1332.393, "start", "ais", "ais: tuned 162.0000 MHz" + fb),
        (1400.078, "stop", "ais", "switching" + fb),
        (1400.085, "start", "adsb", "adsb: tuned 1090.0000 MHz" + fb),
        (1408.606, "service-start", None, "pid 7"),
        (1408.901, "start", "adsb", "tuned"),
        (1408.910, "stop", "adsb", "paused for hackrf-agent (a worker has the HackRF)" + fb),
        (1635.950, "stop", "adsb", "service stopping"),
        (1636.150, "service-stop", None, None),
        (1639.111, "service-start", None, "pid 7"),
        (1639.140, "start", "adsb", "tuned"),
    ]
    for ts, e, p, d in ev:
        w.event(e, p, d, ts=ts)
    conn = live_log.open_reader(w.path)
    cov = live_log.coverage(conn, 1100.0, 1700.0)
    assert [(c["state"], c["profile"], c["from"], c["to"], c.get("fallback", False)) for c in cov] == [
        ("running", "adsb", 1100.0, 1209.579, False),
        ("down", None, 1209.579, 1332.393, False),
        ("running", "ais", 1332.393, 1400.078, True),
        ("running", "adsb", 1400.085, 1408.901, True),
        ("running", "adsb", 1408.901, 1635.950, False),
        ("down", None, 1635.950, 1639.111, False),
        ("idle", None, 1639.111, 1639.140, False),
        ("running", "adsb", 1639.140, 1700.0, False),
    ]
    # A range starting while only the fallback decoded knows it from before.
    assert live_log.coverage(conn, 1350.0, 1360.0) == [
        {"state": "running", "profile": "ais", "from": 1350.0, "to": 1360.0, "fallback": True}
    ]
    # And one starting after the hand-back is the worker's, not paused.
    assert live_log.coverage(conn, 1500.0, 1510.0) == [{"state": "running", "profile": "adsb", "from": 1500.0, "to": 1510.0}]
    assert live_log.is_fallback("x" + fb) and live_log.is_fallback(live_log.FALLBACK_NOTE) and not live_log.is_fallback("tuned")


def test_summarise_survives_fields_of_mixed_types(tmp_path):
    w = _writer(tmp_path)
    w.write([
        Row(ts=1.0, profile="adsb", kind="aircraft", subject="A1", fields={"altitude_min": 3000, "altitude_max": 3000, "messages": 2}),
        Row(ts=2.0, profile="adsb", kind="aircraft", subject="A1", fields={"altitude_min": "ground", "altitude_max": 3500, "messages": "x"}),
        Row(ts=3.0, profile="adsb", kind="aircraft", subject="A1", fields={"altitude_min": 2500, "altitude_max": 4000, "messages": 3}),
    ])
    [s] = live_log.summarise(live_log.open_reader(w.path), "adsb", ("aircraft",), 0.0)
    assert s["fields"]["altitude_max"] == 4000 and s["fields"]["messages"] == 5
    assert s["fields"]["altitude_min"] == 2500  # the odd "ground" didn't break the comparison


def test_summaries_count_messages_subjects_and_the_busiest(tmp_path):
    w = _writer(tmp_path)
    w.write(
        [
            # A row that started before the range but runs into it counts.
            _aircraft(970, "A1B2C3", "UAL123", alt=10000, msgs=5, level=-30),
            _aircraft(1060, "A1B2C3", "UAL123", alt=12000, msgs=7, level=-10),
            _aircraft(1100, "ABCDEF", None, msgs=40),
            _aircraft(1500, "OUTSIDE", None, msgs=99),  # after the range
            Row(1010, "ais", "vessel", {"mmsi": "1", "name": "KILLEEN", "messages": 3}, subject="367000001", label="KILLEEN"),
            Row(1020, "ais", "aid-to-navigation", {"mmsi": "2", "messages": 2}, subject="993000002"),
            Row(1030, "ais", "ais", {"mmsi": "1"}, subject="367000001"),  # no count: one message
            Row(1040, "flex", "page", {"capcodes": ["1234567"], "type": "ALN"}, "FLEX|ALN|", 931_750_000, None, "1234567", "ALN"),
            Row(1041, "flex", "page", {"capcodes": ["1234567"], "type": "NUM"}, "FLEX|NUM|", 931_937_500, None, "1234567", "NUM"),
        ]
    )
    c = live_log.open_reader(w.path)
    profiles = {
        "adsb": (("aircraft",), ("callsign", "altitude_min", "altitude_max", "messages")),
        "ais": (("vessel", "aid-to-navigation", "ais"), ("name", "messages")),
        "flex": (("page",), ("type",)),
        "eas": (("eas",), ()),
    }
    out = live_log.summaries(c, 1000.0, 1200.0, profiles, buckets=4, top=1)
    assert out["bucket_s"] == 50.0
    adsb = out["profiles"]["adsb"]
    assert adsb["rows"] == 3 and adsb["messages"] == 52 and adsb["subjects"] == 2
    assert adsb["rate"] == [5, 7, 40, 0] and sum(adsb["rate"]) == adsb["messages"]
    (busiest,) = adsb["top"]  # top=1: the one with the most messages
    assert busiest["subject"] == "ABCDEF" and busiest["messages"] == 40 and busiest["labels"] == []
    two = live_log.summaries(c, 1000.0, 1200.0, profiles, buckets=4, top=5)["profiles"]
    a = next(e for e in two["adsb"]["top"] if e["subject"] == "A1B2C3")
    assert a["rows"] == 2 and a["messages"] == 12 and a["max_level_db"] == -10 and a["labels"] == ["UAL123"]
    assert a["fields"] == {"callsign": "UAL123", "altitude_min": 10000, "altitude_max": 12500}
    assert a["kinds"] == ["aircraft"] and a["freqs"] == [1_090_000_000]
    ais = two["ais"]
    assert ais["messages"] == 6 and ais["subjects"] == 2
    assert ais["by_kind"] == {"vessel": 1, "aid-to-navigation": 1, "ais": 1}
    ship = ais["top"][0]
    assert ship["subject"] == "367000001" and ship["messages"] == 4 and ship["fields"] == {"name": "KILLEEN"}
    assert sorted(ship["kinds"]) == ["ais", "vessel"]
    flex = two["flex"]
    assert flex["messages"] == 2 and flex["subjects"] == 1
    assert flex["top"][0]["freqs"] == [931_750_000, 931_937_500] and flex["top"][0]["fields"] == {"type": "NUM"}
    assert flex["top"][0]["labels"] == ["NUM", "ALN"]
    assert two["eas"] == {"rows": 0, "messages": 0, "rate": [0, 0, 0, 0], "subjects": 0, "by_kind": {}, "top": []}


def test_summaries_read_odd_rows_without_failing(tmp_path):
    w = _writer(tmp_path)
    w.write([Row(1000, "adsb", "aircraft", {"messages": "lots"}, subject=None, label=None)])
    w.conn.execute("UPDATE messages SET fields = 'not json'")
    w.conn.commit()
    c = live_log.open_reader(w.path)
    out = live_log.summaries(c, 900.0, 1100.0, {"adsb": (("aircraft",), ("callsign",))})
    adsb = out["profiles"]["adsb"]
    assert adsb["messages"] == 1 and adsb["subjects"] == 1 and adsb["top"][0]["subject"] == "?"


def test_position_grid_counts_fixes_and_targets_per_cell(tmp_path):
    w = _writer(tmp_path)
    w.write_positions(
        [
            live_log.Position(1000, "adsb", "A1", 33.9101, -118.4101),
            live_log.Position(1010, "adsb", "A1", 33.9102, -118.4102),
            live_log.Position(1020, "adsb", "B2", 33.9103, -118.4103),
            live_log.Position(1030, "adsb", "B2", 34.5, -117.0),
            live_log.Position(1040, "ais", "S1", 33.7, -118.2),
            live_log.Position(5000, "adsb", "LATE", 33.91, -118.41),  # after the range
        ],
        now=1000,
    )
    c = live_log.open_reader(w.path)
    g = live_log.position_grid(c, 900, 2000, cell_deg=0.1)
    assert g["cell_deg"] == 0.1 and g["fixes"] == 5 and g["targets"] == 3 and not g["truncated"]
    cells = {tuple(x[:2]): x[2:] for x in g["cells"]["adsb"]}
    x, y = int((-118.41 + 180) / 0.1), int((33.91 + 90) / 0.1)
    assert cells[(x, y)] == [3, 2] and len(cells) == 2
    assert len(g["cells"]["ais"]) == 1
    only = live_log.position_grid(c, 900, 2000, profile="ais", cell_deg=0.1)
    assert set(only["cells"]) == {"ais"} and only["fixes"] == 1
    boxed = live_log.position_grid(c, 900, 2000, cell_deg=0.1, bbox=(-118.5, 33.8, -118.3, 34.0))
    assert boxed["fixes"] == 3 and list(boxed["cells"]) == ["adsb"]
    # Too many cells for the cap: the cell doubles until they fit.
    coarse = live_log.position_grid(c, 900, 2000, cell_deg=0.002, max_cells=2)
    assert coarse["cell_deg"] > 0.002 and sum(len(v) for v in coarse["cells"].values()) <= 2
    assert live_log.position_grid(c, 900, 2000, cell_deg=0.0)["cell_deg"] == live_log.GRID_MIN_DEG


# A made-up receiver, for the range tests.
RX = (45.0, 10.0)


def _toward(bearing, km, origin=RX):
    """The point `km` from `origin` on `bearing` (degrees): the inverse of
    live_log.distance_bearing, to place test fixes."""
    import math

    p0, l0 = math.radians(origin[0]), math.radians(origin[1])
    d, b = km / live_log.EARTH_KM, math.radians(bearing)
    p = math.asin(math.sin(p0) * math.cos(d) + math.cos(p0) * math.sin(d) * math.cos(b))
    lon = l0 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p0), math.cos(d) - math.sin(p0) * math.sin(p))
    return math.degrees(p), math.degrees(lon)


def test_distance_and_bearing_round_trip():
    import numpy as np

    for bearing, km in ((0, 10), (45, 150), (180, 300), (271, 42.5)):
        lat, lon = _toward(bearing, km)
        d, b = live_log.distance_bearing(RX[0], RX[1], np.array([lat]), np.array([lon]))
        assert abs(d[0] - km) < 0.01 and abs(b[0] - bearing) < 0.01
    # The horizon to an airliner is a few hundred km; to the ground, far less.
    assert 450 < live_log.radio_horizon_km(35_000) < 600 and live_log.radio_horizon_km(0) < 80


def test_receiver_range_per_sector_band_and_level(tmp_path):
    w = _writer(tmp_path)
    fixes = [
        ("A1", 1000, 10, 100, 30_000),
        ("A1", 1010, 12, 150, 30_000),  # the farthest in 10-15 degrees, high
        ("B2", 1020, 92, 50, 5_000),  # 90-95: low
        ("B2", 1030, 93, 80, 15_000),  # 90-95: mid, and the farthest there
        ("C3", 1040, 200, 300, 1_000),  # 300 km at 1,000 ft: past the horizon, an error
        ("D4", 1050, 270, 40, None),  # no altitude: in "all" only
    ]
    pos = [live_log.Position(ts, "adsb", s, *_toward(b, km), alt=alt) for s, ts, b, km, alt in fixes]
    pos.append(live_log.Position(1060, "ais", "S1", *_toward(180, 20)))
    pos.append(live_log.Position(5000, "adsb", "LATE", *_toward(0, 400), alt=35_000))  # after the range
    w.write_positions(pos, now=1000)
    rows = []
    for i, (km, level) in enumerate(((10, -5.0), (12, -7.0), (14, -6.0), (110, -30.0), (120, -32.0))):
        lat, lon = _toward(5, km)
        r = _aircraft(1000 + i, f"L{i}", "UAL1" if i == 4 else None, level=level)
        r.fields.update(lat=lat, lon=lon, altitude=30_000)
        rows.append(r)
    rows.append(_aircraft(1011, "A1", "SWA12"))  # A1's callsign, next to its farthest fix
    w.write(rows)
    c = live_log.open_reader(w.path)
    out = live_log.receiver_range(c, RX, 900, 2000, profile="adsb", sector_deg=5)
    assert out["sector_deg"] == 5 and out["fixes"] == 5 and out["targets"] == 3 and out["beyond_horizon"] == 1
    allb, high, mid, low = (out["sectors"][k] for k in ("all", "high", "mid", "low"))
    assert len(allb) == 72 and allb[2] == 150.0 and allb[18] == 80.0 and allb[54] == 40.0
    assert sum(v is not None for v in allb) == 3 and allb[40] is None  # 200 degrees: only the error was there
    assert high[2] == 150.0 and high[18] is None and mid[18] == 80.0 and low[18] == 50.0
    assert low[54] is None and [b["name"] for b in out["bands"]] == ["low", "mid", "high"]
    assert out["median_km"] == 80.0
    f = out["farthest"]
    assert (f["subject"], f["label"], f["km"], f["alt"]) == ("A1", "SWA12", 150.0, 30_000.0) and abs(f["bearing"] - 12) < 0.1
    lv = out["levels"]
    assert lv["bin_km"] == 25 and lv["rows"] == 5
    near, far = lv["bins"]
    assert near[:2] == [0.0, 3] and near[3] == -6.0 and far[:2] == [100.0, 2] and far[3] == -31.0
    # Ships have no altitude: no bands, and no level rows here.
    ships = live_log.receiver_range(c, RX, 900, 2000, profile="ais")
    assert ships["bands"] == [] and set(ships["sectors"]) == {"all"} and ships["sectors"]["all"][36] == 20.0
    assert ships["levels"]["bins"] == [] and ships["farthest"]["label"] is None
    # Nothing in the range: all empty, no error.
    none = live_log.receiver_range(c, RX, 9000, 9100)
    assert none["fixes"] == 0 and none["sectors"] == {} and none["farthest"] is None and none["median_km"] is None
