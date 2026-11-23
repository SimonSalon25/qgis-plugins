"""Esri LERC elevation tiles (`.tpkx` with `tileImageInfo.format = LERC`) → float32 TIFF tiles for a
GeoPackage 2D gridded coverage. Requires the optional `lerc` package (pip, Apache 2.0).

LERC is a lossy compression with a guaranteed maximum error (`maxZError` chosen when the cache was built);
the values are decoded as Esri stored them and written without further loss (float32 TIFF, LZW, one band —
as OGC 17-066r1 requires).

**Assumption verified only on synthetic data:** a tile has the cache tile size, or one more (Esri elevation
caches use 257 × 257 tiles overlapping the neighbour by the last row and column) — then the last row and
column are dropped. Any other size is an error. On a real elevation package the position (half a pixel)
should be checked against known points.
"""
from __future__ import annotations

import io

import numpy as np

from .reader import TileCacheError

NODATA = -9999.0


def decode(blob: bytes, tile_size: int, data_null: float = NODATA) -> np.ndarray:
    """Decode one LERC tile to a `tile_size × tile_size` float32 array; masked cells = `data_null`."""
    try:
        import lerc
    except ImportError as exc:
        raise TileCacheError("LERC elevation tiles need the `lerc` package (pip install lerc)") from exc
    result, arr, mask = lerc.decode(bytes(blob))[:3]
    if result != 0:
        raise TileCacheError(f"a LERC tile could not be decoded (code {result})")
    arr = np.asarray(arr, dtype="float32")
    if arr.ndim == 3:
        arr = arr[..., 0] if arr.shape[-1] <= 4 else arr[0]
        if mask is not None and np.ndim(mask) == 3:
            mask = mask[..., 0] if np.shape(mask)[-1] <= 4 else mask[0]
    t = tile_size
    if arr.shape == (t + 1, t + 1):
        arr = arr[:t, :t]
        mask = None if mask is None else np.asarray(mask)[:t, :t]
    if arr.shape != (t, t):
        raise TileCacheError(f"LERC tile {arr.shape} does not match the cache tile size {t} px")
    arr = arr.copy()
    if mask is not None:
        arr[~np.asarray(mask, dtype=bool)] = data_null
    arr[~np.isfinite(arr)] = data_null
    return arr


def to_tiff(arr: np.ndarray) -> bytes:
    """One-band float32 TIFF (LZW)."""
    from PIL import Image
    bio = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr, dtype="float32"), mode="F").save(bio, "TIFF", compression="tiff_lzw")
    return bio.getvalue()


def decode_to_tiff(blob: bytes, tile_size: int, data_null: float = NODATA):
    """(TIFF bytes, (min, max, mean, std_dev)) — statistics of valid values only."""
    arr = decode(blob, tile_size, data_null)
    valid = arr[arr != data_null]
    stats = ((float(valid.min()), float(valid.max()), float(valid.mean()), float(valid.std()))
             if valid.size else (None, None, None, None))
    return to_tiff(arr), stats
