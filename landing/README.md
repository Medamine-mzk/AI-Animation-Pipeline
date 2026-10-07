# Landing page

Static information page for visitors and jury members. It explains why the project
cannot run on a free hosting tier, and how to run it locally.

## Deploying to Vercel

Import the repository in Vercel, then set:

| Setting | Value |
|---|---|
| Framework preset | **Other** |
| Root directory | `landing` |
| Build command | *(leave empty)* |
| Output directory | *(leave empty)* |
| Install command | *(leave empty)* |

There is no build step and no dependency: `index.html` is a single self-contained
file using the Tailwind CDN and Google Fonts.

## Files

- `index.html` — the page. Bilingual FR/EN; the toggle stores the choice in
  `localStorage` under `landing_lang`, and the language is applied before first
  paint so there is no flash of the wrong one.
- `player-01.png`, `player-02.png` — **not present yet.** Screenshots of the
  running player, to be captured in a real browser. Headless Chrome renders these
  skinned meshes through SwiftShader and produces a visibly blurry face, so the
  captures were discarded rather than shipped.

## Editing the copy

Every translatable string carries `data-fr` and `data-en` on the same element:

```html
<p data-fr>Texte français</p>
<p data-en>English text</p>
```

Adding a string means adding **both** attributes — a `[data-fr]` with no `[data-en]`
partner will be invisible in English. The counts are currently balanced at 36/36;
`test_landing_page.py` asserts that so the mistake cannot ship silently.

## Why the page does not say "impossible on Render"

Because it is not true, and the page exists to be believed. Render's $25/month
Standard plan offers 2 GB against a measured 1.76 GB peak — borderline, not
impossible. The page states the free tier cannot work, that the cheapest tier
which *might* work leaves almost no headroom, and that Docker locally is the
supported path. Any stronger claim could be disproved by checking Render's pricing
table.

The figures come from `DEPLOYMENT.md` and `README.md`. If the models or the peak
change, update this page in the same commit.
