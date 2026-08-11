"""M1: audio -> diarized transcript.json with word-level timestamps (spec 3.1).

Runs WhisperX (faster-whisper + pyannote diarization) end to end.
Requires HF_TOKEN in .env and accepted pyannote licenses.

Usage:
    python -m app.pipeline.transcribe media/golden_clip.wav -o jobs/default/transcript.json
"""

import argparse
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
    language: str | None = None,
    diarize_model: str = "pyannote/speaker-diarization-community-1",
) -> dict:
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

    diarize = DiarizationPipeline(model_name=diarize_model, token=hf_token, device=device)
    diarize_segments = diarize(
        audio, min_speakers=min_speakers, max_speakers=max_speakers
    )
    result = whisperx.assign_word_speakers(diarize_segments, result)

    segments = []
    for seg in result["segments"]:
        words = [
            w
            for w in seg.get("words", [])
            if w.get("speaker")
        ]
        if not words:
            continue
        text = " ".join(w["word"].strip() for w in words).strip()
        segments.append(
            {
                "speaker": words[0]["speaker"],
                "start": round(float(words[0]["start"]), 3),
                "end": round(float(words[-1]["end"]), 3),
                "text": text,
                "words": [
                    {"word": w["word"], "start": round(float(w["start"]), 3), "end": round(float(w["end"]), 3)}
                    for w in words
                ],
            }
        )

    transcript = {"segments": segments}
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[transcribe] wrote {out}")

    speakers = sorted({s["speaker"] for s in segments})
    print(f"[transcribe] done: {len(segments)} segments, {len(speakers)} speaker(s) {speakers}")
    return transcript


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe + diarize audio to transcript.json")
    parser.add_argument("audio", help="input audio file (wav/mp3)")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output transcript.json path")
    parser.add_argument("--model", default="small", help="whisper model size (tiny/base/small/medium/large-v3)")
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
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
            language=args.language,
            diarize_model=args.diarize_model,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI entrypoint, surface everything
        print(f"[transcribe] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
