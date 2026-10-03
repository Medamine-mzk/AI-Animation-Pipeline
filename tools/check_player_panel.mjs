import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE_URL = 'http://127.0.0.1:8000';
const JOB = process.argv[2] || 'ce2f201a';
// Negative control: put a removed control back and prove this check fails.
const INJECT = process.argv.includes('--inject-removed');

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

const GONE = ['audioInput', 'jsonInput', 'lipsyncMode', 'animMode', 'saveSpotsBtn', 'camHud', 'smoothStatus'];
const WANTED = ['playBtn', 'stopBtn', 'recordBtn', 'downloadBtn', 'cameraMode', 'smoothToggle', 'drawBoxBtn', 'hidePanelBtn'];

try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e.message).slice(0, 140)));
  await page.setViewport({ width: 1500, height: 950 });
  await page.goto(`${BASE_URL}/dialogue-player.html?job=${JOB}`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForFunction(() => window.dialogueModes, { timeout: 60000, polling: 500 });
  await new Promise(r => setTimeout(r, 3500));

  if (INJECT) {
    await page.evaluate(() => {
      const el = document.createElement('input');
      el.type = 'file';
      el.id = 'audioInput';
      document.getElementById('ui-panel').prepend(el);
    });
  }

  const dom = await page.evaluate((gone) => {
    const panel = document.getElementById('ui-panel');
    const vis = el => !!el && el.offsetParent !== null;
    const box = panel.getBoundingClientRect();
    return {
      width: Math.round(box.width),
      visibleIds: [...panel.querySelectorAll('input,select,button')].filter(vis).map(e => e.id).filter(Boolean),
      goneStillThere: gone.filter(id => !!document.getElementById(id)),
      modes: window.dialogueModes(),
      source: (document.getElementById('jobSource') || {}).textContent || '',
      stageVisible: vis(document.getElementById('drawBoxBtn')),
      fileInputs: panel.querySelectorAll('input[type="file"]').length,
      camText: (document.body.innerText.match(/cam \(-?\d/) || []).length,
    };
  }, GONE);

  // The panel was 760px wide and covered ~40% of the stage.
  check('panel is narrow', dom.width <= 420, `${dom.width}px (was 760px)`);

  // The point of the change: fewer controls, not the same ones rearranged.
  check('visible control count is 8', dom.visibleIds.length === 8, dom.visibleIds.join(', ') || 'none');
  check('exactly the intended controls', WANTED.every(id => dom.visibleIds.includes(id)),
    `missing: ${WANTED.filter(id => !dom.visibleIds.includes(id)).join(',') || 'none'}`);
  check('nothing extra is visible', dom.visibleIds.every(id => WANTED.includes(id)),
    `unexpected: ${dom.visibleIds.filter(id => !WANTED.includes(id)).join(',') || 'none'}`);

  check('no file pickers remain', dom.fileInputs === 0, `${dom.fileInputs} found`);
  check('removed controls are gone from the DOM', dom.goneStillThere.length === 0,
    dom.goneStillThere.join(', ') || 'none');
  check('no camera debug readout', dom.camText === 0);

  // Animation and lip-sync are now fixed, so they must be provable.
  check('animation is Full FBX', dom.modes.animMode === 'fbx', dom.modes.animMode);
  check('lip-sync is auto', dom.modes.lipsyncMode === 'auto', dom.modes.lipsyncMode);

  check('stage box is reachable, not hidden', dom.stageVisible === true);
  check('the job is named', /#\w+/.test(dom.source) && /speaker/.test(dom.source), JSON.stringify(dom.source.trim()));

  // Collapse and reopen.
  await page.evaluate(() => document.getElementById('hidePanelBtn').click());
  await new Promise(r => setTimeout(r, 600));
  const collapsed = await page.evaluate(() => ({
    hidden: document.getElementById('ui-panel').classList.contains('hidden'),
    toggleShown: document.getElementById('togglePanelBtn').offsetParent !== null,
    label: document.getElementById('togglePanelBtn').textContent.trim(),
  }));
  check('panel collapses', collapsed.hidden === true);
  check('a labelled button brings it back', collapsed.toggleShown === true, `label ${JSON.stringify(collapsed.label)}`);

  await page.evaluate(() => document.getElementById('togglePanelBtn').click());
  await new Promise(r => setTimeout(r, 600));
  const reopened = await page.evaluate(() => ({
    hidden: document.getElementById('ui-panel').classList.contains('hidden'),
    controls: [...document.querySelectorAll('#ui-panel input,#ui-panel select,#ui-panel button')].filter(e => e.offsetParent !== null).length,
  }));
  check('panel reopens', reopened.hidden === false && reopened.controls === 8, `${reopened.controls} controls`);

  check('no page errors', errors.length === 0, errors.slice(0, 2).join(' | ') || 'none');
  await page.close();

  // The status line must never claim Play is ready while the button is
  // disabled. With no job the avatars load but there is no audio, and the old
  // wording reported "Play enabled" next to a dead button.
  const noJob = await browser.newPage();
  await noJob.setViewport({ width: 1500, height: 950 });
  await noJob.goto(`${BASE_URL}/dialogue-player.html`, { waitUntil: 'networkidle2', timeout: 120000 });
  await new Promise(r => setTimeout(r, 6500));
  const nj = await noJob.evaluate(() => ({
    status: (document.getElementById('status') || {}).textContent || '',
    play: !document.getElementById('playBtn').disabled,
    pointsAtConfig: !!document.querySelector('#jobSource a'),
  }));
  check('status never claims ready while Play is disabled',
    !(nj.play === false && /play enabled/i.test(nj.status)),
    JSON.stringify(nj.status.trim().slice(0, 70)));
  check('a missing job points back to Dialogue Config', nj.pointsAtConfig === true);
  await noJob.close();
} finally {
  await browser.close();
}

console.log(failures ? `FAILED (${failures})` : 'ALL PANEL CHECKS PASSED');
process.exit(failures ? 1 : 0);