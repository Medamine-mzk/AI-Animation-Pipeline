"""M4: frame renderer — background + characters + lip-sync + idle motion + camera.

Composites one scene into a sequence of JPEG frames:
- viseme-driven mouths per character (from lipsync output)
- randomized blink cycles (every 3-6s, ~150ms closed) and breathing sway
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
ZOOM = {"wide": 1.0, "medium": 1.7, "closeup": 2.5}
HEAD_FRACTION = 0.22
SWAY_AMP = 2.0
SWAY_FREQ = 0.3
BLINK_MIN, BLINK_MAX = 3.0, 6.0
BLINK_MS = 0.15


@dataclass
class CharacterLayout:
    character_id: str
    sprite_id: str
    x: int
    top: int
    height: int
    tint_deg: int


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


def layout_characters(chars: list[dict], canvas_w: int = 1920, floor_y: int = 810) -> dict[str, CharacterLayout]:
    """Stand positions: tallest in back, spread left/right, feet on the floor."""
    sized = []
    for char in chars:
        with Image.open(char["paths"]["body"]) as img:
            height = img.height
        sized.append((char, height))
    sized.sort(key=lambda item: item[1], reverse=True)
    layout = {}
    for i, (char, height) in enumerate(sized):
        x = round(canvas_w * SPOT_FRACTIONS[i % len(SPOT_FRACTIONS)])
        layout[char["character_id"]] = CharacterLayout(
            character_id=char["character_id"],
            sprite_id=char["sprite_id"],
            x=x,
            top=floor_y - height,
            height=height,
            tint_deg=0,
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
    ) -> None:
        self.assets = assets
        self.visemes = visemes
        self.shots = shots
        self.canvas_w, self.canvas_h = canvas
        self.seed = seed
        self.duration = duration
        self._cache: dict[tuple[str, int], Image.Image] = {}

        self.background = Image.open(assets["background"]).convert("RGB")
        self.layout = layout_characters(assets["characters"], self.canvas_w, floor_y)
        sprite_ids = [c["sprite_id"] for c in assets["characters"]]
        tints = assign_tints(sprite_ids)
        for char, tint in zip(assets["characters"], tints):
            self.layout[char["character_id"]].tint_deg = tint
        self.blinks = build_blink_windows(duration, seed, len(assets["characters"]))
        self.sway_phases = [(seed * 0.13 + i * 0.37) % 1.0 for i in range(len(assets["characters"]))]
        self._preload()

    @staticmethod
    def _hue_shift(img: Image.Image, deg: int) -> Image.Image:
        if deg == 0:
            return img
        h, s, v = img.convert("HSV").split()
        shift = round(deg * 255 / 360)
        h = h.point(lambda p: (p + shift) % 256)
        return Image.merge("HSV", (h, s, v)).convert("RGBA")

    def _load(self, path: str, tint_deg: int) -> Image.Image:
        key = (path, tint_deg)
        if key not in self._cache:
            img = Image.open(path).convert("RGBA")
            self._cache[key] = self._hue_shift(img, tint_deg)
        return self._cache[key]

    def _preload(self) -> None:
        for char in self.assets["characters"]:
            tint = self.layout[char["character_id"]].tint_deg
            for key, path in char["paths"].items():
                self._load(path, tint)
        if self.background.size != (self.canvas_w, self.canvas_h):
            self.background = self.background.resize((self.canvas_w, self.canvas_h), Image.LANCZOS)

    def _visemes_for(self, char: dict) -> list[dict]:
        return self.visemes.get(char.get("speaker_ref") or char["character_id"], [])

    def _focus_point(self, char_id: str) -> tuple[int, int]:
        layout = self.layout[char_id]
        return (layout.x, round(layout.top + layout.height * HEAD_FRACTION))

    def render(self, t: float) -> Image.Image:
        frame = self.background.copy()
        for i, char in enumerate(self.assets["characters"]):
            layout = self.layout[char["character_id"]]
            manifest = char["manifest"]
            sway = round(SWAY_AMP * math.sin(2 * math.pi * SWAY_FREQ * (t + self.sway_phases[i])))
            x_left = layout.x - self._cache[(char["paths"]["body"], layout.tint_deg)].width // 2
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
    args = parser.parse_args(argv)

    try:
        script = SceneScript.model_validate_json(Path(args.script).read_text(encoding="utf-8"))
        assets = ResolvedAssets.model_validate_json(Path(args.assets).read_text(encoding="utf-8"))
        visemes = json.loads(Path(args.visemes).read_text(encoding="utf-8"))
        duration = max((ev["end"] for evs in visemes.values() for ev in evs), default=0.0) + 1.0
        shots = [s.model_dump() for s in script.camera]

        print(f"[animate] {len(assets.characters)} character(s), {len(shots)} shot(s), {duration:.1f}s")
        renderer = SceneRenderer(
            assets=assets.model_dump(),
            visemes=visemes,
            shots=shots,
            floor_y=args.floor_y,
            seed=args.seed,
            duration=duration,
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
