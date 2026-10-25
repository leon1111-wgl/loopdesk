/** Voice channel client: microphone -> 16 kHz PCM over a WebSocket, agent speech back.
 *
 * The server does the listening (endpointing, ASR) and the deciding; this side captures audio,
 * plays what it is sent, and reports what was actually played, because the conversation keeps
 * only what the customer heard. On barge-in the server says stop; playback stops at once and
 * the position is reported. Protocol: backend/app/api/voice.py. */
import { getToken } from "./api";
import type { AgentEvent } from "./types";

export type VoiceMode = "ptt" | "continuous";

export interface VoiceConfig {
  enabled: boolean;
  reason?: string | null;
  asr?: string;
  tts?: string;
  end_silence_ms?: number;
  endpointing?: string; // silence | rules | smart_turn (backend/app/voice/turn.py)
}

export type VoiceEvent =
  | { type: "ready"; asr: string; tts: string }
  | { type: "vad"; speech: boolean }
  | { type: "partial"; text: string }
  | { type: "transcript"; text: string; pii: string[]; entities: { kind: string; value: string; confidence: number }[]; merged?: boolean }
  | { type: "withdrawn"; turn: number | null } // the customer went on before the answer was spoken: it is dropped
  | { type: "prompt"; kind: string; text: string }
  | { type: "agent"; event: AgentEvent }
  | { type: "say"; turn: number; id: number; text: string; written: string; sample_rate: number }
  | { type: "say_end"; turn: number; id: number; samples: number }
  | { type: "barge_in"; turn: number }
  | { type: "turn_end"; turn: number; latency: Record<string, number> }
  | { type: "turn_saved"; turn: number; heard: string; interrupted: boolean }
  | { type: "no_speech" }
  | { type: "error"; message: string }
  | { type: "closed"; code: number };

// The microphone processor lives in public/voice-worklet.js (same origin, so the CSP allows it).
const WORKLET_URL = `${import.meta.env.BASE_URL}voice-worklet.js`;

interface Playing {
  turn: number;
  id: number;
  rate: number;
  start: number; // AudioContext time the sentence's first sample plays
  end: number;
  sources: AudioBufferSourceNode[];
  finished: boolean; // say_end received
}

export class VoiceClient {
  private ws: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private sending = false;
  private playing: Playing[] = [];
  private cursor = 0;
  private timer: number | null = null;

  constructor(private onEvent: (event: VoiceEvent) => void) {}

  async open(conversationId: number, mode: VoiceMode) {
    this.ctx = new AudioContext();
    await this.ctx.resume(); // iOS Safari starts it suspended even inside the click
    await this.ctx.audioWorklet.addModule(WORKLET_URL);
    this.stream = await navigator.mediaDevices.getUserMedia({
      // Echo cancellation keeps the agent's own voice from reading as the customer talking over it.
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "pcm16");
    this.node.port.onmessage = (e) => {
      if (this.sending && this.ws?.readyState === WebSocket.OPEN) this.ws.send(e.data as ArrayBuffer);
    };
    source.connect(this.node);
    this.sending = mode === "continuous";
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/voice/ws`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: getToken(), conversation_id: conversationId, mode }));
    ws.onmessage = (m) => (typeof m.data === "string" ? this.message(JSON.parse(m.data) as VoiceEvent) : this.audio(m.data));
    ws.onerror = () => this.onEvent({ type: "error", message: "语音连接出错" });
    ws.onclose = (e) => this.onEvent({ type: "closed", code: e.code });
    this.timer = window.setInterval(() => this.report(), 250);
  }

  /** Push-to-talk: speaking while the agent talks interrupts it, as in continuous mode. */
  press() {
    this.stopPlayback(true);
    this.sending = true;
    this.send({ type: "ptt", down: true });
  }

  release() {
    this.send({ type: "ptt", down: false });
    this.sending = false;
  }

  setMode(mode: VoiceMode) {
    this.sending = mode === "continuous";
    this.send({ type: "mode", mode });
  }

  /** The stop button: the same as talking over the agent, without saying anything. */
  stop() {
    this.send({ type: "stop" });
    this.stopPlayback(true);
  }

  close() {
    if (this.timer) window.clearInterval(this.timer);
    this.stopPlayback(true);
    this.ws?.close();
    this.stream?.getTracks().forEach((t) => t.stop());
    void this.ctx?.close();
    this.ws = null;
  }

  private send(msg: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private message(event: VoiceEvent) {
    if (event.type === "say") {
      const ctx = this.ctx!;
      const start = Math.max(this.cursor, ctx.currentTime + 0.05);
      this.playing.push({ turn: event.turn, id: event.id, rate: event.sample_rate, start, end: start, sources: [], finished: false });
    } else if (event.type === "say_end") {
      const p = this.playing.find((x) => x.turn === event.turn && x.id === event.id);
      if (p) p.finished = true;
    } else if (event.type === "barge_in") {
      this.stopPlayback(true);
    }
    this.onEvent(event);
  }

  private audio(data: ArrayBuffer) {
    const view = new DataView(data);
    const turn = view.getUint16(0, true);
    const id = view.getUint16(2, true);
    const p = this.playing.find((x) => x.turn === turn && x.id === id);
    const ctx = this.ctx;
    if (!p || !ctx) return;
    const pcm = new Int16Array(data, 4);
    const buffer = ctx.createBuffer(1, pcm.length, p.rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
    const node = ctx.createBufferSource();
    node.buffer = buffer;
    node.connect(ctx.destination);
    const at = Math.max(p.end, ctx.currentTime);
    node.start(at);
    p.sources.push(node);
    p.end = at + buffer.duration;
    this.cursor = p.end; // the next sentence starts where this one ends: no gaps, no overlaps
  }

  /** Tell the server how far each sentence has played; finished sentences once, with done. */
  private report() {
    const ctx = this.ctx;
    if (!ctx) return;
    const now = ctx.currentTime;
    for (const p of [...this.playing]) {
      const ms = Math.max(0, Math.min(now, p.end) - p.start) * 1000;
      const done = p.finished && now >= p.end;
      if (ms > 0 || done) this.send({ type: "played", turn: p.turn, id: p.id, ms, done });
      if (done) this.playing = this.playing.filter((x) => x !== p);
    }
  }

  private stopPlayback(reportPosition: boolean) {
    const now = this.ctx?.currentTime ?? 0;
    for (const p of this.playing) {
      if (reportPosition) this.send({ type: "played", turn: p.turn, id: p.id, ms: Math.max(0, Math.min(now, p.end) - p.start) * 1000, done: false });
      p.sources.forEach((s) => { try { s.stop(); } catch { /* already ended */ } });
    }
    this.playing = [];
    this.cursor = now;
  }

  get speaking(): boolean {
    return this.playing.length > 0;
  }
}
