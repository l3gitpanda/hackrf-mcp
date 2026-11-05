"""hackrf-web (web UI milestone 4): sign-in, the gate, the API and the
live websocket, with a stand-in control socket and no device."""

import base64
import gzip
import hashlib
import json
import struct

import numpy as np
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hackrf_mcp import decoders, live_client, live_profiles, settings
from hackrf_mcp.live_log import LiveLogWriter, Position, Row
from hackrf_mcp.live_spectrum import BINS, TIER_MINUTE, TIER_SECOND, SpectrumWriter, quantise
from hackrf_mcp.protocols.multimon import CONTENT_NOT_LOGGED
from hackrf_mcp.web import app as web_app
from hackrf_mcp.web.__main__ import accepted_digests
from hackrf_mcp.web.sessions import COOKIE, Sessions, load_secret

TOKEN = "correct-horse-battery-staple-42"
DIGEST = hashlib.sha256(TOKEN.encode()).digest()
BEARER = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = {"Origin": "http://testserver"}
SECRET_TEXT = "MEET AT THE DOCK 0300"


class Control:
    """Stands in for the live service's control socket."""

    def __init__(self):
        self.requests = []
        self.fail: Exception | None = None

    def __call__(self, req, timeout=20.0):
        self.requests.append(req)
        if self.fail is not None:
            raise self.fail
        return {"ok": True, "state": "running", "profile": "adsb", "echo": req}


def _meta(**kw):
    return {
        "profile": "adsb",
        "center_hz": 1_090_000_000,
        "sample_rate_hz": 2_400_000,
        "bins": BINS,
        "db_min": -130.0,
        "db_step": 0.5,
        "lna_gain_db": 32,
        "vga_gain_db": 40,
        "amp": False,
        "duty": 1.0,
        **kw,
    }


@pytest.fixture
def env(tmp_path):
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    w = LiveLogWriter(live.db_path, 86400 * 365 * 100, 1 << 30, 0)
    w.write(
        [
            Row(ts=1000.0, ts_end=1030.0, profile="adsb", kind="aircraft", subject="A1B2C3", label="UAL1",
                fields={"icao": "A1B2C3", "callsign": "UAL1", "lat": 33.9, "lon": -118.4}, raw="A1B2C3 UAL1"),
            # A page logged while pager content was on.
            Row(ts=1010.0, profile="flex", kind="page", subject="1234567", label="ALN", freq_hz=931_750_000,
                fields={"capcode": "001234567", "type": "ALN", "message": SECRET_TEXT},
                raw="FLEX|1600/2|01.002|001234567|LS|5|ALN|" + SECRET_TEXT),
        ]
    )
    w.write_positions([Position(1005.0, "adsb", "A1B2C3", 33.9, -118.4, 30000.0, 250.0, 90.0)])
    w.event("start", "adsb", "adsb: tuned 1090.0000 MHz", ts=999.0)
    sw = SpectrumWriter(live.spectrum_db_path, 1e12, 1e12, 1 << 30, 0)
    m1 = np.full(BINS, -80.0, dtype=np.float32)
    m2 = np.full(BINS, -70.0, dtype=np.float32)
    sw.apply(
        [
            ("segment", 1, {**_meta(), "ts_start": 1000.0}),
            ("row", 1, TIER_SECOND, 1000.0, 10, quantise(m1), quantise(m1 + 10)),
            ("row", 1, TIER_SECOND, 1001.0, 10, quantise(m2), quantise(m2 + 20)),
            ("row", 1, TIER_MINUTE, 1000.0, 20, quantise(m2), quantise(m2 + 20)),
            ("end", 1, 1002.0),
        ]
    )
    cg = tmp_path / "cgroup" / "hackrf-live.service"
    cg.mkdir(parents=True)
    (cg / "memory.current").write_text("83107840\n")
    (cg / "memory.max").write_text("536870912\n")
    (cg / "cpu.stat").write_text("usage_usec 19403696576\nuser_usec 1\n")
    control = Control()
    web = settings.WebSettings()
    gate = web_app.create_app(
        live, web, [DIGEST], b"k" * 32, control=control, cgroup_root=tmp_path / "cgroup", feed=False,
        own_stats=lambda: None,
    )
    return live, gate, control


def _signed_in(gate) -> TestClient:
    c = TestClient(gate, follow_redirects=False)
    r = c.post("/auth/login", data={"token": TOKEN})
    assert r.status_code == 303 and r.headers["location"] == "/"
    return c


# --- The gate and sign-in --------------------------------------------------------------


def test_everything_but_sign_in_needs_a_token(env):
    _, gate, _ = env
    c = TestClient(gate, follow_redirects=False)
    assert c.get("/api/registry").status_code == 401
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.get("/login")
    assert r.status_code == 200 and "Content-Security-Policy" in r.headers
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert c.get("/static/login.css").status_code == 200
    assert c.get("/static/login.css").headers["cache-control"] == "no-cache"
    assert c.get("/api/registry", headers=BEARER).headers["cache-control"] == "no-store"
    assert c.get("/static/index.html").status_code == 303  # the rest of /static needs sign-in
    assert c.get("/api/registry", headers={"Authorization": "Bearer wrong-token-wrong-token"}).status_code == 401
    assert c.get("/api/registry", headers=BEARER).status_code == 200


