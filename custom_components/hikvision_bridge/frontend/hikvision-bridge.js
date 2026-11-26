/*
 * Hikvision Bridge recordings: sidebar panel <hikvision-bridge-panel> and dashboard card
 * <hikvision-bridge-card>. Plain custom elements, no build step; colours come from the
 * Home Assistant theme.
 */

const DOMAIN = "hikvision_bridge";
const BASE = new URL(".", import.meta.url).pathname;
const HOUR = 3600e3;
const DAY = 24 * HOUR;
const ZOOMS = [DAY, 12 * HOUR, 6 * HOUR, 2 * HOUR, HOUR, 15 * 60e3];
const HEVC = 'video/mp4; codecs="hvc1.1.6.L153.B0"';
// Playback speeds of recordings, as the Hik-Connect app offers them.
const SPEEDS = [0.5, 1, 2, 4, 8];
// Digital zoom of the panel video: from the whole picture up to this magnification.
const VIDEO_ZOOM_MIN = 1;
const VIDEO_ZOOM_MAX = 8;
// Live WebRTC gives up after this long and the view falls back to HLS.
const RTC_START_MS = 6000;

// English texts; translations come from the integration (selector "ui").
const TEXT = {
  live: "Live",
  playback: "Playback",
  quality: "Quality",
  speed: "Speed",
  auto: "Auto",
  low: "Low (fast)",
  high: "High",
  original: "Original",
  compatible: "Compatible (H.264)",
  cameras: "Cameras",
  no_cameras: "No cameras found.",
  no_recordings: "No recordings on this day.",
  loading: "Loading recordings...",
  no_recording: "No recording from this time on.",
  playback_encrypted: "The recording is encrypted and the verification code does not fit. Enter it in the integration options.",
  playback_unreadable: "The video from the camera could not be read, often because of a weak connection (for example Wi-Fi). Try again or choose a lower quality. If the recordings are encrypted, check the verification code in the integration options.",
  live_encrypted: "The live stream is encrypted (Hik-Connect stream encryption) and cannot be played here.",
  playback_busy: "The camera is already playing a recording elsewhere. Try again in a moment.",
  playback_auth: "The device rejected the login.",
  playback_failed: "Playback failed.",
  no_ffmpeg: "ffmpeg is not available in Home Assistant.",
  live_unavailable: "No live stream for this camera.",
  hevc_hint: "This browser cannot play H.265 recordings; 'Compatible' converts them (slow on small machines).",
  today: "Today",
  previous_event: "Previous event",
  next_event: "Next event",
  back: "10 seconds back",
  forward: "10 seconds forward",
  play: "Play",
  pause: "Pause",
  download: "Download this recording",
  fullscreen: "Full screen",
  sound_on: "Sound on",
  sound_off: "Sound off",
  no_sound: "No sound from this stream.",
  zoom_in: "Zoom in",
  zoom_out: "Zoom out",
  previous_day: "Previous day",
  next_day: "Next day",
  unread: "Unread",
  mark_all_read: "Mark all read",
  source: "Source",
  recorder: "Recorder",
  card: "Memory card",
  select_camera: "Select a camera.",
  cameras_top: "Cameras above the video",
  cameras_left: "Cameras beside the video",
  pick_day: "Choose a day",
  previous_month: "Previous month",
  next_month: "Next month",
  recording_days: "Days with recordings",
  view_timeline: "Timeline",
  view_clips: "Clips",
  no_picture: "No picture",
};

const KIND_COLORS = {
  continuous: "#4f8fdb",
  motion: "#f5a524",
  alarm: "#e5484d",
  event: "#a855f7",
  manual: "#22b8cf",
  other: "#8b8d98",
};
const EVENT_COLORS = {
  motion: "#f5a524",
  intrusion: "#e5484d",
  line_crossing: "#ec4899",
  region_entrance: "#a855f7",
  region_exiting: "#8b5cf6",
  tamper: "#ef4444",
  video_loss: "#71717a",
  scene_change: "#14b8a6",
  pir: "#f97316",
  target_person: "#22c55e",
  target_vehicle: "#06b6d4",
};
// Symbols of the event kinds (24x24, drawn with the even-odd rule).
const SYMBOL_PATHS = {
  person: "M12 3a4 4 0 1 1 0 8 4 4 0 0 1 0-8m0 10c4.4 0 8 1.8 8 4v3H4v-3c0-2.2 3.6-4 8-4z",
  vehicle:
    "M6.5 6h11l2.5 6v7h-2.5v-2h-11v2H4v-7L6.5 6m1.2 2-1.4 4h11.4l-1.4-4H7.7M7.5 13.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3m9 0a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z",
  motion: "M15 7a5 5 0 1 1 0 10 5 5 0 0 1 0-10M2 8h7v2H2V8m-1 3h7v2H1v-2m1 3h7v2H2v-2z",
  area: "M3 3h18v18H3V3m3 3v12h12V6H6m3 3h6v6H9V9z",
  line: "M11 2h2v20h-2V2M3 11h5V8l4 4-4 4v-3H3v-2z",
  alert: "M10.5 3h3v11h-3V3m0 14h3v3h-3v-3z",
};
const EVENT_SYMBOLS = {
  target_person: "person",
  target_vehicle: "vehicle",
  motion: "motion",
  pir: "motion",
  intrusion: "area",
  region_entrance: "area",
  region_exiting: "area",
  line_crossing: "line",
};
// Drawn last (on top) and labelled first: what was seen beats that something moved.
const EVENT_RANK = { target_person: 3, target_vehicle: 3, motion: 0, pir: 0 };
const eventSymbol = (kind) => SYMBOL_PATHS[EVENT_SYMBOLS[kind] || "alert"];
const eventRank = (kind) => EVENT_RANK[kind] ?? 1;
// Recordings closer than this are drawn as one; the files of a recording follow each other.
const JOIN_GAP = 10e3;
// Event kinds the viewer switched off in the legend, kept in this browser.
const HIDDEN_EVENTS_KEY = "hikvision-bridge.hidden-events";
// Previous/next start an event's recording file from its beginning when the event comes at
// most this long after it (the pre-record time of event recordings); else at the event.
const EVENT_LEAD_MAX = 60e3;

function joinRecordings(recordings) {
  const joined = [];
  for (const rec of [...recordings].sort((a, b) => a.start - b.start)) {
    const last = joined[joined.length - 1];
    if (last && rec.start - last.end <= JOIN_GAP) {
      if (rec.kind === last.kind) {
        last.end = Math.max(last.end, rec.end);
        continue;
      }
      last.end = Math.max(last.end, rec.start);
    }
    joined.push({ start: rec.start, end: rec.end, kind: rec.kind });
  }
  return joined;
}

// Clips of the day for the clip list: the files of one recording of one kind make one clip,
// shown by the picture of its first file.
function clipsOf(recordings) {
  const clips = [];
  for (const rec of [...recordings].sort((a, b) => a.start - b.start)) {
    const last = clips[clips.length - 1];
    if (last && rec.kind === last.kind && rec.start - last.end <= JOIN_GAP) {
      last.end = Math.max(last.end, rec.end);
      continue;
    }
    clips.push({ start: rec.start, end: rec.end, kind: rec.kind, token: rec.token });
  }
  return clips;
}

// The view of the timeline area the user chose last: the timeline or the clip list.
const VIEW_KEY = `${DOMAIN}.view`;
function savedView() {
  try {
    return localStorage.getItem(VIEW_KEY) === "clips" ? "clips" : "timeline";
  } catch (_err) {
    return "timeline"; // storage blocked (private window): the default view
  }
}
function rememberView(view) {
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch (_err) {
    // not remembered
  }
}

// Whether hls.js has appended a sound track: Home Assistant's live HLS drops camera audio
// it cannot pass on (G.711, G.726), so many live streams have none.
function hasAudioTrack(data) {
  if (data.audio) return true;
  return Boolean(data.audiovideo && /mp4a|ac-3|ec-3|opus|mp3|flac/.test(data.audiovideo.codec || ""));
}

// Whether the device sends a sound format that Home Assistant's live HLS drops (anything but
// AAC or MP3), so the picture needs the integration's converted session from the start.
function needsSoundConversion(camera, level) {
  const codec = camera.audio_codec && camera.audio_codec[level === "low" ? 2 : 1];
  return Boolean(codec) && !/aac|mp3|mp4a/i.test(codec);
}

let hlsLoader;
function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsLoader) {
    hlsLoader = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `${BASE}hls.light.min.js`;
      script.onload = () => resolve(window.Hls);
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  return hlsLoader;
}

const translations = new Map();
async function loadTexts(hass) {
  const language = hass.language || "en";
  if (!translations.has(language)) {
    translations.set(
      language,
      Promise.all(
        ["selector", "entity"].map((category) =>
          hass
            .callWS({ type: "frontend/get_translations", language, category, integration: [DOMAIN] })
            .then((r) => r.resources)
            .catch(() => ({})),
        ),
      ).then(([selector, entity]) => ({ ...selector, ...entity })),
    );
  }
  return translations.get(language);
}

function makeTranslator(resources) {
  const prefix = `component.${DOMAIN}.`;
  return {
    ui: (key) => resources[`${prefix}selector.ui.options.${key}`] || TEXT[key] || key,
    kind: (key) => resources[`${prefix}selector.recording_kind.options.${key}`] || key,
    event: (key) => resources[`${prefix}entity.binary_sensor.${key}.name`] || key.replace(/_/g, " "),
    source: (key) => resources[`${prefix}selector.recording_source.options.${key}`] || TEXT[key] || key,
  };
}

const pad = (n) => String(n).padStart(2, "0");
const clock = (date, seconds = true) =>
  `${pad(date.getHours())}:${pad(date.getMinutes())}${seconds ? `:${pad(date.getSeconds())}` : ""}`;
// Length or position in a recording file: 0:07, 12:34 or 1:02:03.
const duration = (ms) => {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return h ? `${h}:${pad(m)}:${pad(total % 60)}` : `${m}:${pad(total % 60)}`;
};
const startOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
const isoDay = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

function icon(path) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`;
}
const ICONS = {
  play: icon("M8 5.14v14l11-7-11-7z"),
  pause: icon("M14 19h4V5h-4M6 19h4V5H6v14z"),
  back: icon("M11.5 3a9 9 0 1 1-9 9h2a7 7 0 1 0 7-7v3l-4-4 4-4v3z"),
  forward: icon("M12.5 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-7-7v3l4-4-4-4v3z"),
  prev: icon("M6 6h2v12H6zm3.5 6 8.5 6V6z"),
  next: icon("M16 18h2V6h-2zM6 18l8.5-6L6 6z"),
  download: icon("M5 20h14v-2H5m14-9h-4V3H9v6H5l7 7 7-7z"),
  fullscreen: icon("M5 5h5v2H7v3H5V5m9 0h5v5h-2V7h-3V5m3 9h2v5h-5v-2h3v-3m-7 3v2H5v-5h2v3h3z"),
  soundOn: icon("M14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.84-5 6.7v2.07c4-.91 7-4.49 7-8.77S18 4.14 14 3.23M16.5 12c0-1.77-1-3.29-2.5-4.03V16c1.5-.71 2.5-2.24 2.5-4M3 9v6h4l5 5V4L7 9H3z"),
  soundOff: icon("M12 4 9.91 6.09 12 8.18M4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.26c-.67.51-1.42.93-2.25 1.2v2.07c1.38-.32 2.63-.95 3.68-1.81L19.73 21 21 19.73l-9-9M19 12c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.9 8.9 0 0 0 21 12c0-4.28-3-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71m-2.5 0c0-1.77-1-3.29-2.5-4.03v2.21l2.45 2.45c.05-.2.05-.42.05-.63z"),
  menu: icon("M3 6h18v2H3V6m0 5h18v2H3v-2m0 5h18v2H3v-2z"),
  left: icon("M15.41 16.58 10.83 12l4.58-4.59L14 6l-6 6 6 6 1.41-1.42z"),
  right: icon("M8.59 16.58 13.17 12 8.59 7.41 10 6l6 6-6 6-1.41-1.42z"),
  plus: icon("M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"),
  minus: icon("M19 13H5v-2h14v2z"),
  live: icon("M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8m0-6a10 10 0 1 1 0 20 10 10 0 0 1 0-20m0 2a8 8 0 1 0 0 16 8 8 0 0 0 0-16z"),
  camsLeft: icon("M3 3h18v18H3V3m2 2v14h5V5H5m7 0v14h7V5h-7z"),
  calendar: icon("M19 4h-1V2h-2v2H8V2H6v2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2m0 16H5V9h14v11M7 11h5v5H7v-5z"),
  caret: icon("M7 10l5 5 5-5H7z"),
  camsTop: icon("M3 3h18v18H3V3m2 2v5h14V5H5m0 7v7h14v-7H5z"),
};
const symbolIcon = (kind) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill-rule="evenodd" d="${eventSymbol(kind)}"/></svg>`;

