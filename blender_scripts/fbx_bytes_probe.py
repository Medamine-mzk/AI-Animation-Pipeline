import re
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_bytes()
terms = [
    b"Blendshape",
    b"blendShape",
    b"BlendShape",
    b"Shape",
    b"Blink",
    b"Brows",
    b"Mouth",
    b"Sneer",
    b"jaw",
    b"eyeBlink",
    b"BSL",
    b"Channels",
    b"Deformers",
    b"Skin",
]
for t in terms:
    print(f"{t.decode():<15} {raw.count(t)}")
print(f"size {len(raw)}")
print("header:", raw[:60])
strings = re.findall(rb"[A-Za-z][A-Za-z0-9_:]{2,40}", raw)
mixamo_like = sorted({s.decode() for s in strings if b"Mouth" in s or b"eye" in s.lower() and len(s) > 4})[:40]
print("mouth/eye-ish strings:", mixamo_like)