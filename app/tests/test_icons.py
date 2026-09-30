"""Tests for the icons stage (transcript words -> word-icon timeline) and the
word-icon animation overlay in the frame renderer."""

import json

import pytest
from PIL import Image
from pathlib import Path

from app.pipeline.animate import SceneRenderer
from app.pipeline.icons import build_icons, build_icons_file, merged_map, normalize

_envelope = SceneRenderer._icon_envelope


def _transcript() -> dict:
    return {
        "segments": [
            {
                "speaker": "SPEAKER_01",
                "start": 1.0,
                "end": 4.0,
                "text": "Mom, I received a letter from Chris.",
                "words": [
                    {"word": "Mom,", "start": 1.0, "end": 1.4},
                    {"word": "I", "start": 1.4, "end": 1.6},
                    {"word": "received", "start": 1.6, "end": 2.0},
                    {"word": "a", "start": 2.0, "end": 2.1},
                    {"word": "letter", "start": 2.1, "end": 2.6},
                    {"word": "from", "start": 2.6, "end": 2.8},
                    {"word": "Chris.", "start": 2.8, "end": 3.2},
                ],
            },
            {
                "speaker": "SPEAKER_01",
                "start": 4.0,
                "end": 6.0,
                "text": "I love London in summer.",
                "words": [
                    {"word": "I", "start": 4.0, "end": 4.2},
                    {"word": "love", "start": 4.2, "end": 4.6},
                    {"word": "London", "start": 4.6, "end": 5.2},
                    {"word": "in", "start": 5.2, "end": 5.4},
                    {"word": "summer.", "start": 5.4, "end": 6.0},
                ],
            },
        ]
    }


_DICT = {
    "mom": "👩",
    "received": "📥",
    "letter": "✉️",
    "summer": "☀️",
    "love": "❤️",
    "can": "🫴",
}
_CLIP = {
    "chris": "🧒",
    "london": "🇬🇧",
    "mom": "👩‍👧",
}


def test_normalize_strips_punctuation_and_apostrophes():
    assert normalize("Mom,") == "mom"
    assert normalize("Chris.") == "chris"
    assert normalize("summer.") == "summer"
    assert normalize("can't") == "cant"


def test_merged_map_clip_overrides_dictionary():
    merged = merged_map(_DICT, _CLIP)
    assert merged["chris"] == "🧒"
    assert merged["mom"] == "👩‍👧"
    assert merged["letter"] == "✉️"


def test_build_icons_maps_words_with_clip_map():
    icons, missing = build_icons(_transcript(), _DICT, _CLIP)
    by_word = {e["word"]: e for e in icons}
    assert by_word["letter"]["icon"] == "✉️"
    assert by_word["chris"]["icon"] == "🧒"
    assert by_word["summer"]["start"] == 5.4
    assert set(by_word) == {"mom", "received", "letter", "chris", "london", "summer", "love"}


def test_build_icons_chains_to_next_icon_start():
    icons, _ = build_icons(_transcript(), _DICT, _CLIP)
    by_word = {e["word"]: e for e in icons}
    # letter spoken [2.1, 2.6] holds to MIN_HOLD (2.8) which meets chris's start
    assert by_word["letter"]["end"] > 2.6
    assert by_word["letter"]["end"] <= 2.8
    # last icon (summer) has no next -> holds MIN_HOLD past spoken end
    assert by_word["summer"]["end"] == pytest.approx(5.4 + 0.7, abs=0.001)


def test_build_icons_caps_chain_length():
    # long silence between two icons must not make the first swallow the gap
    transcript = {
        "segments": [
            {
                "speaker": "S",
                "start": 0.0,
                "end": 20.0,
                "text": "Mom. Summer.",
                "words": [
                    {"word": "Mom.", "start": 0.0, "end": 0.4},
                    {"word": "Summer.", "start": 19.0, "end": 19.4},
                ],
            }
        ]
    }
    icons, _ = build_icons(transcript, _DICT, _CLIP)
    by_word = {e["word"]: e for e in icons}
    assert by_word["mom"]["end"] <= by_word["mom"]["start"] + 4.0


def test_build_icons_reports_missing_content_words():
    transcript = {
        "segments": [
            {
                "speaker": "S",
                "start": 0.0,
                "end": 2.0,
                "text": "I love huge zebras here.",
                "words": [
                    {"word": "I", "start": 0.0, "end": 0.2},
                    {"word": "love", "start": 0.2, "end": 0.4},
                    {"word": "huge", "start": 0.4, "end": 0.6},
                    {"word": "zebras", "start": 0.6, "end": 0.8},
                    {"word": "here.", "start": 0.8, "end": 1.0},
                ],
            }
        ]
    }
    icons, missing = build_icons(transcript, _DICT, _CLIP)
    assert [e["word"] for e in icons] == ["love"]
    assert "huge" in missing
    assert "zebras" in missing
    assert "here" not in missing  # stopword


