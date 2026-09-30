/* Deterministic headless plate renderer for a Cubism 2.1 model.
   Exposes window.__l2d: { ready:true, model, render(params) } used by bake.js. */

(function () {
  const W = 1024, H = 2048;
  const RESET = [
    "PARAM_ANGLE_X", "PARAM_ANGLE_Y", "PARAM_ANGLE_Z",
    "PARAM_EYE_L_OPEN", "PARAM_EYE_L_SMILE", "PARAM_EYE_R_OPEN", "PARAM_EYE_R_SMILE",
    "PARAM_EYE_FORM", "PARAM_EYE_BALL_X", "PARAM_EYE_BALL_Y",
    "PARAM_BROW_L_Y", "PARAM_BROW_R_Y", "PARAM_BROW_L_X", "PARAM_BROW_R_X",
    "PARAM_BROW_L_ANGLE", "PARAM_BROW_R_ANGLE", "PARAM_BROW_L_FORM", "PARAM_BROW_R_FORM",
    "PARAM_MOUTH_FORM", "PARAM_MOUTH_OPEN_Y",
    "PARAM_TERE", "PARAM_TEAR", "PARAM_SWEAT", "PARAM_RAGE",
    "PARAM_BODY_ANGLE_X", "PARAM_BODY_ANGLE_Z", "PARAM_BODY_ANGLE_Y",
    "PARAM_ARM_L", "PARAM_ARM_R", "PARAM_BREATH",
    "PARAM_HAIR_FRONT", "PARAM_HAIR_SIDE", "PARAM_HAIR_SIDE_L", "PARAM_HAIR_SIDE_R",
    "PARAM_HAIR_BACK", "PARAM_HAIR_BACK_L", "PARAM_HAIR_BACK_R",
  ];

  async function main() {
    const q = new URLSearchParams(location.search);
    const modelPath = q.get("model") || "Epsilon/Epsilon.model.json";
    const scale = parseFloat(q.get("scale") || "0");
    const cx = parseFloat(q.get("cx") || "512");
    const cy = parseFloat(q.get("cy") || "1024");

    const app = new PIXI.Application({
      view: document.getElementById("stage"),
      width: W,
      height: H,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: false,
      autoStart: false,
      powerPreference: "high-performance",
    });
    app.stop();

    const model = await PIXI.live2d.Live2DModel.from(modelPath, {
      autoUpdate: false,
      autoInteract: false,
    });
    app.stage.addChild(model);

    const s = scale > 0 ? scale : 1;
    model.anchor.set(0.5, 0.5);
    model.scale.set(s, s);
    model.position.set(cx, cy);
    model.elapsedTime = 16;
    model.deltaTime = 16;

    const core = model.internalModel.coreModel;
    function setParam(id, v) {
      try { core.setParamFloat(id, v); } catch (e) {}
    }

function render(params) {
      for (const id of RESET) setParam(id, 0);
      for (const [id, v] of Object.entries(params)) setParam(id, v);
      model.deltaTime = 16;
      model.update(16);
      app.renderer.render(app.stage);
    }

    function bbox(params) {
      render(params);
      const c = document.createElement("canvas");
      c.width = W; c.height = H;
      const ctx = c.getContext("2d");
      ctx.drawImage(document.getElementById("stage"), 0, 0);
      const data = ctx.getImageData(0, 0, W, H).data;
      let x0 = W, y0 = H, x1 = -1, y1 = -1;
      const n = W * H;
      for (let i = 0; i < n; i++) {
        if (data[i * 4 + 3] > 8) {
          const x = i % W, y = (i / W) | 0;
          if (x < x0) x0 = x; if (x > x1) x1 = x;
          if (y < y0) y0 = y; if (y > y1) y1 = y;
        }
      }
      return x1 < 0 ? null : { x0, y0, x1, y1 };
    }

    window.__l2d = { ready: true, model, app, render, bbox, RESET, mw: model.internalModel.width, mh: model.internalModel.height, paramIds: core.getParamIndexMap ? Object.keys(core.getParamIndexMap()) : [],
    hasParam: (id) => { try { return core.getParamIndex(id) >= 0; } catch (e) { return false; } } };
    console.log("L2D ready:", modelPath, "scale", s, "at", cx, cy);
  }

  window.addEventListener("DOMContentLoaded", () => {
    main().catch((e) => {
      console.error("L2D init failed:", e);
      window.__l2dError = String(e && e.stack || e);
    });
  });
})();
