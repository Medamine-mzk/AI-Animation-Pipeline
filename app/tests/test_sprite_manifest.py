"""Unit tests for the sprite manifest schema (spec 3.3)."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.sprite_manifest import SpriteLibrary, SpriteManifest

SPEC_SAMPLE = {
    "id": "dad_v1",
    "base_body": "dad_v1_body.png",
    "visemes": {
        "A": "dad_v1_mouth_A.png",
        "B": "dad_v1_mouth_B.png",
        "C": "dad_v1_mouth_C.png",
        "D": "dad_v1_mouth_D.png",
        "E": "dad_v1_mouth_E.png",
        "F": "dad_v1_mouth_F.png",
        "G": "dad_v1_mouth_G.png",
        "H": "dad_v1_mouth_H.png",
        "X": "dad_v1_mouth_X.png",
    },
    "eyes": {"open": "dad_v1_eyes_open.png", "closed": "dad_v1_eyes_closed.png"},
    "anchor_point": {"x": 512, "y": 300},
}


def test_parses_spec_sample():
    manifest = SpriteManifest.model_validate(SPEC_SAMPLE)
    assert manifest.id == "dad_v1"
    assert manifest.visemes["A"] == "dad_v1_mouth_A.png"
    assert manifest.eyes.open.endswith("open.png")


def test_round_trip_json():
    manifest = SpriteManifest.model_validate(SPEC_SAMPLE)
    reparsed = SpriteManifest.model_validate_json(manifest.model_dump_json())
    assert reparsed == manifest


def test_eye_anchor_defaults_to_anchor_point():
    manifest = SpriteManifest.model_validate(SPEC_SAMPLE)
    assert manifest.eye_anchor == manifest.anchor_point


def test_rejects_missing_viseme():
    data = deepcopy(SPEC_SAMPLE)
    del data["visemes"]["B"]
    with pytest.raises(ValidationError):
        SpriteManifest.model_validate(data)


def test_rejects_unknown_viseme_key():
    data = deepcopy(SPEC_SAMPLE)
    data["visemes"]["Q"] = "dad_v1_mouth_Q.png"
    with pytest.raises(ValidationError):
        SpriteManifest.model_validate(data)


def test_rejects_missing_eye_state():
    data = deepcopy(SPEC_SAMPLE)
    del data["eyes"]["closed"]
    with pytest.raises(ValidationError):
        SpriteManifest.model_validate(data)


def test_library_get_and_missing():
    lib = SpriteLibrary.model_validate(
        {"sprites": [SPEC_SAMPLE, {**deepcopy(SPEC_SAMPLE), "id": "mom_v1"}]}
    )
    assert lib.get("mom_v1").id == "mom_v1"
    with pytest.raises(KeyError):
        lib.get("ghost_v1")
