"""The aircraft database (web/aircraftdb.py): building it from small
made-up FAA and plane-alert-db files, the classes and their order, what is
and isn't kept, refreshing, and the API. No network."""

import csv
import hashlib
import io
import zipfile

import pytest
from starlette.testclient import TestClient

from hackrf_mcp import settings
from hackrf_mcp.web import aircraftdb as A
from hackrf_mcp.web import app as web_app

MASTER_HEAD = [
    "N-NUMBER", "SERIAL NUMBER", "MFR MDL CODE", "ENG MFR MDL", "YEAR MFR", "TYPE REGISTRANT", "NAME", "STREET",
    "STREET2", "CITY", "STATE", "ZIP CODE", "REGION", "COUNTY", "COUNTRY", "LAST ACTION DATE", "CERT ISSUE DATE",
    "CERTIFICATION", "TYPE AIRCRAFT", "TYPE ENGINE", "STATUS CODE", "MODE S CODE", "FRACT OWNER", "AIR WORTH DATE",
    "OTHER NAMES(1)", "OTHER NAMES(2)", "OTHER NAMES(3)", "OTHER NAMES(4)", "OTHER NAMES(5)", "EXPIRATION DATE",
    "UNIQUE ID", "KIT MFR", "KIT MODEL", "MODE S CODE HEX", "",
]  # fmt: skip
STREET = "1 NOWHERE LANE"
CO_OWNER = "SECOND OWNER NAME"
# (N-number, model code, registrant type, name, aircraft type, hex): all made up.
FAA = [
    ("12345", "M1", "1", "JANE EXAMPLE", "4", "A0B1C2"),
    ("911SD", "H1", "5", "EXAMPLE COUNTY SHERIFFS OFFICE", "6", "A1A1A1"),
    ("1AF", "M2", "5", "UNITED STATES AIR FORCE", "5", "A2A2A2"),
    ("42UN", "M1", "5", "EXAMPLE STATE UNIVERSITY", "4", "A3A3A3"),
    ("737EX", "M2", "3", "EXAMPLE AIRLINES INC", "5", "A4A4A4"),
    ("9BAD", "M1", "1", "BAD HEX", "4", "XYZ"),
    ("1PD", "H1", "5", "EXPD AIR UNIT", "6", "A5A5A5"),
    ("88DH", "H1", "5", "DEPT OF HOMELAND SECURITY", "6", "A6A6A6"),
    ("7CA", "M1", "3", "CIVIL AIR PATROL INC", "4", "A7A7A7"),
]
PA_HEAD = ["$ICAO", "$Registration", "$Operator", "$Type", "$ICAO Type", "#CMPG", "$Tag 1", "$#Tag 2", "$#Tag 3", "Category", "$#Link"]
PA = [
    ["AE1234", "10-0001", "United States Air Force", "C-17A Globemaster III", "C17", "Mil", "Moose", "", "", "USAF", ""],
    ["C0FFEE", "C-POLI", "Example Police Service", "AS350", "AS50", "Pol", "Eye in the sky", "", "", "Police Forces", ""],
    ["400ABC", "G-GOVT", "Example Government", "BAe 146", "B462", "Gov", "", "", "", "https://example.invalid/x", ""],
    ["A0B1C2", "N12345", "Jane", "C172", "C172", "Civ", "", "", "", "Vanity Plate", ""],
    ["12345", "BAD", "short address", "", "", "Mil", "", "", "", "USAF", ""],
    ["ABCDEF", "X", "odd class", "", "", "???", "", "", "", "", ""],
    ["A6A6A6", "N88DH", "Homeland Security", "AS350", "AS50", "Gov", "", "", "", "Governments", ""],
]


def _faa_zip(path, rows=FAA):
    lines = io.StringIO()
    w = csv.writer(lines)
    w.writerow(MASTER_HEAD)
    for n, mdl, reg, name, kind, hx in rows:
        row = [""] * len(MASTER_HEAD)
        row[0], row[2], row[5], row[6], row[7], row[9], row[18], row[24], row[33] = (
            n, mdl, reg, name, STREET, "SOMETOWN", kind, CO_OWNER, hx,
        )  # fmt: skip
        w.writerow(row)
    ref = "CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT,BUILD-CERT-IND,NO-ENG,NO-SEATS,AC-WEIGHT,SPEED,TC-DATA-SHEET,TC-DATA-HOLDER,\r\n"
    ref += "M1     ,CESSNA                        ,172S              ,4,1,1,0,01,004,CLASS 1,0000,,,\r\n"
    ref += "M2     ,BOEING                        ,737-8             ,5,5,1,0,02,189,CLASS 3,0000,,,\r\n"
    ref += "H1     ,AIRBUS HELICOPTERS            ,AS350B3           ,6,3,1,0,01,006,CLASS 1,0000,,,\r\n"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("MASTER.txt", "﻿" + lines.getvalue())
        z.writestr("ACFTREF.txt", ref)
        z.writestr("DEREG.txt", "not read\r\n")


