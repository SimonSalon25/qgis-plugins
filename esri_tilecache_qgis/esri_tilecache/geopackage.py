"""Writing tiles to a GeoPackage with plain SQLite (no GDAL) — OGC GeoPackage 1.3 (12-128r18).

Why without GDAL: the package must also work where GDAL's Python bindings or `rasterio` are missing.
The result is read by GDAL and QGIS — verified by the tests pixel by pixel.

* `data_type = 'tiles'`: PNG/JPEG tiles (WebP with the `gpkg_webp` extension) stored without re-encoding;
* `data_type = '2d-gridded-coverage'`: elevations as float32 TIFF tiles (OGC 17-066r1 extension).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

APPLICATION_ID = 0x47504B47          # 'GPKG'
USER_VERSION = 10300                 # 1.3.0
GRIDDED_URL = "http://docs.opengeospatial.org/is/17-066r1/17-066r1.html"

_BASE = """
CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
  organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
  description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
  CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL,
  geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
  CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name));
CREATE TABLE gpkg_tile_matrix_set (table_name TEXT NOT NULL PRIMARY KEY, srs_id INTEGER NOT NULL,
  min_x DOUBLE NOT NULL, min_y DOUBLE NOT NULL, max_x DOUBLE NOT NULL, max_y DOUBLE NOT NULL,
  CONSTRAINT fk_gtms_table_name FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
  CONSTRAINT fk_gtms_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id));
CREATE TABLE gpkg_tile_matrix (table_name TEXT NOT NULL, zoom_level INTEGER NOT NULL, matrix_width INTEGER NOT NULL,
  matrix_height INTEGER NOT NULL, tile_width INTEGER NOT NULL, tile_height INTEGER NOT NULL,
  pixel_x_size DOUBLE NOT NULL, pixel_y_size DOUBLE NOT NULL,
  CONSTRAINT pk_ttm PRIMARY KEY (table_name, zoom_level),
  CONSTRAINT fk_tmm_table_name FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name));
CREATE TABLE gpkg_extensions (table_name TEXT, column_name TEXT, extension_name TEXT NOT NULL,
  definition TEXT NOT NULL, scope TEXT NOT NULL, CONSTRAINT ge_tce UNIQUE (table_name, column_name, extension_name));
"""

_GRIDDED = """
CREATE TABLE IF NOT EXISTS gpkg_2d_gridded_coverage_ancillary (id INTEGER PRIMARY KEY AUTOINCREMENT,
  tile_matrix_set_name TEXT NOT NULL UNIQUE, datatype TEXT NOT NULL DEFAULT 'integer',
  scale REAL NOT NULL DEFAULT 1.0, offset REAL NOT NULL DEFAULT 0.0, precision REAL DEFAULT 1.0, data_null REAL,
  grid_cell_encoding TEXT DEFAULT 'grid-value-is-center', uom TEXT, field_name TEXT DEFAULT 'Height',
  quantity_definition TEXT DEFAULT 'Height',
  CONSTRAINT fk_g2dgtct_name FOREIGN KEY('tile_matrix_set_name') REFERENCES gpkg_tile_matrix_set (table_name),
  CHECK (datatype IN ('integer','float')));
CREATE TABLE IF NOT EXISTS gpkg_2d_gridded_tile_ancillary (id INTEGER PRIMARY KEY AUTOINCREMENT,
  tpudt_name TEXT NOT NULL, tpudt_id INTEGER NOT NULL, scale REAL NOT NULL DEFAULT 1.0,
  offset REAL NOT NULL DEFAULT 0.0, min REAL DEFAULT NULL, max REAL DEFAULT NULL, mean REAL DEFAULT NULL,
  std_dev REAL DEFAULT NULL, CONSTRAINT fk_g2dgtat_name FOREIGN KEY (tpudt_name) REFERENCES gpkg_contents(table_name),
  UNIQUE (tpudt_name, tpudt_id));
