"""Video / audio source ingestion for the captions feature (M4, spec 3.1).

Turns an uploaded file or a direct media URL into the two artifacts the rest of
the captions pipeline needs:

    source.mp4   the video, for the preview and the final burn-in
    audio.wav    16 kHz mono, what WhisperX wants

Everything here refuses rather than guesses. A container ffmpeg cannot read, a
clip with no audio track, or one past the duration cap is rejected with a reason
the user can act on -- a caption job that cannot possibly succeed is worse than
an upload that fails immediately, because the failure surfaces minutes later as
"0 words found".

Deliberately *not* a YouTube scraper: see ``feature-dialogue-captions.md`` and
the M4 decision to support direct media URLs only. No ``yt-dlp``, no extra
dependency, nothing to keep patched.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"

#: Caption jobs live in their own tree, not under ``jobs/``.
#:
#: The 3D feature's ``GET /api/jobs`` walks every directory under ``jobs/`` and
#: lists any that has a ``meta.json`` (app/api/main.py, list_jobs), skipping only
#: golden/picker/default. Storing caption jobs there made them show up in the 3D
#: feature's "My Projects" list -- a cross-feature regression this project
#: explicitly must not cause. A sibling directory needs no defensive filter in
#: the 3D code, so that file stays untouched.
JOBS_CAPTIONS = ROOT / "jobs_captions"

#: spec 6 -- "short video", hard-capped so a surprise upload cannot wedge the
#: job queue. Configurable per call.
DEFAULT_MAX_DURATION_S = 180.0

#: 2-3 minutes of 720p is comfortably under this, and keeps a stray full-length
#: lesson from filling the disk.
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Containers we will hand to a browser <video> element.
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}

#: For URL input: only direct media. A YouTube watch page is HTML, not video,
#: and treating "it returned 200" as a valid video is how you end up trying to
#: transcribe a web page.
ALLOWED_URL_CONTENT_TYPES = ("video/", "application/octet-stream", "binary/octet-stream")


class SourceRejected(ValueError):
    """The input cannot produce captions. Carries a user-facing reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class MediaInfo:
    """What ffmpeg says is actually inside the file."""

    duration_s: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    video_codec: str = ""
    audio_codec: str = ""

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else "unknown"


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video:\s*([A-Za-z0-9_]+).*?(\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio:\s*([A-Za-z0-9_]+)")


def ffmpeg_available() -> bool:
    return FFMPEG.exists()


def probe(path: str | Path) -> MediaInfo:
    """Read duration and stream types out of a media file.

    Parses ffmpeg's stderr rather than shelling out to ``ffprobe``, which is not
    bundled here. ffmpeg exits non-zero when given no output target, which is
    the cheapest way to make it describe the input and stop.
    """
    if not ffmpeg_available():
        raise SourceRejected(
            "ffmpeg is missing",
            f"expected the bundled binary at {FFMPEG}",
        )
    p = Path(path)
    if not p.exists():
        raise SourceRejected("file does not exist", str(p))

    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(p), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceRejected("could not read the file", "ffmpeg timed out") from exc

    # ffmpeg always writes the stream report to stderr, and exits 1 because no
    # output was produced. A parseable report is the success signal.
    err = proc.stderr or ""
    m = _DURATION_RE.search(err)
    if not m:
        raise SourceRejected(
            "this does not look like a video file",
            "ffmpeg could not read any stream from it",
        )

    duration_s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vm = _VIDEO_RE.search(err)
    am = _AUDIO_RE.search(err)

    return MediaInfo(
        duration_s=round(duration_s, 3),
        has_video=vm is not None,
        has_audio=am is not None,
        width=int(vm.group(2)) if vm else 0,
        height=int(vm.group(3)) if vm else 0,
        video_codec=vm.group(1) if vm else "",
        audio_codec=am.group(1) if am else "",
    )


def validate_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise SourceRejected(
            f"unsupported file type '{ext or filename}'",
            "use one of: " + ", ".join(sorted(ALLOWED_EXTENSIONS)),
        )
    return ext


def validate_for_captions(info: MediaInfo, max_duration_s: float = DEFAULT_MAX_DURATION_S) -> None:
    """Refuse anything that cannot yield captions, with a reason worth reading."""
    if not info.has_audio:
        raise SourceRejected(
            "this file has no audio track",
            "captions are generated from speech, so a silent video cannot be captioned",
        )
    if not info.has_video:
        raise SourceRejected(
            "this file has no video track",
            "the preview and the export both need a video to draw captions over",
        )
    if info.duration_s <= 0:
        raise SourceRejected("this file appears to be empty", "reported duration is zero")
    if info.duration_s > max_duration_s:
        raise SourceRejected(
            f"clip is {info.duration_s / 60:.1f} min long, over the "
            f"{max_duration_s / 60:.0f} min limit",
            "trim it, or raise the cap if this should be allowed",
        )


def new_job_dir() -> tuple[str, Path]:
    """Create an isolated ``jobs_captions/cap_xxxx/`` directory.

    The ``cap_`` prefix plus the separate tree is the whole isolation strategy.
    The 3D feature lists everything under ``jobs/`` that has a ``meta.json``, so
    a caption job must be unable to reach that directory at all rather than
    relying on a filter nobody remembers to maintain.
    """
    job_id = f"cap_{uuid.uuid4().hex[:8]}"
    job_dir = JOBS_CAPTIONS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_id, job_dir


def extract_audio(
    source: str | Path, out_wav: str | Path, sample_rate: int = 16000
) -> Path:
    """Extract a mono PCM wav for WhisperX."""
    out = Path(out_wav)
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-i", str(source),
        "-vn",                  # drop any video
        "-ac", "1",             # mono
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise SourceRejected(
            "could not extract audio from this video",
            (proc.stderr or "").strip()[:300] or "ffmpeg produced no output",
        )
    return out


def ingest_upload(
    data: bytes,
    filename: str,
    job_dir: str | Path,
    max_duration_s: float = DEFAULT_MAX_DURATION_S,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> dict:
    """Persist an uploaded video and extract its audio.

    Returns a dict for the job's ``meta.json``. Raises :class:`SourceRejected`
    with a readable reason for anything unusable.
    """
    if not data:
        raise SourceRejected("the uploaded file is empty", "0 bytes received")
    if len(data) > max_bytes:
        raise SourceRejected(
            f"file is {len(data) / 1e6:.0f} MB, over the {max_bytes / 1e6:.0f} MB limit",
            "compress it or trim it before uploading",
        )
    ext = validate_extension(filename)

    job_path = Path(job_dir)
    job_path.mkdir(parents=True, exist_ok=True)
    # Always store as .mp4: the browser preview needs one consistent type, and a
    # .mov that some browsers refuse would otherwise become a silent failure.
    video_path = job_path / "source.mp4"
    video_path.write_bytes(data)

    try:
        info = probe(video_path)
    except SourceRejected:
        video_path.unlink(missing_ok=True)
        raise

    try:
        validate_for_captions(info, max_duration_s=max_duration_s)
        extract_audio(video_path, job_path / "audio.wav")
    except SourceRejected:
        video_path.unlink(missing_ok=True)
        raise

    return {
        "source": "upload",
        "originalFilename": filename,
        "sourceBytes": len(data),
        "containerExt": ext,
        "durationS": info.duration_s,
        "hasVideo": info.has_video,
        "hasAudio": info.has_audio,
        "resolution": info.resolution,
        "videoCodec": info.video_codec,
        "audioCodec": info.audio_codec,
    }


def is_direct_media_url(url: str) -> bool:
    """True for an http(s) URL that plausibly serves a video file itself.

    Rejects the YouTube case on purpose: a watch page is HTML, and there is no
    scraper here to turn it into video.
    """
    if not url:
        return False
    lowered = url.strip().lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    path = lowered.split("?", 1)[0].split("#", 1)[0]
    if Path(path).suffix in ALLOWED_EXTENSIONS:
        return True
    # Hosts that only ever serve web pages, not media files.
    for host in ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
                 "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com"):
        if host in lowered:
            return False
    return True


def ingest_url(
    url: str,
    job_dir: str | Path,
    max_duration_s: float = DEFAULT_MAX_DURATION_S,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    timeout_s: int = 120,
) -> dict:
    """Fetch a direct media URL, then ingest it exactly like an upload."""
    if not is_direct_media_url(url):
        raise SourceRejected(
            "that link is not a direct video file",
            "paste a link to an .mp4/.mov/.webm file. Page links such as YouTube "
            "are not supported.",
        )

    import requests  # already a project dependency

    job_path = Path(job_dir)
    job_path.mkdir(parents=True, exist_ok=True)
    tmp = job_path / "download.tmp"

    try:
        with requests.get(
            url, stream=True, timeout=timeout_s, allow_redirects=True
        ) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if resp.status_code != 200:
                raise SourceRejected(
                    f"the server returned HTTP {resp.status_code}",
                    "check the link is still valid and publicly reachable",
                )
            if not content_type.startswith(ALLOWED_URL_CONTENT_TYPES):
                raise SourceRejected(
                    f"that link serves '{content_type or 'an unknown type'}', not a video",
                    "it must be a direct link to a video file, not a web page",
                )

            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise SourceRejected(
                    f"file is {int(declared) / 1e6:.0f} MB, over the "
                    f"{max_bytes / 1e6:.0f} MB limit",
                    None,
                )

            written = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    written += len(chunk)
                    # Enforce the cap on the stream, not just the header: a
                    # server can lie about Content-Length.
                    if written > max_bytes:
                        raise SourceRejected(
                            f"download passed the {max_bytes / 1e6:.0f} MB limit "
                            "and was stopped",
                            None,
                        )
                    fh.write(chunk)
    except SourceRejected:
        tmp.unlink(missing_ok=True)
        raise
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        raise SourceRejected("could not download that link", str(exc)[:200]) from exc

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise SourceRejected("the link returned an empty file", None)

    name = Path(url.split("?", 1)[0]).name or "source.mp4"
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        name += ".mp4"
    try:
        meta = ingest_upload(
            tmp.read_bytes(), name, job_path,
            max_duration_s=max_duration_s, max_bytes=max_bytes,
        )
    finally:
        tmp.unlink(missing_ok=True)

    meta["source"] = "url"
    meta["sourceUrl"] = url
    return meta
