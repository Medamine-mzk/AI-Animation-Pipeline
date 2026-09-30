/**
 * Real end-to-end export test.
 *
 * Drives a browser through an actual export of a real job, then verifies the mp4
 * that comes back: it must decode cleanly, be roughly the right length, carry the
 * original audio, and -- the part that actually matters -- contain burned-in
 * pixels where the caption sat. A captioned video with an empty lower third is
 * the failure mode worth catching, and only the pixel comparison catches it.
 */
import { writeFileSync, unlinkSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';

const REPO = 'E:/PROJECTS/AI Animation Pipeline';
const PP = `${REPO}/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js`;
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const FFMPEG = `${REPO}/tools/ffmpeg/ffmpeg.exe`;
const BASE = 'http://127.0.0.1:8000';
const OUTDIR = 'C:\\Users\\SBS\\AppData\\Local\\Temp\\opencode';
const DL = `${OUTDIR}\\export_out.mp4`;

const JOB = process.argv[2];
if (!JOB) {
  console.error('usage: node export_e2e.mjs <cap_job_id>');
  process.exit(2);
}
try { unlinkSync(DL); } catch (e) {}

const failures = [];
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
  if (!ok) failures.push(`${name}${detail ? ' -> ' + detail : ''}`);
};

// ffmpeg exits non-zero when given no output target; stderr still carries the
// report, so treat that as the normal path rather than an exception.
function ff(args, opts = {}) {
  // ffmpeg prints its report (Duration:, Stream #0:0 Video: ...) to STDERR and
  // leaves stdout empty for the null/rawvideo muxes, so both streams have to be
  // combined on EVERY path. Returning stdout only on success handed back an empty
  // string, which made the Duration regex fail, secs collapse to 0, and the
  // "while speaking" sample silently get taken at t=0 -- i.e. the silence frame,
  // so a perfectly good export reported as unreadable, silent and captionless.
  const r = spawnSync(FFMPEG, args, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    ...opts,
  });
  if (r.error) throw r.error;
  return (r.stderr || '') + (r.stdout || '');
}

