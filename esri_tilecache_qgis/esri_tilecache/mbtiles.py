"""Esri vector tile package (`.vtpk`) → MBTiles 1.3 (Mapbox vector tiles), without re-encoding the tiles.

A `.vtpk` is a ZIP with `p12/root.json` (tiling scheme) and `p12/tile/Lxx/R…C….bundle` (Compact V2); the tiles
are Mapbox Vector Tiles 2.x compressed with gzip; the Esri style is in `p12/resources/styles/root.json`.

MBTiles prescribes Web Mercator and the XYZ grid (level z = 2^z × 2^z tiles of the whole world) with TMS row
order (from the bottom). The converter therefore accepts only a `.vtpk` in Web Mercator with the origin
−20037508.34 / +20037508.34, where the Esri level equals the XYZ level (resolution × tile size × 2^z = equator
length). Any other CRS (e.g. S-JTSK) does not belong into MBTiles by its specification — the conversion is refused
with an explanation; QGIS opens such a `.vtpk` directly.

Tiles are stored as they are (gzip); the metadata `json` lists the layers and attribute types read from the tiles
(`vector_layers`). The Esri style is saved next to the file as `<name>.style.json`.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import sqlite3
import time
from pathlib import Path

from .reader import Cache, Cancelled, TileCacheError, open_cache

logger = logging.getLogger(__name__)

HALF_EQUATOR = 20037508.342789244       # π · 6378137
_R = 6378137.0


def _varint(b: bytes, i: int) -> tuple[int, int]:
    v = s = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << s
        if not x & 0x80:
            return v, i
        s += 7


def _fields(b: bytes):
    """Protobuf fields of a message: (field number, wire type, value) — bytes for type 2, int for type 0."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        number, wtype = key >> 3, key & 7
        if wtype == 2:
            length, i = _varint(b, i)
            if i + length > len(b):
                raise ValueError("field beyond the end of the message")
            yield number, wtype, b[i:i + length]
            i += length
        elif wtype == 0:
            value, i = _varint(b, i)
            yield number, wtype, value
        elif wtype == 5:
            i += 4
        elif wtype == 1:
            i += 8
        else:
            raise ValueError(f"unknown wire type {wtype}")


_VALUE_TYPE = {1: "String", 2: "Number", 3: "Number", 4: "Number", 5: "Number", 6: "Number", 7: "Boolean"}


