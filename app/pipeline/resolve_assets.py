"""M3: asset resolution — script.json -> concrete sprite/background files.

Picks sprites from a fixed library by character role (spec 2: curated library,
not generative) and backgrounds by setting. Writes assets.json.

Usage:
    python -m app.pipeline.resolve_assets jobs/golden/script.json -o jobs/golden/assets.json
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas.script import SceneScript
from app.schemas.sprite_manifest import (
    BackgroundLibrary,
    SpriteLibrary,
    SpriteManifest,
)

ROOT = Path(__file__).resolve().parents[2]
SPRITES_DIR = ROOT / "app" / "assets" / "sprites"
BACKGROUNDS_DIR = ROOT / "app" / "assets" / "backgrounds"
DEFAULT_OUT = ROOT / "jobs" / "golden" / "assets.json"

ROLE_KEYWORDS = {
    "dad_v1": ("father", "dad", "papa", "daddy", "man"),
    "mom_v1": ("mother", "mom", "mum", "mama", "mommy", "woman"),
    "child_v1": ("child", "kid", "daughter", "son", "girl", "boy"),
}

BACKGROUND_FALLBACKS = ("living_room", "unknown")


class ResolvedCharacter(BaseModel):
    character_id: str
    speaker_ref: str
    role: str
    sprite_id: str
    manifest: SpriteManifest
    paths: dict[str, str] = Field(
        description="body/viseme/eye file paths, keyed for the animator"
    )


class ResolvedAssets(BaseModel):
    background: str
    characters: list[ResolvedCharacter]


class ResolveError(RuntimeError):
    pass


def _load_sprite_library(sprites_dir: Path) -> SpriteLibrary:
    lib_file = sprites_dir / "library.json"
    if lib_file.exists():
        return SpriteLibrary.model_validate_json(lib_file.read_text(encoding="utf-8"))
    manifests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(sprites_dir.glob("*/manifest.json"))
    ]
    return SpriteLibrary.model_validate({"sprites": manifests})


def pick_sprite(role: str, library: SpriteLibrary) -> SpriteManifest:
    normalized = role.strip().lower()
    for sprite_id, keywords in ROLE_KEYWORDS.items():
        if normalized == sprite_id or any(k in normalized for k in keywords):
            return library.get(sprite_id)
    raise ResolveError(f"no sprite for role {role!r} (known: {list(ROLE_KEYWORDS)})")


def resolve_assets(
    script: SceneScript,
    sprites_dir: Path = SPRITES_DIR,
    backgrounds_dir: Path = BACKGROUNDS_DIR,
) -> ResolvedAssets:
    library = _load_sprite_library(sprites_dir)
    bg_library = BackgroundLibrary.model_validate_json(
        (backgrounds_dir / "manifest.json").read_text(encoding="utf-8")
    )

    try:
        background = bg_library.get(script.setting)
    except KeyError:
        background = None
        for fallback in BACKGROUND_FALLBACKS:
            try:
                background = bg_library.get(fallback)
                break
            except KeyError:
                continue
        if background is None:
            raise ResolveError(
                f"no background for setting {script.setting!r} in {backgrounds_dir}"
            )
    background = str(backgrounds_dir / background)

    characters = []
    for char in script.characters:
        sprite = pick_sprite(char.role, library)
        base = sprites_dir / sprite.id
        paths = {
            "body": str(base / sprite.base_body),
            "eyes_open": str(base / sprite.eyes.open),
            "eyes_closed": str(base / sprite.eyes.closed),
        }
        for viseme, filename in sprite.visemes.items():
            paths[f"viseme_{viseme}"] = str(base / filename)
        characters.append(
            ResolvedCharacter(
                character_id=char.id,
                speaker_ref=char.speaker_ref,
                role=char.role,
                sprite_id=sprite.id,
                manifest=sprite,
                paths=paths,
            )
        )
    return ResolvedAssets(background=background, characters=characters)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="script.json -> resolved assets")
    parser.add_argument("script", help="input script.json")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output assets.json path")
    args = parser.parse_args(argv)

    try:
        script = SceneScript.model_validate_json(Path(args.script).read_text(encoding="utf-8"))
        resolved = resolve_assets(script)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(resolved.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[assets] wrote {out}")
        print(f"[assets] background={resolved.background}")
        for c in resolved.characters:
            print(f"[assets] {c.character_id} ({c.role}) -> {c.sprite_id}")
        return 0
    except Exception as exc:
        print(f"[assets] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
