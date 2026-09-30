/**
 * Definitive dump of the active-word state at a known time.
 *
 * Earlier probes sampled a single pixel near a rounded corner, which cannot tell
 * "no pill" apart from "sampled just outside the corner radius". This one reports,
 * for every .w element the canvas loop will actually visit:
 *   - whether it carries .active
 *   - its computed background-color and colour
 *   - its rect
 * and then scans a horizontal line straight through the active word's vertical
 * centre to find which pixels the pill really painted.
 *
 * Usage: node probe_active_words.mjs <cap_job_id> [timeMs]
 */
import { pathToFileURL } from 'node:url';

const REPO = 'E:/PROJECTS/AI Animation Pipeline';
const PP = `${REPO}/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js`;
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const JOB = process.argv[2];
const FORCED_MS = process.argv[3] ? Number(process.argv[3]) : null;

const pp = (await import(pathToFileURL(PP).href)).default;
const browser = await pp.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required', '--mute-audio'],
});

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 800 });
  page.on('pageerror', (e) => console.log('  [pageerror]', e.message));
  await page.goto(`http://127.0.0.1:8000/captions.html?job=${JOB}`, { waitUntil: 'networkidle2' });
  await page.waitForFunction(() => window.captionsApp && window.captionsApp.state.captions);
  await page.waitForFunction(() => window.captionsApp.state.hasVideo, { timeout: 20000 });

  const result = await page.evaluate(async (forcedMs) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const v = document.getElementById('video');

    let target = forcedMs;
    if (target == null) {
      const pg = captionsApp.state.captions.pages.find((p) => p.words.length >= 4);
      target = (pg.words[2].startMs + pg.words[2].endMs) / 2;
    }

    await new Promise((res) => {
      const on = () => { v.removeEventListener('seeked', on); res(); };
      v.addEventListener('seeked', on);
      v.currentTime = target / 1000;
    });
    await sleep(250);

    const plate = document.getElementById('plate');
    const stage = document.getElementById('stage').getBoundingClientRect();

    // Exactly the selector drawCaptionOnCanvas uses.
    const spans = Array.from(document.querySelectorAll('#words .w'));
    const rows = spans.map((s, i) => {
      const cs = getComputedStyle(s);
      const r = s.getBoundingClientRect();
      return {
        i,
        text: s.textContent,
        active: s.classList.contains('active'),
        past: s.classList.contains('past'),
        bg: cs.backgroundColor,
        color: cs.color,
        transform: cs.transform,
        rect: {
          x: Math.round(r.left - stage.left),
          y: Math.round(r.top - stage.top),
          w: Math.round(r.width),
          h: Math.round(r.height),
        },
      };
    });

    const activeRows = rows.filter((r) => r.active);

    // Repaint the real function onto a canvas and scan a line through the
    // active word, reporting every run of distinct colour.
    const vw = v.videoWidth, vh = v.videoHeight;
    const cv = document.createElement('canvas');
    cv.width = vw; cv.height = vh;
    const ctx = cv.getContext('2d');
    const scale = vw / document.getElementById('stage').getBoundingClientRect().width;

    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, vw, vh);
    drawCaptionOnCanvas(ctx, scale);

    let scan = null;
    if (activeRows.length === 1) {
      const el = document.querySelector('#words .w.active');
      const r = el.getBoundingClientRect();
      const y = Math.round((r.top + r.height / 2 - stage.top) * scale);
      const x0 = Math.round((r.left - stage.left) * scale) - 14;
      const x1 = Math.round((r.right - stage.left) * scale) + 14;
      const data = ctx.getImageData(x0, y, x1 - x0, 1).data;
      const runs = [];
      let prev = null;
      for (let i = 0; i < data.length; i += 4) {
        const key = `${data[i]},${data[i + 1]},${data[i + 2]}`;
        if (key !== prev) { runs.push({ at: i / 4, rgb: key }); prev = key; }
      }
      scan = {
        y, x0, x1,
        widthInCanvasPx: x1 - x0,
        activeRectInCanvasPx: {
          x: Math.round((r.left - stage.left) * scale),
          w: Math.round(r.width * scale),
          h: Math.round(r.height * scale),
        },
        runs,
      };
    }

    return {
      targetMs: target,
      videoTimeMs: v.currentTime * 1000,
      spanCount: spans.length,
      activeCount: activeRows.length,
      plateActiveColorVar: plate.style.getPropertyValue('--active-color'),
      plateBackground: getComputedStyle(plate).backgroundColor,
      rows,
      scan,
    };
  }, FORCED_MS);

  console.log(JSON.stringify(result, null, 2));
  if (result.activeCount !== 1) {
    console.log(`\nFAIL  expected exactly 1 active word, found ${result.activeCount}`);
    process.exitCode = 1;
  } else if (result.plateActiveColorVar.trim() === '') {
    console.log('\nFAIL  --active-color is unset on the plate, so the pill colour is invalid');
    process.exitCode = 1;
  } else {
    console.log('\nPASS  exactly one active word and --active-color resolved');
  }
} finally {
  await browser.close();
}