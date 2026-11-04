"""hackrf-web's Starlette app: the JSON API, the live websocket, sign-in
and the static UI.

  GET  /api/registry             profiles and decoders, from the registries
  GET  /api/status               the live service's status, plus the CPU and
                                 memory of hackrf-live and hackrf-web
  GET  /api/log                  rows matching filters (as hackrf_live_search),
                                 newest first, paged with before_id
  GET  /api/log/histogram        matching rows per time bucket and profile
  GET  /api/coverage             what the receiver was doing over a range:
                                 running (which profile), paused, error, idle
  GET  /api/summary              one entry per subject (as hackrf_live_summary)
  GET  /api/summaries            per profile over a range: messages over time,
                                 subjects heard and the busiest, with coverage
  GET  /api/events               starts, stops, pauses, errors
  GET  /api/positions            map-track points, thinned for long ranges
  GET  /api/positions/grid       positions counted per map cell (a heat map)
  GET  /api/basemap.pmtiles      the map's base map (vector tiles, byte
                                 ranges), if the operator has put one in the
                                 web state directory (see web/basemap.py)
  GET  /api/spectrum/segments    spectrum history segments in a range
  GET  /api/spectrum             one segment's waterfall, downsampled
  POST /api/live/start           start or switch a profile, or a rotation
  POST /api/live/stop            stop live decoding
  POST /api/live/watch           lend the receiver to a watch of one range
                                 (waterfall only) for a while; retunes one
  POST /api/live/watch/stop      end a watch: back to the rotation
  GET  /api/alerts               alert rules (with hits in the last 24 h), the
                                 fields each profile's rules may test, and
                                 whether a webhook is set up
  POST /api/alerts/rules         add a rule; PUT/DELETE /api/alerts/rules/<id>
  POST /api/alerts/preview       what a rule would have matched (default the
                                 last 7 days)
  GET  /api/alerts/hits          hits, newest first, paged with before_id
  POST /api/alerts/ack           acknowledge hits ({"ids": [...]} or {"all": true})
  POST /api/alerts/test          post a test message to the webhook
  GET  /api/plugins              the catalogue: decoders, profiles (program
                                 installed or not, tuning, kinds, views,
                                 colour) and the operator's panel files
  GET  /api/panels               a slot's panels run over a range
  WS   /ws/live                  rows, events, positions and FFT rows as
                                 they happen, and status every few seconds

Times are Unix seconds, ISO 8601, or "30m"/"2h"/"1d" ago. Every route but
the sign-in page needs a token (Authorization: Bearer) or the session cookie
signing in sets. Requests that change something, and websockets, must come
from the server's own origin. Pager text is removed on read unless the
operator allows it (HACKRF_MCP_PAGER_CONTENT).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import parse_qs

import anyio
import anyio.abc
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from hackrf_mcp import decoders, live_client, live_log, live_profiles, live_spectrum, procstats
from hackrf_mcp.live_service import WATCH_DEFAULT_MIN, WATCH_MAX_MIN
from hackrf_mcp.settings import LiveSettings, WebSettings
from hackrf_mcp.web import alerts, basemap, panels
from hackrf_mcp.web.hub import FeedHub
from hackrf_mcp.web.sessions import COOKIE, Sessions

STATIC = Path(__file__).parent / "static"
# Reachable without signing in: the sign-in page and what it loads.
PUBLIC = {"/login", "/auth/login", "/static/login.css", "/static/login.js", "/favicon.ico"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
]
CGROUP = Path("/sys/fs/cgroup/system.slice")
STATUS_PUSH_S = 3.0
STATUS_CACHE_S = 2.0
# Seconds between the fixes kept per target when a range of positions is thinned.
POSITION_STEPS = (20, 30, 60, 120, 300, 600, 1800, 3600)
# The screens: path -> page.
PAGES = {
    "/": "index.html",
    "/log": "log.html",
    "/map": "map.html",
    "/history": "history.html",
    "/bands": "bands.html",
    "/summaries": "summaries.html",
    "/alerts": "alerts.html",
    "/plugins": "plugins.html",
}


# --- Reading helpers -----------------------------------------------------------


def _when(request: Request, name: str, now: float) -> float | None:
    return live_log.parse_when(request.query_params.get(name), now)


def _int(request: Request, name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(request.query_params.get(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    return max(lo, min(hi, v))


def make_redactor(live: LiveSettings) -> Callable[[dict], dict]:
    """Applies each profile's redaction (pager text) unless the operator
    allows the content."""
    live_profiles.load()

    def redact(row: dict) -> dict:
        if live.pager_content:
            return row
        profile = live_profiles.PROFILES.get(row.get("profile") or "")
        if profile is not None and profile.redact is not None:
            return profile.redact(row)
        return row

    return redact


def row_json(r) -> dict:
    d = dict(r)
    try:
        d["fields"] = json.loads(d.get("fields") or "{}")
    except ValueError:
        d["fields"] = {}
    return d


def registry_json(live: LiveSettings) -> dict:
    live_profiles.load()
    decoders.load()
    return {
        "profiles": [
            {
                "name": p.name,
                "summary": p.summary,
                "programs": list(p.programs),
                "installed": live_profiles.find_program(p) is not None,
                "sample_rate_hz": p.sample_rate_hz,
                "center_hz": p.center_hz,
                "default_freqs_hz": list(p.default_freqs_hz),
                "needs_freqs": p.needs_freqs,
                "fixed_freqs": p.fixed_freqs,
                "lna_gain_db": p.lna_gain_db,
                "vga_gain_db": p.vga_gain_db,
                "summary_kinds": list(p.summary_kinds),
                "summary_fields": list(p.summary_fields),
                "views": [v.to_json() for v in p.ui_views()],
                "color": p.color,
                "spectrum": p.sample_rate_hz > 0,
                "redacted": p.redact is not None and not live.pager_content,
            }
            for p in live_profiles.PROFILES.values()
        ],
        "decoders": [
            {
                "name": d.name,
                "summary": d.summary,
                "recipe": d.recipe,
                "programs": list(d.programs),
                "installed": decoders.find_program(d) is not None,
                "default_freqs_hz": list(d.default_freqs_hz),
            }
            for d in decoders.DECODERS.values()
        ],
        "pager_content": live.pager_content,
        # HACKRF_LIVE_RECEIVER, for the map's range rings; None if unset.
        "receiver": list(live.receiver) if live.receiver else None,
        # What a watch of a custom range may be (the live service checks again).
        "watch": {
            "rates_hz": list(live_profiles.WATCH_RATES_HZ),
            "min_hz": live_profiles.WATCH_MIN_HZ,
            "max_hz": live_profiles.WATCH_MAX_HZ,
            "default_minutes": WATCH_DEFAULT_MIN,
            "max_minutes": WATCH_MAX_MIN,
        },
    }


def summary_fields(profile: live_profiles.Profile) -> tuple[str, ...]:
    """The fields the Summaries screen shows for a profile's subjects: its
    summary fields, then those of its table view, less the position (the
    heat map has those). Nothing else is read out of the log for it, so a
    field like pager text can't come along."""
    out = [f for f in profile.summary_fields if f not in ("lat", "lon")]
    for v in profile.ui_views():
        if v.name == "table":
            out += [f for f in v.fields if f not in out and f not in ("lat", "lon")]
    return tuple(out)


