"""Esri cache → GeoPackage. Image tiles are copied byte for byte; elevation tiles (LERC) are decoded to float32
TIFF (2D gridded coverage). Vector packages (.vtpk) go to MBTiles (`mbtiles.py`).

Levels (`levels`):
* `"auto"` (default) — all levels with tiles that form, from the finest one up, a chain with a resolution
  ratio of 2 (the Esri and Web Mercator standard) are copied as they are; missing coarser levels are computed
  by `build_overviews`. A single-level cache gives just that level.
* `"finest"` — only the finest level.

The GeoPackage tile matrix follows GDAL: a 2^Z × 2^Z matrix whose top-left corner lies on the Esri tile grid,
levels 0…Z (coarser ones empty, for overviews), and the real data extent in `gpkg_contents`.
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path

from . import geopackage as _g
from .reader import IMAGE_FORMATS, Cache, Cancelled, TileCacheError, bundle_occupancy, bundle_tiles, open_cache

logger = logging.getLogger(__name__)


def is_valid_image(data: bytes) -> bool:
    """The tile starts with a PNG or JPEG signature (otherwise GDAL cannot decode it and the map has a hole)."""
    return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"


def level_chain(cache: Cache, levels: str = "auto") -> list[int]:
    """Esri levels from the finest up that are copied (resolution ratio 2)."""
    have = cache.levels_with_tiles()
    if not have:
        raise TileCacheError("the cache has no tiles")
    by_res = sorted(have, key=lambda lv: cache.levels[lv])
    chain = [by_res[0]]
    if levels == "finest":
        return chain
    for lv in by_res[1:]:
        ratio = cache.levels[lv] / cache.levels[chain[-1]]
        if abs(ratio - 2.0) > 2e-6:
            break
        chain.append(lv)
    return chain


def _tile_range(cache: Cache, level: int) -> tuple[int, int, int, int]:
    """(row_min, col_min, row_max, col_max) of the rows and columns that hold tiles (max exclusive)."""
    if cache.storage == "exploded":
        t = cache.exploded[level]
        return min(x[0] for x in t), min(x[1] for x in t), max(x[0] for x in t) + 1, max(x[1] for x in t) + 1
    ranges = [x for x in (bundle_occupancy(cache._source, b) for b in cache.bundles_of_level(level)) if x]
    if not ranges:
        raise TileCacheError(f"level {level} has no tiles")
    return (min(x[0] for x in ranges), min(x[1] for x in ranges),
            max(x[2] for x in ranges), max(x[3] for x in ranges))


def tile_matrix(cache: Cache, chain: list[int]) -> dict:
    """GeoPackage tile matrix for a chain of levels (finest first)."""
    finest = chain[0]
    res, t = cache.levels[finest], cache.tile_size
    step = t * res
    r_min, c_min, r_max, c_max = _tile_range(cache, finest)
    if cache.extent:                                      # clip to the data extent (conf.cdi / fullExtent)
        x0, y0, x1, y1 = cache.extent
        c_min = max(c_min, int((x0 - cache.origin_x) // step))
        c_max = min(c_max, int((x1 - cache.origin_x) // step) + 1)
        r_min = max(r_min, int((cache.origin_y - y1) // step))
        r_max = min(r_max, int((cache.origin_y - y0) // step) + 1)
    if c_max <= c_min or r_max <= r_min:
        raise TileCacheError("the data extent (conf.cdi / fullExtent) does not overlap the tiles")
    d_c0, d_r0, d_c1, d_r1 = c_min, r_min, c_max, r_max   # data: tiles of the finest level inside the extent
    f = 2 ** (len(chain) - 1)                             # align to the grid of the coarsest copied level
    c_min, r_min = (c_min // f) * f, (r_min // f) * f
    c_max, r_max = -(-c_max // f) * f, -(-r_max // f) * f
    width, height = c_max - c_min, r_max - r_min          # in tiles of the finest level
    zmax = max(0, math.ceil(math.log2(max(width, height))))
    n = 2 ** zmax
    x_left = cache.origin_x + c_min * step
    y_top = cache.origin_y - r_min * step
    levels = [_g.TileLevel(z, 2 ** z, 2 ** z, t, res * 2 ** (zmax - z)) for z in range(zmax + 1)]
    return {"res": res, "t": t, "step": step, "r_min": r_min, "c_min": c_min, "r_max": r_max, "c_max": c_max,
            "width_px": (d_c1 - d_c0) * t, "height_px": (d_r1 - d_r0) * t, "zmax": zmax,
            "matrix_bounds": (x_left, y_top - n * step, x_left + n * step, y_top),
            # gpkg_contents holds only the data (GDAL derives the raster size from it); the matrix is wider
            "data_bounds": (cache.origin_x + d_c0 * step, cache.origin_y - d_r1 * step,
                            cache.origin_x + d_c1 * step, cache.origin_y - d_r0 * step),
            "gpkg_levels": levels}


def to_geopackage(source, target, table: str | None = None, *, levels: str = "auto", epsg: int | None = None,
                  progress=None, cancelled=None) -> dict:
    """Esri cache (a path or an open `Cache`) → GeoPackage `target` (a new file).

    `progress(done, total, text)`; `cancelled()` → True interrupts (the unfinished file is deleted).
    Returns a dict with counts, size, levels and `overviews_missing` (zoom levels without tiles).
    """
    own = not isinstance(source, Cache)
    cache = open_cache(source) if own else source
    try:
        return _to_geopackage(cache, Path(target), table, levels, epsg, progress, cancelled)
    finally:
        if own:
            cache.close()


def _to_geopackage(cache, target, table, levels, epsg, progress, cancelled):
    if cache.content == "mvt":
        raise TileCacheError("vector tiles (.vtpk) are converted to MBTiles — use `to_mbtiles`")
    if cache.content == "image" and cache.format not in IMAGE_FORMATS:
        raise TileCacheError(f"tile format {cache.format} is not supported")
    if target.exists():
        raise TileCacheError(f"{target.name} already exists — choose another file")
    chain = level_chain(cache, levels)
    m = tile_matrix(cache, chain)
    epsg = epsg or cache.epsg
    table = table or "tiles"
    elevation = cache.content == "lerc"
    bundles = [(lv, b) for lv in chain for b in (cache.bundles_of_level(lv) if cache.storage != "exploded" else [None])]
    logger.info("esri_tilecache: %s → %s, %d × %d px, %.4f units/px, EPSG:%s, Esri levels %s → GPKG %d…%d",
                cache.path.name, target.name, m["width_px"], m["height_px"], m["res"], epsg, chain,
                m["zmax"] - len(chain) + 1, m["zmax"])
    t0 = time.perf_counter()
    con = _g.create(target, table, epsg=epsg, wkt=cache.wkt, bounds=m["matrix_bounds"], levels=m["gpkg_levels"],
                    description=cache.title, elevation=elevation)
    con.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=? WHERE table_name=?",
                (*m["data_bounds"], table))
    from .overviews import record_source_levels
    record_source_levels(con, table, m["zmax"] - len(chain) + 1)   # overviews never overwrite source levels
    con.commit()
    stats: dict = {}
    per_level: dict = {}
    count = 0
    try:
        try:
            con.execute("PRAGMA journal_mode=OFF")
            con.execute("PRAGMA synchronous=OFF")
            if elevation:
                from .lerc import NODATA, decode_to_tiff
            for i, (lv, b) in enumerate(bundles):
                if cancelled is not None and cancelled():
                    raise Cancelled("conversion cancelled")
                k = chain.index(lv)                        # how many times coarser than the finest level
                zoom = m["zmax"] - k
                f = 2 ** k
                r0, c0 = m["r_min"] // f, m["c_min"] // f
                r1, c1 = -(-m["r_max"] // f), -(-m["c_max"] // f)
                tiles = cache.tiles(lv, stats) if b is None else bundle_tiles(cache._source, b, stats)
                rows = [(zoom, c - c0, r - r0, data) for r, c, data in tiles if r0 <= r < r1 and c0 <= c < c1]
                if not elevation:
                    good = [x for x in rows if is_valid_image(x[3])]
                    if len(good) != len(rows):
                        stats["invalid"] = stats.get("invalid", 0) + len(rows) - len(good)
                        rows = good
                per_level[zoom] = per_level.get(zoom, 0) + len(rows)
                if elevation:                              # in batches — float32 TIFF is ~4× larger than LERC
                    for j in range(0, len(rows), 256):
                        _g.insert_elevation(con, table, [(z, c, r, *decode_to_tiff(d, m["t"], NODATA))
                                                         for z, c, r, d in rows[j:j + 256]])
                else:
                    _g.insert(con, table, rows)
                con.commit()
                count += len(rows)
                if progress is not None:
                    bad = stats.get("damaged", 0) + stats.get("invalid", 0)
                    progress(i + 1, len(bundles), f"{'bundle' if b else 'level'} {i + 1}/{len(bundles)}, "
                             f"tiles {count:,}" + (f", damaged {bad}" if bad else ""))
            # source levels are only those from the finest one up without a gap; coarser empty ones (e.g. all
            # tiles invalid, as in levels 0 and 1 of the Esri sample Usa.tpkx) are computed as overviews
            zoom_from = m["zmax"]
            while zoom_from - 1 >= m["zmax"] - len(chain) + 1 and per_level.get(zoom_from - 1):
                zoom_from -= 1
            if zoom_from != m["zmax"] - len(chain) + 1:
                logger.warning("esri_tilecache: levels coarser than GPKG %d are empty — overviews will fill them",
                               zoom_from)
            record_source_levels(con, table, zoom_from)
            con.commit()
        finally:
            con.close()
    except BaseException:
        if target.exists():
            target.unlink()                                # our own unfinished output, not user data
        raise
    duration = time.perf_counter() - t0
    copied = list(range(zoom_from, m["zmax"] + 1))
    logger.info("esri_tilecache: done — %d tiles, %.1f s, %.1f MB", count, duration, target.stat().st_size / 1e6)
    return {"path": target, "table": table, "tiles": count, "damaged": stats.get("damaged", 0),
            "invalid": stats.get("invalid", 0), "width": m["width_px"], "height": m["height_px"],
            "resolution": m["res"], "epsg": epsg, "duration_s": duration, "esri_levels": chain,
            "gpkg_levels": copied, "overviews_missing": list(range(0, copied[0])), "elevation": elevation,
            "cache": cache}
