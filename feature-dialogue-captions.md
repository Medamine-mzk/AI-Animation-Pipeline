# Feature: Real-Time Dialogue Captions with Word-Level Highlight

## 0. Summary

**Input:** a short video containing a dialogue between 2+ people.
**Output:** the same video, re-rendered with animated, student-friendly
captions burned into the frame — text appears in sync with speech, grouped
by speaker, with the **currently-spoken word visually distinct** from the
rest of the line (karaoke-style highlight), TikTok/Reels-caption style
rather than a plain subtitle bar.

## 1. User Flow

1. User uploads a short video (drag-and-drop or file picker).
2. System extracts audio, transcribes it, and detects who's speaking when.
3. A **preview player** shows the video with captions already overlaid,
   word-by-word, so the user can watch it play back before committing.
4. User can correct any misheard word or wrong speaker split directly in a
   transcript editor next to the preview (this matters more than it sounds —
   ASR always mishears the occasional name or technical term, and there's
   no recovery path if the only output is a locked video file).
5. User clicks **Export**; the system renders a final video file with the
   captions burned in, downloadable.

## 2. Functional Requirements

- Detect distinct speakers in the audio (diarization) — don't assume one
  speaker.
- Transcribe speech to text with **word-level timestamps**, not just
  sentence-level — the whole point of the highlight effect depends on
  knowing exactly when each word starts and ends.
- Multilingual: the platform teaches in French, so the ASR/diarization
  step needs to handle French (and ideally Arabic/English) reliably, not
  just English.
- Group words into short on-screen "pages" (2–8 words at a time,
  TikTok-caption style) rather than dumping a full sentence on screen at
  once — full-sentence subtitles are worse for students following along in
  real time than short bursts.
- Highlight the current word distinctly (color + subtle scale/pop) as it's
  spoken, and return it to normal style once the next word starts.
- Color-code captions by speaker (a consistent accent color + small name
  tag per detected speaker, assigned in order of first appearance).
- Let the user edit the transcript before final export.
- Export a real video file (mp4) with captions burned in — not just a
  live web overlay, since the point is a shareable/downloadable artifact.

## 3. Pipeline

```mermaid
flowchart LR
    U[Upload video] --> EX[Extract audio<br/>ffmpeg]
    EX --> ASR[Transcribe + diarize<br/>+ word timestamps<br/>WhisperX]
    ASR --> MAP[Map to Caption[]<br/>+ speakerId per token]
    MAP --> PAGE[Speaker-aware pagination<br/>createTikTokStyleCaptions]
    PAGE --> COLOR[Assign speaker colors]
    COLOR --> PREVIEW[Live preview + transcript editor<br/>Remotion Player in-app]
    PREVIEW -->|edits| PAGE
    PREVIEW -->|Export| RENDER[Burn captions into video<br/>Remotion render]
    RENDER --> OUT[Downloadable mp4]
```

### 3.1 Transcribe + Diarize

**WhisperX** (faster-whisper + pyannote.audio for diarization + forced
alignment, one library) — gives word-level timestamps and speaker labels
in a single pass, and handles French/multilingual well with the
`large-v3` model. This needs a real process with a loaded model, not a
stateless serverless function — same constraint you've already hit with
the local model in the hint pipeline: Vercel/Render serverless can't hold
a persistent GPU process, so this needs a background worker + job queue
(upload → enqueue → worker transcribes → webhook/poll for completion),
not a request/response API route.

If self-hosting WhisperX turns out to be more infra than it's worth for a
first version, a hosted ASR+diarization API (Deepgram, AssemblyAI) is a
reasonable build-vs-buy fallback that sidesteps the worker/GPU problem
entirely — worth deciding early since it changes the architecture, not
just a config value.

### 3.2 Map to Caption Data

Convert WhisperX's per-word output into `@remotion/captions`' native
`Caption` shape, with one addition — a `speakerId` per token:

```ts
interface SpeakerCaption {
  text: string;       // includes leading space, per @remotion/captions convention
  startMs: number;
  endMs: number;
  timestampMs: number;
  confidence: number | null;
  speakerId: string;  // "SPEAKER_00"
}
```

### 3.3 Speaker-Aware Pagination

