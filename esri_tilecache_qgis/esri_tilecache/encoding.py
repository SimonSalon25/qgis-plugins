"""Encoding of one tile, safe to run on threads (Pillow releases the GIL while encoding and decoding).

Formats:

* ``WEBP``  — opaque tile as RGB, with transparency RGBA (lossless alpha);
* ``PNG``   — lossless, RGB or RGBA;
* ``JPEG``  — composited on white (overviews of line maps);
* ``JPEG_AUTO`` — opaque tile as JPEG, with transparency PNG (JPEG has no alpha; like GDAL `TILE_FORMAT=AUTO`).

A fully transparent tile is not encoded (`None`) and is not stored.
"""
from __future__ import annotations

import gc
import io
from contextlib import contextmanager

import numpy as np


def encode(image, fmt: str, quality: int = 75) -> bytes | None:
    """`image` = PIL Image RGBA or a (h, w, 4) uint8 array."""
    from PIL import Image

    if isinstance(image, np.ndarray):
        image = Image.fromarray(image, "RGBA")
    alpha_min, alpha_max = image.getchannel("A").getextrema()
    if alpha_max == 0:
        return None
    opaque = alpha_min == 255
    bio = io.BytesIO()
    fmt = fmt.upper()
    if fmt == "WEBP":
        (image.convert("RGB") if opaque else image).save(bio, "WEBP", quality=int(quality), method=4)
    elif fmt == "JPEG":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        background.save(bio, "JPEG", quality=int(quality))
    elif fmt == "JPEG_AUTO":
        if opaque:
            image.convert("RGB").save(bio, "JPEG", quality=int(quality))
        else:
            image.save(bio, "PNG")
    else:
        (image.convert("RGB") if opaque else image).save(bio, "PNG")
    return bio.getvalue()


def reduce(image, factor: int):
    """Downsample by averaging with premultiplied alpha (RGBa) — otherwise transparent pixels would blend
    black into the edges of the data."""
    return image.convert("RGBa").reduce(factor).convert("RGBA")


@contextmanager
def no_garbage_collection():
    """Disable automatic cyclic garbage collection while worker threads run.

    The collector may run on any thread and destroy objects there; GUI toolkits (Qt) forbid that outside the
    main thread and the host application crashes ("Timers cannot be stopped from another thread"). Reference
    counting keeps freeing memory; cycles are collected at the next collection after re-enabling.
    """
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()
