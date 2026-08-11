"""Tests for the story-structuring stage helpers (M2)."""

from app.pipeline.structure_script import missing_segments
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
    return SceneScript(
        setting="living_room",
        characters=CHARACTERS,
        lines=[
            Line(
                character_id="a",
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
