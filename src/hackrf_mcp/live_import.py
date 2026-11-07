"""The worker's side of the split station's fallback (see agent.py).

A heartbeat to the hackrf-agent every 30 s tells the agent a worker is
around, so the agent keeps its machine's own live service paused. After each
heartbeat this imports what that machine logged while no worker was around:
messages, positions, and the service's start, stop and error events, with
their own timestamps. Its pauses and resumes are the agent's bookkeeping
and stay behind; so does the spectrum it recorded.

Imported rows go into this service's log through its LogPump, but not to
the feed (the web UI's live view): by the time they arrive they are history.
Pager rows come as they were stored there, and this service's redaction is
applied on top unless HACKRF_MCP_PAGER_CONTENT is on here.

The last id imported from each table is kept in agent-import.json in the
live directory, saved only after the rows before it are written. With no
such file, the first contact records the agent's current ids and imports
nothing: the log copied over when the station was split has the rest.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from hackrf_mcp import agent, live_log, live_profiles
from hackrf_mcp.live_log import Position, Row

HEARTBEAT_S = 30.0
MAX_RETRY_S = 60.0
PAGES_PER_ROUND = 20
TABLES = agent.EXPORT_TABLES
IMPORT_EVENTS = frozenset({"start", "stop", "error", "service-start", "service-stop"})
# Marks the imported events, so coverage can follow them on a lane of their own.
FALLBACK_NOTE = live_log.FALLBACK_NOTE


def redactor(pager_content: bool):
    """Each profile's redaction (pager text) unless the operator allows the
    content; the same rule the web UI applies on read."""
    live_profiles.load()

    def redact(row: dict) -> dict:
        if pager_content:
            return row
        profile = live_profiles.PROFILES.get(row.get("profile") or "")
        if profile is not None and profile.redact is not None:
            return profile.redact(row)
        return row

    return redact


def to_row(d: dict, redact) -> Row:
    try:
        fields = json.loads(d.get("fields") or "{}")
    except ValueError:
        fields = {}
    r = redact({**d, "fields": fields if isinstance(fields, dict) else {}})
    return Row(
        ts=r["ts"],
        profile=r["profile"],
        kind=r["kind"],
        fields=r["fields"],
        raw=r.get("raw"),
        freq_hz=r.get("freq_hz"),
        level_db=r.get("level_db"),
        subject=r.get("subject"),
        label=r.get("label"),
        ts_end=r.get("ts_end"),
        device=r.get("device") or "hackrf",
    )


def to_position(d: dict) -> Position:
    return Position(
        ts=d["ts"],
        profile=d["profile"],
        subject=d["subject"],
        lat=d["lat"],
        lon=d["lon"],
        alt=d.get("alt"),
        speed=d.get("speed"),
        track=d.get("track"),
    )


class AgentLink(threading.Thread):
    """Heartbeats to the hackrf-agent and imports its machine's fallback log.
    `pump` is the service's LogPump (anything with row, position, event and
    after_write)."""

    def __init__(
        self,
        pump,
        address: tuple[str, int],
        token_file: Path,
        mark_path: Path,
        pager_content: bool = False,
        every_s: float = HEARTBEAT_S,
    ):
        super().__init__(daemon=True, name="agent-link")
        self.pump = pump
        self.address = address
        self.token_file = Path(token_file)
        self.mark_path = Path(mark_path)
        self.redact = redactor(pager_content)
        self.every_s = every_s
        self._halt = threading.Event()
        self.lock = threading.Lock()
        self.mark: dict[str, int] | None = self._load()
        self.state: dict = {
            "address": f"{address[0]}:{address[1]}",
            "last_contact": None,
            "error": None,
            "error_at": None,
            "fallback": None,
            "imported": {t: 0 for t in TABLES},
            "last_import_at": None,
        }

    # --- the watermark ------------------------------------------------------

    def _load(self) -> dict[str, int] | None:
        try:
            data = json.loads(self.mark_path.read_text())
            return {t: int(data["since"].get(t, 0)) for t in TABLES}
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            print(f"hackrf-live: ignoring an unreadable {self.mark_path}: {exc!r}", file=sys.stderr, flush=True)
            return None

    def _commit(self, since: dict[str, int]) -> None:
        """Runs on the pump's thread once the rows before `since` are written."""
        tmp = self.mark_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"agent": self.state["address"], "since": since, "saved": time.time()}) + "\n")
        os.replace(tmp, self.mark_path)
        with self.lock:
            self.mark = dict(since)

    # --- one round ------------------------------------------------------------

    def round(self) -> None:
        token = self.token_file.read_text().strip()
        st = agent.status(self.address, token, timeout=10, heartbeat=True)
        with self.lock:
            # The agent's view of the stream, its queue included (agent.BUFFER_S).
            self.state.update(last_contact=time.time(), error=None, fallback=st.get("fallback"), stream=st.get("stream"))
            mark = None if self.mark is None else dict(self.mark)
        if st.get("fallback") is None:
            return  # the agent's machine keeps no log of its own
        if mark is None:
            _, done = agent.export(self.address, token, None, max_only=True)
            self._commit({t: int(done["max_ids"].get(t, 0)) for t in TABLES})
            return
        since = mark
        for _ in range(PAGES_PER_ROUND):
            rows, done = agent.export(self.address, token, since)
            counts = self._queue(rows)
            for t in TABLES:
                if t in done.get("last_ids", {}):
                    since[t] = int(done["last_ids"][t])
            reset = False
            for t in TABLES:
                if int(done["max_ids"].get(t, 0)) < since[t]:
                    # The log there was started afresh: its ids begin again.
                    since[t] = 0
                    reset = True
            self.pump.after_write(lambda s=dict(since): self._commit(s))
            with self.lock:
                for t, n in counts.items():
                    self.state["imported"][t] += n
                if rows:
                    self.state["last_import_at"] = time.time()
            if not (done.get("more") or reset):
                break

    def _queue(self, rows: list[dict]) -> dict[str, int]:
        counts = {t: 0 for t in TABLES}
        for item in rows:
            t, d = item.get("table"), item.get("row") or {}
            try:
                if t == "messages":
                    self.pump.row(to_row(d, self.redact))
                elif t == "positions":
                    self.pump.position(to_position(d))
                elif t == "events":
                    if d.get("event") not in IMPORT_EVENTS:
                        continue
                    detail = f"{d['detail']} ({FALLBACK_NOTE})" if d.get("detail") else FALLBACK_NOTE
                    self.pump.event(d["event"], d.get("profile"), detail, ts=d["ts"])
                else:
                    continue
            except (KeyError, TypeError) as exc:
                print(f"hackrf-live: skipping an unreadable {t} row from the agent: {exc!r}", file=sys.stderr, flush=True)
                continue
            counts[t] += 1
        return counts

    # --- the thread -------------------------------------------------------------

    def run(self) -> None:
        delay = 0.0
        failures = 0
        while not self._halt.wait(delay):
            try:
                self.round()
                failures = 0
                delay = self.every_s
            except Exception as exc:  # noqa: BLE001 - the heartbeat must outlive a bad reply
                if not isinstance(exc, (OSError, agent.AgentRefused)):
                    print(f"hackrf-live: agent link: {exc!r}", file=sys.stderr, flush=True)
                failures += 1
                with self.lock:
                    self.state.update(error=str(exc) or repr(exc), error_at=time.time())
                # Sooner than the heartbeat at first, so a restarted agent
                # hears from the worker again quickly.
                delay = min(5.0 * 2 ** (failures - 1), MAX_RETRY_S)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=15)

    def status(self) -> dict:
        with self.lock:
            return {**self.state, "imported": dict(self.state["imported"]), "since": self.mark}
