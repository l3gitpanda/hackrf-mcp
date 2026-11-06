"""Captures from the web UI (captures.py): planning, recording through the
live service's hold, decoding, the files, and the web routes."""

import hashlib
import json
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from hackrf_mcp import agent, captures, cli, decoders, live_client, live_log, server, settings
from hackrf_mcp.captures import MIB, Plan, PlanError, Store
from hackrf_mcp.live_service import ControlServer, LiveService, start_captures
from hackrf_mcp.web import app as web_app

from test_live_service import Clock, FakePipeline

FAKE_TRANSFER = Path(__file__).parent.parent / "tools" / "fake_hackrf_transfer.py"
T0 = 1_793_923_200.0  # 2026-11-06 00:00:00 UTC
# A frequency for the decoders that have no usual channels of their own.
SAMPLE_FREQ = {"pocsag": 152_007_500, "flex": 929_612_500, "dtmf": 146_520_000, "rds": 98_100_000, "lora": 915_000_000}
LIMITS = dict(max_seconds=60, max_bytes=2048 * MIB)


def write_cs8(path: Path, rate: int, seconds: float, tone_hz: float = 0.0) -> None:
    n = int(rate * seconds)
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.05
    x += 0.3 * np.exp(2j * np.pi * tone_hz / rate * np.arange(n))
    out = np.empty(2 * n, np.int8)
    out[0::2] = np.clip(np.rint(x.real * 127), -127, 127)
    out[1::2] = np.clip(np.rint(x.imag * 127), -127, 127)
    path.write_bytes(out.tobytes())


# --- planning ------------------------------------------------------------------------


def _pick(d: decoders.Decoder) -> int:
    return d.default_freqs_hz[0] if d.default_freqs_hz else SAMPLE_FREQ[d.name]


@pytest.mark.parametrize("name", decoders.names())
def test_every_decoder_takes_the_capture_planned_for_it(name, tmp_path, monkeypatch):
    """The plan's rate and centre pass the decoder's own checks: run()
    reaches its program (stubbed) without a DecodeError."""
    d = decoders.DECODERS[name]
    f = _pick(d)
    p = captures.plan(f, f, [name], 30, **LIMITS)
    assert d.rate_ok(p.sample_rate_hz) and p.covers()[0] <= f <= p.covers()[1]
    if d.tune_offset_hz and not d.usual_center_hz:
        assert abs(f - p.center_hz) >= d.tune_offset_hz  # off the DC spike
    reached = []
    monkeypatch.setattr(decoders, "find_program", lambda dec: decoders.BUILT_IN if not dec.programs else "/bin/true")

    def fake_run(args, stdin_chunks, timeout, workdir):
        for _ in stdin_chunks or ():
            break
        reached.append(args)
        return decoders.RunResult("", 0, False, 0.0)

    monkeypatch.setattr(decoders, "run_program", fake_run)
    path = tmp_path / "c.cs8"
    write_cs8(path, p.sample_rate_hz, 0.05)
    freqs = dict(p.decoders)[name]
    decoders.decode(name, path, p.sample_rate_hz, p.center_hz, freqs, None, 10, tmp_path / "work")
    assert reached or not d.programs


def test_a_tap_on_a_pager_tunes_below_it_and_passes_the_frequency():
    p = captures.plan(152_007_500, 152_007_500, ["pocsag", "flex"], 30, **LIMITS)
    assert p.sample_rate_hz == 2_000_000 and p.center_hz == 152_007_500 - 40_000
    assert p.decoders == (("pocsag", (152_007_500,)), ("flex", (152_007_500,)))
    assert p.bytes == 120_000_000 and "40 kHz below" in p.notes[0]


def test_the_running_profiles_channels_are_passed_when_inside_the_range():
    known = {"pocsag": (152_007_500, 152_240_000, 157_450_000)}
    p = captures.plan(152_000_000, 152_300_000, ["pocsag"], 30, known=known, **LIMITS)
    assert dict(p.decoders)["pocsag"] == (152_007_500, 152_240_000)


def test_a_decoder_with_a_recipe_centre_uses_it_when_it_holds_the_range():
    p = captures.plan(162_025_000, 162_025_000, ["ais"], 30, **LIMITS)
    assert p.center_hz == 162_000_000 and p.decoders == (("ais", None),)
    p = captures.plan(1_090_000_000, 1_090_000_000, ["adsb"], 30, **LIMITS)
    assert (p.center_hz, p.sample_rate_hz) == (1_090_000_000, 2_400_000)


