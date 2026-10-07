"""Guard rails for the landing page.

The page is bilingual by convention, not by compiler: every translatable string
carries ``data-fr`` *and* ``data-en`` on the same element, and CSS hides whichever
set does not match ``html[data-lang]``. Nothing enforces that pairing, so a string
added with only one attribute is invisible in the other language -- it renders
correctly in development, in one language, and nobody notices.

These tests are cheap and catch exactly that class of mistake, plus the factual
claims the page makes, which must not drift away from DEPLOYMENT.md.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LANDING = ROOT / "landing" / "index.html"


def _source() -> str:
    assert LANDING.exists(), f"missing landing page at {LANDING}"
    return LANDING.read_text(encoding="utf-8")


def test_translatable_attributes_are_paired():
    """Every data-fr has a data-en twin, and the counts match."""
    src = _source()
    fr = len(re.findall(r"data-fr", src))
    en = len(re.findall(r"data-en", src))
    assert fr == en, (
        f"unpaired translations: {fr} data-fr vs {en} data-en. "
        "A string with only one attribute is invisible in the other language."
    )


def test_both_languages_are_actually_written():
    """The page is bilingual, so both sets must carry real prose.

    Most strings use ``data-fr`` as a boolean attribute with the translation as the
    element's text content, so both that form and the ``data-fr="..."`` form count.
    """
    src = _source()
    for attr in ("data-fr", "data-en"):
        as_content = re.findall(attr + r"[^>]*>([^<]{12,})<", src)
        as_value = re.findall(attr + r'="([^"]{12,})"', src)
        assert len(as_content) + len(as_value) >= 10, (
            f"{attr} has too few real strings ({len(as_content) + len(as_value)})"
        )


def test_language_is_applied_before_first_paint():
    """The inline <head> script must set data-lang before the body renders.

    Otherwise the page paints French, then swaps to English a moment later.
    """
    head = _source().split("</head>")[0]
    assert "data-lang" in head, "no inline language script in <head>"
    assert "localStorage" in head or "navigator.language" in head, (
        "the <head> script must pick a language, not just leave the attribute unset"
    )


def test_instructions_survive_without_javascript():
    """The reveal is a <details>, not a JS-only toggle, so it opens without JS."""
    src = _source()
    assert "<details" in src and "<summary" in src, "reveal must use <details>/<summary>"
    assert "<noscript>" in src, "no <noscript> fallback for the Docker steps"


def test_the_contact_address_is_reachable():
    """The jury must be able to ask for the Docker file."""
    src = _source()
    assert "mailto:med.amine.mzk@gmail.com" in src
    assert "med.amine.mzk@gmail.com" in src


def test_memory_figures_match_the_deployment_doc():
    """The page quotes 512 MB / 879 MB / 1.76 GB. Those must still be true.

    These numbers are the whole argument of the page. If the models grow or the
    measured peak changes, this fails so the page gets updated in the same commit
    rather than quietly going stale.
    """
    src = _source()
    deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for figure in ("512", "879", "1,76"):
        assert figure in src, f"landing page no longer states {figure}"
    assert "512 Mo" in deployment
    assert "879" in deployment
    assert "1,76" in deployment


def test_the_page_does_not_overclaim():
    """It must not assert Render is impossible, only that the free tier fails.

    Render's $25/month Standard plan gives 2 GB against a 1.76 GB peak: borderline,
    not impossible. A flat "cannot be deployed on Render" could be disproved by
    checking the pricing table, which would cost the page its credibility.
    """
    src = _source().lower()
    assert "impossible" not in src, "the page must not claim Render is impossible"
    # The precise hedge has to survive in both languages.
    assert "pourrait" in src and "might" in src, "the $25 tier must be hedged as 'might work'"
