"""M1: audio -> diarized transcript.json with word-level timestamps (spec 3.1).

Runs WhisperX (faster-whisper + pyannote diarization) end to end.
Requires HF_TOKEN in .env and accepted pyannote licenses.

Usage:
    python -m app.pipeline.transcribe media/golden_clip.wav -o jobs/default/transcript.json
"""

import argparse
import copy
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

TOOLS_FFMPEG = ROOT / "tools" / "ffmpeg"
DEFAULT_OUT = ROOT / "jobs" / "default" / "transcript.json"


def ensure_ffmpeg_on_path() -> None:
    os.environ["PATH"] = str(TOOLS_FFMPEG) + os.pathsep + os.environ.get("PATH", "")


def transcribe(
    audio_path: str,
    output_path: str | None = None,
    model_size: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    require_speakers: int | None = None,
    language: str | None = None,
    diarize_model: str = "pyannote/speaker-diarization-community-1",
) -> dict:
    import pandas as pd
    import whisperx

    ensure_ffmpeg_on_path()
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        raise RuntimeError("HF_TOKEN not set — add it to .env")

    print(f"[transcribe] loading audio: {audio_path}")
    audio = whisperx.load_audio(audio_path)

    print(f"[transcribe] loading WhisperX model ({model_size}, {device}, {compute_type})")
    model = whisperx.load_model(model_size, device=device, compute_type=compute_type)
    result = model.transcribe(audio, language=language)
    lang = result.get("language", "en")
    print(f"[transcribe] detected language: {lang}")

    print("[transcribe] aligning word timestamps")
    model_a, metadata = whisperx.load_align_model(language_code=lang, device=device)
    result = whisperx.align(
        result["segments"], model_a, metadata, audio, device, return_char_alignments=False
    )

    print(f"[transcribe] diarizing speakers (pyannote, {diarize_model})")
    from whisperx.diarize import DiarizationPipeline

    aligned = copy.deepcopy(result)
    diarization_report = {
        "requested_speakers": require_speakers,
        "detected_speakers": None,
        "matched": None,
        "attempts": [],
    }

    def build_segments(diarize_segments: pd.DataFrame) -> list[dict]:
        """Assign diarization labels to the aligned words, then flatten to lines.

        One speaker per line is taken from the FIRST word that carries a label,
        so a line never mixes two voices under one name.
        """
        labelled = whisperx.assign_word_speakers(diarize_segments, copy.deepcopy(aligned))
        built = []
        for seg in labelled["segments"]:
            words = [w for w in seg.get("words", []) if w.get("speaker")]
            if not words:
                continue
            built.append(
                {
                    "speaker": words[0]["speaker"],
                    "start": round(float(words[0]["start"]), 3),
                    "end": round(float(words[-1]["end"]), 3),
                    "text": " ".join(w["word"].strip() for w in words).strip(),
                    "words": [
                        {"word": w["word"], "start": round(float(w["start"]), 3),
                         "end": round(float(w["end"]), 3)}
                        for w in words
                    ],
                }
            )
        return built

    def attempt(model: str, lo: int | None, hi: int | None) -> list[dict]:
        pipe = DiarizationPipeline(model_name=model, token=hf_token, device=device)
        kwargs = {}
        if lo:
            kwargs["min_speakers"] = lo
        if hi:
            kwargs["max_speakers"] = hi
        return build_segments(pipe(audio, **kwargs))

    # pyannote treats min/max as a request, not a guarantee, and clustering is
    # not deterministic enough to trust a single call: the same file with
    # min=max=4 returned 4 speakers on three runs and 2 on a fourth, which is
    # how job b171c19f shipped with 2 voices while meta.json promised 4. So try
    # a few configurations and keep the closest to what was actually asked for.
    if require_speakers:
        plan = [
            (diarize_model, require_speakers, require_speakers),
            (diarize_model, None, None),
            ("pyannote/speaker-diarization-3.1", require_speakers, require_speakers),
            ("pyannote/speaker-diarization-3.1", None, None),
        ]
    else:
        plan = [(diarize_model, min_speakers, max_speakers)]

    best_segments: list[dict] | None = None
    best_delta = None
    for model, lo, hi in plan:
        label = f"{model.rsplit('/', 1)[-1]}[{lo or 'auto'}..{hi or 'auto'}]"
        try:
            got = attempt(model, lo, hi)
        except Exception as exc:  # a single bad config must not kill the job
            print(f"[transcribe]   {label} failed: {exc}", flush=True)
            diarization_report["attempts"].append({"config": label, "error": str(exc)[:200]})
            continue
        count = len({s["speaker"] for s in got})
        delta = abs(count - require_speakers) if require_speakers else 0
        diarization_report["attempts"].append({"config": label, "detected": count})
        print(f"[transcribe]   {label} -> {count} speaker(s)", flush=True)
        if best_delta is None or delta < best_delta:
            best_segments, best_delta = got, delta
        if require_speakers and count == require_speakers:
            print(f"[transcribe]   exact match on {label}", flush=True)
            break

    if best_segments is None:
        raise RuntimeError("speaker diarization failed for every configuration tried")
    segments = best_segments

    transcript = {"segments": segments}
    detected = sorted({s["speaker"] for s in segments})
    diarization_report["detected_speakers"] = len(detected)
    if require_speakers:
        diarization_report["matched"] = len(detected) == require_speakers
        transcript["diarization"] = diarization_report
        if not diarization_report["matched"]:
            print(
                f"[transcribe] WARNING: asked for {require_speakers} speakers, "
                f"best effort found {len(detected)} {detected}. Reporting this "
                f"rather than inventing the difference.",
                flush=True,
            )
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[transcribe] wrote {out}")

    print(f"[transcribe] done: {len(segments)} segments, {len(detected)} speaker(s) {detected}")
    return transcript


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe + diarize audio to transcript.json")
    parser.add_argument("audio", help="input audio file (wav/mp3)")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output transcript.json path")
    parser.add_argument("--model", default="small", help="whisper model size (tiny/base/small/medium/large-v3)")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    parser.add_argument("--require-speakers", type=int, default=None,
                        help="try several diarization configs until this many voices are "
                             "found; records the achieved count either way")
    parser.add_argument("--language", default=None, help="force language code, e.g. en")
    parser.add_argument("--diarize-model", default="pyannote/speaker-diarization-community-1",
                        help="pyannote diarization model (must be license-accepted)")
    args = parser.parse_args(argv)

    try:
        transcribe(
            args.audio,
            output_path=args.output,
            model_size=args.model,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
            require_speakers=args.require_speakers,
            language=args.language,
            diarize_model=args.diarize_model,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI entrypoint, surface everything
        print(f"[transcribe] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
