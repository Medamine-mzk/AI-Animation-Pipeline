"""M2: transcript.json -> structured scene script.json via LLM (spec 3.2).

Validates the LLM output against the Pydantic SceneScript schema with
retry-on-validation-failure (validation errors fed back to the model).

Usage:
    python -m app.pipeline.structure_script jobs/golden/transcript.json -o jobs/golden/script.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from app.pipeline.llm import StructuredOutputError, generate_structured
from app.schemas.script import SceneScript

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "jobs" / "golden" / "script.json"

SYSTEM_PROMPT = """You are the story-structuring stage of an animation pipeline.
You turn a diarized conversation transcript into a structured scene script for a
2D cartoon.

Rules:
- NEVER alter dialogue text. Copy it verbatim from the transcript.
- Use the exact start/end timestamps from the transcript segments.
- One line per transcript segment; keep every segment.
- Map each distinct speaker (SPEAKER_XX) to a character with a short role name
  inferred from the dialogue (e.g. mother, father, child, friend).
- Assign each line an emotion from the closed set:
  neutral, happy, sad, angry, surprised, worried, excited, annoyed.
- Infer a single `setting` from the closed set:
  living_room, kitchen, bedroom, office, classroom, outdoor_park, street, cafe, unknown.
- Build a simple `camera` plan: one medium shot per line, focused on whoever is
  speaking, spanning that line's timestamps. No pans, no effects.
- Leave `sprite` null — assets are resolved by a later stage.
- Respond with JSON only, matching the provided schema exactly."""


def build_user_prompt(transcript: dict) -> str:
    lines = []
    for i, seg in enumerate(transcript["segments"]):
        lines.append(
            f"seg_{i}: [{seg['speaker']}] {seg['start']:.2f}-{seg['end']:.2f}: {seg['text']}"
        )
    return (
        "Here is the diarized transcript. Produce the scene script.\n\n"
        + "\n".join(lines)
    )


def missing_segments(transcript: dict, script: SceneScript) -> list[int]:
    """Return transcript segment indices not covered by any script line."""
    covered = {round(line.start, 2) for line in script.lines}
    return [
        i for i, seg in enumerate(transcript["segments"])
        if round(seg["start"], 2) not in covered
    ]


def coverage_check(transcript: dict) -> Callable[[SceneScript], str | None]:
    total = len(transcript["segments"])

    def check(script: SceneScript) -> str | None:
        missing = missing_segments(transcript, script)
        if missing:
            return (
                f"your script has {len(script.lines)} lines but the transcript has "
                f"{total} segments; these segment indices are missing: {missing}. "
                "Include one line per transcript segment, verbatim."
            )
        return None

    return check


def structure_script(
    transcript_path: str | Path,
    output_path: str | Path | None = None,
) -> SceneScript:
    transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    if not transcript.get("segments"):
        raise ValueError(f"transcript {transcript_path} has no segments")

    print(f"[structure] structuring {transcript_path} ({len(transcript['segments'])} segments)")
    script = generate_structured(
        SceneScript,
        SYSTEM_PROMPT,
        build_user_prompt(transcript),
        validate=coverage_check(transcript),
    )

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(script.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[structure] wrote {out}")

    print(
        f"[structure] done: setting={script.setting}, "
        f"{len(script.characters)} character(s), {len(script.lines)} line(s), "
        f"{len(script.camera)} shot(s)"
    )
    return script


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcript -> structured scene script")
    parser.add_argument("transcript", help="input transcript.json")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output script.json path")
    args = parser.parse_args(argv)

    try:
        structure_script(args.transcript, output_path=args.output)
        return 0
    except (StructuredOutputError, ValueError, json.JSONDecodeError) as exc:
        print(f"[structure] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
