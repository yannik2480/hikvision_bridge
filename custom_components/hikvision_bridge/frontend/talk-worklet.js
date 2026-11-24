// @ts-nocheck: an audio worklet, whose globals (AudioWorkletProcessor, sampleRate) only exist there
/**
 * The audio worklet of the microphone: the browser's sound in, frames of 20 ms of G.711 out to
 * the page (talk.js), which sends them on. It runs on the audio thread, so the encoding never
 * waits for the page.
 */

import { Framer } from "./talk-audio.js";

class HikvisionBridgeTalk extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const { codec } = options.processorOptions;
    this.framer = new Framer(sampleRate, codec, (frame) => this.port.postMessage(frame, [frame.buffer]));
  }

  process(inputs) {
    // The first channel of the microphone: speech is mono, and G.711 has one channel
    const channel = inputs[0]?.[0];
    if (channel) this.framer.push(channel);
    return true;
  }
}

registerProcessor("hikvision-bridge-talk", HikvisionBridgeTalk);
