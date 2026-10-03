"""A character must be on stage before they speak.

Job ce2f201a lost the mom for most of the scene. Her `walkins` entry put her
entrance at 30.07s while her first line was at 8.64s, so the player -- which
trusted that schedule absolutely -- hid her through her own dialogue. She only
appeared once the dad started talking at 32s, which is exactly what was reported.

`walkins`/`entranceOrder` are derived from the dialogue's segments, but only one
code path maintained them while positions were rewritten by player-side spot
saves. The two drifted apart and nothing noticed, because a missing character is
not an error.

Four things are pinned here:

* the derivation is general -- for any N and any spacing of first lines, no
  entrance may start after its own speaker's first line;
* drift is *reported*, not silently tolerated;
* the player no longer trusts a schedule that contradicts the dialogue;
* every dialogue write re-derives, so the drift cannot come back.
"""

import json
import pathlib
import random
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.api import main as main_api

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _dialogue(n_speakers: int, first_lines: list[float]) -> dict:
    """A dialogue whose speakers first speak at the given times."""
    sids = [f"SPEAKER_{i:02d}" for i in range(n_speakers)]
    segments = []
    for idx, sid in enumerate(sids):
        t = first_lines[idx]
        segments.append({"speaker": sid, "start": t, "end": t + 1.0, "text": f"line {idx}"})
    return {
        "speakers": {sid: {"name": sid, "position": {"x": 0, "y": 0, "z": -0.85}} for sid in sids},
        "segments": segments,
    }


# ------------------------------------------------------------ the invariant


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 8])
def test_every_speaker_is_on_stage_before_their_first_line(n):
    dlg = _dialogue(n, [1.0 + 7.0 * i for i in range(n)])
    report = main_api.refresh_entrance(dlg)

    assert report["ok"], report["problems"]
    firsts = main_api.first_line_times(dlg["segments"])
    for w in dlg["walkins"]:
        assert w["t0"] <= firsts[w["speaker"]], (
            f"{w['speaker']} enters at {w['t0']} but speaks at {firsts[w['speaker']]}"
        )
    assert set(dlg["entranceOrder"]) == set(firsts)


@pytest.mark.parametrize("seed", range(12))
def test_no_random_timing_can_break_the_invariant(seed):
    """Clustered first lines -- everyone talking at once -- is the case that used
    to push entrances past the lines they were supposed to precede."""
    rng = random.Random(seed)
    n = rng.randint(2, 6)
    firsts = sorted(round(rng.uniform(0.5, 30.0), 2) for _ in range(n))
    dlg = _dialogue(n, firsts)
    report = main_api.refresh_entrance(dlg)

    assert report["ok"], report["problems"]
    lines = main_api.first_line_times(dlg["segments"])
    for w in dlg["walkins"]:
        assert w["t0"] <= lines[w["speaker"]]


def test_two_speakers_talking_at_the_same_instant_are_both_reachable():
    """Same start time: the old arithmetic gave the second speaker an entrance
    after both lines had begun."""
    dlg = _dialogue(2, [5.0, 5.0])
    report = main_api.refresh_entrance(dlg)

    assert report["ok"], report["problems"]
    late = [w for w in dlg["walkins"] if w["speaker"] == "SPEAKER_01"][0]
    assert late["t0"] <= 5.0


def test_a_speaker_who_never_speaks_gets_no_entrance():
    """entranceOrder must describe the cast, not every key in speakers."""
    dlg = _dialogue(3, [1.0, 4.0, 9.0])
    dlg["speakers"]["SPEAKER_07"] = {"name": "unused", "position": {"x": 2, "y": 0, "z": 0}}
    main_api.refresh_entrance(dlg)

    assert "SPEAKER_07" not in dlg["entranceOrder"]
    assert all(w["speaker"] != "SPEAKER_07" for w in dlg["walkins"])


def test_entrance_order_starts_with_the_first_speaker():
    dlg = _dialogue(3, [12.0, 1.0, 5.0])
    main_api.refresh_entrance(dlg)

    assert dlg["entranceOrder"][0] == "SPEAKER_01", "the earliest speaker is already on stage"


