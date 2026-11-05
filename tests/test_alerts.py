"""Alerts (web/alerts.py): rules, matching, the engine over a real live
log, the repeat limit, catching up, a rebuilt log, pager privacy, the
preview, the webhook against a local server, and the derived fields (the
aircraft database's and the distance from a made-up receiver)."""

import json
import sqlite3
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hackrf_mcp import live_profiles
from hackrf_mcp.live_log import LiveLogWriter, Row
from hackrf_mcp.web import aircraftdb, alerts

SECRET_TEXT = "MEET AT THE DOCK 0300"
NOW = 2_000_000_000.0


def _aircraft(ts, icao, squawk="1200", callsign="UAL1"):
    return Row(
        ts=ts,
        ts_end=ts + 50,
        profile="adsb",
        kind="aircraft",
        freq_hz=1_090_000_000,
        level_db=-20.0,
        subject=icao,
        label=callsign,
        fields={"icao": icao, "callsign": callsign, "squawk": squawk, "messages": 40, "altitude_max": 3000},
        raw=f"{icao} {squawk}",
    )


def _page(ts, capcode="1234567", text=SECRET_TEXT):
    return Row(
        ts=ts,
        profile="flex",
        kind="page",
        freq_hz=931_750_000,
        subject=capcode,
        label="ALN",
        fields={"capcodes": [capcode], "type": "ALN", "message": text},
        raw=f"FLEX|ALN|{text}",
    )


@pytest.fixture
def env(tmp_path):
    live_profiles.load()
    writer = LiveLogWriter(tmp_path / "live.db", 86400 * 365 * 100, 1 << 30, 0)
    store = alerts.AlertStore(tmp_path / "web" / "alerts.db")
    return writer, store, tmp_path


def _rule(**kw):
    body = {"name": "emergency", "profiles": ["adsb"], "conditions": [{"field": "squawk", "op": "any_of", "values": "7500, 7600,7700"}]}
    body.update(kw)
    return alerts.parse_rule(body, pager_content=False)


# --- Rules and matching ------------------------------------------------------------


def test_rules_are_checked_against_what_the_profiles_log():
    live_profiles.load()
    r = _rule()
    assert r.conditions[0].values == ("7500", "7600", "7700") and r.repeat_s == 600 and r.enabled
    bad = [
        {"name": ""},
        {"profiles": ["nope"]},
        {"conditions": []},
        {"conditions": [{"field": "lat", "op": "gt", "values": ["33"]}]},  # positions aren't fields to match
        {"conditions": [{"field": "raw", "op": "matches", "values": ["*"]}]},  # nor raw text
        {"conditions": [{"field": "squawk", "op": "near", "values": ["7700"]}]},
        {"conditions": [{"field": "level_db", "op": "gt", "values": ["loud"]}]},
        {"conditions": [{"field": "squawk", "op": "equals", "values": ["1", "2"]}]},
        {"conditions": [{"field": "squawk", "op": "any_of", "values": []}]},
        {"repeat_s": -1},
    ]
    for b in bad:
        with pytest.raises(ValueError):
            _rule(**b)
    # Pager text is a field only when the operator allows pager content.
    msg = {"name": "words", "profiles": ["flex"], "conditions": [{"field": "message", "op": "matches", "values": ["*dock*"]}]}
    with pytest.raises(ValueError, match="message"):
        alerts.parse_rule(msg, pager_content=False)
    assert alerts.parse_rule(msg, pager_content=True).conditions[0].field == "message"
    assert "message" not in alerts.allowed_fields(live_profiles.PROFILES["flex"], False)
    assert "squawk" in alerts.allowed_fields(live_profiles.PROFILES["adsb"], False)


