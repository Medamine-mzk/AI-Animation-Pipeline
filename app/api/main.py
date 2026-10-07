from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.captions import router as captions_router, reap_stale_jobs
from fastapi.responses import FileResponse
import contextlib
import uuid, json, pathlib, shutil, subprocess, sys, os, math, threading, time
from typing import Literal
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).resolve().parents[2]
JOBS = ROOT / "jobs"
FFMPEG = ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"
GOLDEN_TRANSCRIPT = ROOT / "jobs" / "golden" / "transcript.json"
GOLDEN_CATALOG = ROOT / "jobs" / "golden" / "clip_catalog.json"
DEFAULT_PICKER = ROOT / "jobs" / "picker" / "living_curtains_picker.json"
ROLE_ORDER = ["girl", "woman", "man", "boy"]

#: The golden London clip. Paired demo mode copies this *together with* the golden
#: transcript -- never one without the other, which is what produced the silent
#: mismatches this guard now catches.
GOLDEN_AUDIO = ROOT / "media" / "golden_clip.wav"

#: How far a dialogue may run past its audio before we call it a mismatch. A
#: little slack absorbs rounding in the segment timings; 5s is far more than any
#: legitimate trailing pause observed in a real transcript.
DIALOGUE_OVERRUN_TOLERANCE_S = 5.0


def audio_duration_s(path: pathlib.Path) -> float | None:
    """Duration of an audio file in seconds, or None if it cannot be read.

    Reads the WAV header directly rather than shelling out to ffprobe: this runs
    whenever a job is opened, and a subprocess per open is a needless cost.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            byte_rate = None
            while True:
                hdr = fh.read(8)
                if len(hdr) < 8:
                    return None
                cid = hdr[:4]
                size = int.from_bytes(hdr[4:8], "little")
                if cid == b"fmt ":
                    body = fh.read(size)
                    byte_rate = int.from_bytes(body[8:12], "little")
                elif cid == b"data":
                    if not byte_rate:
                        return None
                    return size / byte_rate
                else:
                    fh.seek(size + (size % 2), 1)   # chunks are word-aligned
    except (OSError, ValueError):
        return None


def dialogue_end_s(job_dir: pathlib.Path) -> float | None:
    """When the last dialogue line ends, in seconds."""
    p = job_dir / "dialogue.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ends = [float(s["end"]) for s in (data.get("segments") or []) if "end" in s]
    return max(ends) if ends else None


def dialogue_vs_audio(job_dir: pathlib.Path) -> dict:
    """Does this job's dialogue actually belong to this job's audio?

    A job whose dialogue outlives its audio by more than a few seconds is talking
    about a different recording. That happened here because the golden-fallback
    copied the golden *transcript* into a job that still held its own *audio*, so
    the user heard their own recording under someone else's words -- with no
    warning, because the flag that was meant to surface it had been lost.

    Detected from the data rather than from a flag, so it also catches jobs that
    predate the flag or whose meta.json was overwritten.
    """
    report = {"ok": True, "audioS": None, "dialogueEndS": None, "overrunS": 0.0,
              "reason": None}
    audio = job_dir / "audio.wav"
    if not audio.exists():
        audio = job_dir / "input.wav"
    if not audio.exists():
        return report

    audio_s = audio_duration_s(audio)
    end_s = dialogue_end_s(job_dir)
    report["audioS"] = audio_s
    report["dialogueEndS"] = end_s
    if audio_s is None or end_s is None:
        return report

    overrun = end_s - audio_s
    report["overrunS"] = round(overrun, 1)
    if overrun > DIALOGUE_OVERRUN_TOLERANCE_S:
        report["ok"] = False
        report["reason"] = (
            f"this dialogue was written for a different recording: it runs "
            f"{end_s:.1f}s but the audio is only {audio_s:.1f}s, so the lines "
            f"after {audio_s:.1f}s have nothing to attach to"
        )
    return report

# Stage -> (elapsed-budget seconds, [base%, span]) for the *elapsed* progress
# estimate. Budgets mirror the actual subprocess timeouts used in the workers so
# the end of each stage (a real, known point) lands roughly on its boundary.
STAGE_BUDGET = {
    "queued": 8,
    "transcribing": 210,
    "structuring": 75,
    "emotions": 90,
    "lipsyncing": 210,
    "assembling": 30,
    "generating": 75,
    "emote": 60,
}
STAGE_SPAN = {
    "queued": (0, 4),
    "transcribing": (6, 40),
    "structuring": (43, 62),
    "emotions": (65, 80),
    "lipsyncing": (83, 93),
    "assembling": (96, 99),
    "generating": (8, 45),
    "emote": (88, 95),
}

@contextlib.asynccontextmanager
async def lifespan(_app):
    """Reconcile work that a previous process left behind.

    Caption transcription and encoding both run as subprocesses, so if the
    server stops -- crash, reboot, container restart -- the workers stop with it
    and no terminal status is ever written. Without this sweep those jobs report
    "transcribing" forever and their pages poll a progress bar that cannot
    finish. Every host that restarts the app hits this, so it is fixed once at
    startup rather than per request.
    """
    report = reap_stale_jobs()
    if report["reaped"]:
        print(f"[startup] marked {len(report['reaped'])} orphaned caption job(s) "
              f"as failed: {', '.join(report['reaped'])}", flush=True)
    if report["healed"]:
        print(f"[startup] corrected {len(report['healed'])} job(s) whose export had "
              f"finished without clearing its status: {', '.join(report['healed'])}",
              flush=True)
    yield


app = FastAPI(title="AI Animation Pipeline", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)


def run_with_timeout(fn, timeout):
    """Run fn in a daemon thread, aborting intent after `timeout` seconds.

    The LLM client's per-request timeout (600s) can otherwise stall the whole
    background job well past the attempt-level deadline, so we bound it here.
    """
    result = {}

    def runner():
        try:
            result["ok"] = fn()
        except Exception as exc:
            result["err"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"LLM call exceeded {timeout}s")
    if "err" in result:
        raise result["err"]
    return result["ok"]


def set_status(job_dir, status: str):
    """Record stage entry. `stage_started_at` lets /api/jobs/{id} turn elapsed
    wall-clock time into a real, monotonic progress number server-side."""
    (job_dir / "status.json").write_text(
        json.dumps({"status": status, "stage_started_at": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )


def run_module(module: str, args: list[str], timeout: int = 600):
    subprocess.run(
        [sys.executable, "-m", module, *args],
        check=True, timeout=timeout, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


# ---------- deterministic assemble helpers (no LLM required) ----------

def script_from_transcript(transcript: dict) -> dict:
    """Fallback script.json builder when the LLM structuring stage is unavailable."""
    seen: dict[str, int] = {}
    characters: list[dict] = []
    lines: list[dict] = []
    camera: list[dict] = []
    for i, seg in enumerate(transcript.get("segments", [])):
        spk = seg.get("speaker", "SPEAKER_00")
        if spk not in seen:
            seen[spk] = len(characters)
            characters.append({
                "id": spk, "speaker_ref": spk,
                "role": ROLE_ORDER[seen[spk] % len(ROLE_ORDER)],
                "gender": "unknown", "age_group": "unknown", "sprite": None,
            })
        lines.append({
            "character_id": spk, "start": seg["start"], "end": seg["end"],
            "text": seg.get("text", ""), "emotion": "neutral",
            "audio_segment_ref": f"seg_{i}",
        })
        camera.append({
            "start": seg["start"], "end": seg["end"],
            "focus_character": spk, "shot": "medium",
        })
    return {"setting": "living_room", "characters": characters, "lines": lines, "camera": camera}


def fallback_family_script(prompt: str = "") -> dict:
    """Deterministic 4-speaker family scene used when AI generation has no LLM key."""
    chars = [
        {"id": "SPEAKER_00", "speaker_ref": "SPEAKER_00", "role": "man", "gender": "male", "age_group": "adult", "sprite": None},
        {"id": "SPEAKER_01", "speaker_ref": "SPEAKER_01", "role": "girl", "gender": "female", "age_group": "child", "sprite": None},
        {"id": "SPEAKER_02", "speaker_ref": "SPEAKER_02", "role": "woman", "gender": "female", "age_group": "adult", "sprite": None},
        {"id": "SPEAKER_03", "speaker_ref": "SPEAKER_03", "role": "boy", "gender": "male", "age_group": "child", "sprite": None},
    ]
    script_lines = [
        ("SPEAKER_01", 0.0, 2.2,  "Mom, I received a letter from Chris!", "excited"),
        ("SPEAKER_02", 2.5, 4.6,  "What does it say?", "curious"),
        ("SPEAKER_01", 4.9, 8.4,  "The Browns are inviting me to London for the summer!", "excited"),
        ("SPEAKER_00", 8.7, 11.2,  "London? That is a long way from home.", "neutral"),
        ("SPEAKER_01", 11.5, 13.0,  "Please say yes, Dad!", "happy"),
        ("SPEAKER_03", 13.3, 15.4,  "Can I come too? I want to see the buses!", "happy"),
        ("SPEAKER_02", 15.7, 18.1,  "We need to think about it, alright?", "neutral"),
        ("SPEAKER_00", 18.4, 21.0,  "Your mother is right. Let us talk about it at dinner.", "neutral"),
        ("SPEAKER_01", 21.3, 23.8,  "Thank you! I will write back right now.", "joy"),
        ("SPEAKER_03", 24.1, 26.0,  "I am going to pack my suitcase already!", "excited"),
        ("SPEAKER_02", 26.3, 28.6,  "Slow down, you two. We have a few weeks.", "happy"),
        ("SPEAKER_00", 28.9, 30.5,  "Then it is settled. Family trip to London.", "happy"),
    ]
    lines, camera = [], []
    for i, (cid, start, end, text, emo) in enumerate(script_lines):
        lines.append({
            "character_id": cid, "start": start, "end": end, "text": text,
            "emotion": emo, "audio_segment_ref": f"line_{i}",
        })
        camera.append({"start": start, "end": end, "focus_character": cid, "shot": "medium"})
    return {"setting": "living_room", "characters": chars, "lines": lines, "camera": camera}


def make_picker(speaker_ids: list[str]) -> str:
    """Build a deterministic .picker.json with circle positions matching speaker order."""
    ids = sorted(speaker_ids)
    n = len(ids)
    r = min(max(0.85, n * 0.28), 1.6)
    spots = []
    for i, sid in enumerate(ids):
        ang = 2 * math.pi * i / n - math.pi / 2
        spots.append({
            "id": sid,
            "role": ROLE_ORDER[i % len(ROLE_ORDER)],
            "x": round(r * math.cos(ang), 3),
            "y": 0,
            "z": round(r * math.sin(ang), 3),
            "yaw": str(int((ang + math.pi) * 180 / math.pi + 540) % 360),
        })
    return json.dumps({"spots": spots, "background": None, "camera": None, "fov": 42})


def first_line_times(segments: list) -> dict:
    """Earliest line time per speaker, taken from the dialogue's own segments."""
    out: dict[str, float] = {}
    for seg in segments or []:
        spk = seg.get("speaker")
        t = seg.get("start")
        if spk is None or not isinstance(t, (int, float)):
            continue
        if spk not in out or t < out[spk]:
            out[spk] = float(t)
    return out