def test_build_icons_sorted_by_start():
    icons, _ = build_icons(_transcript(), _DICT, _CLIP)
    starts = [e["start"] for e in icons]
    assert starts == sorted(starts)


def test_build_icons_file_writes_output(tmp_path):
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps(_transcript()), encoding="utf-8")
    out = tmp_path / "icons.json"
    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps(_CLIP), encoding="utf-8")
    icons = build_icons_file(transcript, output_path=out, dictionary_path=None, clip_map_path=map_file)
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8")) == icons


def _renderer(icons: list[dict], icon_dir) -> SceneRenderer:
    icon_dir = Path(icon_dir)
    bg = icon_dir / "_bg.png"
    Image.new("RGB", (1920, 1080), (0, 0, 0)).save(bg)
    return SceneRenderer(
        assets={
            "background": str(bg),
            "characters": [],
        },
        visemes={},
        shots=[{"start": 0.0, "shot": "wide", "focus_character": "c0"}],
        duration=10.0,
        icons=icons,
        icon_dir=icon_dir,
    )


def test_active_icon_picks_longest_on_overlap():
    r = _renderer([], "")
    r.icons = [
        {"word": "mom", "icon": "👩", "start": 1.0, "end": 1.4},
        {"word": "letter", "icon": "✉️", "start": 1.1, "end": 2.6},
    ]
    assert r.active_icon(1.2)["word"] == "letter"


def test_active_icon_none_outside_events():
    r = _renderer([{"word": "mom", "icon": "👩", "start": 1.0, "end": 1.4}], "")
    assert r.active_icon(0.5) is None
    assert r.active_icon(1.5) is None


def test_icon_envelope_animates_pop_hold_fade():
    ev = {"start": 2.0, "end": 2.6}
    assert _envelope(1.5, ev) == (0.0, 0.3)
    alpha, scale = _envelope(2.05, ev)
    assert 0.0 < alpha < 1.0 and 0.3 < scale < 1.0
    assert _envelope(2.2, ev) == (1.0, 1.0)
    alpha, scale = _envelope(2.5, ev)
    assert 0.0 < alpha < 1.0 and scale < 1.0


def test_icon_envelope_short_word_reaches_full_midpoint():
    ev = {"start": 2.0, "end": 2.2}
    alpha, _ = _envelope(2.1, ev)
    assert alpha == 1.0


def test_load_icon_resizes_to_icon_height(tmp_path):
    img = Image.new("RGBA", (100, 300), (0, 0, 0, 255))
    img.save(tmp_path / "letter.png")
    r = _renderer([], tmp_path)
    loaded = r._load_icon("letter")
    assert loaded is not None
    assert loaded.height == 200


def test_load_icon_none_when_missing(tmp_path):
    r = _renderer([], tmp_path)
    assert r._load_icon("does_not_exist") is None


def test_load_icon_prefers_override_folder(tmp_path):
    emoji = tmp_path / "emoji"
    over = tmp_path / "override"
    emoji.mkdir(); over.mkdir()
    Image.new("RGB", (1920, 1080), (0, 0, 0)).save(emoji / "_bg.png")
    Image.new("RGBA", (50, 50), (0, 0, 0, 255)).save(emoji / "letter.png")
    Image.new("RGBA", (80, 80), (255, 0, 0, 255)).save(over / "letter.png")
    r = SceneRenderer(
        assets={"background": str(emoji / "_bg.png"), "characters": []},
        visemes={}, shots=[{"start": 0.0, "shot": "wide", "focus_character": "c0"}],
        duration=10.0, icons=[], icon_dir=emoji, override_dir=over,
    )
    loaded = r._load_icon("letter")
    assert loaded is not None
    assert loaded.getpixel((0, 0))[0] == 255


def test_paste_icon_missing_asset_does_not_raise(tmp_path):
    r = _renderer([{"word": "ghost", "icon": "👻", "start": 1.0, "end": 2.0}], tmp_path)
    frame = Image.new("RGB", (1920, 1080), (255, 255, 255))
    r._paste_icon(frame, 1.5)


def test_paste_icon_puts_pixels_on_frame(tmp_path):
    img = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    img.save(tmp_path / "letter.png")
    r = _renderer([{"word": "letter", "icon": "✉️", "start": 1.0, "end": 2.0}], tmp_path)
    frame = Image.new("RGB", (1920, 1080), (255, 255, 255))
    r._paste_icon(frame, 1.5)
    crop = frame.crop((0, 950 - 100, 1920, 950 + 100))
    assert crop.getbbox() is not None


def test_render_with_icons_flag(tmp_path):
    img = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    img.save(tmp_path / "letter.png")
    r = _renderer([{"word": "letter", "icon": "✉️", "start": 0.0, "end": 2.0}], tmp_path)
    frame = r.render(1.0)
    assert frame.size == (1920, 1080)
    assert frame.getbbox() is not None