`@remotion/captions` ships `createTikTokStyleCaptions()` for exactly this
job — it groups tokens into "pages" (a few words shown together) based on
a `combineTokensWithinMilliseconds` threshold. Run it **per diarized turn**
(split the caption array on speaker change first, then paginate each
speaker's run separately) so a page never mixes two speakers' words, and
tag each resulting page with its `speakerId`.

### 3.4 Preview + Edit

Embed a **Remotion `<Player>`** in the Next.js app, rendering the same
React caption component that'll be used for the final export, layered over
the uploaded video. Next to it, a simple editable transcript (word list
grouped by page/speaker) — editing a word's text updates the same data the
player reads, so the preview reflects corrections immediately. This reuses
one React component for both preview and final render, so there's no
"looks different after export" surprise.

### 3.5 Export

Render via Remotion (CLI locally to start; Remotion Lambda later if export
volume grows) to produce the final mp4 with captions burned into the
frame. Same worker/queue reasoning as §3.1 applies — rendering isn't
instant, so this is a background job with a completion notification, not
a synchronous request.

## 4. UI / Animation Design — "student-friendly"

- **Typography:** bold, rounded sans-serif (Inter, Poppins, or Nunito) —
  legible at small sizes, not a serif/condensed font that's harder to
  parse quickly.
- **Layout:** lower-third safe zone, max 2 lines per page, generous
  line-height; a soft dark translucent backdrop bar behind the text block
  (not just a text shadow) so it stays readable over any footage.
- **Current-word highlight:** the active word gets a filled color pill
  (the speaker's accent color) behind it plus a small scale-up (~1.1–1.15x)
  with a spring easing — not a hard jump. Once the next word starts, it
  settles back to plain white/light text, no lingering glow.
- **Per-speaker color coding:** assign each detected speaker a distinct
  accent color from a fixed, WCAG-contrast-checked palette, in order of
  first appearance (matches the entrance-order convention already used
  elsewhere in your pipeline work). A small name/role tag (e.g. "Speaker
  1" by default, editable) appears briefly when the speaker changes.
- **Page transitions:** new caption pages fade + rise in slightly (~150ms),
  rather than hard-cutting — words within a page pop in individually as
  their timestamp arrives, not all at once, so the reveal itself carries
  some of the "watch and follow along" engagement.
- **Accessibility touches worth including from the start:** adjustable
  caption size, a high-contrast/reduced-motion toggle (some students will
  want the highlight without the scale animation), and confirm color
  choices work for common color-vision deficiencies (don't rely on red/
  green alone to distinguish speakers).

## 5. Tech Stack

- **Frontend/preview:** Next.js (existing app) + Remotion `<Player>` for
  live in-browser preview of the caption component over the uploaded video.
- **Caption data + pagination:** `@remotion/captions` —
  `npx remotion add @remotion/captions`, gives you `createTikTokStyleCaptions()`
  and the `Caption`/`TikTokPage` types for free instead of hand-rolling
  pagination logic.
- **Transcription + diarization:** WhisperX (self-hosted worker) or a
  hosted ASR+diarization API (Deepgram/AssemblyAI) — decide per §3.1.
- **Rendering/export:** Remotion CLI render (or Remotion Lambda once
  volume justifies it) — same React component used for preview, so no
  separate "export renderer" to keep in sync.
- **Worth adding to your Claude Code setup:** the community
  `remotion-best-practices` skill (covers exactly this "captioned
  talking-head, TikTok-style word reveal" pattern with current
  `@remotion/captions` API usage) — will save a lot of back-and-forth
  versus having Claude Code guess at Remotion's caption API from general
  training knowledge, which tends to drift from the actual current SDK.

## 6. Constraints / Assumptions

- "Short video" — assume a hard cap (e.g. 2–3 minutes, configurable) for
  a first version, both to keep transcription/render time reasonable and
  to avoid surprise processing costs.
- Supported input formats: mp4/mov/webm to start.
- 2–4 speakers is the realistic target for the highlight/color-coding UX
  to stay legible; beyond that, per-speaker colors stop being visually
  distinct — flag as a soft limit, not a hard block.

## 7. Edge Cases

- **Overlapping speech:** diarization will sometimes assign the same
  audio window to two speakers, or miscount a fast interruption. Fine to
  degrade gracefully (pick the higher-confidence speaker) rather than
  block export on it, but surface a warning in the editor so the user
  knows to double check.
- **Silence / non-speech audio:** no caption page during silent stretches;
  don't force an empty page to appear.
- **Low-confidence words:** `Caption.confidence` from WhisperX can flag
  words worth visually marking as uncertain in the editor (e.g. underline)
  so users know where to double-check rather than trusting every word
  equally.
- **Single speaker:** should still work — diarization with 1 detected
  speaker is a normal case, not an error.

## 8. Milestones

- **M0 — Pipeline stub.** Fixture caption data (hand-written JSON)
  rendered through the Remotion preview component over a sample video —
  proves the render/animation side works before any ASR is wired in.
- **M1 — Real transcription.** WhisperX (or hosted API) worker wired up,
  producing real `Caption[]` + speaker labels from an uploaded video.
- **M2 — Pagination + color.** Speaker-aware `createTikTokStyleCaptions`
  grouping, palette assignment, name tags.
- **M3 — Editable preview.** Transcript editor next to the live player;
  edits flow back into the same data the player renders.
- **M4 — Export.** Background render job producing the final downloadable
  mp4, with a completion notification (matches the async-job pattern
  you'll likely reuse from the hint pipeline's own infra decisions).
- **M5 — Polish.** Animation tuning, accessibility toggles, and a
  multilingual pass (French/Arabic/English) to confirm WhisperX/the
  chosen ASR handles all three acceptably before calling this done.

## 9. Open Questions

- Self-host WhisperX vs. hosted ASR API — mostly a cost/infra-ownership
  call, worth deciding before M1 since it shapes the worker architecture.
- Does the export job share infrastructure with the existing hint-pipeline
  worker (same queue/host), or does it need its own — both are "can't run
  on serverless" problems, so there may be a shared answer worth solving
  once rather than twice.
- Caption edits made after a partial preview watch — do they require
  re-running pagination (if a word's text length changes on-screen
  wrapping) or just a text swap? Worth confirming before building the
  editor UI so it doesn't silently desync from the final render.