def entrances_consistent(dlg_data: dict) -> dict:
    """Report whether the entrance schedule agrees with the dialogue.

    `walkins`/`entranceOrder` are derived data, and they drift: positions are also
    rewritten by player-side spot saves, so a job can end up with 4-speaker
    positions and a 2-speaker schedule. The player trusts the schedule, so drift
    silently removes a character from the stage -- job ce2f201a kept a walk-in at
    30.07s for a speaker whose first line was at 8.64s, leaving the mom invisible
    through her own dialogue until the dad started talking at 32s.

    Reporting it here turns a missing character into a visible, testable fault.
    """
    firsts = first_line_times(dlg_data.get("segments") or [])
    order = dlg_data.get("entranceOrder") or []
    walkins = {w.get("speaker"): w for w in (dlg_data.get("walkins") or [])}
    problems: list[dict] = []
    for sid, t_first in sorted(firsts.items()):
        if order and sid == order[0]:
            continue  # first on stage, always present
        wk = walkins.get(sid)
        if wk is None:
            problems.append({"speaker": sid, "issue": "no_entrance",
                             "firstLine": round(t_first, 3)})
        elif float(wk.get("t0", 0)) > t_first:
            problems.append({"speaker": sid, "issue": "entrance_after_first_line",
                             "entrance": wk.get("t0"), "firstLine": round(t_first, 3)})
    speaking = set(firsts)
    if order and set(order) != speaking:
        problems.append({"issue": "entrance_order_mismatch",
                         "missing": sorted(speaking - set(order)),
                         "extra": sorted(set(order) - speaking)})
    return {"ok": not problems, "problems": problems,
            "speakers": sorted(speaking), "checked": len(firsts)}


# Doors cycle for any N. The walk length is fixed, but the *start* is clamped so a
# character is always on stage before their own first line.
_STAGE_DOORS = [{"x": 0.0, "z": 2.1}, {"x": -0.5, "z": 2.1},
                {"x": 0.5, "z": 2.1}, {"x": 0.0, "z": 2.5}]


def refresh_entrance(dlg_data: dict) -> dict:
    """Re-derive walkins, entranceOrder and centred positions from the segments.

    Called on every dialogue write, because positions are also changed by
    player-side spot saves and the two drifted apart.

    The enforced invariant: no speaker's entrance starts after their own first
    line. Without it a character is hidden while talking.
    """
    segments = dlg_data.get("segments") or []
    speakers = dlg_data.get("speakers") or {}
    sids = sorted(speakers.keys())
    if not segments or not sids:
        return entrances_consistent(dlg_data)

    # Centred line-up for N, so a 2-speaker job is not spread across 4 slots.
    n = len(sids)
    spacing = 0.7 if n <= 3 else 0.6
    start_x = -((n - 1) * spacing) / 2
    for idx, sid in enumerate(sids):
        cfg = speakers.get(sid)
        if not isinstance(cfg, dict):
            continue
        x = round(start_x + idx * spacing, 3)
        if not cfg.get("position"):
            cfg["position"] = {"x": x, "y": 0, "z": -0.85}
        else:
            cfg["position"]["x"] = x
            cfg["position"]["y"] = 0
            cfg["position"]["z"] = -0.85

    firsts = first_line_times(segments)
    speaking = [sid for sid in sids if sid in firsts]
    speaking.sort(key=lambda sid: (firsts[sid], sid))
    walkins = []
    for idx, sid in enumerate(speaking):
        if idx == 0:
            continue  # already on stage when playback starts
        t_first = firsts[sid]
        t0 = round(max(0.5, t_first - 2.0), 2)
        t1 = round(min(t0 + 1.8, t_first - 0.2), 2)
        if t1 <= t0:
            # No room to walk before this line: appear rather than schedule a
            # walk that would finish after the speaker starts talking.
            t0 = t1 = max(0.5, round(t_first - 0.2, 2))
        if t0 > t_first:  # the invariant, stated rather than left to arithmetic
            t0 = t1 = max(0.5, round(t_first - 0.2, 2))
        walkins.append({"speaker": sid, "t0": round(t0, 2), "t1": round(t1, 2),
                        "door": dict(_STAGE_DOORS[(idx - 1) % len(_STAGE_DOORS)])})
    walkins.sort(key=lambda w: w["t0"])

    dlg_data["walkins"] = walkins
    dlg_data["entranceOrder"] = speaking
    return entrances_consistent(dlg_data)


def assemble_dialogue(job_dir: pathlib.Path, script_path: pathlib.Path) -> pathlib.Path:
    """Run the shared picker staging stage -> jobs/<id>/dialogue.json."""
    script = json.loads(script_path.read_text(encoding="utf-8"))
    speakers = [c["id"] for c in script.get("characters", [])]
    picker_args = "--picker"
    picker_path = DEFAULT_PICKER if (DEFAULT_PICKER.exists() and len(script.get("characters", [])) <= 4) else None
    if picker_path is None:
        tmp_picker = job_dir / "picker.json"
        tmp_picker.write_text(make_picker(speakers), encoding="utf-8")
        picker_path = tmp_picker
    visemes = job_dir / "visemes.json"
    line_emo = job_dir / "line_emotions.json"
    dialogue = job_dir / "dialogue.json"
    args = [
        picker_args, str(picker_path),
        "--script", str(script_path),
        "--visemes", str(visemes) if visemes.exists() else str(ROOT / "jobs" / "golden" / "visemes.json"),
        "--line-emotions", str(line_emo) if line_emo.exists() else str(ROOT / "jobs" / "golden" / "line_emotions.json"),
        "--catalog", str(GOLDEN_CATALOG if GOLDEN_CATALOG.exists() else ROOT / "jobs" / "golden" / "clip_catalog.json"),
        "--out", str(dialogue),
    ]
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "picker_to_dialogue.py"), *args],
        check=True, timeout=300, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assembled = json.loads(dialogue.read_text(encoding="utf-8"))
    if not assembled.get("segments"):
        raise RuntimeError(
            "ASR produced 0 dialogue segments; no clear speech detected in the audio. "
            "Upload a clip with voiced dialogue, or check the source audio."
        )
    return dialogue


def _refuse_empty_dialogue(job_dir: pathlib.Path) -> None:
    """Honest done-gate: dialogue.json must have >= 1 segment before a job may
    flip to 'done'. Refuses (raises) when ASR picks up 0 voiced segments, so the
    UI never shows a fake 0-line 'done' with a broken preview."""
    gate_msg = (
        "ASR produced 0 dialogue segments — no clear speech detected in the "
        "audio. Upload a clip with voiced dialogue, or check the source file."
    )
    if not (job_dir / "dialogue.json").exists():
        raise RuntimeError("dialogue.json missing — assembly did not run.")
    try:
        segs = json.loads((job_dir / "dialogue.json").read_text(encoding="utf-8")).get("segments", [])
    except Exception as exc:
        raise RuntimeError("dialogue.json unreadable: " + str(exc)) from exc
    if not segs:
        raise RuntimeError(gate_msg)


def to_player_audio(job_dir: pathlib.Path, source: pathlib.Path) -> pathlib.Path:
    """Convert any uploaded audio to jobs/<id>/audio.wav (48k mono) for the player."""
    out = job_dir / "audio.wav"
    if FFMPEG.exists():
        subprocess.run(
            [str(FFMPEG), "-y", "-loglevel", "error", "-i", str(source), "-ar", "48000", "-ac", "1", str(out)],
            check=True, capture_output=True, timeout=300,
        )
    else:
        out.write_bytes(source.read_bytes())
    return out


def silent_audio(job_dir: pathlib.Path, duration_s: float) -> pathlib.Path:
    """Placeholder audio.wav for AI mode (no TTS yet)."""
    out = job_dir / "audio.wav"
    if FFMPEG.exists():
        subprocess.run(
            [str(FFMPEG), "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
             "-t", f"{max(duration_s, 1.0):.2f}", str(out)],
            check=True, capture_output=True, timeout=300,
        )
    else:
        out.write_bytes(b"")
    return out


# ---------- background jobs ----------

def _refuse_empty_dialogue(job_dir: pathlib.Path) -> None:
    """Honest gate shared by every audio branch: a job is only 'done' when
    dialogue.json actually has >= 1 segment. Refuses the fake-empty 'done' that
    used to render 0 lines as if the job succeeded."""
    dialogue = job_dir / "dialogue.json"
    if not dialogue.exists():
        return
    try:
        data = json.loads(dialogue.read_text(encoding="utf-8"))
    except Exception:
        return
    if not data.get("segments"):
        raise RuntimeError(
            "ASR produced 0 dialogue segments — no clear speech detected in the "
            "audio. Upload a clip with voiced dialogue, or check the source audio."
        )


