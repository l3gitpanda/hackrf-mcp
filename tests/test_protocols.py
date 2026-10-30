"""The protocol registries: what makes adding a decoder a matter of adding
a module to hackrf_mcp/protocols/."""

import sys
from pathlib import Path

import pytest

from hackrf_mcp import decoders, live_profiles, protocols, server


def test_builtin_order_is_stable():
    assert decoders.names() == ("adsb", "uat", "ais", "acars", "vdl2", "pocsag", "flex", "aprs", "eas", "dtmf", "rds")
    assert live_profiles.names() == ("adsb", "ais", "acars", "vdl2", "flex", "pocsag", "aprs", "eas")


def test_every_profile_can_be_summarised_and_described():
    for name, p in live_profiles.PROFILES.items():
        assert p.summary_kinds, name
        assert p.summary_fields, name
        assert issubclass(p.pipeline, live_profiles.Pipeline), name


def test_tool_descriptions_and_choices_come_from_the_registries():
    doc = server.hackrf_decode.__doc__
    assert "<<" not in doc
    for d in decoders.DECODERS.values():
        assert d.help.strip().splitlines()[0].strip() in doc if d.help else True
    doc = server.hackrf_live_start.__doc__
    assert "<<" not in doc and "eas    - EAS/SAME alert headers" in doc
    assert set(server.DecoderName.__args__) == set(decoders.names())
    assert set(server.ProfileName.__args__) == set(live_profiles.names())


def test_registering_a_name_twice_is_an_error():
    with pytest.raises(ValueError, match="twice"):
        decoders.register(decoders.DECODERS["adsb"])
    with pytest.raises(ValueError, match="twice"):
        live_profiles.register(live_profiles.PROFILES["adsb"])


def _readme_example() -> str:
    """The example module in the README's "Adding a decoder"."""
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    start = readme.index("A minimal offline decoder:")
    block = readme[readme.index("```python\n", start) + len("```python\n") :]
    return block[: block.index("```")]


def test_the_readme_example_module_is_picked_up_and_runs(tmp_path, monkeypatch):
    (tmp_path / "zz_readme_example.py").write_text(_readme_example())
    monkeypatch.setattr(protocols, "__path__", [*protocols.__path__, str(tmp_path)])
    monkeypatch.setattr(decoders, "find_program", lambda d: f"/usr/bin/{d.programs[0]}")
    ran = []

    def fake_run(args, stdin_chunks, timeout, workdir):
        ran.append((args, b"".join(stdin_chunks)))
        return decoders.RunResult("MSG one\nnoise\nMSG two\n", 0, False, 0.1)

    monkeypatch.setattr(decoders, "run_program", fake_run)
    try:
        protocols._load()
        assert "mydecoder" in decoders.names()
        assert "mydecoder - what it decodes" in decoders.tool_help()
        capture = tmp_path / "c.cs8"
        capture.write_bytes(bytes([0, 255]) * 10)
        result = decoders.decode("mydecoder", capture, 2_000_000, 100_000_000, None, None, 5, tmp_path / "work")
        assert result.headline == "mydecoder: 2 messages."
        assert ran[0][0] == ["/usr/bin/my-decoder-program", "-"]
        assert ran[0][1] == bytes([128, 127]) * 10  # cs8 to cu8
        protocols._load()  # loading again doesn't register it twice
    finally:
        decoders.DECODERS.pop("mydecoder", None)
        sys.modules.pop("hackrf_mcp.protocols.zz_readme_example", None)


def test_no_tool_description_loses_docstring_text():
    """FastMCP's description is the docstring's first text section only: a
    line like "Profiles:" before an indented block starts a new section and
    everything from it on silently disappears from what clients see."""
    import asyncio
    import inspect
    import re

    def norm(text):
        return re.sub(r"\s+", " ", text).strip()

    async def tools():
        return await server.mcp.list_tools()

    lost = []
    for tool in asyncio.run(tools()):
        doc = inspect.cleandoc(getattr(server, tool.name).__doc__)
        doc = doc.split("\nArgs:", 1)[0]
        if norm(tool.description) != norm(doc):
            lost.append(tool.name)
    assert lost == []
