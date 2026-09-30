#!/usr/bin/env python3
"""
picker_to_dialogue.py — convert picker JSON + script.json + visemes.json -> dialogue.json for TalkingHead.

Usage:
  python tools/picker_to_dialogue.py --picker jobs/picker/living_curtains_picker.json --script jobs/golden/script.json --visemes jobs/golden/visemes.json --out jobs/dialogue.json
"""
import argparse, json
from pathlib import Path

MODEL_MAP = {"man":"assets/man.glb","woman":"assets/woman.glb","boy":"assets/man1.glb","girl":"assets/woman1.glb"}
ROLE_TO_SPEAKER = {"man":"SPEAKER_00","woman":"SPEAKER_02","girl":"SPEAKER_01","boy":"SPEAKER_03"}
SPEAKER_TO_ROLE = {v:k for k,v in ROLE_TO_SPEAKER.items()}

def parse_args():
    ap=argparse.ArgumentParser()
    ap.add_argument("--picker", required=True)
    ap.add_argument("--script", default="jobs/golden/script.json")
    ap.add_argument("--visemes", default="jobs/golden/visemes.json")
    ap.add_argument("--out", default="jobs/dialogue.json")
    ap.add_argument("--line-emotions", default="jobs/golden/line_emotions.json")
    ap.add_argument("--catalog", default="jobs/golden/clip_catalog.json")
    return ap.parse_args()

SCRIPT_TO_GO = {"neutral":"neutral","happy":"joy","sad":"sadness","angry":"anger","surprised":"surprise","worried":"nervousness","excited":"excitement","annoyed":"annoyance"}