function meanGray(file, filter) {
  // Must always be a Buffer: ffmpeg failing (or a filter error) makes the
  // catch path return a *string*, and a string has no .reduce, which used to
  // throw a TypeError and hide the real failure.
  let raw;
  try {
    raw = execFileSync(FFMPEG,
      ['-hide_banner', '-i', file, '-vf', filter, '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
      { stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) {
    return null;
  }
  return Buffer.isBuffer(raw) && raw.length ? raw : null;
}

function stats(bytes) {
  if (!bytes) return { mean: NaN, sd: NaN };
  const n = bytes.length;
  const mean = bytes.reduce((a, b) => a + b, 0) / n;
  const sd = Math.sqrt(bytes.reduce((a, b) => a + (b - mean) ** 2, 0) / n);
  return { mean, sd };
}

// One frame of raw pixels from the caption band, at a given timestamp. Kept at
// native resolution: downscaling would blend the pill into the plate and a
// tolerance search for the pill colour would then find nothing.
function rawPixels(file, filter, pixFmt, ss) {
  const r = spawnSync(FFMPEG, [
    '-hide_banner', '-loglevel', 'error', '-ss', String(ss),
    '-i', file, '-vf', filter, '-frames:v', '1',
    '-f', 'rawvideo', '-pix_fmt', pixFmt, '-',
  ], { stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 1 << 28 });
  return r.stdout && r.stdout.length ? r.stdout : null;
}

function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
  const n = m ? parseInt(m[1], 16) : 0x2f505f;
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const pp = (await import('file:///' + PP)).default;
const browser = await pp.launch({
  executablePath: CHROME,
  headless: 'new',
  args: [
    '--no-sandbox',
    '--autoplay-policy=no-user-gesture-required',
    '--mute-audio',
  ],
});

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 800 });

  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errs.push('console: ' + m.text());
  });

  await page.goto(`${BASE}/captions.html?job=${JOB}`, { waitUntil: 'networkidle2' });
  await page.waitForFunction(() => window.captionsApp && window.captionsApp.state.captions);
  await page.waitForFunction(() => window.captionsApp.state.hasVideo, { timeout: 20000 });

  const info = await page.evaluate(() => ({
    pages: captionsApp.state.captions.pages.length,
    words: captionsApp.state.captions.wordCount,
    durationMs: captionsApp.state.captions.durationMs,
    videoDuration: document.getElementById('video').duration,
    vw: document.getElementById('video').videoWidth,
    vh: document.getElementById('video').videoHeight,
    exportShown: !document.getElementById('exportCard').classList.contains('hidden'),
    mime: MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'vp9'
        : (MediaRecorder.isTypeSupported('video/webm') ? 'webm' : 'none'),
  }));

  check('the export panel is offered for a real job', info.exportShown);
  check('a recordable mime type is available', info.mime !== 'none', info.mime);
  check('the video has decoded dimensions', info.vw > 0 && info.vh > 0,
        `${info.vw}x${info.vh}, ${info.videoDuration.toFixed(1)}s`);
  console.log(`  clip: ${info.pages} pages / ${info.words} words / ${info.durationMs}ms`);

  console.log('  running export (plays the clip once, in real time)...');
  const t0 = Date.now();
  await page.click('#exportBtn');

  const outcome = await page
    .waitForFunction(
      () => {
        const link = document.getElementById('exportLink');
        const err = document.getElementById('exportError');
        // Read the `hidden` *property*, not the class: exportLink is toggled with
        // el.hidden while exportError carries a class, and a class check would
        // report success the instant the page loaded.
        if (link && !link.hidden) return { done: true };
        if (err && !err.classList.contains('hidden')) {
          return { done: true, error: err.textContent };
        }
        return false;
      },
      { timeout: 300000, polling: 1000 }
    )
    .then((h) => h.jsonValue());

  const elapsed = (Date.now() - t0) / 1000;

  if (outcome.error) {
    check('export completed', false, outcome.error);
  } else {
    check(`export completed in ${elapsed.toFixed(1)}s`, true, 'realtime capture + server encode');
    check('it really did play the clip rather than shortcutting it',
          elapsed >= info.videoDuration * 0.8,
          `${elapsed.toFixed(1)}s for a ${info.videoDuration.toFixed(1)}s clip`);
  }
  check('no page errors during export', errs.length === 0, errs.slice(0, 3).join(' | '));

  if (outcome.error) {
    console.log('\nFAILED: ' + failures.join('\n'));
    process.exit(1);
  }

  // Download in Node, straight from the URL the page produced.
  //
  // This deliberately does not round-trip the bytes through page.evaluate +
  // base64: a multi-megabyte file through String.fromCharCode chunks and back
  // out over CDP produced a truncated download that failed with "moov atom not
  // found", which looked exactly like an encoding failure and was not one.
  const href = await page.evaluate(() => document.getElementById('exportLink').href);
  const res = await fetch(href);
  check('the mp4 downloads', res.ok, `HTTP ${res.status}`);
  if (!res.ok) {
    console.log('\nFAILED: download');
    process.exit(1);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  writeFileSync(DL, buf);
  check('the mp4 is a plausible size', buf.length > 20000,
        `${(buf.length / 1e6).toFixed(2)} MB`);
  check('the mp4 has an mp4 container signature',
        buf.subarray(4, 8).toString('latin1') === 'ftyp',
        `bytes 4-8 = ${JSON.stringify(buf.subarray(4, 8).toString('latin1'))}`);
} catch (e) {
  check('the export harness ran to completion', false, e.message);
}

await browser.close();

