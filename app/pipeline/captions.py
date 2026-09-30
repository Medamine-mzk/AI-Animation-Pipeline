"""M1: speaker-aware caption pagination and palette (captions feature, spec 3.3, 4).

Turns a :class:`~app.schemas.word_timeline.WordTimeline` into the list of short
caption pages the renderer draws.

This is a hand-rolled equivalent of Remotion's ``createTikTokStyleCaptions``,
written as pure Python. The project has no npm build step and no React, and
adopting Remotion to obtain ~60 lines of grouping logic would mean introducing a
bundler and a second rendering engine. The data emitted here is deliberately
shaped like ``@remotion/captions`` output so swapping in that library later is a
drop-in change, not a rewrite.

Pure functions only: no I/O, no model, no network.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from app.schemas.captions import (
    CaptionPage,
    CaptionSet,
    PageWord,
    SpeakerStyle,
)
from app.schemas.word_timeline import WordTimeline

# ---------------------------------------------------------------- page sizing

#: spec 2 -- "2-8 words at a time, TikTok-caption style". A single-word page is
#: still emitted when a speaker only says one thing, because dropping speech to
#: satisfy a minimum would be worse than a short page.
MIN_WORDS_PER_PAGE = 2
MAX_WORDS_PER_PAGE = 8

#: A page should be readable rather than linger. The highlighter moves the pill
#: between words, so a very long page means the viewer reads a long burst.
MAX_PAGE_MS = 4000

#: Gaps longer than this end the current page. Keeps a caption from sitting on
#: screen through a pause (spec 7: no page during silence).
SILENCE_GAP_MS = 900

#: Sentence-final punctuation also ends a page -- a full stop is a natural
#: reading beat regardless of how long the sentence took.
_SENTENCE_END = re.compile(r"[.!?。！？]$")


# ----------------------------------------------------------------- appearance

#: Semi-opaque dark plate behind the text, per spec 4 ("a soft dark translucent
#: backdrop bar behind the text block ... not just a text shadow").
BACKDROP = "#0b0b12"

#: Speaker accent colours, in order of first appearance.
#:
#: Chosen to be safe for the common forms of colour-vision deficiency: the
#: series alternates blue / amber / violet / teal / rose / lime rather than
#: running along the red-green axis that deuteranopia and protanopia collapse.
#:
#: Do not hardcode expected ratios here. ``audit_palette()`` computes them, and
#: the test suite asserts every entry clears WCAG AA (4.5:1) both on the
#: backdrop and under the active-word pill, so a future edit that breaks
#: contrast fails CI instead of shipping an unreadable caption.
SPEAKER_PALETTE = [
    "#7dd3fc",  # sky
    "#fcd34d",  # amber
    "#c4b5fd",  # violet
    "#5eead4",  # teal
    "#fda4af",  # rose
    "#bef264",  # lime
    "#93c5fd",  # blue-300
    "#fdba74",  # orange
]

#: Pill fill for the currently-spoken word: a darkened version of the accent so
#: white text on top of it stays above 4.5:1.
ACTIVE_PILL_DARKEN = 0.62


# ---------------------------------------------------------------- colour maths

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _parse_hex(color: str) -> tuple[int, int, int]:
    """Parse ``#rgb`` or ``#rrggbb`` into 0-255 channels."""
    if not isinstance(color, str) or not _HEX_RE.match(color):
        raise ValueError(f"not a hex colour: {color!r}")
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def relative_luminance(color: str) -> float:
    """WCAG 2.1 relative luminance of a hex colour, in 0.0-1.0."""
    r, g, b = _parse_hex(color)
    linear = []
    for raw in (r, g, b):
        c = raw / 255.0
        # sRGB -> linear RGB
        linear.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio between two hex colours, in 1.0-21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def darken(color: str, amount: float) -> str:
    """Scale a colour toward black by ``amount`` (0.0-1.0)."""
    r, g, b = _parse_hex(color)
    f = 1.0 - max(0.0, min(1.0, amount))
    return _to_hex((int(r * f), int(g * f), int(b * f)))


def _active_pill_for(color: str) -> str:
    """Pick a pill fill that keeps white text readable on top of it.

    Darkens the accent until white-on-pill clears 4.5:1, rather than assuming one
    fixed darkening is enough for every entry in the palette.
    """
    for amount in (ACTIVE_PILL_DARKEN, 0.7, 0.78, 0.86, 0.94):
        candidate = darken(color, amount)
        if contrast_ratio("#ffffff", candidate) >= 4.5:
            return candidate
    return darken(color, 0.94)


def speaker_style(speaker_id: str, index: int) -> SpeakerStyle:
    """Build the style entry for the speaker at ``index`` in appearance order."""
    color = SPEAKER_PALETTE[index % len(SPEAKER_PALETTE)]
    return SpeakerStyle(
        speakerId=speaker_id,
        index=index,
        color=color,
        activeColor=_active_pill_for(color),
        contrastRatio=round(contrast_ratio(color, BACKDROP), 2),
    )