def test_signing_in_sets_a_strict_http_only_cookie(env, monkeypatch):
    _, gate, _ = env

    async def no_wait(s):
        pass

    monkeypatch.setattr(web_app.asyncio, "sleep", no_wait)
    c = TestClient(gate, follow_redirects=False)
    r = c.post("/auth/login", data={"token": "not-the-token-at-all"})
    assert r.status_code == 303 and r.headers["location"] == "/login?failed=1"
    assert COOKIE not in c.cookies
    r = c.post("/auth/login", data={"token": TOKEN})
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    assert c.get("/api/status").status_code == 200
    assert c.get("/").status_code == 200
    r = c.post("/auth/logout", headers=ORIGIN)
    assert r.status_code == 303 and c.get("/api/status").status_code == 401


def test_sessions_end_when_their_token_is_no_longer_accepted(tmp_path):
    key = load_secret(tmp_path / "web")
    assert load_secret(tmp_path / "web") == key and (tmp_path / "web" / "session-key").stat().st_mode & 0o777 == 0o600
    other = hashlib.sha256(b"another-token-another-token").digest()
    cookie = Sessions(key, 30, [DIGEST, other]).issue(DIGEST, now=0)
    assert Sessions(key, 30, [DIGEST]).valid(cookie, now=10)
    assert not Sessions(key, 30, [other]).valid(cookie, now=10)  # its token was removed
    assert not Sessions(key, 30, [DIGEST]).valid(cookie, now=31 * 86400)  # expired
    assert not Sessions(b"x" * 32, 30, [DIGEST]).valid(cookie, now=10)  # another key
    body, sig = cookie.split(".")
    assert not Sessions(key, 30, [DIGEST]).valid(body + "." + sig[:-2] + "AA", now=10)


