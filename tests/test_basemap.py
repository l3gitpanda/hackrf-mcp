"""The base map extract (hackrf_mcp.web.basemap): the area from the log, and
a two-part extract from a local planet stand-in. Needs the pmtiles package
(uv run --with pmtiles pytest); the header reading hackrf-web does is tested
in test_web.py without it."""

import http.client
import io
import sqlite3

import pytest

from hackrf_mcp.web import basemap

pmtiles = pytest.importorskip("pmtiles")
from pmtiles.reader import MmapSource, all_tiles  # noqa: E402
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid  # noqa: E402
from pmtiles.writer import Writer  # noqa: E402


def planet(path, zmax=6):
    """A stand-in for a planet build: every tile to zmax, each tile's bytes naming it."""
    with open(path, "wb") as f:
        w = Writer(f)
        ids = sorted(zxy_to_tileid(z, x, y) for z in range(zmax + 1) for x in range(2**z) for y in range(2**z))
        for t in ids:
            w.write_tile(t, ("%d/%d/%d" % tileid_to_zxy(t)).encode())
        w.finalize(
            {"tile_type": TileType.MVT, "tile_compression": Compression.GZIP, "min_zoom": 0, "max_zoom": zmax},
            {"attribution": "test", "vector_layers": []},
        )


def test_a_wide_area_shallow_and_a_core_deep(tmp_path):
    src = tmp_path / "planet.pmtiles"
    planet(src)
    out = tmp_path / "basemap.pmtiles"
    wide, core = (-125.0, 30.0, -112.0, 40.0), (-119.0, 33.0, -117.0, 34.5)
    got = basemap.extract(str(src), out, wide, core, wide_zoom=3, max_zoom=6, log=lambda _: None)
    with open(out, "rb") as f:
        tiles = {tuple(zxy): data for zxy, data in all_tiles(MmapSource(f))}
    assert got["tiles"] == len(tiles)
    assert all(data == ("%d/%d/%d" % zxy).encode() for zxy, data in tiles.items())
    by_zoom = {z: {(x, y) for (zz, x, y) in tiles if zz == z} for z in range(7)}
    assert by_zoom[0] == {(0, 0)}
    # Deeper than the wide zoom, only tiles covering the core.
    assert basemap.tile_ids(core, 6, 6) == {zxy_to_tileid(6, x, y) for x, y in by_zoom[6]}
    assert len(by_zoom[4]) < len(basemap.tile_ids(wide, 4, 4))
    assert basemap.info(out) == {
        "minzoom": 0,
        "maxzoom": 6,
        "full_zoom": 3,
        "bounds": [-125.0, 30.0, -112.0, 40.0],
        "core": [-119.0, 33.0, -117.0, 34.5],
    }
    assert not (tmp_path / "basemap.pmtiles.part").exists()
    # A dry run measures without writing.
    dry = basemap.extract(str(src), None, wide, core, wide_zoom=3, max_zoom=6, log=lambda _: None)
    assert dry["tiles"] == got["tiles"] and "file_bytes" not in dry


def test_the_area_from_the_log_ignores_stray_fixes(tmp_path):
    db = tmp_path / "live.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE positions (ts REAL, lat REAL, lon REAL)")
        pts = [(1000.0, 33.0 + i / 1000, -118.5 + i / 1000) for i in range(1000)]
        pts.append((1000.0, -40.0, 170.0))  # a bad decode on the other side of the world
        conn.executemany("INSERT INTO positions VALUES (?, ?, ?)", pts)
    wide, core = basemap.areas_from_log(db, now=2000.0)
    assert -119 < wide[0] < core[0] < core[2] < wide[2] < -117
    assert 32.5 < wide[1] < core[1] < core[3] < wide[3] < 34.5
    with pytest.raises(ValueError, match="only 0 positions"):
        basemap.areas_from_log(db, now=10**9)


def test_main_needs_an_area_and_an_output(tmp_path, capsys):
    with pytest.raises(SystemExit):
        basemap.main(["--out", str(tmp_path / "x.pmtiles")])
    with pytest.raises(SystemExit):
        basemap.main(["--around", "33.9,-118.4"])  # neither --out nor --dry-run
    src = tmp_path / "planet.pmtiles"
    planet(src, zmax=4)
    assert basemap.main(["--around", "33.9,-118.4", "--source", str(src), "--max-zoom", "4", "--wide-zoom", "2", "--dry-run"]) == 0
    assert "tiles" in capsys.readouterr().out


def test_a_read_cut_short_is_retried(monkeypatch):
    calls = []

    class Response(io.BytesIO):
        status = 206

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        calls.append(req.get_header("Range"))
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"par", 5)
        return Response(b"bytes")

    monkeypatch.setattr(basemap.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(basemap.time, "sleep", lambda s: None)
    assert basemap.Source("https://example.invalid/planet.pmtiles").get(10, 5) == b"bytes"
    assert calls == ["bytes=10-14", "bytes=10-14"]
