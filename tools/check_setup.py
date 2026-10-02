#!/usr/bin/env python3
"""One command that says whether this install can actually caption a video.

    python tools/check_setup.py

Answers the questions that otherwise only surface when a jury is watching:
is ffmpeg really there, are the models on disk, is the token set, is the port
free, is the server running.

Checks are grouped. Anything in REQUIRED stops a caption job from working at
all; anything in DEGRADED means it will work but less well than advertised --
which is exactly the distinction the rest of this project refuses to blur.

Exit code 0 when nothing is required-and-missing, 1 otherwise.
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRED = "REQUIRED"
DEGRADED = "DEGRADED"
INFO = "INFO"

OK, BAD, WARN = "OK", "MISSING", "THIN"

results: list[tuple[str, str, str, str]] = []


def record(level: str, name: str, state: str, detail: str = "") -> None:
    results.append((level, name, state, detail))


def check_python() -> None:
    v = sys.version_info
    ok = v >= (3, 10)
    record(REQUIRED if ok else REQUIRED, "Python 3.10+",
           OK if ok else BAD, f"{v.major}.{v.minor}.{v.micro}")


def check_packages() -> None:
    needed = ["fastapi", "uvicorn", "pydantic", "whisperx", "faster_whisper", "pyannote"]
    missing = []
    for mod in needed:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if missing:
        record(REQUIRED, "Python packages", BAD,
               "missing: " + ", ".join(missing) + "  ->  pip install -r requirements.txt")
    else:
        record(REQUIRED, "Python packages", OK, f"{len(needed)} required modules import")


def check_ffmpeg() -> None:
    try:
        from app.pipeline.caption_export import ffmpeg_available
        available = ffmpeg_available()
    except Exception as exc:                       # pragma: no cover - defensive
        record(REQUIRED, "FFmpeg", BAD, f"could not resolve: {exc}")
        return

    if available:
        from app.pipeline.caption_export import FFMPEG
        version = ""
        try:
            out = subprocess.run([str(FFMPEG), "-version"], capture_output=True,
                                 text=True, timeout=15)
            first = (out.stdout or "").splitlines()
            version = first[0].split(" ")[2] if first else ""
        except Exception:
            pass
        record(REQUIRED, "FFmpeg", OK, f"{FFMPEG}" + (f"  ({version})" if version else ""))
    else:
        record(REQUIRED, "FFmpeg", BAD,
               "not found. Run tools/fetch-dependencies.ps1, or install it and put it on PATH")


def check_models() -> None:
    from app.api.main import CAPTION_MODELS, _hf_hub_cache, _model_dir_size

    cache = _hf_hub_cache()
    missing, total = [], 0.0
    for name in CAPTION_MODELS:
        p = cache / name
        if p.is_dir():
            total += _model_dir_size(p) / 1e6
        else:
            missing.append(name.replace("models--", ""))

    if not cache.exists():
        record(REQUIRED, "Speech models", BAD,
               f"no model cache at {cache}. They download on the first job "
               f"(~880 MB, 5-10 min)")
        return
    if missing:
        record(REQUIRED, "Speech models", BAD,
               "missing: " + ", ".join(missing)
               + "  ->  they download on the first job")
    else:
        record(REQUIRED, "Speech models", OK, f"cached, {total:.0f} MB")


def check_token() -> None:
    if os.environ.get("HF_TOKEN"):
        record(INFO, "HuggingFace token", OK, "HF_TOKEN is set")
        return
    # A cached gated model loads without authenticating, so this is only a
    # problem when something is missing. Say which, rather than crying wolf.
    from app.api.main import CAPTION_MODELS, _hf_hub_cache

    cache = _hf_hub_cache()
    cached = all((cache / n).is_dir() for n in CAPTION_MODELS)
    if cached:
        record(INFO, "HuggingFace token", WARN,
               "not set, but the gated model is already cached, so diarization works. "
               "Set it before adding a machine that has to download.")
    else:
        record(REQUIRED, "HuggingFace token", BAD,
               "HF_TOKEN not set and the gated model is not cached. Copy "
               ".env.example to .env -- see README section 6.")


def check_disk() -> None:
    try:
        free = shutil.disk_usage(ROOT).free / 1e9
    except OSError as exc:
        record(INFO, "Free disk", WARN, str(exc))
        return
    state = OK if free >= 5 else (WARN if free >= 2 else BAD)
    record(INFO, "Free disk", state, f"{free:.1f} GB")


def check_port(port: int = 8000) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        record(INFO, f"Port {port}", OK, "in use, so the server is probably running")
    else:
        record(INFO, f"Port {port}", OK, "free")


def check_server(port: int = 8000) -> None:
    """If the server is up, ask it what it thinks of itself."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=4) as r:
            body = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        record(INFO, "Server", WARN, f"not answering on :{port} (fine if not started)")
        return
    record(INFO, "Server", OK if body.get("ready") else WARN,
           f"/api/health ready={body.get('ready')}"
           + (f"  note: {body['note']}" if body.get("note") else ""))


def check_jobs() -> None:
    jobs = ROOT / "jobs_captions"
    if not jobs.is_dir():
        return
    dirs = [d for d in jobs.iterdir() if d.is_dir() and d.name.startswith("cap_")]
    if not dirs:
        record(INFO, "Caption jobs", OK, "none yet")
        return
    stuck = []
    in_flight = {"queued", "extracting_audio", "transcribing", "aligning_captions"}
    for d in dirs:
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("status") in in_flight:
            stuck.append(d.name)
            continue
        # A stale "encoding" flag only counts as stuck when the artifacts are
        # absent. The flag is never cleared by the encode itself, so a process
        # that died just after the rename leaves it behind on a finished job --
        # and calling that "stuck" would cry wolf on every completed export.
        if meta.get("exportStatus") == "encoding":
            if not ((d / "export.mp4").exists() and (d / "export.json").exists()):
                stuck.append(d.name)
    if stuck:
        record(DEGRADED, "Caption jobs", WARN,
               f"{len(dirs)} job(s); {len(stuck)} still marked in-flight: "
               + ", ".join(stuck) + " -- restart the server and they will be reaped")
    else:
        record(INFO, "Caption jobs", OK, f"{len(dirs)} job(s), none stuck")


def main() -> int:
    print()
    print("  Caption pipeline -- installation check")
    print("  " + "-" * 64)

    for fn in (check_python, check_packages, check_ffmpeg, check_models,
               check_token, check_disk, check_port, check_server, check_jobs):
        try:
            fn()
        except Exception as exc:                   # a check must never crash the check
            record(INFO, fn.__name__.replace("check_", "").replace("_", " "), WARN,
                   f"could not run: {exc}")

    mark = {OK: "  ok  ", BAD: " FAIL ", WARN: " note "}
    width = max(len(n) for _, n, _, _ in results)
    for level, name, state, detail in results:
        tag = mark[state]
        if state == BAD and level == DEGRADED:
            tag = " note "
        print(f"{tag} {name.ljust(width)}  {detail}")

    print("  " + "-" * 64)
    failed = [n for lvl, n, s, _ in results if s == BAD and lvl == REQUIRED]
    thin = [n for lvl, n, s, _ in results if s == WARN]

    if failed:
        print(f"  NOT READY - {len(failed)} required item(s) missing: "
              + ", ".join(failed))
        print()
        return 1
    print("  READY - a video can be transcribed and exported.")
    if thin:
        print(f"  {len(thin)} note(s) above; none of them block a caption job.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
