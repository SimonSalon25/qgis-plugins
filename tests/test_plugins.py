"""Both QGIS plugins inside a headless QGIS (run with the Python of QGIS, e.g. python3.12 on Debian/Ubuntu):

    python3 tests/prepare_fixtures.py /tmp/fx         # normal Python with lerc + pyproj
    QT_QPA_PLATFORM=offscreen python3.12 tests/test_plugins.py /tmp/fx
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX = Path(sys.argv[1])
sys.path.insert(0, str(ROOT))
sys.path.append("/usr/share/qgis/python/plugins")

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProject,  # noqa: E402
                       QgsRasterLayer, QgsVectorLayer)

QgsApplication.setPrefixPath("/usr", True)
app = QgsApplication([], False)
app.initQgis()
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
import processing  # noqa: E402

import esri_tilecache_qgis.provider as ep  # noqa: E402
import sjtsk_qgis.provider as sp  # noqa: E402

PROVIDERS = [ep.Provider(), sp.Provider()]          # keep references (Python owns the providers)
for _p in PROVIDERS:
    QgsApplication.processingRegistry().addProvider(_p)
EXP = json.loads((FX / "expected.json").read_text(encoding="utf-8"))
OUT = Path(tempfile.mkdtemp())
failed = []


def check(cond, msg):
    if not cond:
        failed.append(msg)
        print("FAIL", msg)


def test_tpk_v1():
    r = processing.run("esritilecache:convert_tile_cache", {"INPUT": str(FX / "v1.tpk"), "OUTPUT": str(OUT / "a.gpkg"),
                                                            "TABLE": "map", "OVERVIEWS": True, "LOAD": False})
    layer = QgsRasterLayer(f"GPKG:{OUT / 'a.gpkg'}:map", "map", "gdal")
    check(layer.isValid() and layer.crs().authid() == "EPSG:8353", f"V1 GeoPackage layer: {layer.crs().authid()}")
    ox, oy, r0, c0, res = EXP["origin"]
    ext = layer.extent()
    check(abs(ext.xMinimum() - (ox + c0 * 512 * res)) < 1e-3 and abs(ext.yMaximum() - (oy - r0 * 512 * res)) < 1e-3,
          f"extent {ext.toString()}")
    check("5 tiles copied" in r["SUMMARY"] and "overviews" in r["SUMMARY"], f"summary {r['SUMMARY']}")
    check(layer.dataProvider().hasPyramids() or "overviews [4]" in r["SUMMARY"], "overviews present")
    try:
        processing.run("esritilecache:convert_tile_cache", {"INPUT": str(FX / "v1.tpk"), "OUTPUT": str(OUT / "a.gpkg")})
        check(False, "existing output must be refused")
    except Exception as exc:  # QgsProcessingException
        check("already exists" in str(exc), f"refusal message: {exc}")
    print("OK  .tpk Compact V1 → GeoPackage in QGIS (CRS, extent, overviews, no overwrite)")


def test_gdal_overview_fallback():
    from esri_tilecache_qgis import algorithms as alg

    processing.run("esritilecache:convert_tile_cache", {"INPUT": str(FX / "v1.tpk"), "OUTPUT": str(OUT / "b.gpkg"),
                                                        "TABLE": "map", "OVERVIEWS": False, "LOAD": False})

    class FB:
        def setProgress(self, *_a): pass
        def setProgressText(self, *_a): pass
        def isCanceled(self): return False
        def pushInfo(self, t): self.info = t

    saved = sys.modules.get("PIL")
    sys.modules["PIL"] = None                      # simulate QGIS without Pillow
    try:
        text = alg.build_overviews(str(OUT / "b.gpkg"), "map", FB())
    finally:
        sys.modules["PIL"] = saved
    layer = QgsRasterLayer(f"GPKG:{OUT / 'b.gpkg'}:map", "map", "gdal")
    check(text.startswith("GDAL overviews") and layer.dataProvider().hasPyramids(), f"GDAL fallback: {text}")
    print("OK  overviews without Pillow (GDAL fallback)")


def test_lerc_and_vtpk():
    try:
        import lerc  # noqa: F401
        has_lerc = True
    except ImportError:
        has_lerc = False
    try:
        processing.run("esritilecache:convert_tile_cache", {"INPUT": str(FX / "dem.tpkx"), "OUTPUT": str(OUT / "d.gpkg"),
                                                            "LOAD": False})
        check(has_lerc, "LERC without the lerc package must fail with an explanation")
    except Exception as exc:
        check(not has_lerc and "lerc" in str(exc), f"LERC message: {exc}")
        check(not (OUT / "d.gpkg").exists(), "no partial GeoPackage after the LERC failure")
    r = processing.run("esritilecache:convert_vtpk", {"INPUT": str(FX / "v.vtpk"), "OUTPUT": str(OUT / "v.mbtiles"),
                                                       "LOAD": True})
    check("21 vector tiles" in r["SUMMARY"] and (OUT / "v.style.json").exists(), f"vtpk: {r['SUMMARY']}")
    from qgis.core import QgsVectorTileLayer
    vt = QgsVectorTileLayer(f"type=mbtiles&url={OUT / 'v.mbtiles'}", "v")
    check(vt.isValid() and vt.sourceMaxZoom() == 2, "QGIS opens the MBTiles as a vector tile layer")
    try:
        processing.run("esritilecache:convert_vtpk", {"INPUT": str(FX / "sjtsk.vtpk"), "OUTPUT": str(OUT / "j.mbtiles")})
        check(False, "a non-Web-Mercator .vtpk must be refused")
    except Exception as exc:
        check("Web Mercator" in str(exc), f"refusal: {exc}")
    print(f"OK  LERC {'converted' if has_lerc else 'refused with an explanation (no lerc in this QGIS)'}; "
          ".vtpk → MBTiles opens as a vector tile layer; S-JTSK .vtpk refused")


def _points_layer(crs, coords):
    layer = QgsVectorLayer(f"Point?crs={crs}&field=id:integer", "p", "memory")
    feats = []
    for i, (x, y) in enumerate(coords):
        f = QgsFeature(layer.fields())
        f.setAttributes([i])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    return layer


def test_sjtsk_transform():
    layer = _points_layer("EPSG:4326", EXP["points"])
    for target, key in ((0, "wgs84_to_5514"), (1, "wgs84_to_8353")):
        r = processing.run("sjtsk:transform_layer", {"INPUT": layer, "TARGET": target, "OUTPUT": "memory:"})
        out = r["OUTPUT"]
        got = [(f.geometry().asPoint().x(), f.geometry().asPoint().y()) for f in out.getFeatures()]
        d = max(abs(a - c) + abs(b - e) for (a, b), (c, e) in zip(got, EXP[key]))
        check(d < 1e-4, f"{key}: max difference {d} m")
        check(out.crs().authid() == ("EPSG:5514" if target == 0 else "EPSG:8353"), f"output CRS {out.crs().authid()}")
    r = processing.run("sjtsk:transform_layer", {"INPUT": layer, "TARGET": 0, "OUTPUT": "memory:"})
    check(r["OUTSIDE"] == 1, f"Prague counted outside the grid: {r['OUTSIDE']}")
    l5513 = _points_layer("EPSG:5513", [EXP["from_5513"]["input"]])
    r = processing.run("sjtsk:transform_layer", {"INPUT": l5513, "TARGET": 1, "OUTPUT": "memory:"})
    p = next(r["OUTPUT"].getFeatures()).geometry().asPoint()
    ex = EXP["from_5513"]["expected_8353"]
    check(abs(p.x() - ex[0]) < 1e-4 and abs(p.y() - ex[1]) < 1e-4, f"5513 → 8353: {p.x()}, {p.y()} vs {ex}")
    poly = QgsVectorLayer("Polygon?crs=EPSG:4258", "poly", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((19 48.7, 19.01 48.7, 19.01 48.71, 19 48.71, 19 48.7))"))
    poly.dataProvider().addFeatures([f])
    out = processing.run("sjtsk:transform_layer", {"INPUT": poly, "TARGET": 0, "OUTPUT": "memory:"})["OUTPUT"]
    g = next(out.getFeatures()).geometry()
    check(g.isGeosValid() and 7.0e5 < g.area() < 9.0e5, f"polygon area {g.area()} m²")
    utm = _points_layer("EPSG:32634", [(350000.0, 5400000.0)])
    try:
        processing.run("sjtsk:transform_layer", {"INPUT": utm, "TARGET": 0, "OUTPUT": "memory:"})
        check(False, "UTM input must be refused with an explanation")
    except Exception as exc:
        check("reproject" in str(exc), f"UTM message: {exc}")
    print("OK  S-JTSK plugin: WGS 84 → 5514 / 8353 = sjtsk (PROJ + grid) < 0.1 mm; outside counted; 5513; polygon; UTM refused")


def test_install_grid():
    r = processing.run("sjtsk:install_grid", {})
    proj = Path(QgsApplication.qgisSettingsDirPath()) / "proj" / "sk_gku_JTSK03_to_JTSK.tif"
    check(proj.exists() and proj.stat().st_size == 183132 and "Installed" in r["INFO"], f"install: {r['INFO']}")
    r = processing.run("sjtsk:install_grid", {})
    check("already installed" in r["INFO"], f"second install: {r['INFO']}")
    # 0.1.1: the official chain is the preferred operation (QGIS 3.44 otherwise used the Czech parameters, 3.6 m)
    from qgis.core import QgsCoordinateReferenceSystem as CRS, QgsCoordinateTransformContext
    default = QgsCoordinateTransformContext()
    default.readSettings()
    for ctx, kind in ((default, "default"), (QgsProject.instance().transformContext(), "project")):
        for src, dst in (("EPSG:4258", "EPSG:5514"), ("EPSG:4326", "EPSG:5514"), ("EPSG:8353", "EPSG:5514")):
            op = ctx.calculateCoordinateOperation(CRS(src), CRS(dst))
            check("sk_gku_JTSK03_to_JTSK" in op, f"{kind} preferred {src} → {dst}: {op[:120]}")
    check("preferred = official" in r["INFO"], f"log: {r['INFO']}")
    # the preferred chain gives the official result (point E01 of the example control points, sjtsk "ugkk")
    from qgis.core import QgsCoordinateTransform
    for src in ("EPSG:4258", "EPSG:4326"):
        t = QgsCoordinateTransform(CRS(src), CRS("EPSG:5514"), QgsProject.instance().transformContext())
        p = t.transform(QgsPointXY(17.12, 48.15))
        check(abs(p.x() + 572758.891) < 0.002 and abs(p.y() + 1280259.888) < 0.002,
              f"QGIS reprojection {src} → 5514 with the preferred chain: {p.x():.3f} {p.y():.3f}")
    print("OK  grid installed into the QGIS profile (once); official chain preferred for 4258/4326/8353 → 5514")


for t in (test_tpk_v1, test_gdal_overview_fallback, test_lerc_and_vtpk, test_sjtsk_transform, test_install_grid):
    t()
QgsProject.instance().clear()
shutil.rmtree(OUT, ignore_errors=True)
print("FAILED:", len(failed)) if failed else print("ALL PLUGIN TESTS PASSED")
os._exit(1 if failed else 0)
