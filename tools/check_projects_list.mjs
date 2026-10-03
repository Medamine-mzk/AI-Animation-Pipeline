
import { pathToFileURL } from 'node:url';
const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const pp = (await import(pathToFileURL(PP).href)).default;
const browser = await pp.launch({executablePath: CHROME, headless:'new', args:['--no-sandbox']});
let fail=0; const check=(n,ok,d='')=>{console.log((ok?'PASS':'FAIL')+'  '+n+(d?'  '+d:'')); if(!ok)fail++;};
try {
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:8000/config.html', {waitUntil:'networkidle2', timeout:90000});
  await new Promise(r=>setTimeout(r,4000));
  const rows = await page.evaluate(()=>[...document.querySelectorAll('#projectsList > div')].map(d=>d.innerText.replace(/\n/g,' | ')));
  const target = rows.find(t=>t.includes('b171c19f'));
  check('b171c19f row present', !!target, target||'');
  check('row shows the DETECTED count 4', /4 spk/.test(target||''), target||'');
  check('no shortfall warning on a matched job', !/Audio only supports/.test(target||''));
  check('no red mismatch note', !/Dialogue is for another recording/.test(target||''));
  check('no backup folder listed as a project', !rows.some(t=>/_backup/.test(t)), rows.filter(t=>/_backup/.test(t)).join(' // '));
  // open the project so the editor toolbar renders
  await page.evaluate(()=>{ const el=[...document.querySelectorAll('#projectsList button')].find(b=>/open/i.test(b.textContent)); el&&el.click(); });
  await new Promise(r=>setTimeout(r,2500));
  const btn = await page.evaluate(()=>{const b=document.getElementById('remapBtn'); return b? b.textContent.trim():null;});
  check('button reads Re-detect, not Remap', !!btn && /^Re-detect/.test(btn), btn||'(not rendered)');
  const tofu = await page.evaluate(()=>document.body.innerText.includes('\uFFFD'));
  check('no replacement glyphs', !tofu);
} finally { await browser.close(); }
console.log(fail? `FAILED (${fail})` : 'ALL PROJECTS-LIST CHECKS PASSED');
process.exit(fail?1:0);
