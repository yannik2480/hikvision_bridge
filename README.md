<p align="center">
  <img src="https://raw.githubusercontent.com/yannik2480/hikvision_bridge/main/custom_components/hikvision_bridge/brand/icon.png" alt="Hikvision Bridge logo" width="128">
</p>

<h1 align="center">Hikvision Bridge</h1>

<p align="center">
  Local Home Assistant integration for Hikvision cameras, recorders (NVR/DVR) and video intercoms over ISAPI.<br>
  Push events, live streams, recordings with a timeline viewer, detection switches and image settings,<br>
  with optional Hik-Connect cloud status and alarm areas.
</p>

<p align="center">
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS custom repository"></a>
  <img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fyannik2480%2Fhikvision_bridge%2Fmain%2Fcustom_components%2Fhikvision_bridge%2Fmanifest.json&query=%24.version&label=version&color=10306b" alt="Version">
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.9%2B-14a898.svg" alt="Home Assistant 2026.9 or newer">
  <img src="https://img.shields.io/badge/languages-30-lightgrey.svg" alt="30 languages">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="Apache 2.0 license">
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=yannik2480&repository=hikvision_bridge&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open in HACS"></a>
  <a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=hikvision_bridge"><img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Add integration"></a>
</p>

---

## Why

* **Everything stays local.** Events arrive by push over the device's alert stream; nothing is polled once per second.
* **Your devices are not reconfigured behind your back.** The integration reads; every change is either an entity you switch or a repair that asks first.
* **Recorders and cameras together.** Cameras behind an NVR become channel devices, and can additionally be reached directly for live view and image settings.
* **Careful with logins.** Hikvision locks users after failed logins, so a rejected password is never retried in a loop.

<p align="center">
  <img src="https://raw.githubusercontent.com/yannik2480/hikvision_bridge/main/images/architecture.svg" alt="Home Assistant talks ISAPI to the recorder; cameras can also be reached directly; Hik-Connect is optional" width="820">
</p>

## What you get

