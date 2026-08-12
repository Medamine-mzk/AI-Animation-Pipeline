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


def load_remap_file(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return {
        str(k): str(v)
        for k, v in json.loads(p.read_text(encoding="utf-8-sig")).items()
    }


def parse_remap_arg(arg: str | None) -> dict[str, str]:
    """Parse 'seg_3=SPEAKER_02,seg_5=SPEAKER_01' into {ref: speaker}."""
    if not arg:
        return {}
    out = {}
    for pair in arg.split(","):
        if "=" not in pair:
            raise ValueError(f"bad remap entry {pair!r} (expected seg_N=SPEAKER_XX)")
        key, value = pair.strip().split("=", 1)
        out[key.strip()] = value.strip()
    return out


def apply_remap_segments(transcript: dict, remap: dict[str, str]) -> dict:
    """Override segment speakers by transcript index (seg_3=SPEAKER_02)."""
    if not remap:
        return transcript
    segments = []
    for i, seg in enumerate(transcript["segments"]):
        updated = dict(seg)
        updated["speaker"] = remap.get(f"seg_{i}", seg["speaker"])
        segments.append(updated)
    return {**transcript, "segments": segments}


def speaker_consistency_check(
    transcript: dict, remap: dict[str, str] | None = None
) -> Callable[[SceneScript], str | None]:
    """Validate that each line's character matches its segment's (remapped) speaker."""
    corrected = apply_remap_segments(transcript, remap or {})
    ref_speaker = {
        f"seg_{i}": seg["speaker"] for i, seg in enumerate(corrected["segments"])
    }

    def check(script: SceneScript) -> str | None:
        by_id = {c.id: c.speaker_ref for c in script.characters}
        bad = [
            line.audio_segment_ref
            for line in script.lines
            if by_id.get(line.character_id) != ref_speaker.get(line.audio_segment_ref)
        ]
        if bad:
            return (
                f"lines {bad} belong to a different character than the transcript "
                "speaker of their audio segment (per segment speaker refs). Set "
                "each line's character_id to the character whose speaker_ref matches "
                "the segment's SPEAKER_XX."
            )
        return None

    return check


def remap_script(script: SceneScript, remap: dict[str, str]) -> SceneScript:
    """Deterministically move lines/camera to the corrected speaker's character."""
    if not remap:
        return script
    char_by_speaker = {c.speaker_ref: c for c in script.characters}
    lines = []
    for line in script.lines:
        target = char_by_speaker.get(remap.get(line.audio_segment_ref, ""))
        if target is not None and target.id != line.character_id:
            line = line.model_copy(update={"character_id": target.id})
        lines.append(line)
    camera = []
    for shot in script.camera:
        for line in script.lines:
            if (
                round(line.start, 2) == round(shot.start, 2)
                and round(line.end, 2) == round(shot.end, 2)
                and shot.focus_character == line.character_id
                and line.audio_segment_ref in remap
            ):
                target = char_by_speaker[remap[line.audio_segment_ref]]
                shot = shot.model_copy(update={"focus_character": target.id})
                break
        camera.append(shot)
    return script.model_copy(update={"lines": lines, "camera": camera})


def structure_script(
    transcript_path: str | Path,
    output_path: str | Path | None = None,
    remap: dict[str, str] | None = None,
    remap_file: str | Path | None = None,
) -> SceneScript:
    transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    if not transcript.get("segments"):
        raise ValueError(f"transcript {transcript_path} has no segments")

    remap = {**(remap or {}), **load_remap_file(remap_file)}
    if remap:
        print(f"[structure] applying speaker remap: {remap}")
    transcript = apply_remap_segments(transcript, remap)
    checks = [coverage_check(transcript), speaker_consistency_check(transcript)]

    def combined_check(script: SceneScript) -> str | None:
        for check in checks:
            error = check(script)
            if error:
                return error
        return None

    print(f"[structure] structuring {transcript_path} ({len(transcript['segments'])} segments)")
    script = generate_structured(
        SceneScript,
        SYSTEM_PROMPT,
        build_user_prompt(transcript),
        validate=combined_check,
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
    parser.add_argument("transcript", help="input transcript.json (or script.json with --remap-only)")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output script.json path")
    parser.add_argument("--remap", default=None, help="inline speaker remap: seg_3=SPEAKER_02,seg_5=SPEAKER_01")
    parser.add_argument("--remap-file", default=None, help="JSON file with {seg_N: SPEAKER_XX}")
    parser.add_argument("--remap-only", action="store_true",
                        help="skip the LLM; deterministically remap an existing script.json")
    args = parser.parse_args(argv)

    try:
        remap = parse_remap_arg(args.remap)
        if args.remap_only:
            path = Path(args.transcript)
            script = SceneScript.model_validate_json(path.read_text(encoding="utf-8"))
            remap = {**(remap or {}), **load_remap_file(args.remap_file)}
            if not remap:
                raise ValueError("--remap-only requires --remap or --remap-file")
            script = remap_script(script, remap)
            out = Path(args.output) if args.output != str(DEFAULT_OUT) else path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(script.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[structure] remapped {len(script.lines)} line(s) -> {out}")
            return 0
        structure_script(
            args.transcript, output_path=args.output, remap=remap, remap_file=args.remap_file
        )
        return 0
    except (StructuredOutputError, ValueError, json.JSONDecodeError) as exc:
        print(f"[structure] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
