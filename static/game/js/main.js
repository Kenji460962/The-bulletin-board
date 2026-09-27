import * as THREE from 'three';
import { buildWorld } from './world.js';
import { buildPlane, makeNameTag, planeColor } from './plane.js';
import { NetClient } from './net.js';
import { EngineSound } from './sound.js';

const WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/game';
const SEND_INTERVAL = 0.1;
const INTERP_DELAY = 0.15;
const WORLD_R = 7000;

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(70, innerWidth / innerHeight, 0.1, 9000);
const world = buildWorld(scene);

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

const me = {
  pos: new THREE.Vector3(0, 80, 0),
  quat: new THREE.Quaternion(),
  speed: 40,
};
const myPlane = buildPlane(planeColor(Math.floor(Math.random() * 8)));
scene.add(myPlane.group);

const keys = {};
addEventListener('keydown', (e) => { keys[e.code] = true; sound.ensure(); });
addEventListener('keyup', (e) => { keys[e.code] = false; });
addEventListener('pointerdown', () => sound.ensure());

const remotes = new Map();

function addRemote(id, name) {
  if (remotes.has(id)) return;
  const { group, prop } = buildPlane(planeColor(id));
  const tag = makeNameTag(name || `Pilot-${id}`);
  tag.position.y = 2.2;
  group.add(tag);
  scene.add(group);
  remotes.set(id, { group, prop, tag, snaps: [] });
}

function removeRemote(id) {
  const r = remotes.get(id);
  if (!r) return;
  scene.remove(r.group);
  remotes.delete(id);
}

const AXIS_X = new THREE.Vector3(1, 0, 0);
const AXIS_Y = new THREE.Vector3(0, 1, 0);
const AXIS_Z = new THREE.Vector3(0, 0, 1);
const qTmp = new THREE.Quaternion();
const vFwd = new THREE.Vector3();

function step(dt) {
  const pitchIn = (keys.ArrowUp ? 1 : 0) - (keys.ArrowDown ? 1 : 0);
  const rollIn = (keys.KeyA ? 1 : 0) - (keys.KeyD ? 1 : 0);
  const thrIn = (keys.KeyW ? 1 : 0) - (keys.KeyS ? 1 : 0);

  vFwd.set(0, 0, 1).applyQuaternion(me.quat);
  me.speed += thrIn * 30 * dt - vFwd.y * 18 * dt;
  me.speed = THREE.MathUtils.clamp(me.speed, 8, 150);

  qTmp.setFromAxisAngle(AXIS_X, pitchIn * 1.5 * dt);
  me.quat.multiply(qTmp);
  qTmp.setFromAxisAngle(AXIS_Z, rollIn * 2.4 * dt);
  me.quat.multiply(qTmp);
  qTmp.setFromAxisAngle(AXIS_Y, -rollIn * 0.9 * dt);
  me.quat.multiply(qTmp);

  vFwd.set(0, 0, 1).applyQuaternion(me.quat);
  me.pos.addScaledVector(vFwd, me.speed * dt);

  if (me.pos.y < 2) { me.pos.y = 2; me.speed = Math.max(me.speed * 0.9, 20); }
  if (me.pos.y > 1500) me.pos.y = 1500;

  if (me.pos.x > WORLD_R) me.pos.x = -WORLD_R;
  if (me.pos.x < -WORLD_R) me.pos.x = WORLD_R;
  if (me.pos.z > WORLD_R) me.pos.z = -WORLD_R;
  if (me.pos.z < -WORLD_R) me.pos.z = WORLD_R;
}

const camUp = new THREE.Vector3();
const desired = new THREE.Vector3();
const lookAt = new THREE.Vector3();

function updateCamera(dt) {
  vFwd.set(0, 0, 1).applyQuaternion(me.quat);
  camUp.set(0, 1, 0).applyQuaternion(me.quat).lerp(AXIS_Y, 0.5).normalize();
  desired.copy(me.pos).addScaledVector(vFwd, -12).addScaledVector(camUp, 4);
  camera.position.lerp(desired, 1 - Math.exp(-6 * dt));
  camera.up.copy(camUp);
  lookAt.copy(me.pos).addScaledVector(vFwd, 20);
  camera.lookAt(lookAt);
}

const snapPos = new THREE.Vector3();
const snapQuat = new THREE.Quaternion();

function updateRemotes() {
  const rt = performance.now() / 1000 - INTERP_DELAY;
  for (const r of remotes.values()) {
    const s = r.snaps;
    if (s.length === 0) continue;
    while (s.length > 2 && s[1].t < rt) s.shift();
    const a = s[0];
    const b = s[1] || s[0];
    const span = Math.max(b.t - a.t, 1e-4);
    const f = THREE.MathUtils.clamp((rt - a.t) / span, 0, 1.5);
    snapPos.set(
      a.px + (b.px - a.px) * f,
      a.py + (b.py - a.py) * f,
      a.pz + (b.pz - a.pz) * f
    );
    snapQuat.set(
      a.qx + (b.qx - a.qx) * f,
      a.qy + (b.qy - a.qy) * f,
      a.qz + (b.qz - a.qz) * f,
      a.qw + (b.qw - a.qw) * f
    ).normalize();
    r.group.position.copy(snapPos);
    r.group.quaternion.copy(snapQuat);
    r.prop.rotation.z += (b.sp || 40) * 0.01;
  }
}

