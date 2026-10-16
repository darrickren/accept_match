"""Synthetic product illustrations for the demo (no photos, no third-party assets).

Draws simple bag illustrations whose visible marks match each unit's recorded
condition, and labels every image "Demo image". Regenerate with:

    python -m ui.demo_art

Writes static/units/<unit>/<view>.jpg for the catalogue and
samples/return_photos/*.jpg for presenters to upload in the Return demo.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SIZE = 900
COLOURS = {"black": (42, 42, 46), "taupe": (166, 152, 136), "cognac": (148, 86, 50), "ivory": (232, 224, 209),
           "burgundy": (108, 35, 50)}
Box = Tuple[int, int, int, int]


def _mix(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _background() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), (246, 246, 248))
    draw = ImageDraw.Draw(img)
    for y in range(SIZE):
        draw.line([(0, y), (SIZE, y)], fill=_mix((248, 248, 250), (234, 234, 238), y / SIZE))
    return img


def _shadow(img: Image.Image, box: Box) -> None:
    layer = Image.new("L", img.size, 0)
    x0, _, x1, y1 = box
    ImageDraw.Draw(layer).ellipse([x0 + 10, y1 - 18, x1 - 10, y1 + 30], fill=120)
    layer = layer.filter(ImageFilter.GaussianBlur(22))
    img.paste((0, 0, 0), mask=layer.point(lambda v: int(v * 0.45)))


def _geometry(category: str, style: str) -> Dict[str, object]:
    if category == "tote":
        return {"body": (190, 330, 710, 690), "radius": 30, "handles": [(270, 330, 450, 150), (450, 330, 630, 150)],
                "zip": False, "flap": False}
    if category == "handbag":
        return {"body": (240, 360, 660, 650), "radius": 40, "handles": [(330, 360, 570, 205)], "zip": True,
                "flap": False}
    if style == "soft":
        return {"body": (290, 400, 610, 630), "radius": 70, "strap": True, "zip": True, "flap": False}
    return {"body": (230, 360, 670, 640), "radius": 46, "strap": True, "zip": True, "flap": True}


def _stitch(draw: ImageDraw.ImageDraw, box: Box, radius: int, colour) -> None:
    x0, y0, x1, y1 = box
    inset = 16
    for x in range(x0 + radius, x1 - radius, 22):
        draw.line([(x, y0 + inset), (x + 10, y0 + inset)], fill=colour, width=2)
        draw.line([(x, y1 - inset), (x + 10, y1 - inset)], fill=colour, width=2)
    for y in range(y0 + radius, y1 - radius, 22):
        draw.line([(x0 + inset, y), (x0 + inset, y + 10)], fill=colour, width=2)
        draw.line([(x1 - inset, y), (x1 - inset, y + 10)], fill=colour, width=2)


def _defect_anchor(geo: Dict[str, object], area: str, defect_type: str) -> Tuple[int, int]:
    x0, y0, x1, y1 = geo["body"]
    if defect_type == "corner_wear":
        return x1 - 38, y1 - 34
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    return (cx + 70, cy + 40) if area == "back" else (cx - 80, cy + 60)


def _draw_defect(img: Image.Image, base, anchor: Tuple[int, int], defect_type: str, severity: str) -> None:
    scale = 1.0 if severity == "minor" else 1.7
    ax, ay = anchor
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    light = _mix(base, (255, 255, 255), 0.55)
    if defect_type == "scratch":
        for i, length in enumerate((84, 62, 46)[: 2 if severity == "minor" else 3]):
            dx, dy = length * scale * math.cos(-0.35), length * scale * math.sin(-0.35)
            ox = ax + i * 9
            oy = ay + i * 12
            draw.line([(ox - dx / 2, oy - dy / 2), (ox + dx / 2, oy + dy / 2)], fill=light + (215,), width=3)
        img.alpha_composite(layer) if img.mode == "RGBA" else img.paste(layer, mask=layer)
        return
    if defect_type == "scuff":
        w, h = 74 * scale, 42 * scale
        draw.ellipse([ax - w, ay - h, ax + w, ay + h], fill=light + (150,))
        layer = layer.filter(ImageFilter.GaussianBlur(7))
    elif defect_type == "stain":
        for dx, dy, r in ((0, 0, 30), (22, 8, 20), (-16, 12, 18)):
            r = r * scale
            draw.ellipse([ax + dx - r, ay + dy - r, ax + dx + r, ay + dy + r], fill=(84, 58, 38, 120))
        layer = layer.filter(ImageFilter.GaussianBlur(3))
    elif defect_type == "discoloration":
        r = 120 * scale
        draw.ellipse([ax - r, ay - r * 0.7, ax + r, ay + r * 0.7], fill=light + (95,))
        layer = layer.filter(ImageFilter.GaussianBlur(26))
    elif defect_type == "corner_wear":
        r = 34 * scale
        draw.ellipse([ax - r, ay - r, ax + r, ay + r], fill=light + (175,))
        layer = layer.filter(ImageFilter.GaussianBlur(8))
    img.paste(layer, mask=layer)


def _tag(img: Image.Image, label: str) -> None:
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=22)
    text_w = draw.textlength(label, font=font)
    draw.rounded_rectangle([28, SIZE - 66, 28 + text_w + 32, SIZE - 26], radius=20, fill=(255, 255, 255))
    draw.text((44, SIZE - 58), label, fill=(110, 110, 115), font=font)


def render(category: str, style: str, colour: str, view: str, defects=(), area: str = "front") -> Image.Image:
    """One view of a bag. `defects` are (type, severity) drawn on `area` when that side is shown."""
    base = COLOURS[colour]
    dark, light = _mix(base, (0, 0, 0), 0.25), _mix(base, (255, 255, 255), 0.22)
    stitch = _mix(base, (255, 255, 255), 0.35) if sum(base) < 380 else _mix(base, (0, 0, 0), 0.25)
    img = _background()
    geo = _geometry(category, style)
    box = geo["body"]
    _shadow(img, box)
    draw = ImageDraw.Draw(img)
    for x0, y0, x1, top in geo.get("handles", []):
        draw.arc([x0, top, x1, y0 + (y0 - top)], start=180, end=360, fill=dark, width=18)
    if geo.get("strap"):
        x0, y0, x1, _ = box
        draw.arc([x0 + 34, 150, x1 - 34, y0 + (y0 - 150) + 8], start=180, end=360, fill=dark, width=10)
    draw.rounded_rectangle(box, radius=geo["radius"], fill=base)
    x0, y0, x1, y1 = box
    draw.rounded_rectangle([x0, y0, x1, y0 + 26], radius=12, fill=light)
    if geo.get("zip"):
        draw.line([(x0 + 34, y0 + 14), (x1 - 34, y0 + 14)], fill=(176, 176, 182), width=6)
        draw.rounded_rectangle([x1 - 70, y0 + 6, x1 - 54, y0 + 40], radius=4, fill=(196, 170, 112))
    if view == "front" and geo.get("flap"):
        draw.rounded_rectangle([x0, y0, x1, y0 + int((y1 - y0) * 0.58)], radius=geo["radius"], fill=dark)
        cx = (x0 + x1) // 2
        flap_y = y0 + int((y1 - y0) * 0.58)
        draw.rounded_rectangle([cx - 26, flap_y - 30, cx + 26, flap_y - 6], radius=8, fill=(196, 170, 112))
    if view == "back":
        mid = (y0 + y1) // 2
        draw.line([(x0 + 40, mid), (x1 - 40, mid)], fill=stitch, width=2)
    _stitch(draw, box, geo["radius"], stitch)
    if (view == "back" and area == "back") or (view == "front" and area == "front"):
        canvas = img.convert("RGBA")
        for defect_type, severity in defects:
            _draw_defect(canvas, base, _defect_anchor(geo, area, defect_type), defect_type, severity)
        img = canvas.convert("RGB")
    return img


def detail(full: Image.Image, anchor: Tuple[int, int]) -> Image.Image:
    ax, ay = anchor
    half = 150
    crop = full.crop((ax - half, ay - half, ax + half, ay + half)).resize((SIZE, SIZE), Image.LANCZOS)
    draw = ImageDraw.Draw(crop)
    draw.ellipse([SIZE / 2 - 230, SIZE / 2 - 230, SIZE / 2 + 230, SIZE / 2 + 230], outline=(255, 255, 255), width=4)
    return crop


def generate() -> int:
    from .client import seed

    products = {p["product_id"]: p for p in seed.PRODUCTS}
    written = 0
    for unit_id, product_id, colour, _avail, _price, _ref, findings in seed.UNIT_SPECS:
        if findings is None:
            continue
        item = products[product_id]
        out = ROOT / "static" / "units" / unit_id
        out.mkdir(parents=True, exist_ok=True)
        for view in ("front", "back"):
            defects = findings.get(view) if isinstance(findings.get(view), list) else []
            img = render(item["category"], item["style"], colour, view, defects, area=view)
            _tag(img, f"Demo image · {unit_id}")
            img.save(out / f"{view}.jpg", quality=82, optimize=True)
            written += 1
            for n, defect in enumerate(defects, start=1):
                clean = render(item["category"], item["style"], colour, view, defects, area=view)
                close = detail(clean, _defect_anchor(_geometry(item["category"], item["style"]), view, defect[0]))
                _tag(close, f"Demo close-up · {unit_id}")
                close.save(out / f"detail-{view}-{n}.jpg", quality=82, optimize=True)
                written += 1
    samples = ROOT / "samples" / "return_photos"
    samples.mkdir(parents=True, exist_ok=True)
    geo = _geometry("crossbody", "structured")
    for name, view, defects, area in (("1-front", "front", (), "front"),
                                      ("2-back", "back", (("scratch", "minor"),), "back"),
                                      ("3-scratch-closeup", "back", (("scratch", "minor"),), "back"),
                                      ("4-zipper-closeup", "front", (), "front")):
        img = render("crossbody", "structured", "black", view, defects, area=area)
        if name.startswith("3"):
            img = detail(img, _defect_anchor(geo, "back", "scratch"))
        if name.startswith("4"):
            img = detail(img, (geo["body"][2] - 120, geo["body"][1] + 20))
        _tag(img, "Sample return photo · synthetic")
        img.save(samples / f"{name}.jpg", quality=82, optimize=True)
        written += 1
    return written


if __name__ == "__main__":
    print("images written:", generate())
