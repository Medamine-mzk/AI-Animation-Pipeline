"""Unit tests for the real word-level timeline (captions feature, M0).

These tests never invoke WhisperX -- the model download is far too slow for a
unit suite. The builder is written so the whole transform is pure functions over
already-aligned words, and only the thin `transcribe_words()` wrapper touches
whisperx. That keeps the meaningful logic covered here.
"""

import json

import pytest
from pydantic import ValidationError

from app.pipeline.word_timeline import (
    build_word_timeline,
    detect_overlaps,
    renumber_by_first_appearance,
    seconds_to_ms,
)
from app.schemas.word_timeline import WordTimeline

# Two speakers talking over each other briefly -- the "interruption" case from
# spec 7, plus a low-confidence name the editor should be able to underline.
RAW_WORDS = [
    {"word": "Hello", "start": 0.42, "end": 0.55, "speaker": "A", "score": 0.99},
    {"word": "Chris", "start": 0.55, "end": 0.81, "speaker": "A", "score": 0.42},
    {"word": "Hi", "start": 0.84, "end": 0.90, "speaker": "B", "score": 0.95},
    {"word": "I", "start": 0.90, "end": 0.94, "speaker": "B", "score": 0.97},
    {"word": "left", "start": 0.95, "end": 1.10, "speaker": "B", "score": 0.93},
    {"word": "already", "start": 1.30, "end": 1.70, "speaker": "A", "score": 0.88},
]


# ---------------------------------------------------------------- time helpers


def test_seconds_to_ms_rounds_not_truncates():
    # 0.1235 * 1000 = 123.5 -> must not silently lose the half
    assert seconds_to_ms(0.1235) == 124
    assert seconds_to_ms(1.627) == 1627
    assert seconds_to_ms(0.0) == 0


def test_seconds_to_ms_clamps_negatives():
    # Alignment should never go negative, but a bad row must not produce -100
    assert seconds_to_ms(-0.5) == 0


# ------------------------------------------------------------ speaker ordering


def test_renumber_by_first_appearance_not_by_volume():
    # 'B' talks longer than 'A' but speaks second, so it must be SPEAKER_01.
    # main.py's existing diarization remap orders by descending segment count;
    # the captions pipeline deliberately differs (spec 4: first appearance).
    mapping = renumber_by_first_appearance(RAW_WORDS)
    assert mapping == {"A": "SPEAKER_00", "B": "SPEAKER_01"}


def test_renumber_ignores_words_without_speaker():
    words = [
        {"speaker": None, "start": 0.0, "end": 0.1},
        {"speaker": "Z", "start": 0.1, "end": 0.2},
    ]
    assert renumber_by_first_appearance(words) == {"Z": "SPEAKER_00"}


# ------------------------------------------------------------------ overlaps


def test_detect_overlaps_finds_interruption():
    # 'Chris' (A) ends 810ms, 'I' (B) starts 900ms -> no overlap there.
    # Give 'left' an overlapping tail against 'already' instead.
    words = [
        {"start": 0.0, "end": 0.5, "speaker": "A"},
        {"start": 0.4, "end": 0.9, "speaker": "B"},
    ]
    overlaps = detect_overlaps(words, tolerance_ms=20)
    assert len(overlaps) == 1
    assert overlaps[0].startMs == 400
    assert overlaps[0].endMs == 500
    assert set(overlaps[0].speakerIds) == {"A", "B"}


def test_detect_overlaps_ignores_same_speaker():
    words = [
        {"start": 0.0, "end": 0.5, "speaker": "A"},
        {"start": 0.4, "end": 0.9, "speaker": "A"},
    ]
    assert detect_overlaps(words) == []


def test_detect_overlaps_tolerates_small_diarization_jitter():
    # 10ms of boundary slop is normal and should not be reported to the user.
    words = [
        {"start": 0.000, "end": 0.500, "speaker": "A"},
        {"start": 0.510, "end": 0.900, "speaker": "B"},
    ]
    assert detect_overlaps(words, tolerance_ms=20) == []


# ------------------------------------------------------------- full assembly


