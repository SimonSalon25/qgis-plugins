# SPDX-License-Identifier: GPL-2.0-or-later
"""Processing algorithms: Esri tile caches → GeoPackage / MBTiles (library `esri_tilecache`, bundled, MIT)."""
from __future__ import annotations

import os
import sqlite3

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingContext, QgsProcessingException,
                       QgsProcessingOutputString, QgsProcessingParameterBoolean, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterString, QgsProject,
                       QgsRasterLayer, QgsVectorTileLayer)

from . import esri_tilecache as etc

CACHE_FILTER = "Esri tile caches (*.tpk *.tpkx *.vtpk conf.xml root.json);;All files (*.*)"


class _Feedback:
    """Adapter: library progress/cancel callbacks → QgsProcessingFeedback."""

    def __init__(self, feedback, start=0.0, span=100.0):
        self.f, self.start, self.span = feedback, start, span

    def progress(self, done, total, text):
        if total:
            self.f.setProgress(self.start + self.span * done / total)
        self.f.setProgressText(text)

    def cancelled(self):
        return self.f.isCanceled()


def build_overviews(path: str, table: str, feedback, start=50.0) -> str:
    """Overviews with the library (needs numpy + Pillow); without Pillow GDAL builds them (slower, one thread)."""
    fb = _Feedback(feedback, start, 100.0 - start)
    try:
        import PIL  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        from osgeo import gdal
        feedback.pushInfo("Pillow is not available — overviews are built by GDAL (one thread).")
        ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_UPDATE, open_options=[f"TABLE={table}"])
        factors, f = [], 2
        while max(ds.RasterXSize, ds.RasterYSize) / f >= 256:
            factors.append(f)
            f *= 2
        if factors:
            ds.BuildOverviews("AVERAGE", factors, callback=lambda p, *_a: (fb.progress(p, 1.0, "GDAL overviews"),
                                                                            not feedback.isCanceled())[1])
        ds = None
        return f"GDAL overviews {factors}"
    done = etc.build_overviews(path, table, progress=fb.progress, cancelled=fb.cancelled)
    return f"overviews {done}"


class ConvertTileCache(QgsProcessingAlgorithm):
    INPUT, OUTPUT, TABLE, OVERVIEWS, LOAD, SUMMARY = "INPUT", "OUTPUT", "TABLE", "OVERVIEWS", "LOAD", "SUMMARY"

    def name(self):
        return "convert_tile_cache"

    def displayName(self):
        return "Esri tile cache → GeoPackage"

    def shortHelpString(self):
        return ("Converts an Esri tile cache (.tpk Compact V1 or V2, .tpkx, or a cache folder — select its conf.xml "
                "or root.json) to a GeoPackage. Image tiles are copied byte for byte (no quality loss); all levels of "
                "a multi-level cache are kept. LERC elevation caches become a float32 gridded coverage (needs the "
                "Python package 'lerc'). Damaged or invalid tiles are skipped and reported.\n\n"
                "GDAL cannot read the older Compact V1 .tpk; this tool can. Method and verification: "
                "https://github.com/SimonSalon25/esri-tilecache")

    def createInstance(self):
        return ConvertTileCache()

    def initAlgorithm(self, config=None):
        p = QgsProcessingParameterFile(self.INPUT, "Esri tile cache (.tpk, .tpkx, conf.xml, root.json)",
                                       fileFilter=CACHE_FILTER)
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, "GeoPackage", "GeoPackage (*.gpkg)"))
        self.addParameter(QgsProcessingParameterString(self.TABLE, "Table name", "tiles"))
        self.addParameter(QgsProcessingParameterBoolean(self.OVERVIEWS, "Build overviews (pyramid)", True))
        self.addParameter(QgsProcessingParameterBoolean(self.LOAD, "Add the result to the map", True))
        self.addOutput(QgsProcessingOutputString(self.SUMMARY, "Summary"))

    def processAlgorithm(self, parameters, context, feedback):
        src = self.parameterAsFile(parameters, self.INPUT, context)
        dst = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        table = self.parameterAsString(parameters, self.TABLE, context) or "tiles"
        try:
            with etc.open_cache(src) as cache:
                feedback.pushInfo(cache.summary())
                if cache.content == "mvt":
                    raise QgsProcessingException("This is a vector tile package — use 'Esri vector tile package → "
                                                 "MBTiles', or open the .vtpk directly in QGIS.")
                fb = _Feedback(feedback, 0.0, 50.0 if self.parameterAsBool(parameters, self.OVERVIEWS, context)
                               else 100.0)
                r = etc.to_geopackage(cache, dst, table, progress=fb.progress, cancelled=fb.cancelled)
        except etc.Cancelled:
            raise QgsProcessingException("Cancelled — no partial file was left.")
        except etc.TileCacheError as exc:
            raise QgsProcessingException(str(exc))
        lines = [f"{r['tiles']:,} tiles copied, {r['width']} × {r['height']} px, EPSG:{r['epsg']}, "
                 f"Esri levels {r['esri_levels']}"]
        bad = r["damaged"] + r["invalid"]
        if bad:
            lines.append(f"{r['damaged']} damaged and {r['invalid']} invalid tiles were skipped (holes in the map).")
            feedback.reportError(lines[-1], fatalError=False)
        if self.parameterAsBool(parameters, self.OVERVIEWS, context) and r["overviews_missing"]:
            try:
                lines.append(build_overviews(dst, table, feedback))
            except etc.Cancelled:
                lines.append("overviews cancelled (the GeoPackage is complete, only without overviews)")
        summary = "\n".join(lines)
        feedback.pushInfo(summary)
        if self.parameterAsBool(parameters, self.LOAD, context):
            name = os.path.splitext(os.path.basename(dst))[0]
            context.addLayerToLoadOnCompletion(dst, QgsProcessingContext.LayerDetails(name, context.project(),
                                                                                       self.OUTPUT))
        return {self.OUTPUT: dst, self.SUMMARY: summary}


