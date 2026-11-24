// @ts-check
/**
 * The microphone of the live card: talking through the speaker of a device.
 *
 * The integration opens the device's two-way audio (`hikvision_bridge/talk`, a subscription
 * that lasts as long as the talk) and says which G.711 variant it takes. An audio worklet
 * (talk-worklet.js) encodes the microphone in it, 20 ms at a time, and the frames go to Home
 * Assistant as binary messages of its websocket, the way Assist sends its microphone.
 */

import { DOMAIN } from "./player.js";

/**
 * @typedef {"stopped" | "replaced" | "device" | "unloaded"} TalkEnd Why a talk ended: the
 *   person stopped it; someone else took the device over; the device ended it; the integration
 *   was unloaded.
 *
 * @typedef {object} TalkHass The parts of Home Assistant the microphone uses.
 * @property {any} connection
 */

/** Settings of the microphone: the browser takes the echo of the door's sound out. */
export const MICROPHONE = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };

/**
 * A message of the websocket with a frame of sound: the handler it goes to, then the frame.
 *
 * @param {number} handler
 * @param {Uint8Array} frame
 */
export function binaryMessage(handler, frame) {
  const message = new Uint8Array(frame.length + 1);
  message[0] = handler;
  message.set(frame, 1);
  return message;
}

/**
 * Return why the microphone cannot be used in this browser, or null when it can.
 *
 * @param {{ isSecureContext?: boolean, navigator?: any }} scope
 */
export function unavailable(scope) {
  if (!scope.isSecureContext) return "talk_https";
  if (!scope.navigator?.mediaDevices?.getUserMedia || !(/** @type {any} */ (scope).AudioWorkletNode)) {
    return "talk_unsupported";
  }
  return null;
}

/** A talk through one device, from its start to its end. */
export class Microphone {
  /**
   * @param {TalkHass} hass
   * @param {string} entryId The config entry of the device.
   * @param {(reason: TalkEnd) => void} onEnd Called when the talk ends without stop().
   */
  constructor(hass, entryId, onEnd) {
    this.hass = hass;
    this.entryId = entryId;
    this.onEnd = onEnd;
    /** @type {number | null} The binary handler of the talk, once the device accepted. */
    this.handler = null;
    /** @type {MediaStream | null} */
    this.stream = null;
    /** @type {AudioContext | null} */
    this.context = null;
    /** @type {(() => Promise<void>) | null} */
    this.unsubscribe = null;
    this.ended = false;
    /** Whether the integration ended the talk: its subscription is gone already. */
    this.closedByIntegration = false;
    /** @type {(codec: string | null) => void} Called once the device accepted, or the talk ended. */
    this.accept = () => {};
  }

  /**
   * Ask for the microphone, open the device's speaker, then send. Rejects with the code of a
   * text of the card: talk_https, talk_unsupported, talk_denied or talk_failed.
   */
  async start() {
    const problem = unavailable(/** @type {any} */ (globalThis));
    if (problem) throw new Error(problem);
    // Created in the click that started the talk, the browser lets it run at once
    this.context = new AudioContext();
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: MICROPHONE });
    } catch (_err) {
      await this.#release();
      throw new Error("talk_denied");
    }
    // Stopped while the browser asked for the permission
    if (this.ended) return this.#release();
    /** @type {Promise<string | null>} */
    const accepted = new Promise((resolve) => (this.accept = resolve));
    try {
      this.unsubscribe = await this.hass.connection.subscribeMessage(
        (/** @type {any} */ event) => {
          if (event.type === "started") {
            // Again after Home Assistant reconnected: the handler is a new one
            this.handler = event.handler_id;
            this.accept(event.codec);
          } else if (event.type === "ended") {
            this.#end(event.reason);
          }
        },
        { type: `${DOMAIN}/talk`, entry_id: this.entryId },
      );
    } catch (_err) {
      // The device did not open its speaker, or has none
      await this.#release();
      throw new Error("talk_failed");
    }
    // Stopped while the device was opening: unsubscribing closes it again
    if (this.ended) return this.#leave();
    const codec = await accepted;
    if (this.ended || !codec || !this.context || !this.stream) return;
    await this.context.audioWorklet.addModule(new URL("./talk-worklet.js", import.meta.url));
    const node = new AudioWorkletNode(this.context, "hikvision-bridge-talk", {
      numberOfOutputs: 0,
      processorOptions: { codec },
    });
    node.port.onmessage = ({ data }) => this.#send(data);
    this.context.createMediaStreamSource(this.stream).connect(node);
    this.resume();
  }

  /**
   * Let the audio run. Started without a touch on the page (a card that opens with the
   * microphone on), browsers hold it until the first one: the card calls this on every touch.
   */
  resume() {
    this.context?.resume().catch(() => {});
  }

  /** @param {Uint8Array} frame */
  #send(frame) {
    const socket = this.hass.connection.socket;
    if (this.ended || this.handler === null || !socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(binaryMessage(this.handler, frame));
  }

  /** End the talk: the device's speaker closes, the microphone is released. */
  async stop() {
    if (this.ended) return;
    this.ended = true;
    this.accept(null);
    await this.#release();
    await this.#leave();
  }

  /** Unsubscribe from the talk, which closes the device's speaker. */
  async #leave() {
    const unsubscribe = this.closedByIntegration ? null : this.unsubscribe;
    this.unsubscribe = null;
    try {
      await unsubscribe?.();
    } catch (_err) {
      /* the connection is gone, and the talk with it */
    }
  }

  /** @param {TalkEnd} reason */
  #end(reason) {
    if (this.ended) return;
    this.ended = true;
    this.closedByIntegration = true;
    this.accept(null);
    this.#release();
    this.onEnd(reason);
  }

  async #release() {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    const context = this.context;
    this.context = null;
    await context?.close().catch(() => {});
  }
}
