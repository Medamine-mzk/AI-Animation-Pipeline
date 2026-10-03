"""One job on screen, and a speakers panel that describes it.

In `config.html?mode=wav` a 4-speaker upload displayed two full editors side by
side, and the speakers panel offered only SPEAKER_00 to edit. Two symptoms, one
defect: the editor was rendered into two containers and only some render paths
set `currentJobId`, so the panel was describing whichever job that variable
happened to hold (46065548, which genuinely has one voice).

What is pinned here is structural, because the symptom is a wiring problem and
wiring cannot be asserted from Python:

* exactly one container receives the editor;
* every path that displays a job goes through `openJob`, which sets currentJobId;
* the panel builds from the union of the map keys and the speakers the lines use;
* wav mode hides the manual-mode chrome that used to overwrite the editor.
"""

import json
import pathlib

from fastapi.testclient import TestClient

from app.api import main as main_api

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _config() -> str:
    return (ROOT / "config.html").read_text(encoding="utf-8")


def _code_only(html: str) -> str:
    """Strip JS comments so names kept to explain a removal do not look live."""
    out = []
    for line in html.splitlines():
        # crude but adequate: drop // comments that are not inside a string we care about
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        out.append(line.split("//")[0] if '"' not in line.split("//")[0][-1:] else line)
    return "\n".join(out)


# ------------------------------------------------------- one editor, one owner


def test_there_is_only_one_editor_container():
    """The duplicate was a lazily created sibling of #lines that was never
    cleared, so it kept showing a previously opened job beside the live one."""
    code = _code_only(_config())
    assert "wavResult" not in code, "the second editor container is back"


def test_only_openjob_renders_the_editor():
    """`renderJobLines` draws a complete editor, so every extra caller is a
    chance to draw it twice."""
    code = _code_only(_config())
    calls = [l for l in code.splitlines() if "renderJobLines(" in l]
    # one definition + one call, and that call is inside openJob
    assert len(calls) == 2, f"expected a single call site, found {len(calls)}"
    assert "renderJobLines(jobId, document.getElementById('lines'))" in code, (
        "the editor must render into #lines, the only container"
    )


def test_openjob_is_what_sets_the_current_job():
    """pollStatus displayed a finished upload without touching currentJobId,
    which is why the panel kept describing the previous job."""
    code = _code_only(_config())
    assert "async function openJob(jobId)" in code
    openjob = code.split("async function openJob", 1)[1].split("\n}", 1)[0]
    assert "currentJobId = jobId" in openjob, "openJob must own currentJobId"
    # No other function may *bind* it to a job. Clearing it (deleting a project,
    # or dropping an id whose job no longer exists) is legitimate; reads and
    # comparisons are fine.
    import re
    outside = code.replace(openjob, "")
    bindings = [
        l.strip() for l in outside.splitlines()
        if re.search(r"\bcurrentJobId\s*=(?!=)", l)
        and "let currentJobId" not in l
        and not re.search(r"currentJobId\s*=\s*(null|undefined|''|\"\")", l)
    ]
    assert not bindings, (
        "another function still binds currentJobId: "
        + " | ".join(b[:70] for b in bindings)
    )


def test_every_render_path_goes_through_openjob():
    code = _code_only(_config())
    # the upload-complete path and the failure path both displayed a job
    assert "await openJob(jobId);" in code
    assert "renderJobLines(jobId, c)" not in code, (
        "pollStatus must not bypass openJob and leave the panel stale"
    )


# ------------------------------------------------------- the panel is complete


def test_the_panel_uses_the_union_of_map_and_segment_speakers():
    """A line can name a speaker the speakers map never described, and that
    voice still needs a name, avatar and gender -- otherwise it is simply not
    editable."""
    code = _code_only(_config())
    assert "if(sid && !targetSpeakers[sid]) targetSpeakers[sid]={}" in code, (
        "the panel must add speakers that the lines use but the map omits"
    )


def test_wav_mode_hides_the_manual_chrome():
    """initAiMode hid #addLine/#saveBtn; wav mode did not, and the header's
    '+ Add Line' handler calls render(), which replaces the loaded editor with
    the manual-mode table."""
    code = _code_only(_config())
    wav = code.split("async function initWavUpload", 1)[1]
    wav = wav.split("\nasync function", 1)[0] + wav.split("\nfunction", 1)[0]
    assert "'addLine'" in wav and "'saveBtn'" in wav, (
        "wav mode must hide the manual-mode buttons, as AI mode already does"
    )


def test_empty_jobs_are_hidden_but_still_reachable():
    """Ten empty rows buried the real work. Hiding them is only acceptable if
    they can be brought back -- silently dropping a row makes a job look like it
    never existed."""
    code = _code_only(_config())
    assert "showEmptyProjects" in code
    assert "empty job" in code, "the count of hidden jobs must be stated"
    assert "toggleEmptyProjects" in code
    assert "(j.segments||0) > 0 || showEmptyProjects" in code


# ------------------------------------------------------- the API side


def test_get_job_reports_a_line_count(tmp_path, monkeypatch):
    """config.html printed 'dialogue ready (? segs)' because status.json holds no
    segment count."""
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = tmp_path / "j1"
    d.mkdir()
    (d / "status.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    (d / "dialogue.json").write_text(json.dumps({"segments": [{"text": "a"}, {"text": "b"}]}), encoding="utf-8")
    client = TestClient(main_api.app)

    body = client.get("/api/jobs/j1").json()

    assert body["segments"] == 2


def test_get_job_reports_zero_lines_when_there_is_no_dialogue(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = tmp_path / "j2"
    d.mkdir()
    (d / "status.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    client = TestClient(main_api.app)

    assert client.get("/api/jobs/j2").json()["segments"] == 0