def _refuse_empty_dialogue(job_dir: pathlib.Path) -> None:
    """Honest done-gate: a job is only 'done' when dialogue.json actually has
    >= 1 segment. Refuses the old fake-done that flipped status to 'done' while
    the preview showed 0 dialogue lines."""
    dialogue = job_dir / "dialogue.json"
    if not dialogue.exists():
        raise RuntimeError("dialogue.json is missing - assembly never ran")
    try:
        data = json.loads(dialogue.read_text(encoding="utf-8"))
    except Exception:
        raise RuntimeError("dialogue.json is not valid JSON")
    segs = data.get("segments", []) or []
    if not segs:
        raise RuntimeError(
            "ASR produced 0 dialogue segments - no clear spoken dialogue was "
            "detected in this audio. Upload a voiced clip or check the source."
        )


def _refuse_empty_dialogue(job_dir: pathlib.Path) -> None:
    """Honest done-gate shared by every audio branch: a job is only 'done' when
    dialogue.json actually carries >= 1 segment. This is what prevents the empty
    0-line jobs (all SPEAKER_00 fake-done) the UI used to show. When no clear
    dialogue exists the job must fail honestly, not report a successful 0-line
    preview, so the user can re-upload voiced audio or edit speakers/emotions."""
    dialogue = job_dir / "dialogue.json"
    if not dialogue.exists():
        raise RuntimeError(
            "dialogue.json is missing — assembler never wrote it (no DIALOGUE output)."
        )
    try:
        data = json.loads(dialogue.read_text(encoding="utf-8"))
    except Exception:
        raise RuntimeError("dialogue.json is not valid JSON — can't run a player preview.")
    if not data.get("segments"):
        raise RuntimeError(
            "ASR produced 0 dialogue segments — no clear speech detected in the audio. "
            "Upload a voiced clip with dialogue so speaker assignment and emotions can "
            "be generated, or fix the transcript and re-run."
        )


def _gate_job_done(job_dir: pathlib.Path) -> None:
    """Belt-and-suspenders gate alias: some call sites still use the old name.
    Delegates to the canonical honest gate."""
    _refuse_empty_dialogue(job_dir)


def _detect_gender_for_speakers(audio_path: pathlib.Path, spk_ranges: dict) -> dict:
    """Pitch-based gender detection per speaker (librosa pyin, 85% accurate, offline).
    Returns {speaker_id: 'F'/'M'} based on median F0 >165Hz => F else M."""
    try:
        import librosa
        import numpy as np
        # load audio at 16k for pitch
        y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
        if y.size == 0:
            return {}
        # pyin needs fmin/fmax
        f0, voiced_flag, voiced_probs = librosa.pyin(y, fmin=librosa.note_to_hz('C2'), fmax=librosa.note_to_hz('C7'), sr=sr, frame_length=2048, hop_length=512)
        # f0 is array with NaN for unvoiced
        times = librosa.times_like(f0, sr=sr, hop_length=512)
        result = {}
        for spk, ranges in spk_ranges.items():
            vals = []
            for (s, e) in ranges:
                # find frames within [s,e]
                mask = (times >= s) & (times <= e)
                seg_f0 = f0[mask]
                # filter NaN and voiced
                seg_f0 = seg_f0[~np.isnan(seg_f0)]
                # also filter reasonable speech range 75-400 Hz
                seg_f0 = seg_f0[(seg_f0 >= 75) & (seg_f0 <= 400)]
                if seg_f0.size > 0:
                    vals.extend(seg_f0.tolist())
            if vals:
                median = float(np.median(vals))
                # threshold 165Hz as in plan
                result[spk] = 'F' if median > 165 else 'M'
            else:
                result[spk] = 'M'  # default
        return result
    except Exception as e:
        print(f"[gender] detection failed: {e}", flush=True)
        return {}


# NOTE: _attempt_diarization() was deleted here. It never ran once: it did
#   `from whisperx import DiarizationPipeline`, but whisperx exports no such
#   top-level name, then passed use_auth_token= (not a parameter of the
#   installed signature) and called .itertracks() on the DataFrame this
#   whisperx returns. All three raised, and `except Exception: return False`
#   swallowed them -- so the honest 'second attempt' was silent dead code, and
#   no job in the repository has a speaker_genders.json to prove otherwise.
#   Speaker counting now lives in app/pipeline/transcribe.py behind
#   --require-speakers, which reports the achieved count instead of pretending.


def write_provenance(job_dir: pathlib.Path, **fields) -> None:
    """Record where a job's transcript and audio came from.

    Kept in its own file rather than meta.json because the upload handler writes
    meta.json as a bare {filename, size} stub. A failed transcription used to
    record its markers in meta.json, and a later upload-shaped write buried them
    -- so config.html's `if (transcript_failed)` guard never fired and a job
    playing someone else's dialogue under the user's own audio showed no warning
    at all. Nothing but the pipeline writes this file.
    """
    path = job_dir / "provenance.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data.update(fields)
    data["updatedAt"] = time.time()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_provenance(job_dir: pathlib.Path) -> dict:
    path = job_dir / "provenance.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _on_transcribe_failure(job_dir: pathlib.Path, audio_path: pathlib.Path,
                           transcript: pathlib.Path, exc: Exception,
                           demo_mode: bool = False) -> None:
    """Handle a failed transcription without ever producing a silent mismatch.

    This used to copy the golden London transcript into the job and leave its own
    audio in place. The result played the user's recording under someone else's
    words, with the dialogue running ~61s past the end of the audio, and the flag
    that was supposed to say so had been buried by a later meta.json write. Four
    jobs were affected.

    So the default is honest: no transcript, with the reason recorded. Demo mode
    is opt-in and copies the golden transcript *and* the golden audio together,
    because a mismatched pair is the actual defect -- not the substitution itself.
    """
    reason = (str(exc) or "unknown error")
    prov = {"transcript_failed": True, "reason": reason}

    used = None
    for part in ("audio.wav", "full_mono.wav"):
        if (job_dir / part).exists():
            used = part
            break
    prov["audio_used"] = used

    if demo_mode and GOLDEN_TRANSCRIPT.exists() and GOLDEN_AUDIO.exists():
        shutil.copy(GOLDEN_TRANSCRIPT, transcript)
        shutil.copy(GOLDEN_AUDIO, job_dir / "audio.wav")
        prov["transcript_source"] = "golden-london"
        prov["demo_mode"] = True
        prov["note"] = ("DEMO material: both the transcript and the audio were "
                        "replaced with the golden London clip. This is not the "
                        "uploaded recording.")
    else:
        transcript.write_text(json.dumps({"segments": []}), encoding="utf-8")
        prov["transcript_source"] = "none"
        # Always set, so a consumer never has to guess whether the key was absent
        # or the value was false.
        prov["demo_mode"] = False
        prov["note"] = ("transcription failed, so this job has no dialogue. "
                        "The uploaded audio has been left untouched.")
        (job_dir / "HONEST_EMPTY").write_text(
            "whisperx transcribe failed: " + reason, encoding="utf-8")

    write_provenance(job_dir, **prov)

    # Mirror into meta.json for the existing readers, but provenance.json is now
    # the authority: meta.json can be overwritten by the upload stub.
    mf = job_dir / "meta.json"
    try:
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}
    for k in ("transcript_failed", "reason", "transcript_source", "demo_mode", "audio_used"):
        if k in prov:
            meta[k] = prov[k]
    mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    (job_dir / "FALLBACK.md").write_text(
        "WhisperX transcribe failed: " + reason
        + "\naudio used: " + str(used or "?")
        + "\ntranscript source: " + str(prov["transcript_source"])
        + "\ndemo mode: " + str(bool(prov.get("demo_mode")))
        + "\n" + str(prov.get("note", "")),
        encoding="utf-8")


