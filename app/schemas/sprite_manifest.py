"""Pydantic models for the character sprite manifest and asset library (spec 3.3)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Viseme = Literal["A", "B", "C", "D", "E", "F", "G", "H", "X"]
VISEMES: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G", "H", "X")


class Point(BaseModel):
    x: int
    y: int


class EyeRefs(BaseModel):
    open: str
    closed: str


class SpriteManifest(BaseModel):
    id: str = Field(min_length=1)
    base_body: str
    visemes: dict[Viseme, str]
    eyes: EyeRefs
    anchor_point: Point  # mouth paste anchor (spec 3.3)
    eye_anchor: Point | None = None  # eye pair paste anchor; defaults to anchor_point

    @model_validator(mode="after")
    def default_eye_anchor(self) -> "SpriteManifest":
        if self.eye_anchor is None:
            self.eye_anchor = self.anchor_point
        return self

    @model_validator(mode="after")
    def all_visemes_present(self) -> "SpriteManifest":
        missing = [v for v in VISEMES if v not in self.visemes]
        if missing:
            raise ValueError(f"sprite {self.id!r} missing visemes: {missing}")
        return self


class SpriteLibrary(BaseModel):
    sprites: list[SpriteManifest]

    def get(self, sprite_id: str) -> SpriteManifest:
        for sprite in self.sprites:
            if sprite.id == sprite_id:
                return sprite
        raise KeyError(f"sprite {sprite_id!r} not in library")


class BackgroundEntry(BaseModel):
    setting: str
    file: str


class BackgroundLibrary(BaseModel):
    backgrounds: list[BackgroundEntry]

    def get(self, setting: str) -> str:
        for entry in self.backgrounds:
            if entry.setting == setting:
                return entry.file
        raise KeyError(f"no background for setting {setting!r}")