def test_a_pinned_centre_must_hold_the_range():
    p = captures.plan(978_100_000, 978_100_000, ["uat"], 10, **LIMITS)
    assert (p.center_hz, p.sample_rate_hz) == (978_000_000, 2_083_333)
    with pytest.raises(PlanError, match="uat needs the capture centred on 978 MHz"):
        captures.plan(980_000_000, 980_000_000, ["uat"], 10, **LIMITS)


def test_decoders_that_need_different_rates_are_refused():
    with pytest.raises(PlanError, match=r"adsb records at 2.4 MS/s and vdl2 at 2.1 MS/s.*tick one"):
        captures.plan(1_090_000_000, 1_090_000_000, ["adsb", "vdl2"], 30, **LIMITS)


def test_a_decoder_whose_channels_are_elsewhere_is_refused():
    with pytest.raises(PlanError, match="ais decodes 161.975 MHz, 162.025 MHz"):
        captures.plan(150_000_000, 150_000_000, ["ais"], 30, **LIMITS)


def test_no_decoder_takes_the_narrowest_rate_holding_the_range():
    p = captures.plan(100_000_000, 100_000_000, [], 10, **LIMITS)
    assert p.sample_rate_hz == 2_000_000 and p.center_hz == 100_000_000 - captures.DEFAULT_OFFSET_HZ
    p = captures.plan(96_000_000, 102_000_000, [], 10, **LIMITS)
    assert p.sample_rate_hz == 8_000_000 and p.center_hz == 99_000_000
    with pytest.raises(PlanError, match="wider than a capture at 20 MS/s holds"):
        captures.plan(90_000_000, 108_000_000, [], 10, **LIMITS)


def test_length_size_and_gains_are_checked():
    with pytest.raises(PlanError, match="1 to 60 s, not 61"):
        captures.plan(1e8, 1e8, [], 61, **LIMITS)
    with pytest.raises(PlanError, match="over this station's 100 MiB for one capture"):
        captures.plan(1e8, 1e8, [], 60, max_seconds=60, max_bytes=100 * MIB)
    with pytest.raises(PlanError, match="LNA gain is 0-40 dB in steps of 8"):
        captures.plan(1e8, 1e8, [], 10, lna_gain_db=12, **LIMITS)
    with pytest.raises(PlanError, match="outside what the HackRF receives"):
        captures.plan(500_000, 500_000, [], 10, **LIMITS)
    p = captures.plan(1e8, 1e8, [], 10, lna_gain_db=24, vga_gain_db=30, amp=True, **LIMITS)
    assert (p.lna_gain_db, p.vga_gain_db, p.amp) == (24, 30, True)
    assert Plan.from_json(json.loads(json.dumps(p.to_json()))) == p


def test_suggestions_are_the_decoders_usually_near_the_range():
    assert captures.suggest(1_090_000_000, 1_090_000_000) == ["adsb"]
    assert captures.suggest(162_010_000, 162_010_000) == ["ais"]
    assert "eas" in captures.suggest(162_450_000, 162_450_000)
    assert captures.suggest(152_007_500, 152_007_500) == []
    assert captures.suggest(152_007_500, 152_007_500, known={"pocsag": (152_007_500,)}) == ["pocsag"]
    assert captures.suggest(1_090_000_000, 1_090_000_000, installed=set()) == []
    # Anywhere in the band the running profile is tuned to suggests its decoder.
    band = {"profile": "adsb", "lo_hz": 1_088_800_000, "hi_hz": 1_091_200_000}
    assert captures.suggest(1_090_900_000, 1_090_900_000, band=band) == ["adsb"]
    assert captures.suggest(1_092_000_000, 1_092_000_000, band=band) == []


def test_fit_checks_a_decoder_against_a_capture_already_made():
    p = captures.plan(152_007_500, 152_007_500, ["pocsag"], 30, **LIMITS)
    assert captures.fit(p, ["flex"]) == (("flex", (152_007_500,)),)
    with pytest.raises(PlanError, match="adsb needs 2.4 MS/s"):
        captures.fit(p, ["adsb"])