def test_matching_is_case_blind_takes_lists_patterns_and_numbers():
    live_profiles.load()
    row = {"profile": "adsb", "subject": "A1B2C3", "label": "ual1", "level_db": -12.0, "freq_hz": 1_090_000_000}
    fields = {"squawk": 7700, "callsign": "UAL1", "capcodes": ["001", "1234567"]}
    assert alerts.match(_rule(), row, fields) == ["squawk=7700"]
    assert alerts.match(_rule(), {**row, "profile": "ais"}, fields) is None  # another profile
    assert alerts.match(_rule(), row, {"squawk": "1200"}) is None
    assert alerts.match(_rule(), row, {}) is None  # no such field: no match
    two = _rule(conditions=[
        {"field": "label", "op": "matches", "values": ["UAL*"]},
        {"field": "level_db", "op": "gt", "values": ["-20"]},
    ])
    assert alerts.match(two, row, fields) == ["label=ual1", "level_db=-12.0"]
    assert alerts.match(two, {**row, "level_db": -30.0}, fields) is None  # every condition must hold
    # A field holding a list matches if any item does (built directly: no
    # profile lists capcodes among its summary fields; pages carry the
    # capcode as their subject).
    pages = alerts.Rule(None, "c", True, (), (alerts.Condition("capcodes", "any_of", ("1234567",)),), False, 0)
    assert alerts.match(pages, {**row, "profile": "flex"}, fields) == ["capcodes=001, 1234567"]


# --- The engine ---------------------------------------------------------------------


def test_the_engine_checks_new_rows_once_with_a_repeat_limit(env):
    writer, store, tmp = env
    writer.write([_aircraft(NOW - 100, "OLD1", "7700")])
    store.save(_rule())
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False)
    assert eng.poll_once(NOW) == []  # the first look only sets the watermark
    writer.write([_aircraft(NOW, "A1", "7700"), _aircraft(NOW + 60, "A1", "7700"), _aircraft(NOW + 60, "B2", "7600"),
                  _aircraft(NOW + 70, "C3", "1200")])
    hits = eng.poll_once(NOW + 80)
    # A1's second row is inside the 10-minute repeat limit; C3 doesn't match.
    assert [(h["subject"], h["detail"]) for h in hits] == [("A1", "squawk=7700"), ("B2", "squawk=7600")]
    assert eng.poll_once(NOW + 90) == []  # nothing new
    writer.write([_aircraft(NOW + 700, "A1", "7700")])
    assert [h["subject"] for h in eng.poll_once(NOW + 710)] == ["A1"]  # past the limit
    s = eng.summary()
    assert s["unacked"] == 3 and s["latest"]["subject"] == "A1" and s["webhook"] is False
    assert {(a["profile"], a["subject"]) for a in s["active"]} == {("adsb", "A1"), ("adsb", "B2")}
    # A restart catches up on rows logged while it was down, keeping the limit.
    writer.write([_aircraft(NOW + 800, "A1", "7700"), _aircraft(NOW + 2000, "D4", "7500")])
    again = alerts.AlertEngine(store, tmp / "live.db", pager_content=False)
    assert [h["subject"] for h in again.poll_once(NOW + 2010)] == ["D4"]


def test_a_rebuilt_log_resets_the_watermark(env):
    writer, store, tmp = env
    writer.write([_aircraft(NOW + i, f"X{i}") for i in range(5)])
    store.save(_rule())
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False)
    eng.poll_once(NOW)
    assert store.get_state("watermark") == "5"
    writer.close()
    for f in tmp.glob("live.db*"):
        f.unlink()
    fresh = LiveLogWriter(tmp / "live.db", 86400 * 365 * 100, 1 << 30, 0)
    fresh.write([_aircraft(NOW + 10, "Y1", "1200")])
    assert eng.poll_once(NOW + 20) == [] and store.get_state("watermark") == "1"
    fresh.write([_aircraft(NOW + 30, "Y2", "7700")])
    assert [h["subject"] for h in eng.poll_once(NOW + 40)] == ["Y2"]  # not silent forever


