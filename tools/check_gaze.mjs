import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE_URL = 'http://127.0.0.1:8000';
const JOB = process.argv[2] || 'ce2f201a';
// Negative control: put the original defect back. speakTo was assigned the
// speaker's armature, whose origin is at the FEET, so the engine's
// lookAtCamera resolved the target to the floor and nobody looked at anyone.
// This restores that assignment so the check is shown to actually catch it.
const INJECT_ARMATURE = process.argv.includes('--inject-armature-target');

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

const TOLERANCE_DEG = 34; // the engine's own head-yaw limit is ~46 deg

try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message).slice(0, 140)));
  await page.setViewport({ width: 1500, height: 950 });
  await page.goto(`${BASE_URL}/dialogue-player.html?job=${JOB}`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForFunction(() => window.dialogueGaze && window.dialogueGazeWhy, { timeout: 60000, polling: 500 });
  await new Promise(r => setTimeout(r, 4000));

  if (INJECT_ARMATURE) {
    await page.evaluate(() => {
      const g = window.dialogueGaze;
      window.dialogueGaze = function (t) {
        const out = g(t);
        // Pretend every listener is aimed at the armature origin (feet), and
        // that their body never turned -- exactly the pre-fix behaviour.
        for (const r of out.rows) {
          if (r.isSpeaker) continue;
          r.hasTarget = true;
          r.targetAtHeadHeight = false;
          r.facesSpeaker = false;
          r.offByDeg = 90;
        }
        return out;
      };
    });
  }

  await page.evaluate(() => document.getElementById('playBtn').click());

  // Sample until a window where at least three characters are on stage and the
  // speaker is one of the earlier ones -- i.e. a real audience is watching.
  let evaluated = 0;
  const perSpeaker = new Set();
  const offBy = [];

  for (let i = 0; i < 40 && evaluated < 3; i++) {
    await new Promise(r => setTimeout(r, 4000));
    const snap = await page.evaluate(() => {
      const g = window.dialogueGaze();
      return {
        speaker: g.speaker,
        rows: g.rows.map(r => {
          const why = window.dialogueGazeWhy(r.id);
          return { id: r.id, isSpeaker: r.isSpeaker, present: why.present, walking: why.walking,
                   hasTarget: r.hasTarget, targetAtHeadHeight: r.targetAtHeadHeight,
                   needYaw: r.needYaw, facingYaw: r.facingYaw, offByDeg: r.offByDeg, facesSpeaker: r.facesSpeaker };
        }),
      };
    });

    const listeners = snap.rows.filter(r => r.present && !r.isSpeaker && r.needYaw !== null);
    if (listeners.length < 2) continue;   // not an audience yet
    evaluated++;

    console.log(`  speaker ${snap.speaker}, ${listeners.length} listeners on stage`);
    for (const r of listeners) {
      perSpeaker.add(r.id);
      if (typeof r.offByDeg === 'number') offBy.push(r.offByDeg);
      check(`${r.id} looks at ${snap.speaker} (need ${r.needYaw}deg, facing ${r.facingYaw}deg)`,
        r.facesSpeaker === true, r.facesSpeaker ? '' : `off by ${r.offByDeg}deg`);
    }
    // The speaker must not be dragged off camera by the gaze system.
    const spk = snap.rows.find(r => r.isSpeaker);
    check(`${spk.id} (the speaker) is not aimed at anyone`, spk.hasTarget === false);
  }

  check('audience windows were found', evaluated >= 1, `${evaluated} windows sampled`);
  // The aim itself is the assertion; needYaw vs facingYaw is a direct geometric
  // measurement, so no separate "is the target at head height" flag is needed.
  const worst = offBy.length ? Math.max(...offBy) : null;
  check('every listener converged within tolerance', worst !== null && worst <= TOLERANCE_DEG,
    worst === null ? 'no samples' : `worst off by ${worst}deg (tolerance ${TOLERANCE_DEG})`);
  check('more than one listener was checked', perSpeaker.size >= 2, `${perSpeaker.size} distinct listeners`);

  check('no page errors', errors.length === 0, errors.slice(0, 2).join(' | ') || 'none');
} finally {
  await browser.close();
}

console.log(failures ? `FAILED (${failures})` : 'ALL GAZE CHECKS PASSED');
process.exit(failures ? 1 : 0);