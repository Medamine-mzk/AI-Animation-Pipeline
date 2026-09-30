"""M4: frame renderer — background + characters + lip-sync + idle motion + camera.

Composites one scene into a sequence of JPEG frames:
- composite mode: viseme-driven mouths per character (from lipsync output) pasted
  onto a body sprite, with randomized blink cycles and breathing sway
- plates mode (Live2D): whole-body plates are swapped per viseme+blink
  ({viseme}_{open|closed}.png), pasted at a per-character scale
- camera cuts to the active speaker per script.camera (wide/medium/closeup)
- duplicate sprite designs get an automatic hue tint so reused art still reads
  as distinct characters

Usage:
    python -m app.pipeline.animate --visemes jobs/golden/visemes.json -o jobs/golden/frames
"""

import argparse
import json
import math
import random
import sys
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.pipeline.resolve_assets import ResolvedAssets
from app.schemas.script import SceneScript

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VISEMES = ROOT / "jobs" / "golden" / "visemes.json"
DEFAULT_SCRIPT = ROOT / "jobs" / "golden" / "script.json"
DEFAULT_ASSETS = ROOT / "jobs" / "golden" / "assets.json"
DEFAULT_FRAMES = ROOT / "jobs" / "golden" / "frames"

SPOT_FRACTIONS = (0.30, 0.70, 0.50, 0.15, 0.85, 0.35, 0.65, 0.22, 0.78)
ZOOM = {"wide": 1.0, "medium": 1.2, "closeup": 1.5}
HEAD_FRACTION = 0.22
SWAY_AMP = 2.0
SWAY_FREQ = 0.3
BLINK_MIN, BLINK_MAX = 3.0, 6.0
BLINK_MS = 0.15
PLATE_TARGET_HEIGHT = 0.6  # plate content height as a fraction of canvas height
ICON_HEIGHT = 200          # icon height in px on the final canvas
ICON_Y = 950               # icon center y on the final canvas (bottom band)
ICON_POP = 0.20            # seconds to scale/ease pop-in
ICON_FADE = 0.30           # seconds to fade out at the end of the word
DEFAULT_ICONS = ROOT / "app" / "assets" / "icons"


@dataclass
class CharacterLayout:
    character_id: str
    sprite_id: str
    x: int
    top: int
    height: int
    tint_deg: int
    scale: float = 1.0  # plates mode: uniform scale applied to whole plate


def viseme_at(events: list[dict], t: float) -> str:
    """Viseme shape active at t; X (mouth closed) outside all events."""
    starts = [ev["start"] for ev in events]
    i = bisect_right(starts, t) - 1
    if i < 0:
        return "X"
    ev = events[i]
    return ev["shape"] if t < ev["end"] else "X"


def shot_at(shots: list[dict], t: float) -> dict:
    """Camera shot active at t; stays on the just-ended shot across gaps."""
    if not shots:
        raise ValueError("no camera shots")
    starts = [s["start"] for s in shots]
    i = bisect_right(starts, t) - 1
    return shots[max(i, 0)]


def build_blink_windows(duration: float, seed: int, count: int) -> list[list[tuple[float, float]]]:
    """Per-character (start, end) blink windows covering [0, duration]."""
    windows = []
    for i in range(count):
        rng = random.Random(seed * 1000 + i * 7919)
        t = rng.uniform(0.5, 2.5)
        char_windows = []
        while t < duration:
            end = t + BLINK_MS
            char_windows.append((t, end))
            t = end + rng.uniform(BLINK_MIN, BLINK_MAX)
        windows.append(char_windows)
    return windows


def _is_blinking(windows: list[tuple[float, float]], t: float) -> bool:
    return any(start <= t <= end for start, end in windows)


def assign_tints(sprite_ids: list[str]) -> list[int]:
    """Hue offset (degrees) per character; 0 for first use, +40 per reuse."""
    seen: dict[str, int] = {}
    tints = []
    for sprite_id in sprite_ids:
        use = seen.get(sprite_id, 0)
        seen[sprite_id] = use + 1
        tints.append(0 if use == 0 else min(use * 40, 200))
    return tints


def _alpha_bbox(path: str) -> tuple[int, int, int, int]:
    with Image.open(path) as img:
        a = img.convert("RGBA").getchannel("A")
        return a.getbbox()