def test_changes_must_come_from_the_page_itself(env):
    _, gate, control = env
    c = _signed_in(gate)
    body = {"profile": "adsb"}
    assert c.post("/api/live/start", json=body).status_code == 403  # no Origin
    assert c.post("/api/live/start", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert control.requests == []
    assert c.post("/api/live/start", json=body, headers=ORIGIN).status_code == 200
    # A script with the token (no cookie, no Origin) may.
    assert TestClient(gate).post("/api/live/stop", headers=BEARER).status_code == 200
    assert [r["cmd"] for r in control.requests] == ["start", "stop"]


# --- The API -----------------------------------------------------------------------------


def test_registry_comes_from_the_registries(env):
    _, gate, _ = env
    reg = TestClient(gate).get("/api/registry", headers=BEARER).json()
    live_profiles.load()
    decoders.load()
    assert [p["name"] for p in reg["profiles"]] == list(live_profiles.PROFILES)
    assert [d["name"] for d in reg["decoders"]] == list(decoders.DECODERS)
    by = {p["name"]: p for p in reg["profiles"]}
    assert {"uat", "rds", "dtmf"} <= {d["name"] for d in reg["decoders"]}
    adsb_map = next(v for v in by["adsb"]["views"] if v["name"] == "map")
    assert adsb_map["roles"]["lat"] == "lat" and adsb_map["roles"]["label"] == "callsign"
    assert [v["name"] for v in by["acars"]["views"]] == ["table", "timeline"]  # the default views
    assert by["flex"]["redacted"] and not by["adsb"]["redacted"]
    assert by["ais"]["spectrum"] and by["ais"]["sample_rate_hz"] == 2_000_000
    assert reg["pager_content"] is False


def test_log_search_hides_pager_text_unless_the_operator_allows_it(env, tmp_path):
    live, gate, _ = env
    rows = TestClient(gate).get("/api/log", params={"since": "0"}, headers=BEARER).json()["rows"]
    assert [r["kind"] for r in rows] == ["page", "aircraft"]
    page = rows[0]
    assert "message" not in page["fields"] and SECRET_TEXT not in json.dumps(rows)
    assert page["raw"].endswith(CONTENT_NOT_LOGGED) and page["raw"].startswith("FLEX|")
    assert rows[1]["fields"]["callsign"] == "UAL1"
    s = TestClient(gate).get("/api/summary", params={"profile": "flex", "since": "0"}, headers=BEARER).json()
    assert s["subjects"][0]["subject"] == "1234567" and SECRET_TEXT not in json.dumps(s)
    allowed = web_app.create_app(
        settings.LiveSettings(live_dir=live.live_dir, pager_content=True),
        settings.WebSettings(),
        [DIGEST],
        b"k" * 32,
        feed=False,
    )
    rows = TestClient(allowed).get("/api/log", params={"since": "0", "profile": "flex"}, headers=BEARER).json()["rows"]
    assert rows[0]["fields"]["message"] == SECRET_TEXT


def test_long_ranges_of_positions_are_thinned_not_cut_short(env, monkeypatch):
    live, gate, _ = env
    w = LiveLogWriter(live.db_path, 86400 * 365 * 100, 1 << 30, 0)
    w.write_positions([Position(2000.0 + 10 * i, "adsb", "C3", 34.0, -118.0 + i / 1000, None, None, None) for i in range(60)])
    c = TestClient(gate)
    full = c.get("/api/positions", params={"since": "1990", "subject": "C3"}, headers=BEARER).json()
    assert len(full["positions"]) == 60 and full["step"] is None and not full["truncated"]
    thin = c.get("/api/positions", params={"since": "1990", "subject": "C3", "limit": "21"}, headers=BEARER).json()
    # 2000-2590 s, a fix every 10 s, at most 21 wanted: one per 30 s, to the end of the range.
    assert thin["step"] == 30 and not thin["truncated"]
    assert len(thin["positions"]) == 21 and thin["positions"][-1]["ts"] == 2590.0
    monkeypatch.setattr(web_app, "POSITION_STEPS", (20,))
    cut = c.get("/api/positions", params={"since": "1990", "subject": "C3", "limit": "21"}, headers=BEARER).json()
    assert cut["step"] == 20 and cut["truncated"] and len(cut["positions"]) == 21


def test_events_positions_and_bad_parameters(env):
    _, gate, _ = env
    c = TestClient(gate)
    ev = c.get("/api/events", params={"since": "0"}, headers=BEARER).json()["events"]
    assert ev[0]["event"] == "start" and ev[0]["profile"] == "adsb"
    pos = c.get("/api/positions", params={"since": "0", "profile": "adsb"}, headers=BEARER).json()["positions"]
    assert [(p["subject"], p["alt"]) for p in pos] == [("A1B2C3", 30000.0)]
    assert c.get("/api/log", params={"since": "yesterday-ish"}, headers=BEARER).status_code == 400
    assert c.get("/api/summary", params={"profile": "nope"}, headers=BEARER).status_code == 400
    assert c.get("/api/spectrum", headers=BEARER).status_code == 400
    assert c.get("/api/spectrum", params={"segment": "99"}, headers=BEARER).status_code == 404


def test_log_paging_histogram_coverage_and_the_log_page(env):
    _, gate, _ = env
    c = TestClient(gate)
    first = c.get("/api/log", params={"since": "0", "limit": 1}, headers=BEARER).json()
    assert len(first["rows"]) == 1 and first["more"] is True
    rest = c.get("/api/log", params={"since": "0", "limit": 5, "before_id": first["rows"][0]["id"]}, headers=BEARER).json()
    assert len(rest["rows"]) == 1 and rest["more"] is False
    assert rest["rows"][0]["id"] != first["rows"][0]["id"]
    only = c.get("/api/log", params={"since": "0", "profiles": "adsb"}, headers=BEARER).json()["rows"]
    assert {r["profile"] for r in only} == {"adsb"}
    hist = c.get("/api/log/histogram", params={"since": "900", "until": "1100", "buckets": 2}, headers=BEARER).json()
    assert hist["counts"] == [{}, {"adsb": 1, "flex": 1}]
    assert c.get("/api/log/histogram", params={"until": "1100"}, headers=BEARER).status_code == 400
    cov = c.get("/api/coverage", params={"since": "990", "until": "1100"}, headers=BEARER).json()["coverage"]
    assert cov[-1] == {"state": "running", "profile": "adsb", "from": 999.0, "to": 1100.0}
    r = TestClient(gate, follow_redirects=False).get("/log")
    assert r.status_code == 303  # needs sign-in
    page = c.get("/log", headers=BEARER)
    assert page.status_code == 200 and "log.js" in page.text and page.headers["cache-control"] == "no-cache"


def test_the_history_page_and_links_into_it(env):
    _, gate, _ = env
    assert TestClient(gate, follow_redirects=False).get("/history").status_code == 303  # needs sign-in
    c = TestClient(gate)
    page = c.get("/history", headers=BEARER)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert "js/history.js" in page.text and 'aria-current="page">History' in page.text
    # Every screen links to it now, and to Bands.
    for path in ("/", "/log", "/map"):
        assert '<a href="/history">History</a>' in c.get(path, headers=BEARER).text
    for path in ("/", "/log", "/map", "/history"):
        assert '<a href="/bands">Bands</a>' in c.get(path, headers=BEARER).text
    bands = c.get("/bands", headers=BEARER)
    assert bands.status_code == 200 and "js/bands.js" in bands.text and bands.headers["cache-control"] == "no-cache"
    assert TestClient(gate, follow_redirects=False).get("/bands").status_code == 303


def pmtiles_header(minz=0, maxz=14, bounds=(-121.7, 31.6, -116.4, 35.1), meta=None) -> bytes:
    """A PMTiles v3 header and gzipped metadata, enough for basemap.info()."""
    raw = gzip.compress(json.dumps(meta or {}).encode())
    h = bytearray(127)
    h[:8] = b"PMTiles\x03"
    struct.pack_into("<QQ", h, 24, 127, len(raw))
    h[97] = 2  # gzip
    h[100], h[101] = minz, maxz
    struct.pack_into("<iiii", h, 102, *(round(v * 1e7) for v in bounds))
    return bytes(h) + raw


def test_the_map_page_basemap_and_receiver(env, tmp_path):
    live, gate, _ = env
    c = TestClient(gate)
    page = c.get("/map", headers=BEARER).text
    assert "leaflet.js" in page and "protomaps-leaflet.js" in page
    assert c.get("/static/vendor/leaflet/leaflet.js", headers=BEARER).status_code == 200
    assert c.get("/static/vendor/protomaps-leaflet/protomaps-leaflet.js", headers=BEARER).status_code == 200
    # No base map until the operator puts one in the web state directory.
    r = c.get("/api/basemap.pmtiles", headers=BEARER)
    assert r.status_code == 404 and "basemap.pmtiles" in r.json()["error"]
    assert c.get("/api/registry", headers=BEARER).json()["basemap"] is None
    state = settings.WebSettings().state_path(live)
    state.mkdir(parents=True, exist_ok=True)
    body = pmtiles_header(meta={"hackrf_full_zoom": 11, "hackrf_core_bounds": [-119.0, 32.9, -117.3, 34.25]}) + bytes(range(256)) * 64
    (state / "basemap.pmtiles").write_bytes(body)
    # The browser reads it in byte ranges.
    r = c.get("/api/basemap.pmtiles", headers={**BEARER, "Range": "bytes=127-1126"})
    assert r.status_code == 206 and r.content == body[127:1127]
    assert r.headers["content-range"] == f"bytes 127-1126/{len(body)}"
    assert TestClient(gate).get("/api/basemap.pmtiles").status_code in (401, 303)
    reg = c.get("/api/registry", headers=BEARER).json()
    assert reg["receiver"] is None
    assert reg["basemap"] == {
        "minzoom": 0,
        "maxzoom": 14,
        "full_zoom": 11,
        "bounds": [-121.7, 31.6, -116.4, 35.1],
        "core": [-119.0, 32.9, -117.3, 34.25],
    }
    # Not a PMTiles archive: no base map rather than an error.
    (state / "basemap.pmtiles").write_bytes(b"not a map")
    assert c.get("/api/registry", headers=BEARER).json()["basemap"] is None
    placed = web_app.create_app(
        settings.LiveSettings(live_dir=live.live_dir, receiver=(33.9, -118.4)), settings.WebSettings(), [DIGEST], b"k" * 32, feed=False
    )
    assert TestClient(placed).get("/api/registry", headers=BEARER).json()["receiver"] == [33.9, -118.4]


def test_spectrum_segments_and_a_downsampled_waterfall(env):
    _, gate, _ = env
    c = TestClient(gate)
    [seg] = c.get("/api/spectrum/segments", params={"since": "0"}, headers=BEARER).json()["segments"]
    assert seg["profile"] == "adsb" and seg["ts_end"] == 1002.0
    out = c.get("/api/spectrum", params={"segment": seg["id"], "buckets": 1}, headers=BEARER).json()
    assert out["tier"] == TIER_SECOND and out["rows"] == 2 and len(out["ts"]) == 1
    mean = np.frombuffer(base64.b64decode(out["mean"]), np.uint8).reshape(1, BINS)
    peak = np.frombuffer(base64.b64decode(out["peak"]), np.uint8).reshape(1, BINS)
    # The mean of -80 and -70 dBFS in power is -72.6 dBFS; the peak is the higher one.
    assert -130 + 0.5 * mean[0, 0] == pytest.approx(10 * np.log10((1e-8 + 1e-7) / 2), abs=0.3)
    assert -130 + 0.5 * peak[0, 0] == pytest.approx(-50.0, abs=0.3)
    two = c.get("/api/spectrum", params={"segment": seg["id"], "buckets": 2}, headers=BEARER).json()
    assert len(two["ts"]) == 2
    minute = c.get("/api/spectrum", params={"segment": seg["id"], "tier": "minute"}, headers=BEARER).json()
    assert minute["tier"] == TIER_MINUTE and minute["rows"] == 1


def test_status_has_the_service_and_its_cgroup(env, monkeypatch):
    _, gate, control = env
    st = TestClient(gate).get("/api/status", headers=BEARER).json()
    assert st["live"]["state"] == "running" and control.requests == [{"cmd": "status"}]
    assert st["service"] == {"memory_bytes": 83107840, "memory_max": 536870912, "cpu_usec": 19403696576, "cpu_limit_pct": None}
    assert st["web"] is None and st["feed"]["connected"] is False
    TestClient(gate).get("/api/status", headers=BEARER)
    assert len(control.requests) == 1  # a second ask within 2 s shares the answer
    monkeypatch.setattr(web_app, "STATUS_CACHE_S", 0.0)
    control.fail = live_client.Unavailable("the live decoder service is not running")
    st = TestClient(gate).get("/api/status", headers=BEARER).json()
    assert st["live"]["state"] == "unreachable"


def test_start_builds_the_same_requests_as_the_mcp_tool(env):
    _, gate, control = env
    c = TestClient(gate)
    r = c.post("/api/live/start", headers=BEARER, json={"profile": "flex", "frequencies_hz": [931750000], "vga_gain_db": 30})
    assert r.status_code == 200
    assert control.requests[-1] == {
        "cmd": "start", "profile": "flex", "freqs_hz": [931750000], "lna_gain_db": None, "vga_gain_db": 30, "amp": False
    }
    c.post("/api/live/start", headers=BEARER, json={"schedule": [{"profile": "adsb", "minutes": 10}, {"profile": "ais", "minutes": 10}]})
    assert [e["profile"] for e in control.requests[-1]["schedule"]] == ["adsb", "ais"]
    assert control.requests[-1]["schedule"][0]["minutes"] == 10.0
    assert c.post("/api/live/start", headers=BEARER, json={}).status_code == 400
    assert c.post("/api/live/start", headers=BEARER, json={"profile": "adsb", "schedule": [{"profile": "ais", "minutes": 1}]}).status_code == 400
    control.fail = live_client.LiveError("flex needs frequencies_hz")
    r = c.post("/api/live/start", headers=BEARER, json={"profile": "flex"})
    assert r.status_code == 400 and "frequencies_hz" in r.json()["error"]
    control.fail = live_client.Unavailable("not running")
    assert c.post("/api/live/stop", headers=BEARER).status_code == 503


def test_a_watch_is_requested_like_a_start(env):
    _, gate, control = env
    c = TestClient(gate)
    reg = c.get("/api/registry", headers=BEARER).json()["watch"]
    assert reg["rates_hz"][-1] == 10_000_000 and reg["default_minutes"] == 15 and reg["max_minutes"] == 60
    r = c.post("/api/live/watch", headers=BEARER, json={"center_hz": 98_000_000, "sample_rate_hz": 10_000_000, "minutes": 5})
    assert r.status_code == 200
    assert control.requests[-1] == {
        "cmd": "watch", "center_hz": 98_000_000, "sample_rate_hz": 10_000_000,
        "lna_gain_db": None, "vga_gain_db": None, "amp": False, "minutes": 5.0,
    }
    # A retune leaves the time limit to the service.
    c.post("/api/live/watch", headers=BEARER, json={"center_hz": 103_000_000})
    assert "minutes" not in control.requests[-1] and control.requests[-1]["sample_rate_hz"] == 10_000_000
    assert c.post("/api/live/watch/stop", headers=BEARER).status_code == 200
    assert control.requests[-1] == {"cmd": "watch-stop"}
    assert c.post("/api/live/watch", headers=BEARER, json={}).status_code == 400
    # Changes need the page's own origin, as start does.
    assert TestClient(gate).post("/api/live/watch", json={"center_hz": 98_000_000}).status_code in (401, 403)
    control.fail = live_client.LiveError("outside what the HackRF receives")
    r = c.post("/api/live/watch", headers=BEARER, json={"center_hz": 2_000_000})
    assert r.status_code == 400 and "outside" in r.json()["error"]


# --- The websocket ---------------------------------------------------------------------------


def test_the_websocket_relays_the_feed_with_pager_text_removed(env):
    _, gate, _ = env
    with _signed_in(gate) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/live", headers={"Origin": "http://evil.example"}) as ws:
                ws.receive_text()
        with c.websocket_connect("/ws/live", headers=ORIGIN) as ws:
            assert json.loads(ws.receive_text()) == {"type": "hello", "pager_content": False}
            first = json.loads(ws.receive_text())
            assert first["type"] == "status" and first["live"]["state"] == "running"
            page = {"type": "row", "ts": 5.0, "profile": "flex", "kind": "page", "subject": "1",
                    "fields": {"message": SECRET_TEXT}, "raw": "FLEX|hdr|" + SECRET_TEXT}
            c.portal.call(gate.hub.dispatch, json.dumps(page, separators=(",", ":")))
            c.portal.call(gate.hub.dispatch, '{"type":"fft","ts":6.0,"mean":"AA=="}')
            got = [json.loads(ws.receive_text()) for _ in range(2)]
            assert got[0]["type"] == "row" and SECRET_TEXT not in json.dumps(got[0])
            assert got[0]["raw"] == "FLEX|hdr|" + CONTENT_NOT_LOGGED
            assert got[1]["type"] == "fft"
    with pytest.raises(WebSocketDisconnect):
        with TestClient(gate).websocket_connect("/ws/live", headers=ORIGIN) as ws:  # not signed in
            ws.receive_text()


# --- Settings ---------------------------------------------------------------------------------------


def test_web_settings_and_tokens(tmp_path):
    token_file = tmp_path / "web-token"
    token_file.write_text(TOKEN + "\n")
    w = settings.WebSettings.from_env(
        {
            "HACKRF_WEB_HOST": "0.0.0.0",
            "HACKRF_WEB_PORT": "8766",
            "HACKRF_WEB_TOKEN_FILE": str(token_file),
            "HACKRF_WEB_TOKEN_SHA256": hashlib.sha256(b"x" * 20).hexdigest(),
            "HACKRF_WEB_SESSION_DAYS": "7",
        }
    )
    assert (w.host, w.port, w.session_days) == ("0.0.0.0", 8766, 7.0)
    assert accepted_digests(w) == [DIGEST, hashlib.sha256(b"x" * 20).digest()]
    assert w.state_path(settings.LiveSettings(live_dir=tmp_path / "live")) == tmp_path / "web"
    token_file.write_text("short")
    with pytest.raises(ValueError):
        accepted_digests(w)
    with pytest.raises(ValueError):
        accepted_digests(settings.WebSettings(token_sha256=("nothex",)))
    with pytest.raises(ValueError):
        settings.WebSettings(port=0)
    with pytest.raises(ValueError):
        web_app.create_app(settings.LiveSettings(live_dir=tmp_path), settings.WebSettings(), [], b"k" * 32, feed=False)


def test_summaries_for_a_range_and_the_summaries_page(env):
    live, gate, _ = env
    c = TestClient(gate)
    r = c.get("/api/summaries", params={"since": "900", "until": "1100", "buckets": 4}, headers=BEARER)
    assert r.status_code == 200
    out = r.json()
    assert out["bucket_s"] == 50.0 and set(out["profiles"]) == set(live_profiles.PROFILES)
    adsb = out["profiles"]["adsb"]
    assert adsb["rows"] == 1 and adsb["messages"] == 1 and adsb["subjects"] == 1 and adsb["rate"] == [0, 0, 1, 0]
    (a,) = adsb["top"]
    assert a["subject"] == "A1B2C3" and a["labels"] == ["UAL1"] and a["fields"] == {"callsign": "UAL1"}
    assert out["coverage"][-1] == {"state": "running", "profile": "adsb", "from": 999.0, "to": 1100.0}
    flex = out["profiles"]["flex"]
    assert flex["subjects"] == 1 and flex["top"][0]["subject"] == "1234567"
    assert "message" not in flex["top"][0]["fields"] and SECRET_TEXT not in r.text
    # Only the fields the screen shows are read out, so even with pager
    # content allowed the text stays in the log.
    allowed = web_app.create_app(
        settings.LiveSettings(live_dir=live.live_dir, pager_content=True), settings.WebSettings(), [DIGEST], b"k" * 32, feed=False
    )
    r = TestClient(allowed).get("/api/summaries", params={"since": "900"}, headers=BEARER)
    assert r.status_code == 200 and SECRET_TEXT not in r.text
    assert c.get("/api/summaries", params={"until": "1100"}, headers=BEARER).status_code == 400
    assert c.get("/api/summaries", params={"since": "1100", "until": "900"}, headers=BEARER).status_code == 400
    assert c.get("/api/summaries", params={"since": "900"}).status_code == 401
    assert TestClient(gate, follow_redirects=False).get("/summaries").status_code == 303  # needs sign-in
    page = c.get("/summaries", headers=BEARER)
    assert page.status_code == 200 and "summaries.js" in page.text and page.headers["cache-control"] == "no-cache"


def test_positions_counted_on_a_grid_for_the_heat_map(env):
    _, gate, _ = env
    c = TestClient(gate)
    g = c.get("/api/positions/grid", params={"since": "900", "until": "1100", "cell": "0.1"}, headers=BEARER).json()
    assert g["cell_deg"] == 0.1 and g["fixes"] == 1 and g["targets"] == 1
    assert g["cells"] == {"adsb": [[int((-118.4 + 180) / 0.1), int((33.9 + 90) / 0.1), 1, 1]]}
    boxed = c.get(
        "/api/positions/grid", params={"since": "900", "cell": "0.1", "bbox": "-117,30,-116,31"}, headers=BEARER
    ).json()
    assert boxed["fixes"] == 0 and boxed["cells"] == {}
    for bad in ({"cell": "x"}, {"cell": "0"}, {"cell": "11"}, {"bbox": "1,2,3"}, {"bbox": "0,50,1,40"}, {"bbox": "a,b,c,d"}):
        assert c.get("/api/positions/grid", params={"since": "900", **bad}, headers=BEARER).status_code == 400, bad
    assert c.get("/api/positions/grid", headers=BEARER).status_code == 400


def test_the_receiver_range_route(env):
    live, gate, _ = env
    c = TestClient(gate)
    # No receiver position: nothing to measure from.
    r = c.get("/api/positions/range", params={"since": "900"}, headers=BEARER)
    assert r.status_code == 400 and "HACKRF_LIVE_RECEIVER" in r.json()["error"]
    placed = TestClient(
        web_app.create_app(
            settings.LiveSettings(live_dir=live.live_dir, receiver=(33.9, -119.0)), settings.WebSettings(), [DIGEST], b"k" * 32, feed=False
        )
    )
    out = placed.get("/api/positions/range", params={"since": "900", "until": "1100"}, headers=BEARER).json()
    assert out["profile"] == "adsb" and out["fixes"] == 1 and out["targets"] == 1 and out["sector_deg"] == 5
    heard = [v for v in out["sectors"]["all"] if v is not None]
    assert len(heard) == 1 and abs(heard[0] - 55.4) < 0.5 and out["sectors"]["high"] == out["sectors"]["all"]
    assert out["farthest"]["subject"] == "A1B2C3" and out["farthest"]["label"] == "UAL1"
    assert out["levels"]["rows"] == 0  # the row has no signal level
    wide = placed.get("/api/positions/range", params={"since": "900", "sector": "30", "profile": "ais"}, headers=BEARER).json()
    assert len(wide["sectors"]) == 0 and wide["fixes"] == 0
    for bad in ({"sector": "x"}, {"sector": "0.5"}, {"sector": "45"}, {"profile": "nope"}, {"since": "2000", "until": "1000"}):
        q = {"since": "900", **bad}
        assert placed.get("/api/positions/range", params=q, headers=BEARER).status_code == 400, bad
    assert placed.get("/api/positions/range", headers=BEARER).status_code == 400
    assert placed.get("/api/positions/range", params={"since": "900"}).status_code == 401


def test_alert_rules_preview_hits_and_acknowledging(env):
    live, gate, _ = env
    c = TestClient(gate)
    a = c.get("/api/alerts", headers=BEARER).json()
    assert a["rules"] == [] and a["webhook"] is False and "is any of" in a["ops"].values()
    assert "squawk" in a["fields"]["adsb"] and "message" not in a["fields"]["flex"]  # no pager text to match
    rule = {"name": "UAL1", "profiles": ["adsb"], "conditions": [{"field": "label", "op": "equals", "values": ["ual1"]}]}
    # Changes need the page's own origin (or a bearer token).
    signed = _signed_in(gate)
    assert signed.post("/api/alerts/rules", json=rule, headers={"Origin": "http://evil.example"}).status_code == 403
    r = c.post("/api/alerts/rules", json=rule, headers=BEARER)
    assert r.status_code == 201 and r.json()["text"] == "label equals ual1"
    rid = r.json()["id"]
    assert c.post("/api/alerts/rules", json={**rule, "conditions": [{"field": "raw", "op": "matches", "values": ["*"]}]}, headers=BEARER).status_code == 400
    assert c.put(f"/api/alerts/rules/{rid}", json={**rule, "name": "renamed", "repeat_s": 0}, headers=BEARER).json()["name"] == "renamed"
    assert c.put("/api/alerts/rules/999", json=rule, headers=BEARER).status_code == 404
    pv = c.post("/api/alerts/preview", json={**rule, "since": "0", "until": "2000"}, headers=BEARER).json()
    assert pv["matches"] == 1 and pv["samples"][0]["subject"] == "A1B2C3"
    # The engine (polled by hand here) checks only what is logged from now on.
    eng = gate.alerts
    assert eng.poll_once() == []
    w = LiveLogWriter(live.db_path, 86400 * 365 * 100, 1 << 30, 0)
    w.write([Row(ts=3000.0, profile="adsb", kind="aircraft", subject="A1B2C3", label="UAL1", fields={"callsign": "UAL1"})])
    assert [h["detail"] for h in eng.poll_once()] == ["label=UAL1"]
    st = c.get("/api/status", headers=BEARER).json()["alerts"]
    assert st["unacked"] == 1 and st["latest"]["rule_name"] == "renamed" and st["active"] == [{"profile": "adsb", "subject": "A1B2C3"}]
    hits = c.get("/api/alerts/hits", headers=BEARER).json()
    assert len(hits["hits"]) == 1 and hits["more"] is False
    assert c.post("/api/alerts/ack", json={}, headers=BEARER).status_code == 400
    ack = c.post("/api/alerts/ack", json={"ids": [hits["hits"][0]["id"]]}, headers=BEARER).json()
    assert ack["acknowledged"] == 1 and ack["summary"]["unacked"] == 0
    assert c.get("/api/alerts", headers=BEARER).json()["rules"][0]["hits_24h"] in (0, 1)
    assert c.post("/api/alerts/test", headers=BEARER).status_code == 409  # no webhook set up
    assert c.delete(f"/api/alerts/rules/{rid}", headers=BEARER).json() == {"deleted": rid}
    assert c.delete(f"/api/alerts/rules/{rid}", headers=BEARER).status_code == 404
    assert c.get("/api/alerts/hits", headers=BEARER).json()["hits"][0]["rule_name"] == "renamed"  # kept
    page = c.get("/alerts", headers=BEARER)
    assert page.status_code == 200 and "alerts.js" in page.text


def test_alert_rules_on_aircraft_types_and_the_distance_from_the_receiver(env):
    live, gate, _ = env
    c = TestClient(gate)
    a = c.get("/api/alerts", headers=BEARER).json()
    assert "aircraft_type" in a["fields"]["adsb"] and "aircraft_owner" in a["fields"]["adsb"]
    assert "aircraft_type" not in a["fields"]["flex"] and "distance_km" not in a["fields"]["adsb"]  # no receiver set
    assert [x["value"] for x in a["choices"]["aircraft_type"]][:2] == ["military", "law_enforcement"]
    near = {"name": "near", "profiles": ["adsb"], "conditions": [{"field": "distance_km", "op": "lt", "values": ["20"]}]}
    assert c.post("/api/alerts/rules", json=near, headers=BEARER).status_code == 400
    # A made-up receiver 9.2 km west of the logged aircraft.
    placed = TestClient(
        web_app.create_app(
            settings.LiveSettings(live_dir=live.live_dir, receiver=(33.9, -118.5)), settings.WebSettings(), [DIGEST], b"k" * 32, feed=False
        )
    )
    a = placed.get("/api/alerts", headers=BEARER).json()
    assert "distance_km" in a["fields"]["adsb"] and "distance_km" not in a["fields"]["flex"]
    assert "-118.5" not in json.dumps(a)
    pv = placed.post("/api/alerts/preview", json={**near, "since": "0", "until": "2000"}, headers=BEARER).json()
    assert pv["matches"] == 1 and pv["samples"][0]["detail"] == "distance_km=9.2" and "-118.5" not in json.dumps(pv)
    typed = {"name": "airlines", "profiles": [], "conditions": [{"field": "aircraft_type", "op": "any_of", "values": "Commercial"}]}
    r = placed.post("/api/alerts/rules", json=typed, headers=BEARER)
    assert r.status_code == 201 and r.json()["conditions"][0]["values"] == ["commercial"]  # the label, stored as the name
    pv = placed.post("/api/alerts/preview", json={**typed, "since": "0", "until": "2000"}, headers=BEARER).json()
    assert pv["matches"] == 1 and pv["samples"][0]["detail"] == "aircraft_type=commercial"  # UAL1: an airline callsign
    bad = {**typed, "conditions": [{"field": "aircraft_type", "op": "any_of", "values": "spaceship"}]}
    assert "aircraft_type is one of" in placed.post("/api/alerts/rules", json=bad, headers=BEARER).json()["error"]


def test_web_settings_for_the_alert_webhook(tmp_path):
    f = tmp_path / "hook"
    f.write_text("https://ntfy.example/hackrf\n")
    web = settings.WebSettings.from_env(
        {"HACKRF_WEB_ALERT_WEBHOOK_FILE": str(f), "HACKRF_WEB_ALERT_WEBHOOK_FORMAT": "NTFY", "HACKRF_WEB_URL": "http://h:8766/"}
    )
    assert web.alert_webhook_url() == "https://ntfy.example/hackrf" and web.alert_webhook_format == "ntfy"
    assert web.public_url == "http://h:8766"
    assert settings.WebSettings().alert_webhook_url() is None
    with pytest.raises(ValueError):
        settings.WebSettings.from_env({"HACKRF_WEB_ALERT_WEBHOOK_FORMAT": "slack"})
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    gate = web_app.create_app(live, web, [DIGEST], b"k" * 32, feed=False)
    a = TestClient(gate).get("/api/alerts", headers=BEARER).json()
    assert a["webhook"] is True and a["webhook_format"] == "ntfy" and "ntfy.example" not in json.dumps(a)


def test_the_plugins_catalogue_and_a_panel_on_summaries(env):
    live, gate, _ = env
    c = TestClient(gate)
    cat = c.get("/api/plugins", headers=BEARER).json()
    assert {p["name"] for p in cat["profiles"]} == set(live_profiles.PROFILES) and cat["decoders"]
    assert cat["panels"] == [] and cat["panel_errors"] == [] and "summaries.card" in cat["slots"]
    folder = settings.WebSettings().state_path(live) / "panels"
    folder.mkdir(parents=True)
    (folder / "aircraft.toml").write_text(
        '[panel]\ntitle = "Aircraft heard"\nslot = "summaries.card"\n[query]\nprofiles = ["adsb"]\nfields = ["callsign"]\n'
    )
    (folder / "pages.toml").write_text(
        '[panel]\ntitle = "Pages"\nslot = "summaries.card"\n[query]\nprofiles = ["flex"]\nfields = ["message"]\n'
    )
    cat = c.get("/api/plugins", headers=BEARER).json()
    assert [p["id"] for p in cat["panels"]] == ["aircraft"]
    assert cat["panel_errors"] == [
        {"file": "pages.toml", "error": "pager text is shown only when the operator allows pager content"}
    ]
    r = c.get("/api/panels", params={"slot": "summaries.card", "since": "900", "until": "1100"}, headers=BEARER).json()
    (p,) = r["panels"]
    assert p["title"] == "Aircraft heard" and p["rows"][0]["key"] == "A1B2C3" and p["rows"][0]["fields"] == {"callsign": "UAL1"}
    assert SECRET_TEXT not in json.dumps(r)
    assert c.get("/api/panels", params={"slot": "map.side", "since": "900"}, headers=BEARER).status_code == 400
    assert c.get("/api/panels", params={"slot": "summaries.card"}, headers=BEARER).status_code == 400
    page = c.get("/plugins", headers=BEARER)
    assert page.status_code == 200 and "plugins.js" in page.text