def run_audio_job(job_id: str, audio_path: pathlib.Path, expected_speakers: int | None = None,
                  demo_mode: bool = False):
    import shutil
    job_dir = JOBS / job_id
    try:
        # persist expected for the verified diarization pass / future re-detect
        if expected_speakers:
            try:
                mf0 = job_dir / "meta.json"
                _m0 = {}
                if mf0.exists():
                    import json as _js
                    try:
                        _m0 = _js.loads(mf0.read_text(encoding="utf-8"))
                    except Exception:
                        _m0 = {}
                _m0["expected_speakers"] = expected_speakers
                import json as _js2
                mf0.write_text(_js2.dumps(_m0, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        set_status(job_dir, "transcribing")
        transcript = job_dir / "transcript.json"
        speaker_report = {}
        try:
            cmd = [str(audio_path), "-o", str(transcript)]
            if expected_speakers and expected_speakers > 0:
                # --require-speakers makes the pipeline verify the voice count
                # instead of trusting one clustering pass. Previously min/max were
                # passed but the outcome was never checked, so a job could ship
                # with 2 voices while meta.json promised 4.
                cmd += ["--require-speakers", str(expected_speakers)]
            run_module("app.pipeline.transcribe", cmd, timeout=1800)
            try:
                speaker_report = (json.loads(transcript.read_text(encoding="utf-8"))
                                  .get("diarization") or {})
            except Exception:
                speaker_report = {}
            detected = speaker_report.get("detected_speakers")
            matched = speaker_report.get("matched")
            if expected_speakers and matched is False:
                print(f"[speakers] {job_id}: asked for {expected_speakers}, "
                      f"best effort detected {detected}", flush=True)
            # Provenance on success too, so "transcript_source" always has a
            # value. Previously only failures recorded one, which left "was this
            # transcript ever verified?" unanswerable for every healthy job.
            write_provenance(
                job_dir,
                transcript_failed=False,
                transcript_source="whisperx",
                demo_mode=False,
                speakers_expected=expected_speakers,
                speakers_detected=detected,
                speaker_count_matched=matched,
            )
        except Exception as e:
            print("WHISPERX_TRANSCRIBE_FAILED: " + str(e), flush=True)
            _on_transcribe_failure(job_dir, audio_path, transcript, e,
                                   demo_mode=demo_mode)

        # per-job icons: Iconify API (150+ sets) with separate cache + SVG, local dictionary fallback
        try:
            icon_dict_p = ROOT / "tools" / "icons" / "dictionary.json"
            iconify_cache_p = ROOT / "tools" / "icons" / "iconify_cache.json"
            if transcript.exists():
                import json as _js_icon
                # load local bib
                dct = {}
                if icon_dict_p.exists():
                    try:
                        dct = _js_icon.loads(icon_dict_p.read_text(encoding="utf-8"))
                    except Exception:
                        dct = {}
                dct_l = {k.lower(): v for k, v in dct.items()}
                # load separate Iconify cache
                iconify_cache = {}
                if iconify_cache_p.exists():
                    try:
                        iconify_cache = _js_icon.loads(iconify_cache_p.read_text(encoding="utf-8"))
                    except Exception:
                        iconify_cache = {}
                tr = _js_icon.loads(transcript.read_text(encoding="utf-8"))
                icons = []
                default_icon = "💬"
                # helper to query Iconify - only colored (palette) icons
                def iconify_search(word: str):
                    wl = word.lower()
                    if wl in iconify_cache:
                        return iconify_cache[wl]
                    # skip stopwords for Iconify - use local only
                    if wl in ("what","is","are","you","doing","the","a","an","and","or","to","of","in","on","at","for","with","about","i","m","it","s","this","that","be","have","has","had","will","would","can","could","should","need","there","here","module","lesson"):
                        iconify_cache[wl] = None
                        return None
                    try:
                        import requests
                        # Iconify search - prefer colored emoji sets, no logos
                        r = requests.get(f"https://api.iconify.design/search?query={wl}&limit=10", timeout=2)
                        if r.status_code == 200:
                            data = r.json()
                            lst = data.get("icons") or []
                            cols = data.get("collections") or {}
                            # filter to only colored (palette true) and not logos, prefer emoji sets
                            colored_icons = []
                            for icon in lst:
                                prefix = icon.split(":")[0] if ":" in icon else ""
                                col = cols.get(prefix, {})
                                # palette true means colored, and category not Logos
                                if col.get("palette") and col.get("category") != "Logos":
                                    # prefer emoji sets
                                    if prefix in ("noto","twemoji","openmoji","emojione","fluent-emoji","fluent-emoji-flat","fluent-emoji-high-contrast"):
                                        colored_icons.insert(0, icon)
                                    else:
                                        colored_icons.append(icon)
                            pick = None
                            if colored_icons:
                                pick = colored_icons[0]
                            else:
                                # no colored found, don't use monochrome - fallback to local/default
                                pick = None
                            iconify_cache[wl] = pick
                            try:
                                iconify_cache_p.write_text(_js_icon.dumps(iconify_cache, ensure_ascii=False, indent=2), encoding="utf-8")
                            except Exception:
                                pass
                            return pick
                    except Exception:
                        pass
                    iconify_cache[wl] = None
                    return None

                for seg in tr.get("segments", []):
                    found = False
                    # try word-level exact in local bib first - use segment timing for slower display (whole segment, not just word)
                    for w in seg.get("words", []):
                        word = str(w.get("word", "")).lower().strip(".,!?\"'").strip()
                        if not word:
                            continue
                        if word in dct_l:
                            icons.append({"word": w.get("word"), "icon": dct_l[word], "start": seg.get("start"), "end": seg.get("end")})
                            found = True
                            break
                    if not found:
                        for word in str(seg.get("text", "")).split():
                            lw = word.lower().strip(".,!?\"'").strip()
                            if lw in dct_l:
                                icons.append({"word": word, "icon": dct_l[lw], "start": seg.get("start"), "end": seg.get("end")})
                                found = True
                                break
                    if not found:
                        # Iconify fallback for new words
                        seg_text = str(seg.get("text", "")).strip()
                        if seg_text:
                            # try first word of segment via Iconify
                            first_word = seg_text.split()[0].strip(".,!?\"'")
                            # try Iconify search
                            icon = iconify_search(first_word)
                            if icon:
                                # store as Iconify identifier, player will render SVG via api.iconify.design
                                icons.append({"word": first_word, "icon": icon, "start": seg.get("start"), "end": seg.get("end"), "isIconify": True})
                                found = True
                            else:
                                # try any word in segment via Iconify
                                for word in seg_text.split():
                                    lw2 = word.lower().strip(".,!?\"'").strip()
                                    if not lw2:
                                        continue
                                    icon2 = iconify_search(lw2)
                                    if icon2:
                                        icons.append({"word": word, "icon": icon2, "start": seg.get("start"), "end": seg.get("end"), "isIconify": True})
                                        found = True
                                        break
                    if not found:
                        txt = str(seg.get("text", "")).strip()
                        if txt:
                            first = txt.split()[0].strip(".,!?\"'")
                            icons.append({"word": first, "icon": default_icon, "start": seg.get("start"), "end": seg.get("end")})
                (job_dir / "icons.json").write_text(_js_icon.dumps(icons, ensure_ascii=False, indent=2), encoding="utf-8")
                # also persist iconify cache at end if updated
                try:
                    if iconify_cache_p.exists():
                        # already written incrementally, but ensure final write
                        pass
                except Exception:
                    pass
        except Exception as e:
            print(f"[icons] generation failed: {e}", flush=True)

        set_status(job_dir, "structuring")
        script_path = job_dir / "script.json"
        try:
            run_module("app.pipeline.structure_script", [str(transcript), "--output", str(script_path)], timeout=75)
        except Exception:
            transcript_data = json.loads(transcript.read_text(encoding="utf-8"))
            script_path.write_text(json.dumps(script_from_transcript(transcript_data), ensure_ascii=False, indent=2), encoding="utf-8")

        set_status(job_dir, "emotions")
        line_emo = job_dir / "line_emotions.json"
        try:
            run_module("app.pipeline.detect_emotions", ["--script", str(script_path), "-o", str(line_emo)], timeout=90)
        except Exception:
            pass

        set_status(job_dir, "lipsyncing")
        visemes = job_dir / "visemes.json"
        try:
            run_module("app.pipeline.lipsync", [str(transcript), str(visemes), "--audio", str(audio_path)], timeout=300)
        except Exception:
            visemes.write_text(json.dumps({}), encoding="utf-8")

        set_status(job_dir, "assembling")
        dialogue = assemble_dialogue(job_dir, script_path)
        # add walkins for audio jobs and ensure clip variety + correct speaker count per job
        try:
            dlg_data = json.loads(dialogue.read_text(encoding="utf-8"))
            segs = dlg_data.get("segments", [])
            speakers = dlg_data.get("speakers", {})
            # prune speakers to actually used + expected (so 2-speaker job doesn't have 4)
            used = {s.get("speaker", "SPEAKER_00") for s in segs}
            # respect expected_speakers from meta (selected in config.html)
            exp = expected_speakers
            if not exp:
                try:
                    _m = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
                    exp = _m.get("expected_speakers")
                except Exception:
                    exp = None
            # if expected is set and used count != expected, we keep used (honest) but prune extras
            # also ensure speakers dict matches used
            if speakers and used and set(speakers.keys()) != used:
                # keep only used speakers with full config, preserve positions, use detected gender
                # load gender map if exists
                gender_map = {}
                try:
                    gm = job_dir / "speaker_genders.json"
                    if gm.exists():
                        gender_map = json.loads(gm.read_text(encoding="utf-8"))
                except Exception:
                    pass
                pruned = {}
                for sid in sorted(used):
                    if sid in speakers and isinstance(speakers[sid], dict) and speakers[sid].get("position"):
                        # keep existing but patch body/avatar if gender detected and mismatched
                        cfg = dict(speakers[sid])
                        g = gender_map.get(sid)
                        if g and cfg.get("body") != g:
                            # update body to match detected gender, keep avatar if already correct gender
                            cfg["body"] = g
                            # if avatar mismatched gender, fix it
                            is_woman_avatar = "woman" in cfg.get("avatarUrl","").lower()
                            if (g=="F" and not is_woman_avatar) or (g=="M" and is_woman_avatar):
                                cfg["avatarUrl"] = "assets/woman1.glb" if g=="F" else "assets/man1.glb"
                        pruned[sid] = cfg
                    else:
                        try:
                            idx = int(sid.split("_")[1])
                        except:
                            idx = len(pruned)
                        g = gender_map.get(sid)
                        pruned[sid] = _ensure_full_speaker(sid, idx, speakers, gender=g)
                dlg_data["speakers"] = pruned
                speakers = pruned
            # also patch existing speakers with detected gender even if not pruned (e.g., 2 vs 2)
            else:
                try:
                    gm = job_dir / "speaker_genders.json"
                    if gm.exists():
                        gender_map = json.loads(gm.read_text(encoding="utf-8"))
                        for sid, g in gender_map.items():
                            if sid in speakers and isinstance(speakers[sid], dict):
                                if speakers[sid].get("body") != g:
                                    speakers[sid]["body"] = g
                                    is_woman = "woman" in speakers[sid].get("avatarUrl","").lower()
                                    if (g=="F" and not is_woman) or (g=="M" and is_woman):
                                        speakers[sid]["avatarUrl"] = "assets/woman1.glb" if g=="F" else "assets/man1.glb"
                        dlg_data["speakers"] = speakers
                except Exception:
                    pass
            # Entrances and positions are DERIVED from the segments, so they are
            # re-derived once after this block rather than here: this block ends
            # in `except Exception: pass`, and a failure inside it must not be
            # able to leave a stale schedule behind.
            speakers = dlg_data.get("speakers", {})
            # ensure clip variety - not all Talking.fbx
            try:
                catalog = json.loads(GOLDEN_CATALOG.read_text(encoding="utf-8")) if GOLDEN_CATALOG.exists() else {}
                go_map = catalog.get("go_emotions", {})
                for seg in segs:
                    # if clip is generic Talking, try to pick based on mood
                    cur = seg.get("clip", "")
                    mood = seg.get("mood") or seg.get("go_label") or "neutral"
                    if not cur or "Talking" in cur:
                        opts = go_map.get(mood) or go_map.get("neutral") or ["Talking.fbx"]
                        # pick deterministically based on segment start
                        import hashlib
                        h = int(hashlib.md5(f"{seg.get('start',0)}-{seg.get('text','')}".encode()).hexdigest()[:8], 16)
                        seg["clip"] = f"media/3d/mixamo/clips/{opts[h % len(opts)]}"
            except Exception:
                pass
            dialogue.write_text(json.dumps(dlg_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        # Re-derive the entrances OUTSIDE that try/except. The block above ends in
        # `except Exception: pass`, so a failure there used to leave whatever
        # schedule was already on disk -- which is how stale entrances survived
        # unnoticed. This run is guaranteed to either produce a consistent
        # schedule or record why it could not.
        try:
            final = json.loads(dialogue.read_text(encoding="utf-8"))
            entrance = refresh_entrance(final)
            dialogue.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
            write_provenance(job_dir, entrances_ok=entrance["ok"],
                             entrance_problems=entrance["problems"][:8],
                             entrances_refreshed_at=time.time())
        except Exception as exc:
            write_provenance(job_dir, entrances_ok=None,
                             entrance_error=str(exc)[:200])
        to_player_audio(job_dir, audio_path)

        _refuse_empty_dialogue(job_dir)

        set_status(job_dir, "done")
    except Exception as e:
        set_status(job_dir, "failed")
        (job_dir / "status.json").write_text(json.dumps({"status": "failed", "error": str(e)}), encoding="utf-8")


def run_generate_job(job_id: str, prompt: str):
    job_dir = JOBS / job_id
    try:
        set_status(job_dir, "generating")
        script_path = job_dir / "script.json"
        fallback = fallback_family_script(prompt)
        try:
            from app.pipeline.llm import generate_structured, StructuredOutputError
            from app.schemas.script import SceneScript
            system = (
                "You are a dialogue-scene writer. Write a script for a 4-speaker family "
                "scene (man, woman, girl, boy) for a 3D animation renderer.\n"
                "Characters: id must be SPEAKER_00 (man), SPEAKER_01 (girl), "
                "SPEAKER_02 (woman), SPEAKER_03 (boy); speaker_ref equals id.\n"
                "Emit 10-14 lines of dialogue where every line has start/end seconds, "
                "emotion from neutral/happy/sad/angry/surprised/worried/excited/annoyed, "
                "and one camera shot per line focused on its speaker.\n"
                "Respond with JSON only matching the provided schema."
            )
            script = run_with_timeout(
                lambda: generate_structured(
                    SceneScript, system, prompt,
                    validate=lambda s: None,
                    max_attempts=2, deadline=75,
                ),
                75,
            )
            script_path.write_text(json.dumps(script.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            script_path.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")

        set_status(job_dir, "emotions")
        line_emo = job_dir / "line_emotions.json"
        try:
            run_module("app.pipeline.detect_emotions", ["--script", str(script_path), "-o", str(line_emo)], timeout=90)
        except Exception:
            pass

        set_status(job_dir, "assembling")
        dialogue = assemble_dialogue(job_dir, script_path)
        script = json.loads(script_path.read_text(encoding="utf-8"))
        duration = max((l["end"] for l in script.get("lines", [])), default=10.0) + 1.0
        silent_audio(job_dir, duration)

        set_status(job_dir, "done")
    except Exception as e:
        set_status(job_dir, "failed")
        (job_dir / "status.json").write_text(json.dumps({"status": "failed", "error": str(e)}), encoding="utf-8")


@app.post("/api/jobs")
async def create_job(request: Request, background_tasks: BackgroundTasks = None):
    body = await request.body()
    fname = request.headers.get("X-Filename", "input.wav")
    exp_raw = request.headers.get("X-Expected-Speakers", "")
    expected = None
    try:
        if exp_raw and exp_raw.strip().isdigit():
            v = int(exp_raw.strip())
            if 1 <= v <= 8:
                expected = v
    except Exception:
        expected = None
    job_id = str(uuid.uuid4())[:8]
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_path = job_dir / "input.wav"
    audio_path.write_bytes(body)
    (job_dir / "status.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
    # Opt-in only: if WhisperX fails, fall back to the golden London clip *as a
    # matched pair* (transcript and audio together) rather than mixing the
    # golden transcript with this upload. Default is an honest empty transcript.
    demo_mode = (request.headers.get("X-Demo-Mode") or "").strip().lower() in ("1", "true", "yes")
    meta = {"filename": fname, "size": len(body), "demo_mode": demo_mode}
    if expected:
        meta["expected_speakers"] = expected
    (job_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if background_tasks is None:
        return {"job_id": job_id, "status": "queued"}
    background_tasks.add_task(run_audio_job, job_id, audio_path, expected, demo_mode)
    return {"job_id": job_id, "status": "queued"}


def _ensure_full_speaker(speaker_id: str, idx: int, template: dict | None = None, gender: str | None = None) -> dict:
    """Create a full speaker entry with position/avatarUrl so player never crashes.
    Uses template if available, else defaults. Gender F/M influences avatar choice (woman/man)."""
    # if gender is known, use it to pick avatar
    def pick_avatar_for_gender(g, i):
        if g == "F":
            return ["assets/woman1.glb", "assets/woman.glb"][i % 2]
        if g == "M":
            return ["assets/man1.glb", "assets/man.glb"][i % 2]
        return ["assets/woman1.glb", "assets/man1.glb", "assets/woman.glb", "assets/man.glb"][i % 4]
    if template and speaker_id in template and isinstance(template[speaker_id], dict):
        t = template[speaker_id]
        if t.get("position") and t.get("avatarUrl"):
            # patch body to match gender if provided
            if gender and t.get("body") != gender:
                # keep avatarUrl but update body
                t = dict(t)
                t["body"] = gender
            return t
        base = dict(t)
        if not base.get("position"):
            base["position"] = {"x": round(-0.8 + idx * 0.6, 3), "y": 0, "z": -0.85}
        if not base.get("avatarUrl"):
            base["avatarUrl"] = pick_avatar_for_gender(gender, idx)
        if not base.get("name"):
            base["name"] = f"speaker_{idx+1}"
        if not base.get("body"):
            base["body"] = gender if gender in ("F","M") else ("F" if idx % 2 == 0 else "M")
        if "rotation" not in base:
            base["rotation"] = 3.03163690725
        if not base.get("lipsyncLang"):
            base["lipsyncLang"] = "en"
        return base
    # fallback defaults - use gender if known
    if gender == "F":
        avatars = ["assets/woman1.glb", "assets/woman.glb"]
        bodies = ["F", "F"]
    elif gender == "M":
        avatars = ["assets/man1.glb", "assets/man.glb"]
        bodies = ["M", "M"]
    else:
        avatars = ["assets/woman1.glb", "assets/man1.glb", "assets/woman.glb", "assets/man.glb"]
        bodies = ["F", "M", "F", "M"]
    names = ["girl", "woman", "man", "boy"]
    return {
        "name": names[idx % len(names)] if idx < len(names) else f"speaker_{idx+1}",
        "avatarUrl": avatars[idx % len(avatars)],
        "body": bodies[idx % len(bodies)],
        "lipsyncLang": "en",
        "position": {"x": round(-0.8 + idx * 0.6, 3), "y": 0, "z": -0.85},
        "rotation": 3.03163690725,
    }


def _persist_dialogue_edits(job_dir: pathlib.Path, segments: list, speakers: dict) -> None:
    """Persist user corrections from the edit UI (B). Overwrites dialogue.json
    and line_emotions.json so subsequent saves/renders use the corrected
    speakers/emotions. Honest: never fabricate data, only what the user set.
    Merges speaker configs to preserve position/avatarUrl so player doesn't crash."""
    dialogue = job_dir / "dialogue.json"
    if not dialogue.exists():
        raise RuntimeError("dialogue.json missing — nothing to edit yet.")
    existing = json.loads(dialogue.read_text(encoding="utf-8"))
    old_speakers = existing.get("speakers", {}) if isinstance(existing.get("speakers"), dict) else {}
    # merge: keep full config for existing speakers, synthesize missing ones
    merged = {}
    # first, preserve all old speakers that are still referenced, with full data
    for sid, cfg in old_speakers.items():
        if isinstance(cfg, dict) and cfg.get("position"):
            merged[sid] = cfg
        else:
            # patch minimal old entry
            try:
                idx = int(sid.split("_")[1])
            except Exception:
                idx = 0
            merged[sid] = _ensure_full_speaker(sid, idx, old_speakers)
    # now ensure every speaker in new `speakers` dict has full config
    for sid in speakers.keys():
        if sid not in merged:
            try:
                idx = int(sid.split("_")[1])
            except Exception:
                idx = len(merged)
            merged[sid] = _ensure_full_speaker(sid, idx, old_speakers if old_speakers else None)
        else:
            # keep existing full, but update with any new fields if provided and not minimal
            # Always overwrite avatarUrl/body/name/position/lipsyncLang when provided — not just when len>1
            # (len==1 case is old minimal spkMap from renderJobLines, which should NOT overwrite avatar)
            if isinstance(speakers[sid], dict):
                incoming = speakers[sid]
                # if incoming looks like minimal spkMap (only speaker_ref), don't overwrite avatar/name
                is_minimal = len(incoming) == 1 and "speaker_ref" in incoming
                if not is_minimal:
                    # overwrite only meaningful fields, preserve any missing ones from merged
                    for k in ("avatarUrl", "body", "name", "position", "rotation", "lipsyncLang"):
                        if k in incoming and incoming[k] is not None:
                            merged[sid][k] = incoming[k]
                    # also merge any extra fields
                    for k, v in incoming.items():
                        if k not in merged[sid]:
                            merged[sid][k] = v
    # also ensure every speaker referenced in segments exists in merged
    seg_speakers = {s.get("speaker", "SPEAKER_00") for s in segments}
    for sid in seg_speakers:
        if sid not in merged:
            try:
                idx = int(sid.split("_")[1])
            except Exception:
                idx = len(merged)
            merged[sid] = _ensure_full_speaker(sid, idx, old_speakers)
    existing["segments"] = segments
    existing["speakers"] = merged
    # Re-derive the entrance schedule here too. This is the path that edits take
    # (PUT /dialogue and remap), and it is also what player-side spot saves go
    # through -- which is how positions got rewritten for 4 speakers while the
    # entrances kept describing 2, in job ce2f201a.
    entrance = refresh_entrance(existing)
    dialogue.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    write_provenance(job_dir, entrances_ok=entrance["ok"],
                     entrance_problems=entrance["problems"][:8],
                     entrances_refreshed_at=time.time())
    emo_path = job_dir / "line_emotions.json"
    if emo_path.exists():
        emo_map = {}
        for i, seg in enumerate(segments):
            key = f"{seg.get('start', 0):.2f}-{i}"
            emo_map[f"{seg.get('end', 0):.2f}"] = {"start": seg.get("start", 0),
                                                  "end": seg.get("end", 0),
                                                  "top": seg.get("mood", "neutral"),
                                                  "segments": {str(i): {"speaker": seg.get("speaker", "SPEAKER_00"), "emotion": seg.get("mood", "neutral")}}}
        emo_path.write_text(json.dumps(emo_map, ensure_ascii=False, indent=2), encoding="utf-8")


@app.put("/api/jobs/{job_id}/dialogue")
async def put_dialogue(job_id: str, request: Request):
    job_dir = JOBS / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="job not found")
    body = await request.json()
    segments = body.get("segments")
    speakers = body.get("speakers")
    if not isinstance(segments, list):
        raise HTTPException(status_code=400, detail="segments must be an array")
    if not isinstance(speakers, dict):
        raise HTTPException(status_code=400, detail="speakers must be an object")
    # honest validation: nonempty, numeric times
    good = []
    for s in segments:
        if not isinstance(s, dict):
            continue
        if not s.get("text"):
            continue
        try:
            st = float(s.get("start", 0))
            en = float(s.get("end", 0))
        except (TypeError, ValueError):
            continue
        good.append({**s, "start": st, "end": en})
    if not good:
        raise HTTPException(status_code=400, detail="no valid dialogue segments to save")
    _persist_dialogue_edits(job_dir, good, speakers)
    return {"ok": True, "saved": len(good)}


class SpeakerSplitRefused(ValueError):
    """Raised when a caller asks for more speakers than the audio actually has.

    Carries the counts so the API can answer with something the user can act on
    instead of inventing labels.
    """

    def __init__(self, detected: int, requested: int):
        # BaseException.__init__ takes *args only -- passing keyword arguments
        # here raised TypeError and turned every refusal into a 500.
        super().__init__(
            f"cannot make {requested} speakers out of {detected} detected"
        )
        self.detected = detected
        self.requested = requested


def _remap_speakers_to_target(segments: list, target: int) -> tuple[list, dict]:
    """Remap segments to exactly `target` speakers, honest: never invent text, only reassign speaker labels.

    Merging is allowed. Splitting raises SpeakerSplitRefused -- see the branch below.
    """
    if not segments or target < 1 or target > 8:
        raise ValueError("invalid target")
    # collect unique speakers sorted
    uniq = sorted({s.get("speaker", "SPEAKER_00") for s in segments})
    u = len(uniq)
    idx_of = {spk: i for i, spk in enumerate(uniq)}
    new_segs = []
    if target == u:
        return segments, {}
    if target < u:
        # Merge is honest: two real voices become one character on stage.
        for s in segments:
            old = s.get("speaker", "SPEAKER_00")
            old_idx = idx_of.get(old, 0)
            new_idx = old_idx % target
            ns = f"SPEAKER_{new_idx:02d}"
            new_segs.append({**s, "speaker": ns})
    else:
        # Splitting is NOT honest and is refused. This used to assign
        # `i % target`, cycling speaker 0,1,2,3,0,1,... through the lines and
        # discarding every real grouping the diarizer had found. On job b171c19f
        # that produced a perfect synthetic cycle that read as a confident
        # four-person conversation. A voice cannot be recovered by arithmetic on
        # line numbers -- it has to come from the audio. Callers must re-detect.
        raise SpeakerSplitRefused(u, target)
    # build full speakers with positions so player never gets minimal
    # try to reuse existing dialogue's speakers as template if available
    speakers = {}
    for i in range(target):
        sid = f"SPEAKER_{i:02d}"
        speakers[sid] = _ensure_full_speaker(sid, i, None)
    return new_segs, speakers


@app.post("/api/jobs/{job_id}/remap")
async def remap_job(job_id: str, request: Request):
    """Merge real voices together. Splitting is refused: it cannot be faked."""
    job_dir = JOBS / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="job not found")
    body = await request.json()
    target = body.get("target", body.get("speakers", body.get("count")))
    try:
        target = int(target)
    except Exception:
        raise HTTPException(status_code=400, detail="target must be integer 1-8")
    if not 1 <= target <= 8:
        raise HTTPException(status_code=400, detail="target must be 1-8")
    dialogue_path = job_dir / "dialogue.json"
    if not dialogue_path.exists():
        raise HTTPException(status_code=404, detail="dialogue.json not ready")
    data = json.loads(dialogue_path.read_text(encoding="utf-8"))
    segs = data.get("segments") or []
    if not segs:
        raise HTTPException(status_code=400, detail="no segments to remap")
    try:
        new_segs, speakers = _remap_speakers_to_target(segs, target)
    except SpeakerSplitRefused as refused:
        # 409, not 200: the request is answerable, but not by making speakers up.
        raise HTTPException(
            status_code=409,
            detail=(
                f"This recording has {refused.detected} speaker"
                f"{'' if refused.detected == 1 else 's'}, so {refused.requested} cannot be "
                f"produced from it. Use Re-detect to run diarization again -- extra voices "
                f"have to come from the audio, not from renumbering lines."
            ),
        )
    _persist_dialogue_edits(job_dir, new_segs, speakers)
    # also update meta for future
    try:
        mf = job_dir / "meta.json"
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        meta["remapped_speakers"] = target
        meta["expected_speakers"] = target
        mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "target": target, "segments": len(new_segs)}


@app.get("/api/clips")
async def get_clips():
    """Return global clip_catalog.json and available fbx list."""
    try:
        catalog = json.loads(GOLDEN_CATALOG.read_text(encoding="utf-8")) if GOLDEN_CATALOG.exists() else {"go_emotions": {}, "clips_dir": "media/3d/mixamo/clips"}
    except Exception:
        catalog = {"go_emotions": {}, "clips_dir": "media/3d/mixamo/clips"}
    # list available fbx files
    clips_dir = ROOT / "media" / "3d" / "mixamo" / "clips"
    available = []
    try:
        if clips_dir.exists():
            available = sorted([p.name for p in clips_dir.glob("*.fbx")])
    except Exception:
        pass
    return {"catalog": catalog, "available": available}


@app.get("/api/clips/list")
async def list_clips():
    clips_dir = ROOT / "media" / "3d" / "mixamo" / "clips"
    try:
        if clips_dir.exists():
            return sorted([p.name for p in clips_dir.glob("*.fbx")])
    except Exception:
        pass
    return []


@app.get("/api/avatars")
async def list_avatars():
    """List available avatar GLBs (man, man1, woman, woman1)."""
    avatars_dir = ROOT / "assets"
    try:
        if avatars_dir.exists():
            # only the 4 main avatars, keep centered
            wanted = {"man.glb", "man1.glb", "woman.glb", "woman1.glb"}
            found = [p.name for p in avatars_dir.glob("*.glb") if p.name in wanted]
            # sort to have woman1, woman, man1, man
            order = {"woman1.glb": 0, "woman.glb": 1, "man1.glb": 2, "man.glb": 3}
            found.sort(key=lambda x: order.get(x, 99))
            if found:
                return found
            # fallback: any glb
            return sorted([p.name for p in avatars_dir.glob("*.glb")])[:8]
    except Exception:
        pass
    return ["woman1.glb", "woman.glb", "man1.glb", "man.glb"]


@app.post("/api/clips")
async def post_clips(request: Request):
    """Update global clip_catalog.json - merges go_emotions."""
    body = await request.json()
    # allow either full catalog or just go_emotions
    try:
        current = json.loads(GOLDEN_CATALOG.read_text(encoding="utf-8")) if GOLDEN_CATALOG.exists() else {"go_emotions": {}}
    except Exception:
        current = {"go_emotions": {}}
    if "go_emotions" in body:
        current["go_emotions"] = body["go_emotions"]
    elif "catalog" in body:
        # full catalog
        current = body["catalog"]
    else:
        # assume body is go_emotions directly
        if isinstance(body, dict) and all(isinstance(v, list) for v in body.values()):
            current["go_emotions"] = body
        else:
            raise HTTPException(status_code=400, detail="body must contain go_emotions or catalog")
    # ensure clips exist
    GOLDEN_CATALOG.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_CATALOG.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "emotions": len(current.get("go_emotions", {}))}


class GenerateRequest(BaseModel):
    prompt: str = "Family asking to go to London, 4 characters girl/woman/man/boy, 30 lines"


@app.post("/api/generate")
async def generate_scene(req: GenerateRequest, background_tasks: BackgroundTasks = None):
    job_id = str(uuid.uuid4())[:8]
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "status.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
    if background_tasks is None:
        return {"job_id": job_id, "status": "queued"}
    background_tasks.add_task(run_generate_job, job_id, req.prompt)
    return {"job_id": job_id, "status": "queued"}


from fastapi.responses import FileResponse

@app.post("/save")
async def save_dialogue(request: Request):
    body = await request.body()
    out = JOBS / "picker" / "picker.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    return {"ok": True}


@app.get("/api/jobs")
async def list_jobs():
    out = []
    if not JOBS.exists():
        return out
    for d in JOBS.iterdir():
        if not d.is_dir():
            continue
        meta_p = d / "meta.json"
        status_p = d / "status.json"
        dialogue_p = d / "dialogue.json"
        if not meta_p.exists() and not status_p.exists():
            continue
        # skip golden/picker/system dirs
        if d.name in ("golden", "picker", "default"):
            continue
        # Repair backups are job-shaped (they carry meta/status), so they used to
        # appear in the projects list as if they were real work -- e.g.
        # "#_backup-speaker-count-20261003-134956, 33 lines, 4 spk".
        if d.name.startswith("_"):
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
            status = json.loads(status_p.read_text(encoding="utf-8")) if status_p.exists() else {"status": "unknown"}
            dialogue = None
            segs = 0
            speakers = 0
            if dialogue_p.exists():
                try:
                    dialogue = json.loads(dialogue_p.read_text(encoding="utf-8"))
                    segs = len(dialogue.get("segments", []))
                    speakers = len(dialogue.get("speakers", {}))
                except Exception:
                    pass
            # Speaker counts, kept apart on purpose. The old fallback did
            # `speakers = meta["expected_speakers"]`, which reported the number
            # the user asked for as though it had been detected -- so job
            # b171c19f showed "4 spk" in the list while the transcript held 2.
            prov = read_provenance(d)
            detected = prov.get("speakers_detected")
            if detected is None and dialogue:
                detected = len(dialogue.get("segments") and
                               {s.get("speaker") for s in dialogue["segments"]} or set())
            mtime = d.stat().st_mtime
            out.append({
                "id": d.name,
                "filename": meta.get("filename", d.name),
                "name": meta.get("name", meta.get("filename", d.name)),
                "size": meta.get("size", 0),
                "expected_speakers": meta.get("expected_speakers"),
                "speakers_detected": detected,
                "speaker_count_matched": prov.get("speaker_count_matched"),
                "remapped_speakers": meta.get("remapped_speakers"),
                "transcript_failed": meta.get("transcript_failed", False),
                "transcript_source": meta.get("transcript_source"),
                "demo_mode": bool(meta.get("demo_mode")),
                # Checked for every row so the projects list can warn about a
                # dialogue that does not match its own audio, instead of the
                # mismatch only surfacing when someone presses Play.
                "consistency": dialogue_vs_audio(d),
                "status": status.get("status", "unknown"),
                "segments": segs,
                "speakers": speakers,
                "mtime": mtime,
                "createdAt": mtime,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


@app.patch("/api/jobs/{job_id}")
async def patch_job(job_id: str, request: Request):
    job_dir = JOBS / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="job not found")
    body = await request.json()
    name = body.get("name")
    if name is not None:
        name = str(name).strip()[:80]
        mf = job_dir / "meta.json"
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        meta["name"] = name
        mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "name": name}
    raise HTTPException(status_code=400, detail="nothing to update")


@app.post("/api/jobs/{job_id}/retranscribe")
async def retranscribe_job(job_id: str, background_tasks: BackgroundTasks = None):
    """Re-run transcription against this job's own uploaded audio.

    The repair path for a job whose dialogue does not match its audio. There was
    no way to do this before: the only routes were upload a new file or hand-edit
    the job directory, so four jobs sat permanently mismatched.
    """
    job_dir = JOBS / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="job not found")

    source = None
    for name in ("input.wav", "full_mono.wav"):
        if (job_dir / name).exists():
            source = job_dir / name
            break
    if source is None:
        raise HTTPException(
            status_code=409,
            detail="this job has no uploaded audio to transcribe; re-upload the file",
        )

    # Clear the mismatched artefacts so a half-finished retry cannot leave the
    # old dialogue on screen next to a new transcript.
    for stale in ("dialogue.json", "transcript.json", "FALLBACK.md", "HONEST_EMPTY"):
        p = job_dir / stale
        if p.exists():
            p.unlink()

    # A demo job holds golden audio, not the upload: transcribing that would
    # regenerate the golden transcript anyway, and pretending otherwise is the
    # bug this endpoint exists to undo.
    prov = read_provenance(job_dir)
    if prov.get("demo_mode"):
        raise HTTPException(
            status_code=409,
            detail="this is a demo job holding the golden clip, not an upload; "
                   "nothing to re-transcribe",
        )

    expected = None
    try:
        meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
        expected = meta.get("expected_speakers")
    except (OSError, json.JSONDecodeError):
        pass

    set_status(job_dir, "queued")
    write_provenance(job_dir, retranscribedAt=time.time())
    if background_tasks is not None:
        background_tasks.add_task(run_audio_job, job_id, source, expected, False)
    return {"ok": True, "job_id": job_id, "audio": source.name, "queued": background_tasks is not None}


@app.post("/api/jobs/{job_id}/redetect")
async def redetect_speakers(job_id: str, request: Request, background_tasks: BackgroundTasks = None):
    """Re-run verified diarization against this job's own audio, targeting N voices.

    The honest replacement for the old round-robin remap. Speaker identity comes
    from the recording or it is not claimed: if pyannote cannot reach the
    requested count after every configuration is tried, the job still completes
    and provenance records how many were actually found.
    """
    job_dir = JOBS / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="job not found")

    body = await request.json()
    target = body.get("speakers", body.get("target"))
    try:
        target = int(target)
    except Exception:
        raise HTTPException(status_code=400, detail="speakers must be an integer 1-8")
    if not 1 <= target <= 8:
        raise HTTPException(status_code=400, detail="speakers must be 1-8")

    source = next((job_dir / n for n in ("input.wav", "full_mono.wav")
                   if (job_dir / n).exists()), None)
    if source is None:
        raise HTTPException(
            status_code=409,
            detail="this job has no uploaded audio to analyse; re-upload the file",
        )
    if read_provenance(job_dir).get("demo_mode"):
        raise HTTPException(
            status_code=409,
            detail="this is a demo job holding the golden clip, not an upload",
        )

    # Same clean slate as retranscribe: no half-updated artefacts on screen.
    for stale in ("dialogue.json", "transcript.json", "visemes.json"):
        p = job_dir / stale
        if p.exists():
            p.unlink()
    try:
        mf = job_dir / "meta.json"
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        meta["expected_speakers"] = target
        meta.pop("remapped_speakers", None)
        mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    set_status(job_dir, "queued")
    write_provenance(job_dir, redetectRequested=target, redetectAt=time.time())
    if background_tasks is not None:
        background_tasks.add_task(run_audio_job, job_id, source, target, False)
    return {"ok": True, "job_id": job_id, "requested_speakers": target,
            "queued": background_tasks is not None}


@app.post("/api/jobs/{job_id}/refresh-entrance")
async def refresh_entrance_job(job_id: str):
    """Re-derive walkins/entranceOrder/positions from the dialogue's own segments.

    The repair for a job whose characters fail to appear. The schedule is derived
    data and can drift from the lines -- when it does, the player hides whoever it
    thinks has not walked on yet, silently, mid-conversation.
    """
    job_dir = JOBS / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="job not found")
    dialogue = job_dir / "dialogue.json"
    if not dialogue.exists():
        raise HTTPException(status_code=404, detail="dialogue.json not ready")
    try:
        data = json.loads(dialogue.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"dialogue.json is not valid JSON: {exc}")
    before = entrances_consistent(data)
    after = refresh_entrance(data)
    dialogue.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_provenance(job_dir, entrances_ok=after["ok"],
                     entrance_problems=after["problems"][:8],
                     entrances_refreshed_at=time.time())
    return {"ok": True, "job_id": job_id, "before": before, "after": after,
            "speakers": after["speakers"]}


@app.post("/api/jobs/{job_id}/duplicate")
async def duplicate_job(job_id: str):
    src = JOBS / job_id
    if not src.exists():
        raise HTTPException(status_code=404, detail="job not found")
    new_id = str(uuid.uuid4())[:8]
    dst = JOBS / new_id
    import shutil
    shutil.copytree(src, dst)
    # update meta name
    try:
        mf = dst / "meta.json"
        if mf.exists():
            meta = json.loads(mf.read_text(encoding="utf-8"))
            base = meta.get("name") or meta.get("filename") or job_id
            meta["name"] = base + " (copy)"
            meta["duplicated_from"] = job_id
            mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        # reset status to done if was done
        sp = dst / "status.json"
        if sp.exists():
            sp.write_text(json.dumps({"status": "done", "progress": 100}), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "job_id": new_id}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job_dir = JOBS / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="job not found")
    import shutil
    shutil.rmtree(job_dir)
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    p = JOBS / job_id / "status.json"
    if not p.exists():
        return {"status": "not_found"}
    data = json.loads(p.read_text(encoding="utf-8"))
    status = data.get("status", "queued")
    started = data.get("stage_started_at", time.time())
    elapsed = max(0.0, time.time() - started)
    budget = STAGE_BUDGET.get(status, 15)
    span = STAGE_SPAN.get(status, (0, 100))
    frac = min(1.0, elapsed / max(budget, 1.0))
    progress = round(span[0] + frac * (span[1] - span[0]))
    if status in ("done", "failed"):
        progress = 100 if status == "done" else 100
    prov = read_provenance(JOBS / job_id)
    # status.json carries no line count, so config.html's "dialogue ready
    # (? segs)" had nothing to print. Read it from the dialogue itself.
    seg_count = 0
    entrances = {"ok": None, "problems": [], "checked": 0}
    try:
        dj = JOBS / job_id / "dialogue.json"
        if dj.exists():
            parsed = json.loads(dj.read_text(encoding="utf-8"))
            seg_count = len(parsed.get("segments", []))
            # Same shape as the audio check: report drift instead of letting the
            # player hide a character because of it.
            entrances = entrances_consistent(parsed)
    except Exception:
        seg_count = 0
    return {**data, "progress": min(100, max(0, progress)), "elapsed": round(elapsed, 1),
            "segments": seg_count,
            "consistency": dialogue_vs_audio(JOBS / job_id),
            "entrances": entrances,
            # Asked vs found, never conflated: the projects list used to show the
            # requested number as if it had been detected.
            "speakers_expected": prov.get("speakers_expected"),
            "speakers_detected": prov.get("speakers_detected"),
            "speaker_count_matched": prov.get("speaker_count_matched"),
            "transcript_source": prov.get("transcript_source"),
            "transcript_failed": prov.get("transcript_failed", False)}


