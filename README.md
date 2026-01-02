# Audio Pro (LinkPlay) – Home Assistant Integration (HACS)

This is a custom Home Assistant integration for **Audio Pro speakers** based on the **LinkPlay/WiiM** platform.

It aims to mirror the **WiiM / Audio Pro mobile app behaviour as closely as possible**, using:
- **UPnP SOAP** for transport + queue control (Next/Previous, repeat, shuffle, etc.)
- **UPnP NOTIFY / LastChange events** for fast, push-based state updates (no HTTP status polling)
- LinkPlay **HTTP API (`httpapi.asp`) only where necessary** (inputs, presets, play URL, shutdown)

## Features
- Input selection: **Wi-Fi, Line-In, Bluetooth, Optical, HDMI ARC**, plus **AirPlay** (display/automation convenience)
- Event-driven updates for: **volume, mute, track info, repeat, shuffle, source naming**
- **Presets 1–6**
- **Play URL** (internet radio / streams) via `media_player.play_media`
- **Clear queue** via UPnP PlayQueue
- **Shutdown** via `media_player.turn_off` (device does not expose a power state)

---

## Install (HACS)

1. Open **HACS** in Home Assistant
2. Go to **Integrations**
3. Open the menu (⋮) → **Custom repositories**
4. Add this repository URL:
   - `https://github.com/amsound/audiopro_linkplay_hacs`
5. Select category **Integration**
6. Click **Install**
7. Restart Home Assistant

Then add it:
- **Settings → Devices & services → Add integration → “Audio Pro (LinkPlay)”**

---

## Notes
- The integration is designed for Audio Pro devices using the LinkPlay/WiiM stack (e.g. A28, C5 variants, etc.).
- Power status is not reported by the device; `turn_off` sends an immediate shutdown command without state feedback.

---

## Support / Issues
Please open an issue in this repo with:
- speaker model + firmware (if available)
- Home Assistant version
- a short description of expected vs actual behaviour
- logs (debug logs are helpful if relevant)