# --- the files ---------------------------------------------------------------------------


def test_store_prunes_by_age_then_size_and_keeps_work_in_progress(tmp_path):
    import os

    st = Store(tmp_path / "web")
    st.ensure()
    ids = []
    for i in range(4):
        cid = st.new_id(T0 + i * 3600)
        ids.append(cid)
        st.save({"id": cid, "created": T0 + i * 3600})
        st.iq_path(cid).write_bytes(b"\0" * 1000)
        for path in (st.iq_path(cid), st.meta_path(cid)):
            os.utime(path, (T0 + i * 3600, T0 + i * 3600))
    assert st.count() == 4 and [r["id"] for r in st.records()] == ids[::-1]
    # The oldest is past 3.5 hours; then the oldest not in use goes, to fit 1500 bytes more in 3700.
    gone = st.prune(T0 + 3 * 3600 + 1800 + 1, 3.5 * 3600, 3700, need_bytes=1500, keep={ids[1]})
    assert gone == [ids[0], ids[2]]
    assert {r["id"] for r in st.records()} == {ids[1], ids[3]}
    with pytest.raises(ValueError):
        st.iq_path("../etc/passwd")


# --- the recorder in the live service ------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    pipes = []

    def make(tuning, ctx):
        pipe = FakePipeline(tuning, ctx)
        pipes.append(pipe)
        return pipe

    clock = Clock(T0 + 100)
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    s = LiveService(live, None, make_pipeline=make, program_finder=lambda p: "/bin/true", clock=clock)
    s.pipes, s.test_clock, s.events = pipes, clock, []
    s._event = lambda event, profile=None, detail=None: s.events.append((event, profile, detail))
    s.handle({"cmd": "start", "profile": "adsb"})
    return s


def recorder(svc, tmp_path, run_capture=None, run_decode=None, **kw):
    seen = {"capture": [], "decode": []}

    def capture(p, path):
        seen["capture"].append({"plan": p, "pipeline": svc.pipeline, "hold": dict(svc.hold) if svc.hold else None})
        write_cs8(path, p.sample_rate_hz, 0.01)
        return {"exit_code": 0, "stderr": "", "timed_out": False}

    def decode(path, p, todo, picture):
        seen["decode"].append({"todo": todo, "picture": picture, "hold": svc.hold})
        return {"decodes": [{"decoder": n, "state": "done", "headline": f"{n}: 2 messages.", "notes": [],
                             "table": [], "messages": ["a", "b"]} for n, _ in todo], "picture": picture is not None}  # fmt: skip

    kw.setdefault("wait_s", 30)
    r = captures.Recorder(
        svc,
        Store(tmp_path / "captures" / "web"),
        max_seconds=60,
        max_bytes=2048 * MIB,
        min_free_bytes=0,
        keep_bytes=4096 * MIB,
        keep_age_s=7 * 86400,
        run_capture=run_capture or capture,
        run_decode=run_decode or decode,
        clock=svc.test_clock,
        poll_s=0.001,
        spawn=lambda fn, *a: fn(*a),
        available=lambda: {n: "/bin/true" for n in decoders.names()},
        **kw,
    )
    svc.captures = r
    return r, seen


def test_a_capture_takes_the_receiver_records_gives_it_back_and_decodes(svc, tmp_path):
    rec, seen = recorder(svc, tmp_path)
    out = svc.handle({"cmd": "capture", "from_hz": 162_025_000, "decoders": ["ais"], "seconds": 20})
    assert out["ok"], out
    cid = out["capture"]["id"]
    # While recording: no decoder, the hold the capture's.
    assert seen["capture"][0]["pipeline"] is None and seen["capture"][0]["hold"]["owner"] == captures.OWNER
    assert svc.pipes[0].stopped and svc.hold is None
    # Decoding ran after the receiver was back, with the picture.
    assert seen["decode"][0]["hold"] is None and seen["decode"][0]["todo"] == (("ais", None),)
    assert seen["decode"][0]["picture"] == rec.store.picture_path(cid)
    svc.supervise()
    assert svc.status()["state"] == "running" and len(svc.pipes) == 2
    names = [e[0] for e in svc.events]
    assert names.count("capture-start") == 1 and "capture-done" in names and "capture-decoded" in names
    assert "pause" not in names and "resume" not in names
    stop = next(e for e in svc.events if e[0] == "stop")
    assert stop[2] == "paused for web capture" and live_log._after(*stop, "running")[0] == "paused"
    done = next(e for e in svc.events if e[0] == "capture-done")[2]
    assert done.startswith(cid) and "adsb paused" in done
    saved = rec.store.load(cid)
    assert saved["state"] == "done" and saved["paused"] == "adsb" and saved["bytes"] > 0
    assert saved["decodes"][0]["headline"] == "ais: 2 messages." and saved["picture"] is True
    listed = svc.handle({"cmd": "capture-list"})
    assert listed["captures"][0]["id"] == cid and listed["status"]["count"] == 1
    st = svc.status()["captures"]
    assert st["enabled"] and st["recorded"] == 1 and st["current"] is None


