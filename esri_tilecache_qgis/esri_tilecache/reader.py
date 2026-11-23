"""Reading Esri tile caches — packages and folders — without decoding the tiles.

| source | content | storage | metadata |
|---|---|---|---|
| `.tpk` (ZIP) | image | Compact V1 (`.bundle` + `.bundlx`) or V2 | `conf.xml`, `conf.cdi`, `iteminfo.xml` |
| `.tpkx` (ZIP) | image or elevation (LERC) | Compact V2 | `root.json`, `iteminfo.json` |
| `.vtpk` (ZIP) | Mapbox vector tiles (gzip) | Compact V2 in `p12/tile` | `p12/root.json` |
| ArcGIS Server cache folder | image | Compact V1, V2 or Exploded | `conf.xml`, `conf.cdi` |
| unpacked `.tpkx` / `.vtpk` | as the package | Compact V2 | `root.json` / `p12/root.json` |

Storage formats (public Esri specifications, Apache 2.0: github.com/Esri/raster-tiles-compactcache,
github.com/Esri/tile-package-spec). Compact V1 is not described there; its layout below was verified on a
national cache of several GB (see METHODOLOGY.md):

* **V1** — `.bundlx`: 16-byte header + 128 × 128 records of 5 bytes (offset into `.bundle`), **column-major**;
  at the offset in `.bundle` a 4-byte length followed by the image; offset < 60 = empty tile.
* **V2** — 64-byte header, then 128 × 128 records of 8 bytes (low 40 bits offset, high 24 bits length),
  **row-major**; length 0 = no tile.
* **Exploded** — `_alllayers/Lxx/R<8 hex>/C<8 hex>.<png|jpg>`.

A bundle name `R<row hex>C<column hex>` is the top-left tile of the bundle; level folders are `L<two digits>`.
"""
from __future__ import annotations

import html
import json
import logging
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

BUNDLE_SIZE = 128
IMAGE_FORMATS = ("PNG", "PNG8", "PNG24", "PNG32", "JPEG", "JPG", "MIXED")


class TileCacheError(Exception):
    """The cache cannot be read or converted; the message says why."""


class Cancelled(Exception):
    """The caller's `cancelled()` callback returned True."""


# ------------------------------------------------------------------ file access (ZIP or folder)

class _StoredMember:
    """An uncompressed ZIP member read directly from disk.

    `ZipFile.open().seek()` after reading another member returned data from the wrong position on
    Python 3.12 (observed on a .tpk of several GB); direct reads avoid that and are faster.
    """

    def __init__(self, z: zipfile.ZipFile, name: str) -> None:
        zi = z.getinfo(name)
        self._f = open(z.filename, "rb")  # noqa: SIM115 — closed in __exit__
        self._f.seek(zi.header_offset)
        header = self._f.read(30)
        if header[:4] != b"PK\x03\x04":
            self._f.close()
            raise TileCacheError(f"{name}: damaged local ZIP header")
        n, e = struct.unpack("<HH", header[26:30])
        self._start = zi.header_offset + 30 + n + e
        self._size = zi.file_size
        self._pos = 0

    def seek(self, offset: int) -> None:
        self._pos = offset
        self._f.seek(self._start + offset)

    def read(self, n: int = -1) -> bytes:
        left = max(0, self._size - self._pos)
        n = left if n < 0 else min(n, left)
        data = self._f.read(n)
        self._pos += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> None:
        self._f.close()