def _pa_csv(path, rows=PA, head=PA_HEAD):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)


@pytest.fixture
def built(tmp_path):
    _faa_zip(tmp_path / "faa.zip")
    _pa_csv(tmp_path / "pa.csv")
    db = A.AircraftDB(tmp_path / "web" / "aircraft.db")
    db.path.parent.mkdir()
    counts = A.build(db.path, tmp_path / "faa.zip", tmp_path / "pa.csv", now=1000.0)
    return db, counts


def test_build_keeps_what_is_shown_and_nothing_else(built):
    db, counts = built
    assert counts == {"faa_rows": 8, "plane_alert_rows": 5}  # the bad hex and odd rows skipped
    raw = db.path.read_bytes()
    assert STREET.encode() not in raw and b"SOMETOWN" not in raw and CO_OWNER.encode() not in raw
    assert not db.path.with_name("aircraft.db.tmp").exists()
    st = db.status()
    assert (st["built_at"], st["faa_rows"], st["plane_alert_rows"], st["enabled"]) == (1000.0, 8, 5, False)


def test_classes_in_order(built):
    db, _ = built
    got = db.lookup(
        [
            ("ae1234", None),  # plane-alert Mil (and in the US military range)
            ("ADF7C8", None),  # the range alone
            ("ADF7C7", None),  # just below it: N99999's address, civil
            ("A1A1A1", "CHP12"),  # FAA government, a sheriff: before the airline-style callsign
            ("A2A2A2", None),
            ("A3A3A3", None),
            ("A4A4A4", "EXA123"),
            ("A4A4A4", None),
            ("A0B1C2", "N12345"),
            ("C0FFEE", None),
            ("400ABC", None),
            ("123456", "UAL1"),
            ("654321", "N123AB"),
            ("ZZZZZZ", None),  # not an address: left out
        ]
    )
    assert "ZZZZZZ" not in got
    cls = {k: v["class"] for k, v in got.items()}
    assert cls == {
        "AE1234": "military",
        "ADF7C8": "military",
        "ADF7C7": "unknown",
        "A1A1A1": "law_enforcement",
        "A2A2A2": "military",
        "A3A3A3": "government",
        "A4A4A4": "private",  # the last (icao, callsign) asked for wins: no callsign
        "A0B1C2": "private",
        "C0FFEE": "law_enforcement",
        "400ABC": "government",
        "123456": "commercial",
        "654321": "unknown",
    }
    assert got["AE1234"]["why"] == "plane-alert-db: USAF" and got["AE1234"]["operator"] == "United States Air Force"
    assert got["AE1234"]["tags"] == ["Moose"]
    assert "ADF7C8-AFFFFF" in got["ADF7C8"]["why"]
    sheriff = got["A1A1A1"]
    assert sheriff["why"] == "FAA: government registrant, EXAMPLE COUNTY SHERIFFS OFFICE"
    assert (sheriff["registration"], sheriff["manufacturer"], sheriff["model"], sheriff["kind"]) == (
        "N911SD", "AIRBUS HELICOPTERS", "AS350B3", "Rotorcraft",
    )  # fmt: skip
    assert sheriff["owner"] == "EXAMPLE COUNTY SHERIFFS OFFICE" and sheriff["registrant"] == "Government"
    # Every owner is shown (the owner's choice), an individual's too; FAA
    # data wins over plane-alert's Civ entry for the same aircraft.
    jane = got["A0B1C2"]
    assert jane["owner"] == "JANE EXAMPLE" and jane["why"] == "FAA: individual registrant" and jane["registration"] == "N12345"
    assert jane["category"] == "Vanity Plate"
    # A link in plane-alert's category column is dropped.
    assert "category" not in got["400ABC"] and got["400ABC"]["why"] == "plane-alert-db: Gov"
    assert got["123456"]["why"] == "airline-style callsign (UAL)"
    one = db.lookup([("A4A4A4", "EXA123")])["A4A4A4"]
    assert one["class"] == "commercial" and one["registrant"] == "Corporation"
    more = db.lookup([("A5A5A5", None), ("A6A6A6", None), ("A7A7A7", "CAP123"), ("777777", "CAP4567"), ("777778", "CAPX")])
    assert more["A5A5A5"]["class"] == "law_enforcement"  # an "xxPD" owner
    # plane-alert's broad Gov loses to an owner named for police work.
    assert more["A6A6A6"]["class"] == "law_enforcement" and "HOMELAND" in more["A6A6A6"]["why"]
    assert more["A7A7A7"]["class"] == more["777777"]["class"] == "government"
    assert "Civil Air Patrol" in more["777777"]["why"] and more["777778"]["class"] == "unknown"


