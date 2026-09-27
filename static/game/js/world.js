import * as THREE from 'three';

export function buildWorld(scene) {
  scene.background = new THREE.Color(0x87b7e8);
  scene.fog = new THREE.Fog(0x9fc4e8, 800, 6500);

  scene.add(new THREE.HemisphereLight(0xcfe4ff, 0x3f6b34, 0.95));
  const sun = new THREE.DirectionalLight(0xfff1d6, 1.4);
  sun.position.set(600, 900, 300);
  scene.add(sun);

  const tex = makeGroundTexture();
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(80, 80);
  const ground = new THREE.Mesh(
    new THREE.CircleGeometry(8000, 72),
    new THREE.MeshLambertMaterial({ map: tex })
  );
  ground.rotation.x = -Math.PI / 2;
  scene.add(ground);

  const hills = new THREE.InstancedMesh(
    new THREE.ConeGeometry(1, 1, 6),
    new THREE.MeshLambertMaterial({ color: 0x2e5d33 }),
    140
  );
  const m = new THREE.Matrix4();
  const q = new THREE.Quaternion();
  const pos = new THREE.Vector3();
  const scl = new THREE.Vector3();
  for (let i = 0; i < 140; i++) {
    const r = 600 + Math.random() * 6500;
    const a = Math.random() * Math.PI * 2;
    const h = 60 + Math.random() * 260;
    const w = h * (1.5 + Math.random());
    pos.set(Math.cos(a) * r, h / 2, Math.sin(a) * r);
    scl.set(w, h, w);
    m.compose(pos, q, scl);
    hills.setMatrixAt(i, m);
  }
  scene.add(hills);

  const clouds = new THREE.InstancedMesh(
    new THREE.IcosahedronGeometry(1, 0),
    new THREE.MeshLambertMaterial({ color: 0xffffff, transparent: true, opacity: 0.85, flatShading: true }),
    90
  );
  for (let i = 0; i < 90; i++) {
    const r = 300 + Math.random() * 6000;
    const a = Math.random() * Math.PI * 2;
    pos.set(Math.cos(a) * r, 250 + Math.random() * 350, Math.sin(a) * r);
    scl.set(60 + Math.random() * 120, 16 + Math.random() * 18, 40 + Math.random() * 60);
    m.compose(pos, q, scl);
    clouds.setMatrixAt(i, m);
  }
  scene.add(clouds);

  return {
    update(dt) {
      clouds.position.x += dt * 3;
      if (clouds.position.x > 500) clouds.position.x = 0;
    },
  };
}

function makeGroundTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 256;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#4d7a3c';
  ctx.fillRect(0, 0, 256, 256);
  for (let i = 0; i < 900; i++) {
    const g = 100 + Math.random() * 60;
    ctx.fillStyle = `rgba(${(g * 0.55) | 0},${g | 0},${(g * 0.45) | 0},0.35)`;
    ctx.fillRect(Math.random() * 256, Math.random() * 256, 3 + Math.random() * 8, 3 + Math.random() * 8);
  }
  return new THREE.CanvasTexture(c);
}