def layout_characters(chars: list[dict], canvas_w: int = 1920, canvas_h: int = 1080, floor_y: int = 810) -> dict[str, CharacterLayout]:
    """Stand positions: tallest in back, spread left/right, feet on the floor.

    Plate-mode characters are scaled so their opaque content reaches ~85% of
    the canvas height and stands on the floor line.
    """
    sized = []
    for char in chars:
        if char.get("render_mode") == "plates":
            content = _alpha_bbox(char["plates"]["X_open"])
            content_h = content[3] - content[1]
            target_h = round(canvas_h * PLATE_TARGET_HEIGHT)
            scale = target_h / content_h if content_h else 1.0
            height = round(target_h)
            sized.append((char, height, scale))
        else:
            with Image.open(char["paths"]["body"]) as img:
                height = img.height
            sized.append((char, height, 1.0))
    sized.sort(key=lambda item: item[1], reverse=True)
    layout = {}
    for i, (char, height, scale) in enumerate(sized):
        x = round(canvas_w * SPOT_FRACTIONS[i % len(SPOT_FRACTIONS)])
        layout[char["character_id"]] = CharacterLayout(
            character_id=char["character_id"],
            sprite_id=char["sprite_id"],
            x=x,
            top=floor_y - height,
            height=height,
            tint_deg=0,
            scale=scale,
        )
    return layout


