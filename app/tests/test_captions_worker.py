"""Unit tests for the caption job worker (captions feature, M4).

The WhisperX call is stubbed, so the whole flow -- including the meta.json
bookkeeping that decides whether a job reports success or failure -- runs in
milliseconds instead of loading a model.

That bookkeeping is worth testing on its own. A bug in it once marked a fully
transcribed job as failed because one status field was read off the wrong
object, after the work had already been written to disk. And running the worker
in a thread inside the API process starved the event loop until status polls
were being reset, so it is a subprocess now and directly runnable.
"""

import json

import pytest

from app.pipeline import caption_job
from app.pipeline.word_timeline import build_word_timeline

RAW_WORDS = [
    {"word": "Good", "start": 0.10, "end": 0.40, "speaker": "A", "score": 0.95},
    {"word": "morning", "start": 0.40, "end": 0.90, "speaker": "A", "score": 0.93},
    {"word": "professor", "start": 1.00, "end": 1.70, "speaker": "B", "score": 0.41},
    {"word": "how", "start": 1.70, "end": 1.90, "speaker": "B", "score": 0.88},
    {"word": "are", "start": 1.90, "end": 2.10, "speaker": "B", "score": 0.90},
    {"word": "you", "start": 2.10, "end": 2.30, "speaker": "B", "score": 0.91},
]


@pytest.fixture
def job_dir(tmp_path):
    d = tmp_path / "cap_unit0001"
    d.mkdir()
    (d / "audio.wav").write_bytes(b"RIFFfake")
    (d / "meta.json").write_text(
        json.dumps({"id": "cap_unit0001", "status": "queued"}), encoding="utf-8"
    )
    return d


@pytest.fixture
def stub_transcribe(monkeypatch):
    def fake(audio_path, output_path=None, model_size="small", language=None, **kw):
        return build_word_timeline(RAW_WORDS, language="en", duration_s=2.5)

    monkeypatch.setattr("app.pipeline.word_timeline.transcribe_words", fake)
    return fake


def meta_of(d):
    return json.loads((d / "meta.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ success


def test_worker_writes_both_artifacts_and_reports_done(job_dir, stub_transcribe):
    assert caption_job.run_job(job_dir, "small", None) == 0

    meta = meta_of(job_dir)
    assert meta["status"] == "done", meta.get("error")
    assert meta["progress"] == 100
    assert meta["error"] is None
    assert meta["traceback"] is None

    caps = json.loads((job_dir / "captions.json").read_text(encoding="utf-8"))
    assert caps["pages"]
    assert caps["wordAlignment"] == "measured"
    tl = json.loads((job_dir / "words_timeline.json").read_text(encoding="utf-8"))
    assert len(tl["words"]) == len(RAW_WORDS)


def test_worker_done_meta_reads_facts_off_the_timeline(job_dir, stub_transcribe):
    """Regression: hasConfidence lives on the timeline, not the caption set.

    Reading it off CaptionSet raised AttributeError after the artifacts were
    already on disk, so a transcribed job reported "failed".
    """
    caption_job.run_job(job_dir, "small", None)

    meta = meta_of(job_dir)
    assert "AttributeError" not in (meta.get("error") or "")
    assert meta["hasConfidence"] is True
    assert meta["hasInheritedSpeakers"] is False
    assert meta["overlapWarnings"] == 0
    assert meta["wordAlignment"] == "measured"
    assert meta["captions"]["pages"] >= 1
    assert meta["captions"]["words"] == len(RAW_WORDS)
    assert meta["language"] == "en"


def test_worker_reports_progress_through_the_stages(job_dir, stub_transcribe, monkeypatch):
    seen = []
    real = caption_job.update

    def spy(d, **kw):
        if "status" in kw:
            seen.append(kw["status"])
        return real(d, **kw)

    monkeypatch.setattr(caption_job, "update", spy)
    caption_job.run_job(job_dir, "small", None)
    assert seen == ["transcribing", "aligning_captions", "done"]


def test_worker_records_the_low_confidence_word(job_dir, stub_transcribe):
    """The 0.41-scored word must survive into the caption data for the editor."""
    caption_job.run_job(job_dir, "small", None)
    caps = json.loads((job_dir / "captions.json").read_text(encoding="utf-8"))
    scores = [w["confidence"] for p in caps["pages"] for w in p["words"]]
    assert 0.41 in scores


# ------------------------------------------------------------------ failure


def test_worker_fails_honestly_when_the_asr_raises(job_dir, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("no speech detected")

    monkeypatch.setattr("app.pipeline.word_timeline.transcribe_words", boom)
    assert caption_job.run_job(job_dir, "small", None) == 1

    meta = meta_of(job_dir)
    assert meta["status"] == "failed"
    assert "no speech detected" in meta["error"]
    # No half-finished artifact for the renderer to load.
    assert not (job_dir / "captions.json").exists()


def test_worker_fails_when_zero_words(job_dir, monkeypatch):
    """An empty timeline must fail rather than emit a blank caption bar."""

    def empty(audio_path, output_path=None, **kw):
        return build_word_timeline(RAW_WORDS, language="en", duration_s=1.0).model_copy(
            update={"words": [], "speakers": []}
        )

    monkeypatch.setattr("app.pipeline.word_timeline.transcribe_words", empty)
    assert caption_job.run_job(job_dir, "small", None) == 1

    meta = meta_of(job_dir)
    assert meta["status"] == "failed"
    assert "no speech" in meta["error"].lower()
    assert not (job_dir / "captions.json").exists()


def test_worker_fails_when_audio_is_missing(tmp_path):
    d = tmp_path / "cap_noaudio"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": d.name}), encoding="utf-8")
    assert caption_job.run_job(d, "small", None) == 1
    assert meta_of(d)["status"] == "failed"
    assert "audio.wav" in meta_of(d)["error"]


# -------------------------------------------------------------------- guard


def test_worker_refuses_a_3d_job_dir(tmp_path):
    """The 3D feature's job directories must be unreachable from this worker."""
    d = tmp_path / "de988512"
    d.mkdir()
    (d / "audio.wav").write_bytes(b"x")
    assert caption_job.main([str(d)]) == 2
    # Nothing was written into the 3D job directory.
    assert not (d / "meta.json").exists()
    assert not (d / "captions.json").exists()


def test_worker_rejects_a_missing_dir():
    assert caption_job.main(["cap_nope_not_here"]) == 2
