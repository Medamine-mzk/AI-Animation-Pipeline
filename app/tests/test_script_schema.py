"""Unit tests for the structured scene script schema (spec 3.2)."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.script import Character, SceneScript

SPEC_SAMPLE = {
    "setting": "living_room",
    "characters": [
        {"id": "dad", "speaker_ref": "SPEAKER_00", "role": "father"},
        {"id": "daughter", "speaker_ref": "SPEAKER_01", "role": "daughter"},
    ],
    "lines": [
        {
            "character_id": "dad",
            "start": 0.42,
            "end": 3.10,
            "text": "I really think we should leave early tomorrow.",
            "emotion": "neutral",
            "audio_segment_ref": "seg_0",
        }
    ],
    "camera": [
        {"start": 0.0, "end": 3.10, "focus_character": "dad", "shot": "medium"}
    ],
}


def test_parses_spec_sample():
    script = SceneScript.model_validate(SPEC_SAMPLE)
    assert script.characters[0].id == "dad"
    assert script.lines[0].emotion == "neutral"
    assert script.camera[0].shot == "medium"


def test_round_trip_json():
    script = SceneScript.model_validate(SPEC_SAMPLE)
    reparsed = SceneScript.model_validate_json(script.model_dump_json())
    assert reparsed == script


def test_rejects_line_with_unknown_character():
    data = deepcopy(SPEC_SAMPLE)
    data["lines"][0]["character_id"] = "ghost"
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_rejects_camera_with_unknown_focus():
    data = deepcopy(SPEC_SAMPLE)
    data["camera"][0]["focus_character"] = "ghost"
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_rejects_line_end_before_start():
    data = deepcopy(SPEC_SAMPLE)
    data["lines"][0]["end"] = 0.1
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_rejects_unknown_emotion():
    data = deepcopy(SPEC_SAMPLE)
    data["lines"][0]["emotion"] = "chromatic"
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_rejects_unknown_shot():
    data = deepcopy(SPEC_SAMPLE)
    data["camera"][0]["shot"] = "extreme-zoom"
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_rejects_empty_characters():
    data = deepcopy(SPEC_SAMPLE)
    data["characters"] = []
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_rejects_empty_lines():
    data = deepcopy(SPEC_SAMPLE)
    data["lines"] = []
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_rejects_empty_camera():
    data = deepcopy(SPEC_SAMPLE)
    data["camera"] = []
    with pytest.raises(ValidationError):
        SceneScript.model_validate(data)


def test_character_rejects_empty_id():
    with pytest.raises(ValidationError):
        Character.model_validate({"id": "", "speaker_ref": "SPEAKER_00", "role": "father"})
