"""esri-tilecache — open Esri tile caches as standard open formats, without re-encoding the tiles.

    import esri_tilecache as etc

    with etc.open_cache("map.tpk") as c:          # .tpk / .tpkx / .vtpk / cache folder (conf.xml, root.json)
        print(c.summary())
    r = etc.to_geopackage("map.tpk", "map.gpkg")     # images byte for byte; LERC elevation → float32
    etc.build_overviews("map.gpkg")                   # pyramid below the source levels, multi-threaded
    etc.to_mbtiles("vectors.vtpk", "vectors.mbtiles") # MVT unchanged; Web Mercator only

The GeoPackage is written with plain SQLite (no GDAL needed); GDAL and QGIS read it.
"""
from .convert import is_valid_image, level_chain, tile_matrix, to_geopackage
from .geopackage import raster_table
from .mbtiles import mvt_layers, to_mbtiles, xyz_levels
from .overviews import (OVERVIEW_FORMAT, OVERVIEW_QUALITY, build_overviews, delete_overviews, has_overviews,
                        overview_factors, source_zoom)
from .reader import Cache, Cancelled, TileCacheError, bundle_occupancy, bundle_tiles, open_cache

__version__ = "0.1.1"

__all__ = ["Cache", "Cancelled", "TileCacheError", "open_cache", "bundle_tiles", "bundle_occupancy",
           "to_geopackage", "level_chain", "tile_matrix", "is_valid_image", "to_mbtiles", "mvt_layers",
           "xyz_levels", "build_overviews", "delete_overviews", "has_overviews", "overview_factors", "source_zoom",
           "raster_table", "OVERVIEW_FORMAT", "OVERVIEW_QUALITY", "__version__"]
