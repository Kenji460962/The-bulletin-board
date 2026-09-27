export class NetClient {
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.joined = false;
    this.id = 0;
  }

  connect() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.url);
      ws.binaryType = 'arraybuffer';
      this.ws = ws;
      ws.onopen = () => resolve();
      ws.onerror = (e) => reject(e);
      ws.onclose = () => { this.joined = false; this.onClose?.(); };
      ws.onmessage = (ev) => this._onMessage(ev);
    });
  }

  sendJoin(room, name) {
    this._sendJson({ t: 'join', room, name });
  }

  sendLeave() {
    this._sendJson({ t: 'leave' });
  }

  _sendJson(obj) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj));
  }

  sendState(pos, quat, speed) {
    if (!this.ws || this.ws.readyState !== 1) return;
    const buf = new ArrayBuffer(32);
    const v = new DataView(buf);
    v.setFloat32(0, pos.x, true);
    v.setFloat32(4, pos.y, true);
    v.setFloat32(8, pos.z, true);
    v.setFloat32(12, quat.x, true);
    v.setFloat32(16, quat.y, true);
    v.setFloat32(20, quat.z, true);
    v.setFloat32(24, quat.w, true);
    v.setFloat32(28, speed, true);
    this.ws.send(buf);
  }

  _onMessage(ev) {
    if (typeof ev.data === 'string') {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      switch (m.t) {
        case 'hello': this.id = m.id; break;
        case 'joined': this.joined = true; this.id = m.id; this.onJoined?.(m); break;
        case 'join': this.onJoin?.(m); break;
        case 'leave': this.onLeave?.(m); break;
        case 'error': this.onError?.(m); break;
      }
      return;
    }

    const v = new DataView(ev.data);
    if (v.byteLength !== 35 || v.getUint8(0) !== 1) return;

    this.onState?.({
      id: v.getUint16(1, true),
      t: performance.now() / 1000,
      px: v.getFloat32(3, true), py: v.getFloat32(7, true), pz: v.getFloat32(11, true),
      qx: v.getFloat32(15, true), qy: v.getFloat32(19, true),
      qz: v.getFloat32(23, true), qw: v.getFloat32(27, true),
      sp: v.getFloat32(31, true),
    });
  }
}
