"""Captions feature HTTP surface (new page + job API).

Kept in its own module so the existing 3D animation API in ``app/api/main.py``
needs exactly one line to adopt it::

    from app.api.captions import router as captions_router
    app.include_router(captions_router)

Nothing here reads or writes the 3D feature's job directory (``jobs/{id}/``).
Caption jobs live in their own ``jobs_captions/cap_{id}/`` tree. That is not
tidiness: the 3D feature's ``GET /api/jobs`` lists every directory under
``jobs/`` that has a ``meta.json``, so a caption job stored there would appear
in the 3D "My Projects" list. Keeping them apart means the 3D code needs no
defensive filter and stays untouched.

Routes
------
``GET  /captions.html``      the renderer page
``/captions-demo/*``         committed demo fixture, so the page is openable
                             before any upload flow exists
``/caption-jobs/*``          job artifacts (captions.json, source.mp4, ...)
``POST /api/caption-jobs``   video or direct-media URL -> caption job
``GET  /api/caption-jobs/{id}``  status
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.pipeline.caption_source import (
    DEFAULT_MAX_DURATION_S,
    SourceRejected,
    ingest_upload,
    ingest_url,
    new_job_dir,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEMO_DIR = ROOT / "captions_demo"
PAGE = ROOT / "captions.html"
JOBS = ROOT / "jobs_captions"

#: Mirror of the spec's stage names, for an honest progress readout.
STAGE_PCT = {
    "queued": 5,
    "extracting_audio": 15,
    "transcribing": 45,
    "aligning_captions": 85,
    "done": 100,
    "failed": 100,
}

router = APIRouter()

#: WhisperX sizes that have been exercised on CPU in this repo. Anything else is
#: rejected rather than passed through, so a typo cannot trigger a multi-gigabyte
#: model download in a background thread.
ALLOWED_MODELS = {"tiny", "base", "small"}


@router.get("/captions.html", include_in_schema=False)
def captions_page() -> FileResponse:
    """The caption renderer.

    Served as an explicit route rather than a root static mount so the server
    never exposes the whole repository, matching how the other pages are served.
    """
    if not PAGE.exists():
        # Surfaced as a plain 404 by FastAPI rather than a confusing 500.
        raise FileNotFoundError("captions.html is missing from the repository")
    return FileResponse(str(PAGE))


# The demo fixture is committed, unlike jobs_captions/ (gitignored), so a fresh
# checkout can open the page and see captions before running any upload flow.
if DEMO_DIR.exists():
    router.mount(
        "/captions-demo", StaticFiles(directory=str(DEMO_DIR)), name="captions-demo"
    )

# Job artifacts for the renderer to fetch. Its own mount, so the 3D feature's
# /jobs mount is left alone.
JOBS.mkdir(parents=True, exist_ok=True)
router.mount(
    "/caption-jobs", StaticFiles(directory=str(JOBS)), name="caption-jobs"
)


# ------------------------------------------------------------------ job store


def _job_dir(job_id: str) -> pathlib.Path:
    return JOBS / job_id


def _read_meta(job_id: str) -> dict:
    path = _job_dir(job_id) / "meta.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta(job_id: str, data: dict) -> None:
    path = _job_dir(job_id) / "meta.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _update(job_id: str, **fields) -> None:
    meta = _read_meta(job_id)
    meta.update(fields)
    meta["updatedAt"] = time.time()
    _write_meta(job_id, meta)


# ------------------------------------------------------------- the pipeline


def _spawn_caption_job(job_id: str, model_size: str, language: str | None) -> int:
    """Run the ASR in a separate process.

    A thread is not enough: WhisperX, faster-whisper and pyannote hold the GIL
    while doing CPU work, which starves the asyncio event loop in this process
    and makes the server stop answering status polls partway through every job.
    A subprocess also means a model crash or an out-of-memory kill cannot take
    the API down with it.

    Returns the child pid, or 0 if it could not be started.
    """
    cmd = [
        sys.executable, "-m", "app.pipeline.caption_job",
        str(_job_dir(job_id)), "--model", model_size,
    ]
    if language:
        cmd += ["--language", language]

    log_path = _job_dir(job_id) / "worker.log"
    try:
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
    except OSError as exc:
        _update(
            job_id,
            status="failed",
            progress=100,
            error=f"could not start the transcription worker: {exc}",
        )
        return 0

    _update(job_id, workerPid=proc.pid)
    return proc.pid


# ------------------------------------------------------------------ endpoints


@router.post("/api/caption-jobs")
async def create_caption_job(request: Request):
    """Ingest a video and queue transcription.

    Two request shapes:

    * raw body with ``X-Filename``, matching the existing ``POST /api/jobs``
      convention so the two upload paths feel the same to a user
    * ``{"url": "https://..."}`` for a direct media link

    Ingestion is synchronous because it is fast (a copy plus one ffmpeg pass) and
    it is the only part that can give an immediate, actionable rejection. Only
    the ASR is backgrounded.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()

    # Validate the cheap request-level options *before* creating a job dir, so a
    # bad header cannot leave an orphan job directory behind.
    model_size = request.headers.get("X-Caption-Model") or "small"
    if model_size not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported model '{model_size}'; choose one of "
                   + ", ".join(sorted(ALLOWED_MODELS)),
        )
    language = request.headers.get("X-Caption-Language") or None

    if content_type == "application/json":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="body is not valid JSON") from exc
        url = (body.get("url") or "").strip()
        if not url:
            raise HTTPException(
                status_code=400, detail="provide a non-empty 'url' for a direct video link"
            )
        max_duration = float(body.get("maxDurationS") or DEFAULT_MAX_DURATION_S)
        job_id, job_dir = new_job_dir()
        try:
            meta = ingest_url(url, job_dir, max_duration_s=max_duration)
        except SourceRejected as exc:
            _purge(job_dir)
            raise HTTPException(
                status_code=400, detail=_reason(exc)
            ) from exc
    else:
        filename = (request.headers.get("X-Filename") or "").strip()
        if not filename:
            raise HTTPException(
                status_code=400,
                detail="X-Filename header is required so the container can be checked",
            )
        data = await request.body()
        job_id, job_dir = new_job_dir()
        try:
            meta = ingest_upload(data, filename, job_dir)
        except SourceRejected as exc:
            _purge(job_dir)
            raise HTTPException(status_code=400, detail=_reason(exc)) from exc

    meta.update(
        {
            "id": job_id,
            "kind": "caption",
            "status": "queued",
            "progress": STAGE_PCT["queued"],
            "createdAt": time.time(),
            "updatedAt": time.time(),
            "error": None,
        }
    )
    _write_meta(job_id, meta)

    pid = _spawn_caption_job(job_id, model_size, language)
    if not pid:
        raise HTTPException(
            status_code=500,
            detail="the transcription worker could not be started; see the job's meta.json",
        )

    return {"jobId": job_id, "status": "queued", **{
        k: meta[k] for k in ("durationS", "resolution", "source") if k in meta
    }}


