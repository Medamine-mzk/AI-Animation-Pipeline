#!/usr/bin/env python3
"""
bake_pixabay.py — download real photos from Pixabay for icon words (smart, cached 24h, 100/60s).
Uses key 42408337-ef00a32fa25ddb58d20642873, q=word image_type=photo horizontal safesearch per_page3.
"""
import argparse, json, os, time, pathlib, urllib.parse, urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
ENV=ROOT/".env"
def load_env():
    try:
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
    except: pass

def fetch_pixabay(q, key, per_page=3):
    params={"key":key,"q":q,"image_type":"photo","orientation":"horizontal","safesearch":"true","per_page":str(per_page),"order":"popular"}
    url="https://pixabay.com/api/?"+urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            body=r.read().decode(); headers=dict(r.headers)
            return json.loads(body), headers
    except Exception as e:
        print(f"  fetch fail {q}: {e}"); return None, {}

def download_image(url, out_path):
    try:
        req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r, open(out_path,"wb") as f:
            f.write(r.read())
        return True
    except Exception as e:
        print(f"    download fail {e} for {url[:80]}"); return False

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--words", default="tools/icons/dictionary.json")
    ap.add_argument("--map", default="jobs/golden/icon_map.json")
    ap.add_argument("--icons", default="jobs/golden/icons.json")
    ap.add_argument("--out", default="app/assets/pixabay")
    ap.add_argument("--cache", default="jobs/golden/pixabay_cache.json")
    ap.add_argument("--limit", type=int, default=110)
    ap.add_argument("--force", action="store_true")
    args=ap.parse_args()
    load_env()
    key=os.environ.get("PIXABAY_API_KEY","42408337-ef00a32fa25ddb58d20642873")
    # words = unique from icons.json (110) for fast demo, fallback to dictionary
    words=[]
    try:
        icons=json.loads(Path(args.icons).read_text(encoding="utf-8"))
        words=list(dict.fromkeys([ev["word"].lower() for ev in icons]))
    except:
        try: words=list(json.loads(Path(args.words).read_text(encoding="utf-8")).keys())
        except: words=[]
    if args.limit: words=words[:args.limit]
    # abstract blocklist -> keep emoji
    abstract={"opportunity","beautiful","expensive","wonderful","hope","together","always","never","very","really","just","still"}
    words=[w for w in words if w.lower() not in abstract]
    print(f"words {len(words)}")
    cache={}
    cache_path=Path(args.cache)
    if cache_path.exists() and not args.force:
        try: cache=json.loads(cache_path.read_text(encoding="utf-8")); print(f"cache loaded {len(cache)}")
        except: cache={}
    out_dir=Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    fetched=cached=fallback=0
    for idx,w in enumerate(words):
        q=w.lower().strip()
        out_file=out_dir/f"{q}.jpg"
        if q in cache and not args.force:
            entry=cache[q]
            if entry and entry.get("fallback"): fallback+=1; continue
            if entry and out_file.exists():
                cached+=1; continue
        # rate limit 100/60s — sleep 0.7s per req ~85/min
        time.sleep(0.7)
        data, headers = fetch_pixabay(q, key, 3)
        if not data or data.get("totalHits",0)==0:
            cache[q]=None; fallback+=1; print(f"[{idx+1}/{len(words)}] {q} no hits -> emoji fallback")
            # respect remaining
            continue
        hits=data.get("hits",[])
        # pick max downloads with tag overlap
        best=None; best_score=-1
        for h in hits:
            tags=(h.get("tags") or "").lower()
            overlap=1 if q in tags else 0.5
            score= h.get("downloads",0)*overlap + h.get("views",0)*0.01
            if score>best_score: best_score=score; best=h
        if not best:
            cache[q]=None; fallback+=1; continue
        url=best.get("webformatURL") or best.get("largeImageURL")
        if not url:
            cache[q]=None; fallback+=1; continue
        ok=download_image(url, out_file)
        if ok:
            # resize to ~640->200 square fit via PIL if available
            try:
                from PIL import Image, ImageOps
                im=Image.open(out_file).convert("RGB")
                im=ImageOps.fit(im,(400,300), method=Image.LANCZOS, centering=(0.5,0.5))
                im.save(out_file, "JPEG", quality=85)
            except: pass
            cache[q]={"id":best.get("id"),"pageURL":best.get("pageURL"),"webformatURL":best.get("webformatURL"),"tags":best.get("tags"),"q":q,"fallback":False}
            try: print(f"[{idx+1}/{len(words)}] {q} -> {best.get('id')} {best.get('tags')[:40]}")
            except: print(f"[{idx+1}/{len(words)}] {q} -> {best.get('id')}")
        else:
            cache[q]=None; fallback+=1
        # handle 429 reset
        rem=headers.get("X-RateLimit-Remaining")
        if rem and int(rem) < 5:
            reset=int(headers.get("X-RateLimit-Reset","60"))
            print(f"  rate low {rem}, sleep {reset}s"); time.sleep(reset+1)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"done fetched {fetched} cached {cached} fallback {fallback} total {len(words)} -> {args.cache}")

if __name__=="__main__":
    main()
