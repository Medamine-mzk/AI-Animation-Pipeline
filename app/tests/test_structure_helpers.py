"""Tests for the story-structuring stage helpers (M2)."""

import pytest

from app.pipeline.structure_script import (
    apply_remap_segments,
    missing_segments,
    parse_remap_arg,
    remap_script,
    speaker_consistency_check,
)
from app.schemas.script import CameraShot, Character, Line, SceneScript

TRANSCRIPT = {
    "segments": [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0, "text": "one", "words": []},
        {"speaker": "SPEAKER_01", "start": 1.5, "end": 2.5, "text": "two", "words": []},
        {"speaker": "SPEAKER_00", "start": 3.0, "end": 4.0, "text": "three", "words": []},
    ]
}

CHARACTERS = [
    Character(id="a", speaker_ref="SPEAKER_00", role="parent"),
    Character(id="b", speaker_ref="SPEAKER_01", role="child"),
]


def _script(starts: list[float]) -> SceneScript:
    by_speaker = {c.speaker_ref: c.id for c in CHARACTERS}
    return SceneScript(
        setting="living_room",
        characters=CHARACTERS,
        lines=[
            Line(
                character_id=by_speaker[TRANSCRIPT["segments"][i]["speaker"]],
                start=s,
                end=s + 1.0,
                text="x",
                emotion="neutral",
                audio_segment_ref=f"seg_{i}",
            )
            for i, s in enumerate(starts)
        ],
        camera=[CameraShot(start=0.0, end=1.0, focus_character="a", shot="medium")],
    )


def test_all_segments_covered():
    assert missing_segments(TRANSCRIPT, _script([0.0, 1.5, 3.0])) == []


def test_missing_middle_segment():
    assert missing_segments(TRANSCRIPT, _script([0.0, 3.0])) == [1]


def test_missing_tail_segment():
    assert missing_segments(TRANSCRIPT, _script([0.0, 1.5])) == [2]


def test_timestamp_rounding_is_epsilon_safe():
    script = _script([0.0, 1.5, 3.0])
    script.lines[2].start = 3.004
    assert missing_segments(TRANSCRIPT, script) == []


def test_parse_remap_arg():
    assert parse_remap_arg("seg_3=SPEAKER_02, seg_5=SPEAKER_01") == {
        "seg_3": "SPEAKER_02",
        "seg_5": "SPEAKER_01",
    }
    assert parse_remap_arg(None) == {}
    with pytest.raises(ValueError):
        parse_remap_arg("seg_3")


def test_apply_remap_segments_only_touches_mapped():
    remapped = apply_remap_segments(TRANSCRIPT, {"seg_1": "SPEAKER_02"})
    assert remapped["segments"][1]["speaker"] == "SPEAKER_02"
    assert remapped["segments"][0]["speaker"] == "SPEAKER_00"
    assert remapped["segments"][0]["text"] == "one"


def test_speaker_consistency_passes_when_matching():
    script = _script([0.0, 1.5, 3.0])
    check = speaker_consistency_check(TRANSCRIPT)
    assert check(script) is None


def test_speaker_consistency_catches_swap():
    script = _script([0.0, 1.5, 3.0])
    script.lines[1].character_id = "a"
    check = speaker_consistency_check(TRANSCRIPT)
    assert "seg_1" in check(script)


def test_speaker_consistency_honors_remap():
    script = _script([0.0, 1.5, 3.0])
    script.lines[1].character_id = "a"
    check = speaker_consistency_check(TRANSCRIPT, {"seg_1": "SPEAKER_00"})
    assert check(script) is None


def test_remap_script_moves_line_and_camera():
    script = _script([0.0, 1.5, 3.0])
    script.camera.append(CameraShot(start=1.5, end=2.5, focus_character="b", shot="medium"))
    remapped = remap_script(script, {"seg_1": "SPEAKER_00"})
    assert remapped.lines[1].character_id == "a"
    assert any(
        shot.focus_character == "a" and shot.start == 1.5 for shot in remapped.camera
    )


def test_remap_script_noop_without_remap():
    script = _script([0.0, 1.5, 3.0])
    assert remap_script(script, {}) is script