@app.get("/api/jobs/{job_id}/result")
async def get_result(job_id: str):
    job_dir = JOBS / job_id
    return {
        "job_id": job_id,
        "dialogue": (job_dir / "dialogue.json").exists(),
        "audio": (job_dir / "audio.wav").exists(),
        "transcript": (job_dir / "transcript.json").exists(),
        "visemes": (job_dir / "visemes.json").exists(),
        "script": (job_dir / "script.json").exists(),
        "line_emotions": (job_dir / "line_emotions.json").exists(),
    }


#: Headers that stop a browser from silently reusing a stale copy of the app's
#: own code. Without an explicit Cache-Control, Chrome applies heuristic freshness
#: from Last-Modified and may serve an old HTML file without revalidating at all --
#: which is exactly what happened: an edited dialogue-player.html kept running the
#: pre-fix build, so a fixed bug was reported as still broken across several rounds
#: of debugging, and a hard refresh did not help.
#:
#: Scoped deliberately. HTML and JS/CSS must never be stale, because a stale module
#: silently changes behaviour. Large media (FBX/GLB/audio) stays cacheable so it is
#: not re-streamed on every reload.
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _no_store_html(path: str) -> FileResponse:
    return FileResponse(path, headers=dict(NO_STORE_HEADERS))


@app.get("/")
async def root():
    return _no_store_html(str(ROOT / "index.html"))


