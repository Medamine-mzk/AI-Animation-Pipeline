/**
 * Regenerate every screenshot used in README.md.
 *
 * The README is a competition dossier, so its images have to be real captures of
 * this code running, not mockups -- and they have to be reproducible, or they
 * quietly rot. This script drives the real pages in a real browser and writes
 * into docs/screenshots/.
 *
 * Usage:  node tools/capture_screenshots.mjs
 * Requires the server to be running on 127.0.0.1:8000.
 *
 * Override the jobs used with env vars if your own jobs are more representative:
 *   CAPTIONS_LANDSCAPE_JOB, CAPTIONS_PORTRAIT_JOB, PIPELINE_JOB
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const REPO = 'E:/PROJECTS/AI Animation Pipeline';
const PP = `${REPO}/tools/live2d/node_modules/puppeteer-core/lib/cjs/puppeteer/puppeteer-core.js`;
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const FFMPEG = `${REPO}/tools/ffmpeg/ffmpeg.exe`;
const BASE = 'http://127.0.0.1:8000';
const OUT = `${REPO}/docs/screenshots`;

const LAND_JOB = process.env.CAPTIONS_LANDSCAPE_JOB || 'cap_3e90deac';
const PORTRAIT_JOB = process.env.CAPTIONS_PORTRAIT_JOB || 'cap_b5fabbc6';
const PIPE_JOB = process.env.PIPELINE_JOB || '0b779d1b';

mkdirSync(OUT, { recursive: true });

const pp = (await import(pathToFileURL(PP).href)).default;
const browser = await pp.launch({
  executablePath: CHROME,
  headless: 'new',
  args: [
    '--no-sandbox', '--mute-audio',
    '--autoplay-policy=no-user-gesture-required',
    // Software GL, so the WebGL/Three.js player still renders in headless.
    '--use-gl=swiftshader', '--enable-unsafe-swiftshader',
  ],
});

const written = [];
async function shot(page, name) {
  const path = `${OUT}\\${name}.png`;
  await page.screenshot({ path });
  written.push(name);
  console.log('  ' + name);
}

/** Park the playhead inside the first caption page so the plate is on screen. */
const seekIntoCaption = (pageIndex) => `async (pi) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pages = captionsApp.state.captions.pages;
  const p = pages[pi] || pages[0];
  const t = (p.words[0].startMs + p.words[0].endMs) / 2;
  if (captionsApp.state.hasVideo) document.getElementById('video').currentTime = t / 1000;
  else captionsApp.state.timeMs = t;
  await sleep(700);
  return p.words.map((w) => w.text.trim()).join(' ');
}`

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 900, deviceScaleFactor: 1 });
  page.on('pageerror', (e) => console.log('  [pageerror]', e.message));

  const open = async (path, waitVideo) => {
    await page.goto(BASE + path, { waitUntil: 'networkidle2', timeout: 40000 });
    await page.waitForFunction(() => window.captionsApp && window.captionsApp.state.captions,
                               { timeout: 20000 });
    if (waitVideo) {
      await page.waitForFunction(() => window.captionsApp.state.hasVideo, { timeout: 25000 });
    }
    await new Promise((r) => setTimeout(r, 2500));
  };

  // 1. Landing page, full height so the whole flow is visible.
  console.log('captions flow:');
  await page.goto(BASE + '/', { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1200));
  await shot(page, '01-accueil');

  // 2. Intake card with the recent-video list.
  await open('/captions.html', false);
  await shot(page, '02-captions-intake');

  // 3. Landscape preview, mid-caption.
  await open('/captions.html?job=' + LAND_JOB, true);
  await page.evaluate(seekIntoCaption(3));
  await shot(page, '03-captions-apercu-paysage');

  // 4. Portrait preview -- the vertical case that the stage has to frame.
  await open('/captions.html?job=' + PORTRAIT_JOB, true);
  await page.evaluate(seekIntoCaption(4));
  await shot(page, '04-captions-apercu-portrait');

  // 5. Transcript editor.
  await open('/captions.html?job=' + LAND_JOB, true);
  await page.evaluate(seekIntoCaption(3));
  const editor = await page.$('#editorCard');
  if (editor) {
    await editor.screenshot({ path: `${OUT}/05-captions-transcription.png` });
    written.push('05-captions-transcription');
    console.log('  05-captions-transcription');
  }

  // 6. Export result: the finished mp4 playing in the page.
  await open('/captions.html?job=' + LAND_JOB, true);
  await new Promise((r) => setTimeout(r, 3000));
  const hasExport = await page.evaluate(
    () => !document.getElementById('exportResult').hidden);
  const exportCard = await page.$('#exportCard');
  if (exportCard) {
    await exportCard.screenshot({ path: `${OUT}/06-captions-export.png` });
    written.push('06-captions-export');
    console.log('  06-captions-export' + (hasExport ? ' (with finished video)' : ' (no export yet)'));
  }

  // 7 + 8. Real frames lifted out of the two exported mp4s.
  console.log('exported video frames:');
  for (const [job, name, at] of [
    [LAND_JOB, '07-export-paysage', 2.87],
    [PORTRAIT_JOB, '08-export-portrait', 9.6],
  ]) {
    const mp4 = `${REPO}/jobs_captions/${job}/export.mp4`;
    try {
      execFileSync(FFMPEG, ['-v', 'error', '-y', '-ss', String(at), '-i', mp4,
                            '-frames:v', '1', `${OUT}/${name}.png`]);
      written.push(name);
      console.log('  ' + name + '  (' + job + ')');
    } catch (e) {
      console.log('  skipped ' + name + ': ' + e.message.split('\n')[0]);
    }
  }

