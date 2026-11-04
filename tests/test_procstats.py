"""procstats: a cgroup's CPU and memory, under systemd or in a container,
and hackrf-web falling back to what the services report themselves."""

from starlette.testclient import TestClient

from hackrf_mcp import procstats, settings
from hackrf_mcp.web import app as web_app


def _cgroup(base, mem=1048576, mem_max="max", usec=2_000_000, cpu_max="400000 100000"):
    base.mkdir(parents=True, exist_ok=True)
    (base / "memory.current").write_text(f"{mem}\n")
    (base / "memory.max").write_text(f"{mem_max}\n")
    (base / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 1\n")
    if cpu_max is not None:
        (base / "cpu.max").write_text(f"{cpu_max}\n")


def test_read_gives_memory_cpu_and_the_limit(tmp_path):
    _cgroup(tmp_path / "a")
    assert procstats.read(tmp_path / "a") == {
        "memory_bytes": 1048576, "memory_max": None, "cpu_usec": 2_000_000, "cpu_limit_pct": 400.0,
    }
    _cgroup(tmp_path / "b", mem_max="536870912", cpu_max="max 100000")
    assert procstats.read(tmp_path / "b")["cpu_limit_pct"] is None
    assert procstats.read(tmp_path / "b")["memory_max"] == 536870912
    assert procstats.read(tmp_path / "missing") is None


def test_own_finds_the_unit_or_the_container_root(tmp_path):
    root = tmp_path / "fs"
    _cgroup(root, mem=5)  # what a container sees: its own cgroup at the root
    _cgroup(root / "system.slice" / "hackrf-live.service", mem=7)
    proc = tmp_path / "cgroup"
    proc.write_text("0::/\n")
    assert procstats.own(root, proc)["memory_bytes"] == 5
    proc.write_text("0::/system.slice/hackrf-live.service\n")
    assert procstats.own(root, proc)["memory_bytes"] == 7
    proc.write_text("12:cpu:/legacy\n")  # cgroup v1 only: nothing to read
    assert procstats.own(root, proc) is None


class _Control:
    def __call__(self, req, timeout=5.0):
        return {"ok": True, "state": "running", "process": {"memory_bytes": 9, "memory_max": None, "cpu_usec": 3, "cpu_limit_pct": 400.0}}


def test_in_containers_the_web_shows_what_each_reports(tmp_path):
    live = settings.LiveSettings(live_dir=tmp_path / "live")
    gate = web_app.create_app(
        live, settings.WebSettings(), [web_app.hashlib.sha256(b"t").digest()], b"k" * 32,
        control=_Control(), cgroup_root=tmp_path / "no-systemd", feed=False,
        own_stats=lambda: {"memory_bytes": 4, "memory_max": None, "cpu_usec": 1, "cpu_limit_pct": 100.0},
    )
    st = TestClient(gate).get("/api/status", headers={"Authorization": "Bearer t"}).json()
    assert st["service"]["memory_bytes"] == 9 and st["service"]["cpu_limit_pct"] == 400.0
    assert st["web"]["memory_bytes"] == 4
