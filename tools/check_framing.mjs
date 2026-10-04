import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE_URL = 'http://127.0.0.1:8000';
const JOB = process.argv[2] || 'ce2f201a';
// Negative control: restore the old fixed 0.7*scale + 0.50 offset, which framed
// ~0.61m of a ~0.73m character and cropped the body out of shot.
const INJECT = process.argv.includes('--inject-fixed-distance');

const pp = (await import(pathToFileURL(PP).href)).default;
const browser = await pp.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--mute-audio', '--autoplay-policy=no-user-gesture-required'],
});
let failures = 0;
const check = (n, ok, d = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? '  ' + d : ''}`);
  if (!ok) failures++;
};

// A character is fully in shot when its projected bounding box sits inside NDC
// (-1..1). Anything beyond that is outside the frustum and simply not drawn.
const MARGIN_NDC = 0.98;   // allow a little slack at the frame edge

try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message).slice(0, 140)));
  await page.setViewport({ width: 1500, height: 950 });
  await page.goto(`${BASE_URL}/dialogue-player.html?job=${JOB}`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForFunction(() => window.__measureAvatarHeight && window.dialogueGaze, { timeout: 60000, polling: 500 });
  await new Promise(r => setTimeout(r, 4500));

  const sizes = await page.evaluate(() => {
    const out = {};
    for (const r of window.dialogueGaze().rows) out[r.id] = window.__measureAvatarHeight(r.id);
    return out;
  });
  const ids = Object.keys(sizes);
  check('character height is measurable', ids.length > 0 && Object.values(sizes).every(h => h > 0),
    ids.map(i => `${i.replace('SPEAKER_', '')}=${sizes[i].toFixed(2)}m`).join(' '));

  if (INJECT) {
    // Put the old behaviour back for real: hold the camera at the fixed
    // 0.7*scale + 0.50 offset in front of the head, which is what cropped the
    // body before. This must make the frustum check fail.
    await page.evaluate(() => {
      window.__oldOffsetTimer = setInterval(() => {
        const rows = window.dialogueGaze().rows;
        const spk = rows.find(r => r.isSpeaker) || rows[0];
        if (!spk) return;
        const head = window.__headWorld(spk.id);
        const av = window.__stageAvatars()[spk.id];
        const cam = window.__cameraHandle && window.__cameraHandle();
        if (!head || !av || !av.armature || !cam) return;
        const s = av.armature.scale ? av.armature.scale.x : 1;
        cam.position.set(head.x + 0.12 * s, head.y + 0.12, head.z + (0.7 * s + 0.50));
        cam.lookAt(head.x, head.y + 0.06, head.z);
      }, 25);
    });
  }

  await page.evaluate(() => document.getElementById('playBtn').click());

  // Sample the camera against the cast and check each visible character is fully
  // inside the frustum.
  let cropped = [];
  let samples = 0;
  let seenChars = 0;
  for (let i = 0; i < 30 && samples < 10; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const snap = await page.evaluate(() => {
      const rows = window.dialogueGaze().rows;
      return rows.map(r => {
        const onStage = window.__stageAvatars()[r.id];
        const vis = !!(onStage && onStage.armature && onStage.armature.visible);
        return { id: r.id, isSpeaker: r.isSpeaker, visible: vis, ndc: window.__avatarNdc(r.id) };
      });
    });
    if (!snap.length) continue;
    samples++;
    for (const r of snap) {
      if (!r.visible || !r.ndc) continue;
      seenChars++;
      if (!r.ndc.fullyIn) {
        cropped.push(`${r.id}@sample${samples}[${r.ndc.minY.toFixed(2)}..${r.ndc.maxY.toFixed(2)}]`);
      }
    }
  }

  check('camera samples were collected', samples >= 3, `${samples} samples`);
  check('visible characters were inspected', seenChars > 0, `${seenChars} character-frames`);
  check('no visible character is cropped by the frame', cropped.length === 0,
    cropped.length ? `${cropped.length} cropped, e.g. ${cropped.slice(0, 3).join(' ')}` : 'all fully in frame');

  check('no page errors', errors.length === 0, errors.slice(0, 2).join(' | ') || 'none');
} finally {
  await browser.close();
}

console.log(failures ? `FAILED (${failures})` : 'ALL FRAMING CHECKS PASSED');
process.exit(failures ? 1 : 0);