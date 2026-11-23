"""Refresh the bundled libraries and build the plugin ZIPs for plugins.qgis.org.

    python tools/build.py <esri-tilecache repo> <sjtsk repo> <output folder>

The bundled code is copied unchanged from the library repositories (MIT); the plugins themselves are
GPL-2.0-or-later. The ZIPs contain no tests, no caches and no binaries (only the GKÚ grid as data, CC BY 4.0).
"""
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
etc_repo, sjtsk_repo, out = (Path(a) for a in sys.argv[1:4])

shutil.rmtree(ROOT / "esri_tilecache_qgis" / "esri_tilecache", ignore_errors=True)
shutil.copytree(etc_repo / "src" / "esri_tilecache", ROOT / "esri_tilecache_qgis" / "esri_tilecache",
                ignore=shutil.ignore_patterns("__pycache__"))
shutil.copy(etc_repo / "LICENSE", ROOT / "esri_tilecache_qgis" / "esri_tilecache" / "LICENSE")
p = ROOT / "sjtsk_qgis" / "sjtsk_portable"
shutil.copy(sjtsk_repo / "src" / "sjtsk" / "portable.py", p / "portable.py")
for f in ("sk_gku_JTSK03_to_JTSK.tif", "sk_gku_LICENSE.txt"):
    shutil.copy(sjtsk_repo / "src" / "sjtsk" / "data" / f, p / f)
shutil.copy(sjtsk_repo / "LICENSE", p / "LICENSE")

out.mkdir(parents=True, exist_ok=True)
for plugin in ("esri_tilecache_qgis", "sjtsk_qgis"):
    version = next(line.split("=", 1)[1].strip() for line in (ROOT / plugin / "metadata.txt").read_text(
        encoding="utf-8").splitlines() if line.startswith("version="))
    target = out / f"{plugin}-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((ROOT / plugin).rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                z.write(f, f.relative_to(ROOT).as_posix())
    size = target.stat().st_size
    assert size < 25e6, f"{target.name} is {size / 1e6:.1f} MB — the QGIS repository allows 25 MB"
    print(f"{target.name}: {size / 1e3:.0f} kB")
