"""M3: asset resolution — script.json -> concrete sprite/background files.

Picks sprites from a fixed library by character role (spec 2: curated library,
not generative) and backgrounds by setting. Writes assets.json.

When a Live2D plate library is available (app/assets/sprites/live2d_library.json),
each character is instead matched to the plate model that best fits the
character's inferred gender/age_group, and the manifest carries the 18-plate
{viseme}_{open|closed}.png set for the plate-swap animator.

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
    Live2DLibrary,
    SpriteLibrary,
    SpriteManifest,
)

ROOT = Path(__file__).resolve().parents[2]
SPRITES_DIR = ROOT / "app" / "assets" / "sprites"
BACKGROUNDS_DIR = ROOT / "app" / "assets" / "backgrounds"
LIVE2D_LIBRARY = SPRITES_DIR / "live2d_library.json"
DEFAULT_OUT = ROOT / "jobs" / "golden" / "assets.json"

ROLE_KEYWORDS = {
    "dad_v1": ("father", "dad", "papa", "daddy", "man"),
    "mom_v1": ("mother", "mom", "mum", "mama", "mommy", "woman"),
    "child_v1": ("child", "kid", "daughter", "son", "girl", "boy"),
}

BACKGROUND_FALLBACKS = ("living_room", "unknown")

AGE_ORDER = {"child": 0, "teen": 1, "adult": 2, "elder": 3}

# Deterministic attribute fallback for scripts without gender/age_group (e.g.
# produced before the structure stage inferred them). Applied only to unknowns.
ROLE_ATTRIBUTES = [
    (("father", "dad", "papa", "daddy", "husband", "grandpa", "grandfather", "man"), "male", "adult"),
    (("mother", "mom", "mum", "mama", "mommy", "wife", "grandma", "grandmother", "woman"), "female", "adult"),
    (("boy", "son", "brother", "little"), "male", "child"),
    (("girl", "daughter", "sister", "little"), "female", "child"),
    (("child", "kid", "baby", "toddler"), "unknown", "child"),
    (("teen", "student", "school", "classmate", "friend"), "unknown", "teen"),
]


def infer_attributes(role: str, gender: str, age_group: str) -> tuple[str, str]:
    """Fill unknown gender/age from role keywords (used for legacy scripts)."""
    normalized = role.strip().lower()
    gender_out, age_out = gender, age_group
    for keywords, g, a in ROLE_ATTRIBUTES:
        if any(k in normalized for k in keywords):
            if gender_out == "unknown":
                gender_out = g
            if age_out == "unknown":
                age_out = a
            break
    return gender_out, age_out


class ResolvedCharacter(BaseModel):
    character_id: str
    speaker_ref: str
    role: str
    gender: str
    age_group: str
    render_mode: str = "composite"  # "composite" (overlay) or "plates" (swap)
    sprite_id: str
    manifest: SpriteManifest | None = None
    paths: dict[str, str] = Field(
        description="body/viseme/eye file paths, keyed for the animator"
    )
    plates: dict[str, str] = Field(
        default_factory=dict,
        description="plate-mode: {viseme}_{open|closed} -> file path",
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


def _load_live2d_library(path: Path) -> Live2DLibrary | None:
    if not path.exists():
        return None
    return Live2DLibrary.model_validate_json(path.read_text(encoding="utf-8"))


def live2d_match_score(
    model_gender: str, model_age: str, want_gender: str, want_age: str
) -> int:
    """Higher = better fit. Gender mismatch is disqualifying unless unknown."""
    score = 0
    if want_gender == "unknown":
        score += 1
    elif model_gender == want_gender:
        score += 100
    else:
        return -1_000_000
    if want_age == "unknown":
        score += 1
    else:
        score += 10 - min(abs(AGE_ORDER[model_age] - AGE_ORDER[want_age]), 10)
    return score


def pick_live2d_model(
    library: Live2DLibrary,
    want_gender: str,
    want_age: str,
    used: set[str],
) -> str:
    """Best unused model for (gender, age); any unused model as fallback."""
    ranked = sorted(
        library.models,
        key=lambda m: live2d_match_score(m.gender, m.age_group, want_gender, want_age),
        reverse=True,
    )
    for model in ranked:
        if model.id not in used:
            return model.id
    for model in ranked:
        if model.gender == want_gender:
            return model.id
    return ranked[0].id


PLATE_VISEMES = "ABCDEFGHX"


def plate_paths(model, plates_dir: Path) -> dict[str, str]:
    base = ROOT / model.plates_dir
    paths = {}
    for v in PLATE_VISEMES:
        for eye in ("open", "closed"):
            paths[f"{v}_{eye}"] = str(base / f"{v}_{eye}.png")
    return paths


def resolve_assets(
    script: SceneScript,
    sprites_dir: Path = SPRITES_DIR,
    backgrounds_dir: Path = BACKGROUNDS_DIR,
    live2d_library: Path | None = None,
) -> ResolvedAssets:
    library = _load_sprite_library(sprites_dir)
    bg_library = BackgroundLibrary.model_validate_json(
        (backgrounds_dir / "manifest.json").read_text(encoding="utf-8")
    )
    l2d = _load_live2d_library(live2d_library or sprites_dir / "live2d_library.json")

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
    used_models: set[str] = set()
    for char in script.characters:
        gender, age_group = infer_attributes(char.role, char.gender, char.age_group)
        if l2d is not None:
            model_id = pick_live2d_model(l2d, gender, age_group, used_models)
            used_models.add(model_id)
            model = l2d.get(model_id)
            characters.append(
                ResolvedCharacter(
                    character_id=char.id,
                    speaker_ref=char.speaker_ref,
                    role=char.role,
                    gender=gender,
                    age_group=age_group,
                    render_mode="plates",
                    sprite_id=model.id,
                    paths={},
                    plates=plate_paths(model, ROOT),
                )
            )
            continue

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
                gender=gender,
                age_group=age_group,
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
            mode = c.render_mode
            print(f"[assets] {c.character_id} ({c.role}, {c.gender}/{c.age_group}) -> {c.sprite_id} [{mode}]")
        return 0
    except Exception as exc:
        print(f"[assets] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