def test_a_capture_borrows_the_receiver_from_a_watch(svc, tmp_path):
    svc.handle({"cmd": "watch", "center_hz": 100_000_000, "minutes": 15})
    until = svc.watch["until"]
    rec, seen = recorder(svc, tmp_path)
    plan = svc.handle({"cmd": "capture-plan", "from_hz": 100_300_000, "decoders": [], "seconds": 10})
    assert plan["error"] is None and any("watch pauses" in e for e in plan["effects"])
    out = svc.handle({"cmd": "capture", "from_hz": 100_300_000, "decoders": [], "seconds": 10})
    assert out["ok"] and seen["capture"][0]["hold"]["stopped"] == "watch"
    assert svc.watch is not None and svc.watch["until"] == until
    svc.supervise()
    assert svc.running_spec.profile == "watch"
    assert rec.store.load(out["capture"]["id"])["paused"] == "watch"


def test_a_capture_waits_for_a_device_tool_then_gives_up(svc, tmp_path):
    rec, seen = recorder(svc, tmp_path, wait_s=5)
    svc.handle({"cmd": "pause", "owner": "hackrf_sweep", "lease_s": 600})
    clock = svc.test_clock
    take = svc.capture_take

    def slow_take(lease):
        clock.t += 2
        return take(lease)

    svc.capture_take = slow_take
    out = svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": [], "seconds": 10})
    saved = rec.store.load(out["capture"]["id"])
    assert saved["state"] == "failed" and "lent to hackrf_sweep for over 5 s" in saved["error"]
    assert not seen["capture"] and svc.hold["owner"] == "hackrf_sweep"
    assert svc.events[-1][0] == "capture-error"


def test_a_capture_waits_out_an_occupancy_sweep(svc, tmp_path):
    rec, seen = recorder(svc, tmp_path)
    r = svc.occupancy_take(60)
    tries = []
    take = svc.capture_take

    def take_after_sweep(lease):
        tries.append(1)
        if len(tries) == 3:
            svc.occupancy_give(r["token"])
        return take(lease)

    svc.capture_take = take_after_sweep
    out = svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": [], "seconds": 10})
    assert len(tries) == 3 and rec.store.load(out["capture"]["id"])["state"] == "done"


def test_a_device_tool_is_refused_while_a_capture_records(svc, tmp_path):
    answers = []

    def capture(p, path):
        answers.append(svc.handle({"cmd": "pause", "owner": "hackrf_sweep", "lease_s": 60}))
        answers.append(svc.occupancy_take(60))
        write_cs8(path, p.sample_rate_hz, 0.01)
        return {"exit_code": 0}

    rec, _ = recorder(svc, tmp_path, run_capture=capture)

    def no_recorder_under_the_service_lock():
        raise AssertionError("the refusal must not read the recorder under the service's lock")

    rec.status = no_recorder_under_the_service_lock
    svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": [], "seconds": 10})
    assert answers[0]["ok"] is False and answers[0]["busy"] is True
    assert "recording a capture from the web UI for about 10 s more" in answers[0]["error"]
    assert answers[1] == {"state": "busy", "owner": captures.OWNER}
    assert svc.hold is None


