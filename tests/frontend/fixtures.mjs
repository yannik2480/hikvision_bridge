// Fixtures of the tests of the frontend: cameras of Hikvision Bridge as Home Assistant has them.

/** The camera entities: a door station with its main and sub stream, and a recorder channel. */
const CAMERAS = {
  "camera.front_door_main_stream": { device: "door", key: "main_stream", name: "Front door Main stream" },
  "camera.front_door_sub_stream": { device: "door", key: "sub_stream", name: "Front door Sub stream" },
  "camera.garden_main_stream": { device: "garden", key: "main_stream", name: "Garden Main stream" },
};

/**
 * Return a Home Assistant with the cameras of the integration, a camera of another one and
 * answers to the websocket commands the card sends.
 *
 * @param {Record<string, any>} answers The result of each command, by its type.
 */
export function homeAssistant(answers = {}) {
  /** @type {Record<string, any>} */
  const states = { "camera.other": { attributes: { friendly_name: "Other camera" } } };
  /** @type {Record<string, any>} */
  const entities = { "camera.other": { platform: "generic", device_id: "other" } };
  for (const [id, camera] of Object.entries(CAMERAS)) {
    states[id] = { attributes: { friendly_name: camera.name } };
    entities[id] = { platform: "hikvision_bridge", device_id: camera.device, translation_key: camera.key };
  }
  const sent = [];
  return {
    states,
    entities,
    language: "en",
    sent,
    callWS: async (message) => {
      sent.push(message);
      if (!(message.type in answers)) throw new Error(`unexpected ${message.type}`);
      return answers[message.type];
    },
  };
}

/** The cameras as `hikvision_bridge/cameras` lists them. */
export const BRIDGE_CAMERAS = [
  {
    entry_id: "door_entry",
    channel_id: "1",
    live: { high: "camera.front_door_main_stream", low: "camera.front_door_sub_stream" },
    audio: { 1: true, 2: true },
    audio_codec: { 1: "G.711ulaw", 2: "G.711ulaw" },
  },
  {
    entry_id: "nvr_entry",
    channel_id: "3",
    live: { high: "camera.garden_main_stream" },
    audio: { 1: false },
    audio_codec: {},
  },
];
