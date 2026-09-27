function setupGame() {
  const WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/game';
  const SEND_INTERVAL = 0.1;
  const INTERP_DELAY = 0.15;
  const WORLD_R = 7000;

  const gameContainer = document.getElementById('game-container');
  
  const renderer = new THREE.WebGLRenderer({ 
    antialias: true, 
    alpha: false,
    powerPreference: 'high-performance'
  });
  
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setClearColor(0x87b7e8);
  
  gameContainer.appendChild(renderer.domElement);
  
  function updateCanvasSize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    renderer.setSize(w, h);
    if (camera) {
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
  }
  
  updateCanvasSize();
  window.addEventListener('resize', updateCanvasSize);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(
    70, 
    window.innerWidth / window.innerHeight, 
    0.1, 
    9000
  );
  
  const world = buildWorld(scene);

  const me = {
    pos: new THREE.Vector3(0, 80, 0),
    quat: new THREE.Quaternion(),
    speed: 40,
  };
  
  const myPlane = buildPlane(planeColor(Math.floor(Math.random() * 8)));
  scene.add(myPlane.group);

  const keys = {};
  
  window.addEventListener('keydown', (e) => {
    keys[e.code] = true;
    sound.ensure();
  });
  
  window.addEventListener('keyup', (e) => {
    keys[e.code] = false;
  });
  
  window.addEventListener('pointerdown', () => sound.ensure());

  // ===== モバイル用入力管理 =====
  const mobileInput = {
    leftStick: { x: 0, y: 0 },
    rightGauge: 0,
  };

  const net = new NetClient(WS_URL);
  const sound = new EngineSound();

  // モバイル用コントロール初期化
  if (gameMode === 'mobile') {
    console.log('Setting up mobile controls...');
    setupMobileControls(mobileInput);
  }

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
    let pitchIn, rollIn, thrIn;

    if (gameMode === 'mobile') {
      pitchIn = mobileInput.leftStick.y;
      rollIn = mobileInput.leftStick.x;
      thrIn = mobileInput.rightGauge;
    } else {
      pitchIn = (keys.ArrowUp ? 1 : 0) - (keys.ArrowDown ? 1 : 0);
      rollIn = (keys.KeyA ? 1 : 0) - (keys.KeyD ? 1 : 0);
      thrIn = (keys.KeyW ? 1 : 0) - (keys.KeyS ? 1 : 0);
    }

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

    if (me.pos.y < 2) {
      me.pos.y = 2;
      me.speed = Math.max(me.speed * 0.9, 20);
    }
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

  const toast = document.getElementById('toast');

  function showToast(msg) {
    toast.textContent = msg;
    toast.style.display = 'block';
    setTimeout(() => (toast.style.display = 'none'), 3500);
  }

  function refreshRoster() {
    const el = document.getElementById('roster');
    if (!net.joined) {
      el.innerHTML = '';
      return;
    }
    const names = ['<b>' + escapeHtml(myName) + ' (あなた)</b>'];
    for (const [id] of remotes) {
      names.push(escapeHtml(rosterNames.get(id) || `Pilot-${id}`));
    }
    el.innerHTML = `ルーム: ${escapeHtml(myRoom)}<br>` + names.join('<br>');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[c]));
  }

  let myName = '';
  let myRoom = '';
  const rosterNames = new Map();

  net.onJoined = (m) => {
    document.getElementById('menu').style.display = 'none';
    document.getElementById('leaveBtn').style.display = 'block';
    for (const p of m.players) {
      rosterNames.set(p.id, p.name);
      addRemote(p.id, p.name);
    }
    refreshRoster();
  };

  net.onJoin = (m) => {
    rosterNames.set(m.id, m.name);
    addRemote(m.id, m.name);
    refreshRoster();
  };

  net.onLeave = (m) => {
    rosterNames.delete(m.id);
    removeRemote(m.id);
    refreshRoster();
  };

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
    if (!myRoom) {
      showToast('ルーム名を入力してください');
      return;
    }
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
  let fpsAcc = 0,
    fpsCnt = 0,
    fps = 0;

  function updateHud(dt) {
    fpsAcc += dt;
    fpsCnt++;
    hudAcc += dt;
    if (hudAcc < 0.25) return;
    fps = Math.round(fpsCnt / fpsAcc);
    fpsAcc = fpsCnt = 0;
    hudAcc = 0;

    if (gameMode === 'pc') {
      hud.textContent = `SPD ${Math.round(me.speed * 3.6)} km/h  ALT ${Math.round(me.pos.y)} m  FPS ${fps}  オンライン ${
        remotes.size + (net.joined ? 1 : 0)
      } 機`;
    } else {
      document.getElementById('speed-display').textContent = `SPD ${Math.round(me.speed * 3.6)} km/h`;
      document.getElementById('altitude-display').textContent = `ALT ${Math.round(me.pos.y)} m`;
      hud.textContent = `FPS ${fps}  オンライン ${remotes.size + (net.joined ? 1 : 0)} 機`;
    }
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
}

