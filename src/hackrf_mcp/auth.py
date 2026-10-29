"""Static bearer-token check for the HTTP transport."""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MIN_TOKEN_LENGTH = 16


def load_token(token_file: str | None = None) -> str | None:
    """Read the token from token_file (or HACKRF_MCP_TOKEN_FILE), falling
    back to HACKRF_MCP_TOKEN. Returns None if neither is set."""
    token_file = token_file or os.environ.get("HACKRF_MCP_TOKEN_FILE")
    if token_file:
        token = Path(token_file).expanduser().read_text().strip()
    else:
        token = os.environ.get("HACKRF_MCP_TOKEN", "").strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"the HTTP token must be at least {MIN_TOKEN_LENGTH} characters")
    return token


class BearerTokenMiddleware:
    """Reject every HTTP request whose Authorization header is not exactly
    "Bearer <token>", compared in constant time."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
        if hmac.compare_digest(supplied, self._expected):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})
