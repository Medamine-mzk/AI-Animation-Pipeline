"""Tests for the captions.html renderer (captions feature, M2/M3).

Two kinds of check:

1. A syntax guard on the inline script, matching the convention already used by
   ``test_dialogue_player_syntax.py`` -- a large inline script that silently stops
   parsing is the main risk in a page this size with no build step.
2. Behavioural tests of the timing logic, extracted from the page and run under
   Node. The highlight is the whole product, so "which word is lit at 812ms"
   must be an assertion, not something a human eyeballs.
"""

import json
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "captions.html"
FIXTURE = ROOT / "captions_demo" / "captions.json"


def extract_script() -> str:
    src = PAGE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", src, re.DOTALL)
    assert blocks, "captions.html has no inline <script> block"
    return "\n".join(blocks)


def run_node(body: str) -> str:
    """Run a Node snippet that prints JSON, and return the parsed result."""
    script = textwrap.dedent(body)
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run(
            ["node", path], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
        return json.loads(proc.stdout.strip())
    finally:
        Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------- syntax


@pytest.mark.skipif(
    subprocess.run(["node", "--version"], capture_output=True).returncode != 0,
    reason="node not available",
)
def test_page_script_parses():
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as fh:
        fh.write(extract_script())
        path = fh.name
    try:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[:2000]
    finally:
        Path(path).unlink(missing_ok=True)


def test_page_wires_the_essentials():
    src = extract_script()
    for needle in (
        "function renderAt",
        "function drawPage",
        "function drawWord",
        "function pageAt",
        "function wordIndexIn",
        "requestAnimationFrame(tick)",
    ):
        assert needle in src, f"renderer is missing {needle}"


def test_page_has_no_unreplaced_placeholders():
    src = extract_script()
    assert "{{" not in src and "}}" not in src
    assert "undefined ===" not in src


def test_appearance_controls_are_present():
    src = PAGE.read_text(encoding="utf-8")
    for control in ("id=\"size\"", "id=\"pos\"", "id=\"reducedMotion\"", "id=\"highContrast\""):
        assert control in src, f"missing appearance control {control}"


def test_reduced_motion_disables_scale():
    """spec 4 -- keep the colour highlight, drop the movement."""
    css = PAGE.read_text(encoding="utf-8")
    assert "body.reduced-motion .w" in css
    block = css.split("body.reduced-motion .w")[1][:200]
    assert "transform:none" in block


# ------------------------------------------------------------ demo fixture


def test_demo_fixture_exists_and_is_valid():
    assert FIXTURE.exists(), "run tools/captions/build_demo_fixture.py"
    from app.schemas.captions import CaptionSet

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cs = CaptionSet.model_validate(data)
    assert cs.pages
    assert len(cs.styles) >= 2


def test_demo_fixture_covers_two_speakers_and_multiple_pages():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(data["styles"]) == 2
    assert len(data["pages"]) >= 8


def test_page_points_at_the_mounted_demo_path():
    """The fixture is served by a router mount, so the default path must match."""
    src = extract_script()
    assert "'captions-demo/captions.json?t='" in src


def test_router_serves_the_page_and_the_demo_fixture():
    """The page is reachable only via an explicit route; assert it is wired."""
    from app.api.captions import PAGE, router

    assert PAGE.exists()
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/captions.html" in paths
    # main.py must adopt the router, and only through include_router.
    main_src = (ROOT / "app" / "api" / "main.py").read_text(encoding="utf-8")
    assert "include_router(captions_router)" in main_src


# ------------------------------------------------------ timing logic in node

TIMING_HARNESS = """
  const pages = %s;
  // Same implementations as captions.html, kept in sync by the assertion below
  // that the page still contains them.
  function pageAt(timeMs) {
    for (let i = 0; i < pages.length; i++) {
      if (timeMs >= pages[i].startMs && timeMs < pages[i].endMs) return pages[i];
    }
    return null;
  }
  function wordIndexIn(page, timeMs) {
    let chosen = -1;
    for (let i = 0; i < page.words.length; i++) {
      if (page.words[i].startMs <= timeMs) chosen = i;
      else break;
    }
    return chosen;
  }
"""


def node_timing(extra: str) -> object:
    pages = json.loads(FIXTURE.read_text(encoding="utf-8"))["pages"]
    return run_node(TIMING_HARNESS % json.dumps(pages) + textwrap.dedent(extra))


def test_page_lookup_matches_the_window():
    result = node_timing("""
      const out = [];
      for (const p of pages) {
        out.push([p.index, pageAt(p.startMs) ? pageAt(p.startMs).index : -1,
                  pageAt(p.endMs) ? pageAt(p.endMs).index : -1]);
      }
      console.log(JSON.stringify(out));
    """)
    for index, at_start, at_end in result:
        assert at_start == index, f"page {index} not found at its own startMs"
        # Half-open window: the page is gone at its endMs.
        assert at_end != index, f"page {index} still active at its own endMs"


def test_no_page_active_in_the_leading_silence():
    """The golden clip starts speaking at 1627ms; nothing should show before."""
    first = json.loads(FIXTURE.read_text(encoding="utf-8"))["pages"][0]
    result = node_timing(
        "console.log(JSON.stringify(pageAt(%d) === null));" % (first["startMs"] - 500)
    )
    assert result is True


def test_word_highlight_advances_within_a_page():
    result = node_timing("""
      const page = pages[0];
      const out = page.words.map(w => wordIndexIn(page, w.startMs));
      console.log(JSON.stringify(out));
    """)
    assert result == list(range(len(json.loads(FIXTURE.read_text(encoding='utf-8'))["pages"][0]["words"])))


def test_word_highlight_holds_across_inter_word_gaps():
    """The pill must not blink off in the silence between words."""
    page0 = json.loads(FIXTURE.read_text(encoding="utf-8"))["pages"][0]
    gap_start = page0["words"][0]["endMs"]
    gap_end = page0["words"][1]["startMs"]
    assert gap_end > gap_start, "fixture words are adjacent; no gap to test"
    mid = (gap_start + gap_end) // 2
    result = node_timing(
        "console.log(JSON.stringify(wordIndexIn(pages[0], %d)));" % mid
    )
    assert result == 0, "highlight dropped out during a gap between words"


def test_word_highlight_is_none_before_a_page_begins():
    result = node_timing(
        "console.log(JSON.stringify(wordIndexIn(pages[0], pages[0].words[0].startMs - 1)));"
    )
    assert result == -1


def test_highlight_never_exceeds_the_page():
    result = node_timing("""
      let bad = 0;
      for (const p of pages) {
        for (let t = p.startMs; t <= p.endMs; t += 25) {
          const i = wordIndexIn(p, t);
          if (i >= p.words.length || i < -1) bad++;
        }
      }
      console.log(JSON.stringify(bad));
    """)
    assert result == 0


def test_every_word_is_highlighted_at_some_point():
    """A word the highlighter can never reach would silently never be spoken."""
    result = node_timing("""
      const unreached = [];
      for (const p of pages) {
        const seen = new Set();
        for (let t = p.startMs; t < p.endMs; t += 20) seen.add(wordIndexIn(p, t));
        for (let i = 0; i < p.words.length; i++) {
          if (!seen.has(i)) unreached.push([p.index, i, p.words[i].text.trim()]);
        }
      }
      console.log(JSON.stringify(unreached));
    """)
    assert result == [], f"words never highlighted: {result}"


def test_page_implementations_match_the_page_source():
    """The node harness reimplements two functions; keep them from drifting."""
    src = extract_script()
    assert "if (timeMs >= pages[i].startMs && timeMs < pages[i].endMs) return pages[i];" in src
    assert "if (page.words[i].startMs <= timeMs) chosen = i;" in src