| Area | Entities / functions |
| --- | --- |
| Detections | Binary sensor per channel and detection kind (motion, line crossing, intrusion, region entrance/exit, tamper, video loss, scene change, defocus, audio exception); optional person/vehicle sensors |
| Event entities | One per channel, fires on every smart detection (good for automations that must not miss repeated triggers) |
| Plate recognition (ANPR) | On channels that report plate events: last plate as a sensor (country, direction, confidence, plate and vehicle colour, allow/block list as attributes), vehicle picture and plate close-up (close-up disabled by default); the plate details are also on the event entity and the `hikvision_bridge_event` bus event |
| Switches | Detections on/off including video loss on recorders (only the `enabled` flag is written back, everything else stays byte-identical); alarm outputs; arm/disarm alarm inputs |
| Image settings | Day/night, supplement light, colour, WDR and more per channel (see below) |
| Deterrence and audio | Strobe light duration and frequency, siren sound, volume and repeats, speaker and microphone volume (cameras that have them) |
| Cameras | Main/sub stream per channel, snapshots, codec and resolution as attributes |
| PTZ | Pan/tilt/zoom step buttons and a preset select per PTZ channel (also through a recorder); motorised zoom lenses get the zoom buttons only. Park action: switch, idle time, action type (preset, patrol, pattern, scans) and its number, chosen from the stored presets, patrols 1–8 or patterns 1–4 (none for scans) |
| Recorder | One HA device per channel below the recorder, channel connected, recording per channel, storage status/free/capacity, alarm inputs |
| Health | Event connection, event delay, stream encryption, IP address, repairs for misconfigured devices (see below); uptime/last boot, CPU, memory and reboot count (diagnostic, disabled by default; firmware that reports a timestamp instead of a count shows it as the `reported_timestamp` attribute); memory card of directly connected cameras |
| Recordings | Recorder and memory-card recordings in the media browser, by day and kind, with thumbnails; seekable playback, or as MP4, as H.264 or streamed by the device; saving to the media folder (see below) |
| Panel and card | Sidebar viewer and dashboard card: camera list, live view, timeline of recordings and detections to scrub through, automatic quality ([details](#recordings-panel-and-card)) |
| Video Intercom & Doorbell | Remote door unlock button (Access Control relay pulse), call status sensor (`idle`, `ringing`, `on_call`), ringing binary sensor, and dedicated doorbell event entity with fast poll detection |
| Hik-Connect (optional) | Cloud connection per device, stream-encryption status, connection change events, alarm control panels for security areas/groups (arm away, arm home/silent, disarm), door opening for intercoms (unverified, see limits) |
| Actions | `add_center_links`, `isapi_request` (read-only unless confirmed), `ptz_move`, `ptz_preset`, `set_notification_host`, `search_recordings`, `download_recording`, `cloud_create_area`, `cloud_update_area`, `cloud_delete_area` |

<p align="center">
  <img src="https://raw.githubusercontent.com/yannik2480/hikvision_bridge/main/images/configuration.png" alt="Device page, Configuration: detection switches, alarm input, image settings, siren, strobe light and audio" width="300">
  &nbsp;
  <img src="https://raw.githubusercontent.com/yannik2480/hikvision_bridge/main/images/controls-sensors.png" alt="Device page, Controls and Sensors: alarm outputs, detections, streams and recording status" width="300">
</p>

### Image settings

Read from each camera's image capabilities, so a camera only gets what it actually supports. The
settings in **bold** are enabled by default; the others exist but start disabled (enable them in the
entity list when you need them).

| Group | Entities | Type |
| --- | --- | --- |
| Day/night | **Day/night mode** (day, night, auto, schedule), day/night sensitivity, day/night delay | select, number |
| Supplement light | **Supplement light** (smart/event, white light, IR, off), **white light brightness**, **IR light brightness**, light brightness control (auto/manual), white light duration | select, number |
| Colour and detail | **Brightness**, **contrast**, **saturation**, **sharpness** | number |
| Backlight | **WDR** (off, on, auto), WDR level, backlight compensation and area, highlight compensation and level | select, number, switch |
| Noise and haze | Noise reduction and level, defog and level | select, number |
| White balance | White balance mode, red and blue gain | select, number |
| Exposure | Exposure mode, shutter, gain limit, overexposure suppression | select, number, switch |
| Other | Power line frequency, image flip and direction, image stabilization, lens distortion correction | select, switch |
| Strobe light | **Strobe light duration**, **strobe light frequency** (high, medium, low, steady on) | number, select |
| Siren | **Siren sound** (names as the camera reports them), **siren volume**, siren repeats | select, number |
| Two-way audio | **Speaker volume**, **microphone volume** | number |

How it behaves:

* **Mode-dependent settings are unavailable while their mode is off.** White light brightness, for
  example, is only adjustable while the supplement light uses white light or smart mode.
* **One value per change.** A change reads the section (for example only the colour settings), changes one
  element, writes it back and reads it again to confirm. Other settings are left alone.
* **Conflicts are reported, not resolved.** If the camera refuses a combination (for example WDR together
  with backlight compensation), you get an error message and nothing else changes.
* **Behind a recorder** the settings go through the recorder, or straight to the camera when a direct
  camera connection exists. Siren and two-way audio need a direct camera connection.

### Recordings

*Media → Hikvision Bridge* lists the recordings of every recorder and of cameras with a memory card:
device → channel → day → kind (continuous, motion, alarm input, smart event, …) → recorded file.
For a recorder channel with a [direct camera connection](#cameras-behind-a-recorder) whose camera has a
memory card, the channel first asks which store to open: *Recorder* or *Memory card*. Days come from the
device's calendar; files show their first picture as thumbnail (days with up to 60 files).

Devices record into an MPEG program stream with H.265 or H.264 video; downloading always returns the
whole recorded file (often 10 minutes and several hundred MB on recorders). Home Assistant converts it
while it downloads, using its ffmpeg:

| Format | What happens | Use |
| --- | --- | --- |
| Seekable (default) | The file as HLS video on demand, made like the [panel's playback](#recordings-panel-and-card): video copied, audio to AAC | Shows the length of the file and seeks anywhere in it; H.265 needs a browser with hardware support |
| MP4 as recorded | Video copied into MP4, audio to AAC; almost no CPU | Plays at once; H.265 needs a browser with hardware support (Safari, Chrome/Edge on most current PCs) |
| H.264 | Decoded and encoded again, at most 1080p | Plays in every browser and on casting devices; slow on small machines, especially with 4K recordings |
| Stream from the device | The device plays the file over RTSP, Home Assistant serves it as HLS; falls back to MP4 when Home Assistant cannot set up the stream | Long continuous recordings; plays in real time, no seeking, restarts at the end |
| MPEG-TS as recorded | Video copied into MPEG-TS | Saving only |
| Original file | The device's bytes, unchanged (`.ps`, plays in VLC) | Saving only |

The first four are the option *Playback in the media browser*. The action
`hikvision_bridge.download_recording` saves all files of a period (up to one day, at most 50 files) to
`/media/hikvision_bridge/<device>/` in any of the formats and, except for the original file, cuts them
to the period. `hikvision_bridge.search_recordings` returns the list without downloading.

```yaml
action: hikvision_bridge.download_recording
data:
  device_id: <camera or channel device>
  start: "2026-10-22 08:00:00"
  end: "2026-10-22 08:15:00"
  format: mp4        # mp4, h264, ts, original
  source: auto       # auto (recorder first), recorder, card
```

**Encrypted recordings.** With *stream encryption* on (*Network → Platform Access*), the device encrypts
the video it records, and files recorded while it was on stay encrypted. The integration decrypts them
with the device's verification code, which it reads from the device. Many cameras do not report the
code; enter it in the integration options (*Verification code*) then. If no code fits, playback says so
instead of returning a broken file. The media browser mode *Stream from the device* cannot decrypt.

**Seekable playback.** The playlist lists the whole file in parts of 4 seconds, so the player knows its
length and seeks in it. Parts come from one playback session per camera, as in the panel: from the
download of the file where the device is fast enough to get there, else over RTSP from the wanted time
(a few seconds to start, then real time). A seek far from what is prepared starts the session again
there; the first seek into a file may take up to about 10 seconds on recorders that send files only
from their start, while the integration learns how far the download gets. Video carries the time
stamps of the file, so parts made by different sessions line up. Playback stops with the media
browser; the session ends after 45 seconds without requests.

The [Advanced Camera Card](https://github.com/dermotduffy/advanced-camera-card) plays these recordings
through its `folders` with the Home Assistant media browser as source. A channel's folder is
`media-source://hikvision_bridge/<config entry id>/<channel>/<recorder or card>`; day folders are named by
date (`2026-11-08`), files by their time (`08:32:15 - 11:26:52`). Days with recordings of several kinds
have one more level: *All* and one folder per kind.

```yaml
type: custom:advanced-camera-card
cameras:
  - camera_entity: camera.front_yard_main
folders:
  - type: ha
    title: Front yard recordings
    ha:
      path:
        - id: media-source://hikvision_bridge/<config entry id>/1/recorder
        - parsers:
            - type: date
              format: yyyy-MM-dd
```

When the sidebar player cannot download efficiently from the selected time, it falls
back to recorded RTSP playback. With a verification code configured on the local device
entry and a known H.264 codec, that fallback also uses the decryption relay, so DVR encryption can
stay enabled. The relay closes when playback stops, is replaced, or fails to start.
The media browser's separate *Stream from the device* mode is unchanged.

### Recordings panel and card

The sidebar entry **Hikvision** opens a viewer for all cameras: the camera list on the left, the
picture in the middle and a timeline of the day at the bottom. The timeline shows recordings coloured by
kind and the detections of the event sensors (from Home Assistant's history). Click or drag on it to
jump in time, scroll to zoom from 24 hours down to 15 minutes; the buttons step from event to event or
10 seconds back and forward. *Live* returns to the live picture. The download button saves the
recording that is playing.

The options *Show the Hikvision panel in the sidebar* and *Only administrators see the panel*
(see [Options](#options)) remove the entry or limit it to administrators.

Quality defaults to **Auto**:

* **Live** starts with the sub stream, which loads fastest, and switches to the main stream once the
  picture runs smoothly and the connection is fast enough; it goes back to the sub stream when the
  picture stalls. *Low* and *High* fix one stream.
* **Playback** plays the recording as recorded (*Original*). Recordings only exist in the main stream's
  quality, so there is no fast low-quality version of them. A browser without H.265 support gets
  *Compatible*, which converts to H.264 on the fly; on a Raspberry Pi this is slower than real time for
  4K recordings.

<p align="center">
  <img src="https://raw.githubusercontent.com/yannik2480/hikvision_bridge/main/images/panel-mockup.jpg" alt="Mockup of the Hikvision panel: camera list, live picture, controls and day timeline" width="820"><br>
  <sub><b>Mockup</b> (AI-generated illustration of the layout, not a screenshot; labels and picture are not real)</sub>
</p>

The same viewer is available as a dashboard card:

```yaml
type: custom:hikvision-bridge-card
camera_entity: camera.front_yard_main   # a camera entity of the channel
```

Playback downloads the recordings from the chosen moment on, decrypts them if needed (see
[encrypted recordings](#recordings)) and passes them to the browser as HLS. The download is much faster
than real time, so playback starts within about a second and seeking within the loaded part is
instant; it stays about a minute ahead of the player. Devices that do not offer such downloads play
over RTSP instead (real time, no decryption). A second viewer of the same camera's recordings replaces
the first.

## Installation

### HACS (recommended)

1. Click **Open in HACS** above, or open HACS and search for **Hikvision Bridge**.
2. Download **Hikvision Bridge** and restart Home Assistant.
3. Click **Add integration** above, or go to **Settings → Devices & services → Add integration → Hikvision Bridge**.

If the integration doesn't show up in the HACS search yet, add it as a custom repository:
HACS → three-dot menu → **Custom repositories** → `https://github.com/yannik2480/hikvision_bridge`
with category **Integration**.

### Manual

Copy `custom_components/hikvision_bridge` into your `config/custom_components/` folder and restart.

Requires Home Assistant 2026.9 or newer.

## Setup

Settings → Devices & services → **Add integration** → *Hikvision Bridge*, then choose
**Camera or recorder in the local network** or **Hik-Connect account**.

### Devices

| Field | Meaning |
| --- | --- |
| Host | IP address or host name of the camera or recorder |
| Port (optional) | Empty means 80 (HTTP) or 443 (HTTPS) |
| Connection | *Automatic* (default), *HTTPS* or *HTTP* |
| Verify HTTPS certificate | Turn off for self-signed certificates |
| Username / Password | A dedicated operator user is recommended, see [User rights](#user-rights) |

Setup only reads from the device. Then the device is added with all its channels.

* **Connection**: *Automatic* tries HTTPS first and uses HTTP only if HTTPS cannot connect at all.
  An authentication error is never retried with the other scheme.
* **Credentials per device**: every device has its own username and password. A recorder and its
  cameras may use different users.
* **Each device once**: entries are keyed by serial number, not by address. A changed IP is fixed with
  **Reconfigure** (three-dot menu of the entry); the integration refuses if the new address belongs to
  a different device.
* **Login failed**: when a device rejects the stored credentials, the integration stops trying and
  Home Assistant asks you to re-authenticate the entry. Enter the current username and password there.

### Cameras behind a recorder

Cameras connected to a recorder appear as channel devices of the recorder. If a camera is also
reachable directly (for example the recorder's PoE subnet is routed), add a **Direct camera connection**
to the recorder entry (*Add* on the recorder entry). It has its own address and credentials, or reuses the
recorder's, and is used for the live stream, image settings and detection switches, and optionally for
detections. The channel device is then listed under its connection.

| Field | Meaning |
| --- | --- |
| Recorder channel | The channel the camera is connected to (only channels without a direct connection) |
| Host | Address under which Home Assistant reaches the camera directly |
| Port, Connection, Verify HTTPS certificate | As for devices |
| RTSP port (optional) | Port of the camera's live stream. Empty uses the port above when the host is the recorder's own address (the recorder forwards a port to the camera, which carries RTSP as well), otherwise 554 |
| Use the recorder's username and password | Leave off if the camera has its own credentials |
| Username / Password | The camera's credentials |
| Take detections from | *Recorder* (default) or *Camera* |
| Take the live stream from | *Camera* (default) or *Recorder*: the recorder's own stream of this channel, for when the camera's RTSP stream is not reachable (for example behind the recorder's port forwarding). Settings, detections and snapshots still go to the camera |

A direct connection can be changed later with **Reconfigure** on the subentry, or removed again.

When you add such a camera as a normal device, the integration notices that a recorder already shows
it and asks:

* **Link to the recorder channel**: the camera becomes a direct connection of that channel (one device,
  stream directly from the camera).
* **Add as separate device**: the camera gets its own entry next to the recorder channel.

### Hik-Connect

Hik-Connect has no official third-party interface. The integration logs in, reads the device list,
status and the arming state of security areas every 30 seconds, and stores the password in Home
Assistant. It only changes something in the cloud when you arm, disarm or use an area action. Enter the account
(e-mail or phone number) and password. If Hik-Connect asks for a verification code, the flow shows the
image and asks for the code. If the password stops working, Home Assistant asks you to re-authenticate
and enter the new password (and a code, if needed). Cloud devices are shown as their own devices, placed below the
matching local device when there is one.

#### Security areas

Camera groups (security areas) of recorders appear as alarm control panels on the cloud device. They
support **Arm away** (defence mode 1), **Arm home / silent** (mode 2) and **Disarm** (mode 0). The
area's cameras are in the `camera_ids` attribute; the list is refreshed every 10 minutes.

The actions `hikvision_bridge.cloud_create_area`, `cloud_update_area` and `cloud_delete_area` manage
areas. Select the cloud device (or give its serial) and pass camera ids as `resource_ids`:

```yaml
action: hikvision_bridge.cloud_create_area
data:
  device_id: <Hik-Connect device>
  group_name: Ground floor
  resource_ids: ["<camera id>", "<camera id>"]
```

Hik-Connect has no call to change an area, so `cloud_update_area` deletes it and creates it again: the
area gets a new id (returned by the action) and a new panel entity. If creating fails, the old area is
restored. Deleted areas remove their panel.

<p align="center">
  <img src="https://raw.githubusercontent.com/yannik2480/hikvision_bridge/main/images/arming-area.png" alt="Cloud security area alarm control panel entity" width="400">
</p>

### Options

**Configure** on a device entry (a Hik-Connect entry only shows the two panel options):

| Option | Default | Meaning |
| --- | --- | --- |
| How events arrive | Event stream | *Event stream*: Home Assistant keeps a connection to the device's alert stream, no change on the device needed. *Webhook*: the device sends events to Home Assistant. *Both*: stream and webhook at the same time |
| Hold detections on for (seconds) | 5 | Many devices only report the start of a detection. The sensor stays on this long after the last report (1–300); each new report extends it |
| RTSP port (optional) | empty | Empty uses the port reported by the device, otherwise 554. Applies to the device's own streams; direct camera connections have their own RTSP port |
| RTSP transport | TCP | *TCP* is reliable; *UDP* can have less delay but shows artefacts on packet loss |
| Separate sensors for people and vehicles | off | Adds person and vehicle binary sensors for detections with a target filter |
| Playback in the media browser | Seekable | *Seekable*, *MP4 as recorded*, *Convert to H.264* or *Stream from the device*, see [Recordings](#recordings) |
| Recording times | Automatic | The clock the device gives its recording times on: most devices use their own clock, some older cameras real UTC. *Automatic* finds it out from recordings in progress and from the recordings events started |
| Show the Hikvision panel in the sidebar | on | Removes the sidebar entry when off; the dashboard card keeps working. Applies to all entries of the integration |
| Only administrators see the panel | off | Hides the sidebar entry from users without administrator rights. Applies to all entries of the integration |

When *Webhook* or *Both* is chosen, the next step shows the address `/api/webhook/<id>` where
notifications are accepted (local network only, POST or PUT). Point the device there in its web
interface (Network → Advanced → HTTP Listening / Alarm server) or with the action
`hikvision_bridge.set_notification_host`.

## Recommended device settings

The integration reads, it does not reconfigure your devices on its own. Repairs point you to settings
that keep events from arriving; fixes that change the device always ask first.

1. **Notify surveillance center** must be ticked in the linkage of each detection you want in Home
   Assistant (*Event → … → Linkage Method*). Without it the device detects but never reports.
   The repair *“… is not reported”* or the action `add_center_links` can tick it for you (preview first).
2. **Draw the line or area**: a line crossing or intrusion detection without a drawn line/area never triggers.
3. **Hik-Connect stream encryption** (*Network → Platform Access*): encrypted local H.264 live
   video can be decoded without changing this setting. Enter the device's verification code
   in the local camera or recorder entry's options. The integration uses a token-protected
   RTSP relay on loopback, decrypts marked video units and preserves their timestamps;
   Home Assistant's normal player handles playback without transcoding. The relay uses TCP
   and supports video only; encrypted H.265 and audio are not supported. Direct camera
   connections keep their existing stream path. Only an explicitly configured verification
   code and a known H.264 codec select the relay. *Try the live stream even with encryption
   on* alone keeps the normal source URL; H.265 and unknown codecs do too.

   **Older devices that do not report encryption status:** entering a verification code
   also opts into the relay for known H.264 streams when that status is missing. An explicit
   *encryption off* response keeps the normal source URL. Once running, the relay checks the
   video packets: only units carrying Hikvision's encryption marker are decrypted; unmarked
   video passes through unchanged.
4. **Recorder motion target filter**: some recorders report motion only for people/vehicles. The repair names the filter.
5. **Time**: configure NTP; event times are corrected, recordings are not.
6. **Digest authentication** (*System → Security → Authentication*) or HTTPS, so passwords are not sent in clear text.

### User rights

Use a dedicated user per device (an *Operator* is enough). Do not share the user with another
integration: some devices invalidate a login when another client of the same user logs in.

| Function | Remote right needed |
| --- | --- |
| Discovery, sensors, events | Remote: Parameters settings (read), Notify surveillance center / alarm |
| Live view, snapshots | Remote: Live view |
| Detection switches, image settings, alarm outputs, repairs that tick linkage | Remote: Parameters settings |
| PTZ buttons, preset select and actions | Remote: PTZ control |
| Park action | Remote: PTZ control and Remote: Parameters settings |
| Recordings: browse, play, save | Remote: Playback/Download (on the camera too when its memory card is used) |
| Restart button (disabled by default) | Remote: Shutdown/Reboot |

## Example automations

Replace the entity ids with your own.

**Turn on the white light when someone crosses the line at night**

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.front_yard_line_crossing
    to: "on"
conditions:
  - condition: state
    entity_id: sun.sun
    state: below_horizon
actions:
  - action: select.select_option
    target:
      entity_id: select.front_yard_supplement_light
    data:
      option: colorvuwhitelight
```

**Force night mode while the house is armed**

```yaml
triggers:
  - trigger: state
    entity_id: alarm_control_panel.home
    to: armed_away
actions:
  - action: select.select_option
    target:
      entity_id: select.front_yard_day_night_mode
    data:
      option: night
```

**React to every smart detection, also repeated ones**

```yaml
triggers:
  - trigger: state
    entity_id: event.front_yard_detection
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'intrusion' }}"
actions:
  - action: notify.notify
    data:
      message: "Intrusion in the front yard"
```

**Open the gate for plates on the camera's allow list**

```yaml
triggers:
  - trigger: state
    entity_id: event.driveway_detection
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.event_type == 'anpr'
         and trigger.to_state.attributes.list == 'allow' }}
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.driveway_alarm_output_1
```

## Tested devices

| Type | Model | Firmware |
| --- | --- | --- |
| NVR | DS-7608NXI-I2/8P/S | V5.04.066 |
| Camera | DS-2CD2387G3-LIS2UY/SL (ColorVu G3, strobe light and speaker) | V5.8.32 |
| Camera | DS-2CD2T166G3-IS2UY/SL (G3, strobe light and speaker) | V5.8.3 |
| Camera | DS-2CD2187G3-LIS2UY (ColorVu G3) | V5.8.40 |
| Camera | DS-2CD20126G3-IUY (G3) | V5.8.3 |
| Camera | DS-2CD2387G2P-LSU/SL (ColorVu G2) | V5.7.20 |
| Camera | DS-2CD2T87G2-L (ColorVu G2) | V5.7.23 |
| Camera | DS-2DE5425IW-AE(T5)(B) | V5.8.13 build 260402 |
| Camera | DS-2CD2023G2-IU | V5.7.0 build 241023 |
| Camera | DS-I253M(C) | V5.7.3 build 260403 |
| Doorbell | DS-KV6113-PE1(C) (Villa Door Station) | V3.7.0 |

Other ISAPI devices should work; please report models that do or do not.

## Troubleshooting

* **“Login locked”**: Hikvision devices lock a user/IP after several failed logins. The integration
  stops at the first rejection, never retries, and waits for the lock to expire. Correct the password via the reauthentication notice.
* **No events**: check the *Event connection* sensor and the repairs; most often the linkage
  *Notify surveillance center* is missing or no area is drawn.
* **Camera shows no video, snapshots work**: with stream encryption enabled, enter the
  verification code in the local device's options and use an H.264 stream (see above).
  A rejected live-stream password or verification code stops retries until the entry is
  reloaded; correct the corresponding credentials/options first.
* **Main stream does not play in the browser**: 4K H.265 streams need a browser with H.265 support; use the sub stream otherwise.
* **A recording does not play**: *“The recording is encrypted and the verification code does not fit”*
  means stream encryption with an unknown code; enter the code in the options (see [Recordings](#recordings)). A black or missing picture with H.265 means the browser cannot decode it;
  choose *Convert to H.264* in the options. Converting needs Home Assistant's ffmpeg.
* **Recordings off by the time zone**: the timeline shows recordings hours away from when they were made,
  or a click plays a different time. Set *Recording times* in the options to the other clock.
* **Events late**: compare the *Event delay* sensor; large values usually mean a wrong device clock.
* **A setting is unavailable**: it belongs to a mode that is not active (see image settings).
* **Diagnostics**: *Download diagnostics* on the entry. Passwords, usernames, serial numbers, MAC
  addresses, IP addresses, webhook ids and cloud sessions are removed.

For a bug report, enable debug logging for a while:

```yaml
logger:
  logs:
    custom_components.hikvision_bridge: debug
```

Logged requests never contain passwords; still check the log before attaching it.

## Known limits

* Hik-Connect is undocumented and may change; errors show as *cloud request failed* rather than crashes.
* Door opening for intercoms through Hik-Connect is implemented but not yet verified on real hardware.
* Hik-Connect areas: changing an area recreates it with a new id (see [Security areas](#security-areas)).
* Session login (cookie based) is not implemented; Digest and Basic authentication are.
* Holiday schedules are not exposed.
* Encrypted live video is supported for local H.264 RTSP/TCP streams only. Cloud-only
  Hik-Connect live streams, encrypted H.265 and live audio decryption are not supported.
* In the media browser, only *Seekable* playback seeks; *MP4 as recorded* and *Convert to H.264* start at the beginning of the recorded file. *Seekable* plays recordings as recorded (no conversion to H.264).
* Cameras play one recording at a time; recordings exist only in main stream quality.
* Plate recognition (ANPR) follows Hikvision's documented notification format but has not been verified on a real plate camera yet. Plate entities appear when the device lists `ANPR` or `vehicleDetection` among a channel's events; plate events from other channels still reach the bus event.

## Encrypted live video implementation

The local relay opens on demand and closes on entry unload. Its URL contains a random
256-bit path token rather than the DVR credentials or verification code; it listens only
on `127.0.0.1` in Home Assistant's network namespace. Decrypted video is available inside
that namespace and is served to viewers through Home Assistant's normal authentication.
Other processes sharing that namespace are not isolated by loopback alone.

RTSP messages and fragmented video units have size limits. Incomplete fragmented units
are dropped, and rejected credentials stop further login attempts for that relay until
reload. A Digest challenge marked `stale=true` is retried once with the fresh nonce per
request before it is treated as a rejection. Advertised stream URLs are matched by the
configured stream path, so an internal address or omitted port does not bypass the relay.
If SDP reports another codec, its SDP and RTP pass through without decryption. The decryption sanity check is not cryptographic authentication of the footage.
No cloud service, vendor SDK or additional runtime dependency is required.

The H.264 RTP encryption format was informed by the MIT-licensed protocol notes in
[Sentinel Eye](https://github.com/dsameendra/sentinel-eye/blob/main/tools/NOTES.md) and
[homebridge-hikvision-dvr](https://github.com/matthewdeaves/homebridge-hikvision-dvr/blob/main/lib/hikcrypt.js).
Neither reference application is installed or executed.

Hardware validation: DS-7104HQHI-K1, V3.5.37 build 180928, with encryption enabled;
main stream H.264 1920×1080 and sub stream H.264 704×480. Browser playback through Home
Assistant was also confirmed. Encrypted H.265 and other recorder encryption variants
have not been verified.

## Development

Home Assistant does not run on Windows, so tests run in a container:

```sh
sh scripts/test.sh
```

Tests use simulated devices; no test talks to real hardware.

## Languages

The interface is translated into 30 languages: Arabic, Bengali, Chinese (Simplified), Czech, Danish,
Dutch, English, Finnish, French, German, Greek, Hindi, Hungarian, Indonesian, Italian, Japanese, Korean,
Norwegian Bokmål, Persian, Polish, Portuguese (Portugal and Brazil), Romanian, Russian, Spanish, Swedish,
Turkish, Ukrainian, Urdu and Vietnamese. Home Assistant picks the language set in your profile. Apart
from English and German the texts are machine-assisted; corrections are welcome as pull requests.

## License

[Apache License 2.0](https://github.com/yannik2480/hikvision_bridge/blob/main/LICENSE). The panel includes [hls.js](https://github.com/video-dev/hls.js) (Apache License 2.0, see [`hls.js.LICENSE.txt`](https://github.com/yannik2480/hikvision_bridge/blob/main/custom_components/hikvision_bridge/frontend/hls.js.LICENSE.txt)).
