"""Deterministic synthetic footwear images (Pillow). Every image is labelled SYNTHETIC in the manifest.

Used for seeded inventory photos and for a demo set of seven return views.
They are illustrations, not photographs, so real-model results on them prove
wiring only, not visual accuracy (see the plan's section 9).
"""

from __future__ import annotations

import io
import random
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

COLOURS = {"black": (34, 34, 38), "brown": (122, 74, 42), "beige": (214, 190, 156), "white": (236, 236, 232),
           "navy": (34, 46, 84), "burgundy": (110, 30, 44), "grey": (128, 128, 132), "tan": (184, 132, 84)}
VIEW_TEXT = {"pair_top": "both shoes, from above", "pair_outer": "outer sides", "pair_inner": "inner sides",
             "pair_soles": "soles", "pair_heels": "heels, from behind", "defect_closeup": "close-up of the issue",
             "size_label": "size label"}


def _background(size: Tuple[int, int], seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", size, (238, 236, 230))
    draw = ImageDraw.Draw(img)
    for x in range(0, size[0], 14):  # a fine weave so the photo has real edges (sharpness checks)
        draw.line([(x, 0), (x, size[1])], fill=(226, 223, 216), width=1)
    for y in range(0, size[1], 14):
        draw.line([(0, y), (size[0], y)], fill=(229, 226, 219), width=1)
    for _ in range(size[0] * size[1] // 900):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        shade = rng.randrange(205, 245)
        draw.point((x, y), fill=(shade, shade - 2, shade - 6))
    return img


def _shoe(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], colour, flip: bool = False) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    dark = tuple(max(0, c - 40) for c in colour)
    sole = (60, 56, 52) if sum(colour) > 300 else (210, 205, 196)
    points = [(x0, y0 + h * .55), (x0 + w * .18, y0 + h * .2), (x0 + w * .45, y0 + h * .1), (x0 + w * .62, y0),
              (x0 + w * .78, y0 + h * .05), (x1, y0 + h * .45), (x1, y1 - h * .12), (x0, y1 - h * .12)]
    if flip:
        points = [(x0 + x1 - px, py) for px, py in points]
    draw.polygon(points, fill=colour, outline=dark, width=4)
    draw.rectangle([x0, y1 - h * .14, x1, y1], fill=sole, outline=dark, width=3)
    for i in range(10):  # stitching
        sx = x0 + w * (.12 + i * .075)
        draw.line([(sx, y1 - h * .2), (sx + w * .035, y1 - h * .2)], fill=(232, 220, 200), width=3)


def _defect(draw: ImageDraw.ImageDraw, kind: str, centre: Tuple[int, int], radius: int) -> None:
    cx, cy = centre
    if kind == "scratch":
        for i in range(3):
            draw.line([(cx - radius + i * 6, cy - radius // 3 + i * 4), (cx + radius // 2 + i * 6, cy + i * 4)],
                      fill=(200, 200, 196), width=3)
    elif kind in ("stain", "discoloration"):
        draw.ellipse([cx - radius, cy - radius // 2, cx + radius, cy + radius // 2], fill=(150, 130, 90))
    elif kind == "scuff":
        draw.rectangle([cx - radius, cy - radius // 3, cx + radius, cy + radius // 3], fill=(170, 160, 150))
    else:
        draw.line([(cx - radius, cy), (cx + radius, cy)], fill=(230, 60, 40), width=5)
    draw.ellipse([cx - radius * 1.6, cy - radius * 1.6, cx + radius * 1.6, cy + radius * 1.6], outline=(220, 40, 40),
                 width=4)


def _top(draw: ImageDraw.ImageDraw, box, colour) -> None:
    x0, y0, x1, y1 = box
    dark = tuple(max(0, c - 40) for c in colour)
    draw.ellipse(box, fill=colour, outline=dark, width=4)
    w, h = x1 - x0, y1 - y0
    draw.ellipse([x0 + w * .25, y0 + h * .45, x1 - w * .25, y1 - h * .12], fill=(70, 62, 56), outline=dark, width=3)
    for i in range(5):  # laces
        y = y0 + h * (.18 + i * .055)
        draw.line([(x0 + w * .3, y), (x1 - w * .3, y)], fill=(236, 230, 220), width=4)


def _sole(draw: ImageDraw.ImageDraw, box) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=int((x1 - x0) * .45), fill=(66, 62, 58), outline=(30, 30, 30), width=4)
    for i in range(1, 14):  # tread
        y = y0 + (y1 - y0) * i / 14
        draw.line([(x0 + 14, y), (x1 - 14, y)], fill=(118, 112, 106), width=5)


def _heel(draw: ImageDraw.ImageDraw, box, colour) -> None:
    x0, y0, x1, y1 = box
    dark = tuple(max(0, c - 40) for c in colour)
    draw.rounded_rectangle([x0, y0, x1, y1 - (y1 - y0) * .2], radius=int((x1 - x0) * .4), fill=colour, outline=dark,
                           width=4)
    draw.rectangle([x0 + (x1 - x0) * .25, y1 - (y1 - y0) * .22, x1 - (x1 - x0) * .25, y1], fill=(52, 48, 44))


def render(view: str, colour: str = "black", defect: Optional[str] = "scratch", seed: int = 1,
           size: Tuple[int, int] = (1000, 750), label: str = "US W 7 / EU 38", blurry: bool = False,
           dark: bool = False) -> bytes:
    img = _background(size, seed)
    draw = ImageDraw.Draw(img)
    rgb = COLOURS.get(colour, COLOURS["black"])
    w, h = size
    mark = int(min(w, h) * .04)
    if view == "size_label":
        draw.rectangle([w * .2, h * .2, w * .8, h * .8], fill=(190, 186, 178), outline=(80, 80, 80), width=6)
        draw.rectangle([w * .3, h * .32, w * .7, h * .68], fill=(246, 244, 238), outline=(60, 60, 60), width=4)
        draw.text((w * .34, h * .4), "AcceptMatch demo label", fill=(20, 20, 20))
        draw.text((w * .34, h * .5), label, fill=(20, 20, 20))
    elif view == "defect_closeup":
        _shoe(draw, (int(-w * .2), int(h * .1), int(w * 1.2), int(h * .95)), rgb)
        if defect:
            _defect(draw, defect, (int(w * .55), int(h * .45)), int(min(w, h) * .09))
    elif view == "pair_top":
        _top(draw, (int(w * .14), int(h * .06), int(w * .44), int(h * .94)), rgb)
        _top(draw, (int(w * .56), int(h * .06), int(w * .86), int(h * .94)), rgb)
        if defect:
            _defect(draw, defect, (int(w * .29), int(h * .82)), mark)
    elif view == "pair_soles":
        _sole(draw, (int(w * .18), int(h * .05), int(w * .42), int(h * .95)))
        _sole(draw, (int(w * .58), int(h * .05), int(w * .82), int(h * .95)))
    elif view == "pair_heels":
        _heel(draw, (int(w * .22), int(h * .15), int(w * .44), int(h * .9)), rgb)
        _heel(draw, (int(w * .56), int(h * .15), int(w * .78), int(h * .9)), rgb)
        if defect:
            _defect(draw, defect, (int(w * .33), int(h * .45)), mark)
    else:  # pair_outer: toes point outwards; pair_inner: toes point inwards
        outer = view == "pair_outer"
        _shoe(draw, (int(w * .04), int(h * .3), int(w * .48), int(h * .78)), rgb, flip=not outer)
        _shoe(draw, (int(w * .52), int(h * .3), int(w * .96), int(h * .78)), rgb, flip=outer)
        if defect and outer:
            _defect(draw, defect, (int(w * .4), int(h * .55)), mark)
    draw.text((16, 12), f"SYNTHETIC DEMO IMAGE · {VIEW_TEXT.get(view, view)}", fill=(90, 90, 90))
    if blurry:
        img = img.filter(ImageFilter.GaussianBlur(radius=max(4, w // 160)))
    if dark:
        img = img.point(lambda v: v // 6)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()