@app.get("/index.html")
async def index():
    return _no_store_html(str(ROOT / "index.html"))


#: Model directories the captions pipeline needs before it can transcribe. Their
#: absence is the difference between "installed" and "actually usable", and it
#: is invisible until someone uploads a video and waits.
CAPTION_MODELS = (
    "models--Systran--faster-whisper-small",
    "models--distilroberta-base",
    "models--pyannote--speaker-diarization-community-1",
    "models--pyannote--wespeaker-voxceleb-resnet34-LM",
)


def _hf_hub_cache() -> pathlib.Path:
    """Where the models live, without importing huggingface_hub at startup."""
    home = os.environ.get("HF_HOME")
    if home:
        return pathlib.Path(home) / "hub"
    return pathlib.Path.home() / ".cache" / "huggingface" / "hub"


def _model_dir_size(p: pathlib.Path) -> int:
    """Size of a cached model, counted once.

    Hugging Face caches come in two layouts. The usual one holds blobs/ (the
    real files) plus snapshots/ pointing at them -- symlinks on Linux, copies on
    Windows -- so walking the whole directory counts the payload twice and
    reports ~1.4 GB for what is really ~850 MB. But some entries (distilroberta
    here) keep their files directly in snapshots/ with an empty blobs/, so
    falling back to blobs unconditionally reported them as 0 MB.

    Prefer blobs when they hold anything, otherwise measure the directory.
    """
    blobs = p / "blobs"
    root = blobs if blobs.is_dir() and any(blobs.iterdir()) else p
    try:
        return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    except OSError:
        return 0


