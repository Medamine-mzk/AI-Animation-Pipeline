"""Spike: build a contact sheet of the 3D plates + a mock composite on the living room."""

import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
PLATES_DIR = Path(args[0]) if args else Path("jobs/spike3d/cc3")
if not PLATES_DIR.is_absolute():
    PLATES_DIR = ROOT / PLATES_DIR
OUT_SHEET = ROOT / (args[1] if len(args) > 1 else "jobs/spike3d/contact_sheet3d.png")
OUT_COMP = ROOT / (args[2] if len(args) > 2 else "jobs/spike3d/composite3d.png")
BG = ROOT / "app" / "assets" / "backgrounds" / "living_room.png"
CANVAS_W, CANVAS_H = 1920, 1080
FLOOR_Y = 810
SPOTS = [0.28, 0.5, 0.72]

names = [f"{v}_open" for v in "ABCDEFGHX"] + [f"{v}_closed" for v in "ABCDEFGHX"]
cols, rows = 9, 2
thumb_w = 300
thumb_h = 600
pad = 12

sheet = Image.new("RGBA", (cols * (thumb_w + pad) + pad, rows * (thumb_h + pad) + pad), (20, 20, 24, 255))
for i, name in enumerate(names):
    img = Image.open(PLATES_DIR / f"{name}.png")
    img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
    r, c = divmod(i, cols)
    x = pad + c * (thumb_w + pad) + (thumb_w - img.width) // 2
    y = pad + r * (thumb_h + pad) + (thumb_h - img.height) // 2
    sheet.paste(img, (x, y), img)
sheet.convert("RGB").save(OUT_SHEET)
print(f"contact sheet -> {OUT_SHEET}")

if BG.exists():
    bg = Image.open(BG).convert("RGB").resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
    plate = Image.open(PLATES_DIR / "A_open.png")
    target_h = 700  # matches the tallest 2D sibling (dad_v1 = 700px)
    scale = target_h / plate.height
    plate = plate.resize((round(plate.width * scale), target_h), Image.LANCZOS)
    x = round(CANVAS_W * SPOTS[1]) - plate.width // 2
    comp = bg.copy()
    comp.paste(plate, (x, FLOOR_Y - plate.height), plate)
    comp.save(OUT_COMP)
    print(f"composite -> {OUT_COMP}")
else:
    print(f"no background: {BG}")