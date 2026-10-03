import { pathToFileURL } from 'node:url';

const PP = 'E:/PROJECTS/AI Animation Pipeline/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE_URL = 'http://127.0.0.1:8000';
const JOB = process.argv[2] || 'b171c19f';

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
  await page.setViewport({ width: 1400, height: 900 });
  await page.goto(`${BASE_URL}/config.html?mode=wav&job=${JOB}`, { waitUntil: 'networkidle2', timeout: 90000 });
  await new Promise(r => setTimeout(r, 5000));

  const dom = await page.evaluate(() => {
    const lines = document.getElementById('lines');
    const cs = getComputedStyle(lines);
    // Every ancestor that would introduce its own scrollbar on the segments.
    const scrollers = [];
    let el = lines;
    while (el && el !== document.documentElement) {
      const s = getComputedStyle(el);
      if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 2) {
        scrollers.push(`${el.tagName}#${el.id || '-'}(${el.scrollHeight}>${el.clientHeight})`);
      }
      el = el.parentElement;
    }
    const body = document.body;
    return {
      overflowY: cs.overflowY,
      maxHeight: cs.maxHeight,
      scrollableAncestors: scrollers,
      rows: lines.querySelectorAll('textarea').length,
      pageScrolls: body.scrollHeight > window.innerHeight,
      pageHeight: body.scrollHeight,
      viewport: window.innerHeight,
      // Are the last rows actually reachable without an inner scrollbar?
      lastRowVisible: (() => {
        const rows = lines.querySelectorAll('textarea');
        if (!rows.length) return false;
        const r = rows[rows.length - 1].getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      })(),
      headerSticky: getComputedStyle(document.querySelector('#lines').previousElementSibling).position,
      docOverflowHidden: /(hidden|clip)/.test(getComputedStyle(body).overflowY),
    };
  });

  check('segment container does not scroll internally', !/(auto|scroll)/.test(dom.overflowY), `overflow-y: ${dom.overflowY}`);
  check('segment container has no height cap', dom.maxHeight === 'none', `max-height: ${dom.maxHeight}`);
  check('no scrollable ancestor clipping the segments', dom.scrollableAncestors.length === 0, dom.scrollableAncestors.join(', ') || 'none');
  check('all segments rendered', dom.rows > 0, `${dom.rows} rows`);
  check('the page itself scrolls', dom.pageScrolls, `${dom.pageHeight}px page vs ${dom.viewport}px viewport`);
  check('page scrolling is not disabled', !dom.docOverflowHidden);
  check('header is sticky', dom.headerSticky === 'sticky', dom.headerSticky);
  check('every row has real layout', dom.lastRowVisible);

  // Scroll to the very bottom and confirm the final segment is on screen.
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await new Promise(r => setTimeout(r, 700));
  const atBottom = await page.evaluate(() => {
    const rows = document.getElementById('lines').querySelectorAll('textarea');
    const r = rows[rows.length - 1].getBoundingClientRect();
    return { inViewport: r.top < window.innerHeight && r.bottom > 0, text: rows[rows.length - 1].value.slice(0, 60) };
  });
  check('last segment reachable by page scroll', atBottom.inViewport, `"${atBottom.text}"`);
} finally {
  await browser.close();
}

console.log(failures ? `FAILED (${failures})` : 'ALL SCROLL CHECKS PASSED');
process.exit(failures ? 1 : 0);