class SceneRenderer:
    def __init__(
        self,
        assets: dict,
        visemes: dict[str, list[dict]],
        shots: list[dict],
        canvas: tuple[int, int] = (1920, 1080),
        floor_y: int = 810,
        seed: int = 42,
        duration: float = 60.0,
        icons: list[dict] | None = None,
        icon_dir: str | Path = DEFAULT_ICONS,
        override_dir: str | Path | None = None,
    ) -> None:
        self.assets = assets
        self.visemes = visemes
        self.shots = shots
        self.canvas_w, self.canvas_h = canvas
        self.seed = seed
        self.duration = duration
        self._cache: dict[tuple[str, int], Image.Image] = {}
        self.icons = sorted(icons or [], key=lambda e: (e["start"], e["end"]))
        self.icon_dir = Path(icon_dir)
        self.override_dir = Path(override_dir) if override_dir else None
        self._icon_cache: dict[str, Image.Image | None] = {}

        self.background = Image.open(assets["background"]).convert("RGB")
        self.layout = layout_characters(
            assets["characters"], self.canvas_w, self.canvas_h, floor_y
        )
        sprite_ids = [c["sprite_id"] for c in assets["characters"]]
        tints = assign_tints(sprite_ids)
        for char, tint in zip(assets["characters"], tints):
            self.layout[char["character_id"]].tint_deg = tint
        self.blinks = build_blink_windows(duration, seed, len(assets["characters"]))
        self.sway_phases = [(seed * 0.13 + i * 0.37) % 1.0 for i in range(len(assets["characters"]))]
        self._plate_offsets: dict[str, tuple[int, int]] = {}
        self._preload()

    @staticmethod
    def _hue_shift(img: Image.Image, deg: int) -> Image.Image:
        if deg == 0:
            return img
        h, s, v = img.convert("HSV").split()
        shift = round(deg * 255 / 360)
        h = h.point(lambda p: (p + shift) % 256)
        return Image.merge("HSV", (h, s, v)).convert("RGBA")

    def _load(self, path: str, tint_deg: int, scale: float = 1.0) -> Image.Image:
        key = (path, tint_deg, round(scale, 4))
        if key not in self._cache:
            img = Image.open(path).convert("RGBA")
            if scale != 1.0:
                img = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                    Image.LANCZOS,
                )
            self._cache[key] = self._hue_shift(img, tint_deg)
        return self._cache[key]

    def _preload(self) -> None:
        for char in self.assets["characters"]:
            layout = self.layout[char["character_id"]]
            tint = layout.tint_deg
            if char.get("render_mode") == "plates":
                for key, path in char["plates"].items():
                    self._load(path, tint, layout.scale)
                    if key == "X_open":
                        content = _alpha_bbox(path)
                        self._plate_offsets[char["character_id"]] = (
                            content[0],
                            content[1],
                            content[2] - content[0],
                            content[3] - content[1],
                        )
            else:
                for key, path in char["paths"].items():
                    self._load(path, tint)
        if self.background.size != (self.canvas_w, self.canvas_h):
            self.background = self.background.resize((self.canvas_w, self.canvas_h), Image.LANCZOS)

    def _visemes_for(self, char: dict) -> list[dict]:
        return self.visemes.get(char.get("speaker_ref") or char["character_id"], [])

    def _paste_plate(self, frame: Image.Image, char: dict, layout: CharacterLayout, t: float, sway: int) -> None:
        i = next(
            (i for i, c in enumerate(self.assets["characters"]) if c["character_id"] == char["character_id"]),
            0,
        )
        shape = viseme_at(self._visemes_for(char), t)
        blink_key = "closed" if _is_blinking(self.blinks[i], t) else "open"
        plate = self._load(char["plates"][f"{shape}_{blink_key}"], layout.tint_deg, layout.scale)
        x0, y0, w, h = self._plate_offsets[char["character_id"]]
        sc = layout.scale
        content_left = layout.x - (w * sc) / 2
        content_bottom = layout.top + layout.height + sway
        content_top = content_bottom - (h * sc)
        frame.paste(
            plate,
            (round(content_left - x0 * sc), round(content_top - y0 * sc)),
            plate,
        )

    def _focus_point(self, char_id: str) -> tuple[int, int]:
        layout = self.layout[char_id]
        return (layout.x, round(layout.top + layout.height * HEAD_FRACTION))

    def active_icon(self, t: float) -> dict | None:
        """Longest icon event active at t (gives priority on overlap)."""
        active = [e for e in self.icons if e["start"] <= t <= e["end"]]
        if not active:
            return None
        return max(active, key=lambda e: (e["end"] - e["start"], e["start"]))

    @staticmethod
    def _icon_envelope(t: float, ev: dict) -> tuple[float, float]:
        """(alpha, scale) for an icon at time t: pop-in -> hold -> fade-out.

        Pop/fade are clamped to the event duration so very short words still
        reach full size at the midpoint.
        """
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

    def _load_icon(self, word: str) -> Image.Image | None:
        if word in self._icon_cache:
            return self._icon_cache[word]
        path = None
        if self.override_dir is not None:
            candidate = self.override_dir / f"{word}.png"
            if candidate.exists():
                path = candidate
        if path is None:
            candidate = self.icon_dir / f"{word}.png"
            if candidate.exists():
                path = candidate
        if path is None:
            self._icon_cache[word] = None
            return None
        img = Image.open(path).convert("RGBA")
        if img.height != ICON_HEIGHT:
            img = img.resize(
                (max(1, round(img.width * ICON_HEIGHT / img.height)), ICON_HEIGHT),
                Image.LANCZOS,
            )
        self._icon_cache[word] = img
        return img

    def _paste_icon(self, frame: Image.Image, t: float) -> None:
        ev = self.active_icon(t)
        if ev is None:
            return
        img = self._load_icon(ev["word"])
        if img is None:
            return
        alpha, scale = self._icon_envelope(t, ev)
        if alpha <= 0.01:
            return
        w, h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
        icon = img if scale == 1.0 else img.resize((w, h), Image.LANCZOS)
        if alpha < 1.0:
            icon = icon.copy()
            icon.putalpha(icon.getchannel("A").point(lambda p: round(p * alpha)))
        frame.paste(icon, (round(self.canvas_w / 2 - w / 2), round(ICON_Y - h / 2)), icon)

    def render(self, t: float) -> Image.Image:
        frame = self.background.copy()
        for i, char in enumerate(self.assets["characters"]):
            layout = self.layout[char["character_id"]]
            sway = round(SWAY_AMP * math.sin(2 * math.pi * SWAY_FREQ * (t + self.sway_phases[i])))

            if char.get("render_mode") == "plates":
                self._paste_plate(frame, char, layout, t, sway)
                continue

            manifest = char["manifest"]
            x_left = layout.x - self._cache[(char["paths"]["body"], layout.tint_deg, 1.0)].width // 2
            y_top = layout.top + sway

            body = self._load(char["paths"]["body"], layout.tint_deg)
            frame.paste(body, (x_left, y_top), body)

            blink_key = "eyes_closed" if _is_blinking(self.blinks[i], t) else "eyes_open"
            eyes = self._load(char["paths"][blink_key], layout.tint_deg)
            anchor = manifest.get("eye_anchor") or manifest["anchor_point"]
            frame.paste(
                eyes,
                (x_left + anchor["x"] - eyes.width // 2, y_top + anchor["y"] - eyes.height // 2),
                eyes,
            )

            shape = viseme_at(self._visemes_for(char), t)
            mouth = self._load(char["paths"][f"viseme_{shape}"], layout.tint_deg)
            anchor = manifest["anchor_point"]
            frame.paste(
                mouth,
                (x_left + anchor["x"] - mouth.width // 2, y_top + anchor["y"] - mouth.height // 2),
                mouth,
            )

        shot = shot_at(self.shots, t)
        zoom = ZOOM[shot["shot"]]
        if zoom > 1.0:
            cx, cy = self._focus_point(shot["focus_character"])
            w, h = round(self.canvas_w / zoom), round(self.canvas_h / zoom)
            left = min(max(cx - w // 2, 0), self.canvas_w - w)
            top = min(max(cy - h // 2, 0), self.canvas_h - h)
            frame = frame.crop((left, top, left + w, top + h)).resize(
                (self.canvas_w, self.canvas_h), Image.LANCZOS
            )
        self._paste_icon(frame, t)
        return frame

    def render_scene(
        self,
        out_dir: Path,
        fps: int = 24,
        seconds: float | None = None,
        start: float = 0.0,
        on_progress=None,
    ) -> list[Path]:
        seconds = self.duration - start if seconds is None else min(seconds, self.duration - start)
        out_dir.mkdir(parents=True, exist_ok=True)
        total = max(1, round(seconds * fps))
        paths = []
        last_report = -1
        for f in range(total):
            t = start + f / fps
            frame = self.render(t)
            path = out_dir / f"frame_{f:06d}.jpg"
            frame.save(path, quality=92)
            paths.append(path)
            pct = (f + 1) * 100 // total
            if pct >= last_report + 5:
                last_report = pct
                if on_progress:
                    on_progress((f + 1) / fps)
                print(f"[animate] {pct}% ({f + 1}/{total} frames, t={t:.2f}s)")
        return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render scene frames with lip-sync + idle motion")
    parser.add_argument("--script", default=str(DEFAULT_SCRIPT), help="input script.json")
    parser.add_argument("--visemes", default=str(DEFAULT_VISEMES), help="input visemes.json")
    parser.add_argument("--assets", default=str(DEFAULT_ASSETS), help="input assets.json")
    parser.add_argument("-o", "--output", default=str(DEFAULT_FRAMES), help="output frames dir")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seconds", type=float, default=None, help="clip length (default: full)")
    parser.add_argument("--start", type=float, default=0.0, help="start offset in seconds")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--floor-y", type=int, default=810, help="feet floor line in px")
    parser.add_argument("--icons", default=None, help="input icons.json (word-icon timeline)")
    parser.add_argument("--override", default=None, help="dir of per-word PNG overrides (icon_override/)")
    args = parser.parse_args(argv)

    try:
        script = SceneScript.model_validate_json(Path(args.script).read_text(encoding="utf-8"))
        assets = ResolvedAssets.model_validate_json(Path(args.assets).read_text(encoding="utf-8"))
        visemes = json.loads(Path(args.visemes).read_text(encoding="utf-8"))
        duration = max((ev["end"] for evs in visemes.values() for ev in evs), default=0.0) + 1.0
        shots = [s.model_dump() for s in script.camera]

        print(f"[animate] {len(assets.characters)} character(s), {len(shots)} shot(s), {duration:.1f}s")
        icons = None
        if args.icons:
            icons = json.loads(Path(args.icons).read_text(encoding="utf-8"))
            print(f"[animate] {len(icons)} icon event(s)")
        renderer = SceneRenderer(
            assets=assets.model_dump(),
            visemes=visemes,
            shots=shots,
            floor_y=args.floor_y,
            seed=args.seed,
            duration=duration,
            icons=icons,
            override_dir=args.override,
        )
        paths = renderer.render_scene(
            Path(args.output), fps=args.fps, seconds=args.seconds, start=args.start
        )
        print(f"[animate] wrote {len(paths)} frames to {args.output}")
        return 0
    except Exception as exc:
        print(f"[animate] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
