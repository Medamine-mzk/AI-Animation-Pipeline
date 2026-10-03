import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE_URL = 'http://127.0.0.1:8000';
const JOB = process.argv[2] || 'ce2f201a';

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
  // Read the dialogue first: presence is reported per loaded avatar, so the
  // check must wait for the whole cast rather than probing mid-load.
  const dlg = await (await fetch(`${BASE_URL}/jobs/${JOB}/dialogue.json`, { cache: 'no-store' })).json();
  const expected = Object.keys(dlg.speakers || {}).length;
  const firsts = {};
  for (const s of dlg.segments || []) {
    if (s.speaker && !(s.speaker in firsts)) firsts[s.speaker] = s.start;
  }
  const speaking = Object.keys(firsts);
  check('job has speakers to check', speaking.length > 0, `${speaking.length} speaking, ${expected} in cast`);

  const page = await browser.newPage();
  await page.goto(`${BASE_URL}/dialogue-player.html?job=${JOB}`, { waitUntil: 'networkidle2', timeout: 120000 });
  // Wait for every avatar, not just the first: loadAvatars fetches GLBs one by one.
  let loaded = 0;
  try {
    await page.waitForFunction(
      n => window.dialoguePresenceAt && Object.keys(window.dialoguePresenceAt(0)).length >= n,
      { timeout: 120000, polling: 500 }, expected);
    loaded = expected;
  } catch (e) {
    loaded = await page.evaluate(() => (window.dialoguePresenceAt ? Object.keys(window.dialoguePresenceAt(0)).length : 0));
    check('all avatars load', false, `only ${loaded} of ${expected} loaded`);
  }
  if (loaded >= expected) check('all avatars load', true, `${loaded} loaded`);

  // Sample inside each speaker's first line, not on its boundary.
  const probes = Object.entries(firsts).map(([spk, t]) => [spk, Number(t) + 0.3]);
  const presence = await page.evaluate(ts => ts.map(([spk, t]) => ({ spk, t, on: window.dialoguePresenceAt(t)[spk] })), probes);

  for (const p of presence) {
    check(`${p.spk} is on stage while speaking (t=${p.t}s)`, p.on === true);
  }

  // Everyone who has already been introduced should also still be there: a
  // character must not vanish once someone else starts talking.
  const lastFirst = Math.max(...Object.values(firsts));
  const late = await page.evaluate(t => window.dialoguePresenceAt(t), lastFirst + 0.5);
  const missingLater = Object.keys(late).filter(k => late[k] === false);
  check('nobody disappears after their entrance', missingLater.length === 0,
    missingLater.length ? `hidden: ${missingLater.join(', ')}` : `all ${Object.keys(late).length} present`);

  // And the player must still respect entrances: nobody before their walk-in.
  const entrances = dlg.walkins || [];
  const early = await page.evaluate(ts => ts.map(([spk, t]) => ({ spk, on: window.dialoguePresenceAt(t)[spk] })),
    entrances.map(w => [w.speaker, Math.max(0, w.t0 - 0.4)]));
  const wronglyEarly = early.filter(e => e.on === true);
  check('nobody appears before their entrance', wronglyEarly.length === 0,
    wronglyEarly.length ? `early: ${wronglyEarly.map(e => e.spk).join(', ')}` : `${early.length} checked`);
} finally {
  await browser.close();
}

console.log(failures ? `FAILED (${failures})` : 'ALL AVATAR-VISIBILITY CHECKS PASSED');
process.exit(failures ? 1 : 0);