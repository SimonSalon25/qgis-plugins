# QGIS plugins: Esri Tile Cache Converter, S-JTSK Official Transformation

[![DOI](https://zenodo.org/badge/1468812803.svg)](https://doi.org/10.5281/zenodo.24477771)

Two QGIS plugins (Processing providers) built on the open libraries
[esri-tilecache](https://github.com/SimonSalon25/esri-tilecache) and [sjtsk](https://github.com/SimonSalon25/sjtsk).

## Problem, state of the art and solution

**Problem.** Two gaps in QGIS for Slovak spatial data: (1) national maps distributed as Esri Compact V1 `.tpk`
(e.g. national forest maps) cannot be opened; (2) the official ÚGKK transformation between S-JTSK
[JTSK], S-JTSK [JTSK03] and ETRS89 / WGS 84 is not applied reliably.

**State as of 23 November 2026** (QGIS 3.44.12, PROJ 9.8.1, GDAL 3.13.1, Windows): GDAL does not read Compact V1;
QGIS transforms ETRS89 → EPSG:5514 with the Czech parameters "S-JTSK to ETRS89 (1)", 0.5–3.6 m off the official
result across Slovakia, and installing the official GKÚ grid alone does not change that.

**Solution.** *Esri Tile Cache Converter* converts Esri tile caches (incl. Compact V1) to GeoPackage / MBTiles
without re-encoding. *S-JTSK Official Transformation* transforms vector layers by the official ÚGKK chain
(independently of the PROJ installation; 0.6 mm on 12 control points) and makes it QGIS's preferred transformation,
so QGIS's own reprojection gives the official result too.

**Aim.** Correct, reproducible work with Slovak national data in open software — for forestry, cadastre and field
surveys — with an open methodology.

| plugin | algorithms | for whom |
|---|---|---|
| **Esri Tile Cache Converter** (`esri_tilecache_qgis`) | *Esri tile cache → GeoPackage* (.tpk incl. Compact V1, .tpkx, cache folders, LERC elevation); *Esri vector tile package → MBTiles* | anyone who receives Esri tile packages — GDAL cannot read Compact V1 .tpk |
| **S-JTSK Official Transformation** (`sjtsk_qgis`) | *Transform layer (official ÚGKK S-JTSK ↔ ETRS89)*; *Install the GKÚ grid into QGIS* | users of Slovak data (cadastre in JTSK, ZBGIS / DMR5 in JTSK03, GNSS in ETRS89 / WGS 84) |

- No binaries; the libraries are bundled as plain Python (MIT), the GKÚ grid as data (© GKÚ Bratislava, CC BY 4.0).
- Requirements: QGIS 3.34 or newer. The tile converter uses numpy and Pillow shipped with QGIS for fast overviews
  (without Pillow it falls back to GDAL); LERC elevation caches need the optional Python package `lerc`.
- Open methodology: METHODOLOGY.md in each library repository; how AI was used: AI_DISCLOSURE.md there.

## Develop and test

```bash
python tools/build.py ../esri-tilecache ../sjtsk dist        # refresh bundled libraries, build the ZIPs
python tests/prepare_fixtures.py /tmp/fx                     # inputs and expected values (needs lerc, pyproj)
QT_QPA_PLATFORM=offscreen python3 tests/test_plugins.py /tmp/fx   # with the Python of QGIS
python tests/mutations.py /tmp/fx <python of QGIS>           # each deliberate bug must fail the tests
```

Tested headless with QGIS 3.34.4 (Ubuntu 24.04): conversion of a Compact V1 .tpk, overviews with and without
Pillow, LERC refusal without `lerc`, .vtpk → MBTiles opened as a vector tile layer, S-JTSK transformations equal to
PROJ with the official grid (< 0.1 mm), 5513 input, outside-grid counting, grid installation.

## Funding

Developed within the project **APVV-23-0289** *Increasing the operational reliability of linear power structures by using data from remote sensing*, supported by the Slovak Research and Development Agency (APVV).

## Licence

GPL-2.0-or-later (as required by the QGIS plugin repository). Bundled libraries: MIT. GKÚ grid: CC BY 4.0.
