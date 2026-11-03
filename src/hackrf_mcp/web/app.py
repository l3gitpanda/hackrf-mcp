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
  GET  /api/events               starts, stops, pauses, errors
  GET  /api/positions            map-track points, thinned for long ranges
  GET  /api/basemap.pmtiles      the map's base map (vector tiles, byte
                                 ranges), if the operator has put one in the
                                 web state directory (see web/basemap.py)
  GET  /api/spectrum/segments    spectrum history segments in a range
  GET  /api/spectrum             one segment's waterfall, downsampled
  POST /api/live/start           start or switch a profile, or a rotation
  POST /api/live/stop            stop live decoding
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

from hackrf_mcp import decoders, live_client, live_log, live_profiles, live_spectrum
from hackrf_mcp.settings import LiveSettings, WebSettings
from hackrf_mcp.web import basemap
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
    }


def cgroup_stats(unit: str, root: Path = CGROUP) -> dict | None:
    """Memory and CPU of a systemd unit from its cgroup (None if not found)."""
    base = root / unit
    try:
        mem = int((base / "memory.current").read_text())
        max_raw = (base / "memory.max").read_text().strip()
        cpu = 0
        for line in (base / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                cpu = int(line.split()[1])
    except (OSError, ValueError):
        return None
    return {"memory_bytes": mem, "memory_max": None if max_raw == "max" else int(max_raw), "cpu_usec": cpu}


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
                elif path in ("/", "/log", "/map", "/history", "/login") or path.startswith("/static/"):
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
    feed: bool = True,
) -> ASGIApp:
    if not digests:
        raise ValueError("hackrf-web needs at least one accepted token")
    sessions = Sessions(session_key, web.session_days, digests)
    redact = make_redactor(live)
    hub = FeedHub(live.feed_socket_path, redact)

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

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    async def log_page(request: Request) -> Response:
        return FileResponse(STATIC / "log.html")

    async def map_page(request: Request) -> Response:
        return FileResponse(STATIC / "map.html")

    async def history_page(request: Request) -> Response:
        return FileResponse(STATIC / "history.html")

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
            "service": cgroup_stats("hackrf-live.service", cgroup_root),
            "web": cgroup_stats("hackrf-web.service", cgroup_root),
            "feed": hub.status(),
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
        yield
        await hub.stop()

    routes = [
        Route("/", index),
        Route("/log", log_page),
        Route("/map", map_page),
        Route("/history", history_page),
        Route("/login", login_page),
        Route("/auth/login", login, methods=["POST"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/api/registry", api_registry),
        Route("/api/status", api_status),
        Route("/api/log", api_log),
        Route("/api/log/histogram", api_histogram),
        Route("/api/coverage", api_coverage),
        Route("/api/summary", api_summary),
        Route("/api/events", api_events),
        Route("/api/positions", api_positions),
        Route("/api/basemap.pmtiles", api_basemap),
        Route("/api/spectrum/segments", api_segments),
        Route("/api/spectrum", api_spectrum),
        Route("/api/live/start", api_start, methods=["POST"]),
        Route("/api/live/stop", api_stop, methods=["POST"]),
        WebSocketRoute("/ws/live", ws_live),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=asynccontextmanager(lifespan))
    gate = Gate(app, digests, sessions, web.extra_origins)
    gate.hub = hub  # for the tests
    return gate
