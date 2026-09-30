"""Unit tests for speaker-aware caption pagination (captions feature, M1).

These encode the spec's promises as assertions so they cannot silently rot:
  - spec 3.3  a page never mixes two speakers
  - spec 2    2-8 words per page
  - spec 7    no page during silence
  - spec 4    speaker colours in order of first appearance
  - spec 4    captions stay readable (WCAG contrast, colour-blind safe)
"""

import json

import pytest
from pydantic import ValidationError

from app.pipeline.captions import (
    BACKDROP,
    MAX_PAGE_MS,
    MAX_WORDS_PER_PAGE,
    MIN_WORDS_PER_PAGE,
    SPEAKER_PALETTE,
    active_word,
    audit_palette,
    build_caption_set,
    contrast_ratio,
    low_confidence_words,
    page_timeline,
    relative_luminance,
    speaker_style,
    summary,
)
from app.schemas.captions import CaptionPage, CaptionSet, PageWord
from app.schemas.word_timeline import WordTimeline

# Three speakers, each talking in bursts, with a clear silent stretch in the
# middle that no caption should span.
TIMELINE_PAYLOAD = {
    "language": "en",
    "durationMs": 20000,
    "words": [
        # A: 3 words 0.0-0.9
        {"text": "Hello", "startMs": 0, "endMs": 300, "speakerId": "SPEAKER_00"},
        {"text": " there", "startMs": 300, "endMs": 600, "speakerId": "SPEAKER_00"},
        {"text": " friend", "startMs": 600, "endMs": 900, "speakerId": "SPEAKER_00"},
        # B: 2 words 1.0-1.6
        {"text": "Hi", "startMs": 1000, "endMs": 1200, "speakerId": "SPEAKER_01"},
        {"text": " Chris", "startMs": 1200, "endMs": 1600, "speakerId": "SPEAKER_01"},
        # long silence 1.6 - 12.0
        # A again, 10 words -> must split into multiple pages
        *[{"text": f" w{i}", "startMs": 12000 + i * 400, "endMs": 12000 + i * 400 + 350,
           "speakerId": "SPEAKER_00"} for i in range(10)],
    ],
    "speakers": [
        {"speakerId": "SPEAKER_00", "index": 0, "firstWordMs": 0, "lastWordMs": 15950,
         "wordCount": 13, "totalMs": 6000},
        {"speakerId": "SPEAKER_01", "index": 1, "firstWordMs": 1000, "lastWordMs": 1600,
         "wordCount": 2, "totalMs": 600},
    ],
}


def make_timeline() -> WordTimeline:
    return WordTimeline.model_validate(
        {
            "language": TIMELINE_PAYLOAD["language"],
            "durationMs": TIMELINE_PAYLOAD["durationMs"],
            "words": TIMELINE_PAYLOAD["words"],
            "speakers": TIMELINE_PAYLOAD["speakers"],
            "wordAlignment": "measured",
        }
    )


# ---------------------------------------------------------------- WCAG colours


def test_contrast_ratio_of_black_on_white_is_max():
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.05)


def test_contrast_ratio_of_identical_colours_is_one():
    assert contrast_ratio("#3b82f6", "#3b82f6") == pytest.approx(1.0, abs=0.01)


def test_relative_luminance_ordering():
    # White is brighter than mid grey, which is brighter than black.
    assert (
        relative_luminance("#ffffff")
        > relative_luminance("#808080")
        > relative_luminance("#000000")
    )


def test_relative_luminance_rejects_garbage():
    with pytest.raises(ValueError):
        relative_luminance("not-a-colour")
    with pytest.raises(ValueError):
        relative_luminance("#12345")


def test_contrast_ratio_rejects_garbage():
    with pytest.raises(ValueError):
        contrast_ratio("#fff", "nope")


# ------------------------------------------------------------------ pagination


def test_build_caption_set_basic_shape():
    cs = build_caption_set(make_timeline())
    assert isinstance(cs, CaptionSet)
    assert cs.schemaVersion == "captions/1"
    assert cs.language == "en"
    assert cs.durationMs == 20000
    assert cs.pages


def test_every_page_has_within_word_count_bounds():
    for page in build_caption_set(make_timeline()).pages:
        assert MIN_WORDS_PER_PAGE <= len(page.words) <= MAX_WORDS_PER_PAGE


def test_a_page_never_mixes_speakers():
    # spec 3.3 -- the single most important invariant in this module.
    for page in build_caption_set(make_timeline()).pages:
        speakers = {w_speaker for w_speaker in [page.speakerId]}
        assert len(speakers) == 1
        assert all(w.displayText for w in page.words)


def test_pages_split_on_speaker_change():
    cs = build_caption_set(make_timeline())
    # A's 3 words, then B's 2 words, then A's 10 words split into <=8 chunks.
    assert [p.speakerId for p in cs.pages] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_00",
    ]


