"""M3 helper: generate placeholder sprite art + manifests (PIL).

Draws flat cartoon placeholder characters (body + 9 Rhubarb visemes + eye
pair), a living-room background, and writes per-sprite manifests plus the
library/background manifests. Regenerable at any time.

Usage:
    python -m app.pipeline.generate_placeholder_assets
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
SPRITES_DIR = ROOT / "app" / "assets" / "sprites"
BACKGROUNDS_DIR = ROOT / "app" / "assets" / "backgrounds"

BG_W, BG_H = 1920, 1080
MOUTH_W, MOUTH_H = 180, 140
EYE_W, EYE_H = 260, 100

CHARACTERS = [
    {
        "id": "child_v1",
        "height": 480,
        "skin": (255, 219, 172),
        "hair": (122, 85, 55),
        "top": (70, 130, 200),
        "bottom": (60, 60, 90),
    },
    {
        "id": "mom_v1",
        "height": 620,
        "skin": (255, 205, 148),
        "hair": (190, 80, 40),
        "top": (90, 150, 110),
        "bottom": (70, 90, 120),
    },
    {
        "id": "dad_v1",
        "height": 700,
        "skin": (224, 172, 105),
        "hair": (40, 40, 45),
        "top": (110, 110, 125),
        "bottom": (45, 55, 80),
    },
]


def draw_body(cfg: dict) -> tuple[Image.Image, tuple[int, int], int]:
    """Draw a flat cartoon character with a blank face.

    Returns (image, face_center, face_radius) so the generator can place
    visemes/eyes at exact anchors.
    """
    h = cfg["height"]
    w = int(h * 0.62)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    head_r = int(h * 0.16)
    face_cx, face_cy = w // 2, int(h * 0.27)

    torso_top = int(h * 0.44)
    torso_bottom = int(h * 0.82)
    torso_w = int(w * 0.46)

    # legs
    d.rectangle(
        [w // 2 - int(w * 0.20), int(h * 0.78), w // 2 - 4, int(h * 0.97)],
        fill=cfg["bottom"],
    )
    d.rectangle(
        [w // 2 + 4, int(h * 0.78), w // 2 + int(w * 0.20), int(h * 0.97)],
        fill=cfg["bottom"],
    )
    # arms
    d.rounded_rectangle(
        [w // 2 - int(w * 0.30), torso_top + 4, w // 2 - int(w * 0.17), torso_bottom - 6],
        radius=10,
        fill=cfg["skin"],
    )
    d.rounded_rectangle(
        [w // 2 + int(w * 0.17), torso_top + 4, w // 2 + int(w * 0.30), torso_bottom - 6],
        radius=10,
        fill=cfg["skin"],
    )
    # torso
    d.rounded_rectangle(
        [w // 2 - torso_w // 2, torso_top, w // 2 + torso_w // 2, torso_bottom],
        radius=int(h * 0.07),
        fill=cfg["top"],
    )
    # neck
    d.rectangle(
        [face_cx - int(h * 0.045), int(h * 0.38), face_cx + int(h * 0.045), int(h * 0.47)],
        fill=cfg["skin"],
    )
    # head (blank face)
    d.ellipse(
        [face_cx - head_r, face_cy - head_r, face_cx + head_r, face_cy + head_r],
        fill=cfg["skin"],
    )
    # hair cap
    d.pieslice(
        [face_cx - head_r, face_cy - head_r - 2, face_cx + head_r, face_cy + head_r],
        start=180,
        end=360,
        fill=cfg["hair"],
    )
    d.ellipse(
        [face_cx - head_r, face_cy - int(head_r * 0.75), face_cx + head_r, face_cy],
        fill=cfg["hair"],
    )
    return img, (face_cx, face_cy), head_r


def draw_mouth(shape: str) -> Image.Image:
    img = Image.new("RGBA", (MOUTH_W, MOUTH_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = MOUTH_W // 2, MOUTH_H // 2
    dark = (70, 30, 30, 255)
    lip = (160, 70, 70, 255)

    if shape == "X":  # rest: small relaxed line
        d.line([cx - 22, cy, cx + 22, cy], fill=lip, width=5)
    elif shape == "B":  # pressed closed
        d.line([cx - 28, cy, cx + 28, cy], fill=lip, width=7)
    elif shape == "C":  # small open (ee)
        d.ellipse([cx - 22, cy - 10, cx + 22, cy + 10], outline=lip, width=5, fill=dark)
    elif shape == "D":  # medium open (ai)
        d.ellipse([cx - 34, cy - 18, cx + 34, cy + 18], outline=lip, width=5, fill=dark)
    elif shape == "E":  # tall open (oh)
        d.ellipse([cx - 30, cy - 28, cx + 30, cy + 28], outline=lip, width=5, fill=dark)
    elif shape == "F":  # wide flat (fv)
        d.ellipse([cx - 46, cy - 12, cx + 46, cy + 12], outline=lip, width=5, fill=dark)
    elif shape == "G":  # th: rect + tongue
        d.rounded_rectangle([cx - 30, cy - 14, cx + 30, cy + 16], radius=6, outline=lip, width=5, fill=dark)
        d.rounded_rectangle([cx - 12, cy + 8, cx + 12, cy + 26], radius=5, fill=(230, 130, 110, 255))
    elif shape == "H":  # oo: circle
        d.ellipse([cx - 24, cy - 26, cx + 24, cy + 26], outline=lip, width=5, fill=dark)
    elif shape == "A":  # wide open (ah)
        d.ellipse([cx - 40, cy - 24, cx + 40, cy + 24], outline=lip, width=5, fill=dark)
    else:
        raise ValueError(f"unknown viseme {shape}")
    return img


def draw_eyes(open_: bool) -> Image.Image:
    img = Image.new("RGBA", (EYE_W, EYE_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cy = EYE_H // 2
    for side in (-1, 1):
        x = EYE_W // 2 + side * 52
        if open_:
            d.ellipse([x - 26, cy - 20, x + 26, cy + 20], fill=(255, 255, 255, 255))
            d.ellipse([x - 11, cy - 12, x + 11, cy + 12], fill=(35, 35, 40, 255))
        else:
            d.arc([x - 26, cy - 22, x + 26, cy + 14], start=200, end=340, fill=(35, 35, 40, 255), width=6)
    return img


def draw_background() -> Image.Image:
    img = Image.new("RGB", (BG_W, BG_H), (245, 238, 224))
    d = ImageDraw.Draw(img)
    # wall + floor
    d.rectangle([0, 0, BG_W, int(BG_H * 0.72)], fill=(236, 222, 205))
    d.rectangle([0, int(BG_H * 0.72), BG_W, BG_H], fill=(168, 124, 82))
    # rug
    d.ellipse([BG_W // 2 - 420, int(BG_H * 0.82), BG_W // 2 + 420, BG_H + 80], fill=(200, 90, 90))
    # window
    d.rectangle([150, 120, 560, 470], fill=(140, 195, 230))
    d.rectangle([150, 120, 560, 470], outline=(120, 90, 60), width=10)
    d.line([355, 120, 355, 470], fill=(120, 90, 60), width=8)
    d.line([150, 295, 560, 295], fill=(120, 90, 60), width=8)
    # sun + cloud
    d.ellipse([220, 170, 280, 230], fill=(255, 230, 120))
    d.ellipse([390, 200, 470, 240], fill=(255, 255, 255))
    d.ellipse([430, 185, 510, 225], fill=(255, 255, 255))
    # sofa
    d.rounded_rectangle([1250, 640, 1780, 880], radius=30, fill=(110, 140, 170))
    d.rounded_rectangle([1220, 600, 1810, 700], radius=30, fill=(120, 150, 185))
    d.rectangle([1230, 650, 1810, 700], fill=(110, 140, 170))
    # picture frame
    d.rectangle([1620, 180, 1820, 330], outline=(120, 90, 60), width=8, fill=(250, 240, 220))
    d.ellipse([1660, 220, 1780, 300], fill=(220, 190, 90))
    return img


def write_character(cfg: dict) -> None:
    out_dir = SPRITES_DIR / cfg["id"]
    out_dir.mkdir(parents=True, exist_ok=True)

    body, (face_cx, face_cy), head_r = draw_body(cfg)
    body.save(out_dir / "body.png")

    mouth_anchor = {"x": face_cx, "y": face_cy + int(head_r * 0.52)}
    eye_anchor = {"x": face_cx, "y": face_cy - int(head_r * 0.05)}

    visemes = {}
    for shape in "ABCDEFGHX":
        name = f"mouth_{shape}.png"
        draw_mouth(shape).save(out_dir / name)
        visemes[shape] = name

    draw_eyes(True).save(out_dir / "eyes_open.png")
    draw_eyes(False).save(out_dir / "eyes_closed.png")

    manifest = {
        "id": cfg["id"],
        "base_body": "body.png",
        "visemes": visemes,
        "eyes": {"open": "eyes_open.png", "closed": "eyes_closed.png"},
        "anchor_point": mouth_anchor,
        "eye_anchor": eye_anchor,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"[assets-gen] wrote {out_dir} ({len(visemes)} visemes, anchors {mouth_anchor}/{eye_anchor})")


def main() -> int:
    for cfg in CHARACTERS:
        write_character(cfg)

    BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    draw_background().save(BACKGROUNDS_DIR / "living_room.png")
    (BACKGROUNDS_DIR / "manifest.json").write_text(
        json.dumps({"backgrounds": [{"setting": "living_room", "file": "living_room.png"}]}, indent=2),
        encoding="utf-8",
    )
    print(f"[assets-gen] wrote {BACKGROUNDS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