# ------------------------------------------------------------ drift is visible


def _ce2f201a_shape() -> dict:
    """The exact drift: 4-speaker segments carrying a 2-speaker schedule.

    SPEAKER_01's stored entrance (30.07) is SPEAKER_02's correct entrance, and
    two speakers have no entrance at all.
    """
    dlg = _dialogue(4, [1.633, 8.636, 32.069, 75.775])
    dlg["entranceOrder"] = ["SPEAKER_00", "SPEAKER_01"]
    dlg["walkins"] = [{"speaker": "SPEAKER_01", "t0": 30.07, "t1": 31.87, "door": {"x": 0, "z": 2.1}}]
    return dlg


def test_drift_is_reported_rather_than_tolerated():
    report = main_api.entrances_consistent(_ce2f201a_shape())

    assert report["ok"] is False
    issues = {p.get("issue") for p in report["problems"]}
    assert "entrance_after_first_line" in issues
    assert "no_entrance" in issues
    assert "entrance_order_mismatch" in issues


def test_the_mom_specific_fault_is_named():
    """The reported symptom, pinned: SPEAKER_01 hidden from 8.64s to 30.07s."""
    report = main_api.entrances_consistent(_ce2f201a_shape())

    mom = [p for p in report["problems"]
           if p.get("speaker") == "SPEAKER_01"
           and p.get("issue") == "entrance_after_first_line"]
    assert mom, report["problems"]
    assert mom[0]["entrance"] == 30.07
    assert mom[0]["firstLine"] == 8.636


def test_re_deriving_repairs_the_drift():
    dlg = _ce2f201a_shape()
    before = main_api.entrances_consistent(dlg)
    after = main_api.refresh_entrance(dlg)

    assert before["ok"] is False
    assert after["ok"] is True
    assert dlg["entranceOrder"] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"]
    firsts = main_api.first_line_times(dlg["segments"])
    assert len(dlg["walkins"]) == 3, "every non-lead speaker now has an entrance"
    for w in dlg["walkins"]:
        assert w["t0"] <= firsts[w["speaker"]]


def test_a_healthy_schedule_reports_ok():
    dlg = _dialogue(4, [1.0, 5.0, 12.0, 30.0])
    main_api.refresh_entrance(dlg)
    assert main_api.entrances_consistent(dlg)["ok"] is True


# ------------------------------------------------------------ the player trusts the dialogue


