import sys
from pathlib import Path
import json
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def w2p(px, py, x0, y0, s, ox, oy):
    return ox + (px - x0) * s, oy + (py - y0) * s


def draw_floor_map(out_dir):
    out = Path(out_dir)
    data = np.load(out / "grid.npz")
    grid, layers = data["grid"], data["layers"]
    xc, yc = data["xc"], data["yc"]
    bounds = data["bounds"]
    report = json.loads((out / "room_report.json").read_text(encoding="utf-8"))
    spots = report["spots"]
    cell = report["fit"].get("cell", 0.15) if "cell" in report["fit"] else 0.15

    W, H = 1200, 900
    img = Image.new("RGB", (W, H), (12, 12, 16))
    d = ImageDraw.Draw(img)
    x0, x1, y0, y1 = bounds[0], bounds[1], bounds[2], bounds[3]
    pad = 60
    s = min((W - 2 * pad) / max(1e-6, (x1 - x0)),
            (H - 2 * pad) / max(1e-6, (y1 - y0)))
    ox = (W - (x1 - x0) * s) / 2
    oy = (H - (y1 - y0) * s) / 2

    max_layers = max(1, int(layers.max()))
    for r in range(grid.shape[0]):
        for c in range(grid.shape[1]):
            if not grid[r, c]:
                continue
            l = layers[r, c]
            a = 60 + int(90 * (l / max_layers))
            g = 120 + int(90 * (l / max_layers))
            px, py = w2p(xc[c] - cell / 2, yc[r] - cell / 2, x0, y0, s, ox, oy)
            d.rectangle([px, py, px + cell * s, py + cell * s], fill=(20, g, a))
    for i, sp in enumerate(spots, 1):
        px, py = w2p(sp["x"], sp["y"], x0, y0, s, ox, oy)
        d.ellipse([px - 9, py - 9, px + 9, py + 9], fill=(255, 60, 60),
                  outline=(255, 255, 255))
        d.text((px + 11, py - 7), str(i), fill=(255, 255, 255))
    try:
        fnt = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 16)
    except Exception:
        fnt = ImageFont.load_default()
    d.text((10, 8), f"floor map - {len(spots)} candidate spots ({out.name})",
           fill=(220, 220, 220), font=fnt)
    d.text((10, 30),
           "green = walkable (brighter = more clearance), red dot = spot",
           fill=(160, 160, 170), font=fnt)
    img.save(out / "floor_map.png")


def montage(out_dir):
    out = Path(out_dir)
    report = json.loads((out / "room_report.json").read_text(encoding="utf-8"))
    spots = report["spots"]
    files = [out / f"spot_{i:02d}.png" for i in range(1, len(spots) + 1)]
    files = [f for f in files if f.exists()]
    if not files:
        return
    thumbs = [Image.open(f).convert("RGB") for f in files]
    tw, th = thumbs[0].size
    label_h = 26
    cols = 4
    rows = int(math.ceil(len(thumbs) / cols))
    sheet = Image.new("RGB", (cols * tw, rows * (th + label_h) + 30), (10, 10, 12))
    d = ImageDraw.Draw(sheet)
    try:
        fnt = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 13)
    except Exception:
        fnt = ImageFont.load_default()
    d.text((10, 8), "candidate spots - capsule = character standing here",
           fill=(220, 220, 220), font=fnt)
    for i, (im, sp) in enumerate(zip(thumbs, spots)):
        r, c = divmod(i, cols)
        x, y = c * tw, 30 + r * (th + label_h)
        sheet.paste(im, (x, y))
        d.rectangle([x, y + th, x + tw, y + th + label_h], fill=(30, 30, 36))
        d.text((x + 6, y + th + 5),
               f"#{i+1} clr={sp['clearance']:.1f}m ({sp['x']:.2f},{sp['y']:.2f})",
               fill=(255, 220, 120), font=fnt)
    sheet.save(out / "spots_preview.png")


if __name__ == "__main__":
    for d in sys.argv[1:]:
        draw_floor_map(d)
        montage(d)
        print("drew", d)