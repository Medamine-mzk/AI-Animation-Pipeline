"""Speaker identity must come from the audio, or it must not be claimed.

Job b171c19f (full_mono_london.wav, 100.7s) asked for 4 voices and the transcript
carried 2, with the projects list showing "4 spk" because it reported the
*requested* number as though it had been detected. Pressing the old "map to 4
speakers" button then produced a dialogue whose speaker labels cycled
SPEAKER_00, 01, 02, 03, 00, 01... down the lines -- a confident synthetic
conversation built from arithmetic on line numbers.

Three failures are pinned here:

* splitting a recording into more speakers than it contains is refused (409);
* the requested count is never reported as the detected count;
* the player's mismatch banner uses a real comparison, not one that fires on
  every healthy job.

The deeper cause was that nothing verified the diarization result at all: pyannote
treats min/max speakers as a hint, and job b171c19f came back with 2 voices on a
run that returns 4 on every other attempt. `transcribe.py --require-speakers`
now tries several configurations, keeps the closest, and records what it got.
"""

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.api import main as main_api

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _page(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _segments(n: int, speakers: int = 2) -> list[dict]:
    return [
        {"speaker": f"SPEAKER_{i % speakers:02d}", "start": float(i),
         "end": float(i) + 0.9, "text": f"line {i}"}
        for i in range(n)
    ]


def _make_job(tmp_path: pathlib.Path, job_id: str, segs: list[dict]) -> pathlib.Path:
    d = tmp_path / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "dialogue.json").write_text(json.dumps({
        "speakers": {f"SPEAKER_{i:02d}": {"name": f"s{i}"} for i in range(4)},
        "segments": segs,
    }), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"expected_speakers": 2}), encoding="utf-8")
    return d


# ------------------------------------------- fabricating voices is refused


def test_splitting_more_speakers_than_detected_is_refused():
    """The old code assigned `i % target`, cycling labels through the lines.

    That is not a remap, it is invented dialogue: it produced a plausible-looking
    four-person scene from a two-person recording.
    """
    segs = _segments(8, speakers=2)
    with pytest.raises(main_api.SpeakerSplitRefused) as exc:
        main_api._remap_speakers_to_target(segs, 4)

    assert exc.value.detected == 2
    assert exc.value.requested == 4


def test_merging_detected_speakers_is_still_allowed():
    """Merging is honest: two real voices becoming one character on stage."""
    segs = _segments(8, speakers=4)
    merged, speakers = main_api._remap_speakers_to_target(segs, 2)

    assert len({s["speaker"] for s in merged}) == 2
    assert len(speakers) == 2
    assert [s["text"] for s in merged] == [s["text"] for s in segs], "no text may change"


def test_remap_endpoint_answers_409_with_an_actionable_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    _make_job(tmp_path, "j1", _segments(8, speakers=2))
    client = TestClient(main_api.app)

    r = client.post("/api/jobs/j1/remap", json={"target": 4})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "2 speakers" in detail
    assert "Re-detect" in detail, "the message must say what to do instead"


def test_remap_endpoint_still_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    _make_job(tmp_path, "j2", _segments(8, speakers=4))
    client = TestClient(main_api.app)

    r = client.post("/api/jobs/j2/remap", json={"target": 2})

    assert r.status_code == 200
    assert r.json()["target"] == 2


def test_a_split_attempt_leaves_the_dialogue_untouched(tmp_path, monkeypatch):
    """A refusal must not have half-applied before erroring out."""
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = _make_job(tmp_path, "j3", _segments(8, speakers=2))
    before = (d / "dialogue.json").read_text(encoding="utf-8")
    client = TestClient(main_api.app)

    client.post("/api/jobs/j3/remap", json={"target": 6})

    assert (d / "dialogue.json").read_text(encoding="utf-8") == before


# ------------------------------------------- asked vs found are not conflated


