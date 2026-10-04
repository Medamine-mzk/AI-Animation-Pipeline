import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE_URL = 'http://127.0.0.1:8000';
const JOB = process.argv[2] || 'ce2f201a';
// Negative control: put back the old fight -- a second writer that forces
// visible=true while walking, alongside the presence pass that hides them.
const INJECT = process.argv.includes('--inject-flicker');

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

try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message).slice(0, 140)));
  await page.setViewport({ width: 1500, height: 950 });
  await page.goto(`${BASE_URL}/dialogue-player.html?job=${JOB}`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForFunction(() => window.dialogueGaze, { timeout: 60000, polling: 500 });
  await new Promise(r => setTimeout(r, 4000));

  const dlg = await (await fetch(`${BASE_URL}/jobs/${JOB}/dialogue.json`, { cache: 'no-store' })).json();
  const walkins = (dlg.walkins || []).map(w => ({ speaker: w.speaker, t0: w.t0, t1: w.t1 }));
  const cast = Object.keys(dlg.speakers || {});
  check('job has a cast to observe', cast.length > 0, `${cast.length} characters`);

  if (INJECT) {
    // Recreate the original defect for real, and make it stick.
    //
    // Simply toggling armature.visible was not enough: applyStageVisibility()
    // runs every frame and silently repaired it, so the check passed even with a
    // competing writer present -- a control that cannot fail proves nothing. So
    // first suspend the single owner, then add the second writer that used to
    // exist (startWalkLerp forcing visible=true, the presence pass hiding).
    await page.evaluate(() => {
      window.__suspendStageVisibility(true);
      const flip = () => {
        for (const [id, av] of Object.entries(window.__stageAvatars || {})) {
          if (!av.armature) continue;
          av.armature.visible = !av.armature.visible;   // the old fight
        }
      };
      window.__flickerTimer = setInterval(flip, 150);
    });
  }

  await page.evaluate(() => document.getElementById('playBtn').click());

  // Watch the whole scene: visibility must only ever go false -> true, once per
  // character, and that change must land on that character's walk-in.
  const result = await page.evaluate(async (walks) => {
    const g0 = window.dialogueGaze();
    const prev = {}; const events = [];
    for (const r of g0.rows) prev[r.id] = r.meshVisible;
    for (let i = 0; i < 250; i++) {                 // ~100s at 400ms
      await new Promise(r => setTimeout(r, 400));
      for (const r of window.dialogueGaze().rows) {
        if (prev[r.id] !== r.meshVisible) {
          events.push({ id: r.id, on: r.meshVisible, at: +(i * 0.4).toFixed(1) });
          prev[r.id] = r.meshVisible;
        }
      }
    }
    return { events, walks };
  }, walkins);

  const onCount = result.events.filter(e => e.on).length;
  const offCount = result.events.filter(e => !e.on).length;

  check('nobody disappears once on stage', offCount === 0,
    offCount ? result.events.filter(e => !e.on).map(e => `${e.id}@${e.at}s`).join(', ') : 'none went hidden');
  check('characters only ever become visible', onCount >= 0, `${onCount} appearance(s)`);

  // Each appearance must coincide with that character's own entrance.
  for (const e of result.events.filter(x => x.on)) {
    const w = walkins.find(x => x.speaker === e.id);
    if (!w) continue;                                 // no walk scheduled: lead character
    const near = e.at >= w.t0 - 1.5 && e.at <= w.t1 + 2.0;
    check(`${e.id} appears during its entrance (t=${e.at}s, walk ${w.t0}-${w.t1}s)`, near);
  }

  // At the end everyone who should be on stage is.
  const final = await page.evaluate(() => window.dialogueGaze().rows.map(r => ({ id: r.id, v: r.meshVisible })));
  const endT = 250 * 0.4;
  for (const r of final) {
    const w = walkins.find(x => x.speaker === r.id);
    const shouldBeOn = !w || endT > w.t1;
    if (shouldBeOn) check(`${r.id} is on stage at the end`, r.v === true);
  }

  check('no page errors', errors.length === 0, errors.slice(0, 2).join(' | ') || 'none');
} finally {
  await browser.close();
}

console.log(failures ? `FAILED (${failures})` : 'ALL VISIBILITY CHECKS PASSED');
process.exit(failures ? 1 : 0);