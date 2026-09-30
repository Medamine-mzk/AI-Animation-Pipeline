"""Pydantic models for the real word-level timeline (captions feature, spec 3.2).

This is a NEW artifact read only by the captions pipeline. It is deliberately
separate from `app.schemas.transcript.Transcript`, which the existing 3D
animation feature reads and which must not change.

Design notes
------------
* Field names and units mirror `@remotion/captions`' `Caption` shape (ms, with a
  leading space on `text`) so this data is drop-in compatible if the project ever
  adopts that library, without depending on it today.
* Unlike `dialogue.json`'s `wtimes`/`wdurations` -- which are produced by
  `tools/picker_to_dialogue.py` by dividing each segment's duration by its word
  count -- every timestamp here is *measured* by WhisperX forced alignment. The
  `wordAlignment` field records that provenance so the UI never implies these
  are estimates, and never silently treats an estimate as measured.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#: How the word timestamps in this file were obtained. ``measured`` means
#: WhisperX forced alignment. There is intentionally no ``estimated`` value here:
#: a fabricated timeline is a different artifact and belongs in a different file,
#: so a consumer can never confuse the two.
WordAlignment = Literal["measured"]

SCHEMA_VERSION = "word_timeline/1"


class TimelineWord(BaseModel):
    """One spoken word, with the speaker who said it."""

    text: str
    startMs: int = Field(ge=0)
    endMs: int = Field(ge=0)
    timestampMs: int = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    speakerId: str = Field(min_length=1)

    @field_validator("endMs")
    @classmethod
    def end_after_start(cls, v: int, info) -> int:
        start = info.data.get("startMs")
        if start is not None and v < start:
            raise ValueError(f"word endMs ({v}) before startMs ({start})")
        return v

    @field_validator("confidence")
    @classmethod
    def finite_confidence(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v != v:  # NaN
            return None
        return v


class TimelineSpeaker(BaseModel):
    """A detected speaker, indexed in order of first appearance (spec 4)."""

    speakerId: str = Field(min_length=1)
    index: int = Field(ge=0)
    firstWordMs: int = Field(ge=0)
    lastWordMs: int = Field(ge=0)
    wordCount: int = Field(ge=1)
    totalMs: int = Field(ge=0)
    #: Words this speaker "owns" that the diarizer never labelled; they were
    #: inherited from the preceding labelled word. Surfaced so the editor can
    #: show that the split is not fully certain.
    inheritedWordCount: int = Field(default=0, ge=0)


class OverlapWarning(BaseModel):
    """A window where two different speakers both have words (spec 7).

    Kept as a warning rather than an error: overlapping speech is expected and
    export should still be allowed.
    """

    startMs: int = Field(ge=0)
    endMs: int = Field(ge=0)
    speakerIds: list[str] = Field(min_length=2)

    @field_validator("speakerIds")
    @classmethod
    def distinct_speakers(cls, v: list[str]) -> list[str]:
        if len(set(v)) < 2:
            raise ValueError("overlap must involve at least 2 distinct speakers")
        return v

    @model_validator(mode="after")
    def end_after_start(self) -> "OverlapWarning":
        if self.endMs < self.startMs:
            raise ValueError(f"overlap endMs ({self.endMs}) before startMs ({self.startMs})")
        return self


class WordTimeline(BaseModel):
    """The whole real word-level timeline for one clip."""

    #: Named `schemaVersion` rather than `schema` because the latter shadows a
    #: deprecated ``BaseModel`` attribute and trips a pydantic warning.
    schemaVersion: str = SCHEMA_VERSION
    language: str = ""
    durationMs: int = Field(ge=0)
    words: list[TimelineWord] = Field(min_length=1)
    speakers: list[TimelineSpeaker] = Field(min_length=1)
    overlaps: list[OverlapWarning] = Field(default_factory=list)
    wordAlignment: WordAlignment = "measured"    #: True when some words had no diarizer label and were attributed to the
    #: preceding speaker. The editor surfaces this as "check the speaker split".
    hasInheritedSpeakers: bool = False
    #: True when no word carried a confidence score, so the low-confidence
    #: affordance (spec 7) has nothing to work from. Honest, not an error.
    hasConfidence: bool = False

    @model_validator(mode="after")
    def speakers_cover_words(self) -> "WordTimeline":
        known = {s.speakerId for s in self.speakers}
        unknown = {w.speakerId for w in self.words} - known
        if unknown:
            raise ValueError(f"words reference undeclared speakers: {sorted(unknown)}")
        return self

    @model_validator(mode="after")
    def words_are_ordered(self) -> "WordTimeline":
        starts = [w.startMs for w in self.words]
        if starts != sorted(starts):
            raise ValueError("words must be sorted by startMs")
        return self
