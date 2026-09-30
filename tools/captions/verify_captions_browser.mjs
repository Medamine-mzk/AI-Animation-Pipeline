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
const jobArg = args.includes('--job') ? args[args.indexOf('--job') + 1] : null;
const shot = args.includes('--shot') ? args[args.indexOf('--shot') + 1] : null;

const query = jobArg ? '?job=' + encodeURIComponent(jobArg)
  : videoArg ? '?video=' + encodeURIComponent(videoArg) : '';
const url = BASE + '/captions.html' + query;

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
  await page.waitForFunction(() => window.captionsApp && window.captionsApp.state.captions,
                             { timeout: 15000 });

  // Snapshot the real captions now that the page has loaded, before anything
  // writes. The editor checks below deliberately PUT to the server, so without
  // this the harness would leave a real job containing the word "CORRECTED" and a
  // renamed speaker.
  let pristine = null;
  if (jobArg) {
    pristine = await page.evaluate(async (jid) => {
      const r = await fetch('/caption-jobs/' + jid + '/captions.json?t=' + Date.now(),
                            { cache: 'no-store' });
      return r.ok ? await r.text() : null;
    }, jobArg);
    if (!pristine) throw new Error('could not snapshot captions.json to restore later');
  }

  const restore = async () => {
    if (!pristine) return;
    const ok = await page.evaluate(async (jid, text) => {
      const r = await fetch('/api/caption-jobs/' + jid + '/captions', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        cache: 'no-store',
        body: text,
      });
      return r.ok;
    }, jobArg, pristine).catch(() => false);
    console.log(ok
      ? "\nrestored the job's captions.json to its pre-test state"
      : "\nWARNING: could not restore captions.json -- re-run the job if the wording looks wrong");
  };

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
  // Tests computed visibility, not the presence of the `hidden` class: this
  // page uses plain CSS, so a class that is never defined silently leaves an
  // empty warning box on screen while the assertion still passes.
  const mismatch = await page.evaluate(() => {
    const n = document.getElementById('mismatch');
    return {
      display: getComputedStyle(n).display,
      text: n.textContent.trim(),
      provenance: document.getElementById('provenance').textContent,
    };
  });
  const mismatchVisible = mismatch.display !== 'none';
  if (videoArg) {
    check('caption/media length mismatch is reported', mismatchVisible,
          mismatch.text || '(no warning shown)');
    check('provenance survives alongside the warning', /measured/.test(mismatch.provenance));
  } else {
    check('no spurious mismatch box when captions fit', !mismatchVisible,
          `display=${mismatch.display} text=${JSON.stringify(mismatch.text)}`);
  }

  // Every element that should be hidden must actually compute to display:none.
  // Checks computed visibility, not the presence of the `hidden` class: this
  // page uses plain CSS, so a class or attribute that is never honoured leaves
  // an empty overlay on screen while a class-based assertion still passes.
  const strayBoxes = await page.evaluate(() => {
    const out = [];
    for (const sel of ['#mismatch', '#emptyMsg', '#uploadBar']) {
      for (const el of document.querySelectorAll(sel)) {
        if (getComputedStyle(el).display !== 'none' && !el.textContent.trim()) {
          out.push(sel + ' visible while empty');
        }
      }
    }
    // The placeholder is meant to show when there is no video, and must be gone
    // once one is attached.
    const ph = document.getElementById('placeholder');
    const phShown = getComputedStyle(ph).display !== 'none';
    if (captionsApp.state.hasVideo && phShown) out.push('placeholder still shown over the video');
    if (!captionsApp.state.hasVideo && !phShown) out.push('placeholder hidden but no video to show');
    return out;
  });
  check('no stray boxes, and the placeholder tracks video presence',
        strayBoxes.length === 0, strayBoxes.join('; '));

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

  // --- transcript editor -------------------------------------------------
  if (jobArg) {
    const ed = await page.evaluate(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const card = document.getElementById('editorCard');
      const out = { shown: !card.classList.contains('hidden') && card.offsetHeight > 0 };

      // Seek into the first page so the preview is showing it.
      const p0 = captionsApp.state.captions.pages[0];
      const t = (p0.words[0].startMs + p0.words[0].endMs) / 2;
      const v = document.getElementById('video');
      if (captionsApp.state.hasVideo) {
        await new Promise((res) => {
          const on = () => { v.removeEventListener('seeked', on); res(); };
          v.addEventListener('seeked', on); v.currentTime = t / 1000;
        });
      } else { captionsApp.state.timeMs = t; }
      await sleep(150);

      out.pageBoxes = document.querySelectorAll('.pg').length;
      out.wordInputs = document.querySelectorAll('.wi').length;
      out.inputValues = [...document.querySelectorAll('.wi')].map((i) => i.value);
      out.nameValues = [...document.querySelectorAll('.ni')].map((i) => i.value);
      out.activePageBox = document.querySelectorAll('.pg.active').length;
      out.speakingMarked = document.querySelectorAll('.wi.speaking').length;
      out.flagged = document.querySelectorAll('.wi.flagged').length;
      out.flagNote = document.querySelector('.flag-note')
        ? document.querySelector('.flag-note').textContent.trim() : '';
      out.nameInputs = document.querySelectorAll('.ni').length;

      // Edit the first word and confirm the preview shows the new text.
      const first = document.querySelector('.wi');
      out.before = first.value;
      first.value = 'CORRECTED';
      first.dispatchEvent(new Event('input', { bubbles: true }));
      await sleep(150);
      out.spanText = document.querySelector('#words .w')
        ? document.querySelector('#words .w').textContent : null;
      out.modelText = captionsApp.state.captions.pages[0].words[0].text;
      out.modelFirst = captionsApp.state.captions.pages[0].words[0].text;
      out.saveState = document.getElementById('editState').textContent;
      return out;
    });

    check('editor panel is shown for a real job', ed.shown);
    check('one transcript block per caption page', ed.pageBoxes > 0, `${ed.pageBoxes} blocks`);
    check('one input per word', ed.wordInputs > 0, `${ed.wordInputs} inputs`);

    // Regression: reading the Python-only `displayText` property in JS yields
    // "undefined", which put the literal word "undefined" in every input while
    // the rendered captions looked perfectly fine.
    const junk = ed.inputValues.filter(
      (v) => v === 'undefined' || v === 'null' || v === '' || v === 'NaN');
    check('every transcript input holds real text, not undefined/null',
          junk.length === 0, `${junk.length} bad values: ${JSON.stringify(junk.slice(0, 3))}`);
    const firstInput = ed.inputValues[0] || '';
    const firstModel = String(ed.modelFirst || '').trim();
    check('the first input matches the model text', firstInput === firstModel,
          `input=${JSON.stringify(firstInput)} model=${JSON.stringify(firstModel)}`);
    const junkNames = ed.nameValues.filter((v) => v === 'undefined' || v === '');
    check('every speaker-name field holds real text', junkNames.length === 0,
          JSON.stringify(junkNames));

    check('the on-screen page is marked in the transcript', ed.activePageBox === 1,
          `${ed.activePageBox} active`);
    check('the spoken word is marked in the transcript', ed.speakingMarked === 1,
          `${ed.speakingMarked} marked`);
    check('speaker names are editable', ed.nameInputs > 0, `${ed.nameInputs} name fields`);
    check('an edit reaches the rendered caption immediately',
          ed.spanText === 'CORRECTED', `span shows ${JSON.stringify(ed.spanText)}`);
    check('an edit reaches the shared model', ed.modelText === 'CORRECTED', ed.modelText);
    check('editing marks the save state', /unsaved|saving/i.test(ed.saveState), ed.saveState);

    // Wait for the debounced autosave, then confirm it reached the server.
    // NB: match on the tick, not /saved/i -- that also matches "unsaved", which
    // made a failed save report as a pass.
    const saved = await page.evaluate(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      for (let i = 0; i < 40; i++) {
        await sleep(250);
        const s = document.getElementById('editState').textContent;
        if (/not saved/i.test(s)) return { state: s, ok: false };
        if (/^\u2713/.test(s.trim())) return { state: s, ok: true };
      }
      return { state: document.getElementById('editState').textContent, ok: false };
    });
    check('the edit is persisted to the server', saved.ok, saved.state);

    const onDisk = await page.evaluate(async (jid) => {
      const r = await fetch('/caption-jobs/' + jid + '/captions.json?t=' + Date.now(),
                            { cache: 'no-store' });
      if (!r.ok) return { status: r.status, text: null };
      const j = await r.json();
      return { status: r.status, text: j.pages[0].words[0].text };
    }, jobArg);
    check('the exported data carries the correction', onDisk.text === 'CORRECTED',
          `HTTP ${onDisk.status} first word = ${JSON.stringify(onDisk.text)}`);

    // Rename a speaker and confirm the on-screen tag follows.
    const renamed = await page.evaluate(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const ni = document.querySelector('.ni');
      ni.value = 'Professor Adams';
      ni.dispatchEvent(new Event('input', { bubbles: true }));
      await sleep(150);
      const p0 = captionsApp.state.captions.pages[0];
      const t = (p0.words[0].startMs + p0.words[0].endMs) / 2;
      const v = document.getElementById('video');
      if (captionsApp.state.hasVideo) { v.currentTime = t / 1000; } else { captionsApp.state.timeMs = t; }
      await sleep(150);
      return { tag: document.getElementById('tag').textContent.trim() };
    });
    check('a renamed speaker appears in the caption tag',
          /Professor Adams/.test(renamed.tag), JSON.stringify(renamed.tag));
  } else {
    check('editor hidden on the demo (nothing to persist to)',
          await page.evaluate(() =>
            document.getElementById('editorCard').classList.contains('hidden')));
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

  await restore();

  console.log('\n' + (failures.length ? `FAILED (${failures.length}):\n- ` + failures.join('\n- ')
                                   : 'ALL BROWSER CHECKS PASSED'));
} finally {
  await browser.close();
}

process.exit(failures.length ? 1 : 0);

