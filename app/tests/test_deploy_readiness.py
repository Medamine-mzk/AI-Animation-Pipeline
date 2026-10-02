"""Deployment readiness: the things that fail late, silently, or on another host.

Three separate defects motivated this file:

* Transcription and encoding run as subprocesses, so a server that stops takes
  its workers with it and nothing ever writes a terminal status. The job then
  reports "transcribing" forever and its page polls a progress bar that can never
  finish. reap_stale_jobs() reconciles those states at startup.
* export-status answered {"status": "encoding", "ready": true} -- a finished
  export that still claimed to be running.
* The Dockerfile bound a hardcoded port 8000, so on any host that injects PORT
  (Render, Railway, Fly, Cloud Run) the image deployed and answered nothing.
"""

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.api import captions as captions_api
from app.api import main as main_api

ROOT = pathlib.Path(__file__).resolve().parents[2]


def write_job(root: pathlib.Path, name: str, **meta) -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"id": name, **meta}), encoding="utf-8")
    return d


# ------------------------------------------------------------- stale reaping


IN_FLIGHT = list(captions_api.IN_FLIGHT)


@pytest.mark.parametrize("stage", IN_FLIGHT)
def test_an_orphaned_in_flight_job_is_failed(tmp_path, monkeypatch, stage):
    """A job left mid-flight cannot still have a live worker after a restart."""
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    monkeypatch.setattr(captions_api, "_pid_alive", lambda pid: False)
    write_job(tmp_path, "cap_orphan01", status=stage, workerPid=1234)

    report = captions_api.reap_stale_jobs()

    assert report["reaped"] == ["cap_orphan01"]
    meta = json.loads((tmp_path / "cap_orphan01" / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["progress"] == captions_api.STAGE_PCT["failed"]
    assert "restarted" in meta["error"], meta["error"]


def test_a_job_with_a_live_worker_is_left_alone(tmp_path, monkeypatch):
    """The server may start while another instance's worker is still running."""
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    monkeypatch.setattr(captions_api, "_pid_alive", lambda pid: True)
    write_job(tmp_path, "cap_live0001", status="transcribing", workerPid=999)

    assert captions_api.reap_stale_jobs()["reaped"] == []
    meta = json.loads((tmp_path / "cap_live0001" / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "transcribing"


def test_finished_jobs_are_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    monkeypatch.setattr(captions_api, "_pid_alive", lambda pid: False)
    write_job(tmp_path, "cap_done0001", status="done", workerPid=1)
    write_job(tmp_path, "cap_failed01", status="failed", workerPid=2)

    assert captions_api.reap_stale_jobs()["reaped"] == []


def test_a_3d_job_directory_is_never_reaped(tmp_path, monkeypatch):
    """Reaping must not reach outside jobs_captions; isolation is the whole point."""
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    monkeypatch.setattr(captions_api, "_pid_alive", lambda pid: False)
    write_job(tmp_path, "de988512", status="transcribing", workerPid=5)

    assert captions_api.reap_stale_jobs()["reaped"] == []
    meta = json.loads((tmp_path / "de988512" / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "transcribing"


def test_an_orphaned_export_is_failed_but_the_transcript_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    monkeypatch.setattr(captions_api, "_pid_alive", lambda pid: False)
    write_job(tmp_path, "cap_exp0001", status="done", exportStatus="encoding", exportPid=7)

    assert captions_api.reap_stale_jobs()["reaped"] == ["cap_exp0001"]
    meta = json.loads((tmp_path / "cap_exp0001" / "meta.json").read_text(encoding="utf-8"))
    assert meta["exportStatus"] == "failed"
    assert meta["exportError"]
    # The captions are still good; only the encode was lost.
    assert meta["status"] == "done"


def test_a_finished_export_is_healed_not_reaped(tmp_path, monkeypatch):
    """The encode finished; only the status flag was left behind.

    The flag is never cleared by the encode itself, so a process that died
    between the atomic rename and the status write left meta.json claiming
    "encoding" forever -- which made check_setup.py report a finished job as
    stuck. The artifacts on disk are the truth, so the flag is corrected.
    """
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    monkeypatch.setattr(captions_api, "_pid_alive", lambda pid: False)
    d = write_job(tmp_path, "cap_exp0002", status="done", exportStatus="encoding")
    (d / "export.mp4").write_bytes(b"\x00" * 32)
    (d / "export.json").write_text("{}", encoding="utf-8")

    report = captions_api.reap_stale_jobs()

    assert report["reaped"] == []
    assert report["healed"] == ["cap_exp0002"]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["exportStatus"] == "ready"
    assert meta["status"] == "done", "a finished job must not be marked failed"


def test_pid_alive_agrees_with_the_real_process():
    """The liveness probe decides what gets reaped, so it must not be a stub."""
    assert captions_api._pid_alive(0) is False
    assert captions_api._pid_alive(-1) is False
    import os
    assert captions_api._pid_alive(os.getpid()) is True


def test_a_missing_jobs_root_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path / "nope")
    assert captions_api.reap_stale_jobs()["reaped"] == []


# ------------------------------------------------------------- export status


def test_a_finished_export_reports_ready_not_encoding(tmp_path, monkeypatch):
    """The regression: {"status": "encoding", "ready": true}."""
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    d = write_job(tmp_path, "cap_stat001", status="done", exportStatus="encoding")
    (d / "export.mp4").write_bytes(b"\x00" * 16)
    (d / "export.json").write_text("{}", encoding="utf-8")

    # No `with` block: the startup reaper would reconcile this job first.
    client = TestClient(main_api.app)
    body = client.get("/api/caption-jobs/cap_stat001/export-status").json()

    assert body["ready"] is True
    assert body["status"] == "ready", body
    assert body["error"] is None
    assert body["url"]


def test_an_unfinished_export_still_reports_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    write_job(tmp_path, "cap_stat002", status="done", exportStatus="encoding")

    # Deliberately not `with TestClient(...)`: entering the context runs the
    # startup lifespan, which reaps this very job as orphaned -- correctly, since
    # nothing can still be encoding it. This endpoint is being tested in
    # isolation, so the lifecycle is bypassed.
    client = TestClient(main_api.app)
    body = client.get("/api/caption-jobs/cap_stat002/export-status").json()

    assert body["ready"] is False
    assert body["status"] == "encoding"
    assert body["url"] is None


# --------------------------------------------------------------------- health


def test_health_reports_what_is_actually_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_TOKEN", "")
    monkeypatch.setattr(main_api, "_hf_hub_cache", lambda: tmp_path)

    with TestClient(main_api.app) as client:
        body = client.get("/api/health").json()

    for key in ("ready", "ffmpeg", "hfToken", "models", "missingModels",
                "modelCacheMb", "diskFreeGb"):
        assert key in body, key
    # Nothing cached, so the token matters and the install is not ready.
    assert body["missingModels"], body
    assert body["ready"] is False
    assert body["note"] and "HF_TOKEN" in body["note"], body["note"]


def test_cached_models_need_no_token(monkeypatch, tmp_path):
    """A cached gated model loads without authenticating.

    Both real caption jobs on this machine ran with no HF_TOKEN in the
    environment, because the models were already on disk. Requiring the token
    unconditionally would report a working install as broken.
    """
    monkeypatch.setenv("HF_TOKEN", "")
    cache = tmp_path / "hub"
    for name in main_api.CAPTION_MODELS:
        blobs = cache / name / "blobs"
        blobs.mkdir(parents=True)
        # Big enough that the megabyte total is non-zero after rounding.
        (blobs / "weights.bin").write_bytes(b"\x00" * 600_000)
    monkeypatch.setattr(main_api, "_hf_hub_cache", lambda: cache)

    with TestClient(main_api.app) as client:
        body = client.get("/api/health").json()

    assert body["missingModels"] == []
    assert body["hfToken"] is False
    assert body["ready"] is body["ffmpeg"]
    assert body["modelCacheMb"] > 0


def test_model_size_is_not_double_counted(monkeypatch, tmp_path):
    """The cache holds blobs/ plus snapshots/ pointing at them; count it once."""
    monkeypatch.setenv("HF_TOKEN", "x")
    entry = tmp_path / "models--test" / "blobs"
    entry.mkdir(parents=True)
    (entry / "a.bin").write_bytes(b"\x00" * 1000)
    snap = tmp_path / "models--test" / "snapshots" / "rev"
    snap.mkdir(parents=True)
    (snap / "a.bin").write_bytes(b"\x00" * 1000)   # the duplicate

    assert main_api._model_dir_size(tmp_path / "models--test") == 1000


def test_models_without_blobs_are_still_measured(monkeypatch, tmp_path):
    """distilroberta-base keeps its files in snapshots/ with an empty blobs/."""
    entry = tmp_path / "models--test"
    (entry / "blobs").mkdir(parents=True)
    snap = entry / "snapshots" / "rev"
    snap.mkdir(parents=True)
    (snap / "model.safetensors").write_bytes(b"\x00" * 2000)

    assert main_api._model_dir_size(entry) == 2000


# --------------------------------------------------------------------- docker


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _cmd_line() -> str:
    """The last CMD in the Dockerfile, comments excluded."""
    for line in reversed(_dockerfile().splitlines()):
        stripped = line.strip()
        if stripped.startswith("CMD "):
            return stripped
    raise AssertionError("the Dockerfile has no CMD instruction")


def test_the_image_binds_the_injected_port():
    """Every PaaS injects PORT and expects 0.0.0.0.

    Hardcoding 8000 deployed an image that started and answered nothing, which
    looks exactly like a broken application.
    """
    text = _dockerfile()
    assert "${PORT:-8000}" in text, "the CMD must honour $PORT"
    assert "--host 0.0.0.0" in text, "PaaS hosts forward only to 0.0.0.0"


def test_the_image_does_not_bake_the_token_in():
    assert "COPY .env " not in _dockerfile()
    assert "COPY . /app" not in _dockerfile()


def test_the_image_serves_the_app_rather_than_printing_help():
    """The old CMD was `python -m app.pipeline.transcribe --help`: it printed
    usage text and exited, so the image never ran the application at all."""
    cmd = _cmd_line()
    assert "uvicorn" in cmd, cmd
    assert "--help" not in cmd, cmd
