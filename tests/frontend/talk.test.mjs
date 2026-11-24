// Tests of the microphone of the live card: node --test "tests/frontend/*.test.mjs"
import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";

import { TALK_ENDS, TEXT } from "../../custom_components/hikvision_bridge/frontend/hikvision-bridge-live-card.js";
import {
  MICROPHONE,
  Microphone,
  binaryMessage,
  unavailable,
} from "../../custom_components/hikvision_bridge/frontend/talk.js";

/** The parts of a browser the microphone uses, recording what happens to them. */
function browser({ allow = true } = {}) {
  const world = { nodes: [], modules: [], tracksStopped: 0, closed: 0, asked: [] };
  globalThis.isSecureContext = true;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      mediaDevices: {
        getUserMedia: async (constraints) => {
          world.asked.push(constraints);
          if (!allow) throw new Error("NotAllowedError");
          return { getTracks: () => [{ stop: () => (world.tracksStopped += 1) }] };
        },
      },
    },
  });
  globalThis.AudioContext = class {
    audioWorklet = { addModule: async (url) => world.modules.push(String(url)) };
    createMediaStreamSource() {
      return { connect: (node) => (world.connected = node) };
    }
    async resume() {}
    async close() {
      world.closed += 1;
    }
  };
  globalThis.AudioWorkletNode = class {
    constructor(_context, name, options) {
      this.name = name;
      this.options = options;
      this.port = {};
      world.nodes.push(this);
    }
  };
  return world;
}

/** A Home Assistant whose websocket answers the talk as the integration does. */
function homeAssistant({ refuse = false, events = [] } = {}) {
  const sent = [];
  const hass = {
    sent,
    subscriptions: [],
    unsubscribed: 0,
    connection: {
      socket: { readyState: WebSocket.OPEN, send: (data) => sent.push(data) },
      subscribeMessage: async (callback, message) => {
        if (refuse) throw { code: "talk_failed" };
        hass.subscriptions.push({ callback, message });
        // After the result, as Home Assistant sends them
        setTimeout(() => events.forEach((event) => callback(event)));
        return async () => (hass.unsubscribed += 1);
      },
    },
  };
  return hass;
}

const STARTED = { type: "started", handler_id: 4, codec: "G.711alaw" };

describe("the microphone", () => {
  let world;
  beforeEach(() => {
    world = browser();
  });

  it("sends each frame to the handler of the talk", async () => {
    const hass = homeAssistant({ events: [STARTED] });
    const microphone = new Microphone(hass, "door_entry", () => {});
    await microphone.start();

    assert.deepEqual(world.asked, [{ audio: MICROPHONE }]);
    assert.deepEqual(hass.subscriptions[0].message, { type: "hikvision_bridge/talk", entry_id: "door_entry" });
    assert.ok(world.modules[0].endsWith("/talk-worklet.js"));
    const [node] = world.nodes;
    assert.equal(node.name, "hikvision-bridge-talk");
    assert.deepEqual(node.options, { numberOfOutputs: 0, processorOptions: { codec: "G.711alaw" } });
    assert.equal(world.connected, node);

    node.port.onmessage({ data: new Uint8Array([1, 2, 3]) });
    assert.deepEqual(hass.sent, [new Uint8Array([4, 1, 2, 3])]);

    await microphone.stop();
    assert.equal(hass.unsubscribed, 1);
    assert.equal(world.tracksStopped, 1);
    assert.equal(world.closed, 1);
    node.port.onmessage({ data: new Uint8Array([9]) });
    assert.equal(hass.sent.length, 1);
  });

  it("sends nothing while Home Assistant is away", async () => {
    const hass = homeAssistant({ events: [STARTED] });
    const microphone = new Microphone(hass, "door_entry", () => {});
    await microphone.start();
    hass.connection.socket.readyState = WebSocket.CLOSED;

    world.nodes[0].port.onmessage({ data: new Uint8Array([1]) });
    assert.equal(hass.sent.length, 0);
  });

  it("tells when the device or someone else ends the talk", async () => {
    const ends = [];
    const hass = homeAssistant({ events: [STARTED, { type: "ended", reason: "replaced" }] });
    const microphone = new Microphone(hass, "door_entry", (reason) => ends.push(reason));
    await microphone.start();
    await new Promise((resolve) => setTimeout(resolve));

    assert.deepEqual(ends, ["replaced"]);
    assert.equal(world.tracksStopped, 1);
    // The integration ended it already: nothing to unsubscribe
    await microphone.stop();
    assert.equal(hass.unsubscribed, 0);
  });

  it("refused by the browser, never reaches the device", async () => {
    world = browser({ allow: false });
    const hass = homeAssistant({ events: [STARTED] });

    await assert.rejects(new Microphone(hass, "door_entry", () => {}).start(), { message: "talk_denied" });
    assert.equal(hass.subscriptions.length, 0);
    assert.equal(world.closed, 1);
  });

  it("refused by the device, releases the microphone", async () => {
    const hass = homeAssistant({ refuse: true });

    await assert.rejects(new Microphone(hass, "door_entry", () => {}).start(), { message: "talk_failed" });
    assert.equal(world.tracksStopped, 1);
  });

  it("stopped while the browser asks for the permission, never reaches the device", async () => {
    const hass = homeAssistant({ events: [STARTED] });
    const microphone = new Microphone(hass, "door_entry", () => {});
    const starting = microphone.start();
    await microphone.stop();
    await starting;

    assert.equal(hass.subscriptions.length, 0);
    assert.equal(world.tracksStopped, 1);
  });

  it("stopped while the device opens, closes it again", async () => {
    const hass = homeAssistant();
    const microphone = new Microphone(hass, "door_entry", () => {});
    const starting = microphone.start();
    while (!hass.subscriptions.length) await new Promise((resolve) => setTimeout(resolve));
    await microphone.stop();
    await starting;

    assert.equal(hass.unsubscribed, 1);
    assert.equal(world.nodes.length, 0);
  });
});

describe("what the browser needs", () => {
  it("is a secure page, a microphone and audio worklets", () => {
    const media = { mediaDevices: { getUserMedia() {} } };

    assert.equal(unavailable({ isSecureContext: false, navigator: media }), "talk_https");
    assert.equal(unavailable({ isSecureContext: true, navigator: {} }), "talk_unsupported");
    assert.equal(unavailable({ isSecureContext: true, navigator: media }), "talk_unsupported");
    assert.equal(unavailable({ isSecureContext: true, navigator: media, AudioWorkletNode: class {} }), null);
  });

  it("frames carry the handler of the talk first", () => {
    assert.deepEqual(binaryMessage(7, new Uint8Array([1, 2])), new Uint8Array([7, 1, 2]));
  });
});

describe("the card", () => {
  it("has a text for every way a talk can fail or end", () => {
    for (const key of ["talk_https", "talk_unsupported", "talk_denied", "talk_failed", ...Object.values(TALK_ENDS)]) {
      assert.ok(TEXT[key], key);
    }
    assert.equal(TALK_ENDS.stopped, undefined);
  });
});