const STYLE = `
  :host {
    --hb-bg: var(--primary-background-color, #111);
    --hb-surface: var(--card-background-color, var(--ha-card-background, #1c1c1c));
    --hb-text: var(--primary-text-color, #e1e1e1);
    --hb-muted: var(--secondary-text-color, #9b9b9b);
    --hb-line: var(--divider-color, rgba(127,127,127,.25));
    --hb-accent: var(--primary-color, #03a9f4);
    --hb-error: var(--error-color, #db4437);
    color: var(--hb-text);
    font-family: var(--paper-font-body1_-_font-family, var(--ha-font-family-body, Roboto, sans-serif));
  }
  button { font: inherit; color: inherit; }
  .icon-button {
    background: none; border: 0; border-radius: 50%; width: 40px; height: 40px; padding: 8px;
    cursor: pointer; display: inline-flex; align-items: center; justify-content: center; flex: none;
  }
  .icon-button:hover { background: color-mix(in srgb, var(--hb-text) 10%, transparent); }
  .icon-button:disabled { opacity: .35; cursor: default; background: none; }
  .icon-button svg { width: 24px; height: 24px; fill: currentColor; }
  select {
    font: inherit; color: var(--hb-text); background: var(--hb-surface);
    border: 1px solid var(--hb-line); border-radius: 8px; padding: 6px 8px; min-height: 36px;
  }
  .segmented { display: inline-flex; border: 1px solid var(--hb-line); border-radius: 18px; overflow: hidden; }
  .segmented button { background: none; border: 0; padding: 6px 14px; cursor: pointer; min-height: 34px; }
  .segmented button.active { background: var(--hb-accent); color: var(--text-primary-color, #fff); }
  .stage { position: relative; background: #000; display: flex; align-items: center; justify-content: center; min-height: 0; overflow: hidden; }
  .stage video { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
  .stage.armed { touch-action: none; }
  .stage.zoomable video { transform-origin: 0 0; }
  .stage.armed.zoomed { cursor: grab; }
  .overlay { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; pointer-events: none; }
  .message { background: rgba(0,0,0,.7); color: #fff; padding: 10px 16px; border-radius: 8px; max-width: 80%; text-align: center; }
  .message.error { border-left: 4px solid var(--hb-error); }
  .spinner { width: 42px; height: 42px; border-radius: 50%; border: 4px solid rgba(255,255,255,.25); border-top-color: #fff; animation: spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .badges { position: absolute; top: 10px; left: 10px; display: flex; gap: 6px; pointer-events: none; }
  .badge { background: rgba(0,0,0,.6); color: #fff; font-size: 12px; padding: 3px 8px; border-radius: 10px; letter-spacing: .02em; }
  .badge.live { background: #e5484d; }
  .viewer { container-type: inline-size; }
  .controls { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 4px; padding: 4px 8px; }
  .controls .group { display: flex; align-items: center; gap: 2px; min-width: 0; }
  .controls .start { justify-self: start; }
  .controls .middle { justify-self: center; }
  .controls .end { justify-self: end; justify-content: flex-end; flex-wrap: wrap; }
  .controls .middle [data-action="toggle"] { width: 48px; height: 48px; }
  .controls .middle [data-action="toggle"] svg { width: 30px; height: 30px; }
  .controls select { min-width: 0; max-width: 100%; }
  @container (max-width: 640px) {
    .controls { grid-template-columns: auto 1fr; grid-template-areas: "middle middle" "start end"; padding: 2px 4px; }
    .controls .middle { grid-area: middle; }
    .controls .start { grid-area: start; }
    .controls .end { grid-area: end; }
    .controls select { font-size: 13px; padding: 4px; min-height: 32px; }
    .controls .icon-button { width: 36px; height: 36px; }
    .controls .end { row-gap: 4px; }
    .day-bar .icon-button { width: 36px; height: 36px; }
    .legend { gap: 6px; padding: 0 8px 4px; }
  }
  .controls .live-button { display: inline-flex; gap: 6px; align-items: center; border: 1px solid var(--hb-line); background: none; border-radius: 18px; padding: 4px 12px 4px 8px; cursor: pointer; min-height: 34px; }
  .controls .live-button svg { width: 18px; height: 18px; fill: #e5484d; }
  .controls .live-button.active { background: #e5484d; color: #fff; border-color: #e5484d; }
  .controls .live-button.active svg { fill: #fff; }
  .date-button {
    display: inline-flex; align-items: center; gap: 6px; min-height: 36px; padding: 4px 6px 4px 10px;
    border: 1px solid var(--hb-line); border-radius: 18px; background: none; cursor: pointer;
    white-space: nowrap; font-variant-numeric: tabular-nums; min-width: 0;
  }
  .date-button:hover { background: color-mix(in srgb, var(--hb-text) 8%, transparent); }
  .date-button svg { width: 18px; height: 18px; fill: currentColor; flex: none; }
  .date-button svg:last-child { opacity: .6; }
  .date-button .short { display: none; }
  @container (max-width: 480px) {
    .date-button .long { display: none; }
    .date-button .short { display: inline; }
  }
  /* Phone widths: the day bar takes two rows, the date and arrows above, the view and zoom below. */
  @container (max-width: 560px) {
    .day-bar { flex-wrap: wrap; row-gap: 4px; }
    .day-bar .spacer { display: none; }
    .day-bar .date-picker { flex: none; }
    .day-bar .view-switch { margin-left: auto; }
  }
  .calendar {
    position: fixed; inset: auto; margin: 0; width: min(296px, calc(100vw - 16px)); box-sizing: border-box;
    padding: 10px 10px 8px; border: 1px solid var(--hb-line); border-radius: 16px;
    background: var(--card-background-color, var(--hb-surface)); color: var(--hb-text);
    box-shadow: 0 16px 40px rgba(0,0,0,.35), 0 2px 8px rgba(0,0,0,.2); font: inherit; overflow: visible;
  }
  .calendar.fallback { display: none; position: absolute; bottom: calc(100% + 6px); left: 0; z-index: 10; }
  .calendar.fallback.open { display: block; }
  .date-picker { position: relative; min-width: 0; }
  .cal-head { display: flex; align-items: center; justify-content: space-between; gap: 4px; margin-bottom: 4px; }
  .cal-head .icon-button { width: 34px; height: 34px; padding: 5px; }
  .cal-title { font-weight: 500; font-size: 15px; display: inline-flex; align-items: center; gap: 8px; }
  .cal-title::after {
    content: ""; width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--hb-line);
    border-top-color: var(--hb-accent); animation: spin .8s linear infinite; visibility: hidden;
  }
  .calendar.loading .cal-title::after { visibility: visible; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 2px; }
  .cal-wd { text-align: center; font-size: 11px; color: var(--hb-muted); padding: 4px 0 6px; text-transform: uppercase; letter-spacing: .04em; }
  .cal-day {
    position: relative; aspect-ratio: 1; min-width: 0; border: 0; border-radius: 50%; background: none; padding: 0;
    cursor: pointer; font-size: 14px; font-variant-numeric: tabular-nums;
    display: flex; align-items: center; justify-content: center; transition: background .12s;
  }
  .cal-day:hover:not(:disabled) { background: color-mix(in srgb, var(--hb-text) 10%, transparent); }
  .cal-day:disabled { opacity: .28; cursor: default; }
  .cal-day.norec:not(.selected):not(:disabled) { color: var(--hb-muted); }
  .cal-day.rec { font-weight: 500; }
  .cal-day.rec::after {
    content: ""; position: absolute; bottom: 4px; left: 50%; width: 5px; height: 5px; margin-left: -2.5px;
    border-radius: 50%; background: var(--hb-accent);
  }
  .cal-day.today { box-shadow: inset 0 0 0 1.5px var(--hb-accent); }
  .cal-day.selected { background: var(--hb-accent); color: var(--text-primary-color, #fff); font-weight: 600; }
  .cal-day.selected:hover { background: var(--hb-accent); }
  .cal-day.selected.rec::after { background: currentColor; }
  .cal-foot {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 6px; padding-top: 8px;
    border-top: 1px solid var(--hb-line); font-size: 12px; color: var(--hb-muted);
  }
  .cal-foot .key { display: inline-flex; align-items: center; gap: 6px; }
  .cal-foot .key::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--hb-accent); }
  .cal-foot .key[hidden] { visibility: hidden; display: inline-flex; }
  .cal-foot button { background: none; border: 0; color: var(--hb-accent); font-weight: 500; cursor: pointer; padding: 4px 8px; border-radius: 12px; }
  .cal-foot button:hover { background: color-mix(in srgb, var(--hb-accent) 12%, transparent); }
  .timeline { position: relative; user-select: none; touch-action: none; }
  .timeline canvas { width: 100%; height: 100%; display: block; cursor: pointer; }
  .tooltip { position: absolute; top: 2px; transform: translateX(-50%); background: var(--hb-text); color: var(--hb-surface); font-size: 12px; padding: 2px 6px; border-radius: 4px; pointer-events: none; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .legend { display: flex; flex-wrap: wrap; gap: 10px; font-size: 12px; color: var(--hb-muted); padding: 0 12px 6px; }
  .legend span { display: inline-flex; align-items: center; gap: 4px; }
  .legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 2px; background: var(--c); }
  .legend span.event::before { display: none; }
  .legend span.event svg, .legend button.event svg { width: 16px; height: 16px; padding: 2px; border-radius: 50%; background: var(--c); fill: #fff; box-sizing: border-box; }
  .legend button.event { display: inline-flex; align-items: center; gap: 4px; font: inherit; color: inherit; background: none; border: 0; padding: 2px 4px; margin: -2px -4px; border-radius: 10px; cursor: pointer; }
  .legend button.event:hover { background: color-mix(in srgb, var(--hb-text) 8%, transparent); }
  .legend button.event.off { opacity: .45; text-decoration: line-through; }
  .legend button.event.off svg { background: var(--hb-muted); }
  .clip { display: flex; align-items: center; gap: 8px; padding: 4px 12px 0; font-size: 12px; color: var(--hb-muted); font-variant-numeric: tabular-nums; }
  .clip[hidden] { display: none; }
  .clip input { flex: 1; min-width: 0; margin: 0; accent-color: var(--hb-accent); }
  .hint { font-size: 12px; color: var(--hb-muted); padding: 0 12px 4px; }
  .view-switch { flex: none; }
  .view-switch button { font-size: 13px; }
  .clips { height: 200px; overflow-y: auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; align-content: start; padding: 4px 12px 8px; background: var(--hb-surface); }
  .clips[hidden] { display: none; }
  .zoom[hidden] { display: none; }
  .clip-card { display: flex; flex-direction: column; gap: 4px; min-width: 0; padding: 0; border: 0; background: none; cursor: pointer; text-align: left; color: inherit; }
  .clip-card .thumb { position: relative; aspect-ratio: 16 / 9; overflow: hidden; background: #000; border-radius: 6px; box-sizing: border-box; }
  .clip-card:hover .thumb { outline: 2px solid color-mix(in srgb, var(--hb-accent) 50%, transparent); }
  .clip-card.active .thumb { outline: 2px solid var(--hb-accent); }
  .clip-card img { width: 100%; height: 100%; object-fit: cover; display: block; opacity: 0; transition: opacity .2s; }
  .clip-card img.loaded { opacity: 1; }
  .clip-card .none { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; font-size: 12px; color: var(--hb-muted); }
  .clip-card.failed .none { display: flex; }
  .clip-card .clip-time { font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .clip-card .clip-kind { font-size: 12px; color: var(--hb-muted); }
  .clip-card .clip-kind::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 5px; border-radius: 50%; background: var(--c, var(--hb-line)); }
`;

/* ------------------------------------------------------------------ timeline */

class Timeline {
  constructor(container, onSeek) {
    this.container = container;
    this.onSeek = onSeek;
    this.canvas = document.createElement("canvas");
    this.tip = document.createElement("div");
    this.tip.className = "tooltip";
    this.tip.hidden = true;
    container.append(this.canvas, this.tip);
    this.viewStart = startOfDay(new Date()).getTime();
    this.span = DAY;
    this.recordings = [];
    this.events = [];
    // While the day's recordings are searched, the band says so instead of looking empty.
    this.loading = false;
    this.loadingText = "Loading recordings...";
    this.playhead = null;
    // While zoomed in, the view follows the playhead until the user pans or zooms by hand.
    this.followPlayhead = true;
    this.colors = {};
    this.kindName = (k) => k;
    this.eventName = (k) => k;
    this._bind();
    this._resize = new ResizeObserver(() => this.draw());
    this._resize.observe(container);
  }

  destroy() {
    this._resize.disconnect();
  }

  setDay(dayStart) {
    this.dayStart = dayStart.getTime();
    this.span = Math.min(this.span, DAY);
    this.followPlayhead = true;
    this.viewStart = this.dayStart;
    if (this.span < DAY) this.center(this.playhead ?? Date.now());
    this.draw();
  }

  center(time) {
    const lo = this.dayStart;
    const hi = this.dayStart + DAY - this.span;
    this.viewStart = Math.max(lo, Math.min(hi, time - this.span / 2));
  }

