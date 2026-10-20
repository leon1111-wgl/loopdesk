// The voice channel's microphone processor (src/voice.ts). Runs on the audio thread: averages the
// input down to 16 kHz and posts 100 ms of int16 PCM. A file of its own, not a blob: URL, so the
// console's Content-Security-Policy (script-src 'self') stays as strict as it is.
class Pcm16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0;
    this.n = 0;
    this.pos = 0;
    this.out = new Int16Array(1600);
    this.i = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let k = 0; k < ch.length; k++) {
      this.acc += ch[k];
      this.n++;
      this.pos++;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.acc / this.n));
        this.out[this.i++] = v * 32767;
        this.acc = 0;
        this.n = 0;
        if (this.i === this.out.length) {
          this.port.postMessage(this.out.buffer, [this.out.buffer]);
          this.out = new Int16Array(1600);
          this.i = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm16", Pcm16);