@app.get("/api/health")
async def health():
    """Is this install actually able to transcribe and export?

    Deliberately reports the things that fail *late* and quietly: a missing
    ffmpeg only surfaces when someone tries to export, and missing models only
    surface after an upload has already been accepted. Used by the Docker
    healthcheck and by tools/check_setup.py.
    """
    from app.pipeline.caption_export import ffmpeg_available

    cache = _hf_hub_cache()
    models = {}
    for name in CAPTION_MODELS:
        p = cache / name
        models[name] = {
            "present": p.is_dir(),
            "sizeMb": round(_model_dir_size(p) / 1e6, 1) if p.is_dir() else 0,
        }
    missing_models = [n for n, v in models.items() if not v["present"]]

    try:
        free = shutil.disk_usage(ROOT).free
    except OSError:
        free = -1

    hf_token = bool(os.environ.get("HF_TOKEN"))
    ffmpeg = ffmpeg_available()

    # A token is only needed to *download* the gated diarization model. Once it
    # is in the local cache, pyannote loads it without authenticating -- which is
    # why the caption jobs on this machine ran with no token in the environment.
    # So demanding a token unconditionally would report a fully working install
    # as not ready. The token only becomes required once something is missing.
    if missing_models:
        ready = ffmpeg and hf_token
        note = ("models are missing from the cache, so an HF_TOKEN is required "
                "to download them"
                if not hf_token else
                "models are missing from the cache and will download on first use")
    else:
        ready = ffmpeg
        note = None

    return {
        "ready": ready,
        "note": note,
        "ffmpeg": ffmpeg,
        "hfToken": hf_token,
        "models": models,
        "missingModels": missing_models,
        "modelCacheMb": round(sum(v["sizeMb"] for v in models.values()), 1),
        "diskFreeGb": round(free / 1e9, 1) if free >= 0 else None,
    }


