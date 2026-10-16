"""Image helpers.

Product images: the data stores an `image_key`; this module resolves it to a
file under static/ (or accepts an allow-listed https URL).

Customer uploads: Pillow-based clean-up for a better experience only
(decode, fix orientation, drop metadata, resize, re-encode as JPEG). This is
NOT a security boundary; the upload service must re-validate every file.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

from .client.contracts import IMAGE_HOSTS

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_DECODE_PIXELS = 40_000_000  # refuse decompression bombs before decoding


def resolve(image: Dict[str, Any]) -> Optional[str]:
    """Path or URL for one listing image, or None if it can't be shown safely."""
    key = image.get("image_key")
    if key:
        path = (STATIC / str(key)).resolve()
        if STATIC.resolve() in path.parents and path.is_file():
            return str(path)
        return None
    url = str(image.get("url") or "")
    if url.startswith("https://") and url.split("/")[2] in IMAGE_HOSTS:
        return url
    return None


class PhotoRejected(ValueError):
    """The upload can't be used; the message is safe to show to customers."""


def prepare_upload(raw: bytes, max_mb: float, max_px: int) -> Tuple[bytes, Dict[str, Any]]:
    """Return (clean JPEG bytes, info). Raises PhotoRejected with a customer-friendly message."""
    if not raw:
        raise PhotoRejected("That file is empty.")
    if len(raw) > max_mb * 1024 * 1024:
        raise PhotoRejected(f"Please use a photo under {max_mb:g} MB.")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            kind = probe.format
            width, height = probe.size
            if kind not in ALLOWED_FORMATS:
                raise PhotoRejected("Please upload a JPG, PNG or WebP photo.")
            if width * height > MAX_DECODE_PIXELS:
                raise PhotoRejected("That photo's resolution is too large. Please use a smaller photo.")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)  # apply the camera orientation, then drop metadata
            img = img.convert("RGB")
            img.thumbnail((max_px, max_px))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)  # no exif/icc/xmp copied
    except PhotoRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise PhotoRejected("We couldn't read that file as a photo. Please try another one.")
    clean = out.getvalue()
    with Image.open(io.BytesIO(clean)) as final:
        size = final.size
    return clean, {"width": size[0], "height": size[1], "bytes": len(clean),
                   "sha256": hashlib.sha256(clean).hexdigest(), "content_type": "image/jpeg",
                   "source_format": ALLOWED_FORMATS.get(kind)}


def thumbnail(clean: bytes, size: int = 360) -> bytes:
    with Image.open(io.BytesIO(clean)) as img:
        img.thumbnail((size, size))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80)
        return out.getvalue()
