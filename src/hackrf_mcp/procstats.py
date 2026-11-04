"""The CPU and memory a process's cgroup has used, for the status screens.

Under systemd each unit is a cgroup of its own (/sys/fs/cgroup/system.slice/
<unit>); in a container the process sees its own cgroup at /sys/fs/cgroup
itself. /proc/self/cgroup says which applies ("0::/system.slice/x.service"
or "0::/"), so own() works in both. cgroup v2 only; None where it can't be
read.
"""

from __future__ import annotations

from pathlib import Path

CGROUP_FS = Path("/sys/fs/cgroup")


def read(base: Path) -> dict | None:
    """memory_bytes, memory_max (None: unlimited), cpu_usec (used so far) and
    cpu_limit_pct (100 per core it may use; None: unlimited) of a cgroup."""
    try:
        mem = int((base / "memory.current").read_text())
        max_raw = (base / "memory.max").read_text().strip()
        cpu = 0
        for line in (base / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                cpu = int(line.split()[1])
    except (OSError, ValueError):
        return None
    limit = None
    try:
        quota, period = (base / "cpu.max").read_text().split()[:2]
        if quota != "max":
            limit = round(int(quota) / int(period) * 100, 1)
    except (OSError, ValueError):
        pass
    return {
        "memory_bytes": mem,
        "memory_max": None if max_raw == "max" else int(max_raw),
        "cpu_usec": cpu,
        "cpu_limit_pct": limit,
    }


def own(root: Path = CGROUP_FS, proc_cgroup: Path = Path("/proc/self/cgroup")) -> dict | None:
    """This process's own cgroup's figures."""
    try:
        lines = proc_cgroup.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            rel = line[3:].strip().lstrip("/")
            return read(root / rel if rel else root)
    return None
