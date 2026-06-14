import subprocess

import pytest

from hackrf_mcp import cli


@pytest.fixture(autouse=True)
def reset_bin_dir():
    cli.set_bin_dir(None)
    yield
    cli.set_bin_dir(None)


def test_run_binary_not_found(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    with pytest.raises(cli.HackrfToolNotFound):
        cli.run("hackrf_info", [])


def test_resolve_binary_prefers_bin_dir(tmp_path, monkeypatch):
    exe = tmp_path / "hackrf_info.exe"
    exe.write_text("")

    cli.set_bin_dir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/hackrf_info")

    assert cli._resolve_binary("hackrf_info") == str(exe)


def test_resolve_binary_falls_back_to_path_when_not_in_bin_dir(tmp_path, monkeypatch):
    cli.set_bin_dir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert cli._resolve_binary("hackrf_info") == "/usr/bin/hackrf_info"


def test_resolve_binary_not_found_mentions_bin_dir(tmp_path, monkeypatch):
    cli.set_bin_dir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    with pytest.raises(cli.HackrfToolNotFound) as exc_info:
        cli._resolve_binary("hackrf_info")

    assert str(tmp_path) in str(exc_info.value)


def test_run_success(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(args, capture_output, text, timeout):
        assert args == ["/usr/bin/hackrf_info", "-d", "ABC"]
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.run("hackrf_info", ["-d", "ABC"]) == "ok"


def test_run_appends_stderr_and_exit_code_on_failure(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], returncode=1, stdout="out", stderr="err"),
    )

    result = cli.run("hackrf_info", [])

    assert "out" in result
    assert "--- stderr ---\nerr" in result
    assert "exit code: 1" in result


def test_run_timeout_returns_message_without_raising(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hackrf_sweep", timeout=5, output="partial", stderr="oops")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.run("hackrf_sweep", [], timeout=5)

    assert "timed out after 5s" in result
    assert "partial" in result
    assert "oops" in result
