"""M4: transcript.json -> per-character viseme timelines via Rhubarb.

Runs Rhubarb Lip Sync (1.13.0) on each transcript segment independently, parses
the XML output (seconds-based mouthCues), offsets them into global time and
merges them into one sorted timeline per character.

Usage:
    python -m app.pipeline.lipsync jobs/golden/transcript.json jobs/golden/visemes.json
"""

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS_FFMPEG = ROOT / "tools" / "ffmpeg"
TOOLS_RHUBARB = ROOT / "tools" / "rhubarb" / "Rhubarb-Lip-Sync-1.13.0-Windows"
DEFAULT_VISEMES_OUT = ROOT / "jobs" / "golden" / "visemes.json"

VALID_SHAPES = frozenset("ABCDEFGHX")


def ensure_tools_on_path() -> None:
    os.environ["PATH"] = (
        str(TOOLS_FFMPEG)
        + os.pathsep
        + str(TOOLS_RHUBARB)
        + os.pathsep
        + os.environ.get("PATH", "")
    )


@dataclass(frozen=True)
class VisemeEvent:
    start: float
    end: float
    shape: str


def parse_rhubarb_xml(xml_text: str) -> list[VisemeEvent]:
    """Parse Rhubarb 1.13 XML export into VisemeEvents (seconds)."""
    root = ET.fromstring(xml_text)
    events = []
    for cue in root.findall(".//mouthCue"):
        start = float(cue.attrib["start"])
        end = float(cue.attrib["end"])
        shape = (cue.text or "").strip()
        if shape not in VALID_SHAPES:
            raise ValueError(f"unknown viseme shape {shape!r}")
        if end <= start:
            continue
        events.append(VisemeEvent(start=start, end=end, shape=shape))
    return events


def offset_events(events: list[VisemeEvent], delta: float) -> list[VisemeEvent]:
    return [
        VisemeEvent(start=ev.start + delta, end=ev.end + delta, shape=ev.shape)
        for ev in events
    ]


def merge_timeline(events: list[VisemeEvent]) -> list[VisemeEvent]:
    """Sort by start, drop zero/negative-length events, merge adjacent equal shapes."""
    clean = [ev for ev in events if ev.end > ev.start]
    clean.sort(key=lambda ev: (ev.start, ev.end))
    merged: list[VisemeEvent] = []
    for ev in clean:
        if not merged:
            merged.append(ev)
            continue
        prev = merged[-1]
        if prev.shape == ev.shape and ev.start <= prev.end:
            merged[-1] = VisemeEvent(start=prev.start, end=max(prev.end, ev.end), shape=prev.shape)
        elif ev.start >= prev.end:
            merged.append(ev)
        else:
            merged[-1] = VisemeEvent(start=prev.start, end=ev.start, shape=prev.shape)
            merged.append(ev)
    return merged


def cut_audio(ffmpeg_exe: Path, source_wav: Path, start: float, end: float, out_wav: Path) -> None:
    """Cut a [start, end] window from source_wav into out_wav (16 kHz mono)."""
    cmd = [
        str(ffmpeg_exe), "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
        "-i", str(source_wav), "-ar", "16000", "-ac", "1",
        str(out_wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def run_rhubarb(rhubarb_exe: Path, audio_wav: Path, dialog_text: str, out_xml: Path) -> list[VisemeEvent]:
    """Run Rhubarb on one audio segment and return its viseme events."""
    dialog_file = out_xml.with_suffix(".txt")
    dialog_file.write_text(dialog_text, encoding="utf-8")
    cmd = [
        str(rhubarb_exe), "-q", "-f", "xml",
        "-d", str(dialog_file),
        "-o", str(out_xml),
        str(audio_wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    events = parse_rhubarb_xml(out_xml.read_text(encoding="utf-8"))
    out_xml.unlink(missing_ok=True)
    dialog_file.unlink(missing_ok=True)
    return events


def build_viseme_timelines(
    transcript: dict,
    source_wav: Path,
    work_dir: Path,
    rhubarb_exe: Path,
    ffmpeg_exe: Path,
) -> dict[str, list[VisemeEvent]]:
    """Run Rhubarb per segment and return {speaker: merged global timeline}."""
    work_dir.mkdir(parents=True, exist_ok=True)
    per_speaker: dict[str, list[VisemeEvent]] = {}
    for i, seg in enumerate(transcript["segments"]):
        speaker = seg["speaker"]
        seg_wav = work_dir / f"seg_{i}.wav"
        cut_audio(ffmpeg_exe, source_wav, seg["start"], seg["end"], seg_wav)
        events = run_rhubarb(rhubarb_exe, seg_wav, seg["text"], work_dir / f"seg_{i}.xml")
        seg_wav.unlink(missing_ok=True)
        per_speaker.setdefault(speaker, []).extend(offset_events(events, seg["start"]))
    return {speaker: merge_timeline(events) for speaker, events in per_speaker.items()}


def viseme_timelines_to_json(timelines: dict[str, list[VisemeEvent]]) -> dict:
    return {
        speaker: [asdict(ev) for ev in events]
        for speaker, events in timelines.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="transcript.json -> viseme timelines")
    parser.add_argument("transcript", help="input transcript.json")
    parser.add_argument("-o", "--output", default=str(DEFAULT_VISEMES_OUT), help="output visemes.json path")
    parser.add_argument("--audio", default=str(ROOT / "media" / "golden_clip.wav"), help="source audio wav")
    parser.add_argument("--work-dir", default=str(ROOT / "jobs" / "golden" / "lipsync_work"), help="scratch dir")
    args = parser.parse_args(argv)

    ensure_tools_on_path()
    try:
        transcript = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
        if not transcript.get("segments"):
            raise ValueError(f"transcript {args.transcript} has no segments")
        print(f"[lipsync] {len(transcript['segments'])} segment(s) -> rhubarb")
        timelines = build_viseme_timelines(
            transcript,
            Path(args.audio),
            Path(args.work_dir),
            TOOLS_RHUBARB / "rhubarb.exe",
            TOOLS_FFMPEG / "ffmpeg.exe",
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(viseme_timelines_to_json(timelines), indent=2), encoding="utf-8"
        )
        print(f"[lipsync] wrote {out}")
        for speaker, events in timelines.items():
            print(f"[lipsync] {speaker}: {len(events)} viseme events")
        return 0
    except Exception as exc:
        print(f"[lipsync] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
