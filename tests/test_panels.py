"""Panels (web/panels.py): data-only files checked against the registry,
run over a real live log; pager text shown only when the operator allows it."""

import json

import pytest

from hackrf_mcp import live_profiles
from hackrf_mcp.live_log import LiveLogWriter, Row
from hackrf_mcp.web import panels

SECRET_TEXT = "MEET AT THE DOCK 0300"
NOW = 2_000_000_000.0

SQUAWK_BOARD = """
[panel]
title = "Squawk board"
slot = "summaries.card"
render = "table"

[query]
profiles = ["adsb"]
fields = ["squawk", "callsign"]
group_by = "squawk"
sort = "rows"
limit = 5
"""


def _aircraft(ts, icao, squawk, callsign="UAL1"):
    return Row(ts=ts, ts_end=ts + 50, profile="adsb", kind="aircraft", subject=icao, label=callsign,
               fields={"squawk": squawk, "callsign": callsign, "messages": 10}, raw=f"{icao} raw")


@pytest.fixture
def log(tmp_path):
    live_profiles.load()
    w = LiveLogWriter(tmp_path / "live.db", 86400 * 365 * 100, 1 << 30, 0)
    w.write([
        _aircraft(NOW - 60, "A1", "1200"),
        _aircraft(NOW - 50, "B2", "1200", "SWA9"),
        _aircraft(NOW - 40, "C3", "7700", "N4127P"),
        _aircraft(NOW - 30, "A1", "1200"),
        _aircraft(NOW - 9000, "OLD", "7500"),  # outside a 1 h window
        Row(ts=NOW - 20, profile="flex", kind="page", subject="1234567", label="ALN",
            fields={"type": "ALN", "message": SECRET_TEXT}, raw=f"FLEX|{SECRET_TEXT}"),
    ])
    from hackrf_mcp import live_log
    return live_log.open_reader(tmp_path / "live.db")


def _write(folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def test_panel_files_are_checked_and_bad_ones_reported(tmp_path):
    live_profiles.load()
    d = tmp_path / "panels"
    _write(d, "squawk-board.toml", SQUAWK_BOARD)
    _write(d, "broken.toml", "[panel\ntitle = ")
    _write(d, "bad-slot.toml", SQUAWK_BOARD.replace("summaries.card", "map.side"))
    _write(d, "raw.toml", SQUAWK_BOARD.replace('["squawk", "callsign"]', '["raw"]'))
    _write(d, "pager.toml", SQUAWK_BOARD.replace('["adsb"]', '["flex"]').replace('["squawk", "callsign"]', '["message"]').replace('group_by = "squawk"', ""))
    _write(d, "nope.toml", SQUAWK_BOARD.replace('["adsb"]', '["nope"]'))
    _write(d, "big.toml", SQUAWK_BOARD + "#" * 70_000)
    _write(d, "notes.txt", "not a panel")
    loaded = panels.load(d, pager_content=False)  # pager text not allowed
    assert [p.id for p in loaded.panels] == ["squawk-board"]
    errors = {e["file"]: e["error"] for e in loaded.errors}
    assert set(errors) == {"broken.toml", "bad-slot.toml", "raw.toml", "pager.toml", "nope.toml", "big.toml"}
    assert "slot" in errors["bad-slot.toml"] and "pager text" in errors["pager.toml"] and "64 KiB" in errors["big.toml"]
    p = loaded.panels[0]
    assert p.kinds == ("aircraft",) and p.window_s is None and p.rule is None
    assert panels.load(tmp_path / "missing", False).panels == []
    with pytest.raises(ValueError, match="window"):
        panels.parse({"panel": {"title": "t", "slot": "summaries.card"}, "query": {"profiles": ["adsb"], "window": "8d"}}, "x", False)


def test_a_panel_groups_sorts_filters_and_keeps_to_its_window(log):
    p = panels.parse(__import__("tomllib").loads(SQUAWK_BOARD), "squawk-board", False)
    out = panels.run(log, p, NOW - 86400, NOW)
    assert out["total"] == 3  # 1200, 7700, 7500 within the day
    assert [r["key"] for r in out["rows"]] == ["1200", "7700", "7500"]  # most rows first
    assert out["rows"][0]["rows"] == 3 and out["rows"][0]["messages"] == 30
    assert out["rows"][1]["fields"] == {"squawk": "7700", "callsign": "N4127P"}
    emergencies = panels.parse(
        {
            "panel": {"title": "Emergencies", "slot": "summaries.card", "render": "list"},
            "query": {
                "profiles": ["adsb"],
                "fields": ["squawk"],
                "window": "1h",
                "filter": [{"field": "squawk", "op": "any_of", "values": ["7500", "7600", "7700"]}],
            },
        },
        "emergencies",
        False,
    )
    out = panels.run(log, emergencies, NOW - 7 * 86400, NOW)
    # The panel's own hour, not the screen's week: OLD (7500) is outside it.
    assert [r["key"] for r in out["rows"]] == ["C3"] and out["since"] == NOW - 3600
    assert out["scanned"] == 1  # the prefilter read only the row holding "7700"


def test_a_pager_panel_shows_the_text_only_when_allowed(log):
    p = panels.parse(
        {"panel": {"title": "Pages", "slot": "summaries.card"}, "query": {"profiles": ["flex"], "fields": ["type"]}},
        "pages",
        True,
    )
    out = panels.run(log, p, NOW - 3600, NOW)
    assert out["rows"][0]["key"] == "1234567" and out["rows"][0]["fields"] == {"type": "ALN"}
    assert SECRET_TEXT not in json.dumps(out)
    # A filter on the text, and the text itself, with pager content on.
    p2 = panels.parse(
        {"panel": {"title": "Dock", "slot": "summaries.card"},
         "query": {"profiles": ["flex"], "fields": ["type", "message"], "filter": [{"field": "message", "op": "matches", "values": ["*dock*"]}]}},
        "dock",
        True,
    )
    out = panels.run(log, p2, NOW - 3600, NOW)
    assert out["total"] == 1 and out["rows"][0]["fields"]["message"] == SECRET_TEXT
    doc = {"panel": {"title": "Dock", "slot": "summaries.card"},
           "query": {"profiles": ["flex"], "filter": [{"field": "message", "op": "matches", "values": ["*"]}]}}
    with pytest.raises(ValueError):
        panels.parse(doc, "dock", False)  # no pager content: not even a filter on it


def test_the_example_panels_load():
    from pathlib import Path

    examples = Path(__file__).resolve().parents[1] / "deploy" / "panels"
    loaded = panels.load(examples, pager_content=False)
    assert loaded.errors == [] and len(loaded.panels) == len(list(examples.glob("*.toml"))) >= 1
