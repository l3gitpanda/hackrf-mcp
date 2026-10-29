from dataclasses import replace

import pytest

from hackrf_mcp import settings, storage


def test_relative_path_resolves_inside_capture_dir(tmp_path):
    assert storage.resolve("sub/out.iq") == tmp_path / "sub" / "out.iq"
    assert (tmp_path / "sub").is_dir()


def test_absolute_path_inside_capture_dir_is_accepted(tmp_path):
    assert storage.resolve(str(tmp_path / "a.iq")) == tmp_path / "a.iq"


@pytest.mark.parametrize("path", ["../escape.iq", "/etc/passwd", "-R", "", "   "])
def test_paths_outside_or_option_like_are_refused(path):
    with pytest.raises(storage.PathNotAllowed):
        storage.resolve(path)


def test_capture_dir_itself_is_refused(tmp_path):
    with pytest.raises(storage.PathNotAllowed):
        storage.resolve(str(tmp_path))


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside)

    with pytest.raises(storage.PathNotAllowed):
        storage.resolve("link/x.iq")


def test_must_exist_refuses_missing_file():
    with pytest.raises(storage.PathNotAllowed, match="not a file"):
        storage.resolve("missing.iq", must_exist=True)


def test_capture_over_size_limit_is_refused(isolated_settings):
    settings.configure(replace(isolated_settings, max_capture_mb=1))

    with pytest.raises(storage.CaptureRefused, match="limit"):
        storage.check_capture_space(2 * storage.MIB)


def test_capture_that_would_fill_disk_is_refused(isolated_settings):
    settings.configure(replace(isolated_settings, min_free_mb=10**12))

    with pytest.raises(storage.CaptureRefused, match="must stay free"):
        storage.check_capture_space(1)


def test_list_captures_newest_first(tmp_path):
    import os

    (tmp_path / "old.iq").write_bytes(b"x" * 10)
    (tmp_path / "new.iq").write_bytes(b"x" * 20)
    os.utime(tmp_path / "old.iq", (1, 1))

    entries, total = storage.list_captures()

    assert [e["path"] for e in entries] == ["new.iq", "old.iq"]
    assert total == 30


def test_audit_log_round_trip():
    storage.audit_tx({"frequency_hz": 915e6, "allowed": False})
    storage.audit_tx({"frequency_hz": 916e6, "allowed": True})

    recent = storage.recent_tx()

    assert [r["frequency_hz"] for r in recent] == [915e6, 916e6]
    assert all("time_utc" in r for r in recent)