def main():
    args=parse_args()
    picker=json.loads(Path(args.picker).read_text(encoding="utf-8"))
    script=json.loads(Path(args.script).read_text(encoding="utf-8"))
    try:
        visemes=json.loads(Path(args.visemes).read_text(encoding="utf-8"))
    except: visemes={}
    try:
        line_emotions=json.loads(Path(args.line_emotions).read_text(encoding="utf-8"))
    except: line_emotions={}
    try:
        catalog=json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    except: catalog={"go_emotions":{"neutral":["Idle.fbx"]},"clips_dir":"media/3d/mixamo/clips","line_overrides":{}}
    spots=picker.get("spots",[])
    # Build speakers from spots
    speakers={}
    for s in spots:
        role=s.get("role","man")
        sid=ROLE_TO_SPEAKER.get(role, s.get("id", role))
        # also keep original id for trace
        # Picker yaw 0 = +X (east), TalkingHead/Three yaw 0 = +Z, so subtract 90 deg to align
        speakers[sid]={
            "name": role,
            "avatarUrl": MODEL_MAP.get(role, "assets/man.glb"),
            "body": "M" if role in ("man","boy") else "F",
            "position": {"x": float(s["x"]), "y": 0, "z": float(s["z"])},
            "rotation": (float(s.get("yaw",0)) - 90) * 3.14159265/180.0,
            "lipsyncLang": "en"
        }
    # fallback if spots missing roles, ensure 4 speakers
    if not speakers:
        for role in ["girl","woman","man","boy"]:
            sid=ROLE_TO_SPEAKER[role]
            speakers[sid]= {"name":role,"avatarUrl":MODEL_MAP[role],"body":"M" if role in ("man","boy") else "F","position":{"x":0,"y":0,"z":0},"rotation":0,"lipsyncLang":"en"}

    # Build clip/mood mapping per line via line_emotions + catalog (like scene_automator select_line_clips)
    cdir = catalog.get("clips_dir", "media/3d/mixamo/clips")
    overrides = catalog.get("line_overrides", {})
    counters = {}
    segments=[]
    for line in script.get("lines",[]):
        cid=line.get("character_id")
        if cid not in speakers:
            continue
        start=float(line.get("start",0))
        end=float(line.get("end",0))
        text=line.get("text","")
        words=text.split()
        dur_ms=(end-start)*1000
        if words:
            avg=dur_ms/len(words)
            wtimes=[int(i*avg) for i in range(len(words))]
            wdurations=[int(avg*0.8) for _ in words]
        else:
            words,wtimes,wdurations=[],[],[]
        # Determine mood/clip via line_emotions + catalog
        key = f"{start:.2f}"
        det = line_emotions.get(key)
        label = det["top"] if det else SCRIPT_TO_GO.get(line.get("emotion","neutral"), "neutral")
        # line_overrides may specify exact clip for this start
        clip = overrides.get(key)
        if not clip:
            pool = catalog.get("go_emotions", {}).get(label) or catalog.get("go_emotions", {}).get("neutral", ["Idle.fbx"])
            n = counters.get((cid, label), 0)
            clip = pool[n % len(pool)]
            # avoid repeating same clip consecutively if pool >1
            counters[(cid, label)] = n + 1
        # Map clip to mood for TalkingHead (simplify: use label as mood, TalkingHead supports neutral/happy/sad/angry etc.)
        mood = label if label in ("neutral","joy","sadness","anger","surprise","nervousness","excitement","annoyance","admiration","amusement","approval","caring","confusion","curiosity","desire","disappointment","disapproval","disgust","embarrassment","excitement","fear","gratitude","grief","love","optimism","pride","realization","relief","remorse") else "neutral"
        # TalkingHead avatarMood expects narrower set: map go_emotion to TalkingHead mood
        th_mood = {"neutral":"neutral","joy":"happy","sadness":"sad","anger":"angry","surprise":"surprised","nervousness":"worried","excitement":"excited","annoyance":"annoyed"}.get(mood, "neutral")
        segments.append({
            "speaker": cid,
            "start": start,
            "end": end,
            "text": text,
            "words": words,
            "wtimes": wtimes,
            "wdurations": wdurations,
            "mood": th_mood,
            "clip": f"{cdir}/{clip}",
            "go_label": label
        })
    segments.sort(key=lambda s: s["start"])
    # Staged entrance: compute walkins + present order for any future video
    # First appearance per speaker
    first_start={}
    prev_end_before={}
    for s in segments:
        cid=s["speaker"]
        if cid not in first_start or s["start"] < first_start[cid]:
            first_start[cid]=float(s["start"])
    # Order speakers by first appearance
    ordered=[sid for sid,_ in sorted(first_start.items(), key=lambda kv: kv[1])]
    # For each speaker after first, find gap from previous segment end
    all_ends_sorted = sorted([float(s["end"]) for s in segments])
    # Generous 1.2m social: circle packing around center (guaranteed, any N)
    import math as _math
    N = len(speakers)
    target = 1.2
    r_social = target / (2*_math.sin(_math.pi/N)) if N>1 else 0.85
    r = max(r_social, 0.85)
    r = min(r, 1.6)  # inside fitted inner 4.22
    cx0 = sum(s["position"]["x"] for s in speakers.values())/max(1,N)
    cz0 = sum(s["position"]["z"] for s in speakers.values())/max(1,N)
    # Center bias to room center (0,0) 30% to avoid wall lock
    cx = cx0*0.7; cz = cz0*0.7
    # Check if any pair <1.1 — if so, replace with circle (general fix)
    ids = list(speakers.keys())
    need_circle = False
    for i in range(len(ids)):
        for j in range(i+1,len(ids)):
            a=speakers[ids[i]]["position"]; b=speakers[ids[j]]["position"]
            if _math.hypot(a["x"]-b["x"], a["z"]-b["z"]) < 1.1:
                need_circle=True; break
    if need_circle:
        ordered_ids = sorted(ids, key=lambda sid: first_start.get(sid,0))
        for idx, sid in enumerate(ordered_ids):
            ang = 2*_math.pi*idx/len(ordered_ids) - _math.pi/2
            speakers[sid]["position"]["x"] = round(cx + r*_math.cos(ang),3)
            speakers[sid]["position"]["z"] = round(cz + r*_math.sin(ang),3)
        # Recompute center after circle
        cx = sum(s["position"]["x"] for s in speakers.values())/N
        cz = sum(s["position"]["z"] for s in speakers.values())/N
    door_auto = {"x": round(cx,3), "z": round(cz + 3.0, 3)}
    walkins=[]
    # Need prev_end: max end < first_start
    for idx, sid in enumerate(ordered):
        if idx==0: continue
        fs=first_start[sid]
        # max end strictly < fs
        candidate=[e for e in all_ends_sorted if e < fs - 1e-6]
        prev = max(candidate) if candidate else 0.0
        gap=fs - prev
        if gap >= 0.30:
            # Natural walk 1.4 m/s for 3m => 2.14s, allow up to 2.5s
            natural = 3.0/1.4
            dur=min(max(min(gap, natural), 0.8), 2.5)
            t0=fs - dur
            if t0 < prev: t0=prev
            # Per-speaker door 3m south of its slot, axis auto = vector door->slot
            sx = speakers[sid]["position"]["x"]
            sz = speakers[sid]["position"]["z"]
            door = {"x": round(sx,3), "z": round(sz + 3.0,3)}
            walkins.append({"speaker":sid,"t0":round(t0,3),"t1":round(fs,3),"gap":round(gap,3),"door":door})
    # Recompute with intro_len clamp if available (Standard Walk ~1.1s at 24fps)
    out={"speakers":speakers,"segments":segments,"walkins":walkins,"entranceOrder":ordered,"presentTimeline":[{"speaker":sid,"firstStart":first_start[sid]} for sid in ordered]}
    # Persist picker room bounds if available
    try:
        rb=picker.get("roomBounds")
        if rb: out["roomBounds"]=rb
        door=picker.get("door")
        if door: out["door"]=door
        else: out["door"]={**door_auto,"auto":True}
    except: pass
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {args.out} speakers={list(speakers.keys())} segments={len(segments)} walkins={len(walkins)} order={ordered}")
    for w in walkins:
        print(f"  walkin {w['speaker']} {w['t0']:.2f}->{w['t1']:.2f} gap {w['gap']:.2f}")
    # also log positions for verification
    for sid, cfg in speakers.items():
        print(f"  {sid} {cfg['name']} pos {cfg['position']} rot {cfg['rotation']:.2f}")

if __name__=="__main__":
    main()
