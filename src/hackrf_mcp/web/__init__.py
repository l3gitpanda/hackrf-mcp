"""hackrf-web: the web UI's backend (TODO.md, "Web UI").

A small Starlette app, run as its own service (deploy/hackrf-web.service)
with its own CPU and memory limits. It never opens the HackRF: it reads the
live service's log and spectrum history (read-only), relays its feed socket
to browsers over a websocket, and starts, switches and stops profiles over
its control socket, as the MCP's hackrf_live_* tools do. Everything needs a
token: a bearer header, or a session cookie a browser gets by signing in
with one. Pager text is removed on read unless the operator allows it.
"""
