import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE_URL = 'http://127.0.0.1:8000';
const JOB = process.argv[2] || 'b171c19f';
// How many speakers the panel must offer. Defaults to 4; overridden when testing
// a job whose speakers map is deliberately incomplete.
const EXPECTED = Number(process.argv[3] || 4);
// Negative control: re-create the stale second container that used to sit beside
// #lines, and prove this check can actually fail.
const INJECT = process.argv.includes('--inject-duplicate');

const pp = (await import(pathToFileURL(PP).href)).default;
const browser = await pp.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--mute-audio'],
});
let failures = 0;
const check = (n, ok, d = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? '  ' + d : ''}`);
  if (!ok) failures++;
};

try {
  const page = await browser.newPage();
  await page.goto(`${BASE_URL}/config.html?mode=wav&job=${JOB}`, { waitUntil: 'networkidle2', timeout: 90000 });
  await new Promise(r => setTimeout(r, 5000));

  if (INJECT) {
    await page.evaluate(() => {
      // exactly what renderWavResult used to do: a sibling container holding a
      // stale editor for a previously opened job.
      const stale = document.createElement('div');
      stale.id = 'wavResult';
      const bar = document.createElement('div');
      bar.innerHTML = `<button id="remapBtn">Re-detect 3 speakers</button><button>Save edit</button>`;
      stale.appendChild(bar);
      document.getElementById('lines').parentElement.appendChild(stale);
    });
  }

  const dom = await page.evaluate(() => {
    const vis = el => !!el && el.offsetParent !== null;
    const all = sel => [...document.querySelectorAll(sel)];
    return {
      staleContainer: !!document.getElementById('wavResult'),
      toolbars: all('#remapBtn').filter(vis).length,
      editorAddButtons: all('button').filter(b => vis(b) && /^\+ Add line$/i.test(b.textContent.trim())).length,
      saveEditButtons: all('button').filter(b => vis(b) && /^Save edit$/i.test(b.textContent.trim())).length,
      lineRows: all('#lines textarea').length,
      panelHeader: (all('#speakers div').map(d => d.textContent).find(t => /Editing wav job/.test(t)) || '').trim(),
      panelSpeakerBlocks: all('#speakers > div').filter(d => /SPEAKER_\d\d/.test(d.textContent)).length,
      panelSpeakerIds: [...new Set((all('#speakers select').map(s => s.dataset.sid || '').filter(Boolean)))].sort(),
      manualAddVisible: vis(document.getElementById('addLine')),
      manualSaveVisible: vis(document.getElementById('saveBtn')),
      emptyToggle: document.getElementById('toggleEmptyProjects')?.textContent?.trim() || null,
      listedRows: all('#projectsList > div').length,
      notReadyRows: all('#projectsList > div').filter(d => /not ready/.test(d.textContent)).length,
      url: location.search,
    };
  });

  check('no stale second editor container', !dom.staleContainer);
  check('exactly one editor toolbar', dom.toolbars === 1, `found ${dom.toolbars}`);
  check('exactly one "+ Add line"', dom.editorAddButtons === 1, `found ${dom.editorAddButtons}`);
  check('exactly one "Save edit"', dom.saveEditButtons === 1, `found ${dom.saveEditButtons}`);
  check('editor shows the job\'s lines', dom.lineRows > 0, `${dom.lineRows} rows`);

  // The reported bug: the panel said "Editing wav job 46065548" while a
  // 4-speaker job was on screen.
  check('panel describes the job on screen', dom.panelHeader.includes(JOB), dom.panelHeader || '(no header)');
  const declared = (dom.panelHeader.match(/\((\d+) speakers\)/) || [])[1];
  check('panel speaker count matches the API', declared === String(EXPECTED), `header says ${declared}`);
  check(`all ${EXPECTED} speakers are editable`, dom.panelSpeakerBlocks >= EXPECTED, `${dom.panelSpeakerBlocks} blocks`);

  check('manual-mode "+ Add Line" hidden in wav mode', !dom.manualAddVisible);
  check('manual-mode Save hidden in wav mode', !dom.manualSaveVisible);

  check('empty jobs collapsed behind a toggle', !!dom.emptyToggle, dom.emptyToggle || '(no toggle)');
  check('no "not ready" rows in the default list', dom.notReadyRows === 0, `${dom.notReadyRows} shown`);
  check('useful projects still listed', dom.listedRows > 0, `${dom.listedRows} rows`);
} finally {
  await browser.close();
}

console.log(failures ? `FAILED (${failures})` : 'ALL CONFIG-EDITOR CHECKS PASSED');
process.exit(failures ? 1 : 0);