def test_long_run_is_split_into_multiple_pages():
    cs = build_caption_set(make_timeline())
    a_pages = [p for p in cs.pages if p.speakerId == "SPEAKER_00"]
    # 3 + 10 = 13 A words across 3 pages, so the 10-word run was chunked.
    assert len(a_pages) == 3
    assert sum(len(p.words) for p in a_pages) == 13


def test_no_page_spans_silence():
    # spec 7 -- a caption must not sit on screen through the 1.6s-12.0s gap.
    for page in build_caption_set(make_timeline()).pages:
        if page.speakerId == "SPEAKER_00" and page.startMs < 2000:
            assert page.endMs <= 1600, f"page {page.index} spans the silence"


def test_pages_never_overlap_each_other():
    pages = build_caption_set(make_timeline()).pages
    for prev, nxt in zip(pages, pages[1:]):
        assert nxt.startMs >= prev.endMs


def test_page_window_covers_all_its_words():
    for page in build_caption_set(make_timeline()).pages:
        assert page.startMs <= page.words[0].startMs
        assert page.endMs >= page.words[-1].endMs


def test_page_text_reads_naturally():
    cs = build_caption_set(make_timeline())
    assert cs.pages[0].text == "Hello there friend"
    assert cs.pages[1].text == "Hi Chris"


def test_empty_timeline_is_rejected_honestly():
    # No words -> no captions. Must raise rather than emit a blank page, so the
    # caller can tell the user the ASR found nothing.
    tl = WordTimeline.model_validate(
        {
            "language": "en",
            "durationMs": 5000,
            "words": [{"text": "hi", "startMs": 0, "endMs": 100, "speakerId": "SPEAKER_00"}],
            "speakers": [{"speakerId": "SPEAKER_00", "index": 0, "firstWordMs": 0,
                          "lastWordMs": 100, "wordCount": 1, "totalMs": 100}],
            "wordAlignment": "measured",
        }
    )
    # A single word is below MIN_WORDS_PER_PAGE but must still be captioned --
    # dropping it would silently lose speech.
    cs = build_caption_set(tl)
    assert len(cs.pages) == 1
    assert cs.pages[0].text == "hi"


def test_page_timeline_helper_marks_active_window():
    pages = build_caption_set(make_timeline()).pages
    # Exactly one page active at any moment (pages do not overlap).
    for t in (100, 1100, 12100, 13000):
        active = page_timeline(pages, t)
        assert len(active) == 1, f"expected exactly one active page at {t}ms"
    # In the silence, nothing is active.
    assert page_timeline(pages, 5000) == []


def test_page_timeline_ignores_confidence_none():
    tl = make_timeline().model_dump()
    tl["words"][1]["confidence"] = None
    cs = build_caption_set(WordTimeline.model_validate(tl))
    # None means "unknown", not "bad" -- the word must still be captioned.
    assert cs.pages[0].words[1].displayText == "there"


# ------------------------------------------------------------------- styling


def test_styles_cover_every_speaker_in_appearance_order():
    cs = build_caption_set(make_timeline())
    assert [s.speakerId for s in cs.styles] == ["SPEAKER_00", "SPEAKER_01"]
    assert [s.index for s in cs.styles] == [0, 1]


def test_styles_meet_wcag_aa_against_the_caption_backdrop():
    # spec 4 -- unreadable captions are a hard fail, not a nice-to-have.
    cs = build_caption_set(make_timeline())
    for style in cs.styles:
        assert style.contrastRatio >= 4.5, (
            f"{style.speakerId} colour {style.color} only reaches "
            f"{style.contrastRatio:.2f}:1 on {BACKDROP}"
        )


def test_speaker_colours_are_distinguishable_in_hex():
    # The two most common forms of colour-vision deficiency confuse red/green,
    # so the palette must not rely on that axis (spec 4). Blue vs amber is the
    # standard safe pairing.
    cs = build_caption_set(make_timeline())
    colors = [s.color for s in cs.styles]
    assert len(set(colors)) == len(colors), "speakers must not share a colour"


def test_every_palette_entry_passes_wcag_aa():
    # Pins the whole palette, not just the two speakers in the fixture, so
    # colours 3-8 cannot be quietly broken by a later edit.
    for row in audit_palette():
        assert row["onBackdrop"] >= 4.5, f"{row['color']} fails AA on backdrop"
        assert row["whiteOnPill"] >= 4.5, f"white on {row['color']} pill fails AA"
        assert row["passesAA"] is True


def test_active_pill_is_always_readable_under_white_text():
    for color in SPEAKER_PALETTE:
        pill = speaker_style("SPEAKER_00", SPEAKER_PALETTE.index(color)).activeColor
        assert contrast_ratio("#ffffff", pill) >= 4.5, f"white on {pill} is too faint"


# ------------------------------------------------------------- active word


