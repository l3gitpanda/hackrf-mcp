"""Browser sessions for hackrf-web.

A browser signs in once with a token and gets a cookie it sends from then
on (browsers can't add a bearer header to a websocket or a page load). The
cookie holds its expiry and a fingerprint of the token it was issued for,
signed with HMAC-SHA256 under a key kept in the web state directory. It is
valid while the signature checks, it hasn't expired, and its token is still
accepted, so removing a token from the configuration ends its sessions, and
deleting the key file ends them all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

COOKIE = "hackrf_web"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fingerprint(token_digest: bytes) -> str:
    return hashlib.sha256(b"hackrf-web-session:" + token_digest).hexdigest()[:24]


def load_secret(state_dir: Path) -> bytes:
    """The signing key, created (32 random bytes, mode 0600) on first use."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    path = state_dir / "session-key"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError(f"{path} holds too short a key; delete it to make a new one")
    return key


class Sessions:
    def __init__(self, key: bytes, days: float, digests: list[bytes]):
        self.key = key
        self.max_age_s = int(days * 86400)
        self.accepted = {fingerprint(d) for d in digests}

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self.key, payload, hashlib.sha256).digest())

    def issue(self, token_digest: bytes, now: float | None = None) -> str:
        now = time.time() if now is None else now
        payload = json.dumps(
            {"exp": int(now + self.max_age_s), "k": fingerprint(token_digest), "n": secrets.token_hex(4)},
            separators=(",", ":"),
        ).encode()
        return f"{_b64(payload)}.{self._sign(payload)}"

    def valid(self, cookie: str | None, now: float | None = None) -> bool:
        if not cookie or cookie.count(".") != 1:
            return False
        body, sig = cookie.split(".")
        try:
            payload = _unb64(body)
        except (ValueError, TypeError):
            return False
        if not hmac.compare_digest(self._sign(payload), sig):
            return False
        try:
            data = json.loads(payload)
        except ValueError:
            return False
        now = time.time() if now is None else now
        return isinstance(data, dict) and data.get("exp", 0) > now and data.get("k") in self.accepted
