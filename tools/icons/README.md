# Icons

Word-icon visualization for the animation pipeline: when a mapped word is
spoken, its icon pops in at the bottom of the frame, holds until just before
the next icon, then fades out.

## Sources & licensing

- **Default: Unicode emoji** (`dictionary.json`) — no attribution required, free
  for educational/commercial use. Rendered locally via headless Chrome
  (`seguiemj.ttf`) because Pillow cannot render color emoji.
- **Optional hero overrides** (`jobs/<clip>/icon_override/`): drop a `<word>.png`
  to replace that word's emoji. For educational videos, Flaticon/Freepik free
  icons require per-icon attribution (track in `jobs/<clip>/ICON_CREDITS.md`).
  LottieFiles public animations are fine to use under the Lottie Simple License
  (no attribution required).

## Files

- `dictionary.json` — general school-English word → emoji (~500 entries). The
  reusable base for every video.
- `bake.js` — renders every dictionary + clip-map entry to `app/assets/icons/<word>.png`.
  ```
  node tools/icons/bake.js
  node tools/icons/bake.js --map jobs/golden/icon_map.json   # + proper nouns
  node tools/icons/bake.js --force                            # re-bake all
  ```
- `page.html` — minimal page baked against by `bake.js`.

## Extending coverage (any future video)

1. Run the icons stage; it prints content words with no icon:
   ```
   python -m app.pipeline.icons jobs/<clip>/transcript.json \
       -o jobs/<clip>/icons.json --map jobs/<clip>/icon_map.json
   ```
2. Add general words to `tools/icons/dictionary.json`, or clip-specific proper
   nouns to `jobs/<clip>/icon_map.json`.
3. Re-bake icons, re-run the icons stage, render.

## Pipeline

```
transcript.json --[app.pipeline.icons]--> icons.json --[animate --icons]--> frames
```
