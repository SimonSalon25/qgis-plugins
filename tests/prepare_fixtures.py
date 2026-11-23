"""Run with a normal Python (with lerc, pyproj): writes the inputs and expected values for test_plugins.py."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "esri-tilecache" / "tests"))
sys.path.insert(0, str(ROOT / "sjtsk" / "src"))
import synthetic as s  # noqa: E402
import sjtsk  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
tiles = s.tpk(out / "v1.tpk", False)
s.tpkx_lerc(out / "dem.tpkx")
s.vtpk(out / "v.vtpk")
s.vtpk(out / "sjtsk.vtpk", epsg=8353)
pts = [(17.1077, 48.1486), (19.1462, 48.7363), (21.2611, 48.7164), (14.42, 50.09)]      # last: Prague (outside)
exp = {"wgs84_to_5514": [sjtsk.Converter(sjtsk.WGS84, sjtsk.JTSK)(lo, la) for lo, la in pts],
       "wgs84_to_8353": [sjtsk.converter(sjtsk.WGS84, sjtsk.JTSK03)(lo, la) for lo, la in pts],
       "points": pts,
       "tiles": {f"{r},{c}": None for (r, c) in tiles}, "origin": [s.OX, s.OY, s.R0, s.C0, s.RES]}
e, n = exp["wgs84_to_5514"][1]
exp["from_5513"] = {"input": [-n, -e], "expected_8353": list(sjtsk.converter("EPSG:5513", sjtsk.JTSK03)(-n, -e))}
(out / "expected.json").write_text(json.dumps(exp), encoding="utf-8")
print("fixtures in", out)
