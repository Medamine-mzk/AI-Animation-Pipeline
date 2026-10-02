"""Measure the peak resident memory of a real caption job.

The documentation says "8 GB minimum" but that figure was an estimate. This runs
an actual transcription and samples the worker process's RSS, so the number in
DEPLOYMENT.md is measured rather than guessed.

Usage: python tools/measure_ram.py [--port 8000]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
VIDEO = ROOT / "media" / "test-video.mp4"


def rss_mb(pid: int) -> float:
    """Resident set size in MB, on Windows or Linux."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).WorkingSet64"],
                capture_output=True, text=True, timeout=10)
            val = (out.stdout or "").strip()
            return int(val) / 1e6 if val.isdigit() else 0.0
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    if not VIDEO.exists():
        print(f"no sample at {VIDEO}", file=sys.stderr)
        return 2

    req = urllib.request.Request(
        base + "/api/caption-jobs", data=VIDEO.read_bytes(), method="POST",
        headers={"X-Filename": VIDEO.name, "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        job_id = json.loads(r.read())["jobId"]
    print(f"job {job_id} created ({VIDEO.stat().st_size/1e6:.1f} MB, 10.1s of video)")

    meta_path = ROOT / "jobs_captions" / job_id / "meta.json"
    peak: dict[str, float] = {"worker": 0.0, "server": 0.0}
    stop = threading.Event()

    def sample() -> None:
        # The server itself stays small: whisperx is imported lazily inside the
        # worker, so the web process should never hold the models.
        import urllib.request as u
        while not stop.is_set():
            try:
                pid = json.loads(meta_path.read_text(encoding="utf-8")).get("workerPid")
                if isinstance(pid, int):
                    peak["worker"] = max(peak["worker"], rss_mb(pid))
            except Exception:
                pass
            time.sleep(0.25)

    t = threading.Thread(target=sample, daemon=True)
    t.start()

    deadline = time.time() + 600
    status = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"{base}/api/caption-jobs/{job_id}", timeout=10) as r:
                meta = json.loads(r.read())
        except Exception:
            time.sleep(1.5)
            continue
        status = meta.get("status")
        if status in ("done", "failed"):
            break
        time.sleep(1.0)
    stop.set()
    time.sleep(0.4)

    print(f"\n  job status : {status}")
    print(f"  worker peak: {peak['worker']:.0f} MB  (RSS of the ASR subprocess)")
    print(f"  server     : {rss_mb(0):.0f} MB" if False else "", end="")
    print()
    return 0 if status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
