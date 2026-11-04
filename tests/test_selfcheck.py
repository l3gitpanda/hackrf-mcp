from hackrf_mcp import decoders, live_profiles, selfcheck


def test_selfcheck_requires_every_live_and_capture_decoder(monkeypatch):
    monkeypatch.setattr(live_profiles, "available", lambda: {"adsb": "/usr/bin/dump1090-mutability", "ais": None})
    monkeypatch.setattr(decoders, "available", lambda: {"adsb": "/usr/bin/dump1090-mutability", "uat": None, "rds": None, "acars": None})
    monkeypatch.setattr(selfcheck.shutil, "which", lambda name: None)  # no ldd: skip the library check
    lines, problems = selfcheck.check()
    # The image builds redsea and dump978-fa, so uat and rds are required too.
    assert problems == ["missing  live profile ais", "missing  decoder uat", "missing  decoder rds", "missing  decoder acars"]


def test_selfcheck_still_reports_optional_decoders(monkeypatch):
    monkeypatch.setattr(selfcheck, "OPTIONAL", {"uat"})
    monkeypatch.setattr(live_profiles, "available", lambda: {})
    monkeypatch.setattr(decoders, "available", lambda: {"uat": None})
    monkeypatch.setattr(selfcheck.shutil, "which", lambda name: None)
    lines, problems = selfcheck.check()
    assert problems == [] and any("optional decoder uat" in l for l in lines)


def test_selfcheck_reports_a_library_that_does_not_resolve(monkeypatch):
    monkeypatch.setattr(live_profiles, "available", lambda: {"vdl2": "/usr/local/bin/dumpvdl2"})
    monkeypatch.setattr(decoders, "available", lambda: {})
    monkeypatch.setattr(selfcheck.shutil, "which", lambda name: "/usr/bin/ldd")

    class Done:
        stdout = "\tlibacars-2.so.2 => not found\n\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n"

    monkeypatch.setattr(selfcheck.subprocess, "run", lambda *a, **k: Done())
    _, problems = selfcheck.check()
    assert problems == ["library  vdl2 (/usr/local/bin/dumpvdl2): libacars-2.so.2 => not found"]


def test_every_worker_service_starts_with_the_settings_its_compose_file_gives_it(tmp_path):
    """The worker's services share worker.env; a setting there that one of
    them can't take (HACKRF_LIVE_AGENT without its token file) stops it at
    startup."""
    import re
    from pathlib import Path

    import pytest

    from hackrf_mcp import settings

    yaml = pytest.importorskip("yaml")
    deploy = Path(__file__).resolve().parent.parent / "deploy" / "worker"
    compose = yaml.safe_load((deploy / "compose.yaml").read_text())
    shared = {}
    for line in (deploy / "worker.env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            shared[key] = value
    assert "HACKRF_LIVE_AGENT" not in shared
    token = tmp_path / "agent-token"
    token.write_text("t" * 40)
    seen = {}
    for name, svc in compose["services"].items():
        env = dict(shared)
        for key, value in (svc.get("environment") or {}).items():
            env[key] = re.sub(r"\$\{HACKRF_LIVE_AGENT:\?[^}]*\}", "192.0.2.20:8767", str(value))
        if "HACKRF_LIVE_AGENT_TOKEN_FILE" in env:
            assert any(v.endswith(f":{env['HACKRF_LIVE_AGENT_TOKEN_FILE']}:ro") for v in svc["volumes"]), name
            env["HACKRF_LIVE_AGENT_TOKEN_FILE"] = str(token)
        seen[name] = settings.LiveSettings.from_env(env).agent  # raises if it can't start
    assert seen == {"live": ("192.0.2.20", 8767), "web": None, "mcp": ("192.0.2.20", 8767)}
