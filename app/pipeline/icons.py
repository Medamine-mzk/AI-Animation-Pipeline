"""Icons: transcript.json + word->emoji dictionary -> icons.json (word-icon timing).

Every mapped word found in the transcript gets a display interval derived from
its real spoken span, chained so icons persist until just before the next icon:
    end = max(word.end, start + MIN_HOLD)  stretched to next icon's start, capped

A coverage report lists content words with no icon, so the dictionary (or the
clip's icon_map.json) can be extended for future videos.

Usage:
    python -m app.pipeline.icons jobs/golden/transcript.json \
        -o jobs/golden/icons.json --map jobs/golden/icon_map.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DICT = ROOT / "tools" / "icons" / "dictionary.json"
DEFAULT_OUT = ROOT / "jobs" / "golden" / "icons.json"

MIN_HOLD = 0.7        # minimum seconds an icon stays visible
HOLD_GAP = 0.25       # seconds of gap before the next icon pops in
MAX_CHAIN = 4.0       # cap: never let one icon swallow more than this

_STOPWORDS = {
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their", "a", "an", "the",
    "and", "or", "but", "so", "if", "then", "than", "that", "this", "these",
    "those", "to", "of", "in", "on", "at", "by", "for", "with", "about",
    "from", "up", "down", "out", "into", "over", "under", "is", "are", "was",
    "were", "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "can", "could", "should", "may", "might", "shall", "must",
    "am",     "im", "ive", "id", "ill", "youre", "youve", "youll", "hes", "shes",
    "its", "thats", "dont", "cant", "wont", "isnt", "arent", "wasnt", "werent",
    "not", "no", "yes", "just", "very", "really", "there", "here", "what",
    "when", "where", "why", "who", "which", "how", "all", "some", "any",
    "more", "most", "other", "only", "as", "at", "well", "now", "ok", "okay",
}

_WORD_RE = re.compile(r"[A-Za-z']+")


def normalize(word: str) -> str:
    """Lowercase, strip punctuation/apostrophes for dictionary lookup."""
    m = _WORD_RE.search(word)
    key = m.group(0) if m else word
    return re.sub(r"'", "", key).lower()


def load_word_map(path: str | Path) -> dict[str, str]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def merged_map(dictionary: dict[str, str], clip_map: dict[str, str]) -> dict[str, str]:
    """Clip map overrides the general dictionary on conflicts."""
    merged = dict(dictionary)
    merged.update(clip_map)
    return merged


def _content_words(transcript: dict) -> set[str]:
    words = set()
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            key = normalize(w["word"])
            if key and key not in _STOPWORDS:
                words.add(key)
    return words


def build_icons(
    transcript: dict,
    dictionary: dict[str, str],
    clip_map: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Emit {word, icon, start, end} with chained persistence.

    Returns (icons, missing) where missing lists content words with no icon.
    """
    words = merged_map(dictionary, clip_map or {})
    events: list[dict] = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words", []):
            key = normalize(w["word"])
            emoji = words.get(key)
            if emoji is None:
                continue
            events.append(
                {
                    "word": key,
                    "icon": emoji,
                    "start": round(float(w["start"]), 3),
                    "end": round(float(w["end"]), 3),
                }
            )
    events.sort(key=lambda e: (e["start"], e["end"]))

    # Chain: each icon holds until just before the next icon's start (capped).
    for i, ev in enumerate(events):
        spoken_end = max(ev["end"], ev["start"] + MIN_HOLD)
        if i + 1 < len(events):
            next_start = events[i + 1]["start"]
            held_end = max(spoken_end, next_start - HOLD_GAP)
            held_end = min(held_end, ev["start"] + MAX_CHAIN)
        else:
            held_end = spoken_end
        ev["end"] = round(held_end, 3)

    missing = sorted(_content_words(transcript) - set(words))
    return events, missing


def build_icons_file(
    transcript_path: str | Path,
    output_path: str | Path | None = None,
    dictionary_path: str | Path | None = None,
    clip_map_path: str | Path | None = None,
) -> list[dict]:
    transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    dictionary = load_word_map(dictionary_path or DEFAULT_DICT)
    clip_map = load_word_map(clip_map_path) if clip_map_path else {}
    icons, missing = build_icons(transcript, dictionary, clip_map)

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(icons, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[icons] wrote {out}")

    print(f"[icons] done: {len(icons)} icon event(s)")
    if missing:
        print(f"[icons] no icon for {len(missing)} content word(s): {', '.join(missing)}")
        print("[icons] add them to tools/icons/dictionary.json or the clip icon_map.json")
    return icons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="transcript.json -> word-icon timeline")
    parser.add_argument("transcript", help="input transcript.json")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="output icons.json path")
    parser.add_argument("--dict", default=str(DEFAULT_DICT), help="general word->emoji dictionary JSON")
    parser.add_argument("--map", default=None, help="clip icon_map.json override (optional)")
    args = parser.parse_args(argv)

    try:
        build_icons_file(
            args.transcript,
            output_path=args.output,
            dictionary_path=args.dict,
            clip_map_path=args.map,
        )
        return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[icons] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())