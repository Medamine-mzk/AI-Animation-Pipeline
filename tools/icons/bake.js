/* Bake a curated word->emoji icon set to transparent PNGs using headless Chrome.
   Pillow cannot render color emoji (seguiemj is COLR/CPAL), so we draw the glyph
   on a transparent canvas in Chrome and export the PNG dataURL.

   Usage:
     node tools/icons/bake.js
       [--words tools/icons/dictionary.json]
       [--map jobs/golden/icon_map.json]
       [--out app/assets/icons]
       [--size 200]
       [--force]        # re-bake even if the PNG already exists
       [--port 8918]
*/

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..", "..");
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

// Reuse the existing node_modules under tools/live2d (puppeteer-core).
const { createRequire } = await import("node:module");
const require = createRequire(path.join(__dirname, "..", "live2d", "package.json"));
const puppeteer = require("puppeteer-core");

function arg(name, dflt) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

const WORDS_FILE = arg("--words", path.join(__dirname, "dictionary.json"));
const MAP_FILE = arg("--map", null);
const OUT_DIR = arg("--out", path.join(ROOT, "app", "assets", "icons"));
const SIZE = parseInt(arg("--size", "200"), 10);
const FORCE = process.argv.includes("--force");
const PORT = parseInt(arg("--port", "8918"), 10);

const WORDS = JSON.parse(fs.readFileSync(WORDS_FILE, "utf8"));
if (MAP_FILE) Object.assign(WORDS, JSON.parse(fs.readFileSync(MAP_FILE, "utf8")));

const server = http.createServer((req, res) => {
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
  let file = path.join(ROOT, p);
  if (!file.startsWith(ROOT)) { res.writeHead(403); res.end(); return; }
  if (!fs.existsSync(file)) { res.writeHead(404); res.end("not found"); return; }
  const ext = path.extname(file).toLowerCase();
  const MIME = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json" };
  res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

function keyFor(word) {
  return word.toLowerCase().replace(/[^a-z0-9_]/g, "_");
}

const BROWSER_SCALE = 2; // render at 2x for crisp edges, let PIL downscale

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  await new Promise((r) => server.listen(PORT, r));

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--allow-file-access-from-files"],
  });
  const page = await browser.newPage();
  await page.goto(`http://localhost:${PORT}/tools/icons/page.html`, { waitUntil: "networkidle0" });

  let baked = 0, cached = 0;
  for (const [word, emoji] of Object.entries(WORDS)) {
    const outPath = path.join(OUT_DIR, `${keyFor(word)}.png`);
    if (fs.existsSync(outPath) && !FORCE) { cached++; continue; }

    const dataUrl = await page.evaluate(({ emoji, SIZE, BROWSER_SCALE }) => {
      const canvas = document.createElement("canvas");
      canvas.width = SIZE * BROWSER_SCALE + 64;
      canvas.height = SIZE * BROWSER_SCALE + 64;
      const ctx = canvas.getContext("2d");
      ctx.font = `${SIZE * BROWSER_SCALE}px "Segoe UI Emoji", "Segoe UI Symbol", sans-serif`;
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";
      ctx.fillText(emoji, canvas.width / 2, canvas.height / 2);
      return canvas.toDataURL("image/png");
    }, { emoji, SIZE, BROWSER_SCALE });

    const buf = Buffer.from(dataUrl.split(",")[1], "base64");
    fs.writeFileSync(outPath, buf);
    baked++;
    console.log(`[icons]   ${word} -> ${keyFor(word)}.png`);
  }

  await browser.close();
  server.close();
  console.log(`[icons] baked ${baked} new, ${cached} cached -> ${OUT_DIR}`);
}

main().catch((e) => {
  console.error("[icons] FAILED:", e);
  process.exit(1);
});