def mvt_layers(data: bytes) -> dict[str, dict[str, str]]:
    """{layer: {attribute: MBTiles type (String/Number/Boolean)}} of an MVT 2 tile (gzip is unpacked)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    out: dict[str, dict[str, str]] = {}
    try:
        for number, wtype, layer in _fields(data):
            if number != 3 or wtype != 2:                               # Tile.layers
                continue
            name, keys, values, tags = "", [], [], []
            for n2, t2, x in _fields(layer):
                if n2 == 1 and t2 == 2:
                    name = x.decode("utf-8", "replace")
                elif n2 == 3 and t2 == 2:
                    keys.append(x.decode("utf-8", "replace"))
                elif n2 == 4 and t2 == 2:
                    kind = next((_VALUE_TYPE.get(n3) for n3, _t3, _x in _fields(x)), None)
                    values.append(kind or "String")
                elif n2 == 2 and t2 == 2:                               # Feature.tags (packed)
                    for n3, t3, x3 in _fields(x):
                        if n3 == 2 and t3 == 2:
                            j, nums = 0, []
                            while j < len(x3):
                                v, j = _varint(x3, j)
                                nums.append(v)
                            tags.extend(zip(nums[::2], nums[1::2]))
            attrs = out.setdefault(name, {})
            for k, v in tags:
                if k < len(keys) and v < len(values):
                    attrs.setdefault(keys[k], values[v])
    except (IndexError, ValueError) as exc:
        raise TileCacheError(f"the tile is not a valid MVT ({exc})") from exc
    return out


def xyz_levels(cache: Cache) -> dict:
    """Esri level → XYZ level; TileCacheError if the tiling scheme is not Web Mercator XYZ."""
    if cache.epsg != 3857:
        raise TileCacheError(f"MBTiles requires Web Mercator (EPSG:3857); the package is in EPSG:{cache.epsg} — "
                             "it cannot go into MBTiles by the specification (QGIS opens the .vtpk directly)")
    if abs(cache.origin_x + HALF_EQUATOR) > 1.0 or abs(cache.origin_y - HALF_EQUATOR) > 1.0:
        raise TileCacheError(f"the tile origin ({cache.origin_x:.2f}, {cache.origin_y:.2f}) is not the top-left "
                             "corner of the Web Mercator world — not an XYZ grid")
    out = {}
    for level, res in cache.levels.items():
        n = 2 * HALF_EQUATOR / (res * cache.tile_size)
        z = round(math.log2(n))
        if z < 0 or abs(n - 2 ** z) > 1e-6 * n:
            raise TileCacheError(f"level {level} ({res} m/px) is not a level of the XYZ grid")
        out[level] = z
    return out


def to_mbtiles(source, target, *, progress=None, cancelled=None) -> dict:
    """`.vtpk` (a path or an open `Cache`) → MBTiles `target` (a new file) + `<target>.style.json`."""
    own = not isinstance(source, Cache)
    cache = open_cache(source) if own else source
    try:
        return _to_mbtiles(cache, Path(target), progress, cancelled)
    finally:
        if own:
            cache.close()


def _lonlat(x: float, y: float) -> tuple[float, float]:
    """Web Mercator (spherical) → longitude, latitude in degrees."""
    return math.degrees(x / _R), math.degrees(2 * math.atan(math.exp(y / _R)) - math.pi / 2)


def _to_mbtiles(cache: Cache, target: Path, progress, cancelled) -> dict:
    if cache.content != "mvt":
        raise TileCacheError("only vector packages (.vtpk) go to MBTiles; image caches go to GeoPackage")
    if target.exists():
        raise TileCacheError(f"{target.name} already exists — choose another file")
    xyz = xyz_levels(cache)
    have = cache.levels_with_tiles()
    t0 = time.perf_counter()
    con = sqlite3.connect(target)
    layers: dict[str, set] = {}
    attributes: dict[str, dict] = {}
    count = 0
    not_gzip = 0
    try:
        try:
            con.executescript("CREATE TABLE metadata (name TEXT, value TEXT);"
                              "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, "
                              "tile_data BLOB);"
                              "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);")
            con.execute("PRAGMA journal_mode=OFF")
            for i, lv in enumerate(have):
                if cancelled is not None and cancelled():
                    raise Cancelled("conversion cancelled")
                z = xyz[lv]
                rows = []
                for r, c, data in cache.tiles(lv):
                    if data[:2] != b"\x1f\x8b":
                        not_gzip += 1
                    nth = count + len(rows)
                    if nth < 200 or nth % 97 == 0 or not rows:        # layers from a sample of every level
                        for name, attrs in mvt_layers(data).items():
                            layers.setdefault(name, set()).add(z)
                            for k, kind in attrs.items():
                                attributes.setdefault(name, {}).setdefault(k, kind)
                    rows.append((z, c, (2 ** z - 1) - r, data))       # TMS: rows from the bottom
                    if len(rows) >= 2000:                             # bounded memory: write in batches
                        con.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", rows)
                        count += len(rows)
                        rows = []
                        if cancelled is not None and cancelled():
                            raise Cancelled("conversion cancelled")
                con.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", rows)
                con.commit()
                count += len(rows)
                if progress is not None:
                    progress(i + 1, len(have), f"level {lv}: tiles {count:,}")
            meta = _metadata(cache, xyz, have, layers, attributes)
            con.executemany("INSERT INTO metadata VALUES (?,?)", list(meta.items()))
            con.commit()
        finally:
            con.close()
    except BaseException:
        if target.exists():
            target.unlink()                                       # our own unfinished output, not user data
        raise
    style = None
    if cache.style:
        style = target.with_suffix(".style.json")
        style.write_bytes(cache.style)
    duration = time.perf_counter() - t0
    logger.info("esri_tilecache: %s → %s, %d MVT tiles, levels %s, %.1f s", cache.path.name, target.name, count,
                sorted(xyz[lv] for lv in have), duration)
    return {"path": target, "style": style, "tiles": count, "not_gzip": not_gzip, "layers": sorted(layers),
            "xyz_levels": sorted(xyz[lv] for lv in have), "duration_s": duration, "cache": cache}


def _metadata(cache: Cache, xyz: dict, have: list, layers: dict, attributes: dict) -> dict:
    zooms = sorted(xyz[lv] for lv in have)
    if cache.extent:
        x0, y0, x1, y1 = cache.extent
        lo0, la0 = _lonlat(max(x0, -HALF_EQUATOR), max(y0, -HALF_EQUATOR))
        lo1, la1 = _lonlat(min(x1, HALF_EQUATOR), min(y1, HALF_EQUATOR))
        bounds = (lo0, la0, lo1, la1)
    else:
        bounds = (-180.0, -85.0511, 180.0, 85.0511)
    center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    return {
        "name": cache.title or cache.path.stem, "format": "pbf", "type": "overlay", "version": "1",
        "description": cache.description or "", "attribution": cache.rights or "",
        "minzoom": str(zooms[0]), "maxzoom": str(zooms[-1]),
        "bounds": ",".join(f"{v:.6f}" for v in bounds), "center": f"{center[0]:.6f},{center[1]:.6f},{zooms[0]}",
        "json": json.dumps({"vector_layers": [{"id": m, "fields": attributes.get(m, {}), "minzoom": min(z),
                                               "maxzoom": max(z)} for m, z in sorted(layers.items())]},
                           ensure_ascii=False),
    }
