/**
 * Real-browser verification of captions.html.
 *
 * Drives Chrome via the puppeteer-core already vendored in tools/live2d, so no
 * new dependency is added. The point is to prove the highlight is actually in
 * sync with playback on screen -- the logic tests cover pageAt/wordIndexIn, but
 * only a real rAF loop over a real <video> can prove the wiring.
 *
 * Usage: node verify_captions_browser.mjs [--video <path>] [--shot <out.png>]
 */
import { writeFileSync } from 'fs';
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';
import path from 'node:path';

// Resolve the puppeteer-core already vendored for the Live2D plate baker rather
// than adding a new npm dependency. ESM ignores NODE_PATH, so it has to be
// imported by absolute file URL.
const REPO = process.env.CAPTIONS_REPO ||
  'E:\\PROJECTS\\AI Animation Pipeline';
const PP = path.join(REPO, 'tools', 'live2d', 'node_modules', 'puppeteer-core',
                    'lib', 'cjs', 'puppeteer', 'puppeteer-core.js');
if (!existsSync(PP)) {
  console.error(`puppeteer-core not found at ${PP}\n` +
                `run: npm --prefix tools/live2d install`);
  process.exit(2);
}
const puppeteer = (await import(pathToFileURL(PP).href)).default;

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  ].filter(Boolean);
  for (const c of candidates) if (existsSync(c)) return c;
  console.error('no Chrome/Edge found; set CHROME_PATH');
  process.exit(2);
}
const CHROME = findChrome();
const BASE = 'http://127.0.0.1:8000';
const OUT = process.env.TEMP || 'C:\\Users\\SBS\\AppData\\Local\\Temp\\opencode';

const args = process.argv.slice(2);
const videoArg = args.includes('--video') ? args[args.indexOf('--video') + 1] : null;
const shot = args.includes('--shot') ? args[args.indexOf('--shot') + 1] : null;

