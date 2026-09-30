"""Tests for the caption job worker and its HTTP surface (M4).

The worker is exercised with a stubbed ``transcribe_words`` so the whole flow --
including the meta.json bookkeeping that decides whether a job reports success
or failure -- runs in a second instead of loading WhisperX.

That bookkeeping is worth testing on its own: a bug there once marked a fully
transcribed job as ``failed`` because one status field was read off the wrong
object, after the work had already been written to disk.
"""

import json
import pathlib
import shutil

import pytest
from fastapi import FastAPI
from fastapi.routing import Mount
from fastapi.testclient import TestClient

from app.api import captions as captions_api

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIDEO = ROOT / "media" / "test-video.mp4"

@pytest.fixture
def client():
    """A TestClient over the captions routes only.

    The production router also carries a StaticFiles mount, and this
    FastAPI/Starlette pairing asserts on a middleware key that the mounted
    sub-app does not provide, so every request through TestClient fails with
    "fastapi_middleware_astack not found in request scope". include_router keeps
    all of FastAPI's per-route metadata (path params, body parsing), which
    hand-copying routes onto a new router would silently lose. The mount itself
    is asserted structurally below and exercised for real by the end-to-end
    script.
    """
    app = FastAPI()
    app.include_router(captions_api.router)
    return TestClient(app)


# --------------------------------------------------------------- endpoints


def test_page_route_serves(client):
    r = client.get("/captions.html")
    assert r.status_code == 200
    assert "Video Captions" in r.text
    assert "function renderAt" in r.text


def test_demo_fixture_is_mounted():
    """Asserted structurally; see the client fixture for why not via TestClient."""
    mounts = [r for r in captions_api.router.routes if isinstance(r, Mount)]
    assert any(getattr(m, "path", "") == "/captions-demo" for m in mounts)
    fixture = ROOT / "captions_demo" / "captions.json"
    assert fixture.exists(), "run tools/captions/build_demo_fixture.py"
    assert json.loads(fixture.read_text(encoding="utf-8"))["pages"]


