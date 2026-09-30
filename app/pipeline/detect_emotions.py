"""Emotion detection: classify each script line with roberta-base-go_emotions.

Reads script.json lines, classifies each line's text with the local
`SamLowe/roberta-base-go_emotions` model (28 go_emotions labels) and writes
`line_emotions.json` for the 3D renderer to pick animation clips from.

Runs offline as a batch step (no API server needed). If the model / torch /
transformers are unavailable it exits non-zero so the renderer can fall back
to the static `emotion` field already stored in script.json.

Usage:
    python -m app.pipeline.detect_emotions [--script jobs/golden/script.json] [-o jobs/golden/line_emotions.json]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCRIPT = ROOT / "jobs" / "golden" / "script.json"
DEFAULT_OUT = ROOT / "jobs" / "golden" / "line_emotions.json"


def classify(texts: list[str]) -> list[dict]:
    from transformers import pipeline

    classifier = pipeline(
        "text-classification",
        model="SamLowe/roberta-base-go_emotions",
        top_k=None,
    )
    out = classifier(texts)
    results = []
    for row in out:
        ranked = sorted(row, key=lambda x: x["score"], reverse=True)
        results.append(
            {
                "top": ranked[0]["label"],
                "top3": [{"label": r["label"], "score": round(r["score"], 4)} for r in ranked[:3]],
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="classify script lines into go_emotions labels")
    parser.add_argument("--script", default=str(DEFAULT_SCRIPT), help="input script.json")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output line_emotions.json")
    args = parser.parse_args(argv)

    script_path = Path(args.script)
    script = json.loads(script_path.read_text(encoding="utf-8"))
    lines = script["lines"]
    texts = [line["text"] for line in lines]

    try:
        results = classify(texts)
    except Exception as exc:  # noqa: BLE001 - any model/dep failure = fallback path
        print(f"[detect_emotions] FAILED (renderer will fall back to script emotions): {exc}",
              file=sys.stderr)
        return 1

    out = {}
    for line, res in zip(lines, results):
        key = f"{round(line['start'], 2):.2f}"
        out[key] = {
            "character_id": line["character_id"],
            "text": line["text"],
            **res,
        }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[detect_emotions] wrote {len(out)} line emotions -> {out_path}")
    for key in sorted(out, key=float):
        print(f"  {key:>7}s {out[key]['character_id']:<11} {out[key]['top']:<14} {lines[int(list(out).index(key))]['text'][:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())