"""Check that this machine (a worker image, say) has every program the live
profiles and the capture decoders run, and that their shared libraries
resolve. Exits 1 if a required one is missing.

    python -m hackrf_mcp.selfcheck
"""

from __future__ import annotations

import shutil
import subprocess
import sys

# Capture decoders that may be missing: reported, not required. Since the
# worker image builds redsea and dump978-fa (uat, rds), there are none.
OPTIONAL: set[str] = set()


def check() -> tuple[list[str], list[str]]:
    from hackrf_mcp import decoders, live_profiles

    lines, problems = [], []
    found: dict[str, str] = {}
    for kind, avail in (("live profile", live_profiles.available()), ("decoder", decoders.available())):
        for name, path in avail.items():
            if path:
                found[path] = name
                lines.append(f"ok       {kind} {name}: {path}")
            elif kind == "decoder" and name in OPTIONAL:
                lines.append(f"optional {kind} {name}: not installed")
            else:
                problems.append(f"missing  {kind} {name}")
    if shutil.which("ldd"):
        for path, name in sorted(found.items()):
            if not path.startswith("/"):
                continue  # a decoder built into the package, no program
            out = subprocess.run(["ldd", path], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if "not found" in line:
                    problems.append(f"library  {name} ({path}): {line.strip()}")
    return lines, problems


def main() -> int:
    lines, problems = check()
    for line in lines + problems:
        print(line)
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
