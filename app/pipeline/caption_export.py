"""Burn the captions into the video and produce a downloadable mp4 (M6).

WHY THIS IS AN ffmpeg WRAP, NOT A SECOND RENDERER
--------------------------------------------------
The design brief insists the export must not "look different after export". The
tempting shortcut is to re-implement the caption layout in Python or with ffmpeg's
``drawtext``/``ass`` filters, and then any difference in font metrics, wrapping or
pill position becomes a divergence between what the user approved and what they
downloaded.

Instead the browser does the drawing. It already has a <video> and a live DOM
caption, so it can:

1. measure the real rendered geometry of the caption (the DOM is the layout
   engine -- no second implementation to keep in step),
2. draw the video frame plus a canvas copy of that measured caption into an
   offscreen canvas,
3. record the offscreen canvas with MediaRecorder at a fixed fps.

That is a real-time capture, and the honest cost of that is stated in the UI
rather than hidden: a two-minute clip takes two minutes to export.

The recorded webm carries video only. The audio is taken from the original file
server-side rather than re-encoded from the recording, so export cannot degrade
the soundtrack.

Usage:
    python -m app.pipeline.caption_export jobs_captions/cap_x/recording.webm \\
        -o jobs_captions/cap_x/export.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The bundled Windows copy, fetched by tools/fetch-dependencies.ps1.
_BUNDLED_FFMPEG = ROOT / "tools" / "ffmpeg" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")


def _resolve_ffmpeg() -> str | None:
    """Find ffmpeg: the bundled copy first, then whatever is on PATH.

    This used to be a single hardcoded path to tools/ffmpeg/ffmpeg.exe, so the
    export reported "ffmpeg is missing" on every non-Windows host -- including
    the project's own Docker image, which installs ffmpeg through apt. Falling
    back to PATH is what makes the container image work at all, and respects a
    FFMPEG override for unusual setups.
    """
    override = os.environ.get("FFMPEG")
    if override:
        return override if pathlib.Path(override).exists() else shutil.which(override)
    if _BUNDLED_FFMPEG.exists():
        return str(_BUNDLED_FFMPEG)
    return shutil.which("ffmpeg")


#: None means "not found"; callers check with ffmpeg_available().
FFMPEG = _resolve_ffmpeg()

#: A short clip is capped at 180s upstream; allow generous headroom so a capture
#: that overran slightly is not rejected at the last step.
MAX_EXPORT_SECONDS = 600.0

DEFAULT_FPS = 30
DEFAULT_VIDEO_CRF = 20
DEFAULT_AUDIO_BITRATE = "192k"


class ExportFailed(RuntimeError):
    """The recording could not be turned into a usable mp4."""


@dataclass(frozen=True)
class ExportResult:
    output: pathlib.Path
    duration_s: float
    size_bytes: int
    elapsed_s: float


def ffmpeg_available() -> bool:
    return bool(FFMPEG)


def has_video_stream(path: str | pathlib.Path) -> bool:
    p = pathlib.Path(path)
    if not p.exists():
        return False
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(p), "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ": Video:" in (proc.stderr or "")


def measure_decoded_duration(path: str | pathlib.Path) -> float:
    """Length in seconds by actually decoding, not by reading the header.

    Necessary because a file written by MediaRecorder is a live WebM stream: its
    header carries ``Duration: N/A`` and there is no seek index. Asking ffmpeg to
    decode it and reporting progress is the only reliable way to learn its length.

    Decoding a live WebM also emits "non monotonically increasing dts" warnings,
    which are an artifact of probing an unindexed stream rather than corruption,
    so they are ignored here.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return 0.0
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-nostats", "-i", str(p),
             "-map", "0:v:0", "-f", "null", "-progress", "pipe:1", "-"],
            capture_output=True, text=True, timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    # -progress writes "out_time_us=10050000" style lines to stdout.
    last = 0.0
    for line in (proc.stdout or "").splitlines():
        if line.startswith("out_time_us="):
            try:
                last = max(last, int(line.split("=", 1)[1]) / 1_000_000.0)
            except ValueError:
                continue
    return round(last, 2)


