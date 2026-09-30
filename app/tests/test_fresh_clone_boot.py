"""The app must boot on a fresh clone.

`jobs/`, `media/`, `jobs_captions/` and `assets/` are all gitignored, so a
freshly cloned repository has none of them. Starlette's StaticFiles raises
``RuntimeError: Directory does not exist`` at *import* time, which means the
server could not start at all -- the failure looks like a broken repository
rather than like missing data. The mounts now create their directories first.

This copies the code (minus every gitignored data directory) into a temporary
folder and imports the application there, so it fails exactly the way a clone
does.

The import runs in a **subprocess** on purpose. Doing it in-process would mean
mutating ``sys.path`` and deleting ``app.*`` from ``sys.modules``, and that
leaks into every later test: subsequent imports would resolve to this
throwaway copy instead of the real tree, and tests that read data files started
failing with FileNotFoundError.
"""

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Directories that are gitignored, so a clone genuinely does not have them.
DATA_DIRS = ("jobs", "jobs_captions", "media", "assets")


def _copy_code_only(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy the importable tree, deliberately leaving out the data directories."""
    skip = set(DATA_DIRS) | {"node_modules", "__pycache__", ".git", "output"}
    for item in src.iterdir():
        if item.name in skip or item.name.startswith("."):
            continue
        if item.is_dir():
            shutil.copytree(item, dst / item.name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*skip, "__pycache__", "*.pyc"))
        elif item.is_file():
            shutil.copy2(item, dst / item.name)


@pytest.fixture(scope="module")
def fresh_clone(tmp_path_factory):
    clone = tmp_path_factory.mktemp("clone")
    _copy_code_only(ROOT, clone)
    for name in DATA_DIRS:
        assert not (clone / name).exists(), f"{name} should be absent from a clone"
    return clone


def _run_in_clone(clone: pathlib.Path, snippet: str) -> subprocess.CompletedProcess:
    """Execute a snippet with the clone taking precedence on sys.path.

    The real environment is inherited. Overriding PATH with Unix paths breaks
    Winsock on Windows (WinError 10106), and TestClient opens a socket.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(clone)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=clone,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )


def test_app_imports_on_a_fresh_clone(fresh_clone):
    r = _run_in_clone(fresh_clone, "import app.api.main as m; print('OK', bool(m.app))")
    assert r.returncode == 0, f"the app does not import from a clean checkout:\n{r.stderr}"
    assert "OK True" in r.stdout


def test_missing_data_directories_are_created_at_import(fresh_clone):
    r = _run_in_clone(
        fresh_clone,
        "import app.api.main, pathlib\n"
        "root = pathlib.Path(app.api.main.ROOT)\n"
        "missing = [n for n in ('assets','jobs','media','jobs_captions') "
        "if not (root / n).is_dir()]\n"
        "print('MISSING', missing)",
    )
    assert r.returncode == 0, f"import failed:\n{r.stderr}"
    assert "MISSING []" in r.stdout, r.stdout


def test_the_landing_page_is_served_on_a_fresh_clone(fresh_clone):
    r = _run_in_clone(
        fresh_clone,
        "from fastapi.testclient import TestClient\n"
        "from app.api.main import app\n"
        "c = TestClient(app)\n"
        "resp = c.get('/')\n"
        "print('STATUS', resp.status_code)\n",
    )
    assert r.returncode == 0, f"request failed:\n{r.stderr}"
    assert "STATUS 200" in r.stdout, r.stdout
