"""Pydantic models for the structured scene script (spec 3.2)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Emotion = Literal[
    "neutral", "happy", "sad", "angry", "surprised", "worried", "excited", "annoyed"
]
Shot = Literal["wide", "medium", "closeup"]
Gender = Literal["male", "female", "unknown"]
AgeGroup = Literal["child", "teen", "adult", "elder", "unknown"]
Setting = Literal[
    "living_room",
    "kitchen",
    "bedroom",
    "office",
    "classroom",
    "outdoor_park",
    "street",
    "cafe",
    "unknown",
]


class Character(BaseModel):
    id: str = Field(min_length=1)
    speaker_ref: str = Field(min_length=1)
    role: str = Field(min_length=1)
    gender: Gender = "unknown"  # inferred from dialogue by the structure stage
    age_group: AgeGroup = "unknown"
    sprite: str | None = None  # resolved by the asset stage (M3)


class Line(BaseModel):
    character_id: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(min_length=1)
    emotion: Emotion = "neutral"
    audio_segment_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def end_after_start(self) -> "Line":
        if self.end < self.start:
            raise ValueError(f"line end ({self.end}) before start ({self.start})")
        return self


class CameraShot(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    focus_character: str = Field(min_length=1)
    shot: Shot = "medium"

    @model_validator(mode="after")
    def end_after_start(self) -> "CameraShot":
        if self.end < self.start:
            raise ValueError(f"shot end ({self.end}) before start ({self.start})")
        return self


class SceneScript(BaseModel):
    setting: Setting = "unknown"
    characters: list[Character] = Field(min_length=1)
    lines: list[Line] = Field(min_length=1)
    camera: list[CameraShot] = Field(min_length=1)

    @model_validator(mode="after")
    def references_resolve(self) -> "SceneScript":
        known_ids = {c.id for c in self.characters}
        for line in self.lines:
            if line.character_id not in known_ids:
                raise ValueError(f"line references unknown character {line.character_id!r}")
        for shot in self.camera:
            if shot.focus_character not in known_ids:
                raise ValueError(
                    f"camera references unknown character {shot.focus_character!r}"
                )
        return self