def test_without_a_database_the_rules_still_work(tmp_path):
    db = A.AircraftDB(tmp_path / "none.db")
    got = db.lookup([("AE0001", None), ("A00001", "SWA9"), ("A00002", None)])
    assert [got[k]["class"] for k in ("AE0001", "A00001", "A00002")] == ["military", "commercial", "unknown"]
    assert db.status()["built_at"] is None and db.due()


def test_a_failed_build_keeps_the_old_database(built, tmp_path):
    db, _ = built
    _pa_csv(tmp_path / "bad.csv", head=["ICAO", "Class"])
    with pytest.raises(ValueError, match="columns changed"):
        A.build(db.path, tmp_path / "faa.zip", tmp_path / "bad.csv", now=2000.0)
    assert db.status()["built_at"] == 1000.0 and not db.path.with_name("aircraft.db.tmp").exists()
    _faa_zip(tmp_path / "empty.zip", rows=[])
    with pytest.raises(ValueError, match="no aircraft"):
        A.build(db.path, tmp_path / "empty.zip", tmp_path / "pa.csv")
    assert db.lookup([("A1A1A1", None)])["A1A1A1"]["class"] == "law_enforcement"


def test_refresh_weekly_and_retry_after_a_failure(tmp_path):
    _faa_zip(tmp_path / "faa.zip")
    _pa_csv(tmp_path / "pa.csv")
    fetched = []
    fail = {"on": False}

    def fetch(url, dest):
        fetched.append(url)
        if fail["on"]:
            raise OSError("network down")
        dest.write_bytes((tmp_path / ("faa.zip" if url == A.FAA_URL else "pa.csv")).read_bytes())

    now = {"t": 10_000.0}
    (tmp_path / "web").mkdir()
    db = A.AircraftDB(tmp_path / "web" / "aircraft.db", enabled=True, fetch=fetch, clock=lambda: now["t"])
    assert db.due()
    assert db.refresh() and fetched == [A.FAA_URL, A.PLANE_ALERT_URL]
    assert not (tmp_path / "web" / "aircraftdb-tmp").exists()  # downloads removed
    assert db.status()["built_at"] == 10_000.0 and not db.due()
    now["t"] += A.MAX_AGE_S
    assert db.due()
    fail["on"] = True
    assert not db.refresh()
    assert db.status()["last_error"] == "OSError: network down" and db.status()["faa_rows"] == 8  # the old one stays
    assert not db.due()  # wait before trying again
    now["t"] += A.RETRY_S
    assert db.due()
    fail["on"] = False
    assert db.refresh() and db.status()["last_error"] is None


def test_off_by_default_and_only_off_or_weekly(tmp_path):
    assert settings.WebSettings().aircraft_db == "off"
    assert settings.WebSettings.from_env({"HACKRF_WEB_AIRCRAFT_DB": "Weekly"}).aircraft_db == "weekly"
    with pytest.raises(ValueError, match="HACKRF_WEB_AIRCRAFT_DB"):
        settings.WebSettings.from_env({"HACKRF_WEB_AIRCRAFT_DB": "daily"})
    db = A.AircraftDB(tmp_path / "aircraft.db", fetch=lambda url, dest: pytest.fail("fetched while off"))
    db.start()  # off: no thread, nothing fetched
    assert db._thread is None


