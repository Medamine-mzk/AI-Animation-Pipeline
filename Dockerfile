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

# Optional: pre-download the models at build time instead of on the first job.
# Worth it on hosts with an ephemeral filesystem, where the ~780 MB would
# otherwise be re-downloaded after every restart.
#
# The diarization model is deliberately NOT baked: pyannote's repository is
# gated, so it needs a token at runtime anyway. Baking it would only half-solve
# the problem while making the build fail for anyone without a token.
#   docker build --build-arg BAKE_MODELS=1 -t aipipeline .
ARG BAKE_MODELS=0
RUN if [ "$BAKE_MODELS" = "1" ]; then \
        python -c "\
from faster_whisper import WhisperModel; \
WhisperModel('small', device='cpu', compute_type='int8'); \
import whisperx; whisperx.load_align_model(language_code='en', device='cpu'); \
print('models baked')"; \
    fi

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app /models
USER appuser

VOLUME ["/models"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT:-8000}/api/health || exit 1

# Serve, not "--help": the previous CMD printed usage text and exited, so the
# image never actually ran the application.
#
# Shell form on purpose: exec form has no shell to expand ${PORT}, and every
# PaaS (Render, Railway, Fly, Cloud Run) injects PORT and expects a bind on
# 0.0.0.0. Hardcoding 8000 deployed an image that answered nothing.
CMD python -m uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT:-8000}