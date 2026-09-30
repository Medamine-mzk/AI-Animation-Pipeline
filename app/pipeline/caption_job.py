"""Caption job worker, run as a separate process (captions feature, M4).

Why a process and not a thread or a FastAPI background task:

WhisperX, faster-whisper and pyannote are CPU-bound native extensions that hold
the GIL for long stretches. Run in a thread inside the API process, they starve
the asyncio event loop and the server stops answering status polls mid-job --
which is exactly what a user needs answered. This was observed, not assumed.

Running as a subprocess also means:

* the API stays responsive for the whole transcription
* a crash or an OOM in the model stack cannot take the server down
* the job is inspectable and re-runnable from the command line

Usage:
    python -m app.pipeline.caption_job jobs/cap_ab12cd34 --model small
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[2]

STAGE_PCT = {
    "queued": 5,
    "transcribing": 45,
    "aligning_captions": 85,
    "done": 100,
    "failed": 100,
}


def read_meta(job_dir: pathlib.Path) -> dict:
    path = job_dir / "meta.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_meta(job_dir: pathlib.Path, data: dict) -> None:
    (job_dir / "meta.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def update(job_dir: pathlib.Path, **fields) -> None:
    meta = read_meta(job_dir) or {"id": job_dir.name}
    meta.update(fields)
    meta["updatedAt"] = time.time()
    write_meta(job_dir, meta)


def run_job(job_dir: pathlib.Path, model_size: str, language: str | None) -> int:
    """Transcribe, then build caption pages. Returns 0 on success, 1 on failure."""
    from app.pipeline.captions import build_caption_set, summary
    from app.pipeline.word_timeline import transcribe_words

    audio = job_dir / "audio.wav"
    if not audio.exists():
        update(
            job_dir,
            status="failed",
            progress=100,
            error="audio.wav is missing - ingestion did not complete",
        )
        return 1

    try:
        update(job_dir, status="transcribing", progress=STAGE_PCT["transcribing"])
        timeline = transcribe_words(str(audio), model_size=model_size, language=language)
        (job_dir / "words_timeline.json").write_text(
            timeline.model_dump_json(indent=2), encoding="utf-8"
        )

        update(job_dir, status="aligning_captions", progress=STAGE_PCT["aligning_captions"])
        if not timeline.words:
            raise RuntimeError(
                "the transcription found no speech in this clip's audio, so there "
                "is nothing to caption"
            )
        caption_set = build_caption_set(timeline)
        (job_dir / "captions.json").write_text(
            caption_set.model_dump_json(indent=2), encoding="utf-8"
        )

        update(
            job_dir,
            status="done",
            progress=100,
            captions=summary(caption_set),
            language=caption_set.language,
            wordAlignment=caption_set.wordAlignment,
            # These three are facts about the ASR, so they come off the timeline.
            # Reading them off the caption set both crashes and, worse, can mark
            # a fully transcribed job as failed after the work is on disk.
            hasConfidence=timeline.hasConfidence,
            hasInheritedSpeakers=timeline.hasInheritedSpeakers,
            overlapWarnings=len(timeline.overlaps),
            error=None,
            traceback=None,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - the worker must always report why
        update(
            job_dir,
            status="failed",
            progress=100,
            error=f"{type(exc).__name__}: {exc}",
            failedStage="transcribe",
            traceback=traceback.format_exc()[-2000:],
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one caption job")
    parser.add_argument("job_dir", help="path to jobs/cap_xxxx")
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default=None)
    args = parser.parse_args(argv)

    job_dir = pathlib.Path(args.job_dir)
    if not job_dir.is_absolute():
        job_dir = ROOT / job_dir
    if not job_dir.is_dir():
        print(f"[caption_job] no such job dir: {job_dir}", file=sys.stderr)
        return 2
    # Refuse to write into the 3D feature's job directories. Caption jobs live
    # in jobs_captions/cap_*, and that feature lists everything under jobs/ that
    # has a meta.json, so a stray run must not plant a caption job in there.
    if not job_dir.name.startswith("cap_") or job_dir.parent.name != "jobs_captions":
        print(
            f"[caption_job] refusing to run against {job_dir}: caption jobs must "
            "live in a jobs_captions/cap_* directory",
            file=sys.stderr,
        )
        return 2

    code = run_job(job_dir, args.model, args.language)
    print(f"[caption_job] {job_dir.name} -> {read_meta(job_dir).get('status')}")
    return code


if __name__ == "__main__":
    sys.exit(main())
