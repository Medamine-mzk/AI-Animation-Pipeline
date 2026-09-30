"""M0: real word-level timeline for the captions feature (spec 3.2).

WHY THIS EXISTS
---------------
`feature-dialogue-captions.md` needs to know exactly when each word starts and
ends, in order to highlight the current word. This project already contains
word timings, but they are *fabricated*:

* `app/pipeline/transcribe.py:88` writes real WhisperX-aligned words to
  `transcript.json`, but discards the per-word `speaker` and `score` fields.
* `app/pipeline/structure_script.py:47-54` forwards only segment-level text to
  the LLM, so `words` never reach the dialogue stage.
* `tools/picker_to_dialogue.py:74-81` therefore regenerates `wtimes` /
  `wdurations` by dividing each segment's duration by its word count.

A karaoke-style highlight built on those would drift audibly against the
recording. So this module rebuilds the timeline at full fidelity.

WHAT IT DOES NOT TOUCH
----------------------
The existing 3D animation feature reads `transcript.json` and `dialogue.json`.
This module writes a *separate* file, `words_timeline.json`, that nothing in the
3D path opens. `app/pipeline/transcribe.py` is imported for its audio-loading and
model helpers but its output shape is never modified, so the 3D feature cannot
regress.

Usage:
    python -m app.pipeline.word_timeline media/clip.wav -o jobs/cap_x/words_timeline.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.schemas.word_timeline import (
    OverlapWarning,
    TimelineSpeaker,
    TimelineWord,
    WordTimeline,
)

ROOT = Path(__file__).resolve().parents[2]

#: Boundary slop, in ms, that is normal in forced alignment and should not be
#: reported to the user as two people talking at once.
DEFAULT_OVERLAP_TOLERANCE_MS = 20

#: A word whose confidence is below this is worth underlining in the transcript
#: editor so a human double-checks it (spec 7).
LOW_CONFIDENCE_THRESHOLD = 0.5


def seconds_to_ms(seconds: float) -> int:
    """Convert seconds to integer milliseconds, rounding and clamping at zero.

    Rounding rather than truncating matters: repeated truncation of per-word
    timings accumulates visible drift across a long caption page.
    """
    return max(0, int(round(float(seconds) * 1000)))


def renumber_by_first_appearance(raw_words: Sequence[dict]) -> dict[str, str]:
    """Map raw diarizer labels to ``SPEAKER_00..N`` in order of first speech.

    The captions feature wants stable colour assignment "in order of first
    appearance" (spec 4). This deliberately differs from the existing 3D
    pipeline, which orders by descending segment count
    (`app/api/main.py:410-411`) so that the busiest voice becomes the main
    character. Both orderings are valid; they answer different questions.
    """
    mapping: dict[str, str] = {}
    for w in raw_words:
        label = w.get("speaker")
        if not label:
            continue
        if label not in mapping:
            mapping[label] = f"SPEAKER_{len(mapping):02d}"
    return mapping


def detect_overlaps(
    words: Sequence[dict],
    tolerance_ms: int = DEFAULT_OVERLAP_TOLERANCE_MS,
) -> list[OverlapWarning]:
    """Find windows where two different speakers have words at the same time.

    Overlapping speech is a normal diarization outcome, not an error, so this
    returns warnings and never blocks export (spec 7). Only the most recent word
    per speaker is tracked, which is enough to catch a fast interruption without
    an O(n^2) scan over a long clip.
    """
    ordered = sorted(
        (w for w in words if w.get("start") is not None and w.get("end") is not None),
        key=lambda w: float(w["start"]),
    )
    last_by_speaker: dict[str, tuple[int, int]] = {}
    warnings: list[OverlapWarning] = []
    for w in ordered:
        start = seconds_to_ms(w["start"])
        end = seconds_to_ms(w["end"])
        speaker = w.get("speaker") or "SPEAKER_00"
        for other_speaker, (other_start, other_end) in last_by_speaker.items():
            if other_speaker == speaker:
                continue
            # Any real intersection beyond tolerance is a genuine overlap.
            lo, hi = max(start, other_start), min(end, other_end)
            if hi - lo > tolerance_ms:
                warnings.append(
                    OverlapWarning(
                        startMs=lo,
                        endMs=hi,
                        speakerIds=sorted({speaker, other_speaker}),
                    )
                )
        previous = last_by_speaker.get(speaker)
        if previous is None or end > previous[1]:
            last_by_speaker[speaker] = (start, end)
    return warnings


def _usable(raw: dict) -> bool:
    """A row is usable only if it has text and a numeric start and end."""
    if not isinstance(raw, dict):
        return False
    text = raw.get("word")
    if not isinstance(text, str) or not text.strip():
        return False
    start, end = raw.get("start"), raw.get("end")
    if start is None or end is None:
        return False
    try:
        float(start)
        float(end)
    except (TypeError, ValueError):
        return False
    return True


def build_word_timeline(
    raw_words: Iterable[dict],
    language: str = "",
    duration_s: float = 0.0,
    tolerance_ms: int = DEFAULT_OVERLAP_TOLERANCE_MS,
) -> WordTimeline:
    """Build a :class:`WordTimeline` from raw aligned words.

    Pure function: no I/O, no model, no network. Everything WhisperX-specific
    lives in :func:`transcribe_words`.

    Cleaning performed here, so downstream consumers never have to:

    * drops rows with no text or no timing
    * sorts by start time
    * clamps negative timestamps and ``end < start`` artefacts to zero-length
    * renumbers speaker labels in order of first appearance
    * attributes unlabelled words to the preceding labelled word
    * records which words were attributed rather than labelled
    """
    rows = [w for w in raw_words if _usable(w)]
    if not rows:
        raise ValueError(
            "no usable words with text and timing were provided - refusing to "
            "invent a timeline"
        )

    mapping = renumber_by_first_appearance(rows)
    if not mapping:
        # Every row lost its label. Attribute them all to one speaker so the
        # timeline stays usable, and let hasInheritedSpeakers say so.
        mapping = {"SPEAKER_00": "SPEAKER_00"}
    default_speaker = next(iter(mapping.values()))

    rows.sort(key=lambda w: float(w["start"]))

    words: list[TimelineWord] = []
    inherited_per_speaker: dict[str, int] = {sid: 0 for sid in set(mapping.values())}
    current_speaker: str | None = None
    has_confidence = False

    for index, row in enumerate(rows):
        label = row.get("speaker")
        if label and label in mapping:
            current_speaker = mapping[label]
        else:
            # Diarization dropped this word. Speech is continuous, so the
            # preceding speaker is the best available attribution -- but it is
            # a guess, so it is counted and reported.
            current_speaker = current_speaker or default_speaker
            inherited_per_speaker[current_speaker] += 1

        start_ms = seconds_to_ms(row["start"])
        end_ms = max(start_ms, seconds_to_ms(row["end"]))

        confidence = row.get("score")
        if confidence is None:
            confidence = row.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None
            if confidence is not None and not (0.0 <= confidence <= 1.0):
                confidence = None
            else:
                has_confidence = True

        text = " ".join(str(row["word"]).split())
        words.append(
            TimelineWord(
                # @remotion/captions convention: a leading space joins words
                # into a run without needing extra markup.
                text=text if index == 0 else f" {text}",
                startMs=start_ms,
                endMs=end_ms,
                timestampMs=start_ms,
                confidence=confidence,
                speakerId=current_speaker,
            )
        )

    # Recompute overlap windows on the canonical, ms-based word list.
    overlap_rows = [
        {
            "start": w.startMs / 1000.0,
            "end": w.endMs / 1000.0,
            "speaker": w.speakerId,
        }
        for w in words
    ]
    overlaps = detect_overlaps(overlap_rows, tolerance_ms=tolerance_ms)

    grouped: dict[str, list[TimelineWord]] = {}
    for w in words:
        grouped.setdefault(w.speakerId, []).append(w)

    # First-occurrence position per speaker. Used only to *order* the speakers;
    # the public `index` field is the resulting rank, so it is always 0, 1, 2...
    # and can be used directly to index a colour palette.
    first_seen: dict[str, int] = {}
    for position, w in enumerate(words):
        first_seen.setdefault(w.speakerId, position)
    ordered_ids = sorted(grouped, key=lambda sid: first_seen[sid])
    speakers = [
        TimelineSpeaker(
            speakerId=sid,
            index=rank,
            firstWordMs=grouped[sid][0].startMs,
            lastWordMs=grouped[sid][-1].endMs,
            wordCount=len(grouped[sid]),
            totalMs=sum(w.endMs - w.startMs for w in grouped[sid]),
            inheritedWordCount=inherited_per_speaker.get(sid, 0),
        )
        for rank, sid in enumerate(ordered_ids)
    ]

    return WordTimeline(
        language=language or "",
        durationMs=max(
            seconds_to_ms(duration_s),
            (words[-1].endMs if words else 0),
        ),
        words=words,
        speakers=speakers,
        overlaps=overlaps,
        wordAlignment="measured",
        hasInheritedSpeakers=any(s.inheritedWordCount > 0 for s in speakers),
        hasConfidence=has_confidence,
    )


def transcribe_words(
    audio_path: str,
    output_path: str | None = None,
    model_size: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    language: str | None = None,
    diarize_model: str = "pyannote/speaker-diarization-community-1",
) -> WordTimeline:
    """Run WhisperX and return a full-fidelity word timeline.

    Mirrors the orchestration in :func:`app.pipeline.transcribe.transcribe` but
    keeps the per-word ``speaker`` and ``score`` fields that it discards. Kept as
    a separate function on purpose: editing ``transcribe.py`` would put the
    existing 3D feature at risk for no benefit to it.
    """
    import whisperx
    from whisperx.diarize import DiarizationPipeline

    from app.pipeline.transcribe import ensure_ffmpeg_on_path

    ensure_ffmpeg_on_path()
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        raise RuntimeError(
            "HF_TOKEN not set - add it to .env (needed for pyannote diarization)"
        )

    print(f"[word_timeline] loading audio: {audio_path}")
    audio = whisperx.load_audio(audio_path)

    print(f"[word_timeline] loading WhisperX model ({model_size}, {device})")
    model = whisperx.load_model(model_size, device=device, compute_type=compute_type)
    result = model.transcribe(audio, language=language)
    detected_language = result.get("language", "") or ""

    print(f"[word_timeline] aligning word timestamps (lang={detected_language})")
    align_model, metadata = whisperx.load_align_model(
        language_code=detected_language or "en", device=device
    )
    result = whisperx.align(
        result["segments"],
        align_model,
        metadata,
        audio,
        device,
        return_char_alignments=False,
    )

    print(f"[word_timeline] diarizing speakers ({diarize_model})")
    diarize = DiarizationPipeline(model_name=diarize_model, token=hf_token, device=device)
    diarize_segments = diarize(
        audio, min_speakers=min_speakers, max_speakers=max_speakers
    )
    result = whisperx.assign_word_speakers(diarize_segments, result)

    raw: list[dict[str, Any]] = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            raw.append(
                {
                    "word": w.get("word"),
                    "start": w.get("start"),
                    "end": w.get("end"),
                    "speaker": w.get("speaker"),
                    "score": w.get("score"),
                }
            )

    duration_s = _probe_duration_seconds(audio_path)
    timeline = build_word_timeline(
        raw, language=detected_language, duration_s=duration_s
    )
    print(
        f"[word_timeline] {len(timeline.words)} words, "
        f"{len(timeline.speakers)} speakers, "
        f"{len(timeline.overlaps)} overlap warning(s), "
        f"confidence={'yes' if timeline.hasConfidence else 'no'}"
    )

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(timeline.model_dump_json(indent=2), encoding="utf-8")
        print(f"[word_timeline] wrote {out}")
    return timeline


def _probe_duration_seconds(path: str) -> float:
    """Best-effort media duration. Never raises -- duration is cosmetic here.

    Resolves the same bundled ffmpeg that ``app/pipeline/render.py`` uses, but
    computes the path locally rather than importing ``app.api.main``: a pipeline
    module must not depend on the API layer, and importing it would drag FastAPI
    and its static mounts into a CLI context.
    """
    ffmpeg = ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"
    if ffmpeg.exists():
        try:
            import subprocess

            proc = subprocess.run(
                [str(ffmpeg), "-i", path, "-f", "null", "-"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            # ffmpeg has no probe-only flag; Duration lives on stderr.
            for line in (proc.stderr or "").splitlines():
                if "Duration:" in line:
                    stamp = line.split("Duration:")[1].split(",")[0].strip()
                    hh, mm, ss = stamp.split(":")
                    return int(hh) * 3600 + int(mm) * 60 + float(ss)
        except Exception:  # noqa: BLE001
            pass

    try:
        import librosa

        return float(librosa.get_duration(path=path))
    except Exception:  # noqa: BLE001
        return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a real word-level timeline (captions feature, M0)"
    )
    parser.add_argument("audio", help="input audio file (wav/mp3)")
    parser.add_argument("-o", "--output", required=True, help="words_timeline.json path")
    parser.add_argument("--model", default="small")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    parser.add_argument("--language", default=None, help="force language code, e.g. en")
    args = parser.parse_args(argv)

    try:
        transcribe_words(
            args.audio,
            output_path=args.output,
            model_size=args.model,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
            language=args.language,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI entrypoint, surface everything
        print(f"[word_timeline] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
