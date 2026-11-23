"""Mutation check of the plugins (each deliberate bug must make tests/test_plugins.py fail).

    python3 tests/prepare_fixtures.py /tmp/fx && python3 tests/mutations.py /tmp/fx /usr/bin/python3.12
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX, PY = sys.argv[1], sys.argv[2]
MUTATIONS = [
    ("transform: x/y swapped", "sjtsk_qgis/algorithms.py",
     "portable.transform(g, src, dst, v.x(), v.y(), stats)", "portable.transform(g, src, dst, v.y(), v.x(), stats)"),
    ("5513 treated as 5514 without axis change", "sjtsk_qgis/algorithms.py",
     'SAME_DATUM = {"EPSG:5513": 5514, "EPSG:2065": 5514, "EPSG:8352": 8353}', 'SAME_DATUM = {}'),
    ("outside-grid count not reported", "sjtsk_qgis/algorithms.py",
     'outside = stats.get("outside_grid", 0)', "outside = 0"),
    ("0.1.1: grid installed but not set as preferred", "sjtsk_qgis/algorithms.py",
     "        lines += set_preferred_transformations(context, feedback)\n", "        lines += []\n"),
    ("0.1.1: WGS 84 without the ETRS89 chain", "sjtsk_qgis/algorithms.py",
     'if proj is None and src == "EPSG:4326":', 'if False:'),
    ("0.1.1: inverse of 8367 preferred over 8365", "sjtsk_qgis/algorithms.py",
     '"Inverse of S-JTSK [JTSK03] to" in o.name, len(o.name)', '"Inverse of S-JTSK [JTSK03] to" not in o.name, len(o.name)'),
    ("GDAL fallback without overviews", "esri_tilecache_qgis/algorithms.py",
     "        if factors:\n            ds.BuildOverviews(", "        if False:\n            ds.BuildOverviews("),
]
survived = 0
for name, file, old, new in MUTATIONS:
    d = Path(tempfile.mkdtemp()) / "k"
    shutil.copytree(ROOT, d, ignore=shutil.ignore_patterns("__pycache__"))
    p = d / file
    t = p.read_text(encoding="utf-8")
    assert t.count(old) == 1, (name, t.count(old))
    p.write_text(t.replace(old, new), encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=tempfile.mkdtemp())
    r = subprocess.run([PY, str(d / "tests" / "test_plugins.py"), FX], capture_output=True, text=True, env=env)
    caught = r.returncode != 0
    survived += not caught
    print(("CAUGHT   " if caught else "SURVIVED ") + name, flush=True)
    shutil.rmtree(d.parent, ignore_errors=True)
sys.exit(1 if survived else 0)
