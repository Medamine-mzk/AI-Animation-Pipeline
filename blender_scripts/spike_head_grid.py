"""Head crops of the 18 plates in a grid, so viseme/blink differences are visible.

Run: python spike_head_grid.py -- <plates_dir>
"""

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
PLATES_DIR = Path(args[0]) if args else Path("jobs/spike3d/cc3")
if not PLATES_DIR.is_absolute():
    PLATES_DIR = ROOT / PLATES_DIR
OUT = ROOT / (args[1] if len(args) > 1 else "jobs/spike3d/head_grid.png")

names = [f"{v}_open" for v in "ABCDEFGHX"] + [f"{v}_closed" for v in "ABCDEFGHX"]
cols, rows = 9, 2
cw, ch = 180, 220
pad = 6

# head region: top-left crop of the plate (head sits in the upper area)
def head_crop(img: Image.Image) -> Image.Image:
    w, h = img.size
    return img.crop((0, 0, w, int(h * 0.42))).resize((cw, ch), Image.LANCZOS)

bg = Image.new("RGB", (cols * (cw + pad) + pad, rows * (ch + pad) + pad), (25, 25, 28))
for i, name in enumerate(names):
    img = head_crop(Image.open(PLATES_DIR / f"{name}.png"))
    r, c = divmod(i, cols)
    bg.paste(img, (pad + c * (cw + pad), pad + r * (ch + pad)))
    # top-left corner label
bg.save(OUT)
print(f"head grid -> {OUT}")