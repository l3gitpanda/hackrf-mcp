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