def test_the_list_never_reports_the_requested_count_as_detected(tmp_path, monkeypatch):
    """`speakers = meta["expected_speakers"]` is how b171c19f read "4 spk".

    The count shown has to come from the transcript, and the two numbers must be
    reported in separate fields.
    """
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = _make_job(tmp_path, "j4", _segments(8, speakers=2))
    (d / "status.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    client = TestClient(main_api.app)

    row = next(r for r in client.get("/api/jobs").json() if r["id"] == "j4")

    assert row["expected_speakers"] == 2
    assert row["speakers_detected"] == 2
    assert "speaker_count_matched" in row


def test_redetect_needs_uploaded_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    _make_job(tmp_path, "j5", _segments(4, speakers=2))
    client = TestClient(main_api.app)

    r = client.post("/api/jobs/j5/redetect", json={"speakers": 4})

    assert r.status_code == 409
    assert "audio" in r.json()["detail"].lower()


def test_redetect_rejects_a_demo_job(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = _make_job(tmp_path, "j6", _segments(4, speakers=2))
    (d / "input.wav").write_bytes(b"RIFF")
    (d / "provenance.json").write_text(json.dumps({"demo_mode": True}), encoding="utf-8")
    client = TestClient(main_api.app)

    r = client.post("/api/jobs/j6/redetect", json={"speakers": 4})

    assert r.status_code == 409
    assert "demo" in r.json()["detail"].lower()


def test_redetect_records_the_request_and_clears_stale_artefacts(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = _make_job(tmp_path, "j7", _segments(4, speakers=2))
    (d / "input.wav").write_bytes(b"RIFF")
    (d / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
    client = TestClient(main_api.app)

    r = client.post("/api/jobs/j7/redetect", json={"speakers": 4})

    assert r.status_code == 200
    assert r.json()["requested_speakers"] == 4
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["expected_speakers"] == 4
    assert "remapped_speakers" not in meta, "a round-robin target must not linger"
    prov = json.loads((d / "provenance.json").read_text(encoding="utf-8"))
    assert prov["redetectRequested"] == 4


# ------------------------------------------- the pipeline verifies the count


def test_transcription_is_asked_to_verify_the_speaker_count():
    """min/max were passed but the result was never checked, so a 2-voice result
    passed as a 4-voice job. The pipeline must ask for verification and record
    what it achieved."""
    src = (ROOT / "app" / "pipeline" / "transcribe.py").read_text(encoding="utf-8")
    assert "--require-speakers" in src
    assert "require_speakers" in src
    assert '"matched"' in src, "the achieved count has to be recorded, not assumed"
    assert "WARNING" in src, "a shortfall must be reported, not swallowed"

    api = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
    assert '"--require-speakers"' in api
    assert "speaker_count_matched" in api


def test_the_dead_diarization_helper_stays_deleted():
    """It never ran: `from whisperx import DiarizationPipeline` does not exist,
    `use_auth_token=` is not a parameter, and `.itertracks()` is gone from the
    DataFrame this whisperx returns. Three failures, all swallowed by
    `except Exception: return False`."""
    api = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
    # Comments keep the names on purpose, to explain the removal. Compare code.
    code = "\n".join(l.split("#", 1)[0] for l in api.splitlines())
    assert "def _attempt_diarization" not in code
    assert "use_auth_token" not in code
    assert ".itertracks(" not in code


# ------------------------------------------- the player's own comparison


def test_the_players_stop_tail_is_not_part_of_the_mismatch_test():
    """`lastLine*1000 + 1000` was compared against the audio, so the banner fired
    on every healthy job whose last line lands within a second of the audio --
    and printed both numbers identical: 'runs to 100.7s but the audio is only
    100.7s'. The tail belongs to the stop timer only."""
    src = _page("dialogue-player.html")
    assert "const lastLineEndMs = segments[segments.length - 1].end * 1000;" in src
    assert "if (lastLineEndMs > audioEndMs + 5000)" in src
    assert "const totalDuration = Math.min(lastLineEndMs + 1000, audioEndMs);" in src, (
        "the +1s grace period must stay in the timer"
    )
    assert "if (dialogueEndMs > audioEndMs)" not in src, "the broken form is back"


def test_the_player_and_the_server_agree_on_the_tolerance():
    """One tolerance, or the banner and the API argue about the same job."""
    player = _page("dialogue-player.html")
    api = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
    server_tol_ms = "DIALOGUE_OVERRUN_TOLERANCE_S = 5.0" in api
    assert server_tol_ms
    assert "audioEndMs + 5000" in player, "player tolerance must match the server's 5.0s"


def test_the_error_glyphs_can_actually_render():
    """U+26A0 U+FE0F asks for emoji presentation; `font-family: system-ui` has no
    emoji font, so both warnings rendered as an empty box -- an unreadable error
    sign on the very screen meant to report problems."""
    src = _page("dialogue-player.html")
    assert "\u26a0" not in src, "the emoji variation-selector glyph is back"
    assert "Warning:" in src