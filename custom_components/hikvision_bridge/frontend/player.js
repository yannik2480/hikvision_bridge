/*
 * The live and recorded video of one camera, shared by the sidebar panel, the recordings card
 * (hikvision-bridge.js) and the live card (hikvision-bridge-live-card.js). It is a module of its
 * own so both files play video the same way: WebRTC through Home Assistant's camera signalling,
 * HLS when that is missing, and the integration's converted session for sound Home Assistant
 * drops.
 */

export const DOMAIN = "hikvision_bridge";
const BASE = new URL(".", import.meta.url).pathname;

const HEVC = 'video/mp4; codecs="hvc1.1.6.L153.B0"';
// Live WebRTC gives up after this long and the view falls back to HLS.
const RTC_START_MS = 6000;

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

/*
 * One camera's video: live (Home Assistant's camera stream) or recordings (a playback
 * session of the integration). Quality "auto" starts low and goes up while playback is
 * smooth and the connection fast; stalls bring it down again.
 */
export class Player {
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
      // A sound format Home Assistant's HLS drops goes straight to the converted picture: the
      // silent one would only flash on screen before it.
      if (needsSoundConversion(camera, shown) && (await this._liveSound(token))) {
        if (token === this.token) this.host.update();
        return;
      }
      if (token !== this.token) return;
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

  async play(camera, source, time, quality) {
    this.mode = "playback";
    this.quality = quality;
    this.camera = camera;
    this.source = source;
    let level = quality === "auto" ? "original" : quality;
    if (quality === "auto" && camera.codec && camera.codec.includes("265") && !this.canHevc) level = "compatible";
    this._applySpeed();
    await this._play(time, level);
  }

  async _play(time, level) {
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