def test_build_word_timeline_from_raw_words():
    tl = build_word_timeline(RAW_WORDS, language="en", duration_s=2.0)

    assert tl.wordAlignment == "measured"
    assert tl.language == "en"
    assert tl.durationMs == 2000
    assert [w.text for w in tl.words] == [
        "Hello", " Chris", " Hi", " I", " left", " already",
    ]
    # @remotion/captions convention: first word has no leading space.
    assert tl.words[0].text == "Hello"
    assert tl.words[1].text.startswith(" ")

    assert tl.words[0].startMs == 420
    assert tl.words[0].endMs == 550
    assert tl.words[0].speakerId == "SPEAKER_00"

    # Low confidence must survive so the editor can underline it (spec 7).
    chris = next(w for w in tl.words if w.text.strip() == "Chris")
    assert chris.confidence == pytest.approx(0.42)
    assert tl.hasConfidence is True

    # timestampMs must track the spoken start.
    assert all(w.timestampMs == w.startMs for w in tl.words)


def test_build_word_timeline_builds_speaker_summaries_in_appearance_order():
    tl = build_word_timeline(RAW_WORDS, language="en", duration_s=2.0)

    assert [s.speakerId for s in tl.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert [s.index for s in tl.speakers] == [0, 1]

    first = tl.speakers[0]
    assert first.firstWordMs == 420
    assert first.lastWordMs == 1700
    assert first.wordCount == 3
    # Hello 130 + Chris 260 + already 400 = 790ms of speech by this speaker.
    # Note this is the sum of word durations, not the span between first and
    # last word, so the silences between them are correctly excluded.
    assert first.totalMs == 130 + 260 + 400

    second = tl.speakers[1]
    assert second.wordCount == 3
    assert second.firstWordMs == 840


def test_build_word_timeline_inherits_speaker_for_unlabelled_words():
    # The diarizer drops a label on 'left'; it should inherit from 'I' rather
    # than vanish, and the fact should be reported honestly.
    words = [
        {"word": "One", "start": 0.0, "end": 0.2, "speaker": "A", "score": 0.9},
        {"word": "two", "start": 0.2, "end": 0.4, "speaker": None, "score": 0.9},
    ]
    tl = build_word_timeline(words, language="en", duration_s=1.0)

    assert [w.speakerId for w in tl.words] == ["SPEAKER_00", "SPEAKER_00"]
    assert tl.hasInheritedSpeakers is True
    assert tl.speakers[0].inheritedWordCount == 1


def test_build_word_timeline_drops_words_missing_timing():
    words = [
        {"word": "good", "start": 0.0, "end": 0.2, "speaker": "A", "score": 0.9},
        {"word": "bad", "speaker": "A", "score": 0.9},  # no start/end
    ]
    tl = build_word_timeline(words, language="en", duration_s=1.0)
    assert [w.text.strip() for w in tl.words] == ["good"]


def test_build_word_timeline_drops_empty_whitespace_words():
    words = [
        {"word": "hi", "start": 0.0, "end": 0.2, "speaker": "A", "score": 0.9},
        {"word": "   ", "start": 0.2, "end": 0.3, "speaker": "A", "score": 0.9},
    ]
    tl = build_word_timeline(words, language="en", duration_s=1.0)
    assert len(tl.words) == 1


def test_build_word_timeline_clamps_end_before_start():
    # Defensive: forced alignment occasionally emits end < start.
    words = [{"word": "oops", "start": 1.0, "end": 0.5, "speaker": "A", "score": 0.9}]
    tl = build_word_timeline(words, language="en", duration_s=2.0)
    assert tl.words[0].endMs == tl.words[0].startMs == 1000


def test_build_word_timeline_sorts_out_of_order_words():
    words = [
        {"word": "second", "start": 2.0, "end": 2.5, "speaker": "A", "score": 0.9},
        {"word": "first", "start": 1.0, "end": 1.5, "speaker": "A", "score": 0.9},
    ]
    tl = build_word_timeline(words, language="en", duration_s=3.0)
    assert [w.text.strip() for w in tl.words] == ["first", "second"]


def test_build_word_timeline_handles_missing_confidence():
    words = [{"word": "hey", "start": 0.0, "end": 0.2, "speaker": "A"}]
    tl = build_word_timeline(words, language="en", duration_s=1.0)
    assert tl.words[0].confidence is None
    assert tl.hasConfidence is False


def test_build_word_timeline_records_overlaps():
    words = [
        {"word": "a", "start": 0.0, "end": 0.5, "speaker": "A", "score": 0.9},
        {"word": "b", "start": 0.4, "end": 0.9, "speaker": "B", "score": 0.9},
    ]
    tl = build_word_timeline(words, language="en", duration_s=1.0)
    assert len(tl.overlaps) == 1
    assert set(tl.overlaps[0].speakerIds) == {"SPEAKER_00", "SPEAKER_01"}


def test_build_word_timeline_rejects_empty_input():
    with pytest.raises(ValueError):
        build_word_timeline([], language="en", duration_s=1.0)


def test_build_word_timeline_rejects_all_unusable_words():
    with pytest.raises(ValueError):
        build_word_timeline([{"word": "x"}], language="en", duration_s=1.0)


# ------------------------------------------------------------- single speaker


def test_build_word_timeline_single_speaker_is_normal():
    # spec 7: one speaker is a normal case, not an error.
    words = [
        {"word": "Solo", "start": 0.0, "end": 0.3, "speaker": "SPEAKER_00", "score": 0.99},
        {"word": "talk", "start": 0.3, "end": 0.6, "speaker": "SPEAKER_00", "score": 0.98},
    ]
    tl = build_word_timeline(words, language="en", duration_s=1.0)
    assert len(tl.speakers) == 1
    assert tl.overlaps == []
    assert tl.hasInheritedSpeakers is False


# ------------------------------------------------------------------ round trip


def test_timeline_round_trips_through_json(tmp_path):
    tl = build_word_timeline(RAW_WORDS, language="en", duration_s=2.0)
    path = tmp_path / "words_timeline.json"
    path.write_text(tl.model_dump_json(indent=2), encoding="utf-8")

    reloaded = WordTimeline.model_validate_json(path.read_text(encoding="utf-8"))
    assert reloaded == tl

    # A consumer with no pydantic should still get a usable dict.
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schemaVersion"] == "word_timeline/1"
    assert raw["wordAlignment"] == "measured"
    assert len(raw["words"]) == 6


def test_golden_transcript_words_convert_cleanly():
    """The real 34-segment golden clip must produce a sane timeline.

    This is the closest thing to an integration test that runs in CI: it uses
    real WhisperX-aligned words from disk, with no model needed.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    src = root / "jobs" / "golden" / "transcript.json"
    if not src.exists():
        pytest.skip("jobs/golden/transcript.json not present")

    data = json.loads(src.read_text(encoding="utf-8"))
    raw = []
    for seg in data["segments"]:
        for w in seg["words"]:
            raw.append(
                {
                    "word": w["word"],
                    "start": w["start"],
                    "end": w["end"],
                    # transcript.json drops per-word speaker; use the segment's.
                    "speaker": seg["speaker"],
                }
            )

    end_ms = max(seconds_to_ms(w["end"]) for w in raw)
    tl = build_word_timeline(raw, language="en", duration_s=end_ms / 1000.0)

    assert len(tl.words) > 100
    assert len(tl.speakers) == 3
    assert [s.index for s in tl.speakers] == [0, 1, 2]
    # Speaker ids are the already-canonical ones from the golden job.
    assert [s.speakerId for s in tl.speakers] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_02",
    ]
    assert tl.words == sorted(tl.words, key=lambda w: w.startMs)
    # First word in the golden clip is "Mom," at 1.627s.
    assert tl.words[0].text.strip() == "Mom,"
    assert tl.words[0].startMs == 1627


# ------------------------------------------------------------------- schema


def test_schema_rejects_unsorted_words():
    good = build_word_timeline(RAW_WORDS, language="en", duration_s=2.0)
    payload = good.model_dump()
    payload["words"] = list(reversed(payload["words"]))
    with pytest.raises(ValidationError):
        WordTimeline.model_validate(payload)


def test_schema_rejects_word_from_undeclared_speaker():
    good = build_word_timeline(RAW_WORDS, language="en", duration_s=2.0)
    payload = good.model_dump()
    payload["words"][0]["speakerId"] = "SPEAKER_99"
    with pytest.raises(ValidationError):
        WordTimeline.model_validate(payload)


def test_schema_rejects_empty_timeline():
    with pytest.raises(ValidationError):
        WordTimeline.model_validate({"words": [], "speakers": []})


def test_schema_rejects_estimated_alignment_label():
    # The schema must not be able to claim these are estimates -- an estimated
    # timeline is a different artifact and lives in a different file.
    good = build_word_timeline(RAW_WORDS, language="en", duration_s=2.0)
    payload = good.model_dump()
    payload["wordAlignment"] = "estimated"
    with pytest.raises(ValidationError):
        WordTimeline.model_validate(payload)


def test_schema_rejects_confidence_out_of_range():
    good = build_word_timeline(RAW_WORDS, language="en", duration_s=2.0)
    payload = good.model_dump()
    payload["words"][0]["confidence"] = 1.5
    with pytest.raises(ValidationError):
        WordTimeline.model_validate(payload)