  zoom(direction, at) {
    const index = ZOOMS.indexOf(this.span);
    const next = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, (index < 0 ? 0 : index) + direction))];
    if (next === this.span) return;
    // The buttons zoom around the playhead and follow it again; the wheel passes a pointer position.
    if (at === undefined) this.followPlayhead = true;
    const anchor = at ?? this.playhead ?? this.viewStart + this.span / 2;
    const ratio = (anchor - this.viewStart) / this.span;
    this.span = next;
    this.viewStart = anchor - ratio * next;
    this.center(this.viewStart + next / 2);
    this.draw();
  }

  timeAt(x) {
    const width = this.canvas.clientWidth || 1;
    return this.viewStart + (x / width) * this.span;
  }

  _bind() {
    let drag = null;
    const point = (e) => e.clientX - this.canvas.getBoundingClientRect().left;
    this.canvas.addEventListener("pointerdown", (e) => {
      this.canvas.setPointerCapture(e.pointerId);
      drag = { x: point(e), start: this.viewStart, moved: false };
    });
    this.canvas.addEventListener("pointermove", (e) => {
      const x = point(e);
      if (drag) {
        const dx = x - drag.x;
        if (Math.abs(dx) > 4) drag.moved = true;
        if (drag.moved && this.span < DAY) {
          this.followPlayhead = false;
          this.viewStart = drag.start - (dx / this.canvas.clientWidth) * this.span;
          this.center(this.viewStart + this.span / 2);
          this.draw();
        }
      }
      const time = this.timeAt(x);
      const slack = (6 / (this.canvas.clientWidth || 1)) * this.span;
      const seen = this.events
        .filter((ev) => ev.start - slack <= time && ev.end + slack >= time)
        .sort((a, b) => eventRank(b.kind) - eventRank(a.kind))[0];
      this.tip.hidden = false;
      this.tip.style.left = `${Math.max(60, Math.min(this.canvas.clientWidth - 60, x))}px`;
      this.tip.textContent = clock(new Date(time), this.span <= 2 * HOUR) + (seen ? ` · ${this.eventName(seen.kind)}` : "");
    });
    this.canvas.addEventListener("pointerup", (e) => {
      if (drag && !drag.moved) this.onSeek(new Date(this.timeAt(point(e))));
      drag = null;
    });
    this.canvas.addEventListener("pointerleave", () => {
      this.tip.hidden = true;
    });
    this.canvas.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        this.followPlayhead = false;
        if (Math.abs(e.deltaX) > Math.abs(e.deltaY) && this.span < DAY) {
          this.viewStart += (e.deltaX / this.canvas.clientWidth) * this.span;
          this.center(this.viewStart + this.span / 2);
          this.draw();
        } else {
          this.zoom(e.deltaY < 0 ? 1 : -1, this.timeAt(point(e)));
        }
      },
      { passive: false },
    );
  }

  draw() {
    const canvas = this.canvas;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const style = getComputedStyle(this.container);
    const text = style.getPropertyValue("--hb-text").trim() || "#ddd";
    const muted = style.getPropertyValue("--hb-muted").trim() || "#999";
    const line = style.getPropertyValue("--hb-line").trim() || "rgba(127,127,127,.3)";
    const accent = style.getPropertyValue("--hb-accent").trim() || "#03a9f4";
    const x = (t) => ((t - this.viewStart) / this.span) * width;
    const axisY = 22;
    const recY = 30;
    const recH = Math.max(18, height - recY - 8);

    // Hour ticks and labels.
    const steps = [5 * 60e3, 15 * 60e3, 30 * 60e3, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR];
    const step = steps.find((s) => (s / this.span) * width >= 70) || 6 * HOUR;
    const tzOffset = new Date(this.viewStart).getTimezoneOffset() * 60e3;
    let tick = Math.ceil((this.viewStart - tzOffset) / step) * step + tzOffset;
    ctx.font = "11px sans-serif";
    for (; tick <= this.viewStart + this.span; tick += step) {
      const px = Math.round(x(tick)) + 0.5;
      ctx.strokeStyle = line;
      ctx.beginPath();
      ctx.moveTo(px, axisY - 6);
      ctx.lineTo(px, height);
      ctx.stroke();
      ctx.fillStyle = muted;
      // Labels at the edges stay inside the canvas.
      ctx.textAlign = px < 16 ? "left" : px > width - 16 ? "right" : "center";
      ctx.fillText(clock(new Date(tick), false), px, 12);
    }

    // Recordings, on whole pixels so that joined parts show no seams.
    ctx.fillStyle = line;
    ctx.fillRect(0, recY, width, recH);
    for (const rec of joinRecordings(this.recordings)) {
      const a = Math.max(0, Math.floor(x(rec.start)));
      const b = Math.min(width, Math.ceil(x(rec.end)));
      if (b < 0 || a > width) continue;
      ctx.fillStyle = KIND_COLORS[rec.kind] || KIND_COLORS.other;
      ctx.fillRect(a, recY, Math.max(2, b - a), recH);
    }

    if (this.loading) {
      ctx.fillStyle = muted;
      ctx.font = "13px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(this.loadingText, width / 2, recY + recH / 2 + 4);
    }

    // Detections colour their part of the recordings, people and vehicles on top.
    const events = this.events
      .filter((ev) => x(ev.end) >= 0 && x(ev.start) <= width)
      .sort((a, b) => eventRank(a.kind) - eventRank(b.kind));
    for (const ev of events) {
      const a = Math.floor(x(ev.start));
      const b = Math.ceil(x(ev.end));
      ctx.fillStyle = EVENT_COLORS[ev.kind] || accent;
      ctx.fillRect(a, recY, Math.max(3, b - a), recH);
    }
    // Their symbol on that colour where there is room, the most specific kinds first; a
    // part too narrow for it carries a round badge of its colour instead.
    const size = Math.max(12, Math.min(18, Math.floor(recH * 0.6)));
    const cy = recY + recH / 2;
    const placed = [];
    for (const ev of [...events].reverse()) {
      const a = x(ev.start);
      const b = x(ev.end);
      const cx = Math.max(size / 2 + 1, Math.min(width - size / 2 - 1, (a + b) / 2));
      if (placed.some((p) => Math.abs(p - cx) < size + 2)) continue;
      placed.push(cx);
      if (b - a < size + 4) {
        const radius = size / 2 + 2;
        ctx.fillStyle = EVENT_COLORS[ev.kind] || accent;
        ctx.beginPath();
        ctx.arc(cx, cy, radius, 0, Math.PI * 2);
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = "rgba(0,0,0,.35)";
        ctx.stroke();
        ctx.lineWidth = 1;
      }
      const glyph = size * 0.8;
      ctx.save();
      ctx.translate(cx - glyph / 2, cy - glyph / 2);
      ctx.scale(glyph / 24, glyph / 24);
      ctx.fillStyle = "#fff";
      ctx.shadowColor = "rgba(0,0,0,.45)";
      ctx.shadowBlur = 2;
      ctx.fill(new Path2D(eventSymbol(ev.kind)), "evenodd");
      ctx.restore();
    }

    // An event not read yet carries a dot in the corner of its part.
    for (const ev of events) {
      if (ev.read) continue;
      const cx = Math.max(4, Math.min(width - 4, x(ev.start) + 4));
      ctx.beginPath();
      ctx.arc(cx, recY + 4, 4, 0, Math.PI * 2);
      ctx.fillStyle = accent;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#fff";
      ctx.stroke();
      ctx.lineWidth = 1;
    }

    // Now and playhead.
    const now = Date.now();
    if (now > this.viewStart && now < this.viewStart + this.span) {
      ctx.strokeStyle = muted;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x(now), axisY - 6);
      ctx.lineTo(x(now), height);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (this.playhead != null) {
      const px = x(this.playhead);
      if (px >= 0 && px <= width) {
        ctx.strokeStyle = "#e5484d";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(px, axisY - 4);
        ctx.lineTo(px, height);
        ctx.stroke();
        ctx.lineWidth = 1;
        ctx.fillStyle = "#e5484d";
        ctx.beginPath();
        ctx.moveTo(px - 5, axisY - 8);
        ctx.lineTo(px + 5, axisY - 8);
        ctx.lineTo(px, axisY - 2);
        ctx.fill();
      }
    }
    ctx.fillStyle = text;
  }
}

/* -------------------------------------------------------------------- player */

/*
 * One camera's video: live (Home Assistant's camera stream) or recordings (a playback
 * session of the integration). Quality "auto" starts low and goes up while playback is
 * smooth and the connection fast; stalls bring it down again.
 */
class Player {
  constructor(host) {
    this.host = host;
    this.video = host.video;
    this.hls = null;
    this.session = null;
    this.liveSound = null; // session id of the live picture with its own sound, if any
    this.mode = "live";
    this.quality = "auto";
    this.speed = 1; // playback speed of recordings; live always runs at 1x
    this.level = null; // quality actually playing: low/high (live), original/compatible (recordings)
    this.stalls = [];
    this.token = 0;
    this.video.addEventListener("waiting", () => this._stalled());
    this.video.addEventListener("playing", () => this._smooth());
    this.starting = null; // token of the playback start still running
    this.video.addEventListener("ended", () => this.host.onEnded());
    this._upgradeTimer = null;
    this._keepAlive = null;
  }

  get canHevc() {
    const media = window.MediaSource || window.ManagedMediaSource;
    return Boolean(
      (media && media.isTypeSupported && media.isTypeSupported(HEVC)) ||
        this.video.canPlayType('video/mp4; codecs="hvc1"'),
    );
  }

  async _attach(url, token) {
    this._detach();
    const video = this.video;
    const nativeHls = video.canPlayType("application/vnd.apple.mpegurl");
    if (nativeHls && !window.MediaSource) {
      video.src = url;
    } else {
      const Hls = await loadHls();
      if (token !== this.token) return;
      if (!Hls.isSupported()) {
        video.src = url;
      } else {
        this.hls = new Hls({
          lowLatencyMode: this.mode === "live",
          // Load the first segment while the video element is still being set up.
          startFragPrefetch: true,
          backBufferLength: 120,
          manifestLoadingMaxRetry: 6,
          levelLoadingMaxRetry: 6,
          fragLoadingMaxRetry: 6,
        });
        this.hls.on(Hls.Events.BUFFER_CODECS, (_e, data) => {
          if (token !== this.token) return;
          const sound = hasAudioTrack(data);
          this.host.setAudio(sound);
          if (!sound && this.mode === "live") this._liveSound(token);
        });
        this.hls.on(Hls.Events.ERROR, (_e, data) => {
          // A newer start ends this session on the server; the video stays until it is ready.
          if (token !== this.token) return;
          if (data.fatal) {
            if (data.type === Hls.ErrorTypes.MEDIA_ERROR) this.hls.recoverMediaError();
            else this.host.showError("playback_failed");
          }
        });
        this.hls.loadSource(url);
        this.hls.attachMedia(video);
      }
    }
    video.muted = !this.host.sound;
    await this._autoplay();
  }

  // Browsers only start a video with sound after a click on the page: when refused with
  // sound on, play muted instead, so the picture never waits for the sound setting.
  async _autoplay() {
    const video = this.video;
    try {
      await video.play();
    } catch (err) {
      if (video.muted || err?.name !== "NotAllowedError") return;
      this.host.sound = false;
      video.muted = true; // the sound button follows through its volumechange listener
      try {
        await video.play();
      } catch (_err) {
        /* autoplay may need a click */
      }
    }
  }

  // Play or continue after a click; the sound setting is applied before this.
  resume() {
    return this._autoplay();
  }

  _detach() {
    clearTimeout(this._upgradeTimer);
    this._closeRtc();
    if (this.hls) {
      this.hls.destroy();
      this.hls = null;
    }
    this.video.srcObject = null;
    this.video.removeAttribute("src");
    this.video.load();
    // The next source decides again whether it has sound.
    this.host.setAudio(null);
    // Loading resets the rate to the default one: set the speed again for the next source.
    this._applySpeed();
  }

  // Recordings play at the chosen speed (the browser keeps the pitch); live always at 1x.
  _applySpeed() {
    const rate = this.mode === "playback" ? this.speed : 1;
    this.video.defaultPlaybackRate = rate;
    this.video.playbackRate = rate;
  }

  setSpeed(speed) {
    this.speed = speed;
    this._applySpeed();
  }

  _closeRtc() {
    if (this.rtc) this.rtc.close();
    this.rtc = null;
  }

  async stop() {
    this.token += 1;
    this._detach();
    clearInterval(this._keepAlive);
    const session = this.session;
    this.session = null;
    const sound = this.liveSound;
    this.liveSound = null;
    for (const id of [session && session.session_id, sound]) {
      if (!id) continue;
      try {
        await this.host.hass.callWS({ type: `${DOMAIN}/playback/stop`, session_id: id });
      } catch (_err) {
        /* gone already */
      }
    }
  }

  /*
   * Home Assistant's live stream keeps only AAC or MP3 sound, so a camera sending G.711 plays
   * silent there. When the device has sound on this stream, the integration serves the picture
   * with the sound converted to AAC, and the player switches to that session.
   * Resolves true when the converted picture is playing, false when the caller should carry on.
   */
  async _liveSound(token) {
    const camera = this.camera;
    const rank = this.level === "low" ? 2 : 1;
    if (this.liveSound || !camera || !camera.audio || camera.audio[rank] !== true) return false;
    try {
      const session = await this.host.hass.callWS({
        type: `${DOMAIN}/live/sound`,
        entry_id: camera.entry_id,
        channel_id: camera.channel_id,
        rank,
      });
      if (token !== this.token) {
        this.host.hass.callWS({ type: `${DOMAIN}/playback/stop`, session_id: session.session_id }).catch(() => {});
        return false;
      }
      this.liveSound = session.session_id;
      await this._attach(session.url, token);
      return true;
    } catch (err) {
      // The picture stays without sound; the reason is logged so a silent camera can be traced.
      console.warn("[hikvision-bridge] live picture with sound failed", err);
      return false;
    }
  }

  // ---- live

  async live(camera, quality) {
    this.mode = "live";
    this.quality = quality;
    this.speed = 1;
    const level = quality === "auto" ? (camera.live.low ? "low" : "high") : quality;
    await this._live(camera, level);
  }