def _reason(exc: SourceRejected) -> str:
    return f"{exc.reason} ({exc.detail})" if exc.detail else exc.reason


def _purge(job_dir: pathlib.Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)


@router.get("/api/caption-jobs")
def list_caption_jobs():
    """Caption jobs only. Never mixes in the 3D feature's jobs."""
    out = []
    if JOBS.exists():
        for d in JOBS.iterdir():
            if not d.is_dir() or not d.name.startswith("cap_"):
                continue
            meta = _read_meta(d.name)
            if meta:
                out.append(
                    {
                        "id": meta.get("id", d.name),
                        "status": meta.get("status", "unknown"),
                        "progress": meta.get("progress", 0),
                        "createdAt": meta.get("createdAt", 0),
                        "durationS": meta.get("durationS"),
                        "originalFilename": meta.get("originalFilename"),
                        "source": meta.get("source"),
                        "captions": meta.get("captions"),
                        "error": meta.get("error"),
                    }
                )
    out.sort(key=lambda j: j.get("createdAt") or 0, reverse=True)
    return out


@router.get("/api/caption-jobs/{job_id}")
def get_caption_job(job_id: str):
    if not job_id.startswith("cap_"):
        raise HTTPException(
            status_code=400,
            detail=f"'{job_id}' is not a caption job id (caption ids start with cap_)",
        )
    if not _job_dir(job_id).exists():
        raise HTTPException(status_code=404, detail=f"caption job {job_id} not found")
    meta = _read_meta(job_id)
    if not meta:
        raise HTTPException(
            status_code=500, detail=f"caption job {job_id} has no meta.json"
        )
    return meta


@router.delete("/api/caption-jobs/{job_id}")
def delete_caption_job(job_id: str):
    if not job_id.startswith("cap_"):
        raise HTTPException(status_code=400, detail="not a caption job id")
    d = _job_dir(job_id)
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"caption job {job_id} not found")
    _purge(d)
    return {"deleted": job_id}
