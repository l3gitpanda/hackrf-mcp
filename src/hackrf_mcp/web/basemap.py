"""The Map screen's base map.

A PMTiles archive (https://github.com/protomaps/PMTiles) of OpenStreetMap
vector tiles for the station's area. The operator extracts it once, with
this module, from a Protomaps planet build, and puts it in the web state
directory as `basemap.pmtiles`. hackrf-web serves the file with byte ranges
and the browser draws it with the vendored protomaps-leaflet, so nothing is
fetched from elsewhere when the map is viewed. The file says where the
station is, roughly: it belongs on the station, never in the repository.

An extract has two parts so it stays small. The wide area (everything the
receiver might hear, a few hundred km) goes down to `--wide-zoom` (default
11: towns, main roads, coast), and the core (where most targets are) down to
`--max-zoom` (default 14: streets). The archive's metadata records the wide
zoom as `hackrf_full_zoom`, the deepest zoom that covers the whole area, and
the core's bounds as `hackrf_core_bounds`, so the map can draw the core's
detail over the wide area's outline.

    uv run --with pmtiles python -m hackrf_mcp.web.basemap \\
        --from-log /var/lib/hackrf-mcp/live/live.db --out basemap.pmtiles

`--from-log` takes the area from the positions the live log holds; `--around
LAT,LON` or `--wide`/`--core` bounding boxes give it directly. `--dry-run`
reports the size without downloading tiles. Only the archive's directories
and the tiles needed are read from the build (HTTP range requests), about
1 MiB plus the extract's own size.

Map data (c) OpenStreetMap contributors (ODbL); tiles by Protomaps.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import sqlite3
import struct
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FILENAME = "basemap.pmtiles"
FULL_ZOOM_KEY = "hackrf_full_zoom"
CORE_KEY = "hackrf_core_bounds"
HEADER_LEN = 127
GZIP = 2  # PMTiles compression code
BUILDS = "https://build-metadata.protomaps.dev/builds.json"
BUILD_URL = "https://build.protomaps.com/{key}"
# The build host refuses Python's default User-Agent.
USER_AGENT = "hackrf-mcp-basemap/1"
# Tile data reads are merged across gaps up to this size, into requests of
# at most MAX_REQUEST bytes: tiles near each other are near each other in
# the archive (Hilbert order), so a few hundred requests fetch an extract.
MERGE_GAP = 64 * 1024
MAX_REQUEST = 4 * 1024 * 1024

Bbox = tuple[float, float, float, float]  # west, south, east, north


def info(path: Path) -> dict | None:
    """What the map needs to know about the base map at `path`: its zoom
    range, the deepest zoom covering all of it, its bounds, and the bounds
    of the core that has the deeper zooms. None if there is no readable
    PMTiles v3 archive there."""
    try:
        with open(path, "rb") as f:
            h = f.read(HEADER_LEN)
            if len(h) < HEADER_LEN or h[:7] != b"PMTiles" or h[7] != 3:
                return None
            meta_off, meta_len = struct.unpack_from("<QQ", h, 24)
            meta = {}
            if 0 < meta_len <= 1 << 20:
                f.seek(meta_off)
                raw = f.read(meta_len)
                meta = json.loads(gzip.decompress(raw) if h[97] == GZIP else raw)
    except (OSError, ValueError, EOFError):
        return None
    minz, maxz = h[100], h[101]
    w, s, e, n = (v / 1e7 for v in struct.unpack_from("<iiii", h, 102))
    if not isinstance(meta, dict):
        meta = {}
    full = meta.get(FULL_ZOOM_KEY)
    if not isinstance(full, int) or isinstance(full, bool) or not minz <= full <= maxz:
        full = maxz
    core = meta.get(CORE_KEY)
    ok = isinstance(core, list) and len(core) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in core)
    return {
        "minzoom": minz,
        "maxzoom": maxz,
        "full_zoom": full,
        "bounds": [w, s, e, n],
        "core": [float(v) for v in core] if ok and full < maxz else None,
    }


# --- the area


def bbox_around(lat: float, lon: float, km: float) -> Bbox:
    dlat = km / 111.32
    dlon = km / (111.32 * max(0.01, math.cos(math.radians(lat))))
    return (lon - dlon, max(-85.0, lat - dlat), lon + dlon, min(85.0, lat + dlat))


def _pad(b: Bbox, frac: float) -> Bbox:
    w, s, e, n = b
    dx, dy = (e - w) * frac, (n - s) * frac
    return (w - dx, max(-85.0, s - dy), e + dx, min(85.0, n + dy))


def areas_from_log(db: Path, days: float = 7, now: float | None = None) -> tuple[Bbox, Bbox]:
    """The wide area and the core from the positions in the live log: the
    core holds 98% of the fixes, the wide area 99.8% (a stray decode far
    away would otherwise stretch it), each padded by a tenth."""
    since = (now if now is not None else time.time()) - days * 86400
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        pts = conn.execute("SELECT lat, lon FROM positions WHERE ts >= ?", (since,)).fetchall()
    if len(pts) < 10:
        raise ValueError(f"only {len(pts)} positions in the last {days:g} days of {db}: give the area with --around or --wide")
    lats = sorted(p[0] for p in pts)
    lons = sorted(p[1] for p in pts)

    def box(lo: float, hi: float) -> Bbox:
        def q(v: list[float], p: float) -> float:
            return v[min(len(v) - 1, int(p * len(v)))]

        return (q(lons, lo), q(lats, lo), q(lons, hi), q(lats, hi))

    return _pad(box(0.001, 0.999), 0.1), _pad(box(0.01, 0.99), 0.1)


def tile_ids(bbox: Bbox, zmin: int, zmax: int) -> set[int]:
    """The ids of the tiles covering `bbox` at zooms zmin..zmax."""
    from pmtiles.tile import zxy_to_tileid

    w, s, e, n = bbox
    out = set()
    for z in range(zmin, zmax + 1):
        k = 2**z

        def tx(lon: float) -> int:
            return max(0, min(k - 1, int((lon + 180) / 360 * k)))

        def ty(lat: float) -> int:
            r = math.radians(max(-85.05, min(85.05, lat)))
            return max(0, min(k - 1, int((1 - math.asinh(math.tan(r)) / math.pi) / 2 * k)))

        for x in range(tx(w), tx(e) + 1):
            for y in range(ty(n), ty(s) + 1):
                out.add(zxy_to_tileid(z, x, y))
    return out


# --- reading the source


class Source:
    """A PMTiles archive to read byte ranges from: a URL or a local file."""

    def __init__(self, where: str):
        self.where = where
        self.remote = where.startswith(("http://", "https://"))
        self.fetched = 0
        self.requests = 0

    def get(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if self.remote:
            req = urllib.request.Request(
                self.where, headers={"Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": USER_AGENT}
            )
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        if r.status != 206:
                            raise OSError(f"{self.where}: expected 206 Partial Content, got {r.status}")
                        data = r.read()
                    break
                except OSError:
                    if attempt == 3:
                        raise
                    time.sleep(2**attempt)
        else:
            with open(self.where, "rb") as f:
                f.seek(offset)
                data = f.read(length)
        if len(data) != length:
            raise OSError(f"{self.where}: short read at {offset}: {len(data)} of {length} bytes")
        self.fetched += len(data)
        self.requests += 1
        return data


def newest_build() -> str:
    req = urllib.request.Request(BUILDS, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        builds = json.load(r)
    return BUILD_URL.format(key=builds[-1]["key"])


def read_header(src: Source) -> tuple[dict, dict]:
    from pmtiles.tile import Compression, deserialize_header

    header = deserialize_header(src.get(0, HEADER_LEN))
    if header["internal_compression"] != Compression.GZIP:
        raise ValueError(f"{src.where}: directories compressed with {header['internal_compression']}, only gzip is supported")
    raw = src.get(header["metadata_offset"], header["metadata_length"])
    return header, json.loads(gzip.decompress(raw))


def find_tiles(src: Source, header: dict, ids: set[int]) -> dict[int, tuple[int, int]]:
    """Tile id -> (offset, length) in the tile data, for those of `ids` the
    archive has, reading only the directories that cover them."""
    from pmtiles.tile import deserialize_directory

    found: dict[int, tuple[int, int]] = {}

    def walk(entries: list, want: list[int]) -> None:
        for i, e in enumerate(entries):
            hi = entries[i + 1].tile_id if i + 1 < len(entries) else math.inf
            a, b = bisect.bisect_left(want, e.tile_id), bisect.bisect_left(want, hi)
            if a == b:
                continue
            if e.run_length == 0:  # a leaf directory
                leaf = deserialize_directory(src.get(header["leaf_directory_offset"] + e.offset, e.length))
                walk(leaf, want[a:b])
            else:
                for t in want[a:b]:
                    if t < e.tile_id + e.run_length:
                        found[t] = (e.offset, e.length)

    walk(deserialize_directory(src.get(header["root_offset"], header["root_length"])), sorted(ids))
    return found


def fetch_tiles(src: Source, header: dict, spans: set[tuple[int, int]], threads: int = 4) -> dict[tuple[int, int], bytes]:
    """The bytes of each (offset, length) span of tile data, in merged reads."""
    batches: list[list] = []  # [start, end, spans]
    for off, ln in sorted(spans):
        last = batches[-1] if batches else None
        if last and off - last[1] <= MERGE_GAP and off + ln - last[0] <= MAX_REQUEST:
            last[1] = max(last[1], off + ln)
            last[2].append((off, ln))
        else:
            batches.append([off, off + ln, [(off, ln)]])
    base = header["tile_data_offset"]

    def one(batch: list) -> dict:
        start, end, parts = batch
        blob = src.get(base + start, end - start)
        return {(o, n): blob[o - start : o - start + n] for o, n in parts}

    out: dict[tuple[int, int], bytes] = {}
    with ThreadPoolExecutor(threads) as pool:
        for part in pool.map(one, batches):
            out.update(part)
    return out


# --- writing the extract


def extract(
    source: str,
    out: Path | None,
    wide: Bbox,
    core: Bbox,
    wide_zoom: int = 11,
    max_zoom: int = 14,
    log=print,
) -> dict:
    """Extract the wide area to `wide_zoom` and the core to `max_zoom` from
    `source` into `out` (written whole, then renamed into place). With out
    None, only measure. Returns counts and sizes."""
    from pmtiles.writer import Writer

    if not 0 <= wide_zoom <= max_zoom:
        raise ValueError("--wide-zoom must be between 0 and --max-zoom")
    src = Source(source)
    header, metadata = read_header(src)
    max_zoom = min(max_zoom, header["max_zoom"])
    wide_zoom = min(wide_zoom, max_zoom)
    ids = tile_ids(wide, 0, wide_zoom) | tile_ids(core, wide_zoom + 1, max_zoom)
    found = find_tiles(src, header, ids)
    spans = set(found.values())
    size = sum(n for _, n in spans)
    result = {"tiles": len(found), "bytes": size, "directory_bytes": src.fetched}
    log(f"{len(found)} tiles, {size / 2**20:.1f} MiB of tile data (directories read: {src.fetched / 2**20:.1f} MiB)")
    if out is None:
        return result
    if not found:
        raise ValueError(f"{source} has no tiles in that area")
    data = fetch_tiles(src, header, spans)
    part = out.with_name(out.name + ".part")
    w, s, e, n = wide
    cw, cs, ce, cn = core
    with open(part, "wb") as f:
        writer = Writer(f)
        for tid in sorted(found):
            writer.write_tile(tid, data[found[tid]])
        writer.finalize(
            {
                "tile_type": header["tile_type"],
                "tile_compression": header["tile_compression"],
                "min_lon_e7": round(w * 1e7),
                "min_lat_e7": round(s * 1e7),
                "max_lon_e7": round(e * 1e7),
                "max_lat_e7": round(n * 1e7),
                "center_zoom": wide_zoom,
                "center_lon_e7": round((cw + ce) / 2 * 1e7),
                "center_lat_e7": round((cs + cn) / 2 * 1e7),
            },
            {
                **metadata,
                FULL_ZOOM_KEY: wide_zoom,
                CORE_KEY: [cw, cs, ce, cn],
                "hackrf_source": source,
                "hackrf_extracted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
    got = info(part)
    deepest = max(_zxy(t)[0] for t in found) if found else None
    if not got or got["maxzoom"] != deepest or got["full_zoom"] != min(wide_zoom, deepest):
        part.unlink(missing_ok=True)
        raise RuntimeError("the extract did not read back as expected; nothing was written")
    part.replace(out)
    result.update(file_bytes=out.stat().st_size, fetched=src.fetched, requests=src.requests)
    log(f"wrote {out} ({result['file_bytes'] / 2**20:.1f} MiB; {src.requests} requests, {src.fetched / 2**20:.1f} MiB read)")
    return result


def _zxy(tid: int) -> tuple[int, int, int]:
    from pmtiles.tile import tileid_to_zxy

    return tileid_to_zxy(tid)


def _bbox(text: str) -> Bbox:
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 4 or not (parts[0] < parts[2] and parts[1] < parts[3]):
        raise argparse.ArgumentTypeError("a bounding box is WEST,SOUTH,EAST,NORTH in degrees")
    return tuple(parts)  # type: ignore[return-value]


def _point(text: str) -> tuple[float, float]:
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 2 or not (-90 <= parts[0] <= 90 and -180 <= parts[1] <= 180):
        raise argparse.ArgumentTypeError("a point is LAT,LON in degrees")
    return parts[0], parts[1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m hackrf_mcp.web.basemap",
        description="Extract the Map screen's base map (OpenStreetMap vector tiles) for the station's area.",
    )
    area = p.add_mutually_exclusive_group(required=True)
    area.add_argument("--from-log", type=Path, metavar="LIVE_DB", help="take the area from the positions in the live log")
    area.add_argument("--around", type=_point, metavar="LAT,LON", help="a square around this point (see --wide-km, --core-km)")
    area.add_argument("--wide", type=_bbox, metavar="W,S,E,N", help="the wide area's bounding box")
    p.add_argument("--core", type=_bbox, metavar="W,S,E,N", help="the core's bounding box (with --wide; default the wide area)")
    p.add_argument("--wide-km", type=float, default=250, help="with --around: half the wide area's width (default 250)")
    p.add_argument("--core-km", type=float, default=40, help="with --around: half the core's width (default 40)")
    p.add_argument("--wide-zoom", type=int, default=11, help="deepest zoom for the wide area (default 11)")
    p.add_argument("--max-zoom", type=int, default=14, help="deepest zoom for the core (default 14)")
    p.add_argument("--source", help="a PMTiles URL or file (default: the newest Protomaps daily build)")
    p.add_argument("--out", type=Path, help=f"where to write it (hackrf-web reads <web state dir>/{FILENAME})")
    p.add_argument("--dry-run", action="store_true", help="report the size without downloading tiles")
    a = p.parse_args(argv)
    if not a.dry_run and not a.out:
        p.error("--out is required unless --dry-run")
    if a.from_log:
        wide, core = areas_from_log(a.from_log)
    elif a.around:
        wide, core = bbox_around(*a.around, a.wide_km), bbox_around(*a.around, a.core_km)
    else:
        wide, core = a.wide, a.core or a.wide
    source = a.source or newest_build()
    print(f"source {source}")
    print(f"wide area to zoom {a.wide_zoom}, core to zoom {a.max_zoom}")
    extract(source, None if a.dry_run else a.out, wide, core, a.wide_zoom, a.max_zoom)
    return 0


if __name__ == "__main__":
    sys.exit(main())
