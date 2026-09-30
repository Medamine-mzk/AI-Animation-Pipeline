import re
import subprocess
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def extract_script(path):
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r'<script type="module">(.*?)</script>', text, re.DOTALL)
    return m.group(1) if m else ""


def extract_inline_script(path):
    """Grab the plain (non-module) <script>... </script> block(s)."""
    text = Path(path).read_text(encoding="utf-8")
    return "".join(m.group(1) for m in re.finditer(r"<script>(.*?)</script>", text, re.DOTALL))


def node_check(src):
    if not src.strip():
        return True
    with tempfile.NamedTemporaryFile(suffix=".mjs", mode="w", delete=False, encoding="utf-8") as f:
        f.write(src)
        name = f.name
    try:
        r = subprocess.run(["node", "--check", name], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError("node --check failed:\n" + r.stderr[:2000])
        return True
    finally:
        os.unlink(name)


def brace_balance(s):
    c = 0
    for ch in s:
        if ch == "{":
            c += 1
        elif ch == "}":
            c -= 1
        if c < 0:
            return c
    return c


def test_dialogue_player_module_parses():
    src = extract_script(ROOT / "dialogue-player.html")
    assert brace_balance(src) == 0, "brace imbalance"
    assert node_check(src), "module JS must pass node --check"


def test_config_html_script_parses():
    src = extract_inline_script(ROOT / "config.html")
    assert brace_balance(src) == 0, "brace imbalance in config.html script"
    assert "const mode" in src, "config.html must declare mode once"
    assert len(re.findall(r"\bconst mode\b", src)) == 1, "duplicate const mode found"
    assert node_check(src), "config.html script must pass node --check"


def test_config_html_modes_wired():
    src = extract_inline_script(ROOT / "config.html")
    for token in ["initWavUpload", "initAiMode", "render", "/api/jobs", "pollStatus"]:
        assert token in src, f"config.html missing {token}"


def test_initScene_exists():
    src = extract_script(ROOT / "dialogue-player.html")
    assert "async function initScene" in src


def test_buttons_wired():
    src = extract_script(ROOT / "dialogue-player.html")
    for btn in ["playBtn.addEventListener", "drawBoxBtn", "saveSpotsBtn", "confirmSpotsBtn"]:
        assert btn in src


def test_mergePickerLive_exists():
    src = extract_script(ROOT / "dialogue-player.html")
    assert "function mergePickerLive" in src
    assert "window.mergePickerLive" in src


def test_loadBackground_exists():
    src = extract_script(ROOT / "dialogue-player.html")
    assert "async function loadBackground" in src