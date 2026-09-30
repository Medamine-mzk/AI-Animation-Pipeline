"""Captions feature HTTP surface (new page + job API).

Kept in its own module so the existing 3D animation API in ``app/api/main.py``
needs exactly one line to adopt it::

    from app.api.captions import router as captions_router
    app.include_router(captions_router)

Nothing here reads or writes the 3D feature's job directory (``jobs/{id}/``).
Caption jobs live in ``jobs/cap_{id}/`` so they can never collide with, or be
listed alongside, an audio-to-3D job.

Routes
------
``GET  /captions.html``      the renderer page
``/captions-demo/*``         committed demo fixture, so the page is openable
                             before any upload flow exists
``POST /api/caption-jobs``   (M4) video or direct-media URL -> caption job
``GET  /api/caption-jobs/{id}``  (M4) status + artifacts
"""

from __future__ import annotations

import pathlib

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEMO_DIR = ROOT / "captions_demo"
PAGE = ROOT / "captions.html"

router = APIRouter()


@router.get("/captions.html", include_in_schema=False)
def captions_page() -> FileResponse:
    """The caption renderer.

    Served as an explicit route rather than a root static mount so the server
    never exposes the whole repository, matching how the other pages are served.
    """
    if not PAGE.exists():
        # Surfaced as a plain 404 by FastAPI rather than a confusing 500.
        raise FileNotFoundError("captions.html is missing from the repository")
    return FileResponse(str(PAGE))


# The demo fixture is committed, unlike jobs/ (gitignored), so a fresh checkout
# can open the page and see captions before running any ASR.
if DEMO_DIR.exists():
    router.mount(
        "/captions-demo", StaticFiles(directory=str(DEMO_DIR)), name="captions-demo"
    )