def probe_duration(path: str | pathlib.Path) -> float:
    """Duration from the container header, in seconds. 0.0 when absent.

    Works for a normal file such as the uploaded source video. A MediaRecorder
    capture has ``Duration: N/A`` and returns 0.0 here -- use
    :func:`measure_decoded_duration` for those.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return 0.0
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(p), "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    for line in (proc.stderr or "").splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            if stamp.upper().startswith("N/A"):
                return 0.0
            try:
                hh, mm, ss = stamp.split(":")
                return int(hh) * 3600 + int(mm) * 60 + float(ss)
            except ValueError:
                return 0.0
    return 0.0


def has_audio_stream(path: str | pathlib.Path) -> bool:
    p = pathlib.Path(path)
    if not p.exists():
        return False
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(p), "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ": Audio:" in (proc.stderr or "")


def build_command(
    recording: pathlib.Path,
    original: pathlib.Path,
    output: pathlib.Path,
    fps: int = DEFAULT_FPS,
    crf: int = DEFAULT_VIDEO_CRF,
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE,
) -> list[str]:
    """The ffmpeg invocation, as a list so it can be asserted on in tests.

    Video comes from the recording (that is where the burned captions are) and
    audio from the original file, so the soundtrack is the untouched source
    rather than a re-encode of the captured audio. ``-shortest`` guards against a
    capture that ran slightly long.
    """
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-i", str(recording),
        "-i", str(original),
        "-map", "0:v:0",
    ]
    if has_audio_stream(original):
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", audio_bitrate]
    else:
        # A source with no audio must still produce a playable file rather than
        # failing the whole export.
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",       # required for playback in browsers
        "-crf", str(crf),
        "-preset", "medium",
        "-r", str(fps),
        # Pin the colour range and tags. Left to inference, ffmpeg propagates the
        # source's full-range bt709 flag and emits the deprecated yuvj420p, which
        # is not what we asked for and is a poor default for browser playback.
        "-color_range", "tv",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-movflags", "+faststart",   # metadata first, so it streams without a full download
        "-shortest",
        # Explicit, because the encode target is a .part file during an atomic
        # write and ffmpeg cannot infer mp4 from that extension.
        "-f", "mp4",
        str(output),
    ]
    return cmd


def export_recording(
    job_dir: str | pathlib.Path,
    recording_name: str = "recording.webm",
    output_name: str = "export.mp4",
    fps: int = DEFAULT_FPS,
    timeout_s: int = 1800,
) -> ExportResult:
    """Turn a browser capture into a captioned mp4. Returns a summary."""
    job_path = pathlib.Path(job_dir)
    recording = job_path / recording_name
    original = job_path / "source.mp4"
    output = job_path / output_name

    if not ffmpeg_available():
        raise ExportFailed(f"ffmpeg is missing (expected {FFMPEG})")
    if not recording.exists():
        raise ExportFailed(
            f"no recording found at {recording} - the capture step did not complete"
        )
    if recording.stat().st_size == 0:
        raise ExportFailed("the recording is empty (0 bytes)")
    if not original.exists():
        raise ExportFailed(f"the original video is missing at {original}")
    # A recording with no video stream cannot be a captioned video.
    if not has_video_stream(recording):
        raise ExportFailed("the recording has no video stream in it")

    # MediaRecorder writes a live WebM with no duration in the header, so fall
    # back to decoding it, and then to the original's known length. Both numbers
    # are reported so the manifest never implies a header it did not have.
    header_duration = probe_duration(recording)
    decoded_duration = measure_decoded_duration(recording)
    rec_duration = header_duration or decoded_duration
    source_duration = probe_duration(original)
    effective = rec_duration or source_duration
    if effective <= 0:
        raise ExportFailed("neither the recording nor the source has a usable length")
    if effective > MAX_EXPORT_SECONDS:
        raise ExportFailed(
            f"the export is {effective / 60:.1f} min, over the "
            f"{MAX_EXPORT_SECONDS / 60:.0f} min limit"
        )

    started = time.perf_counter()
    # Encode to a temp name and rename only on success. ffmpeg creates its output
    # file immediately and fills it in progressively, so anything watching for the
    # output path would see a partial file and could hand a truncated mp4 to a
    # user -- which fails with "moov atom not found" and looks like an encoding
    # fault rather than a race.
    tmp = output.with_name(output.name + ".part")
    tmp.unlink(missing_ok=True)
    cmd = build_command(recording, original, tmp, fps=fps)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)

    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        detail = (proc.stderr or "").strip()[:400] or "ffmpeg produced no output"
        raise ExportFailed(f"the export failed: {detail}")

    # Verify the temp file is real media *before* publishing it under the final
    # name, so a broken encode never becomes visible.
    tmp_duration = probe_duration(tmp)
    if tmp_duration <= 0:
        tmp.unlink(missing_ok=True)
        raise ExportFailed("the encoded file could not be read back as media")

    tmp.replace(output)
    out_duration = tmp_duration

    manifest_tmp = (job_path / "export.json").with_suffix(".json.part")
    meta = {
        "exportedAt": time.time(),
        "recordingSeconds": rec_duration,
        # A browser capture legitimately has no header duration; recording both
        # keeps the manifest honest about where the number came from.
        "recordingHeaderSeconds": header_duration,
        "recordingDecodedSeconds": decoded_duration,
        "sourceSeconds": source_duration,
        "outputSeconds": round(out_duration, 2),
        "sizeBytes": output.stat().st_size,
        "fps": fps,
        "hadAudio": has_audio_stream(output),
    }
    manifest_tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    manifest_tmp.replace(job_path / "export.json")

    return ExportResult(
        output=output,
        duration_s=out_duration,
        size_bytes=output.stat().st_size,
        elapsed_s=time.perf_counter() - started,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Burn captions into a recorded capture (M6)"
    )
    parser.add_argument("job_dir", help="path to jobs_captions/cap_xxxx")
    parser.add_argument("-o", "--output-name", default="export.mp4")
    parser.add_argument("--recording", default="recording.webm")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    args = parser.parse_args(argv)

    job_dir = pathlib.Path(args.job_dir)
    if not job_dir.is_absolute():
        job_dir = ROOT / job_dir
    if not job_dir.is_dir():
        print(f"[export] no such job dir: {job_dir}", file=sys.stderr)
        return 2
    if not job_dir.name.startswith("cap_"):
        print(
            f"[export] refusing to write into {job_dir}: caption exports only "
            "belong in a jobs_captions/cap_* directory",
            file=sys.stderr,
        )
        return 2

    try:
        result = export_recording(
            job_dir, recording_name=args.recording,
            output_name=args.output_name, fps=args.fps,
        )
    except ExportFailed as exc:
        _mark_failed(job_dir, str(exc))
        print(f"[export] FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        f"[export] wrote {result.output} "
        f"({result.size_bytes / 1e6:.1f} MB, {result.duration_s:.1f}s, "
        f"{result.elapsed_s:.1f}s to encode)"
    )
    return 0


def _mark_failed(job_dir: pathlib.Path, reason: str) -> None:
    path = job_dir / "meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}
    meta["exportError"] = reason
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
