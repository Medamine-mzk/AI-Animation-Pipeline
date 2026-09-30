"""Tests for the caption export (captions feature, M6).

These run the real bundled ffmpeg. A mocked ffmpeg would only test the argument
list; the whole point of this module is producing a file a browser can actually
play, and only a real encode can show whether the pixel format, the audio mapping
and the duration are right.
"""

import json
import pathlib
import subprocess

import pytest

from app.pipeline import caption_export
from app.pipeline.caption_export import (
    DEFAULT_FPS,
    ExportFailed,
    build_command,
    export_recording,
    ffmpeg_available,
    has_audio_stream,
    has_video_stream,
    measure_decoded_duration,
    probe_duration,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
FFMPEG = ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"
VIDEO = ROOT / "media" / "test-video.mp4"

pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="bundled ffmpeg missing"
)


@pytest.fixture(scope="module")
def job_dir(tmp_path_factory):
    """A caption job dir with a genuine video and a genuine short webm capture.

    The 'recording' is produced by encoding a 2s slice of the real test video to
    webm, so the export path is exercised end to end with real media.
    """
    d = tmp_path_factory.mktemp("cap_export") / "cap_exptest"
    d.mkdir(parents=True)

    (d / "source.mp4").write_bytes(VIDEO.read_bytes())

    rec = d / "recording.webm"
    proc = subprocess.run(
        [str(FFMPEG), "-y", "-loglevel", "error",
         "-i", str(VIDEO), "-t", "2",
         "-c:v", "libvpx-vp9", "-b:v", "400k", "-an", str(rec)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert rec.exists() and rec.stat().st_size > 0
    return d


# ------------------------------------------------------------------ helpers


def test_probe_duration_reads_a_real_file(job_dir):
    assert probe_duration(job_dir / "source.mp4") == pytest.approx(10.1, abs=0.4)
    assert probe_duration(job_dir / "recording.webm") == pytest.approx(2.0, abs=0.3)


def test_probe_duration_is_safe_on_rubbish(tmp_path):
    f = tmp_path / "nope.mp4"
    assert probe_duration(f) == 0.0
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not media")
    assert probe_duration(junk) == 0.0


@pytest.fixture(scope="module")
def live_webm(tmp_path_factory):
    """A WebM with no duration header -- what MediaRecorder actually writes.

    Piping ffmpeg's output (rather than writing to a seekable file) produces the
    same "Duration: N/A" header a browser capture has. This is the case that
    broke the first export: the module required a duration that such a file
    never carries.
    """
    out = tmp_path_factory.mktemp("live") / "live.webm"
    gen = subprocess.Popen(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=2",
         "-c:v", "libvpx-vp9", "-b:v", "200k", "-f", "webm", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    data, _ = gen.communicate(timeout=300)
    out.write_bytes(data)
    assert out.stat().st_size > 1000, "could not synthesise a live webm"
    return out


def test_live_webm_really_has_no_duration_header(live_webm):
    """Guard the premise of the next test, so it cannot silently stop testing
    the case it exists for."""
    assert probe_duration(live_webm) == 0.0, (
        "expected a headerless live webm; the fixture stopped reproducing it"
    )
    assert has_video_stream(live_webm) is True


def test_decoded_duration_measures_a_headerless_webm(live_webm):
    measured = measure_decoded_duration(live_webm)
    assert measured == pytest.approx(2.0, abs=0.4), f"measured {measured}s"


def test_export_is_atomic(job_dir):
    """The output must appear only once it is complete.

    ffmpeg creates its output file before filling it, so a watcher that keys off
    the path alone can pick up a truncated mp4. The encode therefore goes to a
    .part name and is renamed only after the result has been read back.
    """
    export_recording(job_dir)
    # No partial artefacts survive a successful export.
    leftovers = [p.name for p in job_dir.iterdir() if p.name.endswith(".part")]
    assert leftovers == [], f"partial files left behind: {leftovers}"
    assert (job_dir / "export.mp4").exists()
    assert (job_dir / "export.json").exists()


def test_export_does_not_publish_a_broken_file(job_dir, tmp_path):
    """A failed encode must leave no export.mp4 for anyone to download."""
    d = tmp_path / "cap_broken"
    d.mkdir()
    (d / "source.mp4").write_bytes(VIDEO.read_bytes())
    (d / "recording.webm").write_bytes(b"not a webm")
    with pytest.raises(ExportFailed):
        export_recording(d)
    assert not (d / "export.mp4").exists()
    assert not (d / "export.json").exists()


def test_export_output_passes_a_full_decode(job_dir):
    export_recording(job_dir)
    proc = subprocess.run(
        [str(FFMPEG), "-v", "error", "-i", str(job_dir / "export.mp4"), "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"decode errors: {proc.stderr[:400]}"


def test_export_output_is_limited_range_yuv420p(job_dir):
    """yuvj420p is deprecated and a poor default for browsers; the range and
    pixel format are pinned rather than inferred from the source."""
    export_recording(job_dir)
    report = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-i", str(job_dir / "export.mp4"), "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    ).stderr
    assert "yuvj420p" not in report, f"still full-range: {report[:300]}"
    assert "yuv420p" in report


def test_export_accepts_a_headerless_recording(live_webm, tmp_path):
    """The regression: a real MediaRecorder capture has no duration header."""
    d = tmp_path / "cap_livecap01"
    d.mkdir()
    (d / "source.mp4").write_bytes(VIDEO.read_bytes())
    (d / "recording.webm").write_bytes(live_webm.read_bytes())

    result = export_recording(d)
    assert result.output.exists()
    assert result.size_bytes > 10_000
    assert result.duration_s > 0
    # The source is 10s and the capture 2s; -shortest must yield the shorter.
    assert result.duration_s < 10.0

    meta = json.loads((d / "export.json").read_text(encoding="utf-8"))
    # The manifest must show where the length came from: no header duration, and
    # the figure measured by decoding instead.
    assert meta["recordingHeaderSeconds"] == 0.0
    assert meta["recordingDecodedSeconds"] == pytest.approx(2.0, abs=0.4)
    assert meta["recordingSeconds"] == pytest.approx(2.0, abs=0.4)
    assert meta["outputSeconds"] > 0
    assert meta["sourceSeconds"] == pytest.approx(10.1, abs=0.4)


def test_export_rejects_a_recording_with_no_video_stream(job_dir, tmp_path):
    d = tmp_path / "cap_audioonly1"
    d.mkdir()
    (d / "source.mp4").write_bytes(VIDEO.read_bytes())
    proc = subprocess.run(
        [str(FFMPEG), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", "libopus", str(d / "recording.webm")],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    with pytest.raises(ExportFailed) as exc:
        export_recording(d)
    assert "no video stream" in str(exc.value).lower()


def test_has_audio_stream(job_dir, tmp_path):
    assert has_audio_stream(job_dir / "source.mp4") is True
    silent = tmp_path / "silent.webm"
    subprocess.run(
        [str(FFMPEG), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=160x90:d=1", "-c:v", "libvpx-vp9", str(silent)],
        capture_output=True, timeout=120, check=True,
    )
    assert has_audio_stream(silent) is False


def test_build_command_maps_video_from_capture_and_audio_from_source(job_dir):
    cmd = build_command(
        job_dir / "recording.webm", job_dir / "source.mp4", job_dir / "out.mp4"
    )
    assert cmd[0] == str(FFMPEG)
    assert "-y" in cmd
    # There are two -map flags; collect their values in order rather than
    # indexing off one occurrence, which lands on the second flag itself.
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    # Video from the recording (input 0) because that is where captions are burned.
    assert maps[0] == "0:v:0"
    # Audio from the original (input 1) so the soundtrack is not re-encoded.
    assert maps[1] == "1:a:0"
    assert "libx264" in cmd
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p", "browsers need yuv420p"
    assert "+faststart" in " ".join(cmd)


def test_build_command_omits_audio_when_the_source_is_silent(job_dir, tmp_path):
    silent = tmp_path / "silent.webm"
    subprocess.run(
        [str(FFMPEG), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=160x90:d=1", "-c:v", "libvpx-vp9", str(silent)],
        capture_output=True, timeout=120, check=True,
    )
    cmd = build_command(silent, silent, tmp_path / "o.mp4")
    assert "-an" in cmd, "a silent source must still export, not fail"
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert "1:a:0" not in maps


# ------------------------------------------------------------------- export


def test_export_produces_a_playable_mp4(job_dir):
    result = export_recording(job_dir)
    out = result.output
    assert out.exists()
    assert out.stat().st_size > 10_000, "output is implausibly small"
    assert out.suffix == ".mp4"

    # It must be real, decodable media of about the capture's length.
    assert result.duration_s == pytest.approx(2.0, abs=0.4)
    assert has_audio_stream(out) is True, "the original audio must be muxed in"

    # A single decode pass proves the whole file is valid, not just the header.
    proc = subprocess.run(
        [str(FFMPEG), "-v", "error", "-i", str(out), "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"decoded with errors: {proc.stderr[:500]}"


def test_export_keeps_the_original_audio(job_dir):
    result = export_recording(job_dir)
    # The capture was made with -an, so any audio in the output came from
    # source.mp4. That is the behaviour we want: the soundtrack is untouched.
    assert has_audio_stream(result.output) is True


def test_export_writes_a_manifest(job_dir):
    export_recording(job_dir)
    meta = json.loads((job_dir / "export.json").read_text(encoding="utf-8"))
    assert meta["sizeBytes"] > 0
    assert meta["outputSeconds"] == pytest.approx(2.0, abs=0.4)
    assert meta["fps"] == DEFAULT_FPS
    assert meta["hadAudio"] is True


def test_export_rejects_a_missing_recording(job_dir, tmp_path):
    empty = tmp_path / "cap_norec"
    empty.mkdir()
    (empty / "source.mp4").write_bytes(VIDEO.read_bytes())
    with pytest.raises(ExportFailed) as exc:
        export_recording(empty)
    assert "no recording" in str(exc.value).lower()


def test_export_rejects_an_empty_recording(job_dir, tmp_path):
    d = tmp_path / "cap_emptyrec"
    d.mkdir()
    (d / "source.mp4").write_bytes(VIDEO.read_bytes())
    (d / "recording.webm").write_bytes(b"")
    with pytest.raises(ExportFailed) as exc:
        export_recording(d)
    assert "empty" in str(exc.value).lower()


def test_export_rejects_a_corrupt_recording(job_dir, tmp_path):
    d = tmp_path / "cap_corrupt"
    d.mkdir()
    (d / "source.mp4").write_bytes(VIDEO.read_bytes())
    (d / "recording.webm").write_bytes(b"this is not a webm file at all")
    with pytest.raises(ExportFailed) as exc:
        export_recording(d)
    assert "no video stream" in str(exc.value).lower()


def test_export_rejects_a_missing_source(job_dir, tmp_path):
    d = tmp_path / "cap_nosrc"
    d.mkdir()
    (d / "recording.webm").write_bytes((job_dir / "recording.webm").read_bytes())
    with pytest.raises(ExportFailed) as exc:
        export_recording(d)
    assert "original video is missing" in str(exc.value).lower()


def test_failed_export_leaves_no_partial_file(job_dir, tmp_path):
    d = tmp_path / "cap_partial"
    d.mkdir()
    (d / "source.mp4").write_bytes(VIDEO.read_bytes())
    (d / "recording.webm").write_bytes(b"garbage")
    with pytest.raises(ExportFailed):
        export_recording(d)
    assert not (d / "export.mp4").exists()
    leftovers = [p.name for p in d.iterdir() if p.name.endswith(".part")]
    assert leftovers == [], f"partial file left behind: {leftovers}"


def test_export_refuses_a_3d_job_dir(tmp_path):
    """The 3D feature's directories must be unreachable from the exporter."""
    d = tmp_path / "de988512"
    d.mkdir()
    (d / "recording.webm").write_bytes(b"x")
    assert caption_export.main([str(d)]) == 2
    assert not (d / "export.mp4").exists()
    assert not (d / "export.json").exists()


def test_export_main_rejects_a_missing_dir():
    assert caption_export.main(["cap_not_here_at_all"]) == 2