def test_the_mcp_server_reports_a_busy_hackrf_while_a_capture_records(svc, tmp_path, monkeypatch):
    replies = []

    def capture(p, path):
        replies.append(server._run_on_device("hackrf_info", [], timeout=5))
        write_cs8(path, p.sample_rate_hz, 0.01)
        return {"exit_code": 0}

    recorder(svc, tmp_path, run_capture=capture)

    def request(req, timeout=20.0, path=None):
        resp = svc.handle(req)
        if not resp.get("ok"):
            raise (live_client.LiveBusy if resp.get("busy") else live_client.LiveError)(resp["error"])
        return resp

    monkeypatch.setattr(live_client, "request", request)
    svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": [], "seconds": 10})
    assert replies[0].startswith("HackRF is busy: the HackRF is recording a capture from the web UI")


def test_a_failed_capture_still_gives_the_receiver_back(svc, tmp_path):
    def boom(p, path):
        path.write_bytes(b"\1\2")
        raise ConnectionError("the agent went away")

    rec, seen = recorder(svc, tmp_path, run_capture=boom)
    out = svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": ["pocsag"], "seconds": 10})
    saved = rec.store.load(out["capture"]["id"])
    assert saved["state"] == "failed" and "could not reach the HackRF: the agent went away" in saved["error"]
    assert svc.hold is None and not rec.store.iq_path(out["capture"]["id"]).exists() and not seen["decode"]


