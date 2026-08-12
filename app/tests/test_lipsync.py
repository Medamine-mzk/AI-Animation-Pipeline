"""Tests for the viseme timeline stage (M4)."""

import json
import pytest

from app.pipeline.lipsync import (
    VisemeEvent,
    build_viseme_timelines,
    merge_timeline,
    offset_events,
    parse_rhubarb_xml,
)

REAL_XML = """<?xml version="1.0" encoding="utf-8"?>
<rhubarbResult>
  <metadata>
    <soundFile>seg0.wav</soundFile>
    <duration>2.70</duration>
  </metadata>
  <mouthCues>
    <mouthCue start="0.00" end="0.03">X</mouthCue>
    <mouthCue start="0.03" end="0.18">E</mouthCue>
    <mouthCue start="0.18" end="0.25">F</mouthCue>
    <mouthCue start="0.25" end="0.36">A</mouthCue>
    <mouthCue start="2.69" end="2.70">X</mouthCue>
  </mouthCues>
</rhubarbResult>
"""


def test_parse_rhubarb_xml_seconds_and_values():
    events = parse_rhubarb_xml(REAL_XML)
    assert [ev.shape for ev in events] == ["X", "E", "F", "A", "X"]
    assert events[1].start == 0.03
    assert events[1].end == 0.18


def test_parse_rejects_unknown_shape():
    with pytest.raises(ValueError):
        parse_rhubarb_xml('<rhubarbResult><mouthCues><mouthCue start="0" end="1">Z</mouthCue></mouthCues></rhubarbResult>')


def test_parse_empty_cues():
    assert parse_rhubarb_xml("<rhubarbResult><mouthCues/></rhubarbResult>") == []


def test_offset_events():
    events = [VisemeEvent(start=0.5, end=1.0, shape="A")]
    assert offset_events(events, 10.0) == [VisemeEvent(start=10.5, end=11.0, shape="A")]


def test_merge_sorts_and_merges_adjacent_equal_shapes():
    events = [
        VisemeEvent(start=2.0, end=3.0, shape="B"),
        VisemeEvent(start=0.0, end=0.5, shape="A"),
        VisemeEvent(start=0.5, end=1.0, shape="A"),
        VisemeEvent(start=1.0, end=1.5, shape="B"),
    ]
    merged = merge_timeline(events)
    assert merged == [
        VisemeEvent(start=0.0, end=1.0, shape="A"),
        VisemeEvent(start=1.0, end=1.5, shape="B"),
        VisemeEvent(start=2.0, end=3.0, shape="B"),
    ]


def test_merge_drops_invalid_lengths():
    merged = merge_timeline([
        VisemeEvent(start=1.0, end=1.0, shape="A"),
        VisemeEvent(start=2.0, end=1.0, shape="B"),
    ])
    assert merged == []


def test_merge_clips_overlapping_events(monkeypatch):
    merged = merge_timeline([
        VisemeEvent(start=0.0, end=2.0, shape="A"),
        VisemeEvent(start=1.0, end=2.5, shape="B"),
    ])
    assert merged[0] == VisemeEvent(start=0.0, end=1.0, shape="A")
    assert merged[1] == VisemeEvent(start=1.0, end=2.5, shape="B")


TRANSCRIPT = {
    "segments": [
        {"speaker": "SPEAKER_01", "start": 1.63, "end": 4.33, "text": "Hi."},
        {"speaker": "SPEAKER_01", "start": 4.77, "end": 6.53, "text": "Bye."},
        {"speaker": "SPEAKER_02", "start": 6.95, "end": 7.63, "text": "Hey."},
    ]
}


def _stub_segment_events(monkeypatch, tmp_path):
    """Stub rhubarb/ffmpeg; segment i returns one 'A' event of length 1s."""

    def fake_cut(ffmpeg_exe, source, start, end, out):
        out.write_bytes(b"wav")

    def fake_rhubarb(rhubarb_exe, audio, dialog, out_xml):
        return [VisemeEvent(start=0.25, end=0.75, shape="A")]

    monkeypatch.setattr("app.pipeline.lipsync.cut_audio", fake_cut)
    monkeypatch.setattr("app.pipeline.lipsync.run_rhubarb", fake_rhubarb)
    return tmp_path / "work"


def test_build_timelines_offsets_into_global_time(monkeypatch, tmp_path):
    work = _stub_segment_events(monkeypatch, tmp_path)
    timelines = build_viseme_timelines(
        TRANSCRIPT, tmp_path / "src.wav", work,
        rhubarb_exe=tmp_path / "rhubarb.exe", ffmpeg_exe=tmp_path / "ffmpeg.exe",
    )
    assert timelines["SPEAKER_01"] == [
        VisemeEvent(start=1.63 + 0.25, end=1.63 + 0.75, shape="A"),
        VisemeEvent(start=4.77 + 0.25, end=4.77 + 0.75, shape="A"),
    ]
    assert timelines["SPEAKER_02"] == [
        VisemeEvent(start=6.95 + 0.25, end=6.95 + 0.75, shape="A"),
    ]


def test_build_timelines_uses_segment_text_and_source(monkeypatch, tmp_path):
    calls = {}

    def fake_rhubarb(rhubarb_exe, audio, dialog, out_xml):
        calls.setdefault("dialogs", []).append(dialog)
        calls["audio"] = audio
        return [VisemeEvent(start=0.0, end=0.1, shape="X")]

    def fake_cut(ffmpeg_exe, source, start, end, out):
        calls["source"] = source
        calls.setdefault("cuts", []).append((start, end))

    monkeypatch.setattr("app.pipeline.lipsync.cut_audio", fake_cut)
    monkeypatch.setattr("app.pipeline.lipsync.run_rhubarb", fake_rhubarb)
    build_viseme_timelines(
        TRANSCRIPT, tmp_path / "src.wav", tmp_path / "work",
        rhubarb_exe=tmp_path / "rhubarb.exe", ffmpeg_exe=tmp_path / "ffmpeg.exe",
    )
    assert calls["dialogs"] == ["Hi.", "Bye.", "Hey."]
    assert calls["source"] == tmp_path / "src.wav"
    assert calls["cuts"][0] == (1.63, 4.33)
    assert len(calls["cuts"]) == 3


def test_build_timelines_honors_remap(monkeypatch, tmp_path):
    work = _stub_segment_events(monkeypatch, tmp_path)
    timelines = build_viseme_timelines(
        TRANSCRIPT, tmp_path / "src.wav", work,
        rhubarb_exe=tmp_path / "rhubarb.exe", ffmpeg_exe=tmp_path / "ffmpeg.exe",
        remap={"seg_2": "SPEAKER_01"},
    )
    assert "SPEAKER_02" not in timelines
    assert timelines["SPEAKER_01"] == [
        VisemeEvent(start=1.63 + 0.25, end=1.63 + 0.75, shape="A"),
        VisemeEvent(start=4.77 + 0.25, end=4.77 + 0.75, shape="A"),
        VisemeEvent(start=6.95 + 0.25, end=6.95 + 0.75, shape="A"),
    ]


def test_json_roundtrip():
    from app.pipeline.lipsync import viseme_timelines_to_json

    payload = viseme_timelines_to_json(
        {"S0": [VisemeEvent(start=1.0, end=2.0, shape="A")]}
    )
    assert json.loads(json.dumps(payload))["S0"][0]["shape"] == "A"