def test_pager_text_reaches_a_hit_only_when_the_operator_allows_it(env):
    writer, store, tmp = env
    rule = alerts.parse_rule(
        {"name": "dock", "profiles": ["flex"], "conditions": [{"field": "message", "op": "matches", "values": ["*dock*"]}]},
        pager_content=True,
    )
    store.save(rule)
    store.save(alerts.parse_rule(
        {"name": "capcode", "profiles": ["flex"], "conditions": [{"field": "subject", "op": "equals", "values": ["1234567"]}]},
        pager_content=False,
    ))
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=True)
    eng.poll_once(NOW)
    writer.write([_page(NOW)])
    hits = eng.poll_once(NOW + 1)
    assert {h["rule_name"] for h in hits} == {"dock", "capcode"}
    # Allowed: the text it matched is in the hit, as any other field is.
    assert next(h for h in hits if h["rule_name"] == "dock")["detail"] == f"message={SECRET_TEXT}"
    # Not allowed (the setting turned off later): the hit records the rule.
    off = alerts.AlertEngine(store, tmp / "live.db", pager_content=False)
    off.poll_once(NOW)
    writer.write([_page(NOW + 700)])
    hits = off.poll_once(NOW + 701)
    assert SECRET_TEXT not in json.dumps(hits)
    assert next(h for h in hits if h["rule_name"] == "dock")["detail"] == "message matches *dock*"


def test_deleting_a_rule_keeps_its_hits_and_acknowledging(env):
    writer, store, tmp = env
    r = store.save(_rule())
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False)
    eng.poll_once(NOW)
    writer.write([_aircraft(NOW, "A1", "7700"), _aircraft(NOW, "B2", "7700")])
    hits = eng.poll_once(NOW + 1)
    assert store.delete(r.id) and not store.delete(r.id)
    kept = store.hits()
    assert len(kept) == 2 and {h["rule_name"] for h in kept} == {"emergency"} and {h["rule_id"] for h in kept} == {None}
    assert store.ack([hits[0]["id"]]) == 1 and store.ack([hits[0]["id"]]) == 0
    eng.refresh()
    assert eng.summary()["unacked"] == 1
    assert store.ack(None) == 1
    eng.refresh()
    assert eng.summary()["unacked"] == 0 and eng.summary()["latest"] is None


def test_the_preview_counts_what_a_rule_would_have_matched(env):
    writer, store, tmp = env
    writer.write([_aircraft(NOW - 3600 * i, f"S{i % 7}", "7700" if i % 5 == 0 else "1200") for i in range(100)])
    writer.write([_page(NOW - 10)])
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False)
    out = eng.preview(_rule(), NOW - 7 * 86400, NOW + 1, limit=3)
    want = sum(1 for i in range(100) if i % 5 == 0 and 3600 * i <= 7 * 86400)
    assert out["matches"] == want and len(out["samples"]) == 3 and out["samples"][0]["ts"] == NOW
    assert out["scanned"] == want  # the prefilter kept only rows holding "7700"
    # No prefilter for a pattern with no literal: every row of the profile is read.
    loose = _rule(conditions=[{"field": "squawk", "op": "matches", "values": ["7?00"]}])
    assert eng.preview(loose, NOW - 7 * 86400, NOW + 1)["matches"] == want


# --- The webhook ----------------------------------------------------------------------


