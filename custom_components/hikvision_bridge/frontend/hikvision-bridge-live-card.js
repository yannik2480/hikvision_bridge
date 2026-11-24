// @ts-check
/**
 * The live card, `custom:hikvision-bridge-live-card`: the live picture of a camera of the
 * integration, with its sound, and buttons to switch between the streams the card lists, such
 * as the main and the sub stream of a door station. Its visual editor lists the streams, each a
 * camera entity with an optional name, in the order of the buttons.
 *
 * It plays the streams as the panel does (player.js): WebRTC through Home Assistant's camera
 * signalling, which carries the sound of the device as it is; HLS when that is missing, with the
 * integration's converted session for the sound Home Assistant's HLS drops.
 *
 * The integration serves this file next to the panel and has the frontend load it
 * (custom_components/hikvision_bridge/panel.py), so the card is in the card picker as soon as
 * the integration is installed.
 */

import { DOMAIN, Player } from "./player.js";
import { Microphone, unavailable } from "./talk.js";

/**
 * @typedef {object} Stream A stream the card can show.
 * @property {string} entity A camera entity of the integration.
 * @property {string} [name] The name on its button; the name of the entity without it.
 *
 * @typedef {object} CardConfig
 * @property {string} [type]
 * @property {Stream[]} [streams] The streams, in the order of their buttons; the first plays.
 * @property {string} [entity] A camera entity: the card with this one stream.
 * @property {boolean} [muted] Starts without sound; true without the option.
 *
 * @typedef {object} BridgeCamera A camera as `hikvision_bridge/cameras` describes it.
 * @property {string} entry_id
 * @property {string} channel_id
 * @property {{ high?: string, low?: string }} live The camera entities of its main and sub stream.
 * @property {boolean} [talk] Whether its device plays sound from Home Assistant (the microphone).
 *
 * @typedef {"high" | "low"} Level The main stream, or the sub stream.
 *
 * @typedef {object} Hass The parts of Home Assistant the card reads.
 * @property {Record<string, { attributes: Record<string, any> }>} states
 * @property {Record<string, { platform?: string, device_id?: string | null, translation_key?: string }>} [entities]
 * @property {string} [language]
 * @property {(message: Record<string, any>) => Promise<any>} callWS
 * @property {any} connection
 *
 * @typedef {Record<string, string>} Resources Translations, as `frontend/get_translations` returns them.
 *
 * @typedef {Record<string, any>} FormField A field of the visual editor, as `ha-form` draws it.
 */

/** The name of the card, `custom:hikvision-bridge-live-card` in a dashboard. */
export const CARD = "hikvision-bridge-live-card";

/** English texts; the translations come from the integration (selector "ui"). */
export const TEXT = {
  live: "Live",
  fullscreen: "Full screen",
  sound_on: "Sound on",
  sound_off: "Sound off",
  no_sound: "This stream has no sound.",
  live_unavailable: "No live stream for this camera.",
  live_encrypted: "The live stream is encrypted (Hik-Connect stream encryption) and cannot be played here.",
  playback_failed: "Playback failed.",
  no_ffmpeg: "ffmpeg is not available in Home Assistant.",
  streams: "Streams",
  stream_camera: "Camera",
  stream_name: "Name",
  start_muted: "Start without sound",
  talk_start: "Talk",
  talk_stop: "Stop talking",
  talk_https: "The microphone needs a secure connection (HTTPS).",
  talk_unsupported: "This browser cannot send the microphone.",
  talk_denied: "The browser did not allow the microphone.",
  talk_failed: "The device did not accept the sound.",
  talk_replaced: "Someone else is now talking through this device.",
  talk_ended: "The device ended the talk.",
};

/** What the card tells when a talk ends without the person stopping it, by the reason. */
export const TALK_ENDS = { replaced: "talk_replaced", device: "talk_ended", unloaded: "talk_ended" };

/** Seconds a notice about the microphone stays on the picture. */
const NOTICE_SECONDS = 5;

/** The options of the card that are what it does without them. */
export const DEFAULTS = { muted: true };

/**
 * Return the streams of a card: its list, or its one entity.
 *
 * @param {CardConfig} config
 * @returns {Stream[]}
 */