def audit_palette() -> list[dict]:
    """Report every palette entry's contrast, for CI or a debugging console.

    Called by the test suite so a colour change that breaks WCAG AA is caught
    before it reaches a student's screen.
    """
    return [
        {
            "color": color,
            "onBackdrop": round(contrast_ratio(color, BACKDROP), 2),
            "whiteOnPill": round(contrast_ratio("#ffffff", _active_pill_for(color)), 2),
            "passesAA": contrast_ratio(color, BACKDROP) >= 4.5,
        }
        for color in SPEAKER_PALETTE
    ]


# ---------------------------------------------------------------- pagination


def paginate(
    words: Iterable[tuple[PageWord, str]], max_words: int = MAX_WORDS_PER_PAGE
) -> list[list[tuple[PageWord, str]]]:
    """Group ``(word, speakerId)`` pairs into pages, never mixing speakers.

    Mirrors ``createTikTokStyleCaptions``'s intent but splits on speaker change
    first, so a page can never contain two speakers (spec 3.3). A page ends when
    any of these holds: the speaker changes, the word cap is reached, the page
    has been on screen too long, a silence follows, or a sentence ends after the
    minimum has been satisfied.
    """
    pages: list[list[tuple[PageWord, str]]] = []
    current: list[tuple[PageWord, str]] = []
    previous_end: int | None = None

    for page_word, speaker_id in words:
        speaker_changed = bool(current) and current[-1][1] != speaker_id
        gap = 0 if previous_end is None else max(0, page_word.startMs - previous_end)
        window = page_word.endMs - current[0][0].startMs if current else 0

        if current and (
            speaker_changed
            or len(current) >= max_words
            or window >= MAX_PAGE_MS
            or gap >= SILENCE_GAP_MS
            or (
                len(current) >= MIN_WORDS_PER_PAGE
                and _SENTENCE_END.search(current[-1][0].displayText)
            )
        ):
            pages.append(current)
            current = []

        current.append((page_word, speaker_id))
        previous_end = page_word.endMs

    if current:
        pages.append(current)
    return pages


def build_caption_set(
    timeline: WordTimeline,
    max_words: int = MAX_WORDS_PER_PAGE,
) -> CaptionSet:
    """Build every caption page for a clip, plus its speaker style table.

    Raises ``ValueError`` when the timeline has no words: an empty caption set
    would render a blank lower third and read as "captions finished" rather
    than "we found no speech", so the caller is made to handle it explicitly.
    """
    if not timeline.words:
        raise ValueError(
            "word timeline is empty - refusing to emit a caption set that would "
            "render as 'no captions' rather than 'no speech found'"
        )

    pairs: list[tuple[PageWord, str]] = [
        (
            PageWord(
                text=w.text,
                startMs=w.startMs,
                endMs=w.endMs,
                confidence=w.confidence,
            ),
            w.speakerId,
        )
        for w in timeline.words
    ]

    pages: list[CaptionPage] = []
    for index, group in enumerate(paginate(pairs, max_words=max_words)):
        members = [pw for pw, _ in group]
        pages.append(
            CaptionPage(
                index=index,
                speakerId=group[0][1],
                startMs=members[0].startMs,
                endMs=members[-1].endMs,
                words=members,
            )
        )

    # Appearance order drives the colour assignment (spec 4).
    ordered_speakers = [
        s.speakerId for s in sorted(timeline.speakers, key=lambda s: s.index)
    ]
    styles = [
        speaker_style(sid, index) for index, sid in enumerate(ordered_speakers)
    ]

    return CaptionSet(
        language=timeline.language,
        durationMs=timeline.durationMs,
        pages=pages,
        styles=styles,
        wordAlignment=timeline.wordAlignment,
        wordCount=sum(len(p.words) for p in pages),
    )


def page_timeline(pages: Sequence[CaptionPage], time_ms: int) -> list[CaptionPage]:
    """Return the pages visible at ``time_ms``.

    Called by the renderer every frame, so this stays a cheap scan over a small
    page list and returns a list rather than a generator to keep the hot path
    trivial.
    """
    return [p for p in pages if p.startMs <= time_ms < p.endMs]


def active_word(page: CaptionPage, time_ms: int) -> int | None:
    """Index of the word being spoken inside ``page`` at ``time_ms``, if any.

    Falls back to the last word whose start has passed, so the pill keeps showing
    the word just spoken during the small gaps forced alignment leaves between
    words, instead of blinking off for a few frames.
    """
    chosen: int | None = None
    for i, word in enumerate(page.words):
        if word.startMs <= time_ms:
            chosen = i
        else:
            break
    return chosen


def low_confidence_words(
    caption_set: CaptionSet, threshold: float = 0.5
) -> list[tuple[int, str, float]]:
    """Words worth underlining in the editor, as ``(pageIndex, text, conf)``.

    Words with no score are excluded: unknown confidence is not low confidence,
    and marking every unscored word would make the affordance useless.
    """
    flagged: list[tuple[int, str, float]] = []
    for page in caption_set.pages:
        for word in page.words:
            if word.confidence is not None and word.confidence < threshold:
                flagged.append((page.index, word.displayText, word.confidence))
    return flagged


def summary(caption_set: CaptionSet) -> dict:
    """Small dict for job metadata and the UI status line."""
    return {
        "pages": len(caption_set.pages),
        "speakers": len(caption_set.styles),
        "words": sum(len(p.words) for p in caption_set.pages),
        "durationMs": caption_set.durationMs,
        "language": caption_set.language,
    }
