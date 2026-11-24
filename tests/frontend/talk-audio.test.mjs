// Tests of the sound of the microphone: node --test "tests/frontend/*.test.mjs"
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { describe, it } from "node:test";

import {
  Downsampler,
  FRAME_SAMPLES,
  Framer,
  RATE,
  aLaw,
  encodeFrame,
  muLaw,
  toInt16,
} from "../../custom_components/hikvision_bridge/frontend/talk-audio.js";

/**
 * Return the SHA-256 of a codec applied to every 16-bit sample, from -32768 up. The expected
 * ones are of Python's audioop (lin2ulaw, lin2alaw), the reference G.711 of ITU-T G.191.
 */
function tableHash(encode) {
  const table = new Uint8Array(65536);
  for (let sample = -32768; sample < 32768; sample += 1) table[sample + 32768] = encode(sample);
  return createHash("sha256").update(table).digest("hex");
}

describe("G.711", () => {
  it("encodes every sample as the reference does", () => {
    assert.equal(tableHash(muLaw), "81d633c9e6972a18c74a58720b96cb8ca0bdd096d4060b646dd708c3b846019a");
    assert.equal(tableHash(aLaw), "38488f6fd710f4686360edc4d38639f96c491595ef93f8eb8d62d5e07ca6ce7b");
  });

  it("has the silence of each codec", () => {
    assert.equal(muLaw(0), 0xff);
    assert.equal(aLaw(0), 0xd5);
  });

  it("takes the samples of the browser, clipped", () => {
    assert.equal(toInt16(0), 0);
    assert.equal(toInt16(1), 32767);
    assert.equal(toInt16(-1), -32768);
    assert.equal(toInt16(2), 32767);
    assert.equal(toInt16(-2), -32768);
  });

  it("encodes a frame in the codec the device asks for", () => {
    const silence = new Float32Array(FRAME_SAMPLES);

    assert.deepEqual(encodeFrame(silence, "G.711ulaw"), new Uint8Array(FRAME_SAMPLES).fill(0xff));
    assert.deepEqual(encodeFrame(silence, "G.711alaw"), new Uint8Array(FRAME_SAMPLES).fill(0xd5));
  });
});

describe("the rate", () => {
  for (const rate of [48000, 44100, 16000]) {
    it(`goes from ${rate} Hz to 8 kHz`, () => {
      const out = [];
      const downsampler = new Downsampler(rate);
      // One second, in the blocks of 128 samples an audio worklet gets
      const block = new Float32Array(128).fill(0.5);
      const blocks = Math.ceil(rate / 128);
      for (let i = 0; i < blocks; i += 1) downsampler.push(block, (sample) => out.push(sample));

      const expected = Math.floor((blocks * 128 * RATE) / rate);
      assert.ok(Math.abs(out.length - expected) <= 1, `${out.length} samples, not ${expected}`);
      assert.ok(out.every((sample) => Math.abs(sample - 0.5) < 1e-6));
    });
  }

  it("takes out what G.711 cannot carry: a tone at the Nyquist rate of the browser", () => {
    const out = [];
    const downsampler = new Downsampler(48000);
    const tone = Float32Array.from({ length: 4800 }, (_, i) => (i % 2 ? 1 : -1));
    downsampler.push(tone, (sample) => out.push(sample));

    assert.ok(out.every((sample) => Math.abs(sample) < 1e-6));
  });
});

describe("the frames", () => {
  it("are 20 ms, sent as soon as they are full", () => {
    const frames = [];
    const framer = new Framer(48000, "G.711ulaw", (frame) => frames.push(frame));
    // 50 ms at 48 kHz: two frames, and half of the third waiting
    framer.push(new Float32Array(2400));

    assert.equal(frames.length, 2);
    assert.ok(frames.every((frame) => frame.length === FRAME_SAMPLES));
    assert.equal(framer.filled, FRAME_SAMPLES / 2);
  });
});
