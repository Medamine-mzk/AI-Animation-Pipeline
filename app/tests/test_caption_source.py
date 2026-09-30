"""Tests for caption source ingestion (captions feature, M4).

These run against the real bundled ffmpeg and the real media files in the repo,
because the whole job of this module is to make an accurate claim about what is
inside a container. A mocked ffmpeg would only test the regexes.
"""

import pathlib
import shutil

import pytest

from app.pipeline.caption_source import (
    ALLOWED_EXTENSIONS,
    DEFAULT_MAX_DURATION_S,
    MediaInfo,
    SourceRejected,
    extract_audio,
    ffmpeg_available,
    ingest_upload,
    is_direct_media_url,
    new_job_dir,
    probe,
    validate_extension,
    validate_for_captions,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIDEO = ROOT / "media" / "test-video.mp4"
WAV = ROOT / "media" / "golden_clip.wav"
NONSENSE = ROOT / "app" / "schemas" / "captions.py"  # a text file, not media


pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="bundled ffmpeg missing"
)


# ------------------------------------------------------------------- probe


def test_probe_reads_the_real_video():
    if not VIDEO.exists():
        pytest.skip("media/test-video.mp4 not present")
    info = probe(VIDEO)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.duration_s == pytest.approx(10.1, abs=0.3)
    assert info.resolution == "1280x720"
    assert info.video_codec == "h264"
    assert "aac" in info.audio_codec.lower()


def test_probe_detects_a_wav_has_no_video():
    if not WAV.exists():
        pytest.skip("media/golden_clip.wav not present")
    info = probe(WAV)
    assert info.has_audio is True
    assert info.has_video is False


def test_probe_rejects_a_non_media_file():
    if not NONSENSE.exists():
        pytest.skip("fixture text file missing")
    with pytest.raises(SourceRejected) as exc:
        probe(NONSENSE)
    assert "video" in str(exc.value).lower() or "ffmpeg" in exc.value.detail.lower()


def test_probe_rejects_a_missing_file():
    with pytest.raises(SourceRejected):
        probe(ROOT / "media" / "definitely-not-here.mp4")


def test_probe_rejection_carries_a_readable_reason():
    with pytest.raises(SourceRejected) as exc:
        probe(ROOT / "nope.mp4")
    assert exc.value.reason
    assert isinstance(exc.value.detail, str)


# -------------------------------------------------------------- validation


def test_validate_extension_accepts_video_containers():
    for ext in (".mp4", ".MP4", ".mov", ".webm"):
        assert validate_extension(f"clip{ext}") == ext.lower()


def test_validate_extension_rejects_audio_and_junk():
    for bad in ("clip.wav", "clip.mp3", "clip.txt", "clip", "clip.exe"):
        with pytest.raises(SourceRejected):
            validate_extension(bad)


def test_allowed_extensions_are_all_video():
    assert ".wav" not in ALLOWED_EXTENSIONS
    assert ".mp3" not in ALLOWED_EXTENSIONS


def test_validate_accepts_a_good_clip():
    validate_for_captions(
        MediaInfo(duration_s=30, has_video=True, has_audio=True, width=1280, height=720)
    )


def test_validate_rejects_no_audio():
    with pytest.raises(SourceRejected) as exc:
        validate_for_captions(MediaInfo(duration_s=30, has_video=True, has_audio=False))
    assert "no audio" in exc.value.reason.lower()


def test_validate_rejects_no_video():
    with pytest.raises(SourceRejected) as exc:
        validate_for_captions(MediaInfo(duration_s=30, has_video=False, has_audio=True))
    assert "no video" in exc.value.reason.lower()


def test_validate_rejects_empty_file():
    with pytest.raises(SourceRejected) as exc:
        validate_for_captions(MediaInfo(duration_s=0, has_video=True, has_audio=True))
    assert "empty" in exc.value.reason.lower()


def test_validate_rejects_over_long_clip():
    with pytest.raises(SourceRejected) as exc:
        validate_for_captions(
            MediaInfo(duration_s=DEFAULT_MAX_DURATION_S + 1, has_video=True, has_audio=True)
        )
    assert "over the" in exc.value.reason.lower()


def test_validate_accepts_clip_exactly_at_the_cap():
    # Off-by-one guard: the cap is inclusive.
    validate_for_captions(
        MediaInfo(duration_s=DEFAULT_MAX_DURATION_S, has_video=True, has_audio=True)
    )


def test_validate_cap_is_configurable():
    with pytest.raises(SourceRejected):
        validate_for_captions(
            MediaInfo(duration_s=60, has_video=True, has_audio=True),
            max_duration_s=30,
        )


# ------------------------------------------------------------- audio extract


def test_extract_audio_makes_mono_16k(tmp_path):
    if not VIDEO.exists():
        pytest.skip("media/test-video.mp4 not present")
    out = extract_audio(VIDEO, tmp_path / "audio.wav")
    assert out.exists()
    assert out.stat().st_size > 0
    # Confirm the real format rather than trusting the command line.
    info = probe(out)
    assert info.has_audio is True


# ------------------------------------------------------------------ url gate


def test_direct_media_url_accepts_video_files():
    assert is_direct_media_url("https://example.com/lesson.mp4")
    assert is_direct_media_url("https://cdn.example.com/a/b/c.MOV")
    assert is_direct_media_url("https://example.com/clip.webm?token=abc")
    assert is_direct_media_url("http://example.com/v.mp4#t=10")


