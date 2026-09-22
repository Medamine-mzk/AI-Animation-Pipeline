"""Honest uvicorn launcher for the served AI Animation Pipeline API.

Eliminates every CLI-quoting failure mode: no --app-dir (we run with the
working directory already set to the project root), no shell interpolation,
no spaces in argv. Simply loads the FastAPI app from the served module and
re-runs uvicorn.run on 127.0.0.1:8000.
"""
import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "app.api.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="warning",
    )