// ===== モバイル用コントロール初期化 =====
function setupMobileControls(mobileInput) {
  const leftStickContainer = document.getElementById('mobile-left-stick');
  const rightGaugeContainer = document.getElementById('mobile-right-gauge');

  if (!leftStickContainer || !rightGaugeContainer) {
    console.error('Mobile control containers not found');
    return;
  }

  console.log('Creating left stick...');
  setupAnalogStick(leftStickContainer, (x, y) => {
    mobileInput.leftStick.x = x;
    mobileInput.leftStick.y = y;
  });

  console.log('Creating right gauge...');
  setupGauge(rightGaugeContainer, (value) => {
    mobileInput.rightGauge = value;
  });
}

function setupAnalogStick(container, onMove) {
  const canvas = document.createElement('canvas');
  canvas.width = 140;
  canvas.height = 140;
  canvas.style.touchAction = 'none';
  canvas.style.userSelect = 'none';
  container.appendChild(canvas);

  const ctx = canvas.getContext('2d');
  const centerX = 70;
  const centerY = 70;
  const radius = 60;
  const stickRadius = 18;

  let touchActive = false;
  let stickX = 0,
    stickY = 0;

  function draw() {
    ctx.fillStyle = 'rgba(16,29,44,0.6)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.fillStyle = 'rgba(47,143,224,0.2)';
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = 'rgba(47,143,224,0.4)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
    ctx.stroke();

    ctx.fillStyle = 'rgba(47,143,224,0.9)';
    ctx.beginPath();
    ctx.arc(centerX + stickX * radius, centerY + stickY * radius, stickRadius, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = 'rgba(255,255,255,0.5)';
    ctx.font = 'bold 10px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('上昇/左右', centerX, 12);
  }

  canvas.addEventListener('touchstart', (e) => {
    e.preventDefault();
    touchActive = true;
    updateStick(e.touches[0]);
  });

  canvas.addEventListener('touchmove', (e) => {
    e.preventDefault();
    if (touchActive) updateStick(e.touches[0]);
  });

  canvas.addEventListener('touchend', (e) => {
    e.preventDefault();
    touchActive = false;
    stickX = 0;
    stickY = 0;
    draw();
  });

  function updateStick(touch) {
    const rect = canvas.getBoundingClientRect();
    const x = touch.clientX - rect.left - centerX;
    const y = touch.clientY - rect.top - centerY;
    const dist = Math.sqrt(x * x + y * y);

    if (dist > radius) {
      stickX = (x / dist);
      stickY = (y / dist);
    } else {
      stickX = x / radius;
      stickY = y / radius;
    }

    onMove(stickX, stickY);
    draw();
  }

  draw();
}

function setupGauge(container, onChange) {
  const canvas = document.createElement('canvas');
  canvas.width = 100;
  canvas.height = 180;
  canvas.style.touchAction = 'none';
  canvas.style.userSelect = 'none';
  container.appendChild(canvas);

  const ctx = canvas.getContext('2d');
  const centerX = 50;
  const centerY = 90;
  let gaugeValue = 0;

  function draw() {
    ctx.fillStyle = 'rgba(16,29,44,0.6)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.strokeStyle = 'rgba(47,143,224,0.5)';
    ctx.lineWidth = 2;
    ctx.strokeRect(15, 30, 70, 120);

    if (gaugeValue > 0) {
      ctx.fillStyle = 'rgba(65,176,107,0.9)';
      const fillHeight = gaugeValue * 60;
      ctx.fillRect(15, 90 - fillHeight, 70, fillHeight);
    } else if (gaugeValue < 0) {
      ctx.fillStyle = 'rgba(201,59,43,0.9)';
      const fillHeight = -gaugeValue * 60;
      ctx.fillRect(15, 90, 70, fillHeight);
    }

    ctx.fillStyle = 'rgba(200,200,200,0.6)';
    ctx.fillRect(15, 88, 70, 4);

    ctx.fillStyle = '#dce9f5';
    ctx.font = 'bold 10px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('加速', centerX, 20);
    ctx.fillText('減速', centerX, 160);
  }

  canvas.addEventListener('touchstart', (e) => {
    e.preventDefault();
    updateGauge(e.touches[0]);
  });

  canvas.addEventListener('touchmove', (e) => {
    e.preventDefault();
    updateGauge(e.touches[0]);
  });

  canvas.addEventListener('touchend', (e) => {
    e.preventDefault();
    gaugeValue = 0;
    draw();
    onChange(0);
  });

  function updateGauge(touch) {
    const rect = canvas.getBoundingClientRect();
    const y = touch.clientY - rect.top;

    if (y < 90) {
      gaugeValue = Math.max(-1, Math.min(1, (90 - y) / 60));
    } else {
      gaugeValue = Math.max(-1, Math.min(1, (y - 90) / 60 * -1));
    }

    onChange(gaugeValue);
    draw();
  }

  draw();
}
