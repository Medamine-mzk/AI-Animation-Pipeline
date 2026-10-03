
import { pathToFileURL } from 'node:url';
const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const JOB = process.argv[2];
const BASE_URL = 'http://127.0.0.1:8000';
const pp = (await import(pathToFileURL(PP).href)).default;
const browser = await pp.launch({executablePath: CHROME, headless:'new', args:['--no-sandbox','--mute-audio','--autoplay-policy=no-user-gesture-required']});
let fail = 0;
const check=(n,ok,d='')=>{console.log((ok?'PASS':'FAIL')+'  '+n+(d?'  '+d:'')); if(!ok)fail++;};
try {
  const api = await (await fetch(`${BASE_URL}/api/jobs/${JOB}`,{cache:'no-store'})).json();
  check('server reports detected count', api.speakers_detected === 4, 'detected=' + api.speakers_detected);
  check('server reports the request separately', api.speakers_expected === 4, 'expected=' + api.speakers_expected);
  check('count matched', api.speaker_count_matched === true);
  const page = await browser.newPage();
  await page.goto(`${BASE_URL}/dialogue-player.html?job=${JOB}`, {waitUntil:'networkidle2', timeout:90000});
  await new Promise(r=>setTimeout(r,6000));
  const dom = await page.evaluate(()=>({
    warn: document.getElementById('consistency-warning')?.hidden,
    warnText: (document.getElementById('consistency-warning')?.textContent||'').trim(),
    body: document.body.innerText,
  }));
  check('no mismatch banner', dom.warn === true, dom.warn ? '' : dom.warnText);
  check('player found the dialogue', !/No dialogue found/i.test(dom.body), dom.body.match(/No dialogue found[^\n]*/i)?.[0] || '');
  check('player reported no load error', !/\bError:\s/.test(dom.body), dom.body.match(/\bError:\s[^\n]*/)?.[0] || '');
  const tofu = dom.body.includes('\uFFFD');
  check('no replacement glyphs on screen', !tofu);
} finally { await browser.close(); }
console.log(fail ? `FAILED (${fail})` : 'ALL SPEAKER CHECKS PASSED');
process.exit(fail?1:0);
