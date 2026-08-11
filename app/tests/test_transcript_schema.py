"""Unit tests for the diarized transcript schema (spec 3.1)."""

import pytest
from pydantic import ValidationError

from app.schemas.transcript import Transcript, Segment, Word

SPEC_SAMPLE = {
    "segments": [
        {
            "speaker": "SPEAKER_00",
            "start": 0.42,
            "end": 3.10,
            "text": "I really think we should leave early tomorrow.",
            "words": [
                {"word": "I", "start": 0.42, "end": 0.55},
                {"word": "really", "start": 0.55, "end": 0.81},
            ],
        }
    ]
}


def test_parses_spec_sample():
    transcript = Transcript.model_validate(SPEC_SAMPLE)
    assert transcript.segments[0].speaker == "SPEAKER_00"
    assert transcript.segments[0].words[1].word == "really"
    assert transcript.segments[0].words[1].end == 0.81


def test_round_trip_json():
    transcript = Transcript.model_validate(SPEC_SAMPLE)
    reparsed = Transcript.model_validate_json(transcript.model_dump_json())
    assert reparsed == transcript


def test_rejects_segment_without_speaker():
    data = SPEC_SAMPLE.copy()
    data["segments"][0]["speaker"] = ""
    with pytest.raises(ValidationError):
        Transcript.model_validate(data)


def test_rejects_negative_timestamps():
    data = SPEC_SAMPLE.copy()
    data["segments"][0]["start"] = -1.0
    with pytest.raises(ValidationError):
        Transcript.model_validate(data)


def test_rejects_word_with_end_before_start():
    data = SPEC_SAMPLE.copy()
    data["segments"][0]["words"][1]["end"] = 0.40
    with pytest.raises(ValidationError):
        Transcript.model_validate(data)


def test_rejects_empty_transcript():
    with pytest.raises(ValidationError):
        Transcript.model_validate({"segments": []})
