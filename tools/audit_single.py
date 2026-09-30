#!/usr/bin/env python3
"""audit_single.py — check single frame mean/white%."""
import argparse, sys
from pathlib import Path
from PIL import Image
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--image", required=True)
ap.add_argument("--min-mean", type=float, default=90)
ap.add_argument("--max-mean", type=float, default=135)
ap.add_argument("--max-white", type=float, default=1.0)
args = ap.parse_args()

img = Image.open(args.image)
arr = np.array(img)
mean = arr.mean()
white = (arr > 240).mean()*100
black = (arr < 20).mean()*100
print(f"Image {args.image}: Mean {mean:.1f} White {white:.2f}% Black {black:.2f}% Max {arr.max()} Min {arr.min()} Size {img.size}")
ok = True
if mean < args.min_mean or mean > args.max_mean:
    print(f"FAIL mean {mean:.1f} not in [{args.min_mean},{args.max_mean}]")
    ok=False
if white > args.max_white:
    print(f"FAIL white {white:.2f}% > {args.max_white}%")
    ok=False
sys.exit(0 if ok else 1)