TOKEN = "correct-horse-battery-staple-42"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def test_the_aircraft_route_registry_and_status(built, tmp_path):
    db, _ = built
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    app = web_app.create_app(
        live, settings.WebSettings(), [hashlib.sha256(TOKEN.encode()).digest()], b"k" * 32, feed=False, aircraft=db,
        control=lambda req, timeout=20.0: {"ok": True, "state": "idle"},
    )  # fmt: skip
    c = TestClient(app)
    out = c.get("/api/aircraft", params={"a": "a1a1a1,123456:UAL12,AE1234"}, headers=BEARER).json()
    assert {k: v["class"] for k, v in out["aircraft"].items()} == {
        "A1A1A1": "law_enforcement",
        "123456": "commercial",
        "AE1234": "military",
    }
    assert out["db"]["faa_rows"] == 8
    reg = c.get("/api/registry", headers=BEARER).json()
    assert [k["name"] for k in reg["aircraft"]["classes"]][:3] == ["military", "law_enforcement", "government"]
    assert {k["short"] for k in reg["aircraft"]["classes"] if k["notable"]} == {"MIL", "LAW", "GOV"}
    assert "ODbL" in reg["aircraft"]["credit"] and reg["aircraft"]["enabled"] is False
    types = {p["name"]: p["subject_type"] for p in reg["profiles"]}
    assert types["adsb"] == "icao24" and types["ais"] is None
    assert c.get("/api/status", headers=BEARER).json()["aircraft_db"]["plane_alert_rows"] == 5
    assert c.get("/api/aircraft", headers=BEARER).status_code == 400
    assert c.get("/api/aircraft", params={"a": ",".join(["A1A1A1"] * 501)}, headers=BEARER).status_code == 400
    # Registrations (ACARS tails) to addresses.
    regs = c.get("/api/aircraft", params={"r": "N911SD,.C-POLI"}, headers=BEARER).json()
    assert regs["registrations"] == {"N911SD": "A1A1A1", "C-POLI": "C0FFEE"} and regs["aircraft"] == {}
    types = {p["name"]: (p["subject_type"], p["positions_reported"]) for p in reg["profiles"]}
    assert types["vdl2"] == ("icao24", True) and types["acars"] == ("registration", True) and types["adsb"] == ("icao24", False)
    assert c.get("/api/aircraft", params={"a": "A1A1A1"}).status_code == 401


def test_registrations_to_addresses(built, tmp_path):
    db, _ = built
    # As ACARS gives them: dots in front, with or without the dash.
    assert db.by_registration(["N12345", ".C-POLI", "100001", "NOPE", "bad reg!", ""]) == {
        "N12345": "A0B1C2",  # FAA and plane-alert-db agree
        "C-POLI": "C0FFEE",
        "100001": "AE1234",  # plane-alert-db's 10-0001
    }
    # The FAA record wins; two plane-alert-db addresses for one registration is no answer.
    _faa_zip(tmp_path / "faa.zip")
    _pa_csv(tmp_path / "pa2.csv", rows=PA + [
        ["ABC123", "N12345", "Someone else", "C172", "C172", "Civ", "", "", "", "", ""],
        ["400ABD", "G-GOVT", "Example Government", "BAe 146", "B462", "Gov", "", "", "", "", ""],
    ])  # fmt: skip
    other = A.AircraftDB(tmp_path / "other" / "aircraft.db")
    other.path.parent.mkdir()
    A.build(other.path, tmp_path / "faa.zip", tmp_path / "pa2.csv", now=1000.0)
    assert other.by_registration(["N12345", "G-GOVT", "GGOVT"]) == {"N12345": "A0B1C2"}
    assert A.AircraftDB(tmp_path / "none.db").by_registration(["N12345"]) == {}


def test_airline_flight_numbers_from_acars_and_vdl2_are_commercial():
    """ACARS and VDL2 give IATA flight numbers (WN1733), not ICAO callsigns
    (SWA1733): an airliner registered to a corporation was read as private.
    A registration that has the same shape (N1234) is not a flight."""
    corp = {"faa": 1, "registrant_type": "3", "registration": "N8710M"}
    for cs, code in (("WN1733", "WN"), ("AA2308", "AA"), ("B61234", "B6"), ("9E5123", "9E"), ("UA0123", "UA"), ("DL12A", "DL")):
        cls, why = A.classify("A1B2C3", cs, corp)
        assert (cls, why) == ("commercial", f"airline flight number ({code})"), cs
    assert A.classify("A1B2C3", "SWA1733", corp)[0] == "commercial"
    for cs in ("N1234", "N12345", "N123AB", "N8710M", ".N8710M", "N1A", "WN", "W1", "123456", "WN12345"):
        assert A.classify("A1B2C3", cs, corp)[0] == "private", cs
    # A foreign registration with the same shape, known from the database.
    assert A.classify("71BE01", "HL7712", {"pa_class": "Civ", "registration": "HL7712"})[0] == "private"
    assert A.classify("71BE01", "HL7712", None)[0] == "commercial"