"""

_REQUIRED_SRS = [
    ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
    ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
    ("WGS 84 geodetic", 4326, "EPSG", 4326,
     'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
     'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
     'AUTHORITY["EPSG","9122"]],AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]',
     "longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid"),
]


@dataclass(frozen=True)
class TileLevel:
    zoom: int
    columns: int
    rows: int
    tile_size: int
    pixel: float


def crs_definition(epsg: int | None, wkt: str | None) -> tuple[str, str]:
    """(name, WKT1) of a CRS — from the EPSG code via pyproj or GDAL/OSR, else the cache WKT, else 'undefined'."""
    if epsg:
        try:
            from pyproj import CRS
            crs = CRS.from_epsg(epsg)
            return crs.name, crs.to_wkt("WKT1_GDAL")
        except Exception:  # noqa: BLE001 — pyproj missing or unknown code
            pass
        try:
            from osgeo import osr
            s = osr.SpatialReference()
            if s.ImportFromEPSG(int(epsg)) == 0:
                return s.GetName() or f"EPSG:{epsg}", s.ExportToWkt()
        except Exception:  # noqa: BLE001 — GDAL missing (QGIS always has it)
            pass
    return ("Esri cache", wkt) if wkt else ("Undefined", "undefined")


def create(path, table: str, *, epsg: int | None, wkt: str | None, bounds: tuple, levels: list[TileLevel],
           description: str = "", elevation: bool = False, webp: bool = False, uom: str | None = "m",
           data_null: float | None = -9999.0) -> sqlite3.Connection:
    """New GeoPackage (or a new table in an existing one) and a connection for writing tiles.

    `bounds` = (min_x, min_y, max_x, max_y) of the tile matrix — its top-left corner is the origin of tile
    [0, 0] of every level. `elevation=True` → 2D gridded coverage (float32 TIFF).
    """
    path = Path(path)
    new = not path.exists()
    con = sqlite3.connect(path)
    if new:
        con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        con.execute(f"PRAGMA user_version = {USER_VERSION}")
        con.executescript(_BASE)
        con.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", _REQUIRED_SRS)
    if con.execute("SELECT 1 FROM gpkg_contents WHERE table_name=?", (table,)).fetchone():
        con.close()
        raise ValueError(f"table {table} already exists in {path.name}")
    srs_id = _srs(con, epsg, wkt)
    x0, y0, x1, y1 = bounds
    con.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY AUTOINCREMENT, zoom_level INTEGER NOT NULL, '
                "tile_column INTEGER NOT NULL, tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL, "
                "UNIQUE (zoom_level, tile_column, tile_row))")
    con.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, description, min_x, min_y, max_x, "
                "max_y, srs_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (table, "2d-gridded-coverage" if elevation else "tiles", table, description, x0, y0, x1, y1, srs_id))
    con.execute("INSERT INTO gpkg_tile_matrix_set VALUES (?,?,?,?,?,?)", (table, srs_id, x0, y0, x1, y1))
    con.executemany("INSERT INTO gpkg_tile_matrix VALUES (?,?,?,?,?,?,?,?)",
                    [(table, lv.zoom, lv.columns, lv.rows, lv.tile_size, lv.tile_size, lv.pixel, lv.pixel)
                     for lv in levels])
    if webp:
        con.execute("INSERT OR IGNORE INTO gpkg_extensions VALUES (?,?,?,?,?)",
                    (table, "tile_data", "gpkg_webp", "http://www.geopackage.org/spec120/#extension_tiles_webp",
                     "read-write"))
    if elevation:
        con.executescript(_GRIDDED)
        con.executemany("INSERT OR IGNORE INTO gpkg_extensions VALUES (?,?,?,?,?)", [
            ("gpkg_2d_gridded_coverage_ancillary", None, "gpkg_2d_gridded_coverage", GRIDDED_URL, "read-write"),
            ("gpkg_2d_gridded_tile_ancillary", None, "gpkg_2d_gridded_coverage", GRIDDED_URL, "read-write"),
            (table, "tile_data", "gpkg_2d_gridded_coverage", GRIDDED_URL, "read-write")])
        con.execute("INSERT INTO gpkg_2d_gridded_coverage_ancillary (tile_matrix_set_name, datatype, scale, offset, "
                    "precision, data_null, grid_cell_encoding, uom) VALUES (?,?,?,?,?,?,?,?)",
                    (table, "float", 1.0, 0.0, 0.0, data_null, "grid-value-is-center", uom))
    con.commit()
    return con


def _srs(con, epsg, wkt) -> int:
    if epsg in (4326, -1, 0):
        return epsg
    name, definition = crs_definition(epsg, wkt)
    if epsg:
        if not con.execute("SELECT 1 FROM gpkg_spatial_ref_sys WHERE srs_id=?", (epsg,)).fetchone():
            con.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
                        (name, epsg, "EPSG", epsg, definition, ""))
        return epsg
    r = con.execute("SELECT srs_id FROM gpkg_spatial_ref_sys WHERE definition=?", (definition,)).fetchone()
    if r:
        return r[0]
    new = max(100000, (con.execute("SELECT max(srs_id) FROM gpkg_spatial_ref_sys").fetchone()[0] or 0) + 1)
    con.execute("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", (name, new, "NONE", new, definition, ""))
    return new


def insert(con: sqlite3.Connection, table: str, rows) -> None:
    """`rows` = [(zoom, column, row, bytes)]."""
    con.executemany(f'INSERT OR REPLACE INTO "{table}" (zoom_level, tile_column, tile_row, tile_data) '
                    "VALUES (?,?,?,?)", rows)


def insert_elevation(con: sqlite3.Connection, table: str, rows) -> None:
    """`rows` = [(zoom, column, row, tiff, (min, max, mean, std_dev))]."""
    for z, c, r, data, stats in rows:
        cur = con.execute(f'INSERT OR REPLACE INTO "{table}" (zoom_level, tile_column, tile_row, tile_data) '
                          "VALUES (?,?,?,?)", (z, c, r, data))
        con.execute("INSERT OR REPLACE INTO gpkg_2d_gridded_tile_ancillary (tpudt_name, tpudt_id, scale, offset, "
                    "min, max, mean, std_dev) VALUES (?,?,1.0,0.0,?,?,?,?)", (table, cur.lastrowid, *stats))


def raster_table(path) -> str | None:
    """The first raster (tile or gridded coverage) table of a GeoPackage."""
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        r = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type IN "
                        "('tiles', '2d-gridded-coverage') ORDER BY table_name").fetchone()
        return r[0] if r else None
    finally:
        con.close()
