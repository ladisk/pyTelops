"""Saving frames and recordings: file names, PNG previews, JSON metadata."""

from __future__ import annotations

import json
import re
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def utc_stamp() -> str:
    """Return the current UTC time as ``YYYYmmdd_HHMMSS``."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def utc_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(name: str) -> str:
    """Return *name* if it is a safe, Windows-compatible file stem.

    Raises
    ------
    ValueError
        If the name has characters other than letters, digits, ``.``, ``_``
        and ``-``, starts with a dot, ends with a dot, or is longer than 80.
    """
    if not _NAME_RE.match(name) or name.endswith("."):
        raise ValueError(
            f"Invalid name {name!r}. Use 1 to 80 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit and not ending with a dot."
        )
    return name


def new_stem(directory: Path, prefix: str, name: str | None, suffixes=(".npy", ".json")) -> str:
    """Return a file stem in *directory* that no existing file uses.

    An explicit *name* is never changed: if it is taken, ``FileExistsError``
    is raised so data is not overwritten. A generated name gets a counter.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if name is not None:
        stem = safe_name(name)
        taken = [stem + s for s in suffixes if (directory / (stem + s)).exists()]
        if taken:
            raise FileExistsError(f"{taken[0]} already exists in {directory}. Pick another name.")
        return stem
    base = f"{prefix}_{utc_stamp()}"
    stem, i = base, 1
    while any((directory / (stem + s)).exists() for s in suffixes):
        i += 1
        stem = f"{base}_{i}"
    return stem


def write_json(path: Path, data: dict) -> None:
    """Write *data* as indented JSON, replacing the file atomically."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=_json_default) + "\n", encoding="utf-8")
    tmp.replace(path)


def _json_default(obj):
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


def png_bytes(img: np.ndarray) -> bytes:
    """Encode a 2-D uint8 array as an 8-bit grayscale PNG (no Pillow needed)."""
    if img.ndim != 2 or img.dtype != np.uint8:
        raise ValueError("png_bytes needs a 2-D uint8 array")
    h, w = img.shape
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def preview_png(frame: np.ndarray) -> bytes:
    """Return a contrast-stretched (1st to 99th percentile) PNG of one frame."""
    f = np.asarray(frame, dtype=np.float32)
    lo, hi = np.percentile(f, [1, 99])
    if hi <= lo:
        hi = lo + 1.0
    img = np.clip((f - lo) / (hi - lo), 0.0, 1.0)
    return png_bytes((img * 255).astype(np.uint8))


def frame_stats(frames: np.ndarray) -> dict:
    """Return min, max, mean and standard deviation of *frames* as floats."""
    f = np.asarray(frames, dtype=np.float64)
    return {
        "min": float(f.min()),
        "max": float(f.max()),
        "mean": float(f.mean()),
        "std": float(f.std()),
    }
