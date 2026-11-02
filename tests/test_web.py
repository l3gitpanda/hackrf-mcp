"""hackrf-web (web UI milestone 4): sign-in, the gate, the API and the
live websocket, with a stand-in control socket and no device."""

import base64
import hashlib
import json

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
        live, web, [DIGEST], b"k" * 32, control=control, cgroup_root=tmp_path / "cgroup", feed=False
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
    assert st["service"] == {"memory_bytes": 83107840, "memory_max": 536870912, "cpu_usec": 19403696576}
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