class ConvertVectorTilePackage(QgsProcessingAlgorithm):
    INPUT, OUTPUT, LOAD, SUMMARY = "INPUT", "OUTPUT", "LOAD", "SUMMARY"

    def __init__(self):
        super().__init__()
        self._load = None

    def name(self):
        return "convert_vtpk"

    def displayName(self):
        return "Esri vector tile package → MBTiles"

    def shortHelpString(self):
        return ("Converts a .vtpk (Web Mercator) to MBTiles 1.3 without re-encoding the vector tiles; the Esri style "
                "is saved next to it as <name>.style.json. Packages in other coordinate systems cannot go into "
                "MBTiles by its specification — QGIS opens such a .vtpk directly.")

    def createInstance(self):
        return ConvertVectorTilePackage()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(self.INPUT, "Vector tile package (.vtpk)",
                                                     fileFilter="Esri vector tile packages (*.vtpk)"))
        self.addParameter(QgsProcessingParameterFileDestination(self.OUTPUT, "MBTiles", "MBTiles (*.mbtiles)"))
        self.addParameter(QgsProcessingParameterBoolean(self.LOAD, "Add the result to the map", True))
        self.addOutput(QgsProcessingOutputString(self.SUMMARY, "Summary"))

    def processAlgorithm(self, parameters, context, feedback):
        src = self.parameterAsFile(parameters, self.INPUT, context)
        dst = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        fb = _Feedback(feedback)
        try:
            r = etc.to_mbtiles(src, dst, progress=fb.progress, cancelled=fb.cancelled)
        except etc.Cancelled:
            raise QgsProcessingException("Cancelled — no partial file was left.")
        except etc.TileCacheError as exc:
            raise QgsProcessingException(str(exc))
        summary = f"{r['tiles']:,} vector tiles, XYZ levels {r['xyz_levels']}, layers: {', '.join(r['layers'])}"
        feedback.pushInfo(summary)
        self._load = dst if self.parameterAsBool(parameters, self.LOAD, context) else None
        return {self.OUTPUT: dst, self.SUMMARY: summary}

    def postProcessAlgorithm(self, context, feedback):
        if self._load:
            layer = QgsVectorTileLayer(f"type=mbtiles&url={self._load}", os.path.splitext(os.path.basename(self._load))[0])
            if layer.isValid():
                (context.project() or QgsProject.instance()).addMapLayer(layer)
        return {}


def gpkg_raster_layer(path: str) -> QgsRasterLayer:
    """Helper for tests and scripts: the first raster table of a GeoPackage as a layer."""
    con = sqlite3.connect(path)
    try:
        table = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type IN ('tiles', "
                            "'2d-gridded-coverage')").fetchone()[0]
    finally:
        con.close()
    return QgsRasterLayer(f"GPKG:{path}:{table}", table, "gdal")
