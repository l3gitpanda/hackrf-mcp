# Contributing

Thanks for your interest in hackrf-mcp!

## Development setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.10+.

```bash
git clone https://github.com/thehappydinoa/hackrf-mcp.git
cd hackrf-mcp
uv sync
uv run pytest
```

Tests mock out `hackrf_mcp.cli.run` and subprocess calls, so you do not need a HackRF device or `hackrf_tools` installed to run the suite.

## Making changes

- Match existing patterns in `src/hackrf_mcp/server.py` for new MCP tools (one tool per `hackrf_*` CLI command, shared `ToolAnnotations`, SAFETY notes for side effects).
- Leave return types unannotated on tools that may return `Image`/`Audio` alongside text (see `CLAUDE.md`).
- Add or update tests in `tests/` for behavior you change.
- Run `uv run pytest` before opening a pull request.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep changes focused; include tests when behavior changes.
3. Open a PR with a short description of what changed and why.

## Reporting issues

Please include your OS, Python version, how you installed `hackrf_tools`, and the full error output when filing a bug. For device-specific problems, the output of `hackrf_info` is helpful.
