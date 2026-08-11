# AI Animation Pipeline — Project Specification

## 0. What this document is

A build spec for an automated pipeline that takes an unstructured audio recording
(e.g. two people talking) and produces a lip-synced 2D cartoon video: transcription →
scene/character understanding → asset resolution → lip-sync animation → composited MP4.

This spec is intentionally **phased**. Do not attempt to build the full web app in one
pass. Each milestone below should be working end-to-end (even if ugly) before the next
one starts. A pipeline that runs from the CLI and produces a correct video is more
valuable at every stage than a polished UI wrapped around a broken pipeline.

---

## 1. Core pipeline (five stages)

1. **Transcription & Diarization** — audio → text with word-level timestamps and
   speaker labels.
2. **Story Structuring** — transcript → structured scene/character/emotion JSON via LLM.
3. **Asset Resolution** — resolve/generate the background and character sprites needed
   for the scene.
4. **Lip-Sync & Animation** — map audio to mouth shapes (visemes) per character, plus
   idle motion (blinking, breathing) so characters don't look frozen when not talking.
5. **Compositing & Render** — layer background + characters + mouth states + idle
   motion + audio into a final MP4.

---

## 2. Tech stack decisions

| Layer | Choice | Notes |
|---|---|---|
| Transcription + diarization | **WhisperX** (`m-bain/whisperX`) | Word-level timestamps + pyannote diarization. Requires a Hugging Face token and accepting the pyannote model license (gated repo) — do this manually before coding starts. |
| Story structuring LLM | Claude or GPT-4o via API, **with enforced structured output** | Use native tool-calling / JSON schema mode, not "please respond in JSON." A malformed response should be a typed validation error, not a `JSON.parse` crash three stages downstream. |
| Lip-sync (primary, v1) | **Rhubarb Lip Sync** (`DanielSWolf/rhubarb-lip-sync`) | Ships as a native binary (Windows/Mac/Linux) — install/vendor it explicitly, don't assume pip can get it. Outputs the standard 9-shape viseme set (A–H, X). |
| Lip-sync (optional, v2) | SadTalker / LivePortrait | Only relevant if you later want a semi-realistic single-portrait style. Do **not** use these for flat 2D cartoon sprites — they animate photoreal/semi-real faces and tend to warp vector art. Treat as a separate art-style track, not a fallback for Rhubarb. |
| Backgrounds | SDXL / Flux (local or API) for v1 backgrounds; curated background library as fallback | Backgrounds don't need cross-frame identity consistency, so generation is safe here. |
| Character sprites | **Curated sprite library, not generative**, for v1 | Diffusion models can't reliably hold a character's identity across frames/scenes without extra machinery (LoRA, ControlNet reference, IP-Adapter). That's a real research problem, not a weekend feature — punt it to a clearly marked v2/stretch section so it doesn't stall v1. |
| Compositing | MoviePy (or direct FFmpeg calls if MoviePy's overhead becomes a bottleneck) | |
| Backend | FastAPI | |
| Job queue | Celery + Redis | Needed once you go async/web. Not needed for the CLI-first milestones. |
| Frontend | Next.js + Tailwind | Built last, after the pipeline is proven from the CLI. |
| Containerization | Docker | Rhubarb binary + FFmpeg + WhisperX/torch/CUDA + Python deps is a lot of native surface area — pin it in a Dockerfile from milestone 0, don't leave it to "works on my machine." |

---

## 3. Data contracts

### 3.1 Diarized transcript (WhisperX output, stage 1 → stage 2 input)

```json
{
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "start": 0.42,
      "end": 3.10,
      "text": "I really think we should leave early tomorrow.",
      "words": [
        {"word": "I", "start": 0.42, "end": 0.55},
        {"word": "really", "start": 0.55, "end": 0.81}
      ]
    }
  ]
}
```

### 3.2 Structured scene script (stage 2 output — enforce with Pydantic)

```json
{
  "setting": "living_room",
  "characters": [
    {"id": "dad", "speaker_ref": "SPEAKER_00", "sprite": "dad_v1", "role": "father"},
    {"id": "daughter", "speaker_ref": "SPEAKER_01", "sprite": "daughter_v1", "role": "daughter"}
  ],
  "lines": [
    {
      "character_id": "dad",
      "start": 0.42,
      "end": 3.10,
      "text": "I really think we should leave early tomorrow.",
      "emotion": "neutral",
      "audio_segment_ref": "seg_0"
    }
  ],
  "camera": [
    {"start": 0.0, "end": 3.10, "focus_character": "dad", "shot": "medium"}
  ]
}
```

Pydantic model this exactly and validate the LLM's output against it before any
downstream stage touches it. Reject and retry (with the validation error fed back to
the model) rather than trying to patch malformed JSON in code.

### 3.3 Character sprite manifest

Each character needs, at minimum:
- 9 viseme mouth images matching Rhubarb's shape set (A–H, X)
- 2 eye states (open, closed) for a blink cycle
- 1–2 idle body poses (for breathing sway — even a 2px vertical oscillation between
  two poses reads as "alive")

```json
{
  "id": "dad_v1",
  "base_body": "dad_v1_body.png",
  "visemes": {"A": "dad_v1_mouth_A.png", "B": "dad_v1_mouth_B.png", "...": "..."},
  "eyes": {"open": "dad_v1_eyes_open.png", "closed": "dad_v1_eyes_closed.png"},
  "anchor_point": {"x": 512, "y": 300}
}
```

---

## 4. Animation details that are easy to skip and shouldn't be

- **Idle motion is a v1 requirement, not a nice-to-have.** A character with a
  perfectly still body and only the mouth changing looks like a broken GIF, not a
  cartoon. Minimum bar: blink every 3–6s (randomized), subtle breathing sway.
- **Non-speaking characters still need idle motion running** while another character
  talks — don't freeze the whole scene to whoever has the mic.
- **Camera direction, even primitive**, matters more than extra render fidelity for
  perceived quality. A simple cut/zoom to the active speaker (driven by the `camera`
  field in the scene JSON) goes a long way. Keep the v1 camera logic dead simple: cut
  to whoever's speaking, medium shot, no smooth pans yet.

---

## 5. Backend architecture (once you're past CLI milestones)

- **Job model**: `job_id`, `status` (`queued`/`transcribing`/`structuring`/`rendering`/`done`/`failed`), `input_audio_path`, `output_video_path`, `error` (nullable), timestamps.
- **Storage layout** (local disk for v1, swap for S3-compatible storage later without changing the interface):
  ```
  /jobs/{job_id}/
    input.wav
    transcript.json
    script.json
    frames/
    output.mp4
  ```
- **API surface (v1)**:
  - `POST /jobs` — upload audio, returns `job_id`
  - `GET /jobs/{job_id}` — status + progress stage
  - `GET /jobs/{job_id}/result` — signed URL / path to finished MP4
- Celery task per pipeline stage, chained, so a failure in stage 3 doesn't require
  re-running stage 1. Persist each stage's output to disk so retries are cheap.

---

## 6. Phased build plan (follow in order)

**M0 — Environment**
Docker image with WhisperX + torch + Rhubarb binary + FFmpeg + Python deps all
verified working. HF token configured, pyannote license accepted. Acceptance: a
throwaway script transcribes a test WAV and prints diarized segments.

**M1 — Transcription stage**
CLI script: audio in → `transcript.json` out, matching the schema in §3.1. Test on a
real two-speaker clip (~30–60s) — this becomes your golden test file for every
subsequent milestone.

**M2 — Story structuring stage**
`transcript.json` → `script.json` via LLM, validated against the Pydantic schema in
§3.2. Build in retry-on-validation-failure. Test that speaker labels map to sensible
character roles on the golden clip.

**M3 — Static sprite library + asset resolution**
Hand-author (or source) one character sprite set matching §3.3, plus one background.
Asset resolution stage just needs to pick from this fixed library for v1 — no
generation yet. Acceptance: given `script.json`, correctly resolves which sprite/background files to use.

**M4 — Lip-sync + idle motion**
Run Rhubarb on each character's audio segments, get viseme timing. Render a frame
sequence combining background + character body + correct mouth shape per timestamp +
blink/sway idle motion for non-speaking characters. Acceptance: visually inspect a
short rendered clip — mouth shapes should roughly match phonemes, idle characters
should not look frozen.

**M5 — Compositing + render**
MoviePy/FFmpeg stitches frames + original audio into `output.mp4`. Acceptance: golden
clip produces a correct final video end-to-end from a single CLI command.

**M6 — FastAPI + job queue**
Wrap the now-proven CLI pipeline in FastAPI endpoints + Celery/Redis job chain per §5.
Acceptance: `POST /jobs` with the golden clip produces the same output as the CLI run.

**M7 — Frontend**
Next.js upload UI + polling + video player. Only start this once M6 is solid — a nice
frontend on top of a shaky backend just makes debugging harder.

**M8 — Stretch goals (optional, separate tracks)**
- Generative background per-scene (SDXL/Flux) instead of fixed library
- Smoother camera work (pans, zoom easing)
- SadTalker/LivePortrait as an alternate semi-realistic art-style pipeline
- Generative character creation (flag: this needs identity-consistency tooling —
  LoRA/ControlNet/IP-Adapter — treat as its own project, not a quick add-on)

---

## 7. Known hard problems (be honest with yourself about these upfront)

- **Character identity consistency in generated art** is unsolved in general — this
  is why v1 uses a fixed sprite library.
- **Diarization accuracy degrades** with overlapping speech, similar-sounding voices,
  or short audio clips. Don't assume the pipeline works on adversarial audio without
  testing.
- **Pyannote/HF gating**: the diarization model requires manually accepting terms on
  Hugging Face before the API token will work — this trips people up and looks like a
  bug when it's actually a license wall.

---

## 8. Suggested repo structure

```
/app
  /pipeline
    transcribe.py         # M1
    structure_script.py   # M2
    resolve_assets.py     # M3
    animate.py            # M4
    render.py             # M5
  /schemas
    transcript.py          # Pydantic models, §3.1
    script.py               # Pydantic models, §3.2
    sprite_manifest.py      # Pydantic models, §3.3
  /assets
    /sprites
    /backgrounds
  /api
    main.py                # FastAPI app, M6
    tasks.py                # Celery tasks
  /tests
    golden_clip.wav
    test_pipeline_e2e.py
/frontend                   # M7, Next.js
Dockerfile
docker-compose.yml           # api + redis + celery worker
```
