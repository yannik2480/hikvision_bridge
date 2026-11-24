// @ts-check
/**
 * The sound of the microphone as the device's speaker takes it: 8 kHz, one channel, G.711
 * (μ-law or A-law, whichever the device lists), 20 ms at a time. Plain functions, shared by the
 * audio worklet that runs them (talk-worklet.js) and by the tests.
 */

/** The rate G.711 is sampled at. */
export const RATE = 8000;

/** Samples in a frame: 20 ms, the packet size of telephone sound. */
export const FRAME_SAMPLES = 160;

/** The codecs a device can ask for, by the name it gives them. */
export const CODECS = /** @type {const} */ (["G.711ulaw", "G.711alaw"]);

/**
 * Return a sample of the browser (-1 to 1) as a 16-bit one.
 *
 * @param {number} value
 */
export function toInt16(value) {
  const clipped = Math.max(-1, Math.min(1, value));
  return Math.round(clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff);
}

/**
 * Return a 16-bit sample in μ-law, as ITU-T G.711 encodes it.
 *
 * @param {number} sample
 */
export function muLaw(sample) {
  let value = sample >> 2;
  let mask = 0xff;
  if (value < 0) {
    mask = 0x7f;
    value = -value;
  }
  value = Math.min(value, 8159) + 0x21;
  const segment = MU_LAW_SEGMENTS.findIndex((end) => value <= end);
  if (segment < 0) return 0x7f ^ mask;
  return ((segment << 4) | ((value >> (segment + 1)) & 0x0f)) ^ mask;
}

/** The upper end of each μ-law segment, on 14 bits. */
const MU_LAW_SEGMENTS = [0x3f, 0x7f, 0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff, 0x1fff];

/** The upper end of each A-law segment, on 13 bits. */
const A_LAW_SEGMENTS = [0x1f, 0x3f, 0x7f, 0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff];

/**
 * Return a 16-bit sample in A-law, as ITU-T G.711 encodes it.
 *
 * @param {number} sample
 */
export function aLaw(sample) {
  let value = sample >> 3;
  let mask = 0xd5;
  if (value < 0) {
    mask = 0x55;
    value = -value - 1;
  }
  const segment = A_LAW_SEGMENTS.findIndex((end) => value <= end);
  if (segment < 0) return 0x7f ^ mask;
  const step = segment < 2 ? 1 : segment;
  return ((segment << 4) | ((value >> step) & 0x0f)) ^ mask;
}

/**
 * Return a frame of samples of the browser in a G.711 codec.
 *
 * @param {Float32Array} samples
 * @param {string} codec "G.711ulaw" or "G.711alaw".
 */
export function encodeFrame(samples, codec) {
  const encode = codec === "G.711alaw" ? aLaw : muLaw;
  const frame = new Uint8Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) frame[i] = encode(toInt16(samples[i]));
  return frame;
}

/**
 * Brings the sound of the browser (44.1 or 48 kHz) down to 8 kHz: each sample out is the mean of
 * the samples in its span, which also takes out most of what G.711 cannot carry above 4 kHz.
 */
export class Downsampler {
  /**
   * @param {number} inputRate The rate of the browser's audio.
   * @param {number} [outputRate]
   */
  constructor(inputRate, outputRate = RATE) {
    this.ratio = inputRate / outputRate;
    this.sum = 0;
    this.count = 0;
    this.position = 0;
  }

  /**
   * Take samples in, and call back with each sample out.
   *
   * @param {Float32Array} input
   * @param {(sample: number) => void} emit
   */
  push(input, emit) {
    for (let i = 0; i < input.length; i += 1) {
      this.sum += input[i];
      this.count += 1;
      this.position += 1;
      if (this.position >= this.ratio) {
        emit(this.sum / this.count);
        this.position -= this.ratio;
        this.sum = 0;
        this.count = 0;
      }
    }
  }
}

/**
 * Cuts the 8 kHz sound into frames of 20 ms, each encoded as soon as it is full.
 */
export class Framer {
  /**
   * @param {number} inputRate
   * @param {string} codec
   * @param {(frame: Uint8Array) => void} send
   */
  constructor(inputRate, codec, send) {
    this.downsampler = new Downsampler(inputRate);
    this.codec = codec;
    this.send = send;
    this.frame = new Float32Array(FRAME_SAMPLES);
    this.filled = 0;
  }

  /** @param {Float32Array} input */
  push(input) {
    this.downsampler.push(input, (sample) => {
      this.frame[this.filled] = sample;
      this.filled += 1;
      if (this.filled === FRAME_SAMPLES) {
        this.send(encodeFrame(this.frame, this.codec));
        this.filled = 0;
      }
    });
  }
}
