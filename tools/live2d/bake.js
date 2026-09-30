/* Bake 18 viseme plates (A-H,X x eyes open/closed) from a Cubism 2.1 model
   using headless Chrome + Pixi + pixi-live2d-display.

   Usage:
     node bake.js [--model media/2d/live2d_collection/Epsilon/Epsilon.model.json]
                  [--out jobs/spike2d/epsilon]
                  [--framing tools/live2d/framing.json]
[--visemes tools/live2d/viseme_map.json]
                   [--only A_open]      # re-bake a single plate
                   [--autofit]          # measure content bbox, write framing.json, then bake
                   [--port 8917]
*/

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const puppeteer = require("puppeteer-core");

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..", "..");
const W = 1024, H = 2048;
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

function arg(name, dflt) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}

const MODEL = arg("--model", "/media/2d/live2d_collection/Epsilon/Epsilon.model.json");
const OUT = arg("--out", "jobs/spike2d/epsilon");
const FRAMING_FILE = arg("--framing", path.join(__dirname, "framing.json"));
const VISEMES_FILE = arg("--visemes", path.join(__dirname, "viseme_map.json"));
const ONLY = arg("--only", null);
const AUTOFIT = process.argv.includes("--autofit");
const PORT = parseInt(arg("--port", "8917"), 10);

const MIME = {
  ".html": "text/html", ".js": "text/javascript", ".json": "application/json",
  ".png": "image/png", ".jpg": "image/jpeg", ".moc": "application/octet-stream",
  ".mtn": "text/plain", ".exp.json": "application/json", ".css": "text/css",
  ".svg": "image/svg+xml",
};

