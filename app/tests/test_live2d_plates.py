"""Tests for the Live2D plate-style asset resolution and plate-swap animation."""

import json

import pytest
from PIL import Image

from app.pipeline.animate import SceneRenderer, layout_characters
from app.pipeline.resolve_assets import live2d_match_score, pick_live2d_model, resolve_assets
from app.schemas.script import SceneScript
from app.schemas.sprite_manifest import Live2DLibrary

L2D_LIBRARY = {
    "models": [
        {"id": "touma", "plates_dir": "jobs/spike2d/touma", "gender": "male", "age_group": "teen"},
        {"id": "mikoto", "plates_dir": "jobs/spike2d/mikoto", "gender": "female", "age_group": "teen"},
        {"id": "chiaki_kitty", "plates_dir": "jobs/spike2d/chiaki_kitty", "gender": "female", "age_group": "child"},
    ]
}


def _script(chars: list[dict]) -> SceneScript:
    return SceneScript.model_validate(
        {
            "setting": "living_room",
            "characters": chars,
            "lines": [
                {
                    "character_id": chars[0]["id"],
                    "start": 0.0,
                    "end": 1.0,
                    "text": "Hi.",
                    "emotion": "neutral",
                    "audio_segment_ref": "seg_0",
                }
            ],
            "camera": [
                {"start": 0.0, "end": 1.0, "focus_character": chars[0]["id"], "shot": "medium"}
            ],
        }
    )


def _make_l2d_dir(tmp_path) -> dict:
    sprites_dir = tmp_path / "sprites"
    bg_dir = tmp_path / "backgrounds"
    sprites_dir.mkdir()
    bg_dir.mkdir()
    (sprites_dir / "live2d_library.json").write_text(json.dumps(L2D_LIBRARY), encoding="utf-8")
    (bg_dir / "manifest.json").write_text(
        json.dumps({"backgrounds": [{"setting": "living_room", "file": "living_room.png"}]}),
        encoding="utf-8",
    )
    for model in L2D_LIBRARY["models"]:
        plates = tmp_path / "jobs" / "spike2d" / model["id"]
        plates.mkdir(parents=True, exist_ok=True)
        for v in "ABCDEFGHX":
            for eye in ("open", "closed"):
                img = Image.new("RGBA", (60, 120), (0, 0, 0, 0))
                px = img.load()
                for yy in range(10, 110):
                    for xx in range(15, 45):
                        px[xx, yy] = (200, 100, 60, 255)
                img.save(plates / f"{v}_{eye}.png")
    return {"sprites_dir": sprites_dir, "bg_dir": bg_dir, "root": tmp_path}


def test_live2d_match_score():
    assert live2d_match_score("male", "teen", "male", "teen") == 110
    assert live2d_match_score("female", "teen", "male", "teen") < 0
    assert live2d_match_score("female", "child", "female", "child") == 110
    # adult woman prefers adult model over teen, but female is required
    adult = live2d_match_score("female", "adult", "female", "adult")
    teen = live2d_match_score("female", "teen", "female", "adult")
    assert adult > teen


def test_pick_live2d_model_distinct_and_gender_aware():
    lib = Live2DLibrary.model_validate(L2D_LIBRARY)
    used = set()
    assert pick_live2d_model(lib, "male", "teen", used) == "touma"
    used.add("touma")
    assert pick_live2d_model(lib, "female", "teen", used) == "mikoto"
    used.add("mikoto")
    assert pick_live2d_model(lib, "female", "child", used) == "chiaki_kitty"


def test_resolve_uses_plates_when_live2d_library_present(tmp_path):
    dirs = _make_l2d_dir(tmp_path)
    script = _script(
        [
            {"id": "SPEAKER_0", "speaker_ref": "SPEAKER_0", "role": "father", "gender": "male", "age_group": "adult"},
            {"id": "SPEAKER_1", "speaker_ref": "SPEAKER_1", "role": "mother", "gender": "female", "age_group": "adult"},
            {"id": "SPEAKER_2", "speaker_ref": "SPEAKER_2", "role": "child", "gender": "female", "age_group": "child"},
        ]
    )
    resolved = resolve_assets(
        script, dirs["sprites_dir"], dirs["bg_dir"], dirs["sprites_dir"] / "live2d_library.json"
    )
    by_id = {c.character_id: c for c in resolved.characters}
    assert by_id["SPEAKER_0"].render_mode == "plates"
    assert by_id["SPEAKER_0"].sprite_id == "touma"  # male
    assert by_id["SPEAKER_1"].sprite_id == "mikoto"  # female, closest to adult of remaining
    assert by_id["SPEAKER_2"].sprite_id == "chiaki_kitty"  # female child
    assert len(by_id["SPEAKER_0"].plates) == 18
    assert by_id["SPEAKER_0"].plates["A_open"].endswith("A_open.png")