def test_one_capture_at_a_time(svc, tmp_path):
    second = []

    def capture(p, path):
        second.append(svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": [], "seconds": 10}))
        write_cs8(path, p.sample_rate_hz, 0.01)
        return {"exit_code": 0}

    recorder(svc, tmp_path, run_capture=capture)
    assert svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": [], "seconds": 10})["ok"]
    assert second[0] == {"ok": False, "error": "a capture is already being recorded; wait for it to finish"}


def test_more_decoders_run_later_and_deleting(svc, tmp_path):
    rec, seen = recorder(svc, tmp_path)
    cid = svc.handle({"cmd": "capture", "from_hz": 152_007_500, "decoders": ["pocsag"], "seconds": 10})["capture"]["id"]
    out = svc.handle({"cmd": "capture-decode", "id": cid, "decoders": ["flex"]})
    assert out["ok"] and seen["decode"][-1] == {"todo": (("flex", (152_007_500,)),), "picture": None, "hold": None}
    saved = rec.store.load(cid)
    assert [d["decoder"] for d in saved["decodes"]] == ["pocsag", "flex"] and saved["state"] == "done"
    assert svc.handle({"cmd": "capture-decode", "id": cid, "decoders": ["adsb"]}) == {
        "ok": False, "error": "adsb needs 2.4 MS/s; this capture is 2 MS/s"}  # fmt: skip
    more = svc.handle({"cmd": "capture-list"})["captures"][0]["more"]
    assert "dtmf" in more and "pocsag" not in more and "flex" not in more and "adsb" not in more and "uat" not in more
    gone = svc.handle({"cmd": "capture-delete", "id": cid})
    assert gone["ok"] and gone["freed_bytes"] > 0 and rec.store.load(cid) is None
    assert svc.handle({"cmd": "capture-delete", "id": "../x"})["ok"] is False


def test_the_plan_says_what_it_would_stop(svc, tmp_path):
    rec, _ = recorder(svc, tmp_path)

    class Slots:
        next_at = T0 + 110

        def status(self):
            return {"enabled": True}

    svc.occupancy = Slots()
    out = svc.handle({"cmd": "capture-plan", "from_hz": 1_090_700_000, "seconds": 30})
    assert out["ok"] and out["suggested"] == ["adsb"] and out["plan"]["center_hz"] == 1_090_000_000
    effects = " ".join(out["effects"])
    assert "Decoding of adsb stops for about 35 s." in effects
    assert "occupancy sweep due at 00:01:50 UTC waits until the capture ends" in effects
    assert out["limits"]["max_seconds"] == 60 and out["decoders"]


def test_the_plan_reports_an_error_rather_than_failing(svc, tmp_path):
    recorder(svc, tmp_path)
    out = svc.handle({"cmd": "capture-plan", "from_hz": 980e6, "decoders": ["uat"], "seconds": 10})
    assert out["ok"] and out["plan"] is None and "uat needs the capture centred on 978 MHz" in out["error"]


def test_captures_off(svc):
    assert svc.handle({"cmd": "capture-list"}) == {
        "ok": False, "error": "captures from the web UI are off on this station (HACKRF_LIVE_CAPTURES)"}  # fmt: skip
    assert svc.status()["captures"] == {"enabled": False}
    assert "Captures from the web UI: off" in server._format_live_status(svc.status())


def test_the_mcp_status_describes_captures(svc, tmp_path):
    recorder(svc, tmp_path)
    svc.handle({"cmd": "capture", "from_hz": 100e6, "decoders": [], "seconds": 10})
    text = server._format_live_status(svc.status())
    assert "Captures from the web UI: up to 60 s each" in text and "1 recorded and 0 failed" in text


def test_start_captures_uses_the_mcp_servers_limits(svc, tmp_path):
    s = settings.Settings(data_dir=tmp_path / "data", max_rx_seconds=20, max_capture_mb=100, min_free_mb=0)
    r = start_captures(svc, replace_live(svc.live, capture_keep_days=3), server=s)
    assert r.max_seconds == 20 and r.max_bytes == 100 * MIB and r.keep_age_s == 3 * 86400
    assert r.store.root == tmp_path / "data" / "web" and svc.captures is r


def replace_live(live, **kw):
    from dataclasses import replace

    return replace(live, **kw)


def test_capture_settings_from_the_environment():
    s = settings.LiveSettings.from_env({
        "HACKRF_LIVE_CAPTURES": "off", "HACKRF_LIVE_CAPTURE_MAX_S": "30", "HACKRF_LIVE_CAPTURE_KEEP_DAYS": "2",
        "HACKRF_LIVE_CAPTURE_KEEP_MB": "1000", "HACKRF_LIVE_CAPTURE_WAIT_S": "10",
    })  # fmt: skip
    assert (s.captures, s.capture_max_s, s.capture_keep_days, s.capture_keep_mb, s.capture_wait_s) == (False, 30, 2, 1000, 10)
    assert settings.LiveSettings().captures is True
    with pytest.raises(ValueError, match="HACKRF_LIVE_CAPTURE_WAIT_S"):
        settings.LiveSettings(capture_wait_s=1000)


# --- recording for real: the fake hackrf_transfer, here and through an agent -------------


@pytest.fixture
def transfer_tools(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "hackrf_transfer").write_text(f"#!{sys.executable}\n" + FAKE_TRANSFER.read_text())
    (d / "hackrf_transfer").chmod(0o755)
    return d


def test_capturing_on_this_machine(tmp_path, transfer_tools):
    cli.set_bin_dir(transfer_tools)
    try:
        p = captures.plan(100e6, 100e6, [], 1, **LIMITS)
        info = captures.capture_runner(settings.LiveSettings(live_dir=tmp_path))(p, tmp_path / "c.cs8")
        assert info["exit_code"] == 0 and (tmp_path / "c.cs8").stat().st_size == p.bytes
    finally:
        cli.set_bin_dir(None)


def test_capturing_through_a_hackrf_agent(tmp_path, transfer_tools):
    token = "capture-test-token"
    (tmp_path / "agent-token").write_text(token)
    a = agent.AgentSettings(
        host="127.0.0.1", port=0, token_sha256=(hashlib.sha256(token.encode()).hexdigest(),),
        binary=str(transfer_tools / "hackrf_transfer"), tools_dir=str(transfer_tools),
    )  # fmt: skip
    a.digests = a.load_digests()
    srv = agent.AgentServer(a)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        live = settings.LiveSettings(live_dir=tmp_path / "live", agent=srv.server_address[:2], agent_token_file=tmp_path / "agent-token")
        p = captures.plan(100e6, 100e6, [], 1, **LIMITS)
        done = captures.capture_runner(live)(p, tmp_path / "c.cs8")
        assert done["exit_code"] == 0 and (tmp_path / "c.cs8").stat().st_size == p.bytes
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_decoding_process_runs_decoders_and_draws_the_picture(tmp_path):
    """The real child process, with the built-in LoRa decoder (no program
    needed) on noise and a tone: nothing decoded, and a picture."""
    p = captures.plan(915_000_000, 915_000_000, ["lora"], 1, **LIMITS)
    path = tmp_path / "c.cs8"
    write_cs8(path, p.sample_rate_hz, 0.2, tone_hz=300_000)
    live = settings.LiveSettings(live_dir=tmp_path)
    out = captures.decode_runner(live, tmp_path / "work")(path, p, p.decoders, tmp_path / "c.png")
    assert out["picture"] is True and (tmp_path / "c.png").read_bytes()[:4] == b"\x89PNG"
    (entry,) = out["decodes"]
    assert entry["decoder"] == "lora" and entry["state"] == "done" and "headline" in entry
    assert not any(n.startswith(captures.RAW_MARK) for n in entry["notes"])


def test_a_decode_error_is_reported_per_decoder(tmp_path):
    path = tmp_path / "c.cs8"
    write_cs8(path, 2_000_000, 0.01)
    out = captures.run_job({
        "path": str(path), "sample_rate_hz": 2_000_000, "center_hz": 100_000_000, "decoders": [["lora", []]],
        "picture": None, "timeout_s": 5, "workroot": str(tmp_path / "w"), "pager_content": False, "receiver": None,
    })  # fmt: skip
    assert out["decodes"][0]["state"] == "failed" and "lora needs the channel frequencies" in out["decodes"][0]["error"]


def test_summaries_leave_the_programs_output_out():
    r = decoders.Result("x: nothing decoded.", [], [], "RAW", ["a note", captures.RAW_MARK, "raw tail"])
    s = captures.summarise(r)
    assert s["notes"] == ["a note"] and "raw" not in json.dumps(s).lower()


# --- the web routes -----------------------------------------------------------------------


@pytest.fixture
def web(tmp_path, monkeypatch):
    calls = []
    data = tmp_path / "data"
    store = Store(data / "web")
    store.ensure()

    def control(req, timeout=20.0):
        calls.append(req)
        if req["cmd"] == "capture-list":
            return {"ok": True, "captures": [{"id": "20261106T000000Z-abcd"}], "status": {"count": 1}}
        if req["cmd"] == "capture-delete" and req["id"] == "20261106T000000Z-dead":
            raise live_client.LiveError("that capture is still being recorded or decoded")
        return {"ok": True, "echo": req}

    monkeypatch.setattr(settings, "_current", settings.Settings(data_dir=data))
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    token = "t" * 32
    app = web_app.create_app(
        live, settings.WebSettings(state_dir=tmp_path / "state"), [hashlib.sha256(token.encode()).digest()], b"k" * 32,
        control=control, feed=False, own_stats=lambda: None,
    )  # fmt: skip
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + token
    return client, calls, store


def test_the_capture_api_asks_the_live_service(web):
    client, calls, store = web
    r = client.post("/api/captures/plan", json={"from_hz": 1e8, "to_hz": 1e8, "decoders": ["pocsag"], "seconds": 30})
    assert r.status_code == 200 and calls[-1] == {
        "cmd": "capture-plan", "from_hz": 1e8, "to_hz": 1e8, "decoders": ["pocsag"], "seconds": 30}  # fmt: skip
    r = client.post("/api/captures", json={"from_hz": 1e8, "origin": "history", "evil": 1})
    assert r.status_code == 200 and calls[-1] == {"cmd": "capture", "from_hz": 1e8, "origin": "history"}
    assert client.get("/api/captures").json()["captures"][0]["id"] == "20261106T000000Z-abcd"
    r = client.post("/api/captures/20261106T000000Z-abcd/decode", json={"decoders": ["flex"]})
    assert calls[-1] == {"cmd": "capture-decode", "id": "20261106T000000Z-abcd", "decoders": ["flex"]}
    assert client.post("/api/captures/20261106T000000Z-abcd/delete").status_code == 200
    r = client.post("/api/captures/20261106T000000Z-dead/delete")
    assert r.status_code == 400 and "still being recorded" in r.json()["error"]
    assert client.post("/api/captures/..%2Fx/delete").status_code in (400, 404)


def test_the_picture_is_served_but_never_the_samples(web):
    client, calls, store = web
    cid = "20261106T000000Z-abcd"
    store.picture_path(cid).write_bytes(b"\x89PNG fake")
    store.iq_path(cid).write_bytes(b"\0" * 10)
    r = client.get(f"/api/captures/{cid}/picture")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == b"\x89PNG fake"
    assert client.get("/api/captures/20261106T000000Z-ffff/picture").status_code == 404
    assert client.get(f"/api/captures/{cid}/samples").status_code == 404
    assert client.get(f"/api/captures/{cid}.cs8").status_code == 404
    assert client.get("/api/captures/x/picture").status_code == 400