export function streamsOf(config) {
  if (config.streams) return config.streams;
  return config.entity ? [{ entity: config.entity }] : [];
}

/**
 * Check the configuration of a card, as Home Assistant shows its errors.
 *
 * @param {CardConfig} config
 */
export function checkConfig(config) {
  if (!config || typeof config !== "object") throw new Error("The card needs a configuration");
  if (config.streams !== undefined && !Array.isArray(config.streams)) {
    throw new Error("streams must be a list of camera entities");
  }
  if (config.entity !== undefined && config.streams !== undefined) {
    throw new Error("Use either entity or streams");
  }
  const streams = streamsOf(config);
  if (!streams.length) throw new Error("The card needs a camera entity in entity or streams");
  for (const stream of streams) {
    if (!stream || typeof stream.entity !== "string" || !stream.entity.startsWith("camera.")) {
      throw new Error("Each stream needs the entity of a camera of Hikvision Bridge");
    }
    if (stream.name !== undefined && typeof stream.name !== "string") {
      throw new Error(`The name of ${stream.entity} must be a text`);
    }
  }
  if (config.muted !== undefined && typeof config.muted !== "boolean") {
    throw new Error("muted must be true or false");
  }
}

/**
 * Return the configuration of a card from its visual editor: the streams without empty
 * names, and without the options that are the defaults, so that its YAML stays short.
 *
 * @param {Record<string, any>} value
 * @returns {CardConfig}
 */
export function cleanConfig(value) {
  /** @type {Record<string, any>} */
  const config = { ...value };
  delete config.entity;
  config.streams = (config.streams ?? [])
    .filter((/** @type {Stream} */ stream) => stream?.entity)
    .map((/** @type {Stream} */ { entity, name }) => (name?.trim() ? { entity, name: name.trim() } : { entity }));
  for (const [option, fallback] of Object.entries(DEFAULTS)) {
    if (config[option] === fallback) delete config[option];
  }
  return config;
}

/**
 * Return the data of the visual editor of a card: a card with one entity is shown as a list
 * of one stream, so that the editor can add more.
 *
 * @param {CardConfig} config
 */
export function editorData(config) {
  const { entity: _entity, ...rest } = config;
  return { ...DEFAULTS, ...rest, streams: streamsOf(config) };
}

/**
 * Return the fields of the visual editor, labeled in the language of the user.
 *
 * @param {(key: string) => string} text
 * @returns {FormField[]}
 */
export function editorSchema(text) {
  return [
    {
      name: "streams",
      label: text("streams"),
      selector: {
        object: {
          multiple: true,
          label_field: "name",
          description_field: "entity",
          fields: {
            entity: {
              label: text("stream_camera"),
              required: true,
              selector: { entity: { domain: "camera", integration: DOMAIN } },
            },
            name: { label: text("stream_name"), selector: { text: {} } },
          },
        },
      },
    },
    { name: "muted", label: text("start_muted"), selector: { boolean: {} } },
  ];
}

/**
 * Return the camera entities of the integration, those of one device after each other.
 *
 * @param {Hass} hass
 */
export function bridgeCameras(hass) {
  return Object.keys(hass.states)
    .filter((id) => id.startsWith("camera.") && hass.entities?.[id]?.platform === DOMAIN)
    .sort((a, b) => {
      const device = (/** @type {string} */ id) => hass.entities?.[id]?.device_id ?? "";
      return device(a).localeCompare(device(b)) || a.localeCompare(b);
    });
}

/**
 * Return the configuration of a new card: the streams of the first camera of the integration,
 * its main and its sub stream.
 *
 * @param {Hass} hass
 * @returns {CardConfig}
 */
export function stubConfig(hass) {
  const cameras = bridgeCameras(hass);
  if (!cameras.length) return { streams: [] };
  const device = hass.entities?.[cameras[0]]?.device_id;
  const streams = device ? cameras.filter((id) => hass.entities?.[id]?.device_id === device) : cameras.slice(0, 1);
  return { streams: streams.map((entity) => ({ entity })) };
}

/**
 * Return the camera a stream belongs to, and which of its streams it is.
 *
 * @param {BridgeCamera[]} cameras
 * @param {string} entity
 * @returns {{ camera: BridgeCamera, level: Level } | undefined}
 */
