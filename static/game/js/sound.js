// sound.js - WebAudioによる手続き的エンジン音 (音声ファイルなし・帯域ゼロ)
export class EngineSound {
  constructor() {
    this.ctx = null;
    this.enabled = true;
  }

  ensure() {
    if (this.ctx || !this.enabled) return;
    try {
      const C = new (window.AudioContext || window.webkitAudioContext)();
      this.osc = C.createOscillator();
      this.osc.type = 'sawtooth';
      this.filter = C.createBiquadFilter();
      this.filter.type = 'lowpass';
      this.filter.frequency.value = 400;
      this.gain = C.createGain();
      this.gain.gain.value = 0.035;
      this.osc.connect(this.filter).connect(this.gain).connect(C.destination);
      this.osc.start();
      this.ctx = C;
    } catch {
      this.enabled = false;
    }
  }

  update(speed) {
    if (!this.ctx) return;
    this.osc.frequency.value = 45 + speed * 1.1;
    this.filter.frequency.value = 250 + speed * 6;
  }
}
