import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const JOB = process.argv[2] || '75d3e27f';
const BASE_URL = 'http://127.0.0.1:8000';

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
  // What the server says first, so the assertions below check the *right*
  // direction: a repaired job must be silent, a mismatched one must warn.
  const api = await (await fetch(`${BASE_URL}/api/jobs/${JOB}`, { cache: 'no-store' })).json();
  const expectMismatch = api && api.consistency && api.consistency.ok === false;
  console.log(`  job ${JOB}: server says ${expectMismatch ? 'MISMATCH' : 'consistent'} `
            + `(audio ${api.consistency.audioS}s, dialogue ends ${api.consistency.dialogueEndS}s)`);
  console.log(`  expecting: ${expectMismatch ? 'a warning' : 'no warning'}`);
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 900 });
  // Without this the browser serves a cached config.html, which silently tested
  // an older revision of the page: the Fix button was present while the mismatch
  // banner -- added in a later edit -- was not.
  await page.setCacheEnabled(false);
  const errs = [];
  // Track the URL, not the console text: Chrome's "Failed to load resource"
  // message does not name the resource, so a 404 can only be classified from
  // the response itself. favicon is noise, and provenance.json legitimately does
  // not exist for jobs predating it -- which is exactly the case where the
  // measured consistency check has to carry the warning instead.
  const EXPECTED_404 = /favicon\.ico|provenance\.json/;
  let checkingResponses = true;
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('response', (r) => {
    if (!checkingResponses || r.status() < 400) return;
    const u = r.url();
    if (EXPECTED_404.test(u)) return;
    errs.push(`HTTP ${r.status()}  ${u.replace(BASE_URL, '')}`);
  });
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const t = m.text();
    if (EXPECTED_404.test(t) || /Failed to load resource/.test(t)) return;
    errs.push('console: ' + t.slice(0, 160));
  });

  // --- config.html: does it show the mismatch banner?
  await page.goto(`http://127.0.0.1:8000/config.html?job=${JOB}&mode=wav`, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 4000));
  const banners = await page.evaluate(() =>
    Array.from(document.querySelectorAll('.honest-banner')).map((b) => b.textContent.trim().slice(0, 130)));
  check('config.html loads with no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
  check('config.html agrees with the server',
        expectMismatch ? banners.some((b) => /mismatch/i.test(b)) : banners.length === 0,
        banners.join(' || ') || 'no banner');

  const fixBtn = await page.evaluate(() =>
    !!document.querySelector('button[data-act="refix"]'));
  check('the Fix button appears only on a mismatched job', fixBtn === expectMismatch, 'fixBtn=' + fixBtn);

  // --- dialogue-player.html: does it show the banner too?
  errs.length = 0;
  await page.goto(`http://127.0.0.1:8000/dialogue-player.html?job=${JOB}`, { waitUntil: 'domcontentloaded' });
  await new Promise((r) => setTimeout(r, 12000));
  const warn = await page.evaluate(() => {
    const el = document.getElementById('consistency-warning');
    return { hidden: el ? el.hidden : null, text: el ? el.textContent.trim().slice(0, 160) : '(no element)' };
  });
  check('dialogue-player loads with no page errors', errs.length === 0, errs.slice(0, 2).join(' | '));
  check('the player agrees with the server',
        expectMismatch ? warn.hidden === false : warn.hidden === true, warn.text);
} finally {
  await browser.close();
}
console.log(failures ? `\nFAILED (${failures})` : '\nALL CHECKS PASSED');
process.exit(failures ? 1 : 0);