const net = new NetClient(WS_URL);
const sound = new EngineSound();
const toast = document.getElementById('toast');

function showToast(msg) {
  toast.textContent = msg;
  toast.style.display = 'block';
  setTimeout(() => (toast.style.display = 'none'), 3500);
}

function refreshRoster() {
  const el = document.getElementById('roster');
  if (!net.joined) { el.innerHTML = ''; return; }
  const names = ['<b>' + escapeHtml(myName) + ' (あなた)</b>'];
  for (const [id] of remotes) names.push(escapeHtml(rosterNames.get(id) || `Pilot-${id}`));
  el.innerHTML = `ルーム: ${escapeHtml(myRoom)}<br>` + names.join('<br>');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>\"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;', "'": '&#39;' }[c]));
}

let myName = '';
let myRoom = '';
const rosterNames = new Map();

net.onJoined = (m) => {
  document.getElementById('menu').style.display = 'none';
  document.getElementById('leaveBtn').style.display = 'block';
  for (const p of m.players) { rosterNames.set(p.id, p.name); addRemote(p.id, p.name); }
  refreshRoster();
};
net.onJoin = (m) => { rosterNames.set(m.id, m.name); addRemote(m.id, m.name); refreshRoster(); };
net.onLeave = (m) => { rosterNames.delete(m.id); removeRemote(m.id); refreshRoster(); };
net.onState = (s) => {
  const r = remotes.get(s.id);
  if (!r) return;
  r.snaps.push(s);
  if (r.snaps.length > 12) r.snaps.shift();
};
net.onError = (m) => {
  const msgs = {
    room_full: 'そのルームは満員です',
    rooms_full: 'サーバーが混み合っています。しばらく待ってください',
    bad_room: 'ルーム名を入力してください',
  };
  showToast(msgs[m.code] || 'エラー: ' + m.code);
};
net.onClose = () => {
  for (const [id] of remotes) removeRemote(id);
  rosterNames.clear();
  document.getElementById('menu').style.display = 'flex';
  document.getElementById('leaveBtn').style.display = 'none';
  refreshRoster();
};

document.getElementById('joinBtn').onclick = async () => {
  myName = document.getElementById('nameInput').value.trim() || '名無しパイロット';
  myRoom = document.getElementById('roomInput').value.trim();
  if (!myRoom) { showToast('ルーム名を入力してください'); return; }
  try {
    if (!net.ws || net.ws.readyState > 1) await net.connect();
    net.sendJoin(myRoom, myName);
  } catch {
    showToast('サーバーに接続できません');
  }
};
document.getElementById('leaveBtn').onclick = () => {
  net.sendLeave();
  document.getElementById('menu').style.display = 'flex';
  document.getElementById('leaveBtn').style.display = 'none';
  for (const [id] of remotes) removeRemote(id);
  rosterNames.clear();
  refreshRoster();
};

const hud = document.getElementById('hud');
let hudAcc = 0;
let fpsAcc = 0, fpsCnt = 0, fps = 0;

function updateHud(dt) {
  fpsAcc += dt; fpsCnt++;
  hudAcc += dt;
  if (hudAcc < 0.25) return;
  fps = Math.round(fpsCnt / fpsAcc);
  fpsAcc = fpsCnt = 0;
  hudAcc = 0;
  hud.textContent =
    `SPD ${Math.round(me.speed * 3.6)} km/h  ALT ${Math.round(me.pos.y)} m  ` +
    `FPS ${fps}  オンライン ${remotes.size + (net.joined ? 1 : 0)} 機`;
}

let last = performance.now();
let sendAcc = 0;

function loop(now) {
  requestAnimationFrame(loop);
  const dt = Math.min((now - last) / 1000, 0.05);
  last = now;

  step(dt);
  myPlane.group.position.copy(me.pos);
  myPlane.group.quaternion.copy(me.quat);
  myPlane.prop.rotation.z += me.speed * dt * 0.8;

  world.update(dt);
  updateCamera(dt);
  updateRemotes();
  sound.update(me.speed);

  if (net.joined) {
    sendAcc += dt;
    if (sendAcc >= SEND_INTERVAL) {
      sendAcc = 0;
      net.sendState(me.pos, me.quat, me.speed);
    }
  }

  updateHud(dt);
  renderer.render(scene, camera);
}
requestAnimationFrame(loop);
