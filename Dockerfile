# syntax=docker/dockerfile:1
#
# Image for the captions pipeline (and the FastAPI server that serves the UI).
#
# Two things this deliberately does NOT do:
#   * bake .env into the image -- a token belongs in a runtime env var or a
#     secret, not in a layer anyone can pull
#   * download the Hugging Face models at build time -- the diarization model
#     is gated, so it cannot be fetched without a token, and the ~2 GB would
#     bloat every rebuild. They land in a mounted volume on first run instead.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    TOKENIZERS_PARALLELISM=false

# ffmpeg is a runtime dependency of the pipeline, so it belongs in the image
# rather than in the fetch script that runs on the host.
RUN apt-get update && apt-get install -y --no-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY requirements.txt .
# torch comes from the CPU wheel index; requirements.txt pins the same version.
RUN pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# The app serves index.html / captions.html / config.html straight off the
# filesystem, so the UI has to be in the image or every page 404s.
COPY app       ./app
COPY tools     ./tools
COPY *.html    ./
COPY requirements.txt ./

# Directories the app mounts. Created at build time so a read-only-ish run does
# not need to write them, and mounted at runtime so uploads survive a restart.
RUN mkdir -p assets jobs jobs_captions media /models

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app /models
USER appuser

VOLUME ["/models"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/caption-jobs || exit 1

# Serve, not "--help": the previous CMD printed usage text and exited, so the
# image never actually ran the application.
CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]