"""Static bearer-token check for the HTTP transport."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MIN_TOKEN_LENGTH = 16
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


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


def load_token_digests(token_file: str | None = None) -> list[bytes]:
    """SHA-256 digests of every token the server accepts: the plain token
    from load_token(), plus each hex digest in HACKRF_MCP_TOKEN_SHA256
    (comma-separated). Accepting digests lets a client's token be issued
    without the server, or whoever configures it, ever seeing the token."""
    digests = []
    token = load_token(token_file)
    if token:
        digests.append(token_digest(token))
    for value in os.environ.get("HACKRF_MCP_TOKEN_SHA256", "").split(","):
        value = value.strip()
        if not value:
            continue
        if not _SHA256_HEX.match(value):
            raise ValueError("HACKRF_MCP_TOKEN_SHA256 entries must be 64 hex characters")
        digests.append(bytes.fromhex(value))
    return digests


class BearerTokenMiddleware:
    """Reject every HTTP request whose Authorization header is not
    "Bearer <token>" for an accepted token. The supplied token is hashed and
    compared against every accepted digest in constant time."""

    def __init__(self, app: ASGIApp, token: str | None = None, digests: list[bytes] | None = None) -> None:
        self.app = app
        self._digests = list(digests or [])
        if token:
            self._digests.append(token_digest(token))
        if not self._digests:
            raise ValueError("BearerTokenMiddleware needs at least one token")

    def _authorized(self, header: bytes) -> bool:
        scheme, _, supplied = header.partition(b" ")
        if scheme != b"Bearer" or not supplied:
            return False
        digest = hashlib.sha256(supplied).digest()
        matched = False
        for expected in self._digests:
            matched |= hmac.compare_digest(digest, expected)
        return matched

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"")
        if self._authorized(header):
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
