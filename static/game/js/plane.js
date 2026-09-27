import * as THREE from 'three';

export function buildPlane(color = 0xd9453a) {
  const g = new THREE.Group();
  const mat = new THREE.MeshLambertMaterial({ color });
  const dark = new THREE.MeshLambertMaterial({ color: 0x222831 });

  const bodyGeo = new THREE.CylinderGeometry(0.16, 0.38, 4, 10);
  bodyGeo.rotateX(Math.PI / 2);
  g.add(new THREE.Mesh(bodyGeo, mat));

  const noseGeo = new THREE.ConeGeometry(0.16, 0.7, 10);
  noseGeo.rotateX(Math.PI / 2);
  const nose = new THREE.Mesh(noseGeo, dark);
  nose.position.z = 2.35;
  g.add(nose);

  const wing = new THREE.Mesh(new THREE.BoxGeometry(7, 0.08, 1.1), mat);
  wing.position.set(0, 0.05, 0.3);
  g.add(wing);

  const hstab = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.06, 0.7), mat);
  hstab.position.set(0, 0.1, -1.85);
  g.add(hstab);

  const vstab = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.9, 0.8), mat);
  vstab.position.set(0, 0.45, -1.85);
  g.add(vstab);

  const canopy = new THREE.Mesh(new THREE.SphereGeometry(0.34, 10, 8), dark);
  canopy.scale.set(0.8, 0.6, 1.4);
  canopy.position.set(0, 0.42, 0.55);
  g.add(canopy);

  const prop = new THREE.Group();
  const blade1 = new THREE.Mesh(new THREE.BoxGeometry(0.18, 2.2, 0.05), dark);
  const blade2 = blade1.clone();
  blade2.rotation.z = Math.PI / 2;
  prop.add(blade1, blade2);
  prop.position.z = 2.75;
  g.add(prop);

  return { group: g, prop };
}

export function makeNameTag(text) {
  const c = document.createElement('canvas');
  c.width = 256;
  c.height = 64;
  const ctx = c.getContext('2d');
  ctx.font = 'bold 34px sans-serif';
  ctx.textAlign = 'center';
  const w = ctx.measureText(text).width + 28;
  ctx.fillStyle = 'rgba(10,22,34,0.55)';
  ctx.beginPath();
  ctx.roundRect(128 - w / 2, 8, w, 48, 10);
  ctx.fill();
  ctx.fillStyle = '#ffffff';
  ctx.fillText(text, 128, 44);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({
    map: new THREE.CanvasTexture(c),
    depthTest: false,
  }));
  sp.scale.set(7, 1.75, 1);
  return sp;
}

export function planeColor(id) {
  const palette = [0xd9453a, 0x2f8fe0, 0xe0a52f, 0x41b06b, 0x9b59d0, 0xe06fa0, 0x3ec6c6, 0xd97b2f];
  return palette[id % palette.length];
}
