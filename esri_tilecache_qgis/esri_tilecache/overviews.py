"""GeoPackage overviews (pyramid) built on threads directly from the tiles.

GDAL builds overviews on one thread and decodes the whole base level (for a national cache about half a million
PNG tiles). Here the first overview level is computed directly from the source tiles (each target tile = f × f
source tiles), in parallel by bands of rows, and every further level from the previous one (2 × 2). Threads,
not processes: Pillow releases the GIL while decoding and resizing, and threads also work in frozen
applications and inside QGIS.

While building, the file is in WAL journal mode (readers do not block the writer — on Windows many reader
threads otherwise led to "database is locked"); afterwards it returns to the default mode, so the GeoPackage
stays a single file. On error or cancellation the incomplete overviews are deleted.

Elevation (2D gridded coverage, float32 TIFF): downsampling = mean of valid values (without `data_null`);
tile statistics go to `gpkg_2d_gridded_tile_ancillary`.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .geopackage import raster_table
from .reader import Cancelled, TileCacheError

logger = logging.getLogger(__name__)

OVERVIEW_FORMAT = "JPEG"
OVERVIEW_QUALITY = 82
LOCK_TIMEOUT_S = 120
JOURNAL_RESTORE_TIMEOUT_S = 60          # leaving WAL needs the file for itself
SOURCE_LEVELS_TABLE = "esri_tilecache_source_levels"
# where the lowest source level is looked up: (table, literal statement); the last one is for files written by
# the Lesná mapa applications
_SOURCE_LEVEL_LOOKUPS = (
    (SOURCE_LEVELS_TABLE, "SELECT zoom_from FROM esri_tilecache_source_levels WHERE table_name=?"),
    ("lj_povodne_urovne", "SELECT zoom_od FROM lj_povodne_urovne WHERE table_name=?"),
)


def overview_factors(width: int, height: int, start: int = 4, smallest_px: int = 512) -> list[int]:
    """Overview factors from `start` until the image fits into `smallest_px`."""
    out, f = [], start
    while max(width, height) / f >= smallest_px:
        out.append(f)
        f *= 2
    return out


def _q(table) -> str:
    """A table name as a quoted SQL identifier: a double quote inside the name is doubled, so it cannot end the identifier."""
    return '"' + str(table).replace('"', '""') + '"'


def _ro(path):
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=LOCK_TIMEOUT_S)


def _rw(path):
    return sqlite3.connect(path, timeout=LOCK_TIMEOUT_S)


def _journal_mode(path, mode: str) -> str:
    con = _rw(path)
    try:
        if mode.upper() != "WAL":
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return con.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
    finally:
        con.close()


def has_overviews(path, table: str) -> bool:
    """Is the pyramid complete? The smallest level is built last."""
    con = _ro(path)
    try:
        zmin = con.execute("SELECT min(zoom_level) FROM gpkg_tile_matrix WHERE table_name=?", (table,)).fetchone()[0]
        zb = con.execute(f"SELECT max(zoom_level) FROM {_q(table)}").fetchone()[0]  # nosec B608
        if zmin is None or zb is None or zmin >= zb:
            return True
        return con.execute(f"SELECT 1 FROM {_q(table)} WHERE zoom_level=? LIMIT 1", (zmin,)).fetchone() is not None  # nosec B608
    finally:
        con.close()


def record_source_levels(con, table: str, zoom_from: int) -> None:
    """Record the lowest source (non-overview) zoom level; called by the converter."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {SOURCE_LEVELS_TABLE} "
                "(table_name TEXT PRIMARY KEY, zoom_from INTEGER NOT NULL)")
    con.execute(f"INSERT OR REPLACE INTO {SOURCE_LEVELS_TABLE} VALUES (?, ?)", (table, zoom_from))


def source_zoom(path, table) -> int:
    """The lowest SOURCE level — only overviews lie below it.

    A multi-level Esri cache keeps all copied levels as source levels (recorded by the converter). Without the
    record (a GeoPackage from GDAL or another tool) only the finest level with tiles is a source level.
    """
    con = _ro(path)
    try:
        for name, sql in _SOURCE_LEVEL_LOOKUPS:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone():
                r = con.execute(sql, (table,)).fetchone()
                if r is not None:
                    return r[0]
        return con.execute(f"SELECT max(zoom_level) FROM {_q(table)}").fetchone()[0]  # nosec B608
    finally:
        con.close()


