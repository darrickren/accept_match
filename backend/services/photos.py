"""Upload validation and deterministic photo quality checks (Pillow; no AI involved).

Every upload is checked by magic bytes and a real decode, re-encoded as JPEG
without metadata (drops EXIF/GPS), and measured for resolution, sharpness,
exposure and a perceptual hash used for duplicate detection. These checks run
in every APP_MODE; the vision model only adds observations on top.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any, Dict, Tuple

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

from ..errors import ApiError

MAX_PIXELS = 40_000_000
MAX_SIDE = 2048
MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def sniff(data: bytes) -> str:
    for prefix, kind in MAGIC:
        if data.startswith(prefix):
            return kind
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def sharpness(img: Image.Image) -> float:
    """Variance of a Laplacian on a 512 px greyscale copy: low means blurry."""
    grey = img.convert("L")
    grey.thumbnail((512, 512))
    lap = grey.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    return round(float(ImageStat.Stat(lap).var[0]), 2)


def brightness(img: Image.Image) -> float:
    return round(float(ImageStat.Stat(img.convert("L")).mean[0]), 2)


def dhash(img: Image.Image, side: int = 16) -> str:
    """Difference hash (side x side bits): near-identical photos have a small Hamming distance."""
    grey = img.convert("L").resize((side + 1, side), Image.Resampling.LANCZOS)
    px = list(grey.tobytes())  # one byte per pixel in mode L
    bits = 0
    for row in range(side):
        for col in range(side):
            bits = (bits << 1) | (1 if px[row * (side + 1) + col] > px[row * (side + 1) + col + 1] else 0)
    return f"{bits:0{side * side // 4}x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def verdict(metrics: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, str]:
    q = rules.get("quality", {})
    return {
        "resolution": "PASS" if min(metrics["width"], metrics["height"]) >= q.get("min_side_px", 480) else "FAIL",
        "sharpness": "PASS" if metrics["sharpness"] >= q.get("min_sharpness", 150.0) else "FAIL",
        "exposure": "PASS" if q.get("min_brightness", 45) <= metrics["brightness"] <= q.get("max_brightness", 240)
        else "FAIL",
    }


def retake_reason(verdict_row: Dict[str, str]) -> str:
    if verdict_row.get("resolution") == "FAIL":
        return "The photo is too small to check. Please use a larger photo."
    if verdict_row.get("exposure") == "FAIL":  # before sharpness: a dark photo also loses its edges
        return "The photo is too dark or too bright. Please retake it in daylight."
    if verdict_row.get("sharpness") == "FAIL":
        return "The photo is too blurry to check. Hold steady, in good light."
    return "Please retake this photo."


def clean_upload(data: bytes, declared_type: str, max_bytes: int, rules: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any]]:
    """(clean JPEG bytes, metrics). Raises ApiError 413/422 with customer-safe messages."""
    if len(data) > max_bytes:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"Images must be {max_bytes // (1024 * 1024)} MB or smaller.")
    if not data:
        raise ApiError(422, "INVALID_IMAGE", "The file is empty.")
    kind = sniff(data)
    if not kind or (declared_type and declared_type.split(";")[0].strip().lower() not in (kind, "application/octet-stream")):
        raise ApiError(422, "INVALID_IMAGE", "Upload a JPEG, PNG or WebP photo.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.size[0] * probe.size[1] > MAX_PIXELS:
                raise ApiError(422, "INVALID_IMAGE", "That photo's resolution is too large.")
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=90, optimize=True)
            metrics = {"width": img.size[0], "height": img.size[1], "sharpness": sharpness(img),
                       "brightness": brightness(img), "dhash": dhash(img)}
    except ApiError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ApiError(422, "INVALID_IMAGE", "That file isn't a readable photo.") from None
    clean = out.getvalue()
    metrics["sha256"] = hashlib.sha256(clean).hexdigest()
    metrics["bytes"] = len(clean)
    metrics["verdict"] = verdict(metrics, rules)
    return clean, metrics