class _Catcher(BaseHTTPRequestHandler):
    got: list = []
    status = 204

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        _Catcher.got.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)})
        self.send_response(_Catcher.status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def catcher():
    _Catcher.got = []
    _Catcher.status = 204
    srv = HTTPServer(("127.0.0.1", 0), _Catcher)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook/SECRETPART"
    srv.shutdown()


def _hit(**kw):
    h = {"id": 1, "ts": time.time(), "rule_name": "emergency", "profile": "adsb", "kind": "aircraft", "subject": "A1",
         "label": "@everyone", "freq_hz": 1_090_000_000, "detail": "squawk=7700"}
    h.update(kw)
    return h


def test_the_webhook_posts_json_discord_and_ntfy(env, catcher):
    _, store, _ = env
    wh = alerts.Webhook(catcher, "json", store, base_url="http://192.0.2.1:8766/")
    assert wh.send(_hit()) == "sent (204)"
    body = json.loads(_Catcher.got[-1]["body"])
    assert body["rule"] == "emergency" and body["detail"] == "squawk=7700"
    assert body["link"].startswith("http://192.0.2.1:8766/log?subject=A1")
    alerts.Webhook(catcher, "discord", store).send(_hit())
    d = json.loads(_Catcher.got[-1]["body"])
    assert d["allowed_mentions"] == {"parse": []} and "@everyone" in d["content"]  # shown, never a ping
    alerts.Webhook(catcher, "ntfy", store).send(_hit(rule_name="line\nbreak"))
    assert _Catcher.got[-1]["headers"]["Title"] == "hackrf: line break"
    assert b"squawk=7700" in _Catcher.got[-1]["body"]
    _Catcher.status = 404
    failed = alerts.Webhook(catcher, "json", store).send(_hit())
    assert failed == "failed: HTTP 404" and "SECRET" not in failed
    with pytest.raises(ValueError):
        alerts.Webhook("ftp://x/y", "json", store)


def test_the_engine_posts_new_hits_and_not_old_ones(env, catcher):
    writer, store, tmp = env
    store.save(_rule(webhook=True))
    wh = alerts.Webhook(catcher, "json", store)
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False, webhook=wh)
    wh.start()  # only the sender: the test polls by itself
    try:
        eng.poll_once()
        now = time.time()
        writer.write([_aircraft(now - 2 * alerts.WEBHOOK_MAX_AGE_S, "OLD", "7700"), _aircraft(now, "NEW", "7700")])
        hits = eng.poll_once(now)
        assert [h["subject"] for h in hits] == ["OLD", "NEW"]
        deadline = time.time() + 10
        while time.time() < deadline and not any(h["webhook"] == "sent (204)" for h in store.hits()):
            time.sleep(0.05)
        by = {h["subject"]: h["webhook"] for h in store.hits()}
        assert by["NEW"] == "sent (204)" and by["OLD"].startswith("not sent")
        assert len(_Catcher.got) == 1 and json.loads(_Catcher.got[0]["body"])["subject"] == "NEW"
    finally:
        wh.q.put(None)


def test_a_failing_look_is_reported_and_the_next_one_tries_again(env, monkeypatch):
    writer, store, tmp = env
    missing = alerts.AlertEngine(store, tmp / "no-such.db", pager_content=False)
    missing.tick()
    assert missing.summary()["error"] == "the live log isn't there yet"
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False)
    writer.write([_aircraft(NOW, "A1")])
    calls = []

    def boom(now=None):
        calls.append(1)
        raise KeyError("surprise")

    monkeypatch.setattr(eng, "poll_once", boom)
    eng.tick()
    assert eng.summary()["error"] == "checking rows: KeyError: 'surprise'"
    monkeypatch.undo()
    eng.tick()
    assert eng.summary()["error"] is None and calls == [1]


# --- Derived fields: the aircraft database and the distance from the receiver ---------


# Made up, like the owners below: nowhere near any real station.
RECEIVER = (10.0, 20.0)