def _player_guard(dlg: dict, probes: list[tuple[str, float]]) -> dict:
    """Run the player's own isPresent() against a dialogue.

    Extracted and executed rather than grepped: the previous version of this
    guard was `assert "min(" in src`, which passed happily while the function
    still hid the speaker.
    """
    html = (ROOT / "dialogue-player.html").read_text(encoding="utf-8")
    start = html.index("let _firstLineCache")
    end = html.index("function presentAvatars(")
    body = html[start:end]
    lines = "\n".join(
        "r[%s] = isPresent(%s, %s);" % (json.dumps(f"{s}@{t}"), json.dumps(s), repr(float(t)))
        for s, t in probes
    )
    script = (
        # The extracted region also defines window.dialoguePresenceAt, which the
        # browser check uses; node has no window, so stub it.
        "const window = {};\n"
        "const state = { diarization: " + json.dumps(dlg) + ", prePlayEditing: false };\n"
        + body +
        "\nconst r = {};\n" + lines +
        "\nconsole.log(JSON.stringify(r));\n"
    )
    out = pathlib.Path(tempfile.gettempdir()) / "entrance_guard_check.mjs"
    out.write_text(script, encoding="utf-8")
    try:
        res = subprocess.run(["node", str(out)], capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stderr[:400]
        return json.loads(res.stdout.strip().splitlines()[-1])
    finally:
        out.unlink(missing_ok=True)


def test_the_player_shows_a_speaker_during_their_own_lines_despite_a_bad_schedule():
    dlg = _ce2f201a_shape()
    probes = [("SPEAKER_01", 9.0), ("SPEAKER_01", 20.0), ("SPEAKER_02", 33.0),
              ("SPEAKER_03", 76.0)]
    got = _player_guard(dlg, probes)

    for key, value in got.items():
        assert value is True, f"{key}: the player would hide a speaking character"


def test_the_player_still_hides_a_speaker_before_they_arrive():
    """The guard must not turn every character on from the first second."""
    dlg = _ce2f201a_shape()
    got = _player_guard(dlg, [("SPEAKER_01", 0.0), ("SPEAKER_01", 5.0)])

    assert got["SPEAKER_01@0.0"] is False, "nobody should appear before their entrance"
    assert got["SPEAKER_01@5.0"] is False


def test_the_player_shows_the_lead_speaker_from_the_start():
    dlg = _ce2f201a_shape()
    got = _player_guard(dlg, [("SPEAKER_00", 0.0)])
    assert got["SPEAKER_00@0.0"] is True


# ------------------------------------------------------------ the API


def test_get_job_reports_entrance_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = tmp_path / "j1"
    d.mkdir()
    (d / "status.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    (d / "dialogue.json").write_text(json.dumps(_ce2f201a_shape()), encoding="utf-8")
    client = TestClient(main_api.app)

    body = client.get("/api/jobs/j1").json()

    assert body["entrances"]["ok"] is False
    assert body["entrances"]["checked"] == 4


def test_refresh_endpoint_repairs_and_reports_before_and_after(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = tmp_path / "j2"
    d.mkdir()
    (d / "dialogue.json").write_text(json.dumps(_ce2f201a_shape()), encoding="utf-8")
    client = TestClient(main_api.app)

    r = client.post("/api/jobs/j2/refresh-entrance")

    assert r.status_code == 200
    body = r.json()
    assert body["before"]["ok"] is False
    assert body["after"]["ok"] is True
    on_disk = json.loads((d / "dialogue.json").read_text(encoding="utf-8"))
    assert len(on_disk["entranceOrder"]) == 4


def test_refresh_endpoint_needs_a_dialogue(tmp_path, monkeypatch):
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    (tmp_path / "j3").mkdir()
    client = TestClient(main_api.app)

    assert client.post("/api/jobs/j3/refresh-entrance").status_code == 404
    assert client.post("/api/jobs/nope/refresh-entrance").status_code == 404


def test_editing_a_job_re_derives_its_entrances(tmp_path, monkeypatch):
    """PUT /dialogue goes through _persist_dialogue_edits, which is also the path
    player-side spot saves use. If it did not re-derive, the drift would return."""
    monkeypatch.setattr(main_api, "JOBS", tmp_path)
    d = tmp_path / "j4"
    d.mkdir()
    dlg = _ce2f201a_shape()
    (d / "dialogue.json").write_text(json.dumps(dlg), encoding="utf-8")
    client = TestClient(main_api.app)

    body = {"segments": dlg["segments"], "speakers": dlg["speakers"]}
    r = client.put("/api/jobs/j4/dialogue", json=body)

    assert r.status_code == 200
    after = json.loads((d / "dialogue.json").read_text(encoding="utf-8"))
    assert main_api.entrances_consistent(after)["ok"] is True
    assert len(after["entranceOrder"]) == 4


def test_the_pipeline_derivation_is_not_swallowed():
    """The derivation used to sit inside a `try: ... except Exception: pass`, so a
    failure left the previous schedule on disk with no trace anywhere."""
    src = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
    body = src.split("def assemble_dialogue", 1)[1]
    # Cut at the real call, not the one inside the docstring.
    guard = body.split("\n        to_player_audio(job_dir, audio_path)", 1)[0]
    assert "refresh_entrance(final)" in guard
    assert "write_provenance(job_dir, entrances_ok=" in guard, (
        "the result must be recorded, not discarded"
    )
    # And it must sit after the swallowing except, not inside the try.
    tail = guard.split("except Exception:", 1)
    assert len(tail) > 1, "the derivation should follow the try/except block"
    assert "refresh_entrance(final)" in tail[-1], (
        "deriving inside the try means a failure still leaves a stale schedule"
    )