const url = BASE + '/captions.html' + (videoArg ? '?video=' + encodeURIComponent(videoArg) : '');

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--autoplay-policy=no-user-gesture-required', '--no-sandbox', '--mute-audio'],
});

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
  if (!ok) failures.push(name + (detail ? ' -> ' + detail : ''));
};

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 900 });

  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message + '\n    ' + (e.stack || '')));

  await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });

  // --- data loaded ------------------------------------------------------
  await page.waitForFunction(() => window.captionsApp && window.captionsApp.state.captions, { timeout: 15000 });
  const info = await page.evaluate(() => ({
    pages: captionsApp.state.captions.pages.length,
    styles: captionsApp.state.captions.styles.length,
    durationMs: captionsApp.state.captions.durationMs,
    hasVideo: captionsApp.state.hasVideo,
    videoSrc: document.getElementById('video').getAttribute('src'),
    legendRows: document.querySelectorAll('.legend-row').length,
    provenance: document.getElementById('provenance').textContent.trim(),
  }));
  check('captions loaded', info.pages > 0, `${info.pages} pages, ${info.styles} styles`);
  check('speaker legend rendered', info.legendRows === info.styles, `${info.legendRows} rows`);
  check('provenance states measured timings', /measured/.test(info.provenance));
  if (videoArg) check('video attached', info.hasVideo, String(info.videoSrc));

  // --- seek to a known word and confirm the highlight -------------------
  // Drive the video's currentTime, then let a few frames run.
  const samples = await page.evaluate(async () => {
    const out = [];
    const v = document.getElementById('video');
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    // Only sample pages that fit inside the media. Seeking past the end of a
    // short video clamps to its last frame, which would look like a highlight
    // bug rather than a caption/media length mismatch.
    const mediaMs = (captionsApp.state.hasVideo && v.duration)
      ? v.duration * 1000
      : captionsApp.state.captions.durationMs;
    const inRange = captionsApp.state.captions.pages.filter((p) => p.startMs < mediaMs - 200);

    for (const p of inRange) {
      const last = p.words[p.words.length - 1];
      const target = Math.min(Math.floor((last.startMs + last.endMs) / 2), mediaMs - 200);
      if (captionsApp.state.hasVideo) {
        await new Promise((res) => {
          const onSeek = () => { v.removeEventListener('seeked', onSeek); res(); };
          v.addEventListener('seeked', onSeek);
          v.currentTime = target / 1000;
        });
      } else {
        captionsApp.state.timeMs = target;
      }
      await sleep(140); // let a few rAF frames run

      const spans = [...document.querySelectorAll('#words .w')];
      const active = spans.findIndex((s) => s.classList.contains('active'));
      const styles = getComputedStyle(spans[active] || document.body);
      // The renderer's rule: the last word whose start has passed.
      let expected = -1;
      for (let i = 0; i < p.words.length; i++) {
        if (p.words[i].startMs <= target) expected = i;
      }
      out.push({
        page: p.index,
        expectedActive: expected,
        active,
        spans: spans.length,
        words: p.words.length,
        visible: document.getElementById('plate').classList.contains('visible'),
        activeText: spans[active] ? spans[active].textContent : null,
        expectText: expected >= 0 ? p.words[expected].text.trim() : null,
        activeBg: styles.backgroundColor,
        activeTransform: styles.transform,
      });
    }
    return out;
  });

  let activeWrong = 0, spanWrong = 0, textWrong = 0, invisible = 0, noScale = 0, noPill = 0;
  for (const s of samples) {
    if (s.active !== s.expectedActive) activeWrong++;
    if (s.spans !== s.words) spanWrong++;
    if (s.activeText !== s.expectText) textWrong++;
    if (!s.visible) invisible++;
    // The active word must have a pill (non-transparent background) and a scale pop.
    const bg = s.activeBg || '';
    const alpha = bg.startsWith('rgba') ? Number(bg.split(',')[3] ?? '1') : 1;
    if (alpha === 0) noPill++;
    if (!/matrix\((?!1, 0, 0, 1, )/.test(s.activeTransform || '')) noScale++;
  }
  check('every page drew its words', spanWrong === 0, `${spanWrong}/${samples.length} wrong span count`);
  check('highlight on the expected word', activeWrong === 0, `${activeWrong}/${samples.length} wrong`);
  check('highlight text matches', textWrong === 0, `${textWrong}/${samples.length} wrong`);
  check('plate visible while speaking', invisible === 0, `${invisible}/${samples.length} hidden`);
  check('active word has a filled pill', noPill === 0, `${noPill}/${samples.length} without`);
  check('active word has a scale pop', noScale === 0, `${noScale}/${samples.length} without`);

  // --- silence shows nothing -------------------------------------------
  const inSilence = await page.evaluate(async () => {
    const v = document.getElementById('video');
    // The golden clip's first word starts at 1627ms; the video may be shorter.
    const t = 200;
    if (captionsApp.state.hasVideo) { v.currentTime = t / 1000; } else { captionsApp.state.timeMs = t; }
    await new Promise((r) => setTimeout(r, 140));
    return {
      visible: document.getElementById('plate').classList.contains('visible'),
      spans: document.querySelectorAll('#words .w').length,
    };
  });
  check('no caption during silence', inSilence.spans === 0 || !inSilence.visible,
        `spans=${inSilence.spans} visible=${inSilence.visible}`);

  // --- play actually advances the highlight ----------------------------
  const played = await page.evaluate(async () => {
    const v = document.getElementById('video');
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const target = captionsApp.state.captions.pages[0];
    const mid = (target.words[0].startMs + target.words[0].endMs) / 2;
    if (captionsApp.state.hasVideo) { v.currentTime = mid / 1000; } else { captionsApp.state.timeMs = mid; }
    await sleep(100);
    document.getElementById('playBtn').click();
    await sleep(700);
    const idx = [...document.querySelectorAll('#words .w')].findIndex(
      (s) => s.classList.contains('active'));
    const clock = captionsApp.state.hasVideo ? v.currentTime * 1000 : captionsApp.state.timeMs;
    document.getElementById('playBtn').click();
    return { activeIndex: idx, clockMs: Math.round(clock) };
  });
  check('playback advances the word highlight', played.activeIndex >= 0,
        `active index ${played.activeIndex} at ${played.clockMs}ms`);

  // --- reduced motion + high contrast ---------------------------------
  const a11y = await page.evaluate(async () => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    document.getElementById('reducedMotion').click();
    document.getElementById('highContrast').click();
    await sleep(60);
    const spans = [...document.querySelectorAll('#words .w')];
    const act = spans.find((s) => s.classList.contains('active')) || spans[0];
    return {
      reduced: document.body.classList.contains('reduced-motion'),
      contrast: document.body.classList.contains('high-contrast'),
      transform: act ? getComputedStyle(act).transform : 'none',
      plateBg: getComputedStyle(document.getElementById('plate')).backgroundColor,
    };
  });
  check('reduced motion removes the scale', /matrix\(1, 0, 0, 1, 0, 0\)|none/.test(a11y.transform), a11y.transform);
  check('high contrast makes the plate opaque', !/rgba\([^)]*0?\.\d+\s*\)/.test(a11y.plateBg), a11y.plateBg);
  await page.evaluate(() => {
    document.getElementById('reducedMotion').click();
    document.getElementById('highContrast').click();
  });

  // --- caption size control -------------------------------------------
  const sized = await page.evaluate(async () => {
    const s = document.getElementById('size');
    s.value = '52';
    s.dispatchEvent(new Event('input'));
    await new Promise((r) => setTimeout(r, 40));
    return {
      px: getComputedStyle(document.querySelector('#words .w')).fontSize,
      label: document.getElementById('sizeVal').textContent,
    };
  });
  check('caption size control applies', sized.px === '52px' && sized.label === '52px',
        `${sized.px} / ${sized.label}`);

  // --- no overflow past two lines --------------------------------------
  const lines = await page.evaluate(() => {
    const w = document.getElementById('words');
    const cs = getComputedStyle(w);
    return { clamp: cs.webkitLineClamp || cs.lineClamp, overflow: cs.overflow };
  });
  check('captions clamped to two lines', String(lines.clamp) === '2', JSON.stringify(lines));

  // --- honest reporting when captions outrun the media ------------------
  const mismatch = await page.evaluate(() => {
    const n = document.getElementById('mismatch');
    return {
      hidden: n.classList.contains('hidden'),
      text: n.textContent.trim(),
      provenance: document.getElementById('provenance').textContent,
    };
  });
  if (videoArg) {
    check('caption/media length mismatch is reported', !mismatch.hidden,
          mismatch.text || '(no warning shown)');
    check('provenance survives alongside the warning', /measured/.test(mismatch.provenance));
  } else {
    check('no spurious mismatch warning on the demo', mismatch.hidden, mismatch.text);
  }

  // --- words must not visually collide ---------------------------------
  // Regression guard: words once ran together as "TheBrownsare" because the
  // spans had padding but no inter-word margin, and no logic test could see it.
  const collisions = await page.evaluate(async () => {
    const s = document.getElementById('size');
    const out = [];
    for (const px of ['20', '34', '64']) {
      s.value = px;
      s.dispatchEvent(new Event('input'));
      await new Promise((r) => setTimeout(r, 90));
      // Use the page with the most words so the check is meaningful.
      let best = null;
      for (const p of captionsApp.state.captions.pages) {
        if (!best || p.words.length > best.words.length) best = p;
      }
      const mid = (best.startMs + best.endMs) / 2;
      const v = document.getElementById('video');
      if (captionsApp.state.hasVideo) v.currentTime = mid / 1000;
      else captionsApp.state.timeMs = mid;
      await new Promise((r) => setTimeout(r, 120));

      const spans = [...document.querySelectorAll('#words .w')];
      const rects = spans.map((x) => x.getBoundingClientRect());
      let minGap = Infinity;
      for (let i = 1; i < rects.length; i++) {
        // Same line only: a line break legitimately leaves a vertical gap.
        if (Math.abs(rects[i].top - rects[i - 1].top) > 2) continue;
        minGap = Math.min(minGap, rects[i].left - rects[i - 1].right);
      }
      out.push({ px: +px, words: spans.length, minGap: Math.round(minGap) });
    }
    s.value = '34';
    s.dispatchEvent(new Event('input'));
    return out;
  });
  for (const c of collisions) {
    check(`words stay separated at ${c.px}px`, c.minGap >= 2,
          `${c.words} words, min gap ${c.minGap}px`);
  }

  // --- 2 lines at the largest size ------------------------------------
  const overflow = await page.evaluate(() => {
    const s = document.getElementById('size');
    s.value = '64';
    s.dispatchEvent(new Event('input'));
    const w = document.getElementById('words');
    const plate = document.getElementById('plate');
    return {
      plateH: plate.getBoundingClientRect().height,
      stageH: document.getElementById('stage').getBoundingClientRect().height,
      lines: Math.round(w.scrollHeight / parseFloat(getComputedStyle(w).lineHeight)),
    };
  });
  check('64px captions still fit the stage', overflow.plateH < overflow.stageH,
        `plate ${Math.round(overflow.plateH)}px vs stage ${Math.round(overflow.stageH)}px`);

  if (shot) {
    await page.evaluate(async () => {
      // Reset appearance to defaults so the screenshot shows the real look
      // rather than whatever the size/overflow checks left behind.
      const s = document.getElementById('size');
      s.value = '34';
      s.dispatchEvent(new Event('input'));
      const rm = document.getElementById('reducedMotion');
      if (rm.checked) rm.click();
      const hc = document.getElementById('highContrast');
      if (hc.checked) hc.click();
      const pos = document.getElementById('pos');
      pos.value = '8';
      pos.dispatchEvent(new Event('change'));

      // Land on the longest page so the two-line layout is exercised.
      let best = captionsApp.state.captions.pages[0];
      for (const p of captionsApp.state.captions.pages) {
        if (p.words.length > best.words.length) best = p;
      }
      // Aim at the middle word so the highlight is mid-line, not at an edge.
      const w = best.words[Math.floor(best.words.length / 2)];
      const t = (w.startMs + w.endMs) / 2;
      const v = document.getElementById('video');
      if (captionsApp.state.hasVideo) v.currentTime = t / 1000;
      else captionsApp.state.timeMs = t;
    });
    await new Promise((r) => setTimeout(r, 350));
    const buf = await page.screenshot({ type: 'png' });
    writeFileSync(shot, buf);
    console.log(`\nscreenshot (defaults, longest page) -> ${shot}`);
  }

  check('no console errors', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '));

  console.log('\n' + (failures.length ? `FAILED (${failures.length}):\n- ` + failures.join('\n- ')
                                   : 'ALL BROWSER CHECKS PASSED'));
} finally {
  await browser.close();
}

process.exit(failures.length ? 1 : 0);

