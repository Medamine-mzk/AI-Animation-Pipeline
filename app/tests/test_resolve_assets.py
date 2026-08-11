"""Tests for the asset resolution stage (M3)."""

import json

import pytest

from app.pipeline.resolve_assets import ResolveError, resolve_assets
from app.schemas.script import SceneScript

VISEMES = {v: f"s_mouth_{v}.png" for v in "ABCDEFGHX"}

SPRITE_DAD = {
    "id": "dad_v1",
    "base_body": "dad_v1_body.png",
    "visemes": VISEMES,
    "eyes": {"open": "dad_v1_eyes_open.png", "closed": "dad_v1_eyes_closed.png"},
    "anchor_point": {"x": 100, "y": 100},
}
SPRITE_MOM = {
    "id": "mom_v1",
    "base_body": "mom_v1_body.png",
    "visemes": VISEMES,
    "eyes": {"open": "mom_v1_eyes_open.png", "closed": "mom_v1_eyes_closed.png"},
    "anchor_point": {"x": 100, "y": 100},
}
SPRITE_KID = {
    "id": "child_v1",
    "base_body": "child_v1_body.png",
    "visemes": VISEMES,
    "eyes": {"open": "child_v1_eyes_open.png", "closed": "child_v1_eyes_closed.png"},
    "anchor_point": {"x": 100, "y": 100},
}


def _make_library(tmp_path) -> tuple:
    sprites_dir = tmp_path / "sprites"
    bg_dir = tmp_path / "backgrounds"
    sprites_dir.mkdir()
    bg_dir.mkdir()
    for sprite in (SPRITE_DAD, SPRITE_MOM, SPRITE_KID):
        (sprites_dir / f"{sprite['id']}").mkdir()
        (sprites_dir / f"{sprite['id']}" / "manifest.json").write_text(
            json.dumps(sprite), encoding="utf-8"
        )
    (bg_dir / "manifest.json").write_text(
        json.dumps(
            {
                "backgrounds": [
                    {"setting": "living_room", "file": "living_room.png"},
                    {"setting": "kitchen", "file": "kitchen.png"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return sprites_dir, bg_dir


def _script(roles: list[str], setting: str = "living_room") -> SceneScript:
    return SceneScript.model_validate(
        {
            "setting": setting,
            "characters": [
                {"id": f"SPEAKER_{i}", "speaker_ref": f"SPEAKER_{i}", "role": role}
                for i, role in enumerate(roles)
            ],
            "lines": [
                {
                    "character_id": "SPEAKER_0",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "Hi.",
                    "emotion": "neutral",
                    "audio_segment_ref": "seg_0",
                }
            ],
            "camera": [
                {"start": 0.0, "end": 1.0, "focus_character": "SPEAKER_0", "shot": "medium"}
            ],
        }
    )


def test_resolves_roles_to_sprites(tmp_path):
    sprites_dir, bg_dir = _make_library(tmp_path)
    resolved = resolve_assets(_script(["father", "mother", "child"]), sprites_dir, bg_dir)
    by_id = {c.character_id: c for c in resolved.characters}
    assert by_id["SPEAKER_0"].sprite_id == "dad_v1"
    assert by_id["SPEAKER_1"].sprite_id == "mom_v1"
    assert by_id["SPEAKER_2"].sprite_id == "child_v1"
    assert resolved.background.endswith("living_room.png")


def test_resolved_character_has_all_render_paths(tmp_path):
    sprites_dir, bg_dir = _make_library(tmp_path)
    resolved = resolve_assets(_script(["father"]), sprites_dir, bg_dir)
    char = resolved.characters[0]
    assert char.paths["body"].endswith("dad_v1_body.png")
    for v in "ABCDEFGHX":
        assert char.paths[f"viseme_{v}"].endswith(f"s_mouth_{v}.png")
    assert char.paths["eyes_open"].endswith("eyes_open.png")
    assert char.paths["eyes_closed"].endswith("eyes_closed.png")


def test_fuzzy_role_mapping(tmp_path):
    sprites_dir, bg_dir = _make_library(tmp_path)
    resolved = resolve_assets(
        _script(["mom", "DAD", "kid"]), sprites_dir, bg_dir
    )
    by_id = {c.character_id: c for c in resolved.characters}
    assert by_id["SPEAKER_0"].sprite_id == "mom_v1"
    assert by_id["SPEAKER_1"].sprite_id == "dad_v1"
    assert by_id["SPEAKER_2"].sprite_id == "child_v1"


def test_unknown_role_raises(tmp_path):
    sprites_dir, bg_dir = _make_library(tmp_path)
    with pytest.raises(ResolveError):
        resolve_assets(_script(["wizard"]), sprites_dir, bg_dir)


def test_unknown_setting_raises(tmp_path):
    sprites_dir, bg_dir = _make_library(tmp_path)
    lib = json.loads((bg_dir / "manifest.json").read_text(encoding="utf-8"))
    lib["backgrounds"] = [b for b in lib["backgrounds"] if b["setting"] != "living_room"]
    (bg_dir / "manifest.json").write_text(json.dumps(lib), encoding="utf-8")
    with pytest.raises(ResolveError):
        resolve_assets(_script(["father"], setting="bedroom"), sprites_dir, bg_dir)


def test_missing_setting_falls_back_to_living_room(tmp_path):
    sprites_dir, bg_dir = _make_library(tmp_path)
    lib = json.loads((bg_dir / "manifest.json").read_text(encoding="utf-8"))
    lib["backgrounds"] = [b for b in lib["backgrounds"] if b["setting"] != "kitchen"]
    (bg_dir / "manifest.json").write_text(json.dumps(lib), encoding="utf-8")
    resolved = resolve_assets(_script(["father"], setting="kitchen"), sprites_dir, bg_dir)
    assert resolved.background.endswith("living_room.png")
