FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r /app/requirements.txt

ARG RHUBARB_VERSION=1.13.0
RUN curl -fsSL -o /tmp/rhubarb.zip \
      "https://github.com/DanielSWolf/rhubarb-lip-sync/releases/download/v${RHUBARB_VERSION}/Rhubarb-Lip-Sync-${RHUBARB_VERSION}-Linux.zip" \
    && unzip /tmp/rhubarb.zip -d /opt/rhubarb && rm /tmp/rhubarb.zip \
    && ln -s /opt/rhubarb/Rhubarb-Lip-Sync-${RHUBARB_VERSION}-Linux/rhubarb /usr/local/bin/rhubarb

WORKDIR /app
COPY app /app/app
COPY .env /app/.env

CMD ["python", "-m", "app.pipeline.transcribe", "--help"]