def _planes(tmp, rows=()):
    """An AircraftDB over a small made-up aircraft.db (none at all if no rows)."""
    db = aircraftdb.AircraftDB(tmp / "web" / "aircraft.db")
    if rows:
        db.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db.path)) as conn:
            conn.executescript(aircraftdb.SCHEMA)
            conn.executemany(
                "INSERT INTO aircraft (icao, faa, registration, manufacturer, model, type_aircraft, registrant_type, owner, "
                "pa_class, pa_operator, pa_category, pa_tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute("INSERT INTO meta VALUES ('built_at', '1000')")
            conn.commit()
    return db


PLANES = [
    ("A1A1A1", 1, "N911SD", "AIRBUS HELICOPTERS", "AS350B3", "6", "5", "EXAMPLE COUNTY SHERIFFS OFFICE", None, None, None, None),
    ("A0B1C2", 1, "N12345", "CESSNA", "172S", "4", "1", "JANE EXAMPLE", None, None, None, None),
    ("C0FFEE", 0, "C-POLI", None, None, None, None, None, "Pol", "Example Police Service", "Police Forces", "Eye|Sky"),
]


def _placed(ts, icao, lat, lon, callsign="TEST1"):
    row = _aircraft(ts, icao, callsign=callsign)
    return Row(**{**row.__dict__, "fields": {**row.fields, "lat": lat, "lon": lon}})


def test_derived_fields_are_offered_only_where_they_apply(tmp_path):
    live_profiles.load()
    P = live_profiles.PROFILES
    both = alerts.Derived(_planes(tmp_path), RECEIVER)
    adsb = alerts.allowed_fields(P["adsb"], False, both)
    assert "aircraft_type" in adsb and "aircraft_owner" in adsb and "distance_km" in adsb
    assert "distance_km" in alerts.allowed_fields(P["ais"], False, both)  # ships have positions
    assert "aircraft_type" not in alerts.allowed_fields(P["ais"], False, both)  # but aren't aircraft
    flex = alerts.allowed_fields(P["flex"], False, both)
    assert not set(flex) & set(alerts.DERIVED_FIELDS)
    assert not set(alerts.allowed_fields(P["adsb"], False)) & set(alerts.DERIVED_FIELDS)  # nothing to work them out
    assert "distance_km" not in alerts.allowed_fields(P["adsb"], False, alerts.Derived(_planes(tmp_path)))
    assert [c["value"] for c in both.choices()["aircraft_type"]] == [c[0] for c in aircraftdb.CLASSES]

    def rule(cond, profiles=("adsb",), derived=both):
        body = {"name": "r", "profiles": list(profiles), "conditions": [cond]}
        return alerts.parse_rule(body, pager_content=False, derived=derived)

    # A class's label, short tag or name, in any case, is stored as its name.
    r = rule({"field": "aircraft_type", "op": "any_of", "values": "Law enforcement, MIL, government"})
    assert r.conditions[0].values == ("law_enforcement", "military", "government")
    assert rule({"field": "aircraft_type", "op": "matches", "values": "*gov*"}).conditions[0].values == ("*gov*",)
    for bad, derived, profiles in [
        ({"field": "aircraft_type", "op": "any_of", "values": "spaceship"}, both, ("adsb",)),
        ({"field": "aircraft_type", "op": "any_of", "values": "military"}, both, ("flex",)),
        ({"field": "aircraft_type", "op": "any_of", "values": "military"}, alerts.NO_DERIVED, ("adsb",)),
        ({"field": "distance_km", "op": "equals", "values": "3"}, both, ("adsb",)),
        ({"field": "distance_km", "op": "lt", "values": "3"}, alerts.Derived(_planes(tmp_path)), ("adsb",)),
    ]:
        with pytest.raises(ValueError):
            rule(bad, profiles, derived)


def test_aircraft_fields_match_in_the_preview_and_the_engine(env):
    writer, store, tmp = env
    derived = alerts.Derived(_planes(tmp, PLANES), RECEIVER)
    writer.write([
        _aircraft(NOW - 300, "ADF7C9", callsign="RCH123"),  # military by its address alone
        _aircraft(NOW - 290, "A1A1A1", callsign="N911SD"),  # a sheriff's helicopter, from the FAA data
        _aircraft(NOW - 280, "C0FFEE", callsign="POL1"),  # plane-alert-db's Pol
        _aircraft(NOW - 270, "A0B1C2", callsign="N12345"),  # private
        _aircraft(NOW - 260, "A4A4A4", callsign="UAL1"),  # commercial by its callsign
        _page(NOW - 250),
    ])  # fmt: skip
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False, derived=derived)
    body = {"name": "notable", "profiles": [], "conditions": [{"field": "aircraft_type", "op": "any_of", "values": "military, law enforcement"}]}
    notable = alerts.parse_rule(body, pager_content=False, derived=derived)
    out = eng.preview(notable, NOW - 3600, NOW)
    # Nothing in a row's JSON says "military": the prefilter must not drop them.
    assert out["matches"] == 3 and out["scanned"] == 5  # the page isn't read: flex has no aircraft
    assert [s["detail"] for s in out["samples"]] == [
        "aircraft_type=law_enforcement", "aircraft_type=law_enforcement", "aircraft_type=military"
    ]  # fmt: skip
    kind = {"name": "helicopters", "profiles": ["adsb"], "conditions": [
        {"field": "aircraft_kind", "op": "equals", "values": ["Rotorcraft"]},
        {"field": "aircraft_owner", "op": "matches", "values": ["*sheriff*"]},
    ]}  # fmt: skip
    k = eng.preview(alerts.parse_rule(kind, False, derived=derived), NOW - 3600, NOW)
    assert k["matches"] == 1 and k["samples"][0]["detail"] == "aircraft_kind=Rotorcraft · aircraft_owner=EXAMPLE COUNTY SHERIFFS OFFICE"
    tags = {"name": "t", "profiles": ["adsb"], "conditions": [{"field": "aircraft_tags", "op": "any_of", "values": ["sky"]}]}
    assert eng.preview(alerts.parse_rule(tags, False, derived=derived), NOW - 3600, NOW)["matches"] == 1
    # Live: the engine looks the new rows up and records the class it matched.
    store.save(notable)
    eng.reload()
    assert eng.poll_once(NOW) == []
    writer.write([_aircraft(NOW + 10, "AE0001", callsign="RCH9"), _aircraft(NOW + 11, "A0B1C2", callsign="N12345")])
    hits = eng.poll_once(NOW + 20)
    assert [(h["subject"], h["detail"]) for h in hits] == [("AE0001", "aircraft_type=military")]
    # Without the database file the address and callsign rules still work.
    bare = alerts.AlertEngine(store, tmp / "live.db", pager_content=False, derived=alerts.Derived(_planes(tmp / "none")))
    assert bare.preview(notable, NOW - 3600, NOW + 60)["matches"] == 2  # the two ADF7C8-AFFFFF addresses


