"""The capture directory: confining tool file paths to it, checking disk
space before a capture, listing captures, and the transmit audit log."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from hackrf_mcp import settings

MIB = 1024 * 1024


class PathNotAllowed(ValueError):
    pass


class CaptureRefused(ValueError):
    pass


def data_dir() -> Path:
    root = settings.get().data_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve(path: str, *, must_exist: bool = False) -> Path:
    """Resolve a tool's file path inside the capture directory.

    Relative paths are taken relative to the capture directory; absolute
    paths are accepted only if they already point inside it. Anything that
    resolves outside it (via "..", an absolute path or a symlink) is refused,
    as is a path starting with "-" that a hackrf_* binary would read as an
    option.
    """
    if not path or not path.strip():
        raise PathNotAllowed("file path is empty")
    if path.startswith("-"):
        raise PathNotAllowed(f"file path {path!r} must not start with '-'")
    root = data_dir()
    candidate = Path(path).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise PathNotAllowed(f"{path!r} is outside the capture directory {root}")
    if must_exist:
        if not resolved.is_file():
            raise PathNotAllowed(f"{path!r} is not a file in the capture directory {root}")
    else:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def check_capture_space(nbytes: int) -> None:
    s = settings.get()
    if nbytes > s.max_capture_mb * MIB:
        raise CaptureRefused(
            f"capture would be {nbytes / MIB:.0f} MiB, over this server's "
            f"{s.max_capture_mb:.0f} MiB limit; lower duration or sample rate"
        )
    free = shutil.disk_usage(data_dir()).free
    if free - nbytes < s.min_free_mb * MIB:
        raise CaptureRefused(
            f"capture would be {nbytes / MIB:.0f} MiB but only {free / MIB:.0f} MiB is "
            f"free and {s.min_free_mb:.0f} MiB must stay free; delete old captures first"
        )


def list_captures(limit: int = 200) -> tuple[list[dict], int]:
    """Files under the capture directory, newest first, and the total
    number of bytes they take."""
    root = data_dir()
    files = [p for p in root.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    total = sum(p.stat().st_size for p in files)
    entries = [
        {
            "path": str(p.relative_to(root)),
            "bytes": p.stat().st_size,
            "modified_utc": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        }
        for p in files[:limit]
    ]
    return entries, total


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(MIB), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_tx(record: dict) -> None:
    """Append one transmit attempt, allowed or refused, to the audit log."""
    log_path = settings.get().tx_log_path.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def recent_tx(limit: int = 5) -> list[dict]:
    log_path = settings.get().tx_log_path.expanduser()
    if not log_path.is_file():
        return []
    lines = log_path.read_text().splitlines()[-limit:]
    return [json.loads(line) for line in lines if line.strip()]