@app.get("/config.html")
async def cfg():
    return _no_store_html(str(ROOT / "config.html"))


@app.get("/dialogue-player.html")
async def player():
    return _no_store_html(str(ROOT / "dialogue-player.html"))


@app.get("/animation-settings.html")
async def anim_settings():
    return _no_store_html(str(ROOT / "animation-settings.html"))


#: A frozen copy of the player from commit 968a03c, for side-by-side comparison
#: while diagnosing characters vanishing during playback.
#:
#: It must be served from a root-level path, not from one of the StaticFiles
#: mounts. The player fetches its dialogue with a *relative* URL
#: (fetch(`jobs/${job}/dialogue.json`)), which resolves against the document's
#: directory: served from /dialogue-player-legacy that becomes /jobs/... and works,
#: but served from /tools/... it would become /tools/jobs/... and 404.
LEGACY_PLAYER = ROOT / "tools" / "legacy" / "dialogue-player-968a03c.html"


@app.get("/dialogue-player-legacy")
async def player_legacy():
    if not LEGACY_PLAYER.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Legacy player build not found at tools/legacy/. Restore it with: "
                "git show 968a03c:dialogue-player.html > tools/legacy/dialogue-player-968a03c.html"
            ),
        )
    return _no_store_html(str(LEGACY_PLAYER))


@app.get("/dialogue-player-legacy.html")
async def player_legacy_html():
    return await player_legacy()


#: Frozen player builds for bisecting a regression, keyed by commit.
#:
#: Characters vanishing during playback turned out to be reproducible in the
#: current player but not in 968a03c, so the culprit is one of the commits
#: between them. Each is served as-is with only an identity banner added, so a
#: behavioural difference between two of them is a real difference.
#:
#: The revision is matched against this dict and the filename is *built from the
#: matched key* -- never from the request. Interpolating a path parameter into a
#: filename would let "../" walk out of tools/legacy, so the allowlist is the
#: security boundary here, not a convenience.
BISECT_PLAYERS: dict[str, str] = {
    "968a03c": "dialogue-player-968a03c.html",
    "3025dae": "dialogue-player-3025dae.html",
    "031b7c7": "dialogue-player-031b7c7.html",
    "e52966b": "dialogue-player-e52966b.html",
}

BISECT_LEGACY_DIR = ROOT / "tools" / "legacy"


def _serve_frozen_player(filename: str, rev: str) -> FileResponse:
    """Serve a frozen player build by a hardcoded filename.

    Deliberately a *flat* URL with no path segment. The player loads its modules
    and its GLB avatars with document-relative URLs, and GLTFLoader resolves
    those independently of <base href>, so serving it from /dialogue-player-at/3025dae
    silently 404'd three.module.js, talkinghead.mjs and all four avatar GLBs --
    and the avatars fell back to placeholders without any visible error. A flat
    path such as /player-3025dae reproduces the URL shape of the known-good
    /dialogue-player-legacy route exactly.
    """
    path = BISECT_LEGACY_DIR / filename
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"{filename} is missing. Restore it with: "
                f"git show {rev}:dialogue-player.html > tools/legacy/{filename}"
            ),
        )
    return _no_store_html(str(path))


@app.get("/player-968a03c", include_in_schema=False)
async def player_968a03c():
    return _serve_frozen_player("dialogue-player-968a03c.html", "968a03c")


@app.get("/player-3025dae", include_in_schema=False)
async def player_3025dae():
    return _serve_frozen_player("dialogue-player-3025dae.html", "3025dae")


@app.get("/player-031b7c7", include_in_schema=False)
async def player_031b7c7():
    return _serve_frozen_player("dialogue-player-031b7c7.html", "031b7c7")


@app.get("/player-e52966b", include_in_schema=False)
async def player_e52966b():
    return _serve_frozen_player("dialogue-player-e52966b.html", "e52966b")


# Static mounts. The directories are created first on purpose: jobs/, media/ and
# assets/ are all gitignored, so on a fresh clone they are absent and StaticFiles
# raises "Directory does not exist" *at import time* -- the server then refuses
# to start at all, which reads as a broken repository rather than missing data.
# Creating them is cheap and lets the app boot before any content is added.
for _mount in ("assets", "jobs", "app", "tools", "media", "jobs_captions"):
    (ROOT / _mount).mkdir(parents=True, exist_ok=True)

app.mount("/jobs", StaticFiles(directory=str(ROOT / "jobs")), name="jobs")
app.mount("/app", StaticFiles(directory=str(ROOT / "app")), name="app")
app.mount("/tools", StaticFiles(directory=str(ROOT / "tools")), name="tools")
app.mount("/media", StaticFiles(directory=str(ROOT / "media")), name="media")


class NoStoreScripts(StaticFiles):
    """Serve JS/CSS uncached, and leave heavy media cacheable.

    ES modules such as assets/talkinghead.mjs are cached harder than HTML and a
    stale copy changes behaviour silently -- gaze, lip-sync and visibility all
    live in that one file. Only script/style extensions are affected, so FBX, GLB
    and audio under /assets still stream from cache.
    """

    _UNCACHEABLE = (".js", ".mjs", ".css", ".map")

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        path = str(args[0]) if args else str(kwargs.get("path", ""))
        if path.lower().endswith(self._UNCACHEABLE):
            response.headers.update(NO_STORE_HEADERS)
        return response


# Only one /assets mount: Starlette matches mounts in registration order, so a
# plain mount listed first would shadow the subclass entirely and the headers
# would never be applied.
app.mount("/assets", NoStoreScripts(directory=str(ROOT / "assets")), name="assets")

# Captions feature (video -> burned-in captions). Self-contained router; it reads
# and writes only jobs/cap_{id}/ and never touches the 3D feature's jobs.
app.include_router(captions_router)