"""Helpers for invoking the hackrf_tools command-line utilities."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Optional directory to look for hackrf_tools binaries in before falling back
# to PATH. Set via set_bin_dir() / the --bin-dir CLI flag or HACKRF_TOOLS_DIR
# env var.
BIN_DIR: Path | None = None


class HackrfToolNotFound(RuntimeError):
    pass


def set_bin_dir(bin_dir: str | Path | None) -> None:
    """Set the directory to search for hackrf_tools binaries first."""
    global BIN_DIR
    BIN_DIR = Path(bin_dir) if bin_dir else None


def _resolve_binary(binary: str) -> str:
    if BIN_DIR is not None:
        for candidate in (BIN_DIR / binary, BIN_DIR / f"{binary}.exe"):
            if candidate.is_file():
                return str(candidate)

    path = shutil.which(binary)
    if path is None:
        location = f" or in '{BIN_DIR}'" if BIN_DIR is not None else ""
        raise HackrfToolNotFound(
            f"'{binary}' was not found on PATH{location}. Install hackrf_tools "
            "(https://github.com/greatscottgadgets/hackrf), or pass --bin-dir / "
            "set HACKRF_TOOLS_DIR to the folder containing its binaries."
        )
    return path


def run(binary: str, args: list[str], timeout: float | None = None) -> str:
    """Run a hackrf_tools binary with the given args and return combined output."""
    path = _resolve_binary(binary)

    try:
        proc = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return (
            f"Command timed out after {timeout}s and was terminated.\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        )

    output = proc.stdout or ""
    if proc.stderr:
        output += f"\n--- stderr ---\n{proc.stderr}"
    if proc.returncode != 0:
        output += f"\n--- exit code: {proc.returncode} ---"
    return output.strip()