export function cameraOf(cameras, entity) {
  for (const camera of cameras) {
    if (camera.live.high === entity) return { camera, level: "high" };
    if (camera.live.low === entity) return { camera, level: "low" };
  }
  return undefined;
}

/**
 * Return a translator of the texts of the card.
 *
 * @param {Resources} resources
 * @returns {(key: string) => string}
 */
export function translator(resources) {
  const prefix = `component.${DOMAIN}.selector.ui.options.`;
  return (key) => resources[prefix + key] || TEXT[/** @type {keyof TEXT} */ (key)] || key;
}

/**
 * Return the name on the button of a stream: its own, else the name of its entity in the
 * language of the user ("Main stream"), else the name Home Assistant shows.
 *
 * @param {Hass} hass
 * @param {Resources} resources
 * @param {Stream} stream
 */
export function streamName(hass, resources, stream) {
  if (stream.name) return stream.name;
  const key = hass.entities?.[stream.entity]?.translation_key;
  const translated = key && resources[`component.${DOMAIN}.entity.camera.${key}.name`];
  return translated || hass.states[stream.entity]?.attributes.friendly_name || stream.entity;
}

/** Translations by language, loaded once. @type {Map<string, Promise<Resources>>} */
const translations = new Map();

/**
 * Return the texts of the integration in the language of the user: those of the panel and the
 * names of its entities.
 *
 * @param {Hass} hass
 * @returns {Promise<Resources>}
 */
function loadTexts(hass) {
  const language = hass.language || "en";
  let loaded = translations.get(language);
  if (!loaded) {
    loaded = Promise.all(
      ["selector", "entity"].map((category) =>
        hass
          .callWS({ type: "frontend/get_translations", language, category, integration: [DOMAIN] })
          .then((result) => result.resources)
          .catch(() => ({})),
      ),
    ).then(([selector, entity]) => ({ ...selector, ...entity }));
    translations.set(language, loaded);
  }
  return loaded;
}

