/**
 * Mic capture: getUserMedia → 16 kHz mono Int16 PCM frames → callback.
 *
 * Returns a `MicSession` whose `stop()` releases the AudioContext and the
 * underlying MediaStream tracks.  Frames are emitted ~every 20 ms (depends
 * on the browser's underlying buffer size, typically 2048 samples at 16 kHz
 * which is ~128 ms — but we resample-on-the-fly to keep the FE simple).
 *
 * Implementation notes:
 *
 * * Uses ScriptProcessorNode (deprecated but universally available); a
 *   v1.1 follow-up upgrades to AudioWorklet which needs a separate file.
 *   The wire protocol is identical.
 * * AudioContext is opened at 16 kHz where supported.  Browsers that
 *   ignore the sampleRate hint will still produce useful audio; the
 *   resample-step downconverts in-process.
 * * Client-side energy gating keeps background noise from becoming
 *   expensive / hallucinated STT turns.
 */

export interface MicFrameMeta {
  frameIndex: number;
  bytes: number;
  totalBytes: number;
  sampleRate: number;
  durationMs: number;
  rms: number;
}

export type VoiceState = "silent" | "active";

export interface MicSession {
  stop: () => void;
  paused: () => boolean;
  setPaused: (value: boolean) => void;
  contextSampleRate: number;
}

export interface MicOpenOptions {
  /**
   * Called for every audible frame (RMS >= `silenceDropThreshold`).
   * Quiet frames are dropped on the client to save Whisper cost and
   * silence-bucket time on the server — the UI still sees them via
   * `onAllFrames` for the frame counter.
   */
  onFrame: (pcm: ArrayBuffer, meta: MicFrameMeta) => void;
  /**
   * Called for *every* captured frame regardless of energy. Use this
   * for UI counters / waveforms; do NOT send these to the server.
   */
  onAllFrames?: (meta: MicFrameMeta) => void;
  onError?: (err: Error) => void;
  /**
   * VAD transitions (energy-threshold based).  Fires when RMS crosses the
   * `vadThreshold` AFTER N consecutive frames of new state — debounces
   * single-frame fluctuations.  `turnEndMs` is the silence-after-speech
   * gap that triggers `silent` (a "turn just ended" hint).
   */
  onVoiceState?: (state: VoiceState, meta: { rms: number; silenceMs: number }) => void;
  targetSampleRate?: number; // default 16000
  vadThreshold?: number; // default 0.018
  vadActiveFrames?: number; // default 3 (debounce activation)
  turnEndMs?: number; // default 850
  /**
   * Frames with RMS below this threshold are NOT delivered to `onFrame`.
   * Default 0.01 (background room noise) — clean quiet voice still gets
   * through because real speech RMS is typically > 0.02.
   */
  silenceDropThreshold?: number;
}

function floatToInt16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    let s = Math.max(-1, Math.min(1, input[i]));
    s = s < 0 ? s * 0x8000 : s * 0x7fff;
    out[i] = s | 0;
  }
  return out;
}

function resampleLinear(input: Float32Array, srcRate: number, dstRate: number): Float32Array {
  if (srcRate === dstRate) return input;
  const ratio = srcRate / dstRate;
  const outLength = Math.floor(input.length / ratio);
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i += 1) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(input.length - 1, lo + 1);
    const t = srcIdx - lo;
    out[i] = input[lo] * (1 - t) + input[hi] * t;
  }
  return out;
}

function computeRms(samples: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / Math.max(1, samples.length));
}

export async function openMic({
  onFrame,
  onAllFrames,
  onError,
  onVoiceState,
  targetSampleRate = 16000,
  vadThreshold = 0.008,
  vadActiveFrames = 2,
  turnEndMs = 850,
  silenceDropThreshold = 0.002,
}: MicOpenOptions): Promise<MicSession> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("MediaDevices.getUserMedia is not available in this browser.");
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      // Browser AGC can amplify room tone until it looks like speech to
      // energy VAD. Keep it off; the backend STT can handle normal speech levels.
      autoGainControl: false,
      channelCount: 1,
    },
  });
  const audioCtx = new AudioContext({ sampleRate: targetSampleRate });
  const source = audioCtx.createMediaStreamSource(stream);
  // ScriptProcessorNode is deprecated but its inputBuffer.getChannelData is the
  // most portable path for streaming.  Buffer size 2048 = ~128ms at 16kHz.
  const processor = audioCtx.createScriptProcessor(2048, 1, 1);

  let frameIndex = 0;
  let totalBytes = 0;
  let paused = false;
  let activeFrameStreak = 0;
  let voiceState: VoiceState = "silent";
  let lastActiveAt = 0;

  processor.onaudioprocess = (event: AudioProcessingEvent) => {
    if (paused) return;
    try {
      const channel = event.inputBuffer.getChannelData(0);
      const resampled = resampleLinear(channel, audioCtx.sampleRate, targetSampleRate);
      const rms = computeRms(resampled);
      const pcm = floatToInt16(resampled);
      const copy = new ArrayBuffer(pcm.byteLength);
      new Uint8Array(copy).set(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength));
      const buffer = copy;
      frameIndex += 1;
      totalBytes += pcm.byteLength;

      // VAD: debounce activation (require N consecutive loud frames);
      // turn-end fires after a configurable silence gap.
      const now = performance.now();
      if (rms >= vadThreshold) {
        activeFrameStreak += 1;
        lastActiveAt = now;
        if (voiceState === "silent" && activeFrameStreak >= vadActiveFrames) {
          voiceState = "active";
          onVoiceState?.("active", { rms, silenceMs: 0 });
        }
      } else {
        activeFrameStreak = 0;
        if (voiceState === "active") {
          const silenceMs = lastActiveAt === 0 ? 0 : now - lastActiveAt;
          if (silenceMs >= turnEndMs) {
            voiceState = "silent";
            onVoiceState?.("silent", { rms, silenceMs });
          }
        }
      }

      const meta: MicFrameMeta = {
        frameIndex,
        bytes: pcm.byteLength,
        totalBytes,
        sampleRate: targetSampleRate,
        durationMs: (resampled.length / targetSampleRate) * 1000,
        rms,
      };
      onAllFrames?.(meta);
      // Energy pre-gate: drop frames below silenceDropThreshold so the
      // server only ever sees audible PCM. Cuts Whisper cost + silence-
      // gate false positives at the source.
      if (rms >= silenceDropThreshold) {
        onFrame(buffer, meta);
      }
    } catch (err) {
      onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  };

  source.connect(processor);
  // ScriptProcessorNode requires a downstream node to actually pull audio;
  // using a muted GainNode keeps the graph alive without producing speaker output.
  const sink = audioCtx.createGain();
  sink.gain.value = 0;
  processor.connect(sink);
  sink.connect(audioCtx.destination);

  return {
    contextSampleRate: audioCtx.sampleRate,
    paused: () => paused,
    setPaused: (value: boolean) => {
      paused = value;
    },
    stop: () => {
      try {
        processor.disconnect();
        source.disconnect();
        sink.disconnect();
      } catch {
        // ignore
      }
      try {
        stream.getTracks().forEach((t) => t.stop());
      } catch {
        // ignore
      }
      audioCtx.close().catch(() => undefined);
    },
  };
}
