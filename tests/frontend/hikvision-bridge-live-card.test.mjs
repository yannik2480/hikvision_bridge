// Tests of the live card: node --test "tests/frontend/*.test.mjs"
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  CARD,
  TEXT,
  bridgeCameras,
  cameraOf,
  checkConfig,
  cleanConfig,
  editorData,
  editorSchema,
  streamName,
  streamsOf,
  stubConfig,
  translator,
} from "../../custom_components/hikvision_bridge/frontend/hikvision-bridge-live-card.js";
import { BRIDGE_CAMERAS, homeAssistant } from "./fixtures.mjs";

const MAIN = "camera.front_door_main_stream";
const SUB = "camera.front_door_sub_stream";

/** The texts of the integration a frontend in German has. */
const GERMAN = {
  "component.hikvision_bridge.selector.ui.options.streams": "Streams",
  "component.hikvision_bridge.selector.ui.options.start_muted": "Ohne Ton starten",
  "component.hikvision_bridge.entity.camera.main_stream.name": "Hauptstream",
};

describe("the configuration", () => {
  it("lists the streams, or the one entity", () => {
    const streams = [{ entity: MAIN, name: "Door" }, { entity: SUB }];

    assert.deepEqual(streamsOf({ streams }), streams);
    assert.deepEqual(streamsOf({ entity: MAIN }), [{ entity: MAIN }]);
    assert.deepEqual(streamsOf({}), []);
  });

  it("accepts streams of camera entities, or one entity", () => {
    checkConfig({ streams: [{ entity: MAIN, name: "Door" }, { entity: SUB }], muted: false });
    checkConfig({ entity: MAIN, microphone: true });
  });

  it("explains what is wrong", () => {
    const wrong = [
      [{}, /needs a camera entity/],
      [{ streams: [] }, /needs a camera entity/],
      [{ streams: MAIN }, /must be a list/],
      [{ entity: MAIN, streams: [{ entity: SUB }] }, /either entity or streams/],
      // A stream of the WebRTC Camera card: a go2rtc stream, not an entity
      [{ streams: [{ url: "doorbell", media: "video,audio" }] }, /entity of a camera/],
      [{ entity: "sensor.door" }, /entity of a camera/],
      [{ streams: [{ entity: MAIN, name: 1 }] }, /name of camera.front_door_main_stream/],
      [{ entity: MAIN, muted: "no" }, /muted must be true or false/],
      [{ entity: MAIN, microphone: "on" }, /microphone must be true or false/],
    ];
    for (const [config, message] of wrong) {
      assert.throws(() => checkConfig(config), message, JSON.stringify(config));
    }
  });
});

describe("the visual editor", () => {
  it("edits a card of one entity as a list of one stream", () => {
    assert.deepEqual(editorData({ type: `custom:${CARD}`, entity: MAIN }), {
      type: `custom:${CARD}`,
      muted: true,
      microphone: false,
      streams: [{ entity: MAIN }],
    });
  });

  it("keeps the YAML short: no empty names, no default options", () => {
    const value = {
      type: `custom:${CARD}`,
      entity: MAIN,
      muted: true,
      microphone: false,
      streams: [{ entity: MAIN, name: "  Door  " }, { entity: SUB, name: " " }, { name: "half added" }],
    };

    assert.deepEqual(cleanConfig(value), {
      type: `custom:${CARD}`,
      streams: [{ entity: MAIN, name: "Door" }, { entity: SUB }],
    });
    assert.equal(cleanConfig({ streams: [], muted: false }).muted, false);
    assert.equal(cleanConfig({ streams: [], microphone: true }).microphone, true);
  });

  it("lists the streams with a camera of the integration and a name", () => {
    const [streams, muted, microphone] = editorSchema(translator(GERMAN));

    assert.equal(streams.name, "streams");
    assert.equal(streams.selector.object.multiple, true);
    assert.deepEqual(streams.selector.object.fields.entity.selector, {
      entity: { domain: "camera", integration: "hikvision_bridge" },
    });
    assert.equal(streams.selector.object.fields.entity.label, TEXT.stream_camera);
    assert.equal(muted.label, "Ohne Ton starten");
    assert.equal(microphone.name, "microphone");
    assert.equal(microphone.label, TEXT.start_talking);
  });
});

describe("a new card", () => {
  it("lists the cameras of the integration, by device", () => {
    assert.deepEqual(bridgeCameras(homeAssistant()), [MAIN, SUB, "camera.garden_main_stream"]);
  });

  it("starts with the streams of the first camera", () => {
    assert.deepEqual(stubConfig(homeAssistant()), { streams: [{ entity: MAIN }, { entity: SUB }] });
  });

  it("without cameras of the integration has no streams", () => {
    const hass = homeAssistant();
    hass.entities = {};

    assert.deepEqual(stubConfig(hass), { streams: [] });
  });
});

describe("the streams", () => {
  it("are the main or the sub stream of a camera of the integration", () => {
    assert.deepEqual(cameraOf(BRIDGE_CAMERAS, MAIN), { camera: BRIDGE_CAMERAS[0], level: "high" });
    assert.deepEqual(cameraOf(BRIDGE_CAMERAS, SUB), { camera: BRIDGE_CAMERAS[0], level: "low" });
    assert.equal(cameraOf(BRIDGE_CAMERAS, "camera.other"), undefined);
  });

  it("are named as configured, else by their entity in the language of the user", () => {
    const hass = homeAssistant();

    assert.equal(streamName(hass, GERMAN, { entity: MAIN, name: "Tür" }), "Tür");
    assert.equal(streamName(hass, GERMAN, { entity: MAIN }), "Hauptstream");
    assert.equal(streamName(hass, {}, { entity: SUB }), "Front door Sub stream");
    assert.equal(streamName(hass, {}, { entity: "camera.removed" }), "camera.removed");
  });
});

describe("the texts", () => {
  it("are those of the integration in English, which every language translates", () => {
    const english = JSON.parse(
      readFileSync(new URL("../../custom_components/hikvision_bridge/translations/en.json", import.meta.url), "utf8"),
    ).selector.ui.options;

    for (const [key, text] of Object.entries(TEXT)) {
      assert.equal(english[key], text, key);
    }
  });

  it("are translated, else in English, else the key", () => {
    const text = translator(GERMAN);

    assert.equal(text("start_muted"), "Ohne Ton starten");
    assert.equal(text("sound_on"), "Sound on");
    assert.equal(text("unknown_key"), "unknown_key");
  });
});
