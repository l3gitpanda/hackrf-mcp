"""python -m hackrf_mcp.web: run hackrf-web (see hackrf_mcp/web/__init__.py)."""

from __future__ import annotations

import argparse
import re
import sys

from hackrf_mcp import settings
from hackrf_mcp.auth import MIN_TOKEN_LENGTH, token_digest
from hackrf_mcp.web.sessions import load_secret

_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def accepted_digests(web: settings.WebSettings) -> list[bytes]:
    digests = []
    if web.token_file is not None:
        token = web.token_file.read_text().strip()
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"the web token must be at least {MIN_TOKEN_LENGTH} characters")
        digests.append(token_digest(token))
    for value in web.token_sha256:
        if not _SHA256_HEX.match(value):
            raise ValueError("HACKRF_WEB_TOKEN_SHA256 entries must be 64 hex characters")
        digests.append(bytes.fromhex(value))
    return digests


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="hackrf-web: the web UI for live decoding.")
    parser.add_argument("--host", help="address to listen on (HACKRF_WEB_HOST, default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (HACKRF_WEB_PORT, default 8766)")
    args = parser.parse_args(argv)
    try:
        live = settings.LiveSettings.from_env()
        web = settings.WebSettings.from_env()
        digests = accepted_digests(web)
        if url := web.alert_webhook_url():
            from hackrf_mcp.web.alerts import check_webhook

            check_webhook(url, web.alert_webhook_format)
    except (ValueError, OSError) as exc:
        parser.error(f"invalid configuration: {exc}")
    if not digests:
        parser.error("hackrf-web needs a token: set HACKRF_WEB_TOKEN_FILE or HACKRF_WEB_TOKEN_SHA256")
    settings.configure_live(live)
    try:
        # The capture directory, where the live service draws each capture's picture.
        settings.configure(settings.Settings.from_env())
    except ValueError as exc:
        print(f"hackrf-web: the MCP server's settings are invalid ({exc}); capture pictures are looked for "
              "in the default capture directory", file=sys.stderr, flush=True)
    key = load_secret(web.state_path(live))

    import uvicorn

    from hackrf_mcp.web.app import create_app

    app = create_app(live, web, digests, key)
    host = args.host or web.host
    port = args.port or web.port
    print(f"hackrf-web: listening on {host}:{port}, {len(digests)} token(s) accepted", file=sys.stderr, flush=True)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        server_header=False,
        proxy_headers=False,
        ws_max_size=65536,
    )


if __name__ == "__main__":
    main()
