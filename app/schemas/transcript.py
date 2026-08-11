"""Pydantic models for the diarized transcript (spec 3.1)."""

from pydantic import BaseModel, Field, field_validator


class Word(BaseModel):
    word: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @field_validator("end")
    @classmethod
    def end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError(f"word end ({v}) before start ({start})")
        return v


class Segment(BaseModel):
    speaker: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    words: list[Word] = Field(min_length=1)

    @field_validator("end")
    @classmethod
    def end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError(f"segment end ({v}) before start ({start})")
        return v


class Transcript(BaseModel):
    segments: list[Segment] = Field(min_length=1)
