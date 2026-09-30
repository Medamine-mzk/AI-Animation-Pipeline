"""M5: compositing — frame sequence + original audio -> output.mp4.

Usage:
    python -m app.pipeline.render jobs/golden/preview_frames -o jobs/golden/preview.mp4 --fps 15 --audio media/golden_clip.wav --seconds 20
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"


def render_video(
    frames_dir: Path,
    output_path: Path,
    fps: int,
    audio_wav: Path | None = None,
    start: float = 0.0,
    seconds: float | None = None,
) -> Path:
    if not frames_dir.is_dir():
        raise FileNotFoundError(f"frames dir {frames_dir} not found")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / "frame_%06d.jpg")

    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-framerate", str(fps),
        "-i", pattern,
    ]
    if audio_wav is not None:
        cmd += ["-i", str(audio_wav)]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
    ]
    if audio_wav is not None:
        if start > 0:
            cmd += ["-af", f"adelay={int(start * 1000)}:all=1"]
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(output_path))
    subprocess.run(cmd, check=True)
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="frames + audio -> mp4")
    parser.add_argument("frames", help="input frames dir (frame_%06d.jpg)")
    parser.add_argument("-o", "--output", default=str(ROOT / "jobs" / "golden" / "output.mp4"))
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--audio", default=str(ROOT / "media" / "golden_clip.wav"))
    parser.add_argument("--start", type=float, default=0.0, help="delay audio by N seconds (intro card time)")
    parser.add_argument("--seconds", type=float, default=None, help="clip length")
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args(argv)

    try:
        audio = None if args.no_audio else Path(args.audio)
        out = render_video(
            Path(args.frames),
            Path(args.output),
            args.fps,
            audio_wav=audio,
            start=args.start,
            seconds=args.seconds,
        )
        print(f"[render] wrote {out}")
        return 0
    except Exception as exc:
        print(f"[render] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
