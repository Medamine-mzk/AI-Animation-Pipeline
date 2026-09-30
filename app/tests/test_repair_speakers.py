"""Tests for the LLM speaker-repair stage (transcript -> speaker remap)."""

import json

import pytest

from app.pipeline.repair_speakers import (
    RepairResult,
    SegmentRemap,
    build_user_prompt,
    make_validator,
    repair_speakers,
    to_remap_dict,
)


def _transcript() -> dict:
    """A transcript whose diarization merged 4 real people into 3 clusters.

    SPEAKER_00 is the dad (clean). SPEAKER_01 mixes the girl Ymen and the boy
    Hazem. SPEAKER_02 mixes the girl and the mom.
    """
    return {
        "segments": [
            {"speaker": "SPEAKER_01", "start": 0.0, "end": 1.0, "text": "Mom, I received a letter from Chris."},
            {"speaker": "SPEAKER_01", "start": 1.0, "end": 2.0, "text": "The Browns are inviting me to London."},
            {"speaker": "SPEAKER_01", "start": 2.0, "end": 3.0, "text": "Can I go?"},
            {"speaker": "SPEAKER_01", "start": 3.0, "end": 4.0, "text": "Well, I'm not sure."},
            {"speaker": "SPEAKER_01", "start": 4.0, "end": 5.0, "text": "It's a wonderful opportunity, Mom."},
            {"speaker": "SPEAKER_02", "start": 5.0, "end": 6.0, "text": "I can't miss it."},
            {"speaker": "SPEAKER_02", "start": 6.0, "end": 7.0, "text": "Please, Mom."},
            {"speaker": "SPEAKER_02", "start": 7.0, "end": 8.0, "text": "Now wait."},
            {"speaker": "SPEAKER_02", "start": 8.0, "end": 9.0, "text": "We should discuss this with Daddy."},
            {"speaker": "SPEAKER_02", "start": 9.0, "end": 10.0, "text": "First, I'll be able to speak English."},
            {"speaker": "SPEAKER_00", "start": 10.0, "end": 11.0, "text": "That's interesting."},
            {"speaker": "SPEAKER_01", "start": 11.0, "end": 12.0, "text": "If she goes, I go."},
            {"speaker": "SPEAKER_00", "start": 12.0, "end": 13.0, "text": "Sorry, Hazem."},
            {"speaker": "SPEAKER_01", "start": 13.0, "end": 14.0, "text": "Promise?"},
        ]
    }


def test_build_user_prompt_contains_every_segment():
    prompt = build_user_prompt(_transcript())
    for i in range(14):
        assert f"seg_{i}:" in prompt


def test_validator_accepts_full_remap():
    t = _transcript()
    result = RepairResult(
        segments=[SegmentRemap(index=i, speaker="SPEAKER_00") for i in range(len(t["segments"]))]
    )
    assert make_validator(t)(result) is None


def test_validator_rejects_missing_segments():
    t = _transcript()
    result = RepairResult(segments=[SegmentRemap(index=i, speaker="SPEAKER_00") for i in range(5)])
    error = make_validator(t)(result)
    assert error and "missing" in error


def test_validator_rejects_duplicate_index():
    t = _transcript()
    result = RepairResult(
        segments=[SegmentRemap(index=i, speaker="SPEAKER_00") for i in range(len(t["segments"]))]
        + [SegmentRemap(index=0, speaker="SPEAKER_01")]
    )
    error = make_validator(t)(result)
    assert error and "duplicate" in error


def test_validator_rejects_non_speaker_label():
    t = _transcript()
    result = RepairResult(
        segments=[
            SegmentRemap(index=i, speaker="Steve" if i == 0 else "SPEAKER_00")
            for i in range(len(t["segments"]))
        ]
    )
    error = make_validator(t)(result)
    assert error and "SPEAKER_" in error


def test_to_remap_dict_orders_by_transcript_index():
    t = _transcript()
    result = RepairResult(
        segments=[
            SegmentRemap(index=0, speaker="SPEAKER_03"),
            SegmentRemap(index=1, speaker="SPEAKER_01"),
        ]
    )
    remap = to_remap_dict(t, result)
    assert remap["seg_0"] == "SPEAKER_03"
    assert remap["seg_1"] == "SPEAKER_01"
    assert set(remap) == {f"seg_{i}" for i in range(len(t["segments"]))}


def test_repair_speakers_writes_file(tmp_path):
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps(_transcript()), encoding="utf-8")
    out = tmp_path / "remap.json"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.pipeline.repair_speakers.generate_structured",
            lambda *a, **k: RepairResult(
                segments=[
                    SegmentRemap(
                        index=i,
                        speaker="SPEAKER_01"
                        if i in (0, 1, 2, 4, 5, 6, 9)
                        else "SPEAKER_02"
                        if i in (3, 7, 8)
                        else "SPEAKER_03"
                        if i in (11, 13)
                        else "SPEAKER_00",
                    )
                    for i in range(len(_transcript()["segments"]))
                ]
            ),
        )
        remap = repair_speakers(transcript, output_path=out)
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == remap
    assert loaded["seg_3"] == "SPEAKER_02"  # mom
    assert loaded["seg_11"] == "SPEAKER_03"  # hazem
    assert loaded["seg_9"] == "SPEAKER_01"  # girl's big speech