  // WebRTC first when Home Assistant offers it for the camera (the same signalling its own
  // viewer uses), HLS otherwise or when WebRTC fails.
  async _live(camera, level, hlsOnly = false) {
    const entity = camera.live[level] || camera.live.high || camera.live.low;
    const token = ++this.token;
    clearInterval(this._keepAlive);
    if (this.session || this.liveSound) this.stop();
    this.token = token;
    this._closeRtc();
    if (!entity) {
      this._detach();
      this.host.showError("live_unavailable");
      return;
    }
    this.host.busy(true);
    this.host.clearError();
    try {
      const shown = camera.live[level] ? level : camera.live.high ? "high" : "low";
      // A sound format Home Assistant drops goes straight to the converted picture, before
      // WebRTC too: a silent WebRTC picture would play for a few seconds and then be replaced.
      if (needsSoundConversion(camera, shown)) {
        this.level = shown;
        this.camera = camera;
        if (await this._liveSound(token)) {
          if (token === this.token) this.host.update();
          return;
        }
        if (token !== this.token) return;
      }
      if (!hlsOnly && (await this._webrtc(camera, level, entity, token))) {
        if (token === this.token) {
          this.level = shown;
          this.camera = camera;
          this.host.update();
          // WebRTC can deliver a picture without its sound track too: same fallback as HLS.
          if (this.host.hasAudio === false) this._liveSound(token);
        }
        return;
      }
      if (token !== this.token) return;
      this.level = shown;
      this.camera = camera;
      const { url } = await this.host.hass.callWS({ type: "camera/stream", entity_id: entity, format: "hls" });
      if (token !== this.token) return;
      await this._attach(url, token);
      this.host.update();
    } catch (err) {
      if (token === this.token) this.host.showError(err.code === "start_stream_failed" ? "live_encrypted" : "live_unavailable");
    }
  }

  /*
   * Live picture over WebRTC through Home Assistant's camera signalling (the same messages
   * its own player uses). Resolves true once the picture plays; false means use HLS: the
   * camera has no WebRTC provider, the connection failed or it took too long.
   */
  async _webrtc(camera, level, entity, token) {
    const hass = this.host.hass;
    if (!window.RTCPeerConnection || !hass.connection) return false;
    let types = [];
    try {
      ({ frontend_stream_types: types = [] } = await hass.callWS({ type: "camera/capabilities", entity_id: entity }));
    } catch (_err) {
      return false;
    }
    if (token !== this.token || !types.includes("web_rtc")) return false;
    let configuration = {};
    try {
      ({ configuration = {} } = await hass.callWS({ type: "camera/webrtc/get_client_config", entity_id: entity }));
    } catch (_err) {
      /* no extra servers: the local network needs none */
    }
    if (token !== this.token) return false;

    this._detach();
    const video = this.video;
    const pc = new RTCPeerConnection(configuration);
    const stream = new MediaStream();
    video.srcObject = stream;
    video.muted = !this.host.sound;
    let done = false;
    let closed = false;
    let session = null;
    let unsubscribe = null;
    const queued = [];

    const send = (candidate) =>
      hass.callWS({ type: "camera/webrtc/candidate", entity_id: entity, session_id: session, candidate }).catch(() => {});
    const teardown = () => {
      if (closed) return;
      closed = true;
      pc.close();
      if (unsubscribe) unsubscribe();
    };

    return new Promise((resolve) => {
      const finish = (ok) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        video.removeEventListener("playing", onPlaying);
        if (!ok) teardown();
        resolve(ok);
      };
      // Tracks arrive before the picture plays, so the sound track is known by then.
      const onPlaying = () => {
        this.host.setAudio(stream.getAudioTracks().length > 0);
        finish(true);
      };
      const timer = setTimeout(() => finish(false), RTC_START_MS);
      const rtc = {
        close: () => {
          finish(false);
          teardown();
        },
      };
      this.rtc = rtc;

      const onMessage = (message) => {
        if (closed) return;
        if (message.type === "session") {
          session = message.session_id;
          queued.splice(0).forEach(send);
        } else if (message.type === "answer") {
          pc.setRemoteDescription({ type: "answer", sdp: message.answer }).catch(() => finish(false));
        } else if (message.type === "candidate") {
          pc.addIceCandidate(message.candidate).catch(() => {});
        } else if (message.type === "error") {
          finish(false);
        }
      };

      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.ontrack = (event) => {
        stream.addTrack(event.track);
        this._autoplay();
      };
      pc.onicecandidate = (event) => {
        if (!event.candidate) return;
        const candidate = event.candidate.toJSON();
        if (session) send(candidate);
        else queued.push(candidate);
      };
      pc.onconnectionstatechange = () => {
        if (pc.connectionState !== "failed") return;
        if (!done) finish(false);
        // Lost after it played: continue with HLS.
        else if (this.rtc === rtc && token === this.token) this._live(camera, level, true);
      };
      video.addEventListener("playing", onPlaying);

      pc.createOffer()
        .then((offer) => pc.setLocalDescription(offer))
        .then(() =>
          hass.connection.subscribeMessage(onMessage, {
            type: "camera/webrtc/offer",
            entity_id: entity,
            offer: pc.localDescription.sdp,
          }),
        )
        .then((stop) => {
          if (closed) stop();
          else unsubscribe = stop;
        })
        .catch(() => finish(false));
    });
  }

  // ---- recordings

  async play(camera, source, time, quality, resumeAt = null) {
    this.mode = "playback";
    this.quality = quality;
    this.camera = camera;
    this.source = source;
    let level = quality === "auto" ? "original" : quality;
    if (quality === "auto" && camera.codec && camera.codec.includes("265") && !this.canHevc) level = "compatible";
    this._applySpeed();
    await this._play(time, level, resumeAt);
  }

  async _play(time, level, resumeAt = null) {
    const token = ++this.token;
    this.host.busy(true);
    this.host.clearError();
    this.starting = token;
    const previous = this.session;
    try {
      const session = await this.host.hass.callWS({
        type: `${DOMAIN}/playback/start`,
        entry_id: this.camera.entry_id,
        channel_id: this.camera.channel_id,
        source: this.source,
        time: new Date(time).toISOString(),
        quality: level,
        ...(previous ? { replace: previous.session_id } : {}),
      });
      if (token !== this.token) {
        this.host.hass.callWS({ type: `${DOMAIN}/playback/stop`, session_id: session.session_id }).catch(() => {});
        return;
      }
      this.session = { ...session, start: new Date(session.start).getTime(), end: new Date(session.end).getTime() };
      this.level = level;
      this.base = null;
      clearInterval(this._keepAlive);
      // Paused players stop asking for the playlist; keep the session alive meanwhile.
      this._keepAlive = setInterval(() => fetch(this.session ? this.session.url : "", { cache: "no-store" }).catch(() => {}), 20e3);
      await this._attach(session.url, token);
      // A session starts at the key frame before the wanted time: play on from that time,
      // not from the key frame, so the part before it is not shown a second time.
      if (resumeAt != null && token === this.token && this.session.start < resumeAt) {
        try {
          this.video.currentTime = (resumeAt - this.session.start) / 1000;
        } catch (_err) {
          /* the video cannot seek yet: it plays from the key frame */
        }
      }
      this.host.update();
    } catch (err) {
      if (token !== this.token) return;
      if (previous && this.session === previous) this.session = null;
      // Replaced by a newer start for the camera from elsewhere (another viewer).
      const code = err.code === "playback_replaced" ? "playback_busy" : err.code;
      this.host.showError(code || "playback_failed");
    } finally {
      if (this.starting === token) this.starting = null;
    }
  }

  // Recorded time now shown (ms), from the session start and the video position.
  get position() {
    if (this.mode === "live") return Date.now();
    if (!this.session) return null;
    const video = this.video;
    if (this.base == null && video.buffered.length) this.base = video.buffered.start(0);
    return this.session.start + (video.currentTime - (this.base ?? 0)) * 1000;
  }

  // Seek inside the running session if that part is prepared, else start a new one. The
  // session keeps everything from its start and prepares a minute or more ahead.
  async seek(time) {
    // While a start is running the server has already ended the session still shown.
    if (this.mode === "playback" && this.session && this.base != null && this.starting == null) {
      const video = this.video;
      const offset = (time - this.session.start) / 1000 + this.base;
      for (let i = 0; i < video.seekable.length; i += 1) {
        if (offset >= video.seekable.start(i) && offset <= video.seekable.end(i) - 0.5) {
          video.currentTime = offset;
          return;
        }
      }
    }
    await this._play(time, this.level || "original");
  }

  // ---- automatic quality

  _stalled() {
    const now = Date.now();
    this.stalls = this.stalls.filter((t) => now - t < 20e3);
    this.stalls.push(now);
    this.host.busy(true);
    if (this.quality !== "auto" || this.mode !== "live" || this.level !== "high") return;
    if (this.stalls.length >= 2 && this.camera.live.low) {
      this.stalls = [];
      this._live(this.camera, "low");
    }
  }

  _smooth() {
    // A picture is playing: an error from an earlier attempt no longer applies.
    this.host.clearError();
    this.host.busy(false);
    clearTimeout(this._upgradeTimer);
    if (this.quality !== "auto" || this.mode !== "live" || this.level !== "low" || !this.camera.live.high) return;
    this._upgradeTimer = setTimeout(() => {
      const recent = this.stalls.filter((t) => Date.now() - t < 20e3).length;
      const bandwidth = this.hls ? this.hls.bandwidthEstimate : null;
      const connection = navigator.connection && navigator.connection.downlink;
      const fast = (bandwidth == null || bandwidth > 6e6) && (!connection || connection >= 5);
      if (!recent && fast && !this.video.paused) this._live(this.camera, "high");
    }, 6000);
  }
}

/* ------------------------------------------------------------------- viewer */

const POPOVER = Object.prototype.hasOwnProperty.call(HTMLElement.prototype, "popover");
const WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];

function firstWeekday(hass) {
  const chosen = WEEKDAYS.indexOf(hass?.locale?.first_weekday);
  if (chosen >= 0) return chosen;
  try {
    const locale = new Intl.Locale(hass?.language || "en");
    const info = locale.getWeekInfo?.() || locale.weekInfo;
    if (info?.firstDay) return info.firstDay % 7;
  } catch (_err) {
    /* unknown language */
  }
  return 1;
}

/*
 * Month calendar of the day picker, in a popover. Days with recordings get a dot; the
 * device keeps a calendar of them that `days(year, month)` asks for.
 */
class Calendar {
  constructor(button, popup, onPick) {
    this.button = button;
    this.popup = popup;
    this.onPick = onPick;
    this.selected = startOfDay(new Date());
    this.month = new Date(this.selected.getFullYear(), this.selected.getMonth(), 1);
    this.days = async () => null;
    this.language = "en";
    this.firstDay = 1;
    this.t = (key) => TEXT[key] || key;
    if (POPOVER) {
      popup.popover = "auto";
      button.popoverTargetElement = popup;
      popup.addEventListener("beforetoggle", (e) => {
        if (e.newState === "open") this._opening();
      });
      popup.addEventListener("toggle", (e) => {
        if (e.newState === "open") this._place();
      });
    } else {
      popup.classList.add("fallback");
      document.addEventListener("click", (e) => {
        if (!e.composedPath().some((el) => el === popup || el === button)) this.close();
      });
    }
    popup.addEventListener("click", (e) => this._click(e));
  }

  toggle() {
    // Without the popover API; with it the button opens and closes the popover itself.
    if (POPOVER) return;
    if (this.popup.classList.contains("open")) this.close();
    else {
      this._opening();
      this.popup.classList.add("open");
    }
  }

  close() {
    if (!POPOVER) this.popup.classList.remove("open");
    else if (this.popup.matches(":popover-open")) this.popup.hidePopover();
  }

  _opening() {
    this.month = new Date(this.selected.getFullYear(), this.selected.getMonth(), 1);
    this.render();
    this._place();
  }

