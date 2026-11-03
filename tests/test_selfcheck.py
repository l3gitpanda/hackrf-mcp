from hackrf_mcp import decoders, live_profiles, selfcheck


def test_selfcheck_requires_every_live_and_capture_decoder_but_the_optional_ones(monkeypatch):
    monkeypatch.setattr(live_profiles, "available", lambda: {"adsb": "/usr/bin/dump1090-mutability", "ais": None})
    monkeypatch.setattr(decoders, "available", lambda: {"adsb": "/usr/bin/dump1090-mutability", "uat": None, "rds": None, "acars": None})
    monkeypatch.setattr(selfcheck.shutil, "which", lambda name: None)  # no ldd: skip the library check
    lines, problems = selfcheck.check()
    assert problems == ["missing  live profile ais", "missing  decoder acars"]
    assert any("optional decoder uat" in l for l in lines) and any("optional decoder rds" in l for l in lines)


def test_selfcheck_reports_a_library_that_does_not_resolve(monkeypatch):
    monkeypatch.setattr(live_profiles, "available", lambda: {"vdl2": "/usr/local/bin/dumpvdl2"})
    monkeypatch.setattr(decoders, "available", lambda: {})
    monkeypatch.setattr(selfcheck.shutil, "which", lambda name: "/usr/bin/ldd")

    class Done:
        stdout = "\tlibacars-2.so.2 => not found\n\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n"

    monkeypatch.setattr(selfcheck.subprocess, "run", lambda *a, **k: Done())
    _, problems = selfcheck.check()
    assert problems == ["library  vdl2 (/usr/local/bin/dumpvdl2): libacars-2.so.2 => not found"]