// 9 + 10. The 3D pipeline.
  //
  // Captured in a *headed* browser on purpose. The room background is an 87 MB
  // GLB and headless Chrome falls back to software GL, where it never finishes
  // uploading in time -- the screenshot catches the bare ground plane instead of
  // the room. A real GPU renders it, which is also what a user actually sees.
  console.log('3D pipeline (headed, for real GPU rendering):');
  await page.close();
  const gpu = await pp.launch({
    executablePath: CHROME,
    headless: false,
    args: ['--no-sandbox', '--mute-audio', '--autoplay-policy=no-user-gesture-required'],
  });
  const g = await gpu.newPage();
  await g.setViewport({ width: 1400, height: 900, deviceScaleFactor: 1 });
  try {
    await g.goto(BASE + `/config.html?mode=wav&job=${PIPE_JOB}`, { waitUntil: 'networkidle2' });
    await new Promise((r) => setTimeout(r, 4000));
    const path = `${OUT}/09-configuration-3d.png`;
    await g.screenshot({ path });
    written.push('09-configuration-3d');
    console.log('  09-configuration-3d');

    await g.goto(BASE + `/dialogue-player.html?job=${PIPE_JOB}`,
                 { waitUntil: 'domcontentloaded', timeout: 60000 });
    // Wait until the background model is actually in the scene, rather than
    // guessing with a fixed sleep.
    try {
      await g.waitForFunction(
        () => {
          const c = document.querySelector('canvas');
          return !!c && !/BG: none/.test(document.body.innerText || '');
        },
        { timeout: 120000, polling: 1000 });
    } catch (e) {
      console.log('  (background did not report in time, capturing anyway)');
    }
    await new Promise((r) => setTimeout(r, 8000));
    await g.evaluate(() => {
      const play = document.getElementById('playBtn');
      if (play) { try { play.click(); } catch (e) {} }
    });
    await new Promise((r) => setTimeout(r, 6000));
    await g.evaluate(() => {
      const hide = document.getElementById('hidePanelBtn');
      if (hide) { try { hide.click(); } catch (e) {} }
    });
    await new Promise((r) => setTimeout(r, 3000));
    const p10 = `${OUT}/10-dialogue-3d.png`;
    await g.screenshot({ path: p10 });
    written.push('10-dialogue-3d');
    console.log('  10-dialogue-3d');
  } finally {
    await gpu.close();
  }
} finally {
  await browser.close();
}

console.log('\n' + written.length + ' screenshots written to docs/screenshots/');