def cgroup_stats(unit: str, root: Path = CGROUP) -> dict | None:
    """Memory and CPU of a systemd unit from its cgroup (None if not found,
    as in a container, where a service reports its own instead)."""
    return procstats.read(root / unit)


# --- Auth, origin and headers ----------------------------------------------------


def _cookie(headers: dict[bytes, bytes]) -> str | None:
    for part in headers.get(b"cookie", b"").decode(errors="replace").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


class Gate:
    """Lets a request through if it carries an accepted bearer token or a
    valid session cookie; checks the Origin of state-changing requests and
    websockets; adds security headers to every response."""

    def __init__(self, app: ASGIApp, digests: list[bytes], sessions: Sessions, extra_origins: tuple[str, ...] = ()):
        self.app = app
        self.digests = list(digests)
        self.sessions = sessions
        self.extra_origins = set(extra_origins)

    def bearer(self, headers: dict[bytes, bytes]) -> bool:
        scheme, _, supplied = headers.get(b"authorization", b"").partition(b" ")
        if scheme != b"Bearer" or not supplied:
            return False
        digest = hashlib.sha256(supplied).digest()
        ok = False
        for d in self.digests:
            ok |= hmac.compare_digest(digest, d)
        return ok

    def origin_ok(self, scope: Scope, headers: dict[bytes, bytes]) -> bool:
        origin = headers.get(b"origin")
        if origin is None:
            return False
        origin = origin.decode(errors="replace").rstrip("/")
        host = headers.get(b"host", b"").decode(errors="replace")
        scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
        return origin == f"{scheme}://{host}" or origin in self.extra_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        path = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = list(message.get("headers") or [])
                h += SECURITY_HEADERS
                if path.startswith("/api/"):
                    h.append((b"cache-control", b"no-store"))
                elif path in PAGES or path == "/login" or path.startswith("/static/"):
                    # Revalidate every time, so a deploy reaches browsers at once.
                    h.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": h}
            await send(message)

        if path in PUBLIC:
            await self.app(scope, receive, send_with_headers)
            return
        by_bearer = self.bearer(headers)
        authed = by_bearer or self.sessions.valid(_cookie(headers))
        if scope["type"] == "websocket":
            # A browser always sends Origin; a page on another site must not
            # be able to ride the cookie.
            if not authed or (not by_bearer and not self.origin_ok(scope, headers)):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if not authed:
            if path.startswith("/api/"):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            else:
                resp = RedirectResponse("/login", status_code=303)
            await resp(scope, receive, send_with_headers)
            return
        if scope["method"] not in ("GET", "HEAD", "OPTIONS") and not by_bearer and not self.origin_ok(scope, headers):
            await JSONResponse({"error": "cross-origin request refused"}, status_code=403)(
                scope, receive, send_with_headers
            )
            return
        await self.app(scope, receive, send_with_headers)