const server = http.createServer((req, res) => {
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
  let file = path.join(ROOT, p);
  if (!file.startsWith(ROOT)) { res.writeHead(403); res.end(); return; }
  if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, "index.html");
  if (!fs.existsSync(file)) { res.writeHead(404); res.end("not found"); return; }
  const ext = path.extname(file).toLowerCase();
  res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

const VISEMES = JSON.parse(fs.readFileSync(VISEMES_FILE, "utf8"));
const FRAMING = JSON.parse(fs.readFileSync(FRAMING_FILE, "utf8"));

const names = [];
for (const v of "ABCDEFGHX") {
  names.push(`${v}_open`);
  names.push(`${v}_closed`);
}

let effVisemes = {};
let hasEyeL = false, hasEyeR = false;

function paramsFor(plate) {
  const [viseme, eye] = plate.split("_");
  const p = Object.assign({}, effVisemes[viseme] || {});
  if (hasEyeL) p["PARAM_EYE_L_OPEN"] = eye === "open" ? 1 : 0;
  if (hasEyeR) p["PARAM_EYE_R_OPEN"] = eye === "open" ? 1 : 0;
  return p;
}

async function main() {
  await new Promise((r) => server.listen(PORT, r));
  const base = `http://127.0.0.1:${PORT}/tools/live2d/page.html`;

  let framing = FRAMING;
  if (AUTOFIT) framing = { scale: 1, cx: 512, cy: 1024 };

  const url = `${base}?model=${encodeURIComponent(MODEL)}&scale=${framing.scale}&cx=${framing.cx}&cy=${framing.cy}`;

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: "new",
    args: ["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader-webgl", "--enable-unsafe-swiftshader"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 });

  console.log("loading model:", MODEL, "framing:", FRAMING);
  await page.goto(url, { waitUntil: "networkidle0", timeout: 120000 });

  await page.waitForFunction("window.__l2d && window.__l2d.ready", { timeout: 60000 }).catch(async () => {
    const err = await page.evaluate("window.__l2dError");
    throw new Error("model load failed: " + err);
  });
const mw = await page.evaluate("window.__l2d.mw"); const mh = await page.evaluate("window.__l2d.mh"); console.log("model ready, intrinsic w/h =", mw, mh);

  const effVisemes0 = {};
  for (const [v, ps] of Object.entries(VISEMES)) {
    const e = {};
    for (const [id, val] of Object.entries(ps)) {
      if (await page.evaluate((i) => window.__l2d.hasParam(i), id)) e[id] = val;
    }
    effVisemes0[v] = e;
  }
  effVisemes = effVisemes0;
  hasEyeL = await page.evaluate((i) => window.__l2d.hasParam(i), "PARAM_EYE_L_OPEN");
  hasEyeR = await page.evaluate((i) => window.__l2d.hasParam(i), "PARAM_EYE_R_OPEN");
  console.log("has eye params:", hasEyeL, hasEyeR);
  const usable = Object.values(effVisemes).some((ps) => Object.keys(ps).length > 0);
  if (!usable) {
    console.log("ALL PARAMS unavailable (no mouth/eye params)");
    throw new Error("model has no usable viseme params (MOUTH_OPEN_Y/MOUTH_FORM absent)");
  }
  console.log("effective viseme params:", Object.fromEntries(Object.entries(effVisemes).map(([v, ps]) => [v, Object.keys(ps)])));

  if (AUTOFIT) {
    let mscale = 1, bb = null;
    for (const attempt of [1, 0.5, 0.25, 0.125]) {
      if (attempt !== mscale) {
        const u = `${base}?model=${encodeURIComponent(MODEL)}&scale=${attempt}&cx=512&cy=1024`;
        await page.goto(u, { waitUntil: "networkidle0", timeout: 120000 });
        await page.waitForFunction("window.__l2d && window.__l2d.ready", { timeout: 60000 });
        mscale = attempt;
      }
      bb = await page.evaluate((ps) => window.__l2d.bbox(ps), paramsFor("X_open"));
      if (!bb) throw new Error("autofit: empty render bbox");
      const clipped = bb.x0 <= 1 || bb.x1 >= W - 1 || bb.y0 <= 1 || bb.y1 >= H - 1;
      console.log(`autofit measure scale=${attempt} bbox=${JSON.stringify(bb)} clipped=${clipped}`);
      if (!clipped) break;
    }
    if (bb.x0 <= 1 || bb.x1 >= W - 1 || bb.y0 <= 1 || bb.y1 >= H - 1) {
      throw new Error("autofit: content still clipped at smallest scale — cannot frame");
    }
    const mx0 = (bb.x0 - 512) / mscale, mx1 = (bb.x1 - 512) / mscale;
    const my0 = (bb.y0 - 1024) / mscale, my1 = (bb.y1 - 1024) / mscale;
    const Wc = mx1 - mx0, Hc = my1 - my0;
    const s = Math.min((W * 0.90) / Wc, (H * 0.90) / Hc);
    framing = {
      scale: Number(s.toFixed(4)),
      cx: Number((512 - ((mx0 + mx1) / 2) * s).toFixed(1)),
      cy: Number((1024 - ((my0 + my1) / 2) * s).toFixed(1)),
    };
    fs.writeFileSync(FRAMING_FILE, JSON.stringify(framing, null, 2));
    console.log("autofit -> framing:", framing);
    const url2 = `${base}?model=${encodeURIComponent(MODEL)}&scale=${framing.scale}&cx=${framing.cx}&cy=${framing.cy}`;
    await page.goto(url2, { waitUntil: "networkidle0", timeout: 120000 });
    await page.waitForFunction("window.__l2d && window.__l2d.ready", { timeout: 60000 });
    console.log("reloaded with new framing");
  }

  const outDir = path.join(ROOT, OUT);
  fs.mkdirSync(outDir, { recursive: true });

  const toBake = ONLY ? [ONLY] : names;
  const plates = {};
  for (const name of toBake) {
    const params = paramsFor(name);
    const png = await page.evaluate((ps) => {
      window.__l2d.render(ps);
      const c = document.getElementById("stage");
      return c.toDataURL("image/png");
    }, params);
    const file = path.join(outDir, `${name}.png`);
    fs.writeFileSync(file, Buffer.from(png.split(",")[1], "base64"));
    plates[name] = `${name}.png`;
    console.log("wrote", file);
  }

  if (!ONLY) {
    fs.writeFileSync(path.join(outDir, "plates.json"), JSON.stringify({
      layout: "plates",
      character: path.basename(path.dirname(MODEL)),
      model: MODEL,
      visemes: effVisemes,
      framing,
      size: [W, H],
      plates,
    }, null, 2));
  }

  await browser.close();
  server.close();
  console.log("done ->", outDir);
}

main().catch((e) => { console.error(e); server.close(); process.exit(1); });