// ---- verify the downloaded mp4 ------------------------------------------
if (failures.length === 0) {
  const report = ff(['-hide_banner', '-i', DL, '-f', 'null', '-']);
  check('ffmpeg can read the exported file', report.includes('Duration:'),
        report.slice(0, 200).replace(/\n/g, ' '));

  const dur = /Duration: (\d+):(\d+):([\d.]+)/.exec(report);
  const secs = dur ? +dur[1] * 3600 + +dur[2] * 60 + +dur[3] : 0;
  check('the duration is close to the source', secs > 3 && secs < 20,
        `${secs.toFixed(2)}s for a ~10s source`);
  check('the original audio is muxed in', /Audio:/.test(report),
        (/Audio: ([a-z0-9]+)/i.exec(report) || [])[1] || 'none');
  check('it is H.264 / yuv420p so browsers can play it',
        /h264/.test(report) && /yuv420p/.test(report),
        (/Video: ([^,]+)/.exec(report) || [])[1] || 'unknown');

  // The check that matters: are the captions really in the pixels?
  //
  // An earlier version compared the band's mean brightness during speech against
  // silence and demanded that speech be BRIGHTER. That is backwards for this
  // design -- the plate is a soft DARK translucent bar (spec 4), so it darkens
  // bright footage -- so measuring brightness reported a perfect export as
  // captionless. Assert the two things that are actually specified instead:
  // white text glyphs, and exactly one pill in the active speaker's colour.
  const BAND = 'crop=iw:ih*0.26:0:ih*0.66';

  const grab = (ss) => {
    const out = `${OUTDIR}\\burn_${String(ss).replace('.', '_')}.png`;
    ff(['-y', '-loglevel', 'error', '-ss', String(ss), '-i', DL, '-frames:v', '1', out]);
    return out;
  };

  // A timestamp guaranteed to have a caption page mid-word, plus the exact
  // colour that word's pill must be painted in.
  const caps = await (await fetch(`${BASE}/api/caption-jobs/${JOB}/captions`,
    { cache: 'no-store' })).json();
  const sample = caps.pages.find((p) => p.words.length >= 3);
  const word = sample.words[1];
  const spokenAt = ((word.startMs + word.endMs) / 2 / 1000).toFixed(2);
  const silentAt = '0.30';
  const style = caps.styles.find((s) => s.speakerId === sample.speakerId);
  const [pr, pg, pb] = hexToRgb(style.activeColor);

  const band = (ss) => {
    const px = rawPixels(DL, BAND, 'rgb24', ss);
    if (!px) return null;
    let pill = 0, white = 0;
    for (let i = 0; i + 2 < px.length; i += 3) {
      const d = Math.abs(px[i] - pr) + Math.abs(px[i + 1] - pg) + Math.abs(px[i + 2] - pb);
      if (d <= 36) pill++;
      if (px[i] > 225 && px[i + 1] > 225 && px[i + 2] > 225) white++;
    }
    return { pill, white, pixels: Math.floor(px.length / 3) };
  };

  const silentBand = band(silentAt);
  const spokenBand = band(spokenAt);

  check('frames can be extracted from the export',
        Boolean(silentBand && spokenBand),
        `${silentBand ? silentBand.pixels : 0}px per band`);

  // One pill, in the right colour, and only while a word is being spoken. This
  // is the regression guard for the CSS transition that used to leave several
  // words holding a faded pill at once.
  //
  // The silence baseline is compared relatively rather than against zero: the
  // footage itself contains teal/blue-grey tones that land within tolerance, so
  // "0 during silence" was never true. Speech has to be several times the
  // silent frame instead. The "never more than one word" half of the guarantee
  // is a DOM property and is asserted by probe_playback_pill.mjs.
  check('one active-word pill is burned in, only while speaking',
        Boolean(spokenBand && silentBand) &&
        spokenBand.pill > 300 && spokenBand.pill > silentBand.pill * 4,
        `${spokenBand ? spokenBand.pill : 0}px of ${style.activeColor} at t=${spokenAt}`
        + ` vs ${silentBand ? silentBand.pill : 0}px at t=${silentAt}`);

  check('the words are legible white text on the plate',
        Boolean(spokenBand) && spokenBand.white > 400,
        `${spokenBand ? spokenBand.white : 0} near-white px`);

  // And the footage itself must still be moving, not frozen.
  const frameA = meanGray(grab(1.5), 'scale=24:24,format=gray');
  const frameB = meanGray(grab((secs - 1).toFixed(2)), 'scale=24:24,format=gray');
  check('the exported video is not a frozen still',
        frameA && frameB && frameA.join(',') !== frameB.join(','),
        'two timestamps compared');
}

console.log();
if (failures.length) {
  console.log(`FAILED (${failures.length}):\n- ` + failures.join('\n- '));
  process.exit(1);
}
console.log('ALL EXPORT CHECKS PASSED');
console.log('mp4 at ' + DL);