class _Source:
    """Cache files inside a ZIP (package) or on disk (folder). Names always use `/`, relative to the root."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if path.is_file() else None
        if self.zip is not None:
            self.names = [m for m in self.zip.namelist() if not m.endswith("/")]
        else:
            self.names = [p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()]
        self._lower = {m.lower(): m for m in self.names}

    def find(self, suffix: str) -> str | None:
        suffix = suffix.lower()
        hits = sorted((m for m in self.names if m.lower() == suffix or m.lower().endswith("/" + suffix)), key=len)
        return hits[0] if hits else None

    def has(self, name: str) -> bool:
        return name.lower() in self._lower

    def actual(self, name: str) -> str:
        return self._lower.get(name.lower(), name)

    def read(self, name: str) -> bytes:
        if self.zip is not None:
            return self.zip.read(name)
        return (self.path / name).read_bytes()

    def size(self, name: str) -> int:
        if self.zip is not None:
            return self.zip.getinfo(name).file_size
        return (self.path / name).stat().st_size

    def open(self, name: str):
        if self.zip is not None:
            if self.zip.getinfo(name).compress_type == zipfile.ZIP_STORED:
                return _StoredMember(self.zip, name)
            return self.zip.open(name)
        return open(self.path / name, "rb")  # noqa: SIM115

    def close(self) -> None:
        if self.zip is not None:
            self.zip.close()


# ------------------------------------------------------------------ cache

@dataclass
class Cache:
    """An opened Esri tile cache. Use as a context manager or call `close()`."""

    path: Path
    kind: str                   # "tpk", "tpkx", "vtpk", "folder"
    storage: str                # "V1", "V2", "exploded"
    content: str                # "image", "lerc", "mvt"
    epsg: int | None
    wkt: str | None
    origin_x: float
    origin_y: float
    tile_size: int
    format: str
    levels: dict                # LevelID → resolution (CRS units per pixel)
    extent: tuple | None        # (xmin, ymin, xmax, ymax) of the data
    title: str = ""
    description: str = ""
    rights: str = ""
    bundles: list = field(default_factory=list)   # [(level, row0, col0, bundle_name, bundlx_name | None)]
    exploded: dict = field(default_factory=dict)  # level → [(row, col, name)]
    style: bytes | None = None  # .vtpk: p12/resources/styles/root.json
    _source: _Source | None = field(default=None, repr=False)

    @property
    def finest_level(self) -> int:
        """The finest level that has tiles (without tiles: the finest level of the tiling scheme)."""
        have = self.levels_with_tiles() or list(self.levels)
        return min(have, key=self.levels.get)

    def levels_with_tiles(self) -> list[int]:
        if self.storage == "exploded":
            have = {lv for lv, t in self.exploded.items() if t}
        else:
            have = {b[0] for b in self.bundles}
        return sorted(lv for lv in self.levels if lv in have)

    def summary(self) -> str:
        """Human-readable description (title, CRS, content, levels, extent, rights)."""
        k = self.finest_level
        res = self.levels[k]
        content = {"mvt": "vector tiles (MVT)", "lerc": "elevation (LERC)", "image": f"image {self.format}"}[self.content]
        lines = [f"{self.title} — {self.description}".strip(" —"),
                 f"coordinate reference system: EPSG:{self.epsg}" if self.epsg else "coordinate reference system: no EPSG code",
                 f"{content}, tiles {self.tile_size} px, storage {self.storage}, source {self.kind}",
                 f"levels with tiles: {len(self.levels_with_tiles())} of {len(self.levels)}, finest "
                 f"{res:.4f} units/px (1 : {res * 96 / 0.0254:,.0f})"]
        if self.storage != "exploded":
            lines.append(f"bundles of the finest level: {sum(1 for b in self.bundles if b[0] == k)}")
        if self.extent:
            x0, y0, x1, y1 = self.extent
            lines.append(f"extent {(x1 - x0) / 1000:.0f} × {(y1 - y0) / 1000:.0f} km")
        if self.rights:
            lines.append(f"rights: {self.rights}")
        return "\n".join(lines)

    def close(self) -> None:
        if self._source is not None:
            self._source.close()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()

    def tiles(self, level: int, stats: dict | None = None):
        """Iterate `(row, column, bytes)` of the non-empty tiles of a level (absolute row and column)."""
        if self.storage == "exploded":
            for r, c, name in self.exploded.get(level, []):
                yield r, c, self._source.read(name)
            return
        for b in self.bundles_of_level(level):
            yield from bundle_tiles(self._source, b, stats)

    def bundles_of_level(self, level: int) -> list:
        return sorted((b for b in self.bundles if b[0] == level), key=lambda v: (v[1], v[2]))


def _tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else None


def _extent_from_json(o) -> tuple | None:
    if not isinstance(o, dict):
        return None
    try:
        return (float(o["xmin"]), float(o["ymin"]), float(o["xmax"]), float(o["ymax"]))
    except (KeyError, TypeError, ValueError):
        return None


WEB_MERCATOR_ALIASES = (102100, 102113, 900913)


def _epsg_from_sr(sr) -> int | None:
    if not isinstance(sr, dict):
        return None
    code = sr.get("latestWkid") or sr.get("wkid")
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    return 3857 if code in WEB_MERCATOR_ALIASES else code


def _bundles(src: _Source, root: str) -> list:
    """Compact bundles below `root` (…/L00/R0000C0000.bundle)."""
    out = []
    for m in src.names:
        if not m.lower().endswith(".bundle") or not m.lower().startswith(root.lower()):
            continue
        u = re.search(r"/L(\d+)/R([0-9a-fA-F]+)C([0-9a-fA-F]+)\.bundle$", "/" + m)
        if not u:
            continue
        bx = m[:-7] + ".bundlx"
        out.append((int(u.group(1)), int(u.group(2), 16), int(u.group(3), 16), m,
                    src.actual(bx) if src.has(bx) else None))
    return out


def _exploded(src: _Source, root: str) -> dict:
    out: dict = {}
    for m in src.names:
        if not m.lower().startswith(root.lower()):
            continue
        u = re.search(r"/L(\d+)/R([0-9a-fA-F]{8})/C([0-9a-fA-F]{8})\.(png|jpg|jpeg|pbf|lerc)$", "/" + m, re.I)
        if u:
            out.setdefault(int(u.group(1)), []).append((int(u.group(2), 16), int(u.group(3), 16), m))
    for v in out.values():
        v.sort()
    return out


def open_cache(path) -> Cache:
    """Detect and open an Esri cache: a package, a folder, or a path to `conf.xml` / `root.json`."""
    path = Path(path)
    if path.is_file() and path.name.lower() in ("conf.xml", "root.json"):
        path = path.parent
        if path.name.lower() in ("layers", "p12"):
            path = path.parent
    if not path.exists():
        raise TileCacheError(f"{path} does not exist")
    if path.is_file() and not zipfile.is_zipfile(path):
        raise TileCacheError(f"{path.name} is not a ZIP file — not an Esri tile package")
    src = _Source(path)
    try:
        if src.has("p12/root.json"):
            cache = _open_json(src, path, "p12/", vtpk=True)
        elif src.find("conf.xml"):
            cache = _open_conf(src, path)
        elif src.has("root.json"):
            cache = _open_json(src, path, "", vtpk=False)
        else:
            raise TileCacheError(f"{path.name}: neither conf.xml nor root.json found — not an Esri cache")
    except BaseException:
        src.close()
        raise
    cache._source = src
    if not cache.levels:
        cache.close()
        raise TileCacheError(f"{path.name}: the cache has no levels (LODs)")
    logger.info("esri_tilecache: %s — %s, %s, %s, EPSG:%s, %d levels", path.name, cache.kind, cache.storage,
                cache.content, cache.epsg, len(cache.levels))
    return cache


def _open_conf(src: _Source, path: Path) -> Cache:
    conf = src.find("conf.xml")
    text = src.read(conf).decode("utf-8", "replace")
    root = conf[: -len("conf.xml")]
    extent = None
    cdi = src.find("conf.cdi")
    if cdi:
        t2 = src.read(cdi).decode("utf-8", "replace")
        values = [_tag(t2, k) for k in ("XMin", "YMin", "XMax", "YMax")]
        if all(values):
            extent = tuple(float(v) for v in values)
    title = description = rights = ""
    ii = src.find("iteminfo.xml")
    if ii:
        t3 = src.read(ii).decode("utf-8", "replace")
        title = _tag(t3, "title") or ""
        description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(html.unescape(
            _tag(t3, "description") or "")))).strip()
        rights = _tag(t3, "accessinformation") or ""
    wkid = _tag(text, "LatestWKID") or _tag(text, "WKID")
    levels = {int(lid): float(res) for lid, res in re.findall(
        r"<LODInfo.*?<LevelID>(.*?)</LevelID>.*?<Resolution>(.*?)</Resolution>", text, re.S)}
    m = re.search(r"<TileOrigin.*?<X>(.*?)</X>.*?<Y>(.*?)</Y>", text, re.S)
    if not (m and levels):
        raise TileCacheError("conf.xml has no tile origin or no levels")
    cols, rows = int(_tag(text, "TileCols") or 256), int(_tag(text, "TileRows") or 256)
    if cols != rows:
        raise TileCacheError(f"non-square tiles {cols} × {rows} are not supported")
    storage_text = (_tag(text, "StorageFormat") or "").lower()
    bundles = _bundles(src, root)
    if "exploded" in storage_text or (not bundles and _exploded(src, root)):
        storage, exp = "exploded", _exploded(src, root)
    else:
        exp = {}
        storage = "V1" if any(b[4] for b in bundles) else "V2"
    epsg = int(wkid) if wkid and wkid.isdigit() else None
    epsg = 3857 if epsg in WEB_MERCATOR_ALIASES else epsg
    fmt = (_tag(text, "CacheTileFormat") or "?").upper()
    return Cache(path=path, kind="tpk" if src.zip is not None else "folder", storage=storage,
                 content="lerc" if fmt.startswith("LERC") else "image", epsg=epsg, wkt=_tag(text, "WKT"),
                 origin_x=float(m.group(1)), origin_y=float(m.group(2)), tile_size=cols, format=fmt,
                 levels=levels, extent=extent, title=title, description=description, rights=rights,
                 bundles=bundles, exploded=exp)


def _open_json(src: _Source, path: Path, root: str, *, vtpk: bool) -> Cache:
    rj = json.loads(src.read(src.actual(root + "root.json")).decode("utf-8-sig"))
    ti = rj.get("tileInfo") or {}
    lods = ti.get("lods") or []
    levels = {int(lod["level"]): float(lod["resolution"]) for lod in lods if "level" in lod and "resolution" in lod}
    origin = ti.get("origin") or {}
    if "x" not in origin or "y" not in origin:
        raise TileCacheError("root.json has no tile origin (tileInfo.origin)")
    px = int(ti.get("cols") or ti.get("rows") or (512 if vtpk else 256))
    if vtpk:
        fmt, content = "PBF", "mvt"
        tile_root = root + "tile/"
    else:
        tii = rj.get("tileImageInfo") or {}
        fmt = str(tii.get("format") or "?").upper()
        content = "lerc" if fmt.startswith("LERC") else "image"
        tile_root = root + str(rj.get("tileBundlesPath") or "./tile").lstrip("./").rstrip("/") + "/"
    title = str(rj.get("name") or "")
    description = str(rj.get("serviceDescription") or rj.get("description") or "")
    rights = ""
    ii = src.find("iteminfo.json")
    if ii:
        try:
            info = json.loads(src.read(ii).decode("utf-8-sig"))
            title = info.get("title") or title
            description = re.sub(r"<[^>]+>", "", html.unescape(info.get("description") or description)).strip()
            rights = info.get("accessInformation") or ""
        except ValueError:
            pass
    sr = ti.get("spatialReference") or rj.get("spatialReference") or {}
    style = None
    if vtpk and src.has(root + "resources/styles/root.json"):
        style = src.read(src.actual(root + "resources/styles/root.json"))
    bundles = _bundles(src, tile_root)
    exp = {} if bundles else _exploded(src, tile_root)
    return Cache(path=path, kind=("vtpk" if vtpk else "tpkx") if src.zip is not None else "folder",
                 storage="exploded" if (exp and not bundles) else "V2", content=content, epsg=_epsg_from_sr(sr),
                 wkt=sr.get("wkt") if isinstance(sr, dict) else None, origin_x=float(origin["x"]),
                 origin_y=float(origin["y"]), tile_size=px, format=fmt, levels=levels,
                 extent=_extent_from_json(rj.get("fullExtent")), title=title, description=description,
                 rights=rights, bundles=bundles, exploded=exp, style=style)


# ------------------------------------------------------------------ bundles

def bundle_occupancy(src: _Source, bundle) -> tuple[int, int, int, int] | None:
    """(row_min, col_min, row_max, col_max) of the non-empty tiles of a bundle (max exclusive, absolute).

    Uses only the index (and, for V1, the 4-byte length prefixes), never the tile data.
    """
    _lv, r0, c0, name, name_x = bundle
    if name_x is not None:                                             # V1, column-major
        index = src.read(name_x)
        filled = [i for i in range(BUNDLE_SIZE * BUNDLE_SIZE)
                  if int.from_bytes(index[16 + 5 * i:21 + 5 * i], "little") >= 60]
        cells = [(i % BUNDLE_SIZE, i // BUNDLE_SIZE) for i in filled]
        if cells:                                                      # an empty tile may point to length 0
            with src.open(name) as f:
                size = src.size(name)
                keep = []
                for i, rc in zip(filled, cells):
                    off = int.from_bytes(index[16 + 5 * i:21 + 5 * i], "little")
                    if off + 4 > size:
                        keep.append(rc)                                # damaged — still inside the extent
                        continue
                    f.seek(off)
                    if struct.unpack("<I", f.read(4))[0]:
                        keep.append(rc)
                cells = keep
    else:                                                              # V2, row-major
        with src.open(name) as f:
            f.seek(64)
            index = f.read(8 * BUNDLE_SIZE * BUNDLE_SIZE)
        cells = [(i // BUNDLE_SIZE, i % BUNDLE_SIZE) for i in range(len(index) // 8)
                 if struct.unpack_from("<Q", index, 8 * i)[0] >> 40]
    if not cells:
        return None
    return (r0 + min(c[0] for c in cells), c0 + min(c[1] for c in cells),
            r0 + max(c[0] for c in cells) + 1, c0 + max(c[1] for c in cells) + 1)


def bundle_tiles(src: _Source, bundle, stats: dict | None = None) -> list[tuple[int, int, bytes]]:
    """`[(row, column, bytes)]` of the non-empty tiles of a bundle, in file order.

    A damaged tile (length beyond the end of the bundle, short read) is skipped and counted in
    `stats["damaged"]`.
    """
    _lv, r0, c0, name, name_x = bundle
    size = src.size(name)
    out = []
    damaged = 0

    def _read(f, offset, length):
        if offset + length > size:
            return None
        try:
            f.seek(offset)
            data = f.read(length)
        except (EOFError, OSError, zipfile.BadZipFile):
            return None
        return data if len(data) == length else None

    index_v1 = src.read(name_x) if name_x is not None else None        # before opening the bundle
    with src.open(name) as f:
        if index_v1 is not None:                                       # V1, column-major
            records = []
            for i in range(BUNDLE_SIZE * BUNDLE_SIZE):
                offset = int.from_bytes(index_v1[16 + 5 * i:21 + 5 * i], "little")
                records.append((offset, i % BUNDLE_SIZE, i // BUNDLE_SIZE))
            records.sort()
            for offset, r, c in records:
                if offset < 60:                                        # before the data = empty
                    continue
                header = _read(f, offset, 4)
                if header is None:
                    damaged += 1
                    continue
                length = struct.unpack("<I", header)[0]
                if not length:
                    continue
                data = _read(f, offset + 4, length)
                if data is None:
                    damaged += 1
                    continue
                out.append((r0 + r, c0 + c, data))
        else:                                                          # V2, row-major
            index = _read(f, 64, 8 * BUNDLE_SIZE * BUNDLE_SIZE) or b""
            records = []
            for i in range(len(index) // 8):
                value = struct.unpack_from("<Q", index, 8 * i)[0]
                offset, length = value & 0xFFFFFFFFFF, value >> 40
                if length:
                    records.append((offset, length, i // BUNDLE_SIZE, i % BUNDLE_SIZE))
            records.sort()
            for offset, length, r, c in records:
                data = _read(f, offset, length)
                if data is None:
                    damaged += 1
                    continue
                out.append((r0 + r, c0 + c, data))
    if damaged:
        logger.warning("esri_tilecache: bundle %s — %d damaged tiles skipped", name, damaged)
    if stats is not None:
        stats["damaged"] = stats.get("damaged", 0) + damaged
    return out
