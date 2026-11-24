# SPDX-License-Identifier: GPL-2.0-or-later
"""Processing algorithms: Slovak S-JTSK (JTSK, JTSK03) ↔ ETRS89 the official ÚGKK way.

Uses the pure-Python reference implementation `sjtsk.portable` (bundled, MIT) with the GKÚ grid (bundled,
CC BY 4.0), so the result does not depend on PROJ grids installed in QGIS and is the same for ETRS89 and WGS 84.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsCoordinateTransformContext, QgsDatumTransform, QgsFeatureSink, QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputNumber,
                       QgsProcessingOutputString, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProject)

from .sjtsk_portable import portable

try:                                    # QGIS ≥ 3.36 (and QGIS 4, where the old enum names are gone)
    from qgis.core import Qgis
    VECTOR_ANY = Qgis.ProcessingSourceType.VectorAnyGeometry
except (ImportError, AttributeError):    # QGIS 3.34
    VECTOR_ANY = QgsProcessing.TypeVectorAnyGeometry
try:
    FAST_INSERT = QgsFeatureSink.Flag.FastInsert
except AttributeError:
    FAST_INSERT = QgsFeatureSink.FastInsert

GRID_TIF = Path(__file__).resolve().parent / "sjtsk_portable" / "sk_gku_JTSK03_to_JTSK.tif"
TARGETS = [(5514, "S-JTSK [JTSK] / Krovak East North — EPSG:5514 (cadastre)"),
           (8353, "S-JTSK [JTSK03] / Krovak East North — EPSG:8353 (ZBGIS, DMR5)"),
           (4258, "ETRS89 — EPSG:4258")]
#: other notations of the same S-JTSK realization → node plane (axis / prime meridian change only, no datum shift)
SAME_DATUM = {"EPSG:5513": 5514, "EPSG:2065": 5514, "EPSG:8352": 8353}

_GRID = None


def grid() -> portable.Grid:
    global _GRID
    if _GRID is None:
        _GRID = portable.Grid.from_geotiff(GRID_TIF)
    return _GRID


class TransformLayer(QgsProcessingAlgorithm):
    INPUT, TARGET, OUTPUT, OUTSIDE, INFO = "INPUT", "TARGET", "OUTPUT", "OUTSIDE", "INFO"

    def name(self):
        return "transform_layer"

    def displayName(self):
        return "Transform layer (official ÚGKK S-JTSK ↔ ETRS89)"

    def shortHelpString(self):
        return ("Transforms a vector layer between S-JTSK [JTSK] (EPSG:5514, also 5513, 2065, ESRI:102067), "
                "S-JTSK [JTSK03] (EPSG:8353, also 8352) and ETRS89 / WGS 84 (EPSG:4258 / 4326) with the official "
                "transformation of ÚGKK: Helmert ETRS89 ↔ JTSK03 (EPSG 8365 / 8367) and the GKÚ grid JTSK03 ↔ JTSK "
                "(EPSG 8364, included). WGS 84 is taken as ETRS89. The result equals PROJ with the official grid to "
                "below 0.01 mm and does not depend on the grids installed in QGIS.\n\n"
                "Why: depending on the installation, QGIS/PROJ may use different transformations for ETRS89 and "
                "WGS 84 (up to 1.36 m apart in Slovakia) and may treat JTSK03 as JTSK (up to 1.64 m). "
                "Points outside Slovakia (outside the grid) get the Helmert only and are counted. "
                "Z values are kept; heights are ignored (effect below 12 mm in Slovakia).\n\n"
                "Method and measurements: https://github.com/SimonSalon25/sjtsk")

    def createInstance(self):
        return TransformLayer()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, "Input layer",
                                                              [VECTOR_ANY]))
        self.addParameter(QgsProcessingParameterEnum(self.TARGET, "Target system", [t[1] for t in TARGETS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, "Transformed"))
        self.addOutput(QgsProcessingOutputNumber(self.OUTSIDE, "Vertices outside the grid"))
        self.addOutput(QgsProcessingOutputString(self.INFO, "Transformation"))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException("invalid input layer")
        dst = TARGETS[self.parameterAsEnum(parameters, self.TARGET, context)][0]
        crs = source.sourceCrs()
        authid = crs.authid().upper()
        pre = None
        if authid in SAME_DATUM:                     # pure change of axes / prime meridian (same datum)
            node = SAME_DATUM[authid]
            pre = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(f"EPSG:{node}"), context.transformContext())
            src = node
        else:
            try:
                src = int(authid.split(":")[1])
            except (IndexError, ValueError):
                src = None
            if src not in portable.SUPPORTED:
                raise QgsProcessingException(
                    f"The layer is in {authid or 'an unknown system'}. Supported: EPSG:5514, 5513, 2065, ESRI:102067, "
                    "8353, 8352, 4258, 4326 — reproject other systems to ETRS89 (EPSG:4258) first.")
        target_crs = QgsCoordinateReferenceSystem(f"EPSG:{dst}")
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, source.fields(), source.wkbType(),
                                             target_crs)
        if sink is None:
            raise QgsProcessingException("cannot create the output layer")
        g = grid()
        stats: dict = {}
        total = source.featureCount() or 1
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            geom = f.geometry()
            if not geom.isNull():
                if pre is not None:
                    geom.transform(pre)
                for k, v in enumerate(list(geom.vertices())):
                    x, y = portable.transform(g, src, dst, v.x(), v.y(), stats)
                    v.setX(x)
                    v.setY(y)
                    geom.moveVertex(v, k)
                f.setGeometry(geom)
            sink.addFeature(f, FAST_INSERT)
            feedback.setProgress(100.0 * (i + 1) / total)
        outside = stats.get("outside_grid", 0)
        if outside:
            feedback.reportError(f"{outside} vertices lie outside the GKÚ grid (outside Slovakia): Helmert only, "
                                 "without the JTSK03↔JTSK correction (error up to ~1.6 m).", fatalError=False)
        info = f"EPSG:{src} → EPSG:{dst}, official ÚGKK chain (Helmert EPSG 8365/8367 + GKÚ grid EPSG 8364)"
        feedback.pushInfo(info)
        return {self.OUTPUT: dest_id, self.OUTSIDE: outside, self.INFO: info}


class InstallGrid(QgsProcessingAlgorithm):
    INFO = "INFO"

    def name(self):
        return "install_grid"

    def displayName(self):
        return "Install the GKÚ grid into QGIS (for on-the-fly reprojection)"

    def shortHelpString(self):
        return ("Copies the official GKÚ grid sk_gku_JTSK03_to_JTSK.tif (© 2013 GKÚ Bratislava, CC BY 4.0) into the "
                "PROJ folder of your QGIS profile and sets the official transformation as the preferred one for "
                "ETRS89 / WGS 84 → S-JTSK [JTSK] and S-JTSK [JTSK03] → [JTSK] (Settings → Options → Transformations, "
                "for new projects, and in the current project). After restarting QGIS, its own reprojection uses "
                "the official ÚGKK transformation. To undo, remove the three entries in Settings → Options → "
                "Transformations.\n\n"
                "Why: without a preference QGIS picks by itself. Tested in QGIS 3.44 (PROJ 9.8): for ETRS89 → "
                "EPSG:5514 it ranks the Czech ČÚZK grid first and, without it, falls back to 'S-JTSK to ETRS89 (1)' "
                "(Czech parameters) — up to 4.2 m off in Slovakia, most in the east. Installing the grid alone does "
                "not change that choice.")

    def createInstance(self):
        return InstallGrid()

    def initAlgorithm(self, config=None):
        self.addOutput(QgsProcessingOutputString(self.INFO, "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        target = Path(QgsApplication.qgisSettingsDirPath()) / "proj"
        target.mkdir(parents=True, exist_ok=True)
        dst = target / GRID_TIF.name
        if dst.exists() and dst.read_bytes() == GRID_TIF.read_bytes():
            lines = [f"The grid is already installed: {dst}"]
        else:
            shutil.copy2(GRID_TIF, dst)
            shutil.copy2(GRID_TIF.with_name("sk_gku_LICENSE.txt"), target / "sk_gku_LICENSE.txt")
            lines = [f"Installed {dst}"]
        lines += set_preferred_transformations(context, feedback)
        lines.append("Restart QGIS to use the grid.")
        info = "\n".join(lines)
        feedback.pushInfo(info)
        return {self.INFO: info}


#: pairs (source, destination) whose preferred operation goes through the GKÚ grid; QGIS uses each entry for the
#: reverse direction too
PREFERRED_PAIRS = (("EPSG:4258", "EPSG:5514"), ("EPSG:4326", "EPSG:5514"), ("EPSG:8353", "EPSG:5514"))


def official_operation(src: str, dst: str) -> str | None:
    """PROJ string of the official ÚGKK chain for the pair: an operation that uses the GKÚ grid, preferring the
    ÚGKK Helmert ETRS89 → JTSK03 (EPSG 8365) over the inverse of 8367. `None` if PROJ offers none."""
    ops = [o for o in QgsDatumTransform.operations(QgsCoordinateReferenceSystem(src), QgsCoordinateReferenceSystem(dst))
           if any(g.shortName == GRID_TIF.name for g in o.grids)]
    ops.sort(key=lambda o: ("Inverse of S-JTSK [JTSK03] to" in o.name, len(o.name)))
    return ops[0].proj if ops else None


def set_preferred_transformations(context=None, feedback=None) -> list[str]:
    """Writes the official operations into the default transform context (Settings → Options → Transformations;
    used by new projects) and into the current project. Returns the lines for the log."""
    lines = []
    default = QgsCoordinateTransformContext()
    default.readSettings()
    project = QgsProject.instance().transformContext()
    for src, dst in PREFERRED_PAIRS:
        proj = official_operation(src, dst)
        note = ""
        if proj is None and src == "EPSG:4326":
            # PROJ has no WGS 84 → S-JTSK path through the grid; as in the library (and the ÚGKK practice for GNSS),
            # WGS 84 is taken as ETRS89 — the ETRS89 pipeline starts from the same geographic degrees
            proj = official_operation("EPSG:4258", dst)
            note = " (WGS 84 taken as ETRS89)"
        if proj is None:
            lines.append(f"{src} → {dst}: no operation with the GKÚ grid offered by PROJ — not set")
            continue
        for ctx in (default, project):
            ctx.removeCoordinateOperation(QgsCoordinateReferenceSystem(src), QgsCoordinateReferenceSystem(dst))
            ctx.addCoordinateOperation(QgsCoordinateReferenceSystem(src), QgsCoordinateReferenceSystem(dst), proj, True)
        lines.append(f"{src} → {dst}: preferred = official ÚGKK chain (GKÚ grid){note}")
    default.writeSettings()
    QgsProject.instance().setTransformContext(project)
    if context is not None:
        context.setTransformContext(project)
    return lines


def project_crs_note() -> str:
    """Short hint for the plugin's about box: the current project CRS and whether it is an S-JTSK realization."""
    crs = QgsProject.instance().crs().authid()
    return {"EPSG:5514": "JTSK", "EPSG:8353": "JTSK03"}.get(crs, crs)