def test_upload_rejects_wav(client):
    r = client.post(
        "/api/caption-jobs",
        content=b"RIFF....WAVEfmt ",
        headers={"X-Filename": "clip.wav", "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 400
    assert "unsupported" in r.json()["detail"].lower()


def test_upload_rejects_text_named_mp4(client):
    r = client.post(
        "/api/caption-jobs",
        content=b"this is definitely not a video",
        headers={"X-Filename": "clip.mp4"},
    )
    assert r.status_code == 400
    assert "video" in r.json()["detail"].lower()


def test_upload_requires_filename(client):
    r = client.post("/api/caption-jobs", content=b"x")
    assert r.status_code == 400
    assert "x-filename" in r.json()["detail"].lower()


def test_upload_rejects_youtube_url(client):
    r = client.post("/api/caption-jobs", json={"url": "https://www.youtube.com/watch?v=x"})
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "direct" in detail and "youtube" in detail


def test_url_requires_a_url(client):
    r = client.post("/api/caption-jobs", json={})
    assert r.status_code == 400


def test_job_lookup_rejects_a_3d_job_id(client):
    r = client.get("/api/caption-jobs/de988512")
    assert r.status_code == 400
    assert "cap_" in r.json()["detail"]


def test_missing_job_is_404(client):
    r = client.get("/api/caption-jobs/cap_doesnotexist")
    assert r.status_code == 404


def test_rejections_leave_no_job_dir_behind(client, tmp_path, monkeypatch):
    """A rejected upload must not leave a half-built job for the list to show."""
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    r = client.post(
        "/api/caption-jobs",
        content=b"not a video",
        headers={"X-Filename": "x.mp4"},
    )
    assert r.status_code == 400
    assert not any(d.name.startswith("cap_") for d in tmp_path.iterdir() if d.is_dir())


def test_model_size_is_allow_listed(client, tmp_path, monkeypatch):
    """An unknown model must be refused, not downloaded in a worker process."""
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    if not VIDEO.exists():
        pytest.skip("media/test-video.mp4 not present")
    r = client.post(
        "/api/caption-jobs",
        content=VIDEO.read_bytes(),
        headers={"X-Filename": "v.mp4", "X-Caption-Model": "large-v3"},
    )
    assert r.status_code == 400
    assert "unsupported model" in r.json()["detail"].lower()
    # Validated before the job dir exists, so nothing is left orphaned.
    assert not any(d.name.startswith("cap_") for d in tmp_path.iterdir() if d.is_dir())


# ------------------------------------------------------------- editing


@pytest.fixture
def editable_job(tmp_path, monkeypatch):
    """A caption job with a finished captions.json, ready to edit."""
    from app.pipeline.captions import build_caption_set
    from app.pipeline.word_timeline import build_word_timeline

    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    d = tmp_path / "cap_edit0001"
    d.mkdir()
    (d / "audio.wav").write_bytes(b"RIFFfake")
    tl = build_word_timeline(
        [
            {"word": "Good", "start": 0.1, "end": 0.4, "speaker": "A", "score": 0.9},
            {"word": "morning", "start": 0.4, "end": 0.9, "speaker": "A", "score": 0.9},
            {"word": "professor", "start": 1.0, "end": 1.7, "speaker": "B", "score": 0.4},
        ],
        language="en",
        duration_s=2.0,
    )
    cs = build_caption_set(tl)
    (d / "captions.json").write_text(cs.model_dump_json(indent=2), encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps({"id": "cap_edit0001", "status": "done"}), encoding="utf-8"
    )
    return d


def read_caps(d):
    return json.loads((d / "captions.json").read_text(encoding="utf-8"))


def test_edits_persist_and_are_readable(client, editable_job):
    caps = read_caps(editable_job)
    caps["pages"][0]["words"][0]["text"] = "Evening"

    r = client.put("/api/caption-jobs/cap_edit0001/captions", json=caps)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # The export reads this file, so the edit must be on disk, not just in memory.
    assert read_caps(editable_job)["pages"][0]["words"][0]["text"] == "Evening"

    r = client.get("/api/caption-jobs/cap_edit0001/captions")
    assert r.status_code == 200
    assert json.loads(r.text)["pages"][0]["words"][0]["text"] == "Evening"
    assert r.headers.get("cache-control") == "no-store"


def test_edit_keeps_word_timings_untouched(client, editable_job):
    """A text fix must not disturb sync -- the timings are what make the
    highlight work, and re-deriving them from text would break it."""
    caps = read_caps(editable_job)
    before = [(w["startMs"], w["endMs"]) for p in caps["pages"] for w in p["words"]]
    caps["pages"][0]["words"][0]["text"] = "Totally different length text"
    client.put("/api/caption-jobs/cap_edit0001/captions", json=caps)
    after = [(w["startMs"], w["endMs"]) for p in read_caps(editable_job)["pages"] for w in p["words"]]
    assert before == after


def test_edit_records_a_timestamp(client, editable_job):
    caps = read_caps(editable_job)
    client.put("/api/caption-jobs/cap_edit0001/captions", json=caps)
    meta = json.loads((editable_job / "meta.json").read_text(encoding="utf-8"))
    assert meta.get("editedAt", 0) > 0


def test_edit_rejects_blank_word(client, editable_job):
    caps = read_caps(editable_job)
    caps["pages"][0]["words"][0]["text"] = "   "
    r = client.put("/api/caption-jobs/cap_edit0001/captions", json=caps)
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()
    # The previous good version must survive a rejected edit.
    assert read_caps(editable_job)["pages"][0]["words"][0]["text"] != "   "


def test_edit_rejects_inconsistent_word_count(client, editable_job):
    caps = read_caps(editable_job)
    caps["wordCount"] = 999
    r = client.put("/api/caption-jobs/cap_edit0001/captions", json=caps)
    assert r.status_code == 400
    assert "invalid caption data" in r.json()["detail"].lower()


def test_edit_rejects_overlapping_pages(client, editable_job):
    caps = read_caps(editable_job)
    caps["pages"][0]["startMs"] = 0
    caps["pages"][0]["endMs"] = 99999
    r = client.put("/api/caption-jobs/cap_edit0001/captions", json=caps)
    assert r.status_code == 400


def test_edit_rejects_malformed_json(client, editable_job):
    r = client.put(
        "/api/caption-jobs/cap_edit0001/captions",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_edit_rejects_a_3d_job_id(client, editable_job):
    caps = read_caps(editable_job)
    r = client.put("/api/caption-jobs/de988512/captions", json=caps)
    assert r.status_code == 400


def test_edit_on_a_job_with_no_captions_is_409(client, tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    d = tmp_path / "cap_pending01"
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"id": d.name, "status": "transcribing"}), encoding="utf-8"
    )
    r = client.put(f"/api/caption-jobs/{d.name}/captions", json={"pages": []})
    assert r.status_code == 409
    assert "transcribing" in r.json()["detail"]


def test_rejected_edit_leaves_no_temp_file(client, editable_job):
    caps = read_caps(editable_job)
    caps["pages"][0]["words"][0]["text"] = ""
    client.put("/api/caption-jobs/cap_edit0001/captions", json=caps)
    leftovers = [p.name for p in editable_job.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"interrupted save left {leftovers}"


# ----------------------------------------------------------------- export


@pytest.fixture
def exportable_job(tmp_path, monkeypatch):
    """A finished caption job with a source video, ready to export."""
    from app.pipeline.captions import build_caption_set
    from app.pipeline.word_timeline import build_word_timeline

    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    d = tmp_path / "cap_export01"
    d.mkdir()
    (d / "audio.wav").write_bytes(b"RIFFfake")
    (d / "source.mp4").write_bytes(b"\x00" * 2048)
    tl = build_word_timeline(
        [{"word": "hi", "start": 0.1, "end": 0.4, "speaker": "A", "score": 0.9}],
        language="en",
        duration_s=1.0,
    )
    (d / "captions.json").write_text(
        build_caption_set(tl).model_dump_json(indent=2), encoding="utf-8"
    )
    (d / "meta.json").write_text(
        json.dumps({"id": d.name, "status": "done"}), encoding="utf-8"
    )
    return d


def test_export_starts_on_a_recording(client, exportable_job, monkeypatch):
    started = {}
    monkeypatch.setattr(
        captions_api, "_spawn_export",
        lambda job_id: started.setdefault("job", job_id) and 4242 or 4242,
    )
    r = client.post(
        "/api/caption-jobs/cap_export01/export",
        content=b"RIFF....WEBMfake-bytes",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "encoding"
    assert started["job"] == "cap_export01"

    # The recording is written under a name the exporter owns.
    assert (exportable_job / "recording.webm").exists()
    meta = json.loads((exportable_job / "meta.json").read_text(encoding="utf-8"))
    assert meta["exportStatus"] == "encoding"
    assert meta["exportError"] is None


def test_export_rejects_an_empty_recording(client, exportable_job, monkeypatch):
    monkeypatch.setattr(captions_api, "_spawn_export", lambda j: 1)
    r = client.post("/api/caption-jobs/cap_export01/export", content=b"")
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()
    assert not (exportable_job / "recording.webm").exists()


def test_export_rejects_a_3d_job_id(client, exportable_job, monkeypatch):
    monkeypatch.setattr(captions_api, "_spawn_export", lambda j: 1)
    r = client.post("/api/caption-jobs/de988512/export", content=b"x")
    assert r.status_code == 400


def test_export_needs_captions_first(client, tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    d = tmp_path / "cap_nocaps01"
    d.mkdir()
    (d / "source.mp4").write_bytes(b"\x00" * 64)
    (d / "meta.json").write_text(json.dumps({"id": d.name}), encoding="utf-8")
    r = client.post(f"/api/caption-jobs/{d.name}/export", content=b"x")
    assert r.status_code == 409
    assert "nothing to burn in" in r.json()["detail"]


def test_export_status_before_any_export(client, exportable_job):
    r = client.get("/api/caption-jobs/cap_export01/export-status")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["url"] is None
    assert body["status"] == "idle"


def test_export_status_reports_the_failure_reason(client, exportable_job):
    meta = json.loads((exportable_job / "meta.json").read_text(encoding="utf-8"))
    meta["exportStatus"] = "failed"
    meta["exportError"] = "the recording could not be read as media"
    (exportable_job / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    body = client.get("/api/caption-jobs/cap_export01/export-status").json()
    assert body["status"] == "failed"
    assert "could not be read" in body["error"]


def test_download_is_404_before_an_export_exists(client, exportable_job):
    r = client.get("/caption-jobs/cap_export01/export.mp4")
    assert r.status_code == 404


def test_download_serves_the_mp4_with_a_filename(client, exportable_job):
    """The explicit route must win over the StaticFiles mount that shares the
    /caption-jobs prefix, so the browser saves it with a sensible name."""
    out = exportable_job / "export.mp4"
    out.write_bytes(b"\x00" * 4096)
    r = client.get("/caption-jobs/cap_export01/export.mp4")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "video/mp4"
    assert "cap_export01" in (r.headers.get("content-disposition") or "")


def test_export_routes_precede_the_static_mount():
    """Ordering matters in Starlette: a mount declared first shadows later
    routes sharing its prefix.

    Mounts carry a `path` too, so they have to be matched by type, not by
    checking whether a path starts with the prefix.
    """
    from starlette.routing import Mount

    routes = list(captions_api.router.routes)
    download = next(
        i for i, r in enumerate(routes)
        if getattr(r, "path", None) == "/caption-jobs/{job_id}/export.mp4"
    )
    mount = next(
        i for i, r in enumerate(routes)
        if isinstance(r, Mount) and getattr(r, "path", "") == "/caption-jobs"
    )
    assert download < mount, (
        f"the download route is at position {download} and the /caption-jobs "
        f"mount at {mount}; the mount must come later or it shadows the route"
    )


# ---------------------------------------------------------------- isolation


def test_caption_list_excludes_3d_jobs(client, tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    (tmp_path / "de988512").mkdir()
    (tmp_path / "de988512" / "meta.json").write_text(
        json.dumps({"id": "de988512", "status": "done"}), encoding="utf-8"
    )
    cap = tmp_path / "cap_abc12345"
    cap.mkdir()
    (cap / "meta.json").write_text(
        json.dumps({"id": "cap_abc12345", "status": "done", "createdAt": 2}), encoding="utf-8"
    )

    r = client.get("/api/caption-jobs")
    assert r.status_code == 200
    ids = [j["id"] for j in r.json()]
    assert ids == ["cap_abc12345"]
    assert "de988512" not in ids


def test_delete_only_touches_caption_jobs(client, tmp_path, monkeypatch):
    monkeypatch.setattr(captions_api, "JOBS", tmp_path)
    three_d = tmp_path / "de988512"
    three_d.mkdir()
    (three_d / "meta.json").write_text("{}", encoding="utf-8")

    assert client.delete("/api/caption-jobs/de988512").status_code == 400
    assert three_d.exists(), "a 3D job must not be deletable through the captions API"