# --- The app ------------------------------------------------------------------------


def create_app(
    live: LiveSettings,
    web: WebSettings,
    digests: list[bytes],
    session_key: bytes,
    *,
    control: Callable[..., dict] = live_client.request,
    cgroup_root: Path = CGROUP,
    own_stats: Callable[[], dict | None] = procstats.own,
    feed: bool = True,
) -> ASGIApp:
    if not digests:
        raise ValueError("hackrf-web needs at least one accepted token")
    sessions = Sessions(session_key, web.session_days, digests)
    redact = make_redactor(live)
    hub = FeedHub(live.feed_socket_path, redact)
    store = alerts.AlertStore(web.state_path(live) / "alerts.db")
    webhook_url = web.alert_webhook_url()
    webhook = alerts.Webhook(webhook_url, web.alert_webhook_format, store, web.public_url) if webhook_url else None
    engine = alerts.AlertEngine(store, live.db_path, live.pager_content, webhook)

    def reader():
        return live_log.open_reader(live.db_path)

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    def guarded(fn):
        """Bad parameters are a 400 and a missing log a 404, not a 500."""

        def wrapper(request: Request):
            try:
                return fn(request)
            except ValueError as exc:
                return error(400, str(exc))
            except (live_log.LogNotFound, live_spectrum.SpectrumNotFound) as exc:
                return error(404, str(exc))

        return wrapper

    # --- pages and sign-in

    async def page(request: Request) -> Response:
        return FileResponse(STATIC / PAGES[request.url.path])

    basemap_path = web.state_path(live) / basemap.FILENAME

    async def api_basemap(request: Request) -> Response:
        # Nothing is fetched from elsewhere: the map draws a base map only if
        # the operator has placed one here. The browser reads it in byte
        # ranges (FileResponse answers Range requests).
        if not basemap_path.is_file():
            return error(404, f"no base map: extract one into {basemap_path} (python -m hackrf_mcp.web.basemap)")
        return FileResponse(basemap_path, media_type="application/vnd.pmtiles")

    async def login_page(request: Request) -> Response:
        return FileResponse(STATIC / "login.html")

    async def login(request: Request) -> Response:
        body = (await request.body())[:4096].decode(errors="replace")
        token = (parse_qs(body).get("token") or [""])[0].strip()
        digest = hashlib.sha256(token.encode()).digest()
        ok = False
        for d in digests:
            ok |= hmac.compare_digest(digest, d)
        if not token or not ok:
            await asyncio.sleep(1.0)  # slows guessing; tokens are long anyway
            return RedirectResponse("/login?failed=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, sessions.issue(digest), max_age=sessions.max_age_s, httponly=True, samesite="strict", path="/"
        )
        return resp

    async def logout(request: Request) -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # --- the API

    def api_registry(request: Request) -> Response:
        return JSONResponse({**registry_json(live), "basemap": basemap.info(basemap_path)})

    status_cache: dict = {"at": 0.0, "value": None}

    def status_json() -> dict:
        # Every open browser asks every few seconds; share one answer.
        if status_cache["value"] is not None and time.monotonic() - status_cache["at"] < STATUS_CACHE_S:
            return status_cache["value"]
        value = _status_json()
        status_cache.update(at=time.monotonic(), value=value)
        return value

    def _status_json() -> dict:
        try:
            st = control({"cmd": "status"}, timeout=5)
        except live_client.Unavailable as exc:
            st = {"state": "unreachable", "error": str(exc)}
        except live_client.LiveError as exc:
            st = {"state": "unreachable", "error": str(exc)}
        return {
            "live": st,
            # Under systemd, read off each unit's cgroup; in containers
            # (the split station) neither sees the other's, so the live
            # service reports its own in its status, and this its own.
            "service": cgroup_stats("hackrf-live.service", cgroup_root) or st.get("process"),
            "web": cgroup_stats("hackrf-web.service", cgroup_root) or own_stats(),
            "feed": hub.status(),
            "alerts": engine.summary(),
            "now": time.time(),
        }

    def api_status(request: Request) -> Response:
        return JSONResponse(status_json())

    def log_filters(request: Request) -> dict:
        q = request.query_params
        freq = q.get("freq_hz")
        tol = q.get("freq_tolerance_hz")
        profiles = tuple(p for p in (q.get("profiles") or "").split(",") if p)
        return {
            "profile": q.get("profile") or None,
            "profiles": profiles or None,
            "kind": q.get("kind") or None,
            "subject": q.get("subject") or None,
            "text": q.get("text") or None,
            "freq_hz": int(float(freq)) if freq else None,
            "freq_tolerance_hz": int(float(tol)) if tol else 5_000,
        }

    @guarded
    def api_log(request: Request) -> Response:
        now = time.time()
        before = request.query_params.get("before_id")
        limit = _int(request, "limit", 200, 1, 2000)
        with closing(reader()) as conn:
            rows = live_log.search(
                conn,
                since=_when(request, "since", now),
                until=_when(request, "until", now),
                limit=limit + 1,
                before_id=int(before) if before else None,
                **log_filters(request),
            )
            more = len(rows) > limit
            return JSONResponse({"rows": [redact(row_json(r)) for r in rows[:limit]], "more": more})

    @guarded
    def api_histogram(request: Request) -> Response:
        now = time.time()
        since = _when(request, "since", now)
        until = _when(request, "until", now) or now
        if since is None or since >= until:
            raise ValueError("since (before until) is required")
        with closing(reader()) as conn:
            out = live_log.histogram(conn, since, until, _int(request, "buckets", 96, 1, 2000), **log_filters(request))
        return JSONResponse(out)

    @guarded
    def api_coverage(request: Request) -> Response:
        now = time.time()
        since = _when(request, "since", now)
        until = _when(request, "until", now) or now
        if since is None or since >= until:
            raise ValueError("since (before until) is required")
        with closing(reader()) as conn:
            return JSONResponse({"since": since, "until": until, "coverage": live_log.coverage(conn, since, until)})

    @guarded
    def api_summary(request: Request) -> Response:
        now = time.time()
        live_profiles.load()
        name = request.query_params.get("profile") or ""
        profile = live_profiles.PROFILES.get(name)
        if profile is None:
            raise ValueError(f"unknown profile {name!r}")
        since = _when(request, "since", now)
        with closing(reader()) as conn:
            entries = live_log.summarise(
                conn,
                profile.name,
                profile.summary_kinds,
                since if since is not None else now - 3600,
                _when(request, "until", now),
                limit=_int(request, "limit", 200, 1, 2000),
            )
        for e in entries:
            e["kinds"], e["freqs"] = sorted(e["kinds"]), sorted(e["freqs"])
            if profile.redact is not None and not live.pager_content:
                e["fields"].pop("message", None)
        return JSONResponse({"profile": profile.name, "subjects": entries})

    @guarded
    def api_summaries(request: Request) -> Response:
        # The Summaries screen's numbers for one range, from one reading of
        # the log, so the cards, the rate chart and the coverage agree.
        now = time.time()
        since = _when(request, "since", now)
        until = _when(request, "until", now) or now
        if since is None or since >= until:
            raise ValueError("since (before until) is required")
        live_profiles.load()
        profiles = {
            p.name: (p.summary_kinds, summary_fields(p)) for p in live_profiles.PROFILES.values() if not p.spectrum_only
        }
        with closing(reader()) as conn:
            out = live_log.summaries(
                conn,
                since,
                until,
                profiles,
                buckets=_int(request, "buckets", 96, 1, 500),
                top=_int(request, "top", 20, 1, 100),
            )
            out["coverage"] = live_log.coverage(conn, since, until)
        for name, stats in out["profiles"].items():
            profile = live_profiles.PROFILES[name]
            for e in stats["top"]:
                if profile.redact is not None and not live.pager_content:
                    for kind in e["kinds"] or [""]:
                        e["fields"] = profile.redact({"kind": kind, "fields": e["fields"], "raw": None})["fields"]
        return JSONResponse(out)

    @guarded
    def api_position_grid(request: Request) -> Response:
        now = time.time()
        since = _when(request, "since", now)
        until = _when(request, "until", now) or now
        if since is None or since >= until:
            raise ValueError("since (before until) is required")
        bbox = None
        if request.query_params.get("bbox"):
            try:
                bbox = tuple(float(v) for v in request.query_params["bbox"].split(","))
            except ValueError as exc:
                raise ValueError("bbox is west,south,east,north in degrees") from exc
            if len(bbox) != 4 or not (-90 <= bbox[1] <= bbox[3] <= 90):
                raise ValueError("bbox is west,south,east,north in degrees")
        try:
            cell = float(request.query_params.get("cell", 0.05))
        except ValueError as exc:
            raise ValueError("cell is a size in degrees") from exc
        if not 0 < cell <= 10:
            raise ValueError("cell is a size in degrees, at most 10")
        with closing(reader()) as conn:
            out = live_log.position_grid(
                conn,
                since,
                until,
                profile=request.query_params.get("profile") or None,
                cell_deg=cell,
                bbox=bbox,
                max_cells=_int(request, "max_cells", 20_000, 100, 50_000),
            )
        return JSONResponse({"since": since, "until": until, **out})

    @guarded
    def api_events(request: Request) -> Response:
        now = time.time()
        with closing(reader()) as conn:
            rows = live_log.events(
                conn,
                since=_when(request, "since", now),
                until=_when(request, "until", now),
                limit=_int(request, "limit", 200, 1, 5000),
            )
            return JSONResponse({"events": [dict(r) for r in rows]})

    @guarded
    def api_positions(request: Request) -> Response:
        now = time.time()
        q = {
            "since": _when(request, "since", now),
            "until": _when(request, "until", now),
            "profile": request.query_params.get("profile") or None,
            "subject": request.query_params.get("subject") or None,
        }
        limit = _int(request, "limit", 10_000, 1, 50_000)
        with closing(reader()) as conn:
            step = None
            total = live_log.count_positions(conn, **q)
            if total <= limit:
                rows = live_log.positions(conn, **q, limit=limit)
            else:
                # More fixes than asked for: thin every track to one fix per
                # step seconds rather than cut the range short. Fixes come at
                # most every 10 s per target, so start near the ratio.
                need = 10 * total / limit
                for step in [s for s in POSITION_STEPS if s >= need] or POSITION_STEPS[-1:]:
                    rows = live_log.positions(conn, **q, limit=limit + 1, step=step)
                    if len(rows) <= limit:
                        break
            truncated = len(rows) > limit
            return JSONResponse(
                {"positions": [dict(r) for r in rows[:limit]], "step": step, "truncated": truncated}
            )

    @guarded
    def api_segments(request: Request) -> Response:
        now = time.time()
        with closing(live_spectrum.open_reader(live.spectrum_db_path)) as conn:
            segs = live_spectrum.segments(conn, _when(request, "since", now), _when(request, "until", now))
        return JSONResponse({"segments": segs})

    @guarded
    def api_spectrum(request: Request) -> Response:
        now = time.time()
        seg = request.query_params.get("segment")
        if not seg or not seg.isdigit():
            raise ValueError("segment (a segment id from /api/spectrum/segments) is required")
        tier = request.query_params.get("tier")
        with closing(live_spectrum.open_reader(live.spectrum_db_path)) as conn:
            out = live_spectrum.downsample(
                conn,
                int(seg),
                _when(request, "since", now),
                _when(request, "until", now),
                buckets=_int(request, "buckets", 600, 1, live_spectrum.MAX_BUCKETS),
                tier={"second": live_spectrum.TIER_SECOND, "minute": live_spectrum.TIER_MINUTE}.get(tier or ""),
            )
        if out is None:
            return error(404, f"no spectrum segment {seg}")
        out["mean"] = base64.b64encode(out["mean"]).decode()
        out["peak"] = base64.b64encode(out["peak"]).decode()
        return JSONResponse(out)

    async def api_start(request: Request) -> Response:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            if body.get("schedule"):
                if body.get("profile"):
                    raise ValueError("give either profile or schedule, not both")
                req = {
                    "cmd": "start",
                    "schedule": [
                        {
                            "profile": str(e["profile"]),
                            "minutes": float(e["minutes"]),
                            "freqs_hz": [int(f) for f in e.get("frequencies_hz") or []],
                            "lna_gain_db": e.get("lna_gain_db"),
                            "vga_gain_db": e.get("vga_gain_db"),
                            "amp": bool(e.get("amp_enable", False)),
                        }
                        for e in body["schedule"]
                    ],
                }
            elif body.get("profile"):
                req = {
                    "cmd": "start",
                    "profile": str(body["profile"]),
                    "freqs_hz": [int(f) for f in body.get("frequencies_hz") or []],
                    "lna_gain_db": body.get("lna_gain_db"),
                    "vga_gain_db": body.get("vga_gain_db"),
                    "amp": bool(body.get("amp_enable", False)),
                }
            else:
                raise ValueError("give a profile to start, or a schedule")
        except (ValueError, KeyError, TypeError) as exc:
            return error(400, f"bad request: {exc}")
        return await _control(req)

    async def api_stop(request: Request) -> Response:
        return await _control({"cmd": "stop"})

    async def api_watch(request: Request) -> Response:
        # Lend the receiver to a watch of one range; the live service hands
        # it back to the rotation when the time runs out.
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            req = {
                "cmd": "watch",
                "center_hz": int(body["center_hz"]),
                "sample_rate_hz": int(body.get("sample_rate_hz") or live_profiles.WATCH_RATES_HZ[-1]),
                "lna_gain_db": body.get("lna_gain_db"),
                "vga_gain_db": body.get("vga_gain_db"),
                "amp": bool(body.get("amp_enable", False)),
            }
            if body.get("minutes") is not None:
                req["minutes"] = float(body["minutes"])
        except (ValueError, KeyError, TypeError) as exc:
            return error(400, f"bad request: {exc}")
        return await _control(req)

    async def api_watch_stop(request: Request) -> Response:
        return await _control({"cmd": "watch-stop"})

    # --- alerts

    def api_alerts(request: Request) -> Response:
        live_profiles.load()
        day = time.time() - 86400
        stats = store.rule_stats(day)
        rules = []
        for r in store.rules():
            st = stats.get(r.id, {})
            rules.append({**r.to_json(), "hits_24h": st.get("hits", 0), "last_hit": st.get("last")})
        return JSONResponse(
            {
                "rules": rules,
                "fields": {
                    p.name: alerts.allowed_fields(p, live.pager_content)
                    for p in live_profiles.PROFILES.values()
                    if not p.spectrum_only
                },
                "ops": alerts.OPS,
                "webhook": webhook is not None,
                "webhook_format": web.alert_webhook_format if webhook is not None else None,
                "summary": engine.summary(),
            }
        )

    async def _json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError as exc:
            raise ValueError("send a JSON object") from exc
        if not isinstance(body, dict):
            raise ValueError("send a JSON object")
        return body

    async def api_rule_create(request: Request) -> Response:
        try:
            rule = alerts.parse_rule(await _json_body(request), live.pager_content)
            saved = await run_in_threadpool(store.save, rule)
        except ValueError as exc:
            return error(400, str(exc))
        engine.reload()
        return JSONResponse(saved.to_json(), status_code=201)

    async def api_rule(request: Request) -> Response:
        rule_id = request.path_params["rule_id"]
        if request.method == "DELETE":
            if not await run_in_threadpool(store.delete, rule_id):
                return error(404, f"no rule {rule_id}")
            engine.reload()
            return JSONResponse({"deleted": rule_id})
        try:
            rule = alerts.parse_rule(await _json_body(request), live.pager_content, rule_id)
            saved = await run_in_threadpool(store.save, rule)
        except KeyError:
            return error(404, f"no rule {rule_id}")
        except ValueError as exc:
            return error(400, str(exc))
        engine.reload()
        return JSONResponse(saved.to_json())

    async def api_preview(request: Request) -> Response:
        now = time.time()
        try:
            body = await _json_body(request)
            rule = alerts.parse_rule(body, live.pager_content)
            since = live_log.parse_when(body.get("since"), now)
            until = live_log.parse_when(body.get("until"), now)
            until = now if until is None else until
            since = until - 7 * 86400 if since is None else since
            if since >= until:
                raise ValueError("since must be before until")
            out = await run_in_threadpool(engine.preview, rule, max(since, until - 7 * 86400), until)
        except ValueError as exc:
            return error(400, str(exc))
        except live_log.LogNotFound as exc:
            return error(404, str(exc))
        return JSONResponse(out)

    @guarded
    def api_hits(request: Request) -> Response:
        before = request.query_params.get("before_id")
        limit = _int(request, "limit", 100, 1, 500)
        rows = store.hits(limit + 1, int(before) if before else None, request.query_params.get("unacked") == "1")
        return JSONResponse({"hits": rows[:limit], "more": len(rows) > limit})

    async def api_ack(request: Request) -> Response:
        try:
            body = await _json_body(request)
            ids = None if body.get("all") else [int(i) for i in body.get("ids") or []]
            if ids is not None and not ids:
                raise ValueError('give "ids" or "all": true')
        except (ValueError, TypeError) as exc:
            return error(400, str(exc))
        n = await run_in_threadpool(store.ack, ids)
        engine.refresh()
        return JSONResponse({"acknowledged": n, "summary": engine.summary()})

    async def api_alert_test(request: Request) -> Response:
        if webhook is None:
            return error(409, "no webhook is set up (HACKRF_WEB_ALERT_WEBHOOK_FILE)")
        now = time.time()
        hit = {
            "id": 0,
            "ts": now,
            "rule_name": "test",
            "profile": "test",
            "kind": None,
            "subject": None,
            "label": None,
            "freq_hz": None,
            "detail": "a test message from hackrf-web's Alerts screen",
        }
        status = await run_in_threadpool(webhook.send, hit, 1)
        return JSONResponse({"status": status})

    # --- plugins

    panels_dir = web.state_path(live) / "panels"

    def api_plugins(request: Request) -> Response:
        reg = registry_json(live)
        loaded = panels.load(panels_dir, live.pager_content)
        return JSONResponse(
            {
                "profiles": reg["profiles"],
                "decoders": reg["decoders"],
                "panels": [p.to_json() for p in loaded.panels],
                "panel_errors": loaded.errors,
                "panels_dir": str(panels_dir),
                "slots": panels.SLOTS,
                "renders": list(panels.RENDERS),
                "pager_content": live.pager_content,
            }
        )

    @guarded
    def api_panels(request: Request) -> Response:
        now = time.time()
        slot = request.query_params.get("slot") or ""
        if slot not in panels.SLOTS:
            raise ValueError(f"slot must be one of: {', '.join(panels.SLOTS)}")
        since = _when(request, "since", now)
        until = _when(request, "until", now) or now
        if since is None or since >= until:
            raise ValueError("since (before until) is required")
        loaded = panels.load(panels_dir, live.pager_content)
        out = []
        with closing(reader()) as conn:
            for p in loaded.panels:
                if p.slot != slot:
                    continue
                try:
                    out.append({**p.to_json(), **panels.run(conn, p, since, until)})
                except sqlite3.Error as exc:
                    out.append({**p.to_json(), "error": f"reading the log: {exc}"})
        return JSONResponse({"slot": slot, "panels": out, "errors": loaded.errors})

    async def _control(req: dict) -> Response:
        try:
            st = await run_in_threadpool(control, req, timeout=30)
        except live_client.Unavailable as exc:
            return error(503, str(exc))
        except live_client.LiveError as exc:
            return error(400, str(exc))
        return JSONResponse(st)

    # --- the live websocket

    async def ws_live(ws: WebSocket) -> None:
        await ws.accept()
        client = hub.subscribe(fft=ws.query_params.get("fft") != "0")
        await ws.send_text(json.dumps({"type": "hello", "pager_content": live.pager_content}))

        async def pump() -> None:
            last_status = 0.0
            while True:
                try:
                    text = await asyncio.wait_for(client.q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    text = None
                if client.dropped:
                    n, client.dropped = client.dropped, 0
                    await ws.send_text(json.dumps({"type": "dropped", "n": n}))
                if text is not None:
                    await ws.send_text(text)
                if time.monotonic() - last_status >= STATUS_PUSH_S:
                    last_status = time.monotonic()
                    st = await run_in_threadpool(status_json)
                    await ws.send_text(json.dumps({"type": "status", **st}, default=str))

        async def listen() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and "fft" in msg:
                    client.fft = bool(msg["fft"])

        # Both halves in one anyio task group (Starlette's own concurrency), so
        # the server stopping or the client leaving unwinds them together.
        # Either half ending ends the session: listen when the client goes,
        # pump when a send fails.
        async def half(tg: anyio.abc.TaskGroup, fn) -> None:
            try:
                await fn()
            except (WebSocketDisconnect, RuntimeError, OSError, anyio.ClosedResourceError):
                pass  # the client is gone
            finally:
                tg.cancel_scope.cancel()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(half, tg, pump)
                tg.start_soon(half, tg, listen)
        finally:
            hub.unsubscribe(client)

    async def lifespan(app):
        if feed:
            hub.start()
        engine.start()
        yield
        engine.stop()
        await hub.stop()

    routes = [
        *[Route(path, page) for path in PAGES],
        Route("/login", login_page),
        Route("/auth/login", login, methods=["POST"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/api/registry", api_registry),
        Route("/api/status", api_status),
        Route("/api/log", api_log),
        Route("/api/log/histogram", api_histogram),
        Route("/api/coverage", api_coverage),
        Route("/api/summary", api_summary),
        Route("/api/summaries", api_summaries),
        Route("/api/events", api_events),
        Route("/api/positions", api_positions),
        Route("/api/positions/grid", api_position_grid),
        Route("/api/basemap.pmtiles", api_basemap),
        Route("/api/spectrum/segments", api_segments),
        Route("/api/spectrum", api_spectrum),
        Route("/api/live/start", api_start, methods=["POST"]),
        Route("/api/live/stop", api_stop, methods=["POST"]),
        Route("/api/live/watch", api_watch, methods=["POST"]),
        Route("/api/live/watch/stop", api_watch_stop, methods=["POST"]),
        Route("/api/alerts", api_alerts),
        Route("/api/alerts/rules", api_rule_create, methods=["POST"]),
        Route("/api/alerts/rules/{rule_id:int}", api_rule, methods=["PUT", "DELETE"]),
        Route("/api/alerts/preview", api_preview, methods=["POST"]),
        Route("/api/alerts/hits", api_hits),
        Route("/api/alerts/ack", api_ack, methods=["POST"]),
        Route("/api/alerts/test", api_alert_test, methods=["POST"]),
        Route("/api/plugins", api_plugins),
        Route("/api/panels", api_panels),
        WebSocketRoute("/ws/live", ws_live),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=asynccontextmanager(lifespan))
    gate = Gate(app, digests, sessions, web.extra_origins)
    gate.hub = hub  # for the tests
    gate.alerts = engine
    return gate
