"""Generate the committed demo fixture using the real M0+M1 pipeline.

Run:  python tools/captions/build_demo_fixture.py
Writes: captions_demo/captions.json

Hand-writing this file would risk drifting from the schema, so it is generated
from the golden clip's real WhisperX-aligned words and then trimmed to a short
loop that is pleasant to watch.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.pipeline.captions import build_caption_set, summary  # noqa: E402
from app.pipeline.word_timeline import build_word_timeline  # noqa: E402

OUT = ROOT / "captions_demo" / "captions.json"
CLIP_SECONDS = 22.0


def main() -> int:
    src = json.loads(
        (ROOT / "jobs" / "golden" / "transcript.json").read_text(encoding="utf-8")
    )

    # Take only the opening of the clip so the demo loops quickly.
    raw = []
    for seg in src["segments"]:
        if seg["start"] > CLIP_SECONDS:
            continue
        for w in seg["words"]:
            raw.append(
                {
                    "word": w["word"],
                    "start": w["start"],
                    "end": w["end"],
                    # transcript.json drops per-word speaker; the segment's is the
                    # best available label for a fixture.
                    "speaker": seg["speaker"],
                }
            )

    timeline = build_word_timeline(
        raw, language="en", duration_s=CLIP_SECONDS
    )
    caption_set = build_caption_set(timeline)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(caption_set.model_dump_json(indent=2), encoding="utf-8")

    print(f"[demo] wrote {OUT}")
    print(f"[demo] {summary(caption_set)}")
    for page in caption_set.pages:
        style = next(
            s for s in caption_set.styles if s.speakerId == page.speakerId
        )
        print(
            f"[demo]  [{page.index:>2}] {page.startMs:>6}-{page.endMs:<6}ms "
            f"{style.color}  {page.text!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
