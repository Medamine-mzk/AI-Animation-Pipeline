"""A job's dialogue must belong to its own audio.

Four jobs in this repository carried the golden London dialogue over a 38.9s
upload: the player read the job's own audio.wav and the player's dialogue came
from a 110s recording, so you heard your own voice under someone else's words,
with no warning. The dialogue ran 61s past the end of the audio and the player
then sat in silence for that minute.

Three things are pinned here:

* the guard that *detects* the mismatch by measuring, so it also catches jobs
  whose markers were lost;
* the fallback that caused it, which now refuses to substitute a transcript
  without its matching audio;
* provenance that a later meta.json write cannot bury.
"""

import json
import pathlib
import struct

import pytest
from fastapi.testclient import TestClient

from app.api import main as main_api

ROOT = pathlib.Path(__file__).resolve().parents[2]


def write_wav(path: pathlib.Path, seconds: float, rate: int = 16000) -> None:
    """A real, minimal PCM WAV, so the duration reader is exercised for real."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 16-bit mono, so two bytes per sample.
    data = b"\x00" * int(rate * seconds * 2)
    byte_rate = rate * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, byte_rate, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def write_dialogue(path: pathlib.Path, last_end: float) -> None:
    path.write_text(json.dumps({
        "speakers": {"SPEAKER_00": {"name": "a"}},
        "segments": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": last_end, "text": "hi"},
        ],
    }), encoding="utf-8")


# ------------------------------------------------------------- the guard


def test_a_dialogue_longer_than_its_audio_is_flagged(tmp_path):
    write_wav(tmp_path / "audio.wav", 38.9)
    write_dialogue(tmp_path / "dialogue.json", 99.75)

    report = main_api.dialogue_vs_audio(tmp_path)

    assert report["ok"] is False
    assert round(report["audioS"], 1) == 38.9
    assert round(report["dialogueEndS"], 1) == 99.8
    assert report["overrunS"] == 60.9
    assert "different recording" in report["reason"]


def test_a_consistent_dialogue_passes(tmp_path):
    write_wav(tmp_path / "audio.wav", 38.9)
    write_dialogue(tmp_path / "dialogue.json", 36.4)

    report = main_api.dialogue_vs_audio(tmp_path)

    assert report["ok"] is True
    assert report["reason"] is None


def test_a_short_trailing_gap_is_tolerated(tmp_path):
    """Real transcripts end slightly before the audio does; that is not a fault."""
    write_wav(tmp_path / "audio.wav", 38.9)
    write_dialogue(tmp_path / "dialogue.json", 36.4)
    assert main_api.dialogue_vs_audio(tmp_path)["ok"] is True

    # 3s of slack is fine, 30s is not.
    write_dialogue(tmp_path / "dialogue.json", 41.5)
    assert main_api.dialogue_vs_audio(tmp_path)["ok"] is True
    write_dialogue(tmp_path / "dialogue.json", 68.0)
    assert main_api.dialogue_vs_audio(tmp_path)["ok"] is False


def test_input_wav_is_used_when_there_is_no_audio_wav(tmp_path):
    write_wav(tmp_path / "input.wav", 20.0)
    write_dialogue(tmp_path / "dialogue.json", 19.0)
    assert main_api.dialogue_vs_audio(tmp_path)["ok"] is True


def test_a_job_with_neither_audio_nor_dialogue_is_not_flagged(tmp_path):
    report = main_api.dialogue_vs_audio(tmp_path)
    assert report["ok"] is True
    assert report["audioS"] is None


def test_the_guard_ignores_unreadable_audio(tmp_path):
    (tmp_path / "audio.wav").write_bytes(b"not a wav at all")
    write_dialogue(tmp_path / "dialogue.json", 99.0)
    report = main_api.dialogue_vs_audio(tmp_path)
    assert report["audioS"] is None
    assert report["ok"] is True     # cannot prove a mismatch, so do not claim one


def test_the_job_endpoint_exposes_the_verdict(tmp_path, monkeypatch):
    job = tmp_path / "cap_guard01"
    job.mkdir()
    write_wav(job / "audio.wav", 10.0)
    write_dialogue(job / "dialogue.json", 40.0)
    (job / "status.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    (job / "meta.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    with TestClient(main_api.app) as client:
        body = client.get("/api/jobs/cap_guard01").json()
    assert body["consistency"]["ok"] is False


# ------------------------------------------------------ the honest fallback


@pytest.fixture
def failed_job(tmp_path):
    job = tmp_path / "cap_fail001"
    job.mkdir()
    write_wav(job / "audio.wav", 12.0)
    (job / "transcript.json").write_text(json.dumps({"segments": [{"text": "stale"}]}),
                                        encoding="utf-8")
    (job / "meta.json").write_text(json.dumps({"filename": "mine.wav"}), encoding="utf-8")
    return job


def test_failure_does_not_borrow_someone_elses_words(failed_job, monkeypatch, tmp_path):
    """The regression: the golden transcript was copied over the job's own audio."""
    golden = tmp_path / "golden_transcript.json"
    golden.write_text(json.dumps({"segments": [{"text": "London"}]}), encoding="utf-8")
    audio = tmp_path / "golden_clip.wav"
    write_wav(audio, 5.0)
    monkeypatch.setattr(main_api, "GOLDEN_TRANSCRIPT", golden)
    monkeypatch.setattr(main_api, "GOLDEN_AUDIO", audio)
    before = (failed_job / "audio.wav").read_bytes()

    main_api._on_transcribe_failure(
        failed_job, failed_job / "audio.wav", failed_job / "transcript.json",
        RuntimeError("whisperx exploded"), demo_mode=False)

    transcript = json.loads((failed_job / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["segments"] == [], "must not substitute a foreign transcript"
    assert (failed_job / "audio.wav").read_bytes() == before, "the upload must be left alone"

    prov = main_api.read_provenance(failed_job)
    assert prov["transcript_failed"] is True
    assert prov["transcript_source"] == "none"
    assert prov["demo_mode"] is False
    assert "whisperx exploded" in prov["reason"]


def test_demo_mode_pairs_the_transcript_with_matching_audio(failed_job, monkeypatch, tmp_path):
    """A substitution is fine as long as both halves move together."""
    golden = tmp_path / "golden_transcript.json"
    golden.write_text(json.dumps({"segments": [{"text": "London"}]}), encoding="utf-8")
    audio = tmp_path / "golden_clip.wav"
    write_wav(audio, 5.0)
    monkeypatch.setattr(main_api, "GOLDEN_TRANSCRIPT", golden)
    monkeypatch.setattr(main_api, "GOLDEN_AUDIO", audio)

    main_api._on_transcribe_failure(
        failed_job, failed_job / "audio.wav", failed_job / "transcript.json",
        RuntimeError("nope"), demo_mode=True)

    prov = main_api.read_provenance(failed_job)
    assert prov["demo_mode"] is True
    assert prov["transcript_source"] == "golden-london"
    # Both files came from the same clip, so the pair is coherent by construction.
    assert (failed_job / "audio.wav").read_bytes() == audio.read_bytes()
    transcript = json.loads((failed_job / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["segments"][0]["text"] == "London"


def test_demo_mode_degrades_honestly_when_the_clip_is_absent(failed_job, monkeypatch, tmp_path):
    """A missing golden clip must not resurrect the half-substitution."""
    monkeypatch.setattr(main_api, "GOLDEN_TRANSCRIPT", tmp_path / "nope.json")
    monkeypatch.setattr(main_api, "GOLDEN_AUDIO", tmp_path / "nope.wav")

    main_api._on_transcribe_failure(
        failed_job, failed_job / "audio.wav", failed_job / "transcript.json",
        RuntimeError("nope"), demo_mode=True)

    prov = main_api.read_provenance(failed_job)
    assert prov["demo_mode"] is False
    assert prov["transcript_source"] == "none"


# ------------------------------------------------------------- provenance


def test_provenance_survives_meta_being_rewritten(failed_job, monkeypatch, tmp_path):
    """The upload stub rewrites meta.json as {filename, size}.

    That is how the original transcript markers were buried and the warning
    never fired, so the record has to live somewhere else.
    """
    main_api._on_transcribe_failure(
        failed_job, failed_job / "audio.wav", failed_job / "transcript.json",
        RuntimeError("boom"), demo_mode=False)

    (failed_job / "meta.json").write_text(
        json.dumps({"filename": "mine.wav", "size": 10}), encoding="utf-8")

    prov = main_api.read_provenance(failed_job)
    assert prov["transcript_failed"] is True
    assert "boom" in prov["reason"]


def test_provenance_reads_as_empty_when_absent(tmp_path):
    assert main_api.read_provenance(tmp_path) == {}


# -------------------------------------------------------------- re-transcribe


def test_retranscribe_needs_an_existing_job(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    with TestClient(main_api.app) as client:
        r = client.post("/api/jobs/cap_nothere/retranscribe")
    assert r.status_code == 404


def test_retranscribe_needs_uploaded_audio(tmp_path, monkeypatch):
    (tmp_path / "cap_noaudio").mkdir()
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    with TestClient(main_api.app) as client:
        r = client.post("/api/jobs/cap_noaudio/retranscribe")
    assert r.status_code == 409
    assert "no uploaded audio" in r.json()["detail"]


def test_retranscribe_refuses_a_demo_job(tmp_path, monkeypatch):
    """A demo job holds the golden clip; transcribing it would fake a repair."""
    job = tmp_path / "cap_demo001"
    job.mkdir()
    write_wav(job / "input.wav", 3.0)
    main_api.write_provenance(job, demo_mode=True)
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    with TestClient(main_api.app) as client:
        r = client.post("/api/jobs/cap_demo001/retranscribe")
    assert r.status_code == 409
    assert "demo job" in r.json()["detail"]


def test_retranscribe_clears_the_mismatched_dialogue(tmp_path, monkeypatch):
    job = tmp_path / "cap_re0001"
    job.mkdir()
    write_wav(job / "input.wav", 12.0)
    write_dialogue(job / "dialogue.json", 99.0)
    (job / "status.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    monkeypatch.setattr(main_api, "run_audio_job", lambda *a, **k: None)

    with TestClient(main_api.app) as client:
        r = client.post("/api/jobs/cap_re0001/retranscribe")

    assert r.status_code == 200, r.text
    assert not (job / "dialogue.json").exists(), "the stale dialogue must be gone, not left beside a retry"
    assert main_api.read_provenance(job)["retranscribedAt"]


# --------------------------------------------- the pages that warn about it


def _page(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_capture_script_refuses_to_photograph_a_mismatch():
    src = _page("tools/capture_screenshots.mjs")
    assert "refusing to screenshot job" in src
    assert "consistency" in src
    assert "throw new Error" in src


def test_the_player_stops_when_the_audio_ends():
    """Not when the last line does, which left a minute of dead air."""
    src = _page("dialogue-player.html")
    # The 1s grace period stays, but on the last line's own end -- naming it
    # `lastLineEndMs` is what stopped the mismatch test from eating the tail.
    assert "Math.min(lastLineEndMs + 1000, audioEndMs)" in src


def test_the_players_banner_awaits_the_json_body():
    """Regression: `const c = (await fetch(...)).json()` assigns a *Promise*, so
    c.consistency was undefined and the warning was silently dead code -- the
    server returned the right payload and it was discarded. The banner existed in
    the source for hours and never once rendered."""
    src = _page("config.html")
    assert "const c = await (await fetch(" in src, "the double-await is required"
    assert "const c = (await fetch(" not in src, "the missing-await form is back"