def test_resolve_assigns_distinct_models(tmp_path):
    dirs = _make_l2d_dir(tmp_path)
    script = _script(
        [
            {"id": "SPEAKER_0", "speaker_ref": "SPEAKER_0", "role": "dad", "gender": "male", "age_group": "adult"},
            {"id": "SPEAKER_1", "speaker_ref": "SPEAKER_1", "role": "mom", "gender": "female", "age_group": "adult"},
        ]
    )
    resolved = resolve_assets(
        script, dirs["sprites_dir"], dirs["bg_dir"], dirs["sprites_dir"] / "live2d_library.json"
    )
    ids = {c.sprite_id for c in resolved.characters}
    assert len(ids) == 2  # each speaker gets a distinct model


def test_layout_characters_scales_plates(tmp_path):
    dirs = _make_l2d_dir(tmp_path)
    char = {
        "character_id": "A",
        "sprite_id": "touma",
        "speaker_ref": "A",
        "render_mode": "plates",
        "paths": {},
        "plates": {
            v + "_" + e: str(dirs["root"] / "jobs" / "spike2d" / "touma" / f"{v}_{e}.png")
            for v in "ABCDEFGHX"
            for e in ("open", "closed")
        },
    }
    layout = layout_characters([char], canvas_w=1920, canvas_h=1080, floor_y=810)
    a = layout["A"]
    assert a.top + a.height == 810
    assert a.height == round(1080 * 0.6)


def test_render_plate_mode_smoke(tmp_path):
    dirs = _make_l2d_dir(tmp_path)
    assets = {
        "background": str(dirs["bg_dir"] / "living_room.png"),
        "characters": [
            {
                "character_id": "A",
                "speaker_ref": "A",
                "role": "dad",
                "render_mode": "plates",
                "sprite_id": "touma",
                "paths": {},
                "plates": {
                    v + "_" + e: str(dirs["root"] / "jobs" / "spike2d" / "touma" / f"{v}_{e}.png")
                    for v in "ABCDEFGHX"
                    for e in ("open", "closed")
                },
            }
        ],
    }
    Image.new("RGB", (240, 180), (90, 120, 90)).save(dirs["bg_dir"] / "living_room.png")
    visemes = {"A": [{"start": 0.0, "end": 2.0, "shape": "A"}]}
    shots = [{"start": 0.0, "end": 2.0, "focus_character": "A", "shot": "wide"}]
    r = SceneRenderer(
        assets=assets,
        visemes=visemes,
        shots=shots,
        canvas=(240, 180),
        floor_y=140,
        seed=42,
        duration=2.0,
    )
    frame = r.render(1.0)
    assert frame.size == (240, 180)
    assert frame.mode == "RGB"
    assert frame.getbbox() is not None


def test_render_plate_viseme_change_differs(tmp_path):
    dirs = _make_l2d_dir(tmp_path)
    assets = {
        "background": str(dirs["bg_dir"] / "living_room.png"),
        "characters": [
            {
                "character_id": "A",
                "speaker_ref": "A",
                "role": "dad",
                "render_mode": "plates",
                "sprite_id": "touma",
                "paths": {},
                "plates": {
                    v + "_" + e: str(dirs["root"] / "jobs" / "spike2d" / "touma" / f"{v}_{e}.png")
                    for v in "ABCDEFGHX"
                    for e in ("open", "closed")
                },
            }
        ],
    }
    Image.new("RGB", (240, 180), (90, 120, 90)).save(dirs["bg_dir"] / "living_room.png")
    visemes = {"A": [{"start": 0.0, "end": 1.0, "shape": "A"}, {"start": 1.0, "end": 2.0, "shape": "X"}]}
    shots = [{"start": 0.0, "end": 2.0, "focus_character": "A", "shot": "wide"}]
    r = SceneRenderer(
        assets=assets,
        visemes=visemes,
        shots=shots,
        canvas=(240, 180),
        floor_y=140,
        seed=42,
        duration=2.0,
    )
    assert r.render(0.3).tobytes() != r.render(1.5).tobytes()