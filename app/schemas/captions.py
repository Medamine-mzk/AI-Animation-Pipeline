"""Pydantic models for speaker-aware caption pages (captions feature, spec 3.3, 4).

A `CaptionPage` is the unit the renderer draws: a short burst of 2-8 words
belonging to exactly one speaker. Pages never mix speakers, which is what makes
per-speaker colour coding meaningful (spec 4).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "captions/1"


class PageWord(BaseModel):
    """One word inside a page, carrying the timing the highlighter needs."""

    text: str
    startMs: int = Field(ge=0)
    endMs: int = Field(ge=0)
    #: None when the ASR gave no score. The editor underlines these rather than
    #: assuming they are fine (spec 7).
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def displayText(self) -> str:
        """Text without the leading-space join convention."""
        return self.text.strip()


class CaptionPage(BaseModel):
    """2-8 consecutive words from a single speaker."""

    index: int = Field(ge=0)
    speakerId: str = Field(min_length=1)
    startMs: int = Field(ge=0)
    endMs: int = Field(ge=0)
    words: list[PageWord] = Field(min_length=1)

    @model_validator(mode="after")
    def words_within_page_window(self) -> "CaptionPage":
        if self.words[0].startMs < self.startMs:
            raise ValueError("first word starts before the page")
        if self.words[-1].endMs > self.endMs:
            raise ValueError("last word ends after the page")
        return self

    @property
    def text(self) -> str:
        return "".join(w.text for w in self.words).strip()


class SpeakerStyle(BaseModel):
    """Per-speaker presentation, derived from the appearance-ordered palette."""

    speakerId: str = Field(min_length=1)
    index: int = Field(ge=0)
    #: Base text colour when the word is not currently spoken.
    color: str
    #: Pill fill behind the currently-spoken word.
    activeColor: str
    #: Contrast ratio of `color` against the caption backdrop, verified at build
    #: time so a bad palette combination fails the test suite rather than
    #: shipping an unreadable caption.
    contrastRatio: float = Field(ge=1.0)


class CaptionSet(BaseModel):
    """Every page for one clip, plus the style table the renderer needs."""

    schemaVersion: str = SCHEMA_VERSION
    language: str = ""
    durationMs: int = Field(ge=0)
    pages: list[CaptionPage] = Field(min_length=1)
    styles: list[SpeakerStyle] = Field(min_length=1)

    @model_validator(mode="after")
    def style_covers_every_speaker(self) -> "CaptionSet":
        styled = {s.speakerId for s in self.styles}
        used = {p.speakerId for p in self.pages}
        missing = used - styled
        if missing:
            raise ValueError(f"pages reference unstyled speakers: {sorted(missing)}")
        return self

    @model_validator(mode="after")
    def pages_do_not_overlap(self) -> "CaptionSet":
        """Pages must not overlap, or two captions would be on screen at once.

        Overlaps between *different* speakers can legitimately happen when
        diarization disagrees, so those are resolved by trimming the later page
        rather than by rejecting the data.
        """
        ordered = sorted(self.pages, key=lambda p: (p.startMs, p.index))
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.startMs < prev.endMs:
                raise ValueError(
                    f"page {nxt.index} starts at {nxt.startMs}ms, before page "
                    f"{prev.index} ends at {prev.endMs}ms"
                )
        return self