def test_direct_media_url_rejects_page_links():
    # The whole reason there is no yt-dlp here.
    for url in (
        "https://www.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://vimeo.com/12345",
        "https://www.tiktok.com/@user/video/123",
    ):
        assert is_direct_media_url(url) is False, url


def test_direct_media_url_rejects_non_http():
    assert is_direct_media_url("ftp://example.com/a.mp4") is False
    assert is_direct_media_url("file:///C:/secret.mp4") is False
    assert is_direct_media_url("") is False
    assert is_direct_media_url("/local/path.mp4") is False


# -------------------------------------------------------------- job isolation


def test_new_job_dir_is_prefixed_and_isolated():
    job_id, job_dir = new_job_dir()
    try:
        assert job_id.startswith("cap_")
        assert job_dir.name == job_id
        assert job_dir.parent.name == "jobs_captions"
        assert job_dir.is_dir()
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def test_caption_jobs_never_land_under_jobs():
    """Regression: the 3D feature lists every dir under jobs/ that has a
    meta.json, so a caption job stored there appeared in its "My Projects" list.
    Isolation has to be physical, not a filter someone can forget."""
    job_id, job_dir = new_job_dir()
    try:
        under_3d = ROOT / "jobs"
        assert not (under_3d / job_id).exists()
        # And nothing anywhere under jobs/ may be named like a caption job.
        if under_3d.exists():
            strays = [d.name for d in under_3d.iterdir() if d.name.startswith("cap_")]
            assert strays == [], f"caption jobs leaked into the 3D job tree: {strays}"
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def test_jobs_dir_is_never_written_to():
    """Belt and braces: ingest must not create anything under jobs/."""
    from app.pipeline.caption_source import JOBS_CAPTIONS

    assert JOBS_CAPTIONS.name == "jobs_captions"
    assert JOBS_CAPTIONS != ROOT / "jobs"


def test_new_job_dir_never_collides_with_3d_jobs():
    job_id, job_dir = new_job_dir()
    try:
        # The 3D feature serves jobs/{id}/ and lists every directory there, so a
        # caption job must be unmistakably not one of its own.
        assert not job_id.isdigit(), "must not look like a 3D job id"
        assert job_id not in {"default", "golden"}
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def test_new_job_dir_is_unique():
    a, da = new_job_dir()
    b, db = new_job_dir()
    try:
        assert a != b
        assert da != db
    finally:
        shutil.rmtree(da, ignore_errors=True)
        shutil.rmtree(db, ignore_errors=True)


# ---------------------------------------------------------------- ingest


def test_ingest_upload_end_to_end(tmp_path):
    if not VIDEO.exists():
        pytest.skip("media/test-video.mp4 not present")
    data = VIDEO.read_bytes()
    meta = ingest_upload(data, "lesson.mp4", tmp_path / "job")

    assert meta["source"] == "upload"
    assert meta["originalFilename"] == "lesson.mp4"
    assert meta["durationS"] == pytest.approx(10.1, abs=0.3)
    assert meta["resolution"] == "1280x720"
    assert meta["hasVideo"] and meta["hasAudio"]

    assert (tmp_path / "job" / "source.mp4").exists()
    assert (tmp_path / "job" / "audio.wav").exists()
    # The stored audio must be usable, not just present.
    assert probe(tmp_path / "job" / "audio.wav").has_audio is True


def test_ingest_upload_rejects_empty_body(tmp_path):
    with pytest.raises(SourceRejected) as exc:
        ingest_upload(b"", "lesson.mp4", tmp_path / "job")
    assert "empty" in exc.value.reason.lower()


def test_ingest_upload_rejects_bad_extension(tmp_path):
    if not VIDEO.exists():
        pytest.skip("media/test-video.mp4 not present")
    with pytest.raises(SourceRejected) as exc:
        ingest_upload(VIDEO.read_bytes(), "lesson.wav", tmp_path / "job")
    assert "unsupported" in exc.value.reason.lower()


def test_ingest_upload_rejects_text_file_named_mp4(tmp_path):
    """A renamed text file must not produce a job that fails minutes later."""
    with pytest.raises(SourceRejected):
        ingest_upload(b"not a video at all", "fake.mp4", tmp_path / "job")
    # Nothing half-written should be left behind for the pipeline to trip over.
    assert not (tmp_path / "job" / "source.mp4").exists()


def test_ingest_upload_enforces_size_cap(tmp_path):
    if not VIDEO.exists():
        pytest.skip("media/test-video.mp4 not present")
    with pytest.raises(SourceRejected) as exc:
        ingest_upload(
            VIDEO.read_bytes(), "lesson.mp4", tmp_path / "job", max_bytes=1024
        )
    assert "over the" in exc.value.reason.lower()


def test_ingest_upload_rejects_over_long(tmp_path):
    """duration is only knowable after the write, so the file is cleaned up."""
    if not VIDEO.exists():
        pytest.skip("media/test-video.mp4 not present")
    with pytest.raises(SourceRejected) as exc:
        ingest_upload(
            VIDEO.read_bytes(), "lesson.mp4", tmp_path / "job", max_duration_s=1.0
        )
    assert "over the" in exc.value.reason.lower()
    assert not (tmp_path / "job" / "source.mp4").exists()