/** @param {string} path */
const icon = (path) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`;
const ICONS = {
  fullscreen: icon("M5 5h5v2H7v3H5V5m9 0h5v5h-2V7h-3V5m3 9h2v5h-5v-2h3v-3m-7 3v2H5v-5h2v3h3z"),
  soundOn: icon(
    "M14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.84-5 6.7v2.07c4-.91 7-4.49 7-8.77S18 4.14 14 3.23M16.5 12c0-1.77-1-3.29-2.5-4.03V16c1.5-.71 2.5-2.24 2.5-4M3 9v6h4l5 5V4L7 9H3z",
  ),
  microphone: icon(
    "M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3 3 3 0 0 1-3-3V5a3 3 0 0 1 3-3m7 9c0 3.53-2.61 6.44-6 6.93V21h-2v-3.07c-3.39-.49-6-3.4-6-6.93h2a5 5 0 0 0 5 5 5 5 0 0 0 5-5h2z",
  ),
  soundOff: icon(
    "M12 4 9.91 6.09 12 8.18M4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.26c-.67.51-1.42.93-2.25 1.2v2.07c1.38-.32 2.63-.95 3.68-1.81L19.73 21 21 19.73l-9-9M19 12c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.9 8.9 0 0 0 21 12c0-4.28-3-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71m-2.5 0c0-1.77-1-3.29-2.5-4.03v2.21l2.45 2.45c.05-.2.05-.42.05-.63z",
  ),
};

/** The style of the card; the colours come from the theme of Home Assistant. */
const STYLE = `
  :host { --hb-text: var(--primary-text-color, #e1e1e1); --hb-line: var(--divider-color, rgba(127,127,127,.25)); --hb-accent: var(--primary-color, #03a9f4); }
  ha-card { overflow: hidden; display: flex; flex-direction: column; height: 100%; }
  button { font: inherit; color: inherit; }
  .stage { position: relative; flex: 1; aspect-ratio: 16 / 9; min-height: 0; background: #000; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .stage video { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
  .overlay { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; pointer-events: none; }
  .message { background: rgba(0,0,0,.7); color: #fff; padding: 10px 16px; border-radius: 8px; max-width: 80%; text-align: center; }
  .message.error { border-left: 4px solid var(--error-color, #db4437); }
  .spinner { width: 42px; height: 42px; border-radius: 50%; border: 4px solid rgba(255,255,255,.25); border-top-color: #fff; animation: spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .badge { position: absolute; top: 10px; left: 10px; background: #e5484d; color: #fff; font-size: 12px; padding: 3px 8px; border-radius: 10px; pointer-events: none; }
  .bar { display: flex; align-items: center; gap: 4px; padding: 4px 8px; }
  .spacer { flex: 1; }
  .streams { display: inline-flex; flex-wrap: wrap; border: 1px solid var(--hb-line); border-radius: 18px; overflow: hidden; }
  .streams[hidden] { display: none; }
  .streams button { background: none; border: 0; padding: 6px 14px; cursor: pointer; min-height: 36px; }
  .streams button[aria-pressed="true"] { background: var(--hb-accent); color: var(--text-primary-color, #fff); }
  .icon-button { background: none; border: 0; border-radius: 50%; width: 40px; height: 40px; padding: 8px; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; flex: none; }
  .icon-button:hover { background: color-mix(in srgb, var(--hb-text) 10%, transparent); }
  .icon-button:disabled { opacity: .35; cursor: default; background: none; }
  .icon-button svg { width: 24px; height: 24px; fill: currentColor; }
  .talk { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--hb-line); background: none; border-radius: 18px; padding: 4px 14px 4px 10px; cursor: pointer; min-height: 36px; }
  .talk[hidden] { display: none; }
  .talk svg { width: 20px; height: 20px; fill: currentColor; }
  .talk[aria-pressed="true"] { background: #e5484d; border-color: #e5484d; color: #fff; }
  .talk.starting { opacity: .6; }
  .talk:disabled { opacity: .35; cursor: default; }
  .notice { position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); background: rgba(0,0,0,.75); color: #fff; padding: 8px 14px; border-radius: 8px; max-width: 80%; text-align: center; pointer-events: none; }
  .notice[hidden] { display: none; }
`;

if (globalThis.customElements && globalThis.HTMLElement) {
  /**
   * Have the frontend define its forms, which it loads with the editors of its own cards: the
   * editor of a tile has one.
   */
  const loadForms = async () => {
    if (customElements.get("ha-form")) return;
    const helpers = await /** @type {any} */ (window).loadCardHelpers();
    helpers.createCardElement({ type: "tile", entity: "sun.sun" });
    await customElements.whenDefined("hui-tile-card");
    await /** @type {any} */ (customElements.get("hui-tile-card")).getConfigElement();
  };

  /** The visual editor of the card: a form of Home Assistant. */
  class HikvisionBridgeLiveCardEditor extends HTMLElement {
    /** @type {CardConfig | undefined} */
    #config;

    /** @type {Hass | undefined} */
    #hass;

    /** @type {any} */
    #form;

    /** @type {(key: string) => string} */
    #text = translator({});

    /** @type {string | undefined} */
    #language;

    /** @param {CardConfig} config */
    setConfig(config) {
      this.#config = config;
      this.#render();
    }

    /** @param {Hass} hass */
    set hass(hass) {
      this.#hass = hass;
      if (hass.language !== this.#language) {
        this.#language = hass.language;
        loadTexts(hass).then((resources) => {
          this.#text = translator(resources);
          this.#render();
        });
      }
      this.#render();
    }

    #render() {
      if (!this.#hass || !this.#config) return;
      if (!this.#form) {
        this.#form = document.createElement("ha-form");
        this.#form.computeLabel = (/** @type {FormField} */ field) => field.label ?? field.name;
        this.#form.addEventListener("value-changed", (/** @type {CustomEvent} */ event) => {
          event.stopPropagation();
          this.dispatchEvent(
            new CustomEvent("config-changed", {
              detail: { config: cleanConfig(event.detail.value) },
              bubbles: true,
              composed: true,
            }),
          );
        });
        this.append(this.#form);
      }
      this.#form.hass = this.#hass;
      this.#form.schema = editorSchema(this.#text);
      this.#form.data = editorData(this.#config);
    }
  }

  /**
   * The card. It is the host of a Player, as the panel's viewer is: the player tells it when the
   * picture is loading, failed, or has a sound track.
   */
  class HikvisionBridgeLiveCard extends HTMLElement {
    /** @type {CardConfig} */
    #config = { streams: [] };

    /** @type {Hass | undefined} */
    #hass;

    /** @type {Resources} */
    #resources = {};

    /** @type {BridgeCamera[] | undefined} The cameras of the integration, once asked for. */
    #cameras;

    /** The stream shown, by its place in the list. */
    #current = 0;

    /** @type {any} */
    player = null;

    /** @type {HTMLVideoElement | undefined} */
    video;

    /** Whether sound plays: the player turns it off when the browser refuses autoplay with it. */
    sound = false;

    /** @type {boolean | null} Whether the stream has a sound track; null until known. */
    hasAudio = null;

    /** @type {BridgeCamera | undefined} The camera of the stream shown. */
    #camera;

    /** @type {Microphone | null} The talk through the camera's device, while it lasts. */
    #microphone = null;

    /** Whether the talk is still starting: the permission, then the device. */
    #starting = false;

    /** @type {ReturnType<typeof setTimeout> | undefined} */
    #noticeTimer;

    /** @type {string | null} */
    #error = null;

    #busy = false;

    #onVisibility = () => this.#visibility();

    constructor() {
      super();
      this.attachShadow({ mode: "open" });
    }

    /** @param {Hass} hass */
    static getStubConfig(hass) {
      return stubConfig(hass);
    }

    static async getConfigElement() {
      await loadForms();
      return document.createElement(`${CARD}-editor`);
    }

    /** @param {CardConfig} config */
    setConfig(config) {
      checkConfig(config);
      const changed = JSON.stringify(streamsOf(config)) !== JSON.stringify(streamsOf(this.#config));
      this.#config = config;
      this.sound = !(config.muted ?? DEFAULTS.muted);
      if (!changed || !this.player) return;
      this.#current = 0;
      this.#render();
      this.#play();
    }

    getCardSize() {
      return 5;
    }

    getGridOptions() {
      return { columns: 12, min_columns: 6 };
    }

    /** @param {Hass} hass */
    set hass(hass) {
      const first = !this.#hass;
      const language = this.#hass?.language;
      this.#hass = hass;
      if (first || hass.language !== language) {
        loadTexts(hass).then((resources) => {
          this.#resources = resources;
          this.#labels();
        });
      }
      if (first && this.isConnected) this.#start();
    }

    get hass() {
      return /** @type {Hass} */ (this.#hass);
    }

    connectedCallback() {
      document.addEventListener("visibilitychange", this.#onVisibility);
      if (this.#hass && !this.player) this.#start();
    }

    disconnectedCallback() {
      document.removeEventListener("visibilitychange", this.#onVisibility);
      this.#stopTalking();
      this.player?.stop();
      this.player = null;
    }

    /** Build the card and play its first stream. */
    #start() {
      const root = /** @type {ShadowRoot} */ (this.shadowRoot);
      root.innerHTML = `
        <style>${STYLE}</style>
        <ha-card>
          <div class="stage">
            <video playsinline muted></video>
            <span class="badge" hidden></span>
            <div class="overlay"></div>
            <div class="notice" role="status" hidden></div>
          </div>
          <div class="bar">
            <div class="streams" role="group"></div>
            <button class="talk" data-action="talk" aria-pressed="false" hidden>${ICONS.microphone}<span></span></button>
            <span class="spacer"></span>
            <button class="icon-button" data-action="sound">${ICONS.soundOff}</button>
            <button class="icon-button" data-action="fullscreen">${ICONS.fullscreen}</button>
          </div>
        </ha-card>`;
      this.video = /** @type {HTMLVideoElement} */ (root.querySelector("video"));
      this.video.addEventListener("volumechange", () => this.#labels());
      root.querySelector(".bar")?.addEventListener("click", (event) => this.#click(event));
      this.player = new Player(this);
      this.#render();
      this.#play();
    }

    /** Draw the buttons of the streams. */
    #render() {
      const root = this.shadowRoot;
      const group = root?.querySelector(".streams");
      if (!group || !this.#hass) return;
      const streams = streamsOf(this.#config);
      group.toggleAttribute("hidden", streams.length < 2);
      group.replaceChildren(
        ...streams.map((stream, index) => {
          const button = document.createElement("button");
          button.dataset.stream = String(index);
          // Names come from the card configuration: never through innerHTML
          button.textContent = streamName(/** @type {Hass} */ (this.#hass), this.#resources, stream);
          button.setAttribute("aria-pressed", String(index === this.#current));
          return button;
        }),
      );
      this.#labels();
    }

    /** Play the stream shown, once the cameras of the integration are known. */
    async #play() {
      const hass = this.#hass;
      const stream = streamsOf(this.#config)[this.#current];
      if (!hass || !stream || !this.player) return;
      if (!this.#cameras?.length) {
        try {
          // Cameras hidden from the panel still show on dashboards
          ({ cameras: this.#cameras } = await hass.callWS({ type: `${DOMAIN}/cameras`, include_hidden: true }));
        } catch (_err) {
          this.#cameras = []; // the integration is not loaded (yet)
        }
      }
      const found = cameraOf(this.#cameras ?? [], stream.entity);
      if (!found) {
        await this.player?.stop();
        this.showMessage(`${stream.entity}: ${translator(this.#resources)("live_unavailable")}`);
        return;
      }
      this.#error = null;
      // Main and sub stream are one device: the talk goes on; another device ends it
      if (this.#microphone && this.#microphone.entryId !== found.camera.entry_id) this.#stopTalking();
      this.#camera = found.camera;
      this.#labels();
      await this.player?.live(found.camera, found.level);
    }

    /** @param {Event} event */
    #click(event) {
      const target = /** @type {HTMLElement} */ (event.target).closest("button");
      if (!target || !this.video) return;
      if (target.dataset.stream !== undefined) {
        const index = Number(target.dataset.stream);
        if (index === this.#current) return;
        this.#current = index;
        this.#render();
        this.#play();
      } else if (target.dataset.action === "sound" && this.hasAudio !== false) {
        this.sound = this.video.muted;
        this.video.muted = !this.sound;
        if (this.video.paused) this.player?.resume();
      } else if (target.dataset.action === "talk") {
        if (this.#microphone) this.#stopTalking();
        else this.#startTalking();
      } else if (target.dataset.action === "fullscreen") {
        const stage = /** @type {HTMLElement} */ (this.shadowRoot?.querySelector(".stage"));
        const video = /** @type {any} */ (this.video);
        if (document.fullscreenElement) document.exitFullscreen();
        else if (stage.requestFullscreen) stage.requestFullscreen();
        else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
      }
    }

    /** Stop the stream while the page is hidden, and play it again when it shows. */
    #visibility() {
      if (!this.player) return;
      if (document.hidden) {
        this.#stopTalking();
        this.player.stop();
      } else this.#play();
    }

    /** Open the camera's device for the microphone; it ends when stopped or by the device. */
    async #startTalking() {
      const camera = this.#camera;
      if (!camera?.talk || !this.#hass || this.#starting) return;
      const microphone = new Microphone(this.#hass, camera.entry_id, (reason) => {
        if (this.#microphone !== microphone) return;
        this.#microphone = null;
        this.#labels();
        const notice = TALK_ENDS[/** @type {keyof TALK_ENDS} */ (reason)];
        if (notice) this.#notice(notice);
      });
      this.#microphone = microphone;
      this.#starting = true;
      this.#labels();
      try {
        await microphone.start();
      } catch (err) {
        if (this.#microphone === microphone) this.#microphone = null;
        this.#notice(/** @type {Error} */ (err).message);
      } finally {
        this.#starting = false;
        this.#labels();
      }
    }

    #stopTalking() {
      const microphone = this.#microphone;
      this.#microphone = null;
      microphone?.stop();
      this.#labels();
    }

    /** @param {string} key A text of the card, shown for a few seconds over the picture. */
    #notice(key) {
      const notice = /** @type {HTMLElement | null} */ (this.shadowRoot?.querySelector(".notice"));
      if (!notice) return;
      notice.textContent = translator(this.#resources)(key);
      notice.hidden = false;
      clearTimeout(this.#noticeTimer);
      this.#noticeTimer = setTimeout(() => (notice.hidden = true), NOTICE_SECONDS * 1000);
    }

    /** Label the badge and the buttons in the language of the user. */
    #labels() {
      const root = this.shadowRoot;
      const text = translator(this.#resources);
      const badge = root?.querySelector(".badge");
      if (badge) badge.textContent = text("live");
      const fullscreen = root?.querySelector('[data-action="fullscreen"]');
      fullscreen?.setAttribute("title", text("fullscreen"));
      fullscreen?.setAttribute("aria-label", text("fullscreen"));
      const talk = /** @type {HTMLButtonElement | null} */ (root?.querySelector('[data-action="talk"]'));
      if (talk) {
        const talking = Boolean(this.#microphone);
        const problem = unavailable(/** @type {any} */ (globalThis));
        talk.hidden = !this.#camera?.talk;
        talk.disabled = Boolean(problem);
        talk.classList.toggle("starting", this.#starting);
        talk.setAttribute("aria-pressed", String(talking && !this.#starting));
        const label = text(talking ? "talk_stop" : "talk_start");
        talk.title = problem ? text(problem) : label;
        talk.setAttribute("aria-label", talk.title);
        const caption = talk.querySelector("span");
        if (caption) caption.textContent = label;
      }
      const sound = /** @type {HTMLButtonElement | null} */ (root?.querySelector('[data-action="sound"]'));
      if (!sound || !this.video) return;
      const muted = this.video.muted;
      const silent = this.hasAudio === false;
      sound.innerHTML = muted || silent ? ICONS.soundOff : ICONS.soundOn;
      sound.disabled = silent;
      const label = text(silent ? "no_sound" : muted ? "sound_on" : "sound_off");
      sound.title = label;
      sound.setAttribute("aria-label", label);
      sound.setAttribute("aria-pressed", String(!muted && !silent));
      const named = root?.querySelectorAll(".streams button");
      if (this.#hass && named) {
        const streams = streamsOf(this.#config);
        named.forEach((button, index) => {
          if (streams[index])
            button.textContent = streamName(/** @type {Hass} */ (this.#hass), this.#resources, streams[index]);
        });
      }
    }

    #overlay() {
      const overlay = this.shadowRoot?.querySelector(".overlay");
      if (!overlay) return;
      if (this.#error) {
        const box = document.createElement("div");
        box.className = "message error";
        box.textContent = this.#error;
        overlay.replaceChildren(box);
      } else if (this.#busy) overlay.innerHTML = `<div class="spinner"></div>`;
      else overlay.replaceChildren();
    }

    // ---- what the player calls

    /** @param {boolean | null} value Whether the stream has a sound track. */
    setAudio(value) {
      this.hasAudio = value;
      this.#labels();
    }

    /** @param {boolean} on */
    busy(on) {
      this.#busy = on;
      this.#overlay();
    }

    clearError() {
      if (this.#error === null) return;
      this.#error = null;
      this.#overlay();
    }

    /** @param {string} code */
    showError(code) {
      this.#busy = false;
      this.#error = translator(this.#resources)(code);
      this.#overlay();
    }

    /** @param {string} text */
    showMessage(text) {
      this.#busy = false;
      this.#error = text;
      this.#overlay();
    }

    update() {
      const badge = this.shadowRoot?.querySelector(".badge");
      badge?.toggleAttribute("hidden", !this.player?.level);
      this.#labels();
    }

    onEnded() {
      /* live never ends */
    }
  }

  customElements.get(CARD) || customElements.define(CARD, HikvisionBridgeLiveCard);
  customElements.get(`${CARD}-editor`) || customElements.define(`${CARD}-editor`, HikvisionBridgeLiveCardEditor);

  // Listed in the card picker
  const pickable = /** @type {any} */ (window).customCards ?? [];
  /** @type {any} */ (window).customCards = pickable;
  if (!pickable.some((/** @type {{ type: string }} */ item) => item.type === CARD)) {
    pickable.push({
      type: CARD,
      name: "Hikvision live",
      description: "Live picture and sound of a Hikvision Bridge camera, with buttons to switch between its streams.",
      preview: false,
      documentationURL: "https://github.com/yannik2480/hikvision_bridge#live-card",
    });
  }
}
