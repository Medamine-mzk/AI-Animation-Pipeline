"""Speaker-repair: transcript.json -> {seg_N: SPEAKER_XX} remap via LLM.

Diarization can merge distinct people into one SPEAKER_XX cluster (or split
one person across clusters). When that happens the downstream structure stage
cannot recover the true cast, because it is pinned to the transcript's labels.

This stage re-reads the full dialogue and uses the LLM to reassign every
segment to a consistent, canonical speaker label (SPEAKER_00..SPEAKER_N),
splitting or merging diarization clusters as the dialogue demands. The output
is a remap file that structure_script accepts via --remap-file.

Usage:
    python -m app.pipeline.repair_speakers jobs/golden/transcript.json -o jobs/golden/speaker_remap.json
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from app.pipeline.llm import StructuredOutputError, generate_structured

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "jobs" / "golden" / "speaker_remap.json"

SYSTEM_PROMPT = """You repair speaker labels in a diarized conversation transcript.

A previous diarization step assigned each transcript segment a SPEAKER_XX label,
but it can be wrong: it may merge two distinct people into one label, or split
one person across several labels. Your job is to figure out, from the dialogue
itself, who actually says each line, and assign every segment to a consistent
canonical speaker label.

Rules:
- Use dialogue content to identify distinct people: names, vocatives ("Mom,",
  "Dad," "Hazem?"), pronouns, family roles, question/answer adjacency, changes
  in who is being addressed.
- Reuse the existing labels where they are correct (SPEAKER_00, SPEAKER_01,
  ...). You may keep a label, reassign a segment to a different existing label,
  or introduce NEW labels (e.g. SPEAKER_03) when a cluster actually contains
  two people.
- Output the SMALLEST number of speakers consistent with the dialogue: if two
  adjacent segments are clearly the same person, they must share one label.
- Every segment must be assigned exactly one label.
- Assign each real person exactly one label across the whole transcript: no
  person may appear under two different labels.
- Respond with JSON only, matching the provided schema exactly."""


class SegmentRemap(BaseModel):
    index: int = Field(description="transcript segment index (0-based)")
    speaker: str = Field(description="canonical speaker label, e.g. SPEAKER_00")


class RepairResult(BaseModel):
    segments: list[SegmentRemap] = Field(description="one entry per transcript segment")


def build_user_prompt(transcript: dict) -> str:
    lines = []
    for i, seg in enumerate(transcript["segments"]):
        lines.append(
            f"seg_{i}: [{seg['speaker']}] {seg['start']:.2f}-{seg['end']:.2f}: {seg['text']}"
        )
    return (
        "Here is the diarized transcript. Reassign each segment to a canonical "
        "speaker label.\n\n"
        + "\n".join(lines)
    )


def make_validator(transcript: dict) -> callable:
    total = len(transcript["segments"])

    def validate(result: RepairResult) -> str | None:
        indexes = [s.index for s in result.segments]
        missing = [i for i in range(total) if i not in indexes]
        if missing:
            return (
                f"you covered segments {sorted(indexes)} but the transcript has "
                f"{total} segments; these are missing: {missing}. Emit exactly one "
                "entry per segment index."
            )
        seen = set()
        duplicated = sorted(i for i in indexes if i in seen or seen.add(i))
        if duplicated:
            return f"duplicate entries for segments {duplicated}; emit each index once."
        labels = [s.speaker for s in result.segments]
        bad_label = [lab for lab in labels if not lab.startswith("SPEAKER_")]
        if bad_label:
            return (
                f"invalid speaker labels {bad_label}; use SPEAKER_00, SPEAKER_01, ..."
            )
        return None

    return validate


def to_remap_dict(transcript: dict, result: RepairResult) -> dict[str, str]:
    """{seg_N: speaker} in transcript order, for structure_script --remap-file."""
    by_index = {s.index: s.speaker for s in result.segments}
    return {
        f"seg_{i}": by_index.get(i, transcript["segments"][i]["speaker"])
        for i in range(len(transcript["segments"]))
    }


def repair_speakers(
    transcript_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, str]:
    transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    if not transcript.get("segments"):
        raise ValueError(f"transcript {transcript_path} has no segments")

    print(f"[repair] repairing speakers in {transcript_path} ({len(transcript['segments'])} segments)")
    result = generate_structured(
        RepairResult,
        SYSTEM_PROMPT,
        build_user_prompt(transcript),
        validate=make_validator(transcript),
    )
    remap = to_remap_dict(transcript, result)

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(remap, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[repair] wrote {out}")

    speakers = sorted({v for v in remap.values()})
    changed = sum(1 for i, seg in enumerate(transcript["segments"]) if seg["speaker"] != remap[f"seg_{i}"])
    print(f"[repair] done: {len(speakers)} speaker(s) {speakers}, {changed} segment(s) relabeled")
    return remap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="transcript.json -> speaker remap")
    parser.add_argument("transcript", help="input transcript.json")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output speaker_remap.json path")
    args = parser.parse_args(argv)

    try:
        repair_speakers(args.transcript, output_path=args.output)
        return 0
    except (StructuredOutputError, ValueError, json.JSONDecodeError) as exc:
        print(f"[repair] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())