def _elevation(path, table) -> tuple[bool, float | None]:
    con = _ro(path)
    try:
        t = con.execute("SELECT data_type FROM gpkg_contents WHERE table_name=?", (table,)).fetchone()
        if not t or t[0] != "2d-gridded-coverage":
            return False, None
        r = con.execute("SELECT data_null FROM gpkg_2d_gridded_coverage_ancillary WHERE tile_matrix_set_name=?",
                        (table,)).fetchone()
        return True, (r[0] if r else None)
    finally:
        con.close()


def _band(args):
    """On a worker thread: target tiles of level `z_target` for target rows `r0..r1`."""
    import io

    from PIL import Image

    from . import encoding

    path, table, z_source, z_target, factor, r0, r1, t, fmt, quality, data_null = args
    if data_null is not None:
        return _band_elevation(path, table, z_source, z_target, factor, r0, r1, t, data_null)
    con = _ro(path)
    out = []
    try:
        rows = con.execute(f"SELECT tile_column, tile_row, tile_data FROM {_q(table)} "  # nosec B608
                           "WHERE zoom_level=? AND tile_row>=? AND tile_row<?",
                           (z_source, r0 * factor, r1 * factor)).fetchall()
        targets: dict = {}
        for c, r, data in rows:
            targets.setdefault((c // factor, r // factor), []).append((c % factor, r % factor, data))
        for (c, r), sources in targets.items():
            canvas = Image.new("RGBA", (t * factor, t * factor), (0, 0, 0, 0))
            for dc, dr, data in sources:
                canvas.paste(Image.open(io.BytesIO(data)).convert("RGBA"), (dc * t, dr * t))
            data = encoding.encode(encoding.reduce(canvas, factor), fmt, quality)
            if data is not None:
                out.append((z_target, c, r, data))
    finally:
        con.close()
    return out


def _band_elevation(path, table, z_source, z_target, factor, r0, r1, t, data_null):
    """Elevation: mean of the valid values of each f × f block (without `data_null`)."""
    import io

    import numpy as np
    from PIL import Image

    from .lerc import to_tiff

    con = _ro(path)
    out = []
    try:
        rows = con.execute(f"SELECT tile_column, tile_row, tile_data FROM {_q(table)} "  # nosec B608
                           "WHERE zoom_level=? AND tile_row>=? AND tile_row<?",
                           (z_source, r0 * factor, r1 * factor)).fetchall()
    finally:
        con.close()
    targets: dict = {}
    for c, r, data in rows:
        targets.setdefault((c // factor, r // factor), []).append((c % factor, r % factor, data))
    for (c, r), sources in targets.items():
        canvas = np.full((t * factor, t * factor), np.nan, dtype="float64")
        for dc, dr, data in sources:
            a = np.asarray(Image.open(io.BytesIO(data)), dtype="float64")
            a[a == data_null] = np.nan
            canvas[dr * t:(dr + 1) * t, dc * t:(dc + 1) * t] = a
        blocks = canvas.reshape(t, factor, t, factor)
        n = np.sum(~np.isnan(blocks), axis=(1, 3))
        if not n.any():
            continue
        with np.errstate(invalid="ignore"):
            mean = np.nansum(blocks, axis=(1, 3)) / np.where(n == 0, 1, n)
        mean[n == 0] = data_null
        valid = mean[n > 0]
        stats = (float(valid.min()), float(valid.max()), float(valid.mean()), float(valid.std()))
        out.append((z_target, c, r, to_tiff(mean.astype("float32")), stats))
    return out


def delete_overviews(path, table: str, zoom_from: int | None = None) -> None:
    """Delete the tiles below the source levels (overviews we built and can rebuild — never source data)."""
    zoom_from = zoom_from if zoom_from is not None else source_zoom(path, table)
    con = _rw(path)
    try:
        con.execute(f"DELETE FROM {_q(table)} WHERE zoom_level < ?", (zoom_from,))  # nosec B608
        con.commit()
    finally:
        con.close()


def build_overviews(path, table: str | None = None, *, start: int | None = None, threads: int | None = None,
                    progress=None, cancelled=None, fmt: str = OVERVIEW_FORMAT,
                    quality: int = OVERVIEW_QUALITY) -> list[int]:
    """Build overviews from factor `start` (a power of 2) down to a single tile, on several threads.

    `table` None = the only raster table of the file. `start` None = ×4 for a single image level (line maps with
    text stay readable at ×2 from the base level; skipping it saves most time and space), ×2 for elevation.
    Below several source levels (an Esri cache) the pyramid always continues by 2.
    Returns the factors of the built levels relative to the coarsest source level.
    """
    import math
    import os

    path = Path(path)
    if table is None:
        table = raster_table(path)
        if table is None:
            raise TileCacheError(f"{path.name} has no raster tile table")
    zb = source_zoom(path, table)
    con = _rw(path)
    t = con.execute("SELECT tile_width FROM gpkg_tile_matrix WHERE table_name=? AND zoom_level=?",
                    (table, zb)).fetchone()[0]
    zooms = {z: (w, h) for z, w, h in con.execute(
        "SELECT zoom_level, matrix_width, matrix_height FROM gpkg_tile_matrix WHERE table_name=?", (table,))}
    con.close()
    con = _ro(path)
    try:
        zmax_data = con.execute(f"SELECT max(zoom_level) FROM {_q(table)}").fetchone()[0]  # nosec B608
    finally:
        con.close()
    is_elev, null = _elevation(path, table)
    if start is None:
        start = 2 if is_elev else 4
    if zmax_data is not None and zb < zmax_data:
        start = 2
    levels = [z for z in range(zb - int(round(math.log2(start))), -1, -1) if z in zooms]
    threads = threads or max(1, os.cpu_count() or 2)
    done = []
    t0 = time.perf_counter()
    try:
        original_mode = _journal_mode(path, "WAL")
    except sqlite3.OperationalError:
        logger.warning("esri_tilecache: %s cannot switch to WAL (file open elsewhere)", path)
        original_mode = None
    try:
        done = _build_levels(path, table, zb, t, zooms, levels, start, threads, progress, cancelled, fmt, quality,
                             (null if null is not None else -9999.0) if is_elev else None)
    except Cancelled:
        raise
    except BaseException:
        try:
            delete_overviews(path, table, zb)
        except Exception:  # noqa: BLE001
            logger.exception("esri_tilecache: deleting incomplete overviews of %s failed", path)
        raise
    finally:
        try:
            if original_mode is not None:
                target = "DELETE" if original_mode.lower() == "wal" else original_mode
                deadline = time.monotonic() + JOURNAL_RESTORE_TIMEOUT_S
                while True:
                    try:
                        if _journal_mode(path, target).lower() == target.lower():
                            break
                    except sqlite3.OperationalError:
                        pass
                    if time.monotonic() > deadline:
                        logger.warning("esri_tilecache: %s stayed in WAL mode (file open elsewhere)", path)
                        break
                    time.sleep(0.5)
        except Exception:  # noqa: BLE001
            logger.exception("esri_tilecache: restoring the journal mode of %s failed", path)
    logger.info("esri_tilecache: overviews of %s — %s in %.0f s (%d threads)", path.name, done,
                time.perf_counter() - t0, threads)
    return done


def _iterate(pool, tasks, zb, table, path):
    """`pool.map` with clean-up: an interrupted build deletes all overviews (an incomplete level shows holes)."""
    try:
        yield from pool.map(_band, tasks)
    except BaseException:
        delete_overviews(path, table, zb)
        raise


def _build_levels(path, table, zb, t, zooms, levels, start, threads, progress, cancelled, fmt, quality, data_null):
    from concurrent.futures import ThreadPoolExecutor

    from .encoding import no_garbage_collection
    from .geopackage import insert_elevation

    done = []
    pool = ThreadPoolExecutor(threads)
    gc_ctx = no_garbage_collection()
    gc_ctx.__enter__()
    try:
        z_source, factor = zb, start
        for z in levels:
            height = zooms[z][1]
            band = 2 if factor > 2 else 8
            tasks = [(str(path), table, z_source, z, factor, r, min(r + band, height), t, fmt, quality, data_null)
                     for r in range(0, height, band)]
            con = _rw(path)
            con.execute("PRAGMA synchronous=OFF")
            try:
                for i, rows in enumerate(_iterate(pool, tasks, zb, table, path)):
                    if cancelled is not None and cancelled():
                        con.close()
                        delete_overviews(path, table, zb)
                        raise Cancelled("overview build cancelled — overviews deleted")
                    if rows:
                        if len(rows[0]) == 5:
                            insert_elevation(con, table, rows)
                        else:
                            con.executemany(f'INSERT OR REPLACE INTO "{table}" '
                                            "(zoom_level, tile_column, tile_row, tile_data) VALUES (?,?,?,?)", rows)
                        con.commit()
                    if progress is not None:
                        progress(i + 1, len(tasks), f"level ×{2 ** (zb - z)}: band {i + 1}/{len(tasks)}")
            finally:
                con.close()
            done.append(2 ** (zb - z))
            z_source, factor = z, 2
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        gc_ctx.__exit__(None, None, None)
    return done