def test_distance_shows_in_a_hit_and_only_the_condition_reaches_the_webhook(env, catcher):
    writer, store, tmp = env
    derived = alerts.Derived(_planes(tmp, PLANES), RECEIVER)
    wh = alerts.Webhook(catcher, "json", store)
    eng = alerts.AlertEngine(store, tmp / "live.db", pager_content=False, webhook=wh, derived=derived)
    near = {"name": "overhead", "profiles": ["adsb"], "webhook": True,
            "conditions": [{"field": "distance_km", "op": "lt", "values": ["5"]}]}  # fmt: skip
    rule = alerts.parse_rule(near, False, derived=derived)
    assert rule.to_json()["text"] == "distance_km is less than 5"
    writer.write([_placed(NOW - 100, "A1A1A1", 10.01, 20.0), _placed(NOW - 90, "A0B1C2", 10.1, 20.0), _aircraft(NOW - 80, "A4A4A4")])
    out = eng.preview(rule, NOW - 3600, NOW)
    assert out["matches"] == 1 and out["samples"][0]["detail"] == "distance_km=1.1"  # 0.01 degrees of latitude
    store.save(rule)
    eng.reload()
    eng.poll_once(NOW)
    writer.write([_placed(NOW + 5, "A1A1A1", 10.0, 20.02, callsign="N911SD")])
    hits = eng.poll_once(NOW + 10)
    assert [h["detail"] for h in hits] == ["distance_km=2.2"]
    assert "_webhook_detail" not in hits[0] and "_post" not in hits[0]
    eng.webhook.start()
    deadline = time.time() + 5
    while not _Catcher.got and time.time() < deadline:
        time.sleep(0.05)
    sent = json.loads(_Catcher.got[0]["body"])
    assert sent["detail"] == "distance_km is less than 5"
    raw = _Catcher.got[0]["body"].decode()
    assert "2.2" not in raw and "10.0" not in raw and "20.0" not in raw and "20.02" not in raw
    # What the aircraft is goes with it (the owner too: the operator allows it).
    assert sent["aircraft"]["class"] == "law_enforcement" and sent["aircraft"]["owner"] == "EXAMPLE COUNTY SHERIFFS OFFICE"
    assert "why" not in sent["aircraft"]
    assert store.hits()[0]["detail"] == "distance_km=2.2"  # the UI keeps the number


def test_the_webhook_text_says_what_the_aircraft_is(env, catcher):
    _writer, store, _tmp = env
    info = aircraftdb.describe("C0FFEE", "POL1", {"pa_class": "Pol", "pa_operator": "Example Police Service",
                                                   "registration": "C-POLI", "pa_type": "AS350"})  # fmt: skip
    alerts.Webhook(catcher, "discord", store).send(_hit(subject="C0FFEE", label="POL1", aircraft=info))
    content = json.loads(_Catcher.got[-1]["body"])["content"]
    assert "C0FFEE (POL1) · Law enforcement, C-POLI, AS350, Example Police Service · squawk=7700" in content
    assert alerts.aircraft_text(None) == ""