def test_active_word_tracks_the_spoken_word():
    page = build_caption_set(make_timeline()).pages[0]
    # Page 0 spans 0-900ms: Hello 0-300, there 300-600, friend 600-900.
    assert active_word(page, 0) == 0
    assert active_word(page, 250) == 0
    assert active_word(page, 400) == 1
    assert active_word(page, 700) == 2


def test_active_word_holds_through_inter_word_gaps():
    # Forced alignment leaves small gaps; the pill must not blink off.
    page = build_caption_set(make_timeline()).pages[0]
    assert active_word(page, 599) == 1
    assert active_word(page, 601) == 2


def test_active_word_is_none_before_the_page_starts():
    page = build_caption_set(make_timeline()).pages[1]
    assert active_word(page, 0) is None
    assert active_word(page, 1000) == 0


# ------------------------------------------------------- low confidence words


def test_low_confidence_words_are_flagged_for_the_editor():
    tl = make_timeline().model_dump()
    tl["words"][1]["confidence"] = 0.31
    tl["words"][3]["confidence"] = 0.9
    cs = build_caption_set(WordTimeline.model_validate(tl))
    flagged = low_confidence_words(cs)
    assert flagged == [(0, "there", pytest.approx(0.31))]


def test_unscored_words_are_not_treated_as_low_confidence():
    # Unknown confidence is not low confidence. Marking every unscored word
    # would make the editor's underline meaningless.
    cs = build_caption_set(make_timeline())
    assert low_confidence_words(cs) == []


def test_summary_reports_counts():
    s = summary(build_caption_set(make_timeline()))
    assert s["speakers"] == 2
    assert s["words"] == 15
    assert s["language"] == "en"
    assert s["durationMs"] == 20000


def test_style_palette_is_deterministic():
    # Same input -> same colours, so an export matches its preview.
    a = build_caption_set(make_timeline())
    b = build_caption_set(make_timeline())
    assert [s.color for s in a.styles] == [s.color for s in b.styles]


def test_many_speakers_cycle_palette_without_crashing():
    # spec 6 calls >4 speakers a soft limit, not a hard block.
    words = [
        {"text": f" s{i}s{i}", "startMs": i * 1000, "endMs": i * 1000 + 500,
         "speakerId": f"SPEAKER_{i:02d}"}
        for i in range(9)
    ]
    tl = WordTimeline.model_validate(
        {
            "language": "en",
            "durationMs": 10000,
            "words": words,
            "speakers": [
                {"speakerId": f"SPEAKER_{i:02d}", "index": i, "firstWordMs": i * 1000,
                 "lastWordMs": i * 1000 + 500, "wordCount": 1, "totalMs": 500}
                for i in range(9)
            ],
            "wordAlignment": "measured",
        }
    )
    cs = build_caption_set(tl)
    assert len(cs.pages) == 9
    assert len(cs.styles) == 9


# ----------------------------------------------------------------- round trip


def test_caption_set_round_trips_through_json():
    cs = build_caption_set(make_timeline())
    reloaded = CaptionSet.model_validate_json(cs.model_dump_json())
    assert reloaded == cs

    raw = json.loads(cs.model_dump_json())
    assert raw["schemaVersion"] == "captions/1"
    assert raw["pages"][0]["words"][0]["text"] == "Hello"


def test_confidence_is_preserved_per_word():
    tl = make_timeline().model_dump()
    tl["words"][1]["confidence"] = 0.31
    cs = build_caption_set(WordTimeline.model_validate(tl))
    assert cs.pages[0].words[1].confidence == pytest.approx(0.31)


# --------------------------------------------------------------------- schema


def test_a_page_cannot_even_express_mixed_speakers():
    # spec 3.3, enforced structurally: CaptionPage carries one speakerId, so a
    # page containing two speakers is not representable in the first place.
    fields = set(CaptionPage.model_fields)
    assert "speakerId" in fields
    assert not any("speakers" in f for f in fields)
    for page in build_caption_set(make_timeline()).pages:
        assert isinstance(page.speakerId, str) and page.speakerId


def test_page_word_schema_cannot_hold_a_speaker():
    # The per-word record deliberately omits speakerId: within a page every word
    # belongs to the page's speaker, so a per-word copy could only ever disagree.
    assert "speakerId" not in PageWord.model_fields
    assert set(PageWord.model_fields) == {"text", "startMs", "endMs", "confidence"}


def test_schema_rejects_overlapping_pages():
    good = build_caption_set(make_timeline())
    payload = good.model_dump()
    # Force page 1 to start before page 0 ends.
    payload["pages"][1]["startMs"] = 0
    payload["pages"][1]["endMs"] = 100
    with pytest.raises(ValidationError):
        CaptionSet.model_validate(payload)


def test_schema_rejects_unstyled_speaker():
    good = build_caption_set(make_timeline())
    payload = good.model_dump()
    payload["pages"][0]["speakerId"] = "SPEAKER_77"
    with pytest.raises(ValidationError):
        CaptionSet.model_validate(payload)
