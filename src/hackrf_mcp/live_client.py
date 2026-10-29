"""Talking to the live decoder service (live_service.py) over its socket.

Used by the MCP server's hackrf_live_* tools, and by _run_on_device to
pause live decoding around a device call. When the service isn't running
(no socket, or nothing listening), requests raise Unavailable and device
tools behave exactly as they would without it.
"""

from __future__ import annotations

import json
import socket

from hackrf_mcp import settings


class Unavailable(ConnectionError):
    """The live service isn't running."""


class LiveError(RuntimeError):
    """The service answered with an error."""


def request(req: dict, timeout: float = 20.0) -> dict:
    path = settings.get_live().socket_path
    if not path.exists():
        raise Unavailable(f"the live decoder service is not running (no socket at {path})")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise Unavailable(f"the live decoder service is not running ({exc.strerror})") from exc
        sock.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout as exc:
        raise LiveError(f"the live decoder service did not answer within {timeout:g} s") from exc
    finally:
        sock.close()
    try:
        resp = json.loads(data)
    except ValueError as exc:
        raise LiveError(f"the live decoder service sent an unreadable reply: {data[:200]!r}") from exc
    if not resp.get("ok"):
        raise LiveError(resp.get("error") or "the live decoder service refused the request")
    return resp


def pause(owner: str, lease_s: float) -> dict:
    """Ask the service to release the HackRF and keep it free for up to
    lease_s seconds. Returns the reply ({token, paused_profile, ...})."""
    return request({"cmd": "pause", "owner": owner, "lease_s": lease_s})


def resume(token: str) -> None:
    try:
        request({"cmd": "resume", "token": token}, timeout=5.0)
    except (Unavailable, LiveError):
        # The lease runs out on its own; nothing more to do.
        pass