  _place() {
    if (!POPOVER) return;
    const rect = this.button.getBoundingClientRect();
    const width = this.popup.offsetWidth || 296;
    const height = this.popup.offsetHeight || 360;
    const below = innerHeight - rect.bottom - 8 >= height + 6;
    this.popup.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - width - 8))}px`;
    this.popup.style.top = `${below ? rect.bottom + 6 : Math.max(8, rect.top - height - 6)}px`;
  }

  async render() {
    const month = this.month;
    const year = month.getFullYear();
    const index = month.getMonth();
    const today = startOfDay(new Date());
    const count = new Date(year, index + 1, 0).getDate();
    const lead = (month.getDay() - this.firstDay + 7) % 7;
    const weekday = new Intl.DateTimeFormat(this.language, { weekday: "short" });
    // 7 January 2024 was a Sunday.
    const names = Array.from({ length: 7 }, (_, i) =>
      weekday.format(new Date(2024, 0, 7 + ((this.firstDay + i) % 7))).replace(/\.$/, "")
    );
    const title = new Intl.DateTimeFormat(this.language, { month: "long", year: "numeric" }).format(month);
    const cells = [];
    for (let day = 1; day <= count; day += 1) {
      const date = new Date(year, index, day);
      const classes = ["cal-day"];
      if (date.getTime() === today.getTime()) classes.push("today");
      if (date.getTime() === this.selected.getTime()) classes.push("selected");
      cells.push(
        `<button class="${classes.join(" ")}" data-day="${isoDay(date)}"${date > today ? " disabled" : ""}>${day}</button>`
      );
    }
    const t = this.t;
    const last = year * 12 + index >= today.getFullYear() * 12 + today.getMonth();
    this.popup.innerHTML = `
      <div class="cal-head">
        <button class="icon-button" data-month="-1" title="${t("previous_month")}" aria-label="${t("previous_month")}">${ICONS.left}</button>
        <span class="cal-title">${title}</span>
        <button class="icon-button" data-month="1" title="${t("next_month")}" aria-label="${t("next_month")}"${last ? " disabled" : ""}>${ICONS.right}</button>
      </div>
      <div class="cal-grid">
        ${names.map((n) => `<span class="cal-wd">${n}</span>`).join("")}
        ${"<span></span>".repeat(lead)}${cells.join("")}
      </div>
      <div class="cal-foot"><span class="key" hidden>${t("recording_days")}</span><button data-today>${t("today")}</button></div>`;
    this.popup.classList.add("loading");
    const days = await this.days(year, index + 1);
    if (month !== this.month) return;
    this.popup.classList.remove("loading");
    if (!days) return;
    for (const button of this.popup.querySelectorAll("[data-day]")) {
      const recorded = days.has(Number(button.dataset.day.slice(8)));
      button.classList.toggle("rec", recorded);
      button.classList.toggle("norec", !recorded);
    }
    this.popup.querySelector(".key").hidden = false;
  }

  _click(e) {
    const step = e.target.closest("[data-month]");
    if (step) {
      if (step.disabled) return;
      this.month = new Date(this.month.getFullYear(), this.month.getMonth() + Number(step.dataset.month), 1);
      this.render().then(() => this._place());
      this._place();
      return;
    }
    const day = e.target.closest("[data-day]");
    const today = e.target.closest("[data-today]");
    if ((!day || day.disabled) && !today) return;
    this.close();
    this.onPick(today ? new Date() : new Date(`${day.dataset.day}T00:00:00`));
  }
}

/*
 * Video, controls and timeline of one camera. Used by the card on its own and by the
 * panel next to its camera list.
 */
class Viewer {
  constructor(root, hass, options = {}) {
    this.root = root;
    this.hass = hass;
    this.compact = Boolean(options.compact);
    this.t = makeTranslator({});
    this.camera = null;
    this.source = null;
    this.mode = "live";
    this.quality = "auto";
    this.sound = false; // starts muted: browsers refuse autoplay with sound
    this.hasAudio = null; // whether the current stream carries sound; null until known
    this.day = startOfDay(new Date());
    this.data = { recordings: [], events: [] };
    root.innerHTML = `
      <div class="viewer">
        <div class="stage">
          <video playsinline muted></video>
          <div class="badges"></div>
          <div class="overlay"></div>
        </div>
        <div class="clip" hidden><span class="clip-at">0:00</span><input type="range" min="0" max="1" step="0.1" value="0"><span class="clip-length">0:00</span></div>
        <div class="controls">
          <div class="group start">
            <button class="live-button" data-action="live">${ICONS.live}<span></span></button>
          </div>
          <div class="group middle">
            <button class="icon-button" data-action="prev">${ICONS.prev}</button>
            <button class="icon-button" data-action="back">${ICONS.back}</button>
            <button class="icon-button" data-action="toggle">${ICONS.pause}</button>
            <button class="icon-button" data-action="forward">${ICONS.forward}</button>
            <button class="icon-button" data-action="next">${ICONS.next}</button>
          </div>
          <div class="group end">
            <select class="source" hidden></select>
            <select class="quality"></select>
            <select class="speed" hidden></select>
            <button class="icon-button" data-action="sound">${ICONS.soundOff}</button>
            <button class="icon-button" data-action="download">${ICONS.download}</button>
            <button class="icon-button" data-action="fullscreen">${ICONS.fullscreen}</button>
          </div>
        </div>
        <div class="day-bar">
          <button class="icon-button" data-action="day-prev">${ICONS.left}</button>
          <div class="date-picker">
            <button class="date-button" data-action="calendar">${ICONS.calendar}<span class="long"></span><span class="short"></span>${ICONS.caret}</button>
            <div class="calendar" role="dialog"></div>
          </div>
          <button class="icon-button" data-action="day-next">${ICONS.right}</button>
          <button class="today" data-action="today"></button>
          <span class="spacer"></span>
          <span class="unread" hidden></span>
          <button class="mark-read" data-action="mark-all-read" hidden></button>
          <div class="segmented view-switch">
            <button data-action="view-timeline"></button>
            <button data-action="view-clips"></button>
          </div>
          <button class="icon-button zoom" data-action="zoom-out">${ICONS.minus}</button>
          <button class="icon-button zoom" data-action="zoom-in">${ICONS.plus}</button>
        </div>
        <div class="timeline"></div>
        <div class="clips" hidden></div>
        <div class="legend"></div>
        <div class="hint" hidden></div>
      </div>`;
    this.video = root.querySelector("video");
    this._view = { scale: VIDEO_ZOOM_MIN, x: 0, y: 0 };
    this._zoomable(root.querySelector(".stage"));
    this.player = new Player(this);
    this.timeline = new Timeline(root.querySelector(".timeline"), (date) => this.seek(date));
    root.querySelector(".controls").addEventListener("click", (e) => this._click(e));
    // Event kinds switched off in the legend leave the timeline and the previous/next buttons.
    this.hiddenEvents = new Set();
    try {
      for (const kind of JSON.parse(localStorage.getItem(HIDDEN_EVENTS_KEY) || "[]")) this.hiddenEvents.add(kind);
    } catch (_err) {
      /* storage unavailable: all kinds shown */
    }
    root.querySelector(".legend").addEventListener("click", (e) => {
      const kind = e.target.closest("[data-kind]")?.dataset.kind;
      if (kind) this.toggleEvent(kind);
    });
    // Seek bar of the recording file playing now: dragging shows the time, letting go seeks.
    const clipRange = root.querySelector(".clip input");
    clipRange.addEventListener("input", () => {
      this._clipDrag = true;
      root.querySelector(".clip-at").textContent = duration(Number(clipRange.value) * 1000);
    });
    clipRange.addEventListener("change", () => {
      this._clipDrag = false;
      const clip = this._clip;
      if (clip) this.seek(new Date(clip.start + Number(clipRange.value) * 1000));
    });
    root.querySelector(".day-bar").addEventListener("click", (e) => this._click(e));
    root.querySelector(".clips").addEventListener("click", (e) => this._click(e));
    this.view = savedView();
    this._clips = [];
    this._clipSignature = null;
    this._clipObserver = null;
    this._activeClip = -1;
    this._applyView();
    root.querySelector(".quality").addEventListener("change", (e) => this.setQuality(e.target.value));
    root.querySelector(".speed").addEventListener("change", (e) => this.setSpeed(Number(e.target.value)));
    root.querySelector(".source").addEventListener("change", (e) => {
      this.source = e.target.value;
      this.loadTimeline();
      if (this.mode === "playback") this.seek(new Date(this.player.position ?? Date.now()));
    });
    this._days = new Map();
    this.calendar = new Calendar(root.querySelector(".date-button"), root.querySelector(".calendar"), (day) => this.setDay(day));
    this.calendar.days = (year, month) => this._recordedDays(year, month);
    this.video.addEventListener("pause", () => this.update());
    this.video.addEventListener("play", () => this.update());
    this.video.addEventListener("volumechange", () => this._soundButton());
    this.video.addEventListener("loadedmetadata", () => this._fit());
    this.video.addEventListener("resize", () => this._fit());
    this._stageSize = new ResizeObserver(() => {
      this._fit();
      this._zoomTo(this._view.scale);
    });
    this._stageSize.observe(root.querySelector(".stage"));
    this._stageSize.observe(this.video); // a resized video keeps its zoom inside the new box
    this._tick = setInterval(() => this._frame(), 250);
    this._refresh = setInterval(() => {
      if (isoDay(this.day) === isoDay(new Date())) this.loadTimeline();
    }, 60e3);
    loadTexts(hass).then((resources) => {
      this.t = makeTranslator(resources);
      this._label();
      this.update();
    });
    this._label();
  }

  destroy() {
    clearInterval(this._tick);
    clearInterval(this._refresh);
    this._stageSize.disconnect();
    this.timeline.destroy();
    this._clipObserver?.disconnect();
    this.player.stop();
  }

  /*
   * Sub streams are often 640x480 on wide cameras, the picture squeezed into 4:3. Such a
   * stream is shown in the main stream's shape, as the Hikvision apps do.
   */
  _fit() {
    const video = this.video;
    const main = /^(\d+)x(\d+)$/.exec(this.camera?.resolution || "");
    const shape = main ? main[1] / main[2] : 0;
    const own = video.videoHeight ? video.videoWidth / video.videoHeight : 0;
    if (!shape || !own || Math.abs(shape / own - 1) < 0.05) {
      video.style.removeProperty("width");
      video.style.removeProperty("height");
      video.style.removeProperty("object-fit");
      return;
    }
    const stage = video.parentElement;
    const width = Math.min(stage.clientWidth, stage.clientHeight * shape);
    video.style.width = `${width}px`;
    video.style.height = `${width / shape}px`;
    video.style.objectFit = "fill";
  }

  /*
   * Digital pan and zoom, presentation only: a CSS transform on the video, the stream is
   * untouched. The wheel or a pinch zooms around the pointer, a drag pans while zoomed and
   * a double click or double tap returns to the whole picture.
   *
   * The panel is always armed. On a dashboard card the wheel and a swipe must keep scrolling
   * the page, so the card starts unarmed: a tap arms it, a double tap or the mouse leaving
   * the card returns to the whole picture and disarms. Tap one of a double tap arms, tap two
   * resets and disarms, so a quick double tap on an unarmed card changes nothing. Ctrl (or
   * Cmd) with the wheel, which is also how a trackpad pinch arrives, zooms without arming.
   */
  _zoomable(stage) {
    stage.classList.add("zoomable");
    const arm = (on) => stage.classList.toggle("armed", (this._zoomArmed = on));
    arm(!this.compact);
    const pointers = new Map();
    let pinch = null; // midpoint and distance of the two fingers at the previous move
    let tap = null; // the previous tap, to recognise a double tap
    // A screen position relative to the unzoomed video box.
    const at = (x, y) => {
      const box = this.video.getBoundingClientRect();
      return { x: x - box.left + this._view.x, y: y - box.top + this._view.y };
    };
    stage.addEventListener(
      "wheel",
      (e) => {
        const ctrl = e.ctrlKey || e.metaKey;
        if (!this._zoomArmed && !ctrl) return; // the page scrolls
        if (ctrl) e.preventDefault(); // never zoom the whole page over the video
        const delta = e.deltaY * (e.deltaMode ? 33 : 1); // lines to pixels
        if (!delta || (delta > 0 && this._view.scale <= VIDEO_ZOOM_MIN)) return;
        e.preventDefault();
        this._zoomTo(this._view.scale * Math.exp(-delta / 400), at(e.clientX, e.clientY));
      },
      { passive: false },
    );
    stage.addEventListener("pointerdown", (e) => {
      if (e.button) return;
      stage.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, travel: 0 });
      pinch = null;
    });
    stage.addEventListener("pointermove", (e) => {
      const p = pointers.get(e.pointerId);
      if (!p) return;
      const dx = e.clientX - p.x;
      const dy = e.clientY - p.y;
      Object.assign(p, { x: e.clientX, y: e.clientY, travel: p.travel + Math.hypot(dx, dy) });
      if (!this._zoomArmed) return; // only the travel counts, to tell a tap from a swipe
      const view = this._view;
      if (pointers.size === 1) {
        view.x += dx;
        view.y += dy;
        this._zoomTo(view.scale);
        return;
      }
      const [a, b] = pointers.values();
      a.travel = b.travel = Infinity; // a pinch is no tap
      const now = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, distance: Math.hypot(a.x - b.x, a.y - b.y) };
      if (pinch?.distance) {
        // Zoom around the previous midpoint, then follow the fingers to the new one.
        this._zoomTo((view.scale * now.distance) / pinch.distance, at(pinch.x, pinch.y));
        view.x += now.x - pinch.x;
        view.y += now.y - pinch.y;
        this._zoomTo(view.scale);
      }
      pinch = now;
    });
    const release = (e) => {
      const p = pointers.get(e.pointerId);
      if (!p) return;
      pointers.delete(e.pointerId);
      pinch = null;
      if (e.type !== "pointerup" || p.travel > 8) return;
      const double = tap && e.timeStamp - tap.time < 350 && Math.hypot(e.clientX - tap.x, e.clientY - tap.y) < 30;
      tap = double ? null : { time: e.timeStamp, x: e.clientX, y: e.clientY };
      if (double) this._zoomTo(VIDEO_ZOOM_MIN);
      if (this.compact) arm(!double);
    };
    stage.addEventListener("pointerup", release);
    stage.addEventListener("pointercancel", release);
    // Only the mouse: a finger also "leaves" when it lifts after every tap.
    stage.addEventListener("pointerleave", (e) => {
      if (!this.compact || e.pointerType !== "mouse") return;
      this._zoomTo(VIDEO_ZOOM_MIN);
      arm(false);
    });
  }

  // Sets the magnification, keeping the picture under `at` (from the unzoomed video box) in
  // place, and holds the zoomed picture over the whole video box: no edge comes into view.
  _zoomTo(scale, at) {
    const view = this._view;
    const next = Math.min(VIDEO_ZOOM_MAX, Math.max(VIDEO_ZOOM_MIN, scale || VIDEO_ZOOM_MIN));
    if (at) {
      view.x = at.x - ((at.x - view.x) * next) / view.scale;
      view.y = at.y - ((at.y - view.y) * next) / view.scale;
    }
    view.scale = next;
    const width = this.video.offsetWidth;
    const height = this.video.offsetHeight;
    view.x = Math.min(0, Math.max(width - width * next, view.x));
    view.y = Math.min(0, Math.max(height - height * next, view.y));
    this.video.style.transform = next > VIDEO_ZOOM_MIN ? `translate(${view.x}px, ${view.y}px) scale(${next})` : "";
    this.video.parentElement.classList.toggle("zoomed", next > VIDEO_ZOOM_MIN);
  }

  // The button shows whether sound plays and offers the opposite; without a sound track in
  // the stream there is nothing to switch on, so it is disabled.
  _soundButton() {
    const button = this.root.querySelector('[data-action="sound"]');
    const muted = this.video.muted;
    const silent = this.hasAudio === false;
    button.innerHTML = muted || silent ? ICONS.soundOff : ICONS.soundOn;
    button.disabled = silent;
    const label = this.t.ui(silent ? "no_sound" : muted ? "sound_on" : "sound_off");
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", String(!muted && !silent));
  }

  // Called by the player once it knows the sound track of the stream (null: not yet known).
  setAudio(value) {
    this.hasAudio = value;
    this._soundButton();
  }

  // A picture started: an error shown for an earlier attempt no longer applies.
  clearError() {
    if (this._error == null) return;
    this._error = null;
    this._overlay();
  }

  _label() {
    const t = this.t.ui;
    const titles = {
      prev: "previous_event",
      back: "back",
      forward: "forward",
      next: "next_event",
      download: "download",
      fullscreen: "fullscreen",
      "day-prev": "previous_day",
      "day-next": "next_day",
      "zoom-in": "zoom_in",
      "zoom-out": "zoom_out",
    };
    for (const [action, key] of Object.entries(titles)) {
      const button = this.root.querySelector(`[data-action="${action}"]`);
      button.title = t(key);
      button.setAttribute("aria-label", t(key));
    }
    this.root.querySelector(".live-button span").textContent = t("live");
    this._soundButton();
    this.root.querySelector(".today").textContent = t("today");
    this.root.querySelector('[data-action="view-timeline"]').textContent = t("view_timeline");
    this.root.querySelector('[data-action="view-clips"]').textContent = t("view_clips");
    const picker = this.root.querySelector(".date-button");
    picker.title = t("pick_day");
    picker.setAttribute("aria-label", t("pick_day"));
    this.calendar.t = t;
    this.calendar.language = this.hass?.language || "en";
    this.calendar.firstDay = firstWeekday(this.hass);
    this._dateLabel();
    this.timeline.kindName = (k) => this.t.kind(k);
    this.timeline.eventName = (k) => this.t.event(k);
    this._qualityOptions();
    this._speedOptions();
    this._legend();
    this._unreadBar();
  }

  _speedOptions() {
    const select = this.root.querySelector(".speed");
    select.innerHTML = SPEEDS.map((s) => `<option value="${s}">${this.t.ui("speed")}: ${s}x</option>`).join("");
    select.value = String(this.player.speed);
  }

  setSpeed(speed) {
    this.player.setSpeed(speed);
    this.update();
  }

  _qualityOptions() {
    const select = this.root.querySelector(".quality");
    const t = this.t.ui;
    const options = this.mode === "live" ? ["auto", "low", "high"] : ["auto", "original", "compatible"];
    if (!options.includes(this.quality)) this.quality = "auto";
    select.innerHTML = options.map((o) => `<option value="${o}">${t("quality")}: ${t(o)}</option>`).join("");
    select.value = this.quality;
  }

  _legend() {
    const kinds = [...new Set(this.data.recordings.map((r) => r.kind))];
    const events = [...new Set(this.data.events.map((e) => e.kind))];
    this.root.querySelector(".legend").innerHTML = [
      ...kinds.map((k) => `<span style="--c:${KIND_COLORS[k] || KIND_COLORS.other}">${this.t.kind(k)}</span>`),
      ...events.map((k) => {
        const off = this.hiddenEvents.has(k);
        return `<button type="button" class="event${off ? " off" : ""}" data-kind="${k}" aria-pressed="${!off}" style="--c:${EVENT_COLORS[k] || "var(--hb-accent)"}">${symbolIcon(k)}${this.t.event(k)}</button>`;
      }),
    ].join("");
  }

  // Events of the kinds shown: the timeline and previous/next use only these.
  _shownEvents() {
    return this.data.events.filter((e) => !this.hiddenEvents.has(e.kind));
  }

  toggleEvent(kind) {
    if (this.hiddenEvents.has(kind)) this.hiddenEvents.delete(kind);
    else this.hiddenEvents.add(kind);
    try {
      localStorage.setItem(HIDDEN_EVENTS_KEY, JSON.stringify([...this.hiddenEvents]));
    } catch (_err) {
      /* storage unavailable: applies until the page is reloaded */
    }
    this.timeline.events = this._shownEvents();
    this.timeline.draw();
    this._legend();
  }

  // The recordings of the day joined into clips, as the clip list shows them.
  _joinedClips() {
    if (this._joinedOf !== this.data.recordings) {
      this._joinedOf = this.data.recordings;
      this._joined = clipsOf(this.data.recordings);
    }
    return this._joined;
  }

  // The recording file that contains a moment, if any.
  _recordingAt(time) {
    return this.data.recordings.find((r) => r.start <= time && time < r.end) || null;
  }

  setHass(hass) {
    this.hass = hass;
  }

  async setCamera(camera) {
    if (this.camera && camera && this.camera.entry_id === camera.entry_id && this.camera.channel_id === camera.channel_id) return;
    // During playback the next camera continues at the recorded time now shown.
    const resume = this.camera && this.mode === "playback" ? (this._target?.time ?? this.player.position ?? this.timeline.playhead) : null;
    const paused = this.video.paused;
    await this.player.stop();
    this.camera = camera;
    this._zoomTo(VIDEO_ZOOM_MIN); // another camera starts with the whole picture
    if (!camera) {
      this.showMessage(this.t.ui("select_camera"));
      return;
    }
    // Keep the recording source when the next camera has it too.
    this.source = camera.sources.includes(this.source) ? this.source : camera.sources[0] || null;
    const select = this.root.querySelector(".source");
    select.hidden = camera.sources.length < 2;
    select.innerHTML = camera.sources.map((s) => `<option value="${s}">${this.t.ui("source")}: ${this.t.source(s)}</option>`).join("");
    if (this.source) select.value = this.source;
    this.data = { recordings: [], events: [] };
    this.loadTimeline();
    if (resume == null || !camera.sources.length) return this.goLive();
    await this.seek(new Date(resume));
    if (paused && this.camera === camera && this.mode === "playback") this.video.pause();
  }

  _dateLabel() {
    const language = this.hass?.language || "en";
    const button = this.root.querySelector(".date-button");
    button.querySelector(".long").textContent = this.day.toLocaleDateString(language, {
      weekday: "short",
      day: "numeric",
      month: "short",
      year: "numeric",
    });
    button.querySelector(".short").textContent = this.day.toLocaleDateString(language, { day: "numeric", month: "short" });
    this.calendar.selected = this.day;
  }

  _recordedDays(year, month) {
    const camera = this.camera;
    if (!camera || !this.source) return Promise.resolve(null);
    const key = `${camera.entry_id}/${camera.channel_id}/${this.source}/${year}-${month}`;
    const now = new Date();
    const current = year === now.getFullYear() && month === now.getMonth() + 1;
    const cached = this._days.get(key);
    // The running month gains days; ask again after a while.
    if (cached && (!current || Date.now() - cached.at < 10 * 60e3)) return cached.days;
    const days = this.hass
      .callWS({ type: `${DOMAIN}/days`, entry_id: camera.entry_id, channel_id: camera.channel_id, source: this.source, year, month })
      .then((result) => (result.days ? new Set(result.days) : null))
      .catch(() => null)
      .then((found) => {
        // No answer this time (device busy with a playback): ask again next time.
        if (!found && this._days.get(key)?.days === days) this._days.delete(key);
        return found;
      });
    this._days.set(key, { at: Date.now(), days });
    return days;
  }

  setDay(day) {
    this.day = startOfDay(day);
    this._dateLabel();
    this.timeline.setDay(this.day);
    this.loadTimeline();
  }

  async loadTimeline() {
    const camera = this.camera;
    if (!camera) return;
    const day = this.day;
    if (this.timeline.dayStart !== day.getTime()) this.timeline.setDay(day);
    this._dateLabel();
    const key = `${camera.entry_id}/${camera.channel_id}/${this.source}/${day.getTime()}`;
    // A day not shown yet is loading until its search answers; the minute refresh of today
    // keeps the recordings on screen meanwhile.
    if (key !== this._timelineKey) {
      this._timelineKey = key;
      this.data = { recordings: [], events: [] };
      this.timeline.recordings = [];
      this.timeline.events = [];
      this.timeline.loadingText = this.t.ui("loading");
      this.timeline.loading = this.loading = true;
      this.timeline.draw();
      this._renderClips();
      this._hint();
      this._unreadBar();
    }
    try {
      const result = await this.hass.callWS({
        type: `${DOMAIN}/timeline`,
        entry_id: camera.entry_id,
        channel_id: camera.channel_id,
        source: this.source || "auto",
        start: day.toISOString(),
        end: new Date(day.getTime() + DAY).toISOString(),
      });
      if (key !== this._timelineKey) return;
      // iso keeps the server's own start: the read state of an event is keyed by it.
      const parse = (list) => list.map((i) => ({ ...i, iso: i.start, start: new Date(i.start).getTime(), end: new Date(i.end).getTime() }));
      this.data = { recordings: parse(result.recordings), events: parse(result.events) };
    } catch (_err) {
      if (key !== this._timelineKey) return;
      this.data = { recordings: [], events: [] };
    }
    this.timeline.loading = this.loading = false;
    this.timeline.recordings = this.data.recordings;
    this.timeline.events = this._shownEvents();
    this.timeline.draw();
    this._legend();
    this._unreadBar();
    this._renderClips();
    this._hint();
  }

  // Events of the day not read yet, with the button that marks them all read.
  _unreadBar() {
    const count = this.data.events.filter((ev) => !ev.read).length;
    const badge = this.root.querySelector(".unread");
    const button = this.root.querySelector(".mark-read");
    badge.textContent = `${this.t.ui("unread")} ${count}`;
    button.textContent = this.t.ui("mark_all_read");
    badge.hidden = button.hidden = count === 0;
  }

  // Opening a clip reads the events it shows: the ones around the moment played from.
  _opened(time) {
    const opened = this.data.events.filter((ev) => !ev.read && ev.start <= time && time <= ev.end);
    if (opened.length) this._markRead(opened);
  }

  // An event is read once the playhead plays through it. Clips start before their events
  // (the recorder keeps the seconds before), so opening a clip alone does not read them. A
  // short event between two frames still counts: the playhead covers the stretch it moved.
  _playedThrough(position) {
    const last = this._readUpTo;
    this._readUpTo = position;
    const from = last != null && position >= last && position - last < 2000 ? last : position;
    const played = this.data.events.filter((ev) => !ev.read && ev.start <= position && ev.end >= from);
    if (played.length) this._markRead(played);
  }

  _markRead(events) {
    const camera = this.camera;
    if (!camera || !events.length) return;
    for (const ev of events) ev.read = true;
    this.timeline.draw();
    this._unreadBar();
    this.hass
      .callWS({
        type: `${DOMAIN}/events/read`,
        entry_id: camera.entry_id,
        channel_id: camera.channel_id,
        events: events.map((ev) => ({ entity_id: ev.entity_id, start: ev.iso })),
      })
      .catch(() => {});
  }

  async _markAll() {
    const camera = this.camera;
    const day = this.day;
    if (!camera) return;
    try {
      await this.hass.callWS({
        type: `${DOMAIN}/events/read_all`,
        entry_id: camera.entry_id,
        channel_id: camera.channel_id,
        start: day.toISOString(),
        end: new Date(day.getTime() + DAY).toISOString(),
      });
    } catch (_err) {
      return;
    }
    // Another camera or day may be on screen by now: only what is still shown changes.
    if (this.camera !== camera || this.day !== day) return;
    for (const ev of this.data.events) ev.read = true;
    this.timeline.draw();
    this._unreadBar();
  }

  // ---- clip list: one picture per recording, loaded when it scrolls into view

  _setView(view) {
    if (this.view === view) return;
    this.view = view;
    rememberView(view);
    this._applyView();
  }

  _applyView() {
    const clips = this.view === "clips";
    this.root.querySelector(".timeline").hidden = clips;
    this.root.querySelector(".clips").hidden = !clips;
    for (const zoom of this.root.querySelectorAll(".zoom")) zoom.hidden = clips;
    for (const button of this.root.querySelectorAll("[data-action^='view-']")) {
      button.classList.toggle("active", button.dataset.action === `view-${this.view}`);
    }
    if (clips) this._renderClips(true);
  }

  _renderClips(force = false) {
    const box = this.root.querySelector(".clips");
    const camera = this.camera;
    const source = this.source;
    this._clips = camera && source
      ? clipsOf(this.data.recordings).map((clip) => ({
          ...clip,
          thumb: `/api/${DOMAIN}/thumbnail/${camera.entry_id}/${camera.channel_id}/${source}/${clip.token}`,
        }))
      : [];
    const signature = `${camera ? `${camera.entry_id}/${camera.channel_id}/${source}` : ""}#${this._clips
      .map((c) => `${c.start}|${c.end}|${c.kind}|${c.token}`)
      .join(";")}`;
    if (!force && signature === this._clipSignature) return;
    this._clipSignature = signature;
    this._activeClip = -1;
    this._clipObserver?.disconnect();
    this._clipObserver = null;
    box.innerHTML = this._clips
      .map((clip, index) => {
        const time = `${clock(new Date(clip.start), false)} - ${clock(new Date(clip.end), false)}`;
        const kind = this.t.kind(clip.kind);
        return `<button class="clip-card" data-action="clip" data-index="${index}" style="--c:${KIND_COLORS[clip.kind] || KIND_COLORS.other}" aria-label="${time} ${kind}">
          <span class="thumb"><img alt="" data-index="${index}"><span class="none">${this.t.ui("no_picture")}</span></span>
          <span class="clip-time">${time}</span><span class="clip-kind">${kind}</span>
        </button>`;
      })
      .join("");
    const images = box.querySelectorAll("img");
    if (typeof IntersectionObserver === "function") {
      this._clipObserver = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            this._clipObserver.unobserve(entry.target);
            this._loadPicture(entry.target);
          }
        },
        { root: box, rootMargin: "160px" },
      );
      for (const img of images) this._clipObserver.observe(img);
    } else {
      for (const img of images) this._loadPicture(img);
    }
  }

  async _loadPicture(img) {
    const clip = this._clips[Number(img.dataset.index)];
    if (!clip) return;
    const card = img.closest(".clip-card");
    img.onload = () => img.classList.add("loaded");
    img.onerror = () => card.classList.add("failed");
    try {
      // Picture requests carry no login header: the path is signed for this user.
      const signed = await this.hass.callWS({ type: "auth/sign_path", path: clip.thumb, expires: 3600 });
      img.src = signed.path;
    } catch (_err) {
      card.classList.add("failed");
    }
  }

  // Highlights the clip the playhead is in.
  _markClip(position) {
    const index = position == null ? -1 : this._clips.findIndex((c) => c.start <= position && position < c.end);
    if (index === this._activeClip) return;
    this._activeClip = index;
    const box = this.root.querySelector(".clips");
    box.querySelector(".clip-card.active")?.classList.remove("active");
    box.querySelector(`.clip-card[data-index="${index}"]`)?.classList.add("active");
  }

  _hint() {
    const hint = this.root.querySelector(".hint");
    let text = "";
    if (this.camera && !this.camera.sources.length) text = "";
    else if (this.loading) text = this.t.ui("loading");
    else if (this.camera && !this.data.recordings.length) text = this.t.ui("no_recordings");
    else if (this.mode === "playback" && this.player.level === "original" && this.camera?.codec?.includes("265") && !this.player.canHevc)
      text = this.t.ui("hevc_hint");
    hint.textContent = text;
    hint.hidden = !text;
  }

  async goLive() {
    this.mode = "live";
    this._target = null;
    this._qualityOptions();
    this.clearMessage();
    if (isoDay(this.day) !== isoDay(new Date())) this.setDay(new Date());
    await this.player.live(this.camera, this.quality);
    this.update();
  }

  async seek(date) {
    if (!this.camera || !this.camera.sources.length) return;
    if (date.getTime() > Date.now()) return this.goLive();
    // A chosen moment brings the view back to the playhead.
    this.timeline.followPlayhead = true;
    this._eventCursor = null;
    this._brokeOff = null;
    this._readUpTo = null;
    const wasLive = this.mode === "live";
    this.mode = "playback";
    if (wasLive) {
      await this.player.stop();
      this._qualityOptions();
    }
    this.clearMessage();
    // Until the video shows the chosen moment, the playhead stays there: the player still
    // reports live time or the previous session meanwhile.
    const target = { time: date.getTime(), done: false };
    this._target = target;
    this.timeline.playhead = target.time;
    this.timeline.draw();
    try {
      if (wasLive || !this.player.session) await this.player.play(this.camera, this.source, target.time, this.quality);
      else await this.player.seek(target.time);
    } finally {
      target.done = true;
    }
    this._opened(target.time);
    this.update();
  }

  setQuality(quality) {
    this.quality = quality;
    if (!this.camera) return;
    if (this.mode === "live") this.player.live(this.camera, quality);
    else this.player.play(this.camera, this.source, this.player.position ?? Date.now(), quality);
  }

  onEnded() {
    // End of a stretch of recordings: continue with the next one.
    if (this.mode !== "playback" || !this.player.session) return;
    // The old video may end while a seek starts a new session; the seek wins.
    if (this.player.starting != null) return;
    // A session ends early when the device broke off the download: carry on from there.
    // A new session starts at the key frame before that point, so a file whose end the device
    // never sends ended at the same spot again and again, jumping back a few seconds each time.
    // If the last restart got no further, the rest of that file is skipped.
    const position = this.player.position;
    const last = this._brokeOff;
    const stuck = last != null && position != null && position < last + 2000;
    if (position != null && position < this.player.session.end - 5000 && !stuck) {
      this._brokeOff = position;
      this.player.play(this.camera, this.source, position, this.quality, position);
      return;
    }
    this._brokeOff = null;
    if (stuck) {
      const after = this.data.recordings.find((r) => r.start > position + 1000);
      if (after) this.player.play(this.camera, this.source, after.start, this.quality);
      return;
    }
    const next = this.data.recordings.find((r) => r.start > this.player.session.end - 1000);
    if (next) this.player.play(this.camera, this.source, next.start, this.quality);
  }

  /*
   * Previous/next event: an event in another recording file plays that file from its start,
   * so the moments recorded before the event are seen too. Inside the file playing now, the
   * jump goes to the event itself.
   */
  _jumpTo(ev, position) {
    const clip = this._recordingAt(ev.start);
    const current = this._recordingAt(position);
    // Long files (continuous recording) would start far before the event: those go to the event.
    if (!clip || clip === current || clip.start >= ev.start || ev.start - clip.start > EVENT_LEAD_MAX) {
      this.seek(new Date(ev.start));
      return;
    }
    this.seek(new Date(clip.start));
    this._eventCursor = { time: ev.start, clip };
  }

  _click(e) {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (!action) return;
    const position = this.player.position ?? Date.now();
    // Without events to show (none, or all kinds switched off), previous/next go by recording.
    const shown = this._shownEvents();
    const events = shown.length ? shown : this.data.recordings;
    // After a jump to the start of an event's recording, the next jump counts from that event,
    // not from the earlier start of its recording.
    const cursor = this._eventCursor;
    const from = cursor && position >= cursor.clip.start && position < cursor.time + 1000 ? cursor.time : position;
    switch (action) {
      case "toggle":
        if (this.video.paused) this.player.resume();
        else this.video.pause();
        break;
      case "sound":
        if (this.hasAudio === false) break;
        this.sound = this.video.muted;
        this.video.muted = !this.sound;
        if (this.video.paused) this.player.resume();
        break;
      case "back":
        this.seek(new Date(position - 10e3));
        break;
      case "forward":
        this.seek(new Date(position + 10e3));
        break;
      case "prev": {
        const prev = [...events].reverse().find((ev) => ev.start < from - 2000);
        if (prev) this._jumpTo(prev, position);
        break;
      }
      case "next": {
        const next = events.find((ev) => ev.start > from + 1000);
        if (next) this._jumpTo(next, position);
        break;
      }
      case "view-timeline":
      case "view-clips":
        this._setView(action.slice("view-".length));
        break;
      case "clip": {
        const clip = this._clips[Number(e.target.closest("[data-action]").dataset.index)];
        if (clip) this.seek(new Date(clip.start));
        break;
      }
      case "live":
        this.goLive();
        break;
      case "download":
        this._download(position);
        break;
      case "fullscreen": {
        const stage = this.root.querySelector(".stage");
        if (document.fullscreenElement) document.exitFullscreen();
        else if (stage.requestFullscreen) stage.requestFullscreen();
        else if (this.video.webkitEnterFullscreen) this.video.webkitEnterFullscreen();
        break;
      }
      case "day-prev":
        this.setDay(new Date(this.day.getTime() - DAY / 2));
        break;
      case "day-next":
        if (this.day.getTime() + DAY <= Date.now()) this.setDay(new Date(this.day.getTime() + DAY * 1.5));
        break;
      case "today":
        this.setDay(new Date());
        break;
      case "calendar":
        this.calendar.toggle();
        break;
      case "mark-all-read":
        this._markAll();
        break;
      case "zoom-in":
        this.timeline.zoom(1);
        break;
      case "zoom-out":
        this.timeline.zoom(-1);
        break;
      default:
    }
  }

  async _download(position) {
    const recording =
      this.data.recordings.find((r) => r.start <= position && r.end > position) ||
      this.data.recordings.find((r) => r.start > position);
    if (!recording || !this.camera) return;
    // Compatible playback means the browser lacks the codec: download as H.264 then too.
    const format = this.player.level === "compatible" ? "h264" : "mp4";
    const path = `/api/${DOMAIN}/recording/${this.camera.entry_id}/${this.camera.channel_id}/${this.source}/${format}/${recording.token}?download=1`;
    const signed = await this.hass.callWS({ type: "auth/sign_path", path, expires: 3600 });
    const link = document.createElement("a");
    link.href = signed.path;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  // ---- state shown

  busy(on) {
    this._busy = on;
    this._overlay();
  }

  showError(code) {
    this._busy = false;
    this._error = this.t.ui(code);
    this._overlay();
  }

  showMessage(text) {
    this._error = null;
    this._message = text;
    this._overlay();
  }

  clearMessage() {
    this._error = null;
    this._message = null;
    this._overlay();
  }

  _overlay() {
    const overlay = this.root.querySelector(".overlay");
    const text = this._error || this._message;
    if (text) {
      // Messages can carry card config values, so they never go through innerHTML.
      const box = document.createElement("div");
      box.className = this._error ? "message error" : "message";
      box.textContent = text;
      overlay.replaceChildren(box);
    } else if (this._busy) overlay.innerHTML = `<div class="spinner"></div>`;
    else overlay.replaceChildren();
  }

  update() {
    const live = this.mode === "live";
    const t = this.t.ui;
    this.root.querySelector(".live-button").classList.toggle("active", live);
    const toggle = this.root.querySelector('[data-action="toggle"]');
    toggle.innerHTML = this.video.paused ? ICONS.play : ICONS.pause;
    toggle.title = t(this.video.paused ? "play" : "pause");
    const recorded = Boolean(this.camera && this.camera.sources.length);
    for (const action of ["prev", "back", "next", "download"]) {
      this.root.querySelector(`[data-action="${action}"]`).disabled = !recorded;
    }
    this.root.querySelector('[data-action="forward"]').disabled = !recorded || live;
    const speed = this.root.querySelector(".speed");
    speed.hidden = live || !recorded;
    speed.value = String(this.player.speed);
    const badges = [];
    if (live) badges.push(`<span class="badge live">${t("live")}</span>`);
    if (this.player.level) {
      const level = t(this.player.level === "low" ? "low" : this.player.level);
      badges.push(`<span class="badge">${this.quality === "auto" ? `${t("auto")} · ` : ""}${level}</span>`);
    }
    this.root.querySelector(".badges").innerHTML = badges.join("");
    this._hint();
  }

  // Seek bar of the clip playing now, with the position and length in it. A clip is the same as
  // in the clip list: recorders list one recording in several parts (one per trigger), and a
  // bar of the part alone grew in steps while the clip played. Continuous recording would join
  // into a whole day, so it keeps the bar of its file.
  _clipBar(position) {
    const bar = this.root.querySelector(".clip");
    const recording = this.mode === "playback" && position != null ? this._recordingAt(position) : null;
    const clip =
      recording && recording.kind !== "continuous"
        ? this._joinedClips().find((c) => c.start <= position && position < c.end) || recording
        : recording;
    this._clip = clip;
    bar.hidden = !clip;
    if (!clip || this._clipDrag) return;
    const range = bar.querySelector("input");
    const length = (clip.end - clip.start) / 1000;
    if (range.max !== String(length)) range.max = String(length);
    range.value = String(Math.min(length, Math.max(0, (position - clip.start) / 1000)));
    bar.querySelector(".clip-at").textContent = duration(position - clip.start);
    bar.querySelector(".clip-length").textContent = duration(clip.end - clip.start);
  }

  _frame() {
    if (this.view === "clips") this._markClip(this.player.position ?? this.timeline.playhead);
    const target = this._target;
    // Reading the position also sets the player's base, which a pending seek waits for.
    // Without this read, a seek from a timeline click never finished and the playhead stuck.
    const position = this.player.position;
    if (target && (!target.done || this.player.base == null || this.video.seeking)) {
      this.timeline.playhead = target.time;
      this.timeline.draw();
      return;
    }
    this._target = null;
    this._clipBar(position);
    if (position != null && this.camera) {
      if (this.mode === "playback" || isoDay(this.day) === isoDay(new Date())) {
        this.timeline.playhead = position;
        // Follow the playhead when zoomed in, unless the user has panned or zoomed by hand.
        if (this.timeline.span < DAY && this.timeline.followPlayhead) {
          const { viewStart, span } = this.timeline;
          if (position < viewStart || position > viewStart + span * 0.9) this.timeline.center(position);
        }
        this.timeline.draw();
      }
      if (this.mode === "playback" && this.player.session) this._playedThrough(position);
      if (this.mode === "playback" && this.player.session && position >= this.player.session.end - 500 && !this.video.paused) {
        this.video.pause();
        this.onEnded();
      }
    }
  }
}

/* -------------------------------------------------------------------- panel */

class HikvisionBridgePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.cameras = [];
    this.selected = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._build();
    else if (this.viewer) this.viewer.setHass(hass);
    this._thumbs();
  }

  get hass() {
    return this._hass;
  }

  set narrow(value) {
    this._narrow = value;
    this.toggleAttribute("narrow", Boolean(value));
  }

  // Title and title bar as set in the integration options.
  set panel(panel) {
    this._panel = panel;
    this._applyConfig();
  }

  _applyConfig() {
    const config = (this._panel && this._panel.config) || {};
    this.toggleAttribute("no-header", config.header === false);
    const title = this.shadowRoot.querySelector("header h1");
    if (title) title.textContent = config.title || "Hikvision";
  }

  connectedCallback() {
    if (this._hass && !this.viewer) this._build();
  }

  disconnectedCallback() {
    if (this.viewer) this.viewer.destroy();
    this.viewer = null;
    clearInterval(this._thumbTimer);
  }

  _build() {
    this.shadowRoot.innerHTML = `
      <style>
        ${STYLE}
        :host { display: block; height: 100%; background: var(--hb-bg); }
        .layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); grid-template-rows: 56px minmax(0, 1fr); height: 100vh; height: 100dvh; }
        header { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; padding: 0 12px; background: var(--app-header-background-color, var(--hb-surface)); color: var(--app-header-text-color, var(--hb-text)); border-bottom: 1px solid var(--hb-line); }
        header h1 { font-size: 20px; font-weight: 400; margin: 0 0 0 4px; }
        header .menu { display: none; }
        :host([narrow]) header .menu { display: inline-flex; }
        aside { overflow-y: auto; border-right: 1px solid var(--hb-line); background: var(--hb-surface); }
        aside h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--hb-muted); margin: 14px 16px 8px; font-weight: 500; }
        .cam { display: flex; gap: 10px; align-items: center; width: 100%; padding: 8px 12px; background: none; border: 0; border-left: 3px solid transparent; cursor: pointer; text-align: left; }
        .cam:hover { background: color-mix(in srgb, var(--hb-text) 6%, transparent); }
        .cam.active { border-left-color: var(--hb-accent); background: color-mix(in srgb, var(--hb-accent) 12%, transparent); }
        .cam img, .cam .noimg { width: 96px; height: 54px; object-fit: cover; border-radius: 6px; background: #000; flex: none; }
        .cam .name { font-size: 14px; }
        .cam .sub { font-size: 12px; color: var(--hb-muted); }
        .cam.offline { opacity: .5; }
        main { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
        .viewer { display: flex; flex-direction: column; height: 100%; min-height: 0; }
        .stage { flex: 1; }
        .controls, .day-bar { background: var(--hb-surface); }
        .controls { border-top: 1px solid var(--hb-line); }
        .day-bar { display: flex; align-items: center; gap: 4px; padding: 0 8px; }
        .day-bar .spacer { flex: 1; }
        .today { background: none; border: 1px solid var(--hb-line); border-radius: 16px; padding: 4px 12px; cursor: pointer; margin-left: 4px; min-height: 32px; }
        .unread { font-size: 13px; color: var(--hb-muted); white-space: nowrap; font-variant-numeric: tabular-nums; margin-right: 4px; }
        .mark-read { background: none; border: 1px solid var(--hb-line); border-radius: 16px; padding: 4px 12px; cursor: pointer; min-height: 32px; color: inherit; font: inherit; font-size: 13px; }
        .timeline { height: 84px; background: var(--hb-surface); padding: 0 12px; }
        .legend, .hint { background: var(--hb-surface); }
        .empty { padding: 24px; color: var(--hb-muted); }
        header .spacer { flex: 1; }
        .aside-head { display: flex; align-items: center; }
        .aside-head h2 { flex: 1; }
        .aside-head .layout-toggle { display: none; margin-right: 4px; }
        /* Without the title bar: the layout toggle sits by the camera list. On narrow
           screens the bar stays, it holds the button that opens the sidebar. */
        :host([no-header]:not([narrow])) header { display: none; }
        :host([no-header]:not([narrow])) .layout { grid-template-rows: minmax(0, 1fr); }
        :host([no-header]:not([narrow])) .aside-head .layout-toggle { display: inline-flex; }
        :host([no-header][cams-top]:not([narrow])) .layout { grid-template-rows: auto minmax(0, 1fr); }
        :host([no-header][cams-top]:not([narrow])) aside { display: flex; align-items: center; }
        :host([no-header][cams-top]:not([narrow])) .list { flex: 1; min-width: 0; overflow-x: auto; }
        :host([no-header][cams-top]:not([narrow])) .aside-head { order: 2; position: sticky; right: 0; }
        /* Cameras above the video, in a row: chosen, or on narrow screens. */
        :host([cams-top]) .layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: 56px auto minmax(0, 1fr); }
        :host([cams-top]) aside { border-right: 0; border-bottom: 1px solid var(--hb-line); overflow-x: auto; overflow-y: hidden; }
        :host([cams-top]) aside h2 { display: none; }
        :host([cams-top]) .list { display: flex; }
        :host([cams-top]) .cam { flex-direction: column; width: auto; min-width: 120px; border-left: 0; border-bottom: 3px solid transparent; padding: 8px; text-align: center; }
        :host([cams-top]) .cam.active { border-bottom-color: var(--hb-accent); }
        :host([cams-top]) .cam .sub { display: none; }
        @media (max-width: 860px) {
          .layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: 48px auto minmax(0, 1fr); }
          main { overflow-y: auto; }
          header { padding: 0 4px; }
          header .layout-toggle { display: none; }
          :host([no-header]) header { display: flex; }
          :host([no-header]) .layout { grid-template-rows: 48px auto minmax(0, 1fr); }
          :host([no-header]) aside { display: block; }
          :host([no-header]) .aside-head .layout-toggle { display: none; }
          aside { border-right: 0; border-bottom: 1px solid var(--hb-line); overflow-x: auto; overflow-y: hidden; }
          aside h2 { display: none; }
          .list { display: flex; }
          .cam { flex-direction: column; width: auto; min-width: 96px; border-left: 0; border-bottom: 3px solid transparent; padding: 6px; gap: 4px; text-align: center; }
          .cam img, .cam .noimg { width: 80px; height: 45px; }
          .cam .name { font-size: 12px; }
          .cam.active { border-bottom-color: var(--hb-accent); }
          .cam .sub { display: none; }
          .stage { min-height: 200px; }
          .timeline { height: 72px; padding: 0 6px; }
          .clips { height: 170px; padding: 4px 6px 8px; }
          .day-bar { padding: 0 2px; }
        }
        @media (max-width: 860px) and (orientation: portrait) {
          .viewer { height: auto; }
          .stage { flex: none; aspect-ratio: 16 / 9; }
          /* The page scrolls: the clip list keeps several rows of cards visible below the player. */
          .clips { height: auto; min-height: 55vh; }
        }
      </style>
      <div class="layout">
        <header>
          <button class="icon-button menu" title="Menu">${ICONS.menu}</button>
          <h1>Hikvision</h1>
          <span class="spacer"></span>
          <button class="icon-button layout-toggle"></button>
        </header>
        <aside><div class="aside-head"><h2></h2><button class="icon-button layout-toggle"></button></div><div class="list"></div></aside>
        <main></main>
      </div>`;
    this.shadowRoot.querySelector(".menu").addEventListener("click", () => {
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    });
    this.viewer = new Viewer(this.shadowRoot.querySelector("main"), this._hass);
    this.texts = makeTranslator({});
    this._camsTop(this._stored("hikvision-bridge-cams") === "top");
    this._applyConfig();
    this.shadowRoot.querySelectorAll(".layout-toggle").forEach((toggle) =>
      toggle.addEventListener("click", () => {
        const top = !this.hasAttribute("cams-top");
        this._camsTop(top);
        try {
          localStorage.setItem("hikvision-bridge-cams", top ? "top" : "left");
        } catch (_err) {
          /* private mode */
        }
      }),
    );
    loadTexts(this._hass).then((r) => {
      this.texts = makeTranslator(r);
      this.shadowRoot.querySelector("aside h2").textContent = this.texts.ui("cameras");
      this._camsTop(this.hasAttribute("cams-top"));
    });
    this._load();
    this._thumbTimer = setInterval(() => this._thumbs(true), 10e3);
  }

  async _load() {
    let cameras = [];
    try {
      ({ cameras } = await this._hass.callWS({ type: `${DOMAIN}/cameras` }));
    } catch (_err) {
      /* integration not loaded */
    }
    this.cameras = cameras;
    const list = this.shadowRoot.querySelector(".list");
    if (!cameras.length) {
      list.innerHTML = `<div class="empty">${TEXT.no_cameras}</div>`;
      this.viewer.setCamera(null);
      return;
    }
    const devices = new Set(cameras.map((c) => c.entry_id)).size;
    list.innerHTML = cameras
      .map(
        (c, i) => `
        <button class="cam${c.online ? "" : " offline"}" data-index="${i}">
          <img alt="" loading="lazy">
          <span><div class="name"></div><div class="sub"></div></span>
        </button>`,
      )
      .join("");
    list.querySelectorAll(".cam").forEach((button, i) => {
      const camera = cameras[i];
      button.querySelector(".name").textContent = camera.name;
      button.querySelector(".sub").textContent = [devices > 1 ? camera.device : null, camera.resolution].filter(Boolean).join(" · ");
      button.addEventListener("click", () => this._select(i));
    });
    const saved = this._stored("hikvision-bridge-camera");
    const index = Math.max(0, cameras.findIndex((c) => `${c.entry_id}/${c.channel_id}` === saved));
    this._select(index);
    this._thumbs(true);
  }

  _stored(key) {
    try {
      return localStorage.getItem(key);
    } catch (_err) {
      return null;
    }
  }

  // Cameras in a row above the video, or in a column beside it.
  _camsTop(top) {
    this.toggleAttribute("cams-top", top);
    const label = this.texts.ui(top ? "cameras_left" : "cameras_top");
    this.shadowRoot.querySelectorAll(".layout-toggle").forEach((button) => {
      button.innerHTML = top ? ICONS.camsLeft : ICONS.camsTop;
      button.title = label;
      button.setAttribute("aria-label", label);
    });
  }

  _select(index) {
    const camera = this.cameras[index];
    this.shadowRoot.querySelectorAll(".cam").forEach((b, i) => b.classList.toggle("active", i === index));
    try {
      localStorage.setItem("hikvision-bridge-camera", `${camera.entry_id}/${camera.channel_id}`);
    } catch (_err) {
      /* private mode */
    }
    this.viewer.setCamera(camera);
  }

  _thumbs(refresh = false) {
    if (!this.shadowRoot || !this.cameras.length) return;
    this.shadowRoot.querySelectorAll(".cam").forEach((button, i) => {
      const camera = this.cameras[i];
      const entity = camera.live.low || camera.live.high;
      const state = entity && this._hass.states[entity];
      const img = button.querySelector("img");
      if (!state || !state.attributes.entity_picture) return;
      if (!img.src || refresh) img.src = `${state.attributes.entity_picture}&t=${Math.floor(Date.now() / 10e3)}`;
    });
  }
}

/* --------------------------------------------------------------------- card */

class HikvisionBridgeCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  static getStubConfig(hass) {
    const camera = Object.keys(hass.states).find(
      (id) => id.startsWith("camera.") && hass.entities?.[id]?.platform === DOMAIN,
    );
    return { camera_entity: camera || "" };
  }

  setConfig(config) {
    if (!config || !config.camera_entity) throw new Error("camera_entity is required");
    this.config = config;
    if (this.viewer) this._pick();
  }

  getCardSize() {
    return 7;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: 7, min_rows: 5 };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._build();
    else if (this.viewer) this.viewer.setHass(hass);
  }

  disconnectedCallback() {
    if (this.viewer) this.viewer.destroy();
    this.viewer = null;
  }

  connectedCallback() {
    if (this._hass && !this.viewer) this._build();
  }

  _build() {
    this.shadowRoot.innerHTML = `
      <style>
        ${STYLE}
        ha-card, .card { overflow: hidden; height: 100%; display: flex; flex-direction: column; }
        .viewer { display: flex; flex-direction: column; flex: 1; min-height: 0; }
        .stage { aspect-ratio: 16 / 9; flex: 1; }
        .stage.armed::after { content: ""; position: absolute; inset: 0; border: 2px solid var(--hb-accent); pointer-events: none; }
        .day-bar { display: flex; align-items: center; gap: 2px; padding: 0 4px; }
        .day-bar .spacer { flex: 1; }
        .today { background: none; border: 1px solid var(--hb-line); border-radius: 16px; padding: 2px 10px; cursor: pointer; }
        .unread { font-size: 12px; color: var(--hb-muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
        .mark-read { background: none; border: 1px solid var(--hb-line); border-radius: 16px; padding: 2px 10px; cursor: pointer; color: inherit; font: inherit; font-size: 12px; }
        .timeline { height: 64px; padding: 0 8px; }
      </style>
      <ha-card><div class="card"></div></ha-card>`;
    this.viewer = new Viewer(this.shadowRoot.querySelector(".card"), this._hass, { compact: true });
    this._pick();
  }

  async _pick() {
    let cameras = [];
    try {
      // Cameras hidden from the panel still show on dashboards.
      ({ cameras } = await this._hass.callWS({ type: `${DOMAIN}/cameras`, include_hidden: true }));
    } catch (_err) {
      /* integration not loaded */
    }
    const wanted = this.config.camera_entity;
    const camera = cameras.find((c) => c.live.high === wanted || c.live.low === wanted);
    if (camera) this.viewer.setCamera(camera);
    else this.viewer.showMessage(`${wanted}: ${TEXT.live_unavailable}`);
  }
}

if (!customElements.get("hikvision-bridge-panel")) customElements.define("hikvision-bridge-panel", HikvisionBridgePanel);
if (!customElements.get("hikvision-bridge-card")) {
  customElements.define("hikvision-bridge-card", HikvisionBridgeCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "hikvision-bridge-card",
    name: "Hikvision recordings",
    description: "Live view and recordings of a Hikvision Bridge camera with a timeline.",
    preview: false,
  });
}
