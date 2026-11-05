"""Positions from ACARS and VDL2 datalink (datalink.py): ADS-C reports and
CPDLC position reports, from what libacars v2.2.1 itself decoded of made-up
ARINC 622 messages (tests/libacars_datalink.json), inside acarsdec's and
dumpvdl2's JSON as the station logs them; the report's own time; and the
rows and positions the acars and vdl2 profiles make of them."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hackrf_mcp import datalink, live_profiles
from hackrf_mcp.protocols import acars, vdl2

CASES = json.loads((Path(__file__).parent / "libacars_datalink.json").read_text())["cases"]


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


# Received at 18:43:00Z: the ADS-C report says 2534.5 s past the hour
# (18:42:14.5Z), the CPDLC one 18:41.
RECEIVED = utc(2026, 11, 5, 18, 43, 0)


def acarsdec_line(case, tail=".N12345", flight="TST123"):
    c = CASES[case]
    e = {"timestamp": RECEIVED, "station_id": "x", "channel": 4, "freq": 131.55, "level": -21.3, "error": 0, "mode": "2",
         "label": c["label"], "block_id": "5", "ack": False, "tail": tail, "flight": flight, "msgno": "M01A",
         "text": c["text"], "assstat": "skipped", "libacars": c["libacars"], "app": {"name": "acarsdec", "ver": "3.7"}}  # fmt: skip
    return json.dumps(e)


def dumpvdl2_line(case, src_type="Aircraft", addr="A1B2C3"):
    c = CASES[case]
    air = {"addr": addr, "type": "Aircraft", "status": "Airborne"}
    gs = {"addr": "10916A", "type": "Ground station"}
    src, dst = (air, gs) if src_type == "Aircraft" else (gs, air)
    acars_part = {"err": False, "crc_ok": True, "more": False, "reg": ".N12345", "mode": "2", "label": c["label"], "blk_id": "5",
                  "ack": "!", "flight": "TST123", "msg_num": "M01", "msg_num_seq": "A", "msg_text": c["text"],
                  "arinc622": c["libacars"]["arinc622"]}  # fmt: skip
    e = {"vdl2": {"app": {"name": "dumpvdl2", "ver": "2.6.0"}, "t": {"sec": int(RECEIVED), "usec": 0}, "freq": 136975000,
                  "sig_level": -12.0, "noise_level": -40.0, "avlc": {"src": src, "dst": dst, "cr": "Command", "frame_type": "I",
                  "rseq": 1, "sseq": 2, "poll": False, "acars": acars_part}}}  # fmt: skip
    return json.dumps(e)


def test_the_report_time_is_the_latest_that_matches_and_not_too_old():
    t = datalink.report_time
    assert t(RECEIVED, seconds_past_hour=2534.5) == utc(2026, 11, 5, 18, 42, 14) + 0.5
    # A little ahead of the station's clock is the same hour, not an hour ago.
    assert t(RECEIVED, seconds_past_hour=2640) == utc(2026, 11, 5, 18, 44, 0)
    # Further ahead: the previous hour.
    assert t(RECEIVED, seconds_past_hour=3000) == utc(2026, 11, 5, 17, 50, 0)
    assert t(RECEIVED, hour=18, minute=41) == utc(2026, 11, 5, 18, 41)
    assert t(utc(2026, 11, 6, 0, 3), hour=23, minute=59) == utc(2026, 11, 5, 23, 59)  # across midnight
    assert t(RECEIVED, hour=17, minute=40) is None  # over an hour old
    assert t(RECEIVED, hour=18, minute=50) is None  # a day ago, once it is behind the clock
    for bad in ({"seconds_past_hour": 3600}, {"seconds_past_hour": -1}, {"hour": 24, "minute": 0}, {"hour": 1}):
        assert t(RECEIVED, **bad) is None


@pytest.mark.parametrize("line", [acarsdec_line, dumpvdl2_line])
def test_positions_from_what_libacars_decoded(line):
    def got(case):
        return datalink.positions(json.loads(line(case)), RECEIVED)

    [p] = got("adsc_periodic")
    assert (p.source, p.report, p.alt, p.speed) == ("ADS-C", "basic_report", 35000, 452.0)
    assert p.lat == pytest.approx(41.2507, abs=1e-4) and p.lon == pytest.approx(-70.4519, abs=1e-4)
    assert p.track == pytest.approx(88.0, abs=0.1) and p.ts == utc(2026, 11, 5, 18, 42, 14) + 0.5
    assert got("adsc_emergency")[0].report == "emerg_basic_report"
    assert got("adsc_with_flight_id")[0].lat == pytest.approx(52.5, abs=1e-4)
    assert got("adsc_no_position") == []  # a flight id alone
    assert got("adsc_bad_crc") == []  # failed ARINC 622's CRC
    [c] = got("cpdlc_position")
    assert (c.source, c.alt, c.speed, c.track) == ("CPDLC", 35000, None, None)
    assert c.lat == pytest.approx(41 + 12.5 / 60) and c.lon == pytest.approx(-70.5) and c.ts == utc(2026, 11, 5, 18, 41)
    assert got("cpdlc_position_fix") == []  # a fix name: no coordinates to draw
    # No minutes, south, and an altitude in feet; reported at 23:59.
    [q] = datalink.positions(json.loads(line("cpdlc_position_qnh")), utc(2026, 11, 6, 0, 3))
    assert (q.lat, q.alt, q.ts) == (-10.0, 10500.0, utc(2026, 11, 5, 23, 59))
    assert q.lon == pytest.approx(120 + 5.9 / 60)


def test_nothing_from_messages_without_arinc622():
    assert datalink.positions({"text": "hello"}, RECEIVED) == []
    assert datalink.positions({"libacars": {"miam": {}}}, RECEIVED) == []
    assert datalink.positions({"vdl2": {"avlc": {"src": {}}}}, RECEIVED) == []
    assert datalink.positions([], RECEIVED) == []


def test_acars_rows_and_positions():
    row, pos = acars.parse_acars_positions(acarsdec_line("adsc_periodic"), 131_550_000, RECEIVED)
    assert row.subject == "N12345" and row.label == "TST123" and row.ts == RECEIVED
    assert row.fields["pos_source"] == "ADS-C" and row.fields["alt"] == 35000 and row.fields["pos_ts"] == pos[0].ts
    assert (row.fields["lat"], row.fields["lon"]) == (round(pos[0].lat, 5), round(pos[0].lon, 5))
    [p] = pos
    assert (p.profile, p.subject, p.alt, p.speed) == ("acars", "N12345", 35000, 452.0) and p.ts < RECEIVED
    plain, none = acars.parse_acars_positions(acarsdec_line("adsc_no_position"), 131_550_000, RECEIVED)
    assert none == [] and "lat" not in plain.fields
    assert acars.parse_acars(acarsdec_line("cpdlc_position"), 131_550_000, RECEIVED).fields["pos_source"] == "CPDLC"
    assert acars.parse_acars_positions("not json", 1, RECEIVED) == (None, [])


def test_vdl2_rows_and_positions_only_from_the_aircraft():
    row, [p] = vdl2.parse_vdl2_positions(dumpvdl2_line("cpdlc_position"), RECEIVED)
    assert row.subject == "A1B2C3" and row.fields["pos_source"] == "CPDLC" and row.fields["alt"] == 35000
    assert (p.profile, p.subject, p.ts) == ("vdl2", "A1B2C3", utc(2026, 11, 5, 18, 41))
    # Sent by a ground station: whatever it holds isn't where the aircraft says it is.
    up, none = vdl2.parse_vdl2_positions(dumpvdl2_line("cpdlc_position", src_type="Ground station"), RECEIVED)
    assert none == [] and up.subject == "A1B2C3" and "lat" not in up.fields


class _Ctx:
    def __init__(self):
        self.rows, self.positions, self.n = [], [], 0

    def emit(self, row):
        self.rows.append(row)

    def emit_position(self, p):
        self.positions.append(p)

    def count(self, n):
        self.n += n


def test_the_pipelines_log_each_report_as_sent():
    a = object.__new__(acars.AcarsPipeline)
    a.ctx = _Ctx()
    a.on_line(0, 131_550_000, acarsdec_line("adsc_periodic"))
    a.on_line(0, 131_550_000, acarsdec_line("adsc_periodic"))  # the same again: not thinned
    assert len(a.ctx.rows) == 2 and len(a.ctx.positions) == 2 and a.ctx.n == 2
    v = object.__new__(vdl2.Vdl2Pipeline)
    v.ctx = _Ctx()
    v._line(dumpvdl2_line("adsc_emergency"))
    assert [p.subject for p in v.ctx.positions] == ["A1B2C3"]


def test_the_profiles_draw_them_on_the_map():
    live_profiles.load()
    for name, kind in (("acars", "registration"), ("vdl2", "icao24")):
        p = live_profiles.PROFILES[name]
        assert p.positions_reported and p.subject_type == kind
        [m] = [v for v in p.ui_views() if v.name == "map"]
        assert dict(m.roles)["lat"] == "lat" and dict(m.roles)["alt"] == "alt"
        assert {"table", "timeline"} <= {v.name for v in p.ui_views()}
    assert not live_profiles.PROFILES["adsb"].positions_reported


def test_alerts_offer_the_aircraft_fields_on_vdl2_and_the_distance_on_both(tmp_path):
    from hackrf_mcp.web import aircraftdb, alerts

    live_profiles.load()
    d = alerts.Derived(aircraftdb.AircraftDB(tmp_path / "aircraft.db"), (10.0, 20.0))
    vdl = alerts.allowed_fields(live_profiles.PROFILES["vdl2"], False, d)
    acr = alerts.allowed_fields(live_profiles.PROFILES["acars"], False, d)
    assert "aircraft_type" in vdl and "distance_km" in vdl
    assert "aircraft_type" not in acr and "distance_km" in acr  # a tail isn't an address
    assert "pos_ts" not in vdl and "pos_source" in vdl
