"""Post-render compositor: word icons + optional intro card onto rendered frames.

Reads a Blender frame sequence (any numbering, gaps OK), overlays the active
word icon per ``icons.json`` on every frame (bottom-center on a dark pill, with
the same pop-in/fade envelope as the 2D renderer), optionally prepends an
``--intro`` card, and writes a contiguous ``frame_%06d.jpg`` sequence.

Usage:
    python -m app.pipeline.overlay_icons jobs/golden/full_room_lq \
        -o jobs/golden/full_room_lq_fx \
        --icons jobs/golden/icons.json --fps 8 --intro 2 \
        --intro-title "A Family Conversation" \
        --intro-characters "Ymen - Mother - Father - Hazem"
"""

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ICONS = ROOT / "jobs" / "golden" / "icons.json"
ICON_DIR = ROOT / "app" / "assets" / "icons"
ICON_HEIGHT_FRAC = 0.08   # icon height as a fraction of canvas height
ICON_Y_FRAC = 0.90        # icon center y as a fraction of canvas height
ICON_POP = 0.20
ICON_FADE = 0.30
PILL_ALPHA = 130

FONT_BOLD = Path(r"C:\Windows\Fonts\arialbd.ttf")
FONT_REG = Path(r"C:\Windows\Fonts\arial.ttf")


def frame_key(name: str) -> int:
    return int(Path(name).stem.split("_")[-1])


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = FONT_BOLD if bold else FONT_REG
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def active_icon(icons: list[dict], t: float) -> dict | None:
    active = [e for e in icons if e["start"] <= t <= e["end"]]
    if not active:
        return None
    return max(active, key=lambda e: (e["end"] - e["start"], e["start"]))


def icon_envelope(t: float, ev: dict) -> tuple[float, float]:
    start, end = ev["start"], ev["end"]
    dur = end - start
    pop = min(ICON_POP, dur * 0.4)
    fade = min(ICON_FADE, dur * 0.4)
    if t <= start:
        return 0.0, 0.3
    if t < start + pop:
        x = (t - start) / pop
        ease = 1 - (1 - x) ** 3
        return ease, 0.3 + 0.7 * ease
    if t < end - fade:
        return 1.0, 1.0
    x = min(max((t - (end - fade)) / fade, 0.0), 1.0)
    return 1.0 - x, 1.0 - 0.1 * x


def rounded_pill(w: int, h: int, radius: int, alpha: int) -> Image.Image:
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(pill)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=(0, 0, 0, alpha))
    return pill


def overlay_icon(frame: Image.Image, icons: list[dict], t: float, cache: dict,
                 use_pill: bool) -> None:
    ev = active_icon(icons, t)
    if ev is None:
        return
    word = ev["word"]
    if word not in cache:
        p = ICON_DIR / f"{word}.png"
        cache[word] = Image.open(p).convert("RGBA") if p.exists() else None
    img = cache[word]
    if img is None:
        return
    alpha, scale = icon_envelope(t, ev)
    if alpha <= 0.01:
        return
    target_h = max(1, round(frame.height * ICON_HEIGHT_FRAC))
    w = max(1, round(img.width * (target_h / img.height) * scale))
    h = max(1, round(target_h * scale))
    icon = img if (w, h) == img.size else img.resize((w, h), Image.LANCZOS)
    if alpha < 1.0:
        icon = icon.copy()
        icon.putalpha(icon.getchannel("A").point(lambda p: round(p * alpha)))

    if use_pill:
        pad = max(8, round(w * 0.12))
        pw, ph = w + pad * 2, h + pad * 2
        layer = rounded_pill(pw, ph, max(4, round(ph * 0.22)), PILL_ALPHA)
    else:
        pad = 0
        pw, ph = w, h
        layer = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    layer.alpha_composite(icon, (pad, pad))
    x = round(frame.width / 2 - pw / 2)
    y = round(frame.height * ICON_Y_FRAC - ph / 2)
    frame.paste(layer, (x, y), layer)


def build_intro(base: Image.Image, fps: int, seconds: float, title: str,
                characters: str) -> list[Image.Image]:
    bg = base.convert("RGB").copy()
    bg = Image.eval(bg, lambda p: round(p * 0.35))
    w, h = bg.size
    draw = ImageDraw.Draw(bg)

    t_size = max(24, round(h * 0.075))
    c_size = max(16, round(h * 0.038))
    t_font = load_font(t_size, bold=True)
    c_font = load_font(c_size)
    y0 = round(h * 0.30)
    for i, txt in enumerate([title]):
        bb = draw.textbbox((0, 0), txt, font=t_font)
        draw.text((round((w - (bb[2] - bb[0])) / 2), y0), txt, fill=(255, 255, 255), font=t_font)
        y0 += bb[3] - bb[1] + round(h * 0.03)
    bb = draw.textbbox((0, 0), characters, font=c_font)
    draw.text((round((w - (bb[2] - bb[0])) / 2), y0), characters,
              fill=(220, 220, 220), font=c_font)

    n = max(1, round(seconds * fps))
    return [bg.copy() for _ in range(n)]


def main(argv: list[str] | None = None) -> int:
    global ICON_HEIGHT_FRAC
    parser = argparse.ArgumentParser(description="icon overlay + intro card compositor")
    parser.add_argument("frames", help="input frames dir (any numbering)")
    parser.add_argument("-o", "--output", required=True, help="output contiguous frames dir")
    parser.add_argument("--icons", default=str(DEFAULT_ICONS), help="icons.json")
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--intro", type=float, default=0.0, help="seconds of intro card")
    parser.add_argument("--intro-title", default="A Family Conversation")
    parser.add_argument("--intro-characters", default="Ymen - Mother - Father - Hazem")
    parser.add_argument("--no-pill", action="store_true", help="skip the dark backing pill")
    parser.add_argument("--no-icons", action="store_true", help="only intro, no word icons")
    parser.add_argument("--icon-size", type=float, default=ICON_HEIGHT_FRAC,
                        help="icon height as a fraction of canvas height")
    args = parser.parse_args(argv)

    ICON_HEIGHT_FRAC = args.icon_size

    src = Path(args.frames)
    dst = Path(args.output)
    dst.mkdir(parents=True, exist_ok=True)
    inputs = sorted(src.glob("frame_*.jpg"), key=lambda p: frame_key(p.name))
    if not inputs:
        print(f"[overlay_icons] no frames in {src}", file=sys.stderr)
        return 1

    icons = json.loads(Path(args.icons).read_text(encoding="utf-8"))
    cache: dict[str, Image.Image | None] = {}
    duration = max((e["end"] for e in icons), default=0.0)

    n = 0
    if args.intro > 0:
        base = Image.open(inputs[0]).convert("RGB")
        for card in build_intro(base, args.fps, args.intro, args.intro_title, args.intro_characters):
            card.save(dst / f"frame_{n + 1:06d}.jpg", quality=92)
            n += 1

    for src_path in inputs:
        frame = Image.open(src_path).convert("RGB")
        if not args.no_icons:
            t = (frame_key(src_path.name) - 1) / 24.0
            overlay_icon(frame, icons, t, cache, not args.no_pill)
        frame.save(dst / f"frame_{n + 1:06d}.jpg", quality=92)
        n += 1

    print(f"[overlay_icons] wrote {n} frames ({args.intro}s intro, "
          f"icons={'off' if args.no_icons else 'on'}) -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())