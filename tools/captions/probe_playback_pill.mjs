/**
 * Sample the active-word state while the video is actually PLAYING, which is the
 * only condition the export ever runs under.
 *
 * The paused/seek probe already proved one word is active and the pill colour
 * resolves. But .w carries a CSS transition on background-color, so during
 * playback a word that was active a moment ago is still fading out while the new
 * one fades in -- several words hold a non-transparent background at the same
 * time. This reports the worst case over a full pass of the clip.
 *
 * Usage: node probe_playback_pill.mjs <cap_job_id>
 */
import { pathToFileURL } from 'node:url';

const REPO = 'E:/PROJECTS/AI Animation Pipeline';
const PP = `${REPO}/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js`;
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const JOB = process.argv[2];

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

  const result = await page.evaluate(async () => {
    const v = document.getElementById('video');
    const opaque = (c) => c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent';

    const worst = { painted: 0, at: -1, words: [] };
    let samples = 0;
    let activeMissing = 0;
    const transitions = new Set();

    // Watch the very same selector the canvas loop uses.
    const observer = new MutationObserver(() => {});
    const spansAll = document.querySelectorAll('#words .w');
    for (const s of spansAll) {
      const cs = getComputedStyle(s);
      if (cs.transitionProperty && cs.transitionProperty !== 'all' &&
          cs.transitionDuration && cs.transitionDuration.split(',')[0] !== '0s') {
        transitions.add(cs.transitionProperty.trim());
      }
    }

    const sampler = () => {
      const spans = Array.from(document.querySelectorAll('#words .w'));
      const painted = [];
      let activeCount = 0;
      for (const s of spans) {
        if (s.classList.contains('active')) activeCount++;
        const cs = getComputedStyle(s);
        if (opaque(cs.backgroundColor)) painted.push({ text: s.textContent, bg: cs.backgroundColor });
      }
      samples++;
      if (activeCount === 0) activeMissing++;
      if (painted.length > worst.painted) {
        worst.painted = painted.length;
        worst.at = v.currentTime * 1000;
        worst.words = painted;
      }
    };

    const iv = setInterval(sampler, 16);
    v.currentTime = 0;
    await new Promise((res) => {
      const on = () => { v.removeEventListener('ended', on); res(); };
      v.addEventListener('ended', on);
      v.play();
    });
    clearInterval(iv);
    observer.disconnect();

    return {
      durationS: v.duration,
      samples,
      framesWithNoActiveWord: activeMissing,
      transitionProperties: Array.from(transitions),
      worst,
    };
  });

  console.log(JSON.stringify(result, null, 2));
  console.log(`\nworst simultaneous painted words: ${result.worst.painted} at t=${result.worst.at.toFixed(0)}ms`);
  if (result.worst.painted > 1) {
    console.log('CONFIRMED  more than one word holds a pill colour at once -> CSS transition bleed');
    process.exitCode = 1;
  } else {
    console.log('PASS  only ever one painted word during playback');
  }
} finally {
  await browser.close();
}