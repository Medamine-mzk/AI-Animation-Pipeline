#!/usr/bin/env python3
"""
show_background.py — generate an interactive viewer for a background GLB
so the user can mouse-navigate to find the correct camera + spots.

Usage:
  python tools/show_background.py --model assets/background/living_room_with_curtains.glb --out jobs/rooms/living_curtains_viewer.html
"""
import argparse
from pathlib import Path

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Background placement — __LABEL__</title>
<style>
html,body{margin:0;height:100%;background:#0d0d10;color:#ddd;font-family:"Segoe UI",Arial,sans-serif;overflow:hidden}
#app{position:absolute;inset:0}
#hud{position:absolute;top:12px;left:12px;z-index:10;background:rgba(20,20,26,.92);border:1px solid #333;border-radius:8px;padding:10px 12px;font-size:13px;min-width:360px;max-width:520px}
#hud h3{margin:0 0 8px;font-size:14px;color:#8cf}
#hud code{background:#15151a;padding:1px 4px;border-radius:4px;font-size:12px}
#hud button{font-size:13px;padding:4px 10px;margin-top:6px}
.row{margin-top:6px;word-break:break-all}
#hint{position:absolute;bottom:12px;left:12px;z-index:10;background:rgba(20,20,26,.78);border:1px solid #333;border-radius:8px;padding:6px 10px;font-size:12px;color:#999}
#spotsList{max-height:120px;overflow:auto;margin-top:6px;background:#0f0f14;border:1px solid #222;border-radius:6px;padding:6px;font-size:12px}
.spot{cursor:pointer;padding:2px 4px;border-radius:4px}
.spot:hover{background:#1e1e2a}
</style>
</head>
<body>
<div id="app"></div>
<div id="hud">
<h3>Background — __LABEL__ <span style="font-weight:normal;color:#999">(__SRC__)</span></h3>
<div class="row">Cam pos: <code id="camPos">-</code></div>
<div class="row">Target (look_at): <code id="camTarget">-</code></div>
<div class="row">FOV: <code id="camFov">45</code>° | dist: <code id="camDist">-</code></div>
<div class="row">Click floor → spot <code id="lastSpot">none</code></div>
<div class="row" style="display:flex;gap:6px;flex-wrap:wrap">
  <button id="btnCopyCam">Copy camera JSON</button>
  <button id="btnFront">Front view</button>
  <button id="btnReset">Fit</button>
  <button id="btnClear">Clear spots</button>
  <button id="btnSave">Save (S)</button>
</div>
<div id="spotsList">spots: <em>Shift+Click floor to add (max 6). Click entry to remove.</em></div>
<div class="row" style="color:#8a8;font-size:11px;margin-top:8px">Floor grid 10cm | drag=orbit, wheel=zoom, right-drag=pan, Shift+Click=add spot</div>
<div id="status" style="margin-top:6px;color:#8cf;font-size:12px">loading…</div>
</div>
<div id="hint">After you like the view, hit <b>Copy camera JSON</b> and paste the numbers back to me</div>
<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
const MODEL_URL = "__MODEL_URL__";
const app=document.getElementById("app");
const statusEl=document.getElementById("status");
const camPosEl=document.getElementById("camPos"), camTargetEl=document.getElementById("camTarget"), camFovEl=document.getElementById("camFov"), camDistEl=document.getElementById("camDist");
const lastSpotEl=document.getElementById("lastSpot"), spotsListEl=document.getElementById("spotsList");
const renderer=new THREE.WebGLRenderer({antialias:true});
renderer.setPixelRatio(window.devicePixelRatio); renderer.setSize(innerWidth,innerHeight); renderer.toneMapping=THREE.ACESFilmicToneMapping;
renderer.outputColorSpace=THREE.SRGBColorSpace;
app.appendChild(renderer.domElement);
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x0d0d10);
const hemi=new THREE.HemisphereLight(0xffffff,0x222233,1.6); scene.add(hemi);
const amb=new THREE.AmbientLight(0xffffff,0.45); scene.add(amb);
const key=new THREE.DirectionalLight(0xfff2dd,3.2); key.position.set(5,8,4); scene.add(key);
const fill=new THREE.DirectionalLight(0xbdd2ff,1.1); fill.position.set(-5,5,-3); scene.add(fill);
const grid=new THREE.GridHelper(20,200,0x2a2a35,0x1c1c24); scene.add(grid);
const axes=new THREE.AxesHelper(1.2); scene.add(axes);
const camera=new THREE.PerspectiveCamera(45, innerWidth/innerHeight, 0.01, 5000);
camera.position.set(4,3,6);
const controls=new OrbitControls(camera, renderer.domElement); controls.target.set(0,0,0); controls.maxDistance=200;
const loader=new GLTFLoader();
const modelGroup=new THREE.Group(); scene.add(modelGroup);
const spotsGroup=new THREE.Group(); scene.add(spotsGroup);
let spots=[];
let lastBox=null,lastCenter=null;
function fitCameraTo(obj) {
  const box=new THREE.Box3().setFromObject(obj);
  const size=box.getSize(new THREE.Vector3()); const center=box.getCenter(new THREE.Vector3());
  lastBox=box.clone(); lastCenter=center.clone();
  const r=Math.max(size.x,size.y,size.z)/2;
  const dist=Math.max(r*2.8+0.8,2.2);
  camera.position.set(center.x, center.y + r*0.35 + 1.1, center.z + dist);
  controls.target.copy(center); controls.target.y = center.y*0.2 + 0.2;
  controls.update(); updateHUD();
}
function frontView() {
  if(!lastBox){fitCameraTo(modelGroup);return;}
  const size=lastBox.getSize(new THREE.Vector3()); const r=Math.max(size.x,size.y,size.z)/2;
  const dist=Math.max(r*2.8+0.8,2.2);
  camera.position.set(lastCenter.x, lastCenter.y + r*0.30 + 0.9, lastCenter.z + dist);
  controls.target.copy(lastCenter); controls.target.y = lastCenter.y*0.2 + 0.2;
  controls.update(); updateHUD();
}
function updateHUD(){
  const p=camera.position, t=controls.target;
  camPosEl.textContent=`(${p.x.toFixed(3)}, ${p.y.toFixed(3)}, ${p.z.toFixed(3)})`;
  camTargetEl.textContent=`(${t.x.toFixed(3)}, ${t.y.toFixed(3)}, ${t.z.toFixed(3)})`;
  camFovEl.textContent=camera.fov.toFixed(1);
  camDistEl.textContent=p.distanceTo(t).toFixed(3);
}
function refreshSpotsList(){
  if(!spots.length){ spotsListEl.innerHTML='spots: <em>Shift+Click floor to add (max 6). Click entry to remove.</em>'; return;}
  spotsListEl.innerHTML = spots.map((s,i)=>`<div class="spot" data-i="${i}">#${i+1} x:${s.x.toFixed(3)} y:${s.y.toFixed(3)} z:${s.z.toFixed(3)} yaw:${s.yaw.toFixed(0)}°</div>`).join('');
  spotsListEl.querySelectorAll('.spot').forEach(el=>el.addEventListener('click',()=>{ spots.splice(parseInt(el.dataset.i),1); rebuildSpots(); refreshSpotsList(); }));
}
function rebuildSpots(){
  spotsGroup.clear();
  spots.forEach((s,i)=>{
    const cyl=new THREE.Mesh(new THREE.CylinderGeometry(0.25,0.25,1.75,16), new THREE.MeshStandardMaterial({color:0xff3c3c, transparent:true, opacity:0.85}));
    cyl.position.set(s.x, s.y+0.875, s.z);
    spotsGroup.add(cyl);
    const arrow=new THREE.Mesh(new THREE.ConeGeometry(0.09,0.35,8), new THREE.MeshStandardMaterial({color:0xffeb3b}));
    const yawR=THREE.MathUtils.degToRad(s.yaw);
    arrow.position.set(s.x+0.4*Math.cos(yawR), s.y+0.95, s.z+0.4*Math.sin(yawR));
    arrow.rotation.x=Math.PI/2; arrow.rotation.z=-yawR;
    spotsGroup.add(arrow);
  });
}
const ray=new THREE.Raycaster(); const mouse=new THREE.Vector2(); const plane=new THREE.Plane(new THREE.Vector3(0,1,0),0);
renderer.domElement.addEventListener('click',(e)=>{
  if(!e.shiftKey) return;
  const rect=renderer.domElement.getBoundingClientRect();
  mouse.x=((e.clientX-rect.left)/rect.width)*2-1; mouse.y=-((e.clientY-rect.top)/rect.height)*2+1;
  ray.setFromCamera(mouse,camera);
  const hit=new THREE.Vector3();
  if(ray.ray.intersectPlane(plane,hit)===null) return;
  let yaw=0;
  if(spots.length){ const cx=spots.reduce((a,s)=>a+s.x,0)/spots.length; const cz=spots.reduce((a,s)=>a+s.z,0)/spots.length; yaw = Math.atan2(cz - hit.z, cx - hit.x) * 180/Math.PI + 180; }
  else { yaw = 180 + Math.atan2(controls.target.z - hit.z, controls.target.x - hit.x)*180/Math.PI; }
  const s={x: hit.x, y: 0, z: hit.z, yaw: ((yaw%360)+360)%360};
  spots.push(s);
  if(spots.length>6) spots.shift();
  lastSpotEl.textContent=`(${s.x.toFixed(3)}, ${s.y.toFixed(3)}, ${s.z.toFixed(3)}) yaw ${s.yaw.toFixed(1)}°`;
  rebuildSpots(); refreshSpotsList();
});
controls.addEventListener('change',updateHUD);
window.addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth,innerHeight);});
document.getElementById('btnCopyCam').addEventListener('click',async()=>{
  const payload={camera:{position:{x:+camera.position.x.toFixed(3),y:+camera.position.y.toFixed(3),z:+camera.position.z.toFixed(3)}, target:{x:+controls.target.x.toFixed(3),y:+controls.target.y.toFixed(3),z:+controls.target.z.toFixed(3)}}, fov:+camera.fov.toFixed(1), distance:+camera.position.distanceTo(controls.target).toFixed(3), spots: spots.map(s=>({x:+s.x.toFixed(3),y:+s.y.toFixed(3),z:+s.z.toFixed(3),yaw:+s.yaw.toFixed(1)})) };
  const txt=JSON.stringify(payload,null,2);
  await navigator.clipboard.writeText(txt).catch(()=>{});
  statusEl.textContent='camera+spots copied to clipboard — paste to me';
  console.log(txt);
});
document.getElementById('btnFront').addEventListener('click',frontView);
document.getElementById('btnReset').addEventListener('click',()=>fitCameraTo(modelGroup));
document.getElementById('btnClear').addEventListener('click',()=>{spots=[]; rebuildSpots(); refreshSpotsList(); lastSpotEl.textContent='none';});
document.getElementById('btnSave').addEventListener('click',()=>{
  const payload={camera:{position:{x:+camera.position.x.toFixed(3),y:+camera.position.y.toFixed(3),z:+camera.position.z.toFixed(3)}, target:{x:+controls.target.x.toFixed(3),y:+controls.target.y.toFixed(3),z:+controls.target.z.toFixed(3)}}, fov:+camera.fov.toFixed(1), distance:+camera.position.distanceTo(controls.target).toFixed(3), spots};
  const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='living_curtains_viewer.json'; a.click();
});
window.addEventListener('keydown',e=>{ if(e.key.toLowerCase()==='s') document.getElementById('btnSave').click(); });
async function load() {
  statusEl.textContent='loading __LABEL__…';
  try {
    const gltf=await loader.loadAsync(MODEL_URL);
    modelGroup.add(gltf.scene);
    fitCameraTo(modelGroup);
    statusEl.textContent='__LABEL__ loaded — Shift+Click floor to place girl/mom';
    updateHUD();
  } catch(e) { statusEl.textContent='error: '+e.message; console.error(e); }
}
function animate(){ requestAnimationFrame(animate); controls.update(); renderer.render(scene,camera); }
load(); animate();
</script>
</body>
</html>
"""

def main():
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser(description="Generate background-only viewer")
    ap.add_argument("--model", required=True, help="path to background .glb")
    ap.add_argument("--out", default=None, help="output html path")
    args = ap.parse_args()
    src = Path(args.model)
    if not src.exists():
        print(f"model not found: {src}")
        raise SystemExit(2)
    label = src.stem
    out = Path(args.out) if args.out else Path("jobs/rooms") / f"{label}_viewer.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    model_url = f"/{src.as_posix()}"
    html = TEMPLATE.replace("__LABEL__", label).replace("__SRC__", src.as_posix()).replace("__MODEL_URL__", model_url)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}  model_url={model_url}")

if __name__ == "__main__":
    main()
