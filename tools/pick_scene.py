#!/usr/bin/env python3
"""
pick_scene.py — blocking picker for camera + character spots.
Opens an interactive viewer for the chosen background, lets the user drag-drop
3D characters (man/woman/boy/girl) and orbit the camera, then blocks until
the user clicks "Confirm" and writes the result for the pipeline.

Usage:
  python tools/pick_scene.py --background assets/background/living_room_with_curtains.glb --characters 4
  python tools/pick_scene.py --background assets/background/living_room_with_curtains.glb --script jobs/golden/script.json
  python tools/pick_scene.py --background assets/background/test-background.glb --characters 2 --port 8000
"""
import argparse
import json
import pathlib
import sys
import webbrowser
import threading
import http.server
import socketserver
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parents[1]

# Map logical roles to GLB files (boy/girl are children)
MODEL_MAP = {
    "man": "assets/man.glb",
    "woman": "assets/woman.glb",
    "boy": "assets/man1.glb",
    "girl": "assets/woman1.glb",
}
# Fallback order for generic characters: girl, mom, dad, boy
DEFAULT_ORDER = ["girl", "woman", "man", "boy"]

TEMPLATE_PICKER = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Picker — __LABEL__ (__COUNT__ chars)</title>
<style>
html,body{margin:0;height:100%;background:#0d0d10;color:#ddd;font-family:"Segoe UI",Arial,sans-serif;overflow:hidden}
#app{position:absolute;inset:0}
#hud{position:absolute;top:12px;left:12px;z-index:10;background:rgba(20,20,26,.96);border:1px solid #333;border-radius:8px;padding:10px 12px;font-size:13px;min-width:420px;max-width:560px}
#hud h3{margin:0 0 8px;font-size:14px;color:#8cf}
#hud code{background:#15151a;padding:1px 4px;border-radius:4px;font-size:12px}
#hud button{font-size:13px;padding:5px 10px;margin-top:6px;cursor:pointer}
#hud button.primary{background:#2a5bd7;color:#fff;border:1px solid #3a6be7;border-radius:6px}
.row{margin-top:6px;word-break:break-all}
#charList{max-height:160px;overflow:auto;margin-top:6px;background:#0f0f14;border:1px solid #222;border-radius:6px;padding:6px;font-size:12px}
.char{padding:4px 6px;border-radius:4px;margin:2px 0;background:#1a1a22;display:flex;justify-content:space-between;align-items:center}
.char.selected{background:#24365a;border:1px solid #3a6be7}
.char small{color:#999}
#hint{position:absolute;bottom:12px;left:12px;z-index:10;background:rgba(20,20,26,.78);border:1px solid #333;border-radius:8px;padding:6px 10px;font-size:12px;color:#999}
#status{margin-top:6px;color:#8cf;font-size:12px;min-height:14px}
</style>
</head>
<body>
<div id="app"></div>
<div id="hud">
<h3>Picker — __LABEL__ <span style="font-weight:normal;color:#999">__SRC__</span> — <span id="countBadge">__COUNT__ chars</span></h3>
<div class="row">Background: <select id="bgSelect"></select> Light: <select id="lightSelect"><option value="L1">L1 dim</option><option value="L2" selected>L2 natural</option><option value="L3">L3 bright</option><option value="L4">L4 over</option><option value="L5">L5 warm</option><option value="L6">L6 cool</option></select></div>
<div class="row">Cam pos: <code id="camPos">-</code></div>
<div class="row">Target: <code id="camTarget">-</code> | FOV <code id="camFov">45</code>° dist <code id="camDist">-</code></div>
<div class="row" style="display:flex;gap:6px;flex-wrap:wrap">
  <button id="btnFront">Front</button>
  <button id="btnFit">Fit</button>
  <button id="btnAdd">+ Add</button>
  <button id="btnRemove">- Remove</button>
  <button id="btnConfirm" class="primary">✓ Confirm & Save (Enter)</button>
</div>
<div id="palette" style="display:none; margin-top:8px; background:#1a1a22; border:1px solid #333; border-radius:6px; padding:8px;">
  <div style="color:#8cf; font-size:12px; margin-bottom:6px;">Choose person to place:</div>
  <div id="paletteBtns" style="display:flex; gap:6px; flex-wrap:wrap;"></div>
  <button id="btnCancelPalette" style="margin-top:6px; background:#333; color:#ddd; border:1px solid #444;">Cancel</button>
</div>
<div id="charList"></div>
<div class="row" style="color:#8a8;font-size:11px">Add → choose person → click floor to place → click again to set facing • Drag to move • Yellow ring to rotate</div>
<div id="status">loading…</div>
</div>
<div id="hint">Pick camera + drag characters, then <b>Confirm</b> to continue pipeline</div>
<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { DragControls } from "three/addons/controls/DragControls.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";
const MODEL_URL = "__MODEL_URL__";
const CHAR_MODELS = __CHAR_MODELS_JSON__;
const INITIAL_SPOTS = __INITIAL_SPOTS_JSON__;
const BG_LIST = __BG_LIST_JSON__;
let LIGHT_PRESETS = {L1:{hemi:0.8,amb:0.25,key:1.5,fill:0.6},L2:{hemi:1.6,amb:0.45,key:3.2,fill:1.1},L3:{hemi:1.8,amb:0.55,key:3.8,fill:1.4},L4:{hemi:2.2,amb:0.7,key:4.5,fill:1.8},L5:{hemi:1.6,amb:0.45,key:3.0,fill:1.0},L6:{hemi:1.6,amb:0.45,key:3.2,fill:1.2}};
let currentLight="L2";
fetch('/tools/lighting_presets.json').then(r=>r.json()).then(j=>{
  const m=j.meta||{};
  const scale=m.three_scale||7.8;
  for(const k of Object.keys(j)){ if(k==='meta') continue; const v=j[k]; LIGHT_PRESETS[k]={hemi:v.hemi/scale, amb:v.amb, key:v.key/scale, fill:v.fill/scale}; }
  // refresh current light intensities if already set
  const p=LIGHT_PRESETS[currentLight]; if(p){ hemi.intensity=p.hemi; amb.intensity=p.amb; key.intensity=p.key; fill.intensity=p.fill; }
}).catch(()=>{});
const PIPELINE_ROLES = __ROLES_JSON__;
const app=document.getElementById("app");
const statusEl=document.getElementById("status");
const camPosEl=document.getElementById("camPos"), camTargetEl=document.getElementById("camTarget"), camFovEl=document.getElementById("camFov"), camDistEl=document.getElementById("camDist");
const charListEl=document.getElementById("charList");
const renderer=new THREE.WebGLRenderer({antialias:true});
renderer.setPixelRatio(window.devicePixelRatio); renderer.setSize(innerWidth,innerHeight); renderer.toneMapping=THREE.ACESFilmicToneMapping; renderer.outputColorSpace=THREE.SRGBColorSpace;
app.appendChild(renderer.domElement);
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x0d0d10);
const hemi=new THREE.HemisphereLight(0xffffff,0x222233,1.6); scene.add(hemi);
const amb=new THREE.AmbientLight(0xffffff,0.45); scene.add(amb);
const key=new THREE.DirectionalLight(0xfff2dd,3.2); key.position.set(5,8,4); scene.add(key);
const fill=new THREE.DirectionalLight(0xbdd2ff,1.1); fill.position.set(-5,5,-3); scene.add(fill);
scene.add(new THREE.GridHelper(20,200,0x2a2a35,0x1c1c24));
scene.add(new THREE.AxesHelper(1.2));
// background selector + light brightness
const bgSelect=document.getElementById('bgSelect');
BG_LIST.forEach(p=>{ const o=document.createElement('option'); o.value='/'+p; o.textContent=p.split('/').pop(); if('/'+p===MODEL_URL) o.selected=true; bgSelect.appendChild(o); });
bgSelect.addEventListener('change', async ()=>{
  const url=bgSelect.value;
  statusEl.textContent='loading '+url+'…';
  modelGroup.clear();
  try{ const gltf=await loader.loadAsync(url); modelGroup.add(gltf.scene); fitCameraTo(modelGroup); statusEl.textContent=url+' loaded — Shift+Click to place'; updateHUD(); } catch(e){ statusEl.textContent='error: '+e.message; console.error(e); }
});
const lightSelect=document.getElementById('lightSelect');
lightSelect.value=currentLight;
lightSelect.addEventListener('change',()=>{
  currentLight=lightSelect.value;
  const p=LIGHT_PRESETS[currentLight];
  hemi.intensity=p.hemi; amb.intensity=p.amb; key.intensity=p.key; fill.intensity=p.fill;
  statusEl.textContent='light '+currentLight+' — hemi '+p.hemi+' amb '+p.amb;
});
const camera=new THREE.PerspectiveCamera(45, innerWidth/innerHeight, 0.01, 5000);
camera.position.set(4,3,6);
const controls=new OrbitControls(camera, renderer.domElement); controls.target.set(0,0,0); controls.maxDistance=200;
const loader=new GLTFLoader();
const modelGroup=new THREE.Group(); scene.add(modelGroup);
const charGroup=new THREE.Group(); scene.add(charGroup);
let chars=[]; // {id, role, glb, group, yaw}
let selectedIdx=-1;
let dragControls, transformControls;
const plane=new THREE.Plane(new THREE.Vector3(0,1,0),0);
const ray=new THREE.Raycaster(); const mouse=new THREE.Vector2();
let pendingRole=null; // role chosen from palette, awaiting first click placement
let pendingSpot=null; // {x,z} after first click, awaiting second click for yaw
let ghostGroup=null;

function updateHUD(){
  const p=camera.position, t=controls.target;
  camPosEl.textContent=`(${p.x.toFixed(3)}, ${p.y.toFixed(3)}, ${p.z.toFixed(3)})`;
  camTargetEl.textContent=`(${t.x.toFixed(3)}, ${t.y.toFixed(3)}, ${t.z.toFixed(3)})`;
  camFovEl.textContent=camera.fov.toFixed(1);
  camDistEl.textContent=p.distanceTo(t).toFixed(3);
}
function refreshList(){
  if(!chars.length){ charListEl.innerHTML='<em>No characters — Add</em>'; return;}
  charListEl.innerHTML=chars.map((c,i)=>`<div class="char ${i===selectedIdx?'selected':''}" data-i="${i}"><span><b>${c.role}</b> <small>${c.id}</small><br><small>x:${c.group.position.x.toFixed(2)} z:${c.group.position.z.toFixed(2)} yaw:${c.yaw.toFixed(0)}°</small></span><span><button data-act="focus" data-i="${i}">Focus</button></span></div>`).join('');
  charListEl.querySelectorAll('.char').forEach(el=>{
    el.addEventListener('click',()=>{ selectIdx(parseInt(el.dataset.i)); });
  });
  charListEl.querySelectorAll('[data-act="focus"]').forEach(btn=>{
    btn.addEventListener('click',(e)=>{ e.stopPropagation(); const i=parseInt(btn.dataset.i); const c=chars[i]; controls.target.set(c.group.position.x,0,c.group.position.z); controls.update(); updateHUD(); });
  });
}
function selectIdx(i){
  selectedIdx=i;
  if(transformControls) transformControls.detach();
  if(i>=0 && chars[i]){
    transformControls.attach(chars[i].group);
  }
  refreshList();
}
function makeChar(id, role, glb, x, z, yaw){
  const group=new THREE.Group();
  group.position.set(x,0,z);
  group.rotation.y=THREE.MathUtils.degToRad(yaw);
  const cyl=new THREE.Mesh(new THREE.CylinderGeometry(0.26,0.26,0.08,16), new THREE.MeshStandardMaterial({color: role==='girl'||role==='woman'?0xff6ea8:0x6ea8ff, transparent:true, opacity:0.35}));
  cyl.position.y=0.04; group.add(cyl);
  const label=document.createElement('canvas'); // placeholder for text sprite
  // try load real GLB as child (ensure absolute URL from repo root)
  const glbUrl = glb.startsWith("/") ? glb : "/" + glb;
  loader.load(glbUrl, (gltf)=>{
    const m=gltf.scene.clone();
    m.traverse(o=>{ if(o.isMesh){ o.castShadow=true; }});
    // normalize scale to ~1.75m height
    const box=new THREE.Box3().setFromObject(m);
    const size=box.getSize(new THREE.Vector3());
    const scale=1.7 / Math.max(size.y,0.5);
    m.scale.setScalar(scale);
    // recenter bottom at y=0
    const box2=new THREE.Box3().setFromObject(m);
    m.position.y -= box2.min.y;
    // small offset so cylinder ring visible
    group.add(m);
  }, undefined, ()=>{
    // fallback: cone arrow
    const arrow=new THREE.Mesh(new THREE.ConeGeometry(0.12,0.5,8), new THREE.MeshStandardMaterial({color:0xffeb3b}));
    arrow.position.y=0.9; arrow.rotation.x=Math.PI/2; group.add(arrow);
  });
  const arrow=new THREE.Mesh(new THREE.ConeGeometry(0.1,0.4,8), new THREE.MeshStandardMaterial({color:0xffeb3b}));
  arrow.position.set(0.45,0.9,0); arrow.rotation.z=-Math.PI/2; group.add(arrow);
  charGroup.add(group);
  return {id, role, glb, group, yaw, x, z};
}
function rebuildDrag(){
  const objs=chars.map(c=>c.group);
  if(dragControls) dragControls.dispose();
  dragControls=new DragControls(objs, camera, renderer.domElement);
  dragControls.addEventListener('dragstart',(e)=>{
    controls.enabled=false;
    selectIdx(chars.findIndex(c=>c.group===e.object));
  });
  dragControls.addEventListener('drag',(e)=>{
    const obj=e.object;
    // constrain to y=0 plane
    obj.position.y=0;
    const c=chars.find(c=>c.group===obj);
    if(c){ c.x=obj.position.x; c.z=obj.position.z; refreshList(); }
  });
  dragControls.addEventListener('dragend',()=>{
    controls.enabled=true;
  });
}
function addChar(role, x, z, yaw){
  const id=role+"_"+(chars.filter(c=>c.role===role).length+1);
  const glb=CHAR_MODELS[role] || CHAR_MODELS[Object.keys(CHAR_MODELS)[0]];
  if(x===undefined){ x=(Math.random()-0.5)*1.5; }
  if(z===undefined){ z=(Math.random()-0.5)*1.5; }
  if(yaw===undefined) yaw=0;
  const c=makeChar(id, role, glb, x, z, yaw);
  chars.push(c);
  rebuildDrag();
  selectIdx(chars.length-1);
}
function showPalette(){
  const palette=document.getElementById('palette');
  const btns=document.getElementById('paletteBtns');
  btns.innerHTML='';
  // pipeline roles in order, show only not yet placed if unique
  const placedCounts={};
  chars.forEach(c=> placedCounts[c.role]=(placedCounts[c.role]||0)+1);
  const neededCounts={};
  PIPELINE_ROLES.forEach(r=> neededCounts[r]=(neededCounts[r]||0)+1);
  let hasAvailable=false;
  PIPELINE_ROLES.forEach(role=>{
    const need=neededCounts[role]||0;
    const have=placedCounts[role]||0;
    if(have < need || Object.keys(neededCounts).length!==PIPELINE_ROLES.length){
      const btn=document.createElement('button');
      btn.textContent=role;
      btn.style.background= role==='girl'||role==='woman' ? '#6b2d5a' : '#2d4a6b';
      btn.style.color='#fff';
      btn.style.border='1px solid #555';
      btn.style.borderRadius='6px';
      btn.style.padding='6px 12px';
      btn.onclick=()=>{
        pendingRole=role;
        pendingSpot=null;
        palette.style.display='none';
        statusEl.textContent=`Placing ${role} — click floor for position`;
        updateGhost(null);
      };
      btns.appendChild(btn);
      hasAvailable=true;
    }
  });
  if(!hasAvailable){
    // all pipeline roles placed, allow any role as extra
    Object.keys(CHAR_MODELS).forEach(role=>{
      const btn=document.createElement('button');
      btn.textContent=role+' (+)';
      btn.style.opacity=0.7;
      btn.onclick=()=>{ pendingRole=role; pendingSpot=null; palette.style.display='none'; statusEl.textContent=`Placing ${role} — click floor`; };
      btns.appendChild(btn);
    });
  }
  if(chars.length >= 12){ btns.innerHTML='<em>Max 12 reached</em>'; }
  palette.style.display='block';
}
function hidePalette(){ document.getElementById('palette').style.display='none'; }
function updateGhost(hit){
  if(!ghostGroup){
    ghostGroup=new THREE.Group();
    const cyl=new THREE.Mesh(new THREE.CylinderGeometry(0.26,0.26,0.08,16), new THREE.MeshStandardMaterial({color:0x44aaff, transparent:true, opacity:0.28}));
    cyl.position.y=0.04;
    ghostGroup.add(cyl);
    const arrow=new THREE.Mesh(new THREE.ConeGeometry(0.12,0.5,8), new THREE.MeshStandardMaterial({color:0xffffff, transparent:true, opacity:0.9}));
    arrow.position.set(0.5,0.9,0); arrow.rotation.z=-Math.PI/2;
    arrow.name='ghostArrow';
    ghostGroup.add(arrow);
    // add label sprite for ghost facing
    scene.add(ghostGroup);
    ghostGroup.visible=false;
  }
  if(!hit || !pendingRole){
    ghostGroup.visible=false;
    return;
  }
  ghostGroup.visible=true;
  if(!pendingSpot){
    ghostGroup.position.set(hit.x,0,hit.z);
    ghostGroup.rotation.y=0;
  } else {
    ghostGroup.position.set(pendingSpot.x,0,pendingSpot.z);
    const yaw=Math.atan2(hit.z - pendingSpot.z, hit.x - pendingSpot.x);
    ghostGroup.rotation.y=yaw;
    const arrow=ghostGroup.getObjectByName('ghostArrow');
    if(arrow){ arrow.position.set(0.45,0.9,0); }
  }
}
let lastBox=null,lastCenter=null; let roomBoxHelper=null;
function isInsideRoom(x,z){
  if(!lastBox) return true;
  const eps=0.35; // margin from wall
  return x >= lastBox.min.x+eps && x <= lastBox.max.x-eps && z >= lastBox.min.z+eps && z <= lastBox.max.z-eps;
}
function updateRoomBoundsHelper(){
  if(!lastBox) return;
  if(roomBoxHelper){ scene.remove(roomBoxHelper); }
  const box=new THREE.Box3(new THREE.Vector3(lastBox.min.x, 0, lastBox.min.z), new THREE.Vector3(lastBox.max.x, 0.02, lastBox.max.z));
  roomBoxHelper=new THREE.Box3Helper(box, 0x6688aa);
  scene.add(roomBoxHelper);
}
function fitCameraTo(obj){
  const box=new THREE.Box3().setFromObject(obj);
  const size=box.getSize(new THREE.Vector3()); const center=box.getCenter(new THREE.Vector3());
  lastBox=box.clone(); lastCenter=center.clone();
  const r=Math.max(size.x,size.y,size.z)/2;
  const dist=Math.max(r*2.8+0.8,2.2);
  camera.position.set(center.x, center.y + r*0.35 + 1.1, center.z + dist);
  controls.target.copy(center); controls.target.y=center.y*0.2+0.2;
  controls.update(); updateHUD();
  updateRoomBoundsHelper();
}
function frontView(){
  if(!lastBox){fitCameraTo(modelGroup);return;}
  const size=lastBox.getSize(new THREE.Vector3()); const r=Math.max(size.x,size.y,size.z)/2;
  const dist=Math.max(r*2.8+0.8,2.2);
  camera.position.set(lastCenter.x, lastCenter.y + r*0.30 + 0.9, lastCenter.z + dist);
  controls.target.copy(lastCenter); controls.target.y=lastCenter.y*0.2+0.2;
  controls.update(); updateHUD();
}
// Transform for rotation (yaw)
transformControls=new TransformControls(camera, renderer.domElement);
transformControls.setMode('rotate');
transformControls.setSpace('local');
transformControls.showX=false; transformControls.showZ=false;
transformControls.addEventListener('dragging-changed',e=>{ controls.enabled=!e.value; });
transformControls.addEventListener('change',()=>{
  if(selectedIdx>=0){
    const c=chars[selectedIdx];
    c.yaw = (THREE.MathUtils.radToDeg(c.group.rotation.y) % 360 + 360) % 360;
    refreshList();
  }
});
scene.add(transformControls);

// click: workflow is Add -> choose person -> click floor for position -> click again for facing
renderer.domElement.addEventListener('click',(e)=>{
  const rect=renderer.domElement.getBoundingClientRect();
  mouse.x=((e.clientX-rect.left)/rect.width)*2-1; mouse.y=-((e.clientY-rect.top)/rect.height)*2+1;
  ray.setFromCamera(mouse,camera);
  const hit=new THREE.Vector3();
  const hasHit = ray.ray.intersectPlane(plane,hit)!==null;
  if(pendingRole){
    if(!hasHit) return;
    if(!pendingSpot){
      if(!isInsideRoom(hit.x, hit.z)){
        statusEl.textContent=`Position outside room (stay inside blue box) — click inside floor`;
        return;
      }
      // first click: lock position, await second click for yaw (second click = look point)
      pendingSpot={x: hit.x, z: hit.z};
      statusEl.textContent=`${pendingRole} position set at (${hit.x.toFixed(2)}, ${hit.z.toFixed(2)}) — now click again where they should LOOK (facing point)`;
      updateGhost(hit);
      return;
    } else {
      // second click: set yaw from pendingSpot to hit
      const dx=hit.x - pendingSpot.x, dz=hit.z - pendingSpot.z;
      if(Math.hypot(dx,dz) < 0.15){
        statusEl.textContent='Click a bit further away to set facing direction';
        return;
      }
      const yaw = Math.atan2(dz, dx) * 180/Math.PI;
      const yawNorm=((yaw%360)+360)%360;
      addChar(pendingRole, pendingSpot.x, pendingSpot.z, yawNorm);
      statusEl.textContent=`placed ${pendingRole} at (${pendingSpot.x.toFixed(2)}, ${pendingSpot.z.toFixed(2)}) yaw ${yawNorm.toFixed(0)}°`;
      pendingRole=null; pendingSpot=null; updateGhost(null);
      return;
    }
  }
  // normal click = select character (only if not placing)
  if(!hasHit) return;
  const isects=ray.intersectObjects(chars.map(c=>c.group), true);
  if(isects.length){
    let obj=isects[0].object;
    while(obj && !chars.find(c=>c.group===obj)) obj=obj.parent;
    if(obj) selectIdx(chars.findIndex(c=>c.group===obj));
  }
});
renderer.domElement.addEventListener('mousemove',(e)=>{
  if(!pendingRole) return;
  const rect=renderer.domElement.getBoundingClientRect();
  mouse.x=((e.clientX-rect.left)/rect.width)*2-1; mouse.y=-((e.clientY-rect.top)/rect.height)*2+1;
  ray.setFromCamera(mouse,camera);
  const hit=new THREE.Vector3();
  if(ray.ray.intersectPlane(plane,hit)===null) return;
  if(!pendingSpot){
    updateGhost(hit);
  } else {
    updateGhost(hit);
  }
});
controls.addEventListener('change',updateHUD);
window.addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth,innerHeight);});
document.getElementById('btnFront').addEventListener('click',frontView);
document.getElementById('btnFit').addEventListener('click',()=>fitCameraTo(modelGroup));
document.getElementById('btnAdd').addEventListener('click',()=>{
  if(chars.length >= PIPELINE_ROLES.length && PIPELINE_ROLES.length>0 && chars.length>=12){
    statusEl.textContent='max characters reached';
    return;
  }
  showPalette();
});
document.getElementById('btnCancelPalette').addEventListener('click',()=>{
  pendingRole=null; pendingSpot=null; hidePalette(); updateGhost(null); statusEl.textContent='cancelled';
});
document.getElementById('btnRemove').addEventListener('click',()=>{
  if(selectedIdx>=0){ const c=chars.splice(selectedIdx,1)[0]; charGroup.remove(c.group); transformControls.detach(); selectedIdx=-1; rebuildDrag(); refreshList(); }
});
async function confirm(){
  const currentBg = document.getElementById('bgSelect') ? document.getElementById('bgSelect').value : MODEL_URL;
  const payload={
    camera:{position:{x:+camera.position.x.toFixed(3),y:+camera.position.y.toFixed(3),z:+camera.position.z.toFixed(3)}, target:{x:+controls.target.x.toFixed(3),y:+controls.target.y.toFixed(3),z:+controls.target.z.toFixed(3)}},
    fov:+camera.fov.toFixed(1), distance:+camera.position.distanceTo(controls.target).toFixed(3),
    spots: chars.map(c=>({x:+c.group.position.x.toFixed(3), y:0, z:+c.group.position.z.toFixed(3), yaw:+c.yaw.toFixed(1), role:c.role, id:c.id})),
    background: currentBg,
    light: currentLight
  };
  statusEl.textContent='saving…';
  try{
    const r=await fetch('/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    if(!r.ok) throw new Error('no save handler');
    const j=await r.json();
    statusEl.textContent='saved ✓ — you can close this tab, pipeline will continue';
    await navigator.clipboard.writeText(JSON.stringify(payload,null,2)).catch(()=>{});
  } catch(e){
    statusEl.textContent='copied to clipboard ✓ — paste to me';
    await navigator.clipboard.writeText(JSON.stringify(payload,null,2)).catch(()=>{});
    console.log(JSON.stringify(payload,null,2));
  }
}
document.getElementById('btnConfirm').addEventListener('click',confirm);
window.addEventListener('keydown',e=>{ if(e.key==='Enter') confirm(); });

async function load(){
  statusEl.textContent='loading __LABEL__…';
  try{
    const gltf=await loader.loadAsync(MODEL_URL);
    modelGroup.add(gltf.scene);
    fitCameraTo(modelGroup);
    // start empty — user clicks to place one by one (per request)
    // keep INITIAL_SPOTS available as suggestions but do not auto-add
    // user adds via + Add button or Shift+Click floor at chosen position
    rebuildDrag();
    refreshList();
    statusEl.textContent='__LABEL__ loaded — Shift+Click floor to place characters one by one (or + Add), then drag & rotate';
    updateHUD();
  } catch(e){ statusEl.textContent='error: '+e.message; console.error(e); }
}
function animate(){ requestAnimationFrame(animate); controls.update(); renderer.render(scene,camera); }
load(); animate();
</script>
</body></html>
"""

def load_script_characters(script_path):
    try:
        data = json.loads(pathlib.Path(script_path).read_text(encoding="utf-8"))
        chars = data.get("characters", [])
        # map to roles
        roles = []
        for c in chars:
            role = c.get("role") or c.get("id") or "man"
            # normalize: child -> girl/boy based on gender
            gender = c.get("gender", "")
            age = c.get("age_group", "")
            if "child" in age:
                if gender == "female":
                    roles.append("girl")
                elif gender == "male":
                    roles.append("boy")
                else:
                    roles.append("girl")
            else:
                if gender == "female":
                    roles.append("woman")
                elif gender == "male":
                    roles.append("man")
                else:
                    roles.append(role)
            roles[-1] = roles[-1].lower()
            if roles[-1] not in MODEL_MAP:
                # fallback to man/woman
                roles[-1] = "man" if gender=="male" else "woman"
        return roles
    except Exception as e:
        print(f"could not load script {e}", file=sys.stderr)
        return []

def main():
    ap = argparse.ArgumentParser(description="Blocking picker for background + characters")
    ap.add_argument("--background", required=True, help="background glb path")
    ap.add_argument("--script", default=None, help="script.json to infer character count/roles")
    ap.add_argument("--characters", type=int, default=None, help="number of characters (overrides script)")
    ap.add_argument("--out", default="jobs/picker/picker.json", help="output picker json")
    ap.add_argument("--port", type=int, default=8000, help="http port")
    ap.add_argument("--light", default="L1", help="light preset for preview (not used in picker)")
    args = ap.parse_args()

    bg = pathlib.Path(args.background)
    if not bg.exists():
        print(f"background not found: {bg}", file=sys.stderr); sys.exit(2)

    # determine roles/count
    roles = []
    if args.script and pathlib.Path(args.script).exists():
        roles = load_script_characters(args.script)
    if args.characters is not None:
        # override count, keep roles but trim/extend
        n = args.characters
        if len(roles) < n:
            # extend with default order
            while len(roles) < n:
                roles.append(DEFAULT_ORDER[len(roles) % len(DEFAULT_ORDER)])
        else:
            roles = roles[:n]
    if not roles:
        n = args.characters or 4
        roles = [DEFAULT_ORDER[i % len(DEFAULT_ORDER)] for i in range(n)]

    print(f"Picker: background={bg} roles={roles} ({len(roles)} chars)", file=sys.stderr)

    # try to load initial spots from placer if available
    initial_spots = []
    try:
        # reuse placer to get collision-free spots
        import subprocess
        tmp = REPO / "C:/Users/SBS/AppData/Local/Temp/opencode/picker_placer.json"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(REPO / "tools/glb_character_placer.py"),
                        "--model", str(bg), "--characters", str(len(roles)),
                        "--out", str(tmp), "--format", "room_report"],
                       check=False, capture_output=True)
        if tmp.exists():
            data = json.loads(tmp.read_text(encoding="utf-8"))
            for i, s in enumerate(data.get("spots", [])):
                # s is blender Z-up: x,y,z ; need to map to viewer floor x,z (y=0)
                # viewer uses y=0 floor, x->x, z->z (since viewer is Y-up world? Three y up)
                # placer blender: x, y forward, z up ; viewer: y up, so map x->x, z-> -y ?
                # For simplicity use x->x, z-> -y from blender
                # But for living_room_with_curtains, placer gave spots y~1.97 forward, z~0 up ; viewer expects x,~ , z
                # We'll just use placer x and y (forward) as x,z
                # For picker, use x from s, z from s.y (forward)
                initial_spots.append({"x": s["x"], "z": s["y"], "yaw": s.get("yaw_deg", 0), "role": roles[i], "id": f"SPEAKER_{i:02d}"})
            print(f"  placer spots: {initial_spots}", file=sys.stderr)
    except Exception as e:
        print(f"  placer fallback: {e}", file=sys.stderr)

    # build html
    label = bg.stem
    html = TEMPLATE_PICKER.replace("__LABEL__", label).replace("__SRC__", bg.as_posix()).replace("__COUNT__", str(len(roles)))
    html = html.replace("__MODEL_URL__", f"/{bg.as_posix()}")
    html = html.replace("__CHAR_MODELS_JSON__", json.dumps(MODEL_MAP))
    html = html.replace("__INITIAL_SPOTS_JSON__", json.dumps(initial_spots))
    html = html.replace("__ROLES_JSON__", json.dumps(roles))
    bg_list = sorted([p.relative_to(REPO).as_posix() for p in (REPO / "assets/background").glob("*.glb")])
    html = html.replace("__BG_LIST_JSON__", json.dumps(bg_list))
    out_html = REPO / f"jobs/rooms/{label}_picker.html"
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html, encoding="utf-8")
    print(f"wrote {out_html}", file=sys.stderr)

    # prepare output file
    out_json = pathlib.Path(args.out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    # if exists, remove
    if out_json.exists():
        out_json.unlink()

    # custom handler to serve repo and handle POST /save
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(REPO), **kw)
        def do_POST(self):
            if self.path == "/save":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    out_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    print(f"saved picker to {out_json}", file=sys.stderr)
                    print(json.dumps(data, indent=2))
                    # shutdown after save in a thread
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode())
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, format, *args):
            sys.stderr.write(f"[http] {format%args}\n")

    with socketserver.TCPServer(("", args.port), Handler) as httpd:
        url = f"http://localhost:{args.port}/{out_html.relative_to(REPO).as_posix()}"
        print(f"Serving picker at {url}  (background={bg})", file=sys.stderr)
        print(f"Output will be written to {out_json} after you click Confirm", file=sys.stderr)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print("Blocking — open the URL, drag characters & camera, then click Confirm (or Enter)...", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

    if out_json.exists():
        print(f"Picker done: {out_json}", file=sys.stderr)
        print(out_json.read_text(encoding="utf-8"))
        # also copy camera/spots to clipboard-friendly log
        return 0
    else:
        print("Picker cancelled or no save", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
