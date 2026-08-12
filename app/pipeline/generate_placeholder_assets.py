"""M3 helper: generate cartoon sprite art + manifests (PIL).

Draws flat-cartoon characters (body with face details + 9 Rhubarb visemes +
eye pair with blink state), a living-room background, and writes per-sprite
manifests plus the library/background manifests. Regenerable at any time.

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
OUTLINE = (48, 40, 52)
REF_HEIGHT = 620  # mouth/eye sprites are drawn at this character scale

CHARACTERS = [
    {
        "id": "child_v1",
        "height": 480,
        "skin": (255, 222, 178),
        "skin_shade": (238, 196, 150),
        "hair": (148, 104, 68),
        "hair_style": "pigtails",
        "top": (66, 135, 245),
        "top_accent": (40, 100, 220),
        "bottom": (72, 72, 100),
        "shoes": (210, 60, 60),
        "cheek": (255, 158, 148),
        "brow": (122, 82, 52),
        "head_r": 0.185,
    },
    {
        "id": "mom_v1",
        "height": 620,
        "skin": (250, 205, 150),
        "skin_shade": (228, 180, 125),
        "hair": (172, 82, 46),
        "hair_style": "bob",
        "top": (110, 168, 132),
        "top_accent": (80, 140, 105),
        "bottom": (92, 112, 152),
        "shoes": (70, 55, 70),
        "cheek": (252, 170, 150),
        "brow": (140, 66, 38),
        "head_r": 0.16,
    },
    {
        "id": "dad_v1",
        "height": 700,
        "skin": (233, 183, 122),
        "skin_shade": (210, 158, 100),
        "hair": (45, 45, 52),
        "hair_style": "short",
        "top": (120, 120, 140),
        "top_accent": (235, 238, 244),
        "bottom": (55, 65, 90),
        "shoes": (50, 45, 48),
        "cheek": (240, 180, 140),
        "brow": (40, 40, 46),
        "head_r": 0.15,
    },
]


def _scaled(cfg: dict, v: int) -> int:
    return round(v * cfg["height"] / REF_HEIGHT)


def _ow(cfg: dict, v: int = 3) -> int:
    return max(2, _scaled(cfg, v))


def draw_body(cfg: dict) -> tuple[Image.Image, tuple[int, int], int]:
    """Draw an outlined flat-cartoon character with face features.

    Returns (image, face_center, head_radius). The face keeps the mouth and
    eye regions blank — viseme/eye sprites overlay them at the anchors.
    """
    h = cfg["height"]
    w = int(h * 0.62)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    head_r = int(h * cfg["head_r"])
    face_cx, face_cy = w // 2, int(h * 0.27)
    ow = _ow(cfg)
    skin, shade = cfg["skin"], cfg["skin_shade"]

    torso_top = int(h * 0.46)
    torso_bottom = int(h * 0.82)
    torso_w = int(w * 0.48)
    leg_top = int(h * 0.78)
    foot_top = int(h * 0.95)

    # ---- legs + shoes ----
    leg_w = int(w * 0.16)
    for side in (-1, 1):
        lx = face_cx + side * int(w * 0.16) - leg_w // 2
        d.rounded_rectangle(
            [lx, leg_top, lx + leg_w, foot_top], radius=leg_w // 2,
            fill=cfg["bottom"], outline=OUTLINE, width=ow,
        )
        d.rounded_rectangle(
            [lx - 2, foot_top, lx + leg_w + 6, h - 4], radius=leg_w // 2,
            fill=cfg["shoes"], outline=OUTLINE, width=ow,
        )

    # ---- torso ----
    d.rounded_rectangle(
        [face_cx - torso_w // 2, torso_top, face_cx + torso_w // 2, torso_bottom],
        radius=int(h * 0.08), fill=cfg["top"], outline=OUTLINE, width=ow,
    )
    if cfg["id"] == "dad_v1":  # collar + buttons
        d.polygon(
            [
                (face_cx - 6, torso_top - 4), (face_cx + 6, torso_top - 4),
                (face_cx, torso_top + 16),
            ],
            fill=cfg["top_accent"], outline=OUTLINE, width=ow,
        )
        for i in range(3):
            d.ellipse(
                [face_cx - 3, torso_top + 26 + i * 22, face_cx + 3, torso_top + 32 + i * 22],
                fill=cfg["top_accent"], outline=OUTLINE, width=1,
            )
        d.rectangle([face_cx - 12, int(h * 0.71), face_cx + 12, int(h * 0.73)], fill=(60, 45, 40))
    elif cfg["id"] == "mom_v1":  # blouse v-neck + necklace
        d.polygon(
            [
                (face_cx - 14, torso_top), (face_cx + 14, torso_top),
                (face_cx, torso_top + 22),
            ],
            fill=cfg["top_accent"], outline=OUTLINE, width=ow,
        )
        d.ellipse([face_cx - 3, torso_top + 24, face_cx + 3, torso_top + 30], fill=(255, 214, 130))
    else:  # child tee stripe
        d.line(
            [face_cx - torso_w // 2 + 8, int(h * 0.55), face_cx + torso_w // 2 - 8, int(h * 0.55)],
            fill=cfg["top_accent"], width=ow,
        )

    # ---- arms (skin) + hands ----
    arm_w = int(w * 0.10)
    for side in (-1, 1):
        ax = face_cx + side * (torso_w // 2 - arm_w // 2) - arm_w // 2
        d.rounded_rectangle(
            [ax, torso_top + 6, ax + arm_w, torso_bottom - 4], radius=arm_w // 2,
            fill=skin, outline=OUTLINE, width=ow,
        )
        hx = ax + arm_w // 2 - int(w * 0.045) if side < 0 else ax + arm_w // 2 - int(w * 0.045)
        d.ellipse(
            [hx - int(w * 0.035), torso_bottom - 14, hx + int(w * 0.035), torso_bottom + 4],
            fill=skin, outline=OUTLINE, width=ow,
        )

    # ---- neck ----
    neck_w = int(h * 0.075)
    d.rectangle(
        [face_cx - neck_w // 2, int(h * 0.385), face_cx + neck_w // 2, int(h * 0.47)],
        fill=skin, outline=OUTLINE, width=ow,
    )

    # ---- head (jaw + ears) ----
    d.ellipse([face_cx - head_r, face_cy - head_r, face_cx + head_r, face_cy + head_r], fill=skin)
    d.ellipse(
        [face_cx - head_r - 4, face_cy - head_r, face_cx + head_r + 4, face_cy + head_r],
        fill=skin, outline=OUTLINE, width=ow,
    )
    ear_y = face_cy + int(head_r * 0.25)
    for side in (-1, 1):
        d.ellipse(
            [face_cx + side * head_r - 8, ear_y - 10, face_cx + side * head_r + 8, ear_y + 12],
            fill=skin, outline=OUTLINE, width=ow,
        )

    # ---- hair ----
    hair = cfg["hair"]
    style = cfg["hair_style"]
    if style == "pigtails":
        d.pieslice(
            [face_cx - head_r - 6, face_cy - head_r - 8, face_cx + head_r + 6, face_cy + head_r],
            180, 360, fill=hair, outline=OUTLINE, width=ow,
        )
        for bump in (-0.62, 0.0, 0.62):  # bangs
            d.ellipse(
                [
                    face_cx + int(head_r * bump) - 14, face_cy - head_r + 4,
                    face_cx + int(head_r * bump) + 14, face_cy - head_r + 34,
                ],
                fill=hair, outline=OUTLINE, width=ow,
            )
        for side in (-1, 1):
            d.ellipse(
                [
                    face_cx + side * (head_r + 6) - 24, face_cy - int(head_r * 0.35) - 18,
                    face_cx + side * (head_r + 6) + 24, face_cy + int(head_r * 0.45) + 6,
                ],
                fill=hair, outline=OUTLINE, width=ow,
            )
            d.ellipse(
                [
                    face_cx + side * (head_r + 10) - 12, face_cy + int(head_r * 0.18) - 10,
                    face_cx + side * (head_r + 10) + 12, face_cy + int(head_r * 0.18) + 14,
                ],
                fill=cfg["top_accent"], outline=OUTLINE, width=2,
            )
    elif style == "bob":
        d.pieslice(
            [face_cx - head_r - 6, face_cy - head_r - 10, face_cx + head_r + 6, face_cy + head_r],
            180, 360, fill=hair, outline=OUTLINE, width=ow,
        )
        for side in (-1, 1):
            d.rounded_rectangle(
                [
                    face_cx + side * head_r - 4, face_cy - head_r + 2,
                    face_cx + side * head_r + int(head_r * 0.55), face_cy + int(head_r * 0.62),
                ],
                radius=10, fill=hair, outline=OUTLINE, width=ow,
            )
        d.rectangle(
            [face_cx - head_r, face_cy - head_r - 12, face_cx + head_r, face_cy - head_r + 8],
            fill=hair, outline=OUTLINE, width=ow,
        )
    else:  # short
        d.pieslice(
            [face_cx - head_r - 6, face_cy - head_r - 8, face_cx + head_r + 6, face_cy + head_r],
            180, 360, fill=hair, outline=OUTLINE, width=ow,
        )
        for side in (-1, 1):
            d.rounded_rectangle(
                [
                    face_cx + side * head_r - 6, face_cy - 6,
                    face_cx + side * head_r + 10, face_cy + 24,
                ],
                radius=6, fill=hair, outline=OUTLINE, width=ow,
            )
        d.line(
            [face_cx - head_r + 8, face_cy - head_r + 14, face_cx + head_r - 8, face_cy - head_r + 14],
            fill=hair, width=ow,
        )

    # ---- face: brows, nose, cheeks ----
    brow_y = face_cy - int(head_r * 0.52)
    for side in (-1, 1):
        bx = face_cx + side * int(head_r * 0.52)
        d.line(
            [bx - 12, brow_y, bx + 12, brow_y],
            fill=cfg["brow"], width=ow,
        )
    d.arc(
        [face_cx - 8, face_cy - int(head_r * 0.05), face_cx + 8, face_cy + int(head_r * 0.25)],
        start=30, end=150, fill=OUTLINE, width=2,
    )
    for side in (-1, 1):
        d.ellipse(
            [
                face_cx + side * int(head_r * 0.52) - 8, face_cy + int(head_r * 0.16) - 7,
                face_cx + side * int(head_r * 0.52) + 8, face_cy + int(head_r * 0.16) + 7,
            ],
            fill=cfg["cheek"],
        )
    return img, (face_cx, face_cy), head_r


def draw_mouth(shape: str, scale: float) -> Image.Image:
    s = lambda v: round(v * scale)
    img = Image.new("RGBA", (s(MOUTH_W), s(MOUTH_H)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = img.width // 2, img.height // 2
    dark = (90, 30, 35)
    lip = (178, 82, 88)
    ow = max(2, s(4))

    def mouth(bbox, kind="ellipse", fill=None, extra=None):
        if kind == "ellipse":
            d.ellipse(bbox, outline=lip, width=ow, fill=fill or dark)
        elif kind == "rect":
            d.rounded_rectangle(bbox, radius=s(6), outline=lip, width=ow, fill=fill or dark)
        elif kind == "line":
            d.line(bbox, fill=lip, width=ow)

    if shape == "X":  # rest: small relaxed line
        mouth([cx - s(22), cy, cx + s(22), cy], "line")
    elif shape == "B":  # pressed closed
        mouth([cx - s(30), cy - 2, cx + s(30), cy + 2], "line", )
    elif shape == "C":  # small open (ee)
        mouth([cx - s(22), cy - s(10), cx + s(22), cy + s(10)])
    elif shape == "D":  # medium open (ai)
        mouth([cx - s(34), cy - s(18), cx + s(34), cy + s(18)])
    elif shape == "E":  # tall open (oh)
        mouth([cx - s(30), cy - s(28), cx + s(30), cy + s(28)])
    elif shape == "F":  # wide flat (fv)
        mouth([cx - s(46), cy - s(12), cx + s(46), cy + s(12)])
    elif shape == "G":  # th: rect + tongue
        mouth([cx - s(30), cy - s(14), cx + s(30), cy + s(16)], "rect")
        d.rounded_rectangle(
            [cx - s(12), cy + s(8), cx + s(12), cy + s(26)], radius=s(5),
            fill=(232, 128, 112), outline=lip, width=2,
        )
    elif shape == "H":  # oo: circle
        mouth([cx - s(24), cy - s(26), cx + s(24), cy + s(26)])
    elif shape == "A":  # wide open (ah)
        mouth([cx - s(40), cy - s(24), cx + s(40), cy + s(24)])
    else:
        raise ValueError(f"unknown viseme {shape}")
    return img


def draw_eyes(open_: bool, scale: float) -> Image.Image:
    s = lambda v: round(v * scale)
    img = Image.new("RGBA", (s(EYE_W), s(EYE_H)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cy = img.height // 2
    ow = max(2, s(4))
    for side in (-1, 1):
        x = img.width // 2 + side * s(52)
        if open_:
            d.ellipse([x - s(26), cy - s(20), x + s(26), cy + s(20)], fill=(255, 255, 255, 255), outline=OUTLINE, width=2)
            d.ellipse([x - s(12), cy - s(13), x + s(12), cy + s(13)], fill=(52, 48, 62))
            d.ellipse([x - s(5), cy - s(7), x + s(5), cy + s(7)], fill=(255, 255, 255))
        else:
            d.arc(
                [x - s(26), cy - s(22), x + s(26), cy + s(16)],
                start=190, end=350, fill=OUTLINE, width=ow,
            )
    return img


def draw_background() -> Image.Image:
    img = Image.new("RGB", (BG_W, BG_H), (236, 222, 205))
    d = ImageDraw.Draw(img)
    wall_bottom = int(BG_H * 0.72)
    floor_top = wall_bottom

    # wall (two-tone) + baseboard
    d.rectangle([0, 0, BG_W, int(BG_H * 0.60)], fill=(243, 232, 218))
    d.rectangle([0, int(BG_H * 0.60), BG_W, wall_bottom], fill=(228, 214, 196))
    d.rectangle([0, wall_bottom - 6, BG_W, wall_bottom + 22], fill=(150, 118, 84))
    d.line([0, wall_bottom + 8, BG_W, wall_bottom + 8], fill=(120, 92, 64), width=3)

    # floor + planks
    d.rectangle([0, floor_top, BG_W, BG_H], fill=(176, 130, 88))
    for i in range(0, BG_W, 140):
        d.line([i, floor_top, i - 40, BG_H], fill=(156, 112, 74), width=2)
    d.line([0, floor_top + 70, BG_W, floor_top + 70], fill=(156, 112, 74), width=2)

    # rug with border
    d.ellipse([BG_W // 2 - 460, floor_top + 34, BG_W // 2 + 460, BG_H + 90], fill=(203, 95, 95))
    d.ellipse([BG_W // 2 - 440, floor_top + 50, BG_W // 2 + 440, BG_H + 74], fill=(222, 130, 118))
    d.ellipse([BG_W // 2 - 300, floor_top + 120, BG_W // 2 + 300, BG_H + 20], fill=(203, 95, 95))

    # window with curtains + sill
    d.rectangle([150, 120, 560, 470], fill=(150, 198, 232), outline=(120, 92, 64), width=10)
    d.line([355, 120, 355, 470], fill=(120, 92, 64), width=8)
    d.line([150, 295, 560, 295], fill=(120, 92, 64), width=8)
    d.ellipse([220, 160, 290, 230], fill=(255, 222, 110))
    for cx in (170, 560):
        d.rounded_rectangle([cx - 26, 96, cx + 26, 470], radius=18, fill=(120, 165, 175), outline=(90, 128, 138), width=5)
    d.rectangle([140, 470, 570, 508], fill=(150, 118, 84), outline=(120, 92, 64), width=6)
    # sky + clouds in window
    d.ellipse([400, 190, 480, 230], fill=(255, 255, 255))
    d.ellipse([440, 175, 520, 215], fill=(255, 255, 255))

    # sofa with cushions
    d.rounded_rectangle([1240, 640, 1790, 890], radius=34, fill=(108, 138, 168), outline=(70, 95, 122), width=8)
    d.rounded_rectangle([1210, 590, 1820, 700], radius=28, fill=(120, 150, 185), outline=(70, 95, 122), width=8)
    d.rectangle([1225, 650, 1820, 705], fill=(108, 138, 168))
    for cx in (1340, 1500, 1660):
        d.rounded_rectangle([cx - 60, 650, cx + 60, 780], radius=20, fill=(150, 178, 208), outline=(70, 95, 122), width=6)
        d.line([cx - 60, 700, cx + 60, 700], fill=(120, 150, 185), width=4)
    # cushions' bottoms
    d.rounded_rectangle([1240, 780, 1790, 890], radius=30, fill=(108, 138, 168), outline=(70, 95, 122), width=8)
    # sofa legs
    for lx in (1260, 1770):
        d.rounded_rectangle([lx, 880, lx + 22, 930], radius=8, fill=(70, 95, 122))
    # floor lamp
    d.line([1130, 560, 1130, 810], fill=(90, 80, 70), width=10)
    d.polygon([(1080, 560), (1180, 560), (1190, 500), (1070, 500)], fill=(250, 214, 130), outline=(90, 80, 70))
    d.ellipse([1118, 800, 1142, 826], fill=(90, 80, 70))

    # plant in corner
    d.polygon([(90, 900), (200, 900), (170, 830), (120, 830)], fill=(160, 92, 64), outline=(110, 62, 44))
    for leaf in [(60, 700), (160, 660), (230, 720), (110, 640), (190, 600)]:
        d.ellipse([leaf[0] - 40, leaf[1] - 22, leaf[0] + 40, leaf[1] + 34], fill=(96, 150, 96), outline=(64, 110, 70), width=4)

    # picture frames + clock
    for fx, fy in [(1620, 150), (1620, 390)]:
        d.rectangle([fx, fy, fx + 200, fy + 150], fill=(250, 240, 220), outline=(120, 92, 64), width=9)
        d.ellipse([fx + 40, fy + 40, fx + 160, fy + 120], fill=(222, 190, 110), outline=(190, 160, 90), width=4)
    d.ellipse([320, 620, 420, 720], fill=(250, 244, 230), outline=(120, 92, 64), width=8)
    d.line([370, 620, 370, 670], fill=(60, 60, 70), width=5)
    d.line([370, 670, 395, 660], fill=(60, 60, 70), width=5)
    return img


def write_character(cfg: dict) -> None:
    out_dir = SPRITES_DIR / cfg["id"]
    out_dir.mkdir(parents=True, exist_ok=True)

    body, (face_cx, face_cy), head_r = draw_body(cfg)
    body.save(out_dir / "body.png")

    mouth_anchor = {"x": face_cx, "y": face_cy + int(head_r * 0.52)}
    eye_anchor = {"x": face_cx, "y": face_cy - int(head_r * 0.05)}
    scale = cfg["height"] / REF_HEIGHT

    visemes = {}
    for shape in "ABCDEFGHX":
        name = f"mouth_{shape}.png"
        draw_mouth(shape, scale).save(out_dir / name)
        visemes[shape] = name

    draw_eyes(True, scale).save(out_dir / "eyes_open.png")
    draw_eyes(False, scale).save(out_dir / "eyes_closed.png")

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
