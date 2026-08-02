# Facemosque Adhan

An always-on Raspberry Pi that pulls prayer times from the Facemosque API and
plays the adhan at each waqt through a Bluetooth speaker — an Amazon Echo, or
anything else that accepts A2DP. Alexa can stop it by voice.

```
Facemosque API ──30 day window, refreshed every 3 days──> local cache
                                                              │
                              scheduler ────────────────────> mpv
                                                              │
                                              PipeWire ──A2DP──> Echo Dot
                                                              ▲
     "Alexa, turn off adhan" ──> WeMo emulation on the LAN ───┘
```

Python 3 standard library only — no pip packages, no venv, nothing for
Debian's PEP 668 to object to.

## Install

On a fresh Raspberry Pi OS (Lite is fine, and preferred):

```sh
git clone <this repo> ~/adhan-player && cd ~/adhan-player
./install.sh
```

Then:

```sh
$EDITOR .env              # API key, mosque id, timezone
./adhanctl pair           # put the speaker in pairing mode first
./adhanctl doctor         # verify every prerequisite
./adhanctl fetch          # pull the prayer times
systemctl --user enable --now adhan
```

Finally, say **"Alexa, discover devices"** once so voice control works.

## Configuration

Everything lives in `.env` — see [.env.example](.env.example). The keys that
matter:

| Key | Meaning |
| --- | --- |
| `EXPO_PUBLIC_API_KEY` | Facemosque visitor key |
| `MOSQUE_ID` | Numeric id. The API rejects slugs. |
| `TIMEZONE` | IANA zone, e.g. `Europe/Berlin` |
| `AUDIO_PATH` | Any format ffmpeg reads |
| `AUDIO_PATH_FAJR` | Optional separate Fajr adhan |
| `BT_SINK_MAC` | Written for you by `./adhanctl pair` |
| `ALEXA_DEVICE_NAME` | What you say: "Alexa, turn off **adhan**" |
| `PRAYERS` | Which waqts play, comma separated |

There is deliberately **no volume setting**. The adhan plays at unity gain and
the speaker's own volume is the only control — set it on the Echo. The service
does pin the PipeWire sink to 1.0 before each play, so a stray `wpctl
set-volume` from some earlier session cannot silently halve the output.

A copyright-free adhan recording ships in [`audio/`](audio/). To use a
different one, drop it in and point `AUDIO_PATH` at it — any format ffmpeg
reads works, with no transcoding step.

**The API's `timezone` field is not trustworthy.** Takaful Mosque reports
`"UTC"` but serves Chemnitz wall-clock times — sunrise `05:28` on 2026-08-02 is
CEST, not UTC. The times are correct as printed; only the label is wrong. So
the zone is configured in `.env` and the payload's field is ignored.

## Commands

```
./adhanctl doctor    every prerequisite, with the fix for each failure
./adhanctl pair      scan, pair, and write the MAC into .env
./adhanctl fetch     refresh the 30 day window
./adhanctl show      print the cached schedule
./adhanctl next      next adhan and how long until it
./adhanctl play      play the adhan now (speaker test)
./adhanctl run       run the service in the foreground
```

Service logs: `systemctl --user status adhan` or
`journalctl --user -u adhan -f`.

## Why the Pi and not an ESP32

An ESP32-S3 cannot do this, and the reason is in Espressif's own
`soc_caps.h`: the S3 defines neither `SOC_BT_CLASSIC_SUPPORTED` nor
`SOC_DAC_SUPPORTED`. No Bluetooth Classic means no A2DP, and no DAC means no
analog output either — so an S3 cannot reach an Echo *or* drive a speaker
without extra hardware. Only the original ESP32 has Classic Bluetooth. The
Pi 5 has both, and a real filesystem, TLS trust store, NTP and systemd on top.

## Why "Alexa, stop" does not work, and what does

The Echo advertises AVRCP (`0000110e`) and the protocol link is fine. But
Alexa's voice layer never converts "stop" or "pause" into an AVRCP command for
a Bluetooth *source*: 35 seconds of `btmon` captured 116,199 packets and not a
single AVCTP frame, while the Echo answered *"I'm not sure how to help you with
that."* Protocol capability is not product behaviour.

So the stop trigger comes over WiFi instead. [`adhan/alexa.py`](adhan/alexa.py)
emulates a WeMo smart plug — SSDP on UDP 1900 plus a little SOAP — which every
Echo discovers natively with no Amazon account, no skill and no cloud
round-trip:

> **"Alexa, turn off adhan"** — stops playback in well under a second.
> **"Alexa, turn on adhan"** — plays it, handy for testing.

`mpris-proxy` is still installed and enabled, so a speaker that *does* send
AVRCP will work without any change.

## What the installer does that no single doc tells you

On a headless Pi, Bluetooth audio fails in three separate ways that each
report as "Protocol not available" or as nothing at all:

1. **The user is not in the `bluetooth` group.** D-Bus then refuses the
   `org.bluez.Media1` calls PipeWire needs to register an A2DP endpoint.
2. **WirePlumber only starts its bluez monitor when logind reports the seat as
   `active`.** A headless box has no seat that ever becomes active, so the
   monitor waits forever and no endpoint is ever registered — every connect
   fails with `br-connection-profile-unavailable`. Fixed with
   `monitor.bluez.seat-monitoring = disabled`.
3. **Without lingering**, the user systemd instance — and PipeWire with it — is
   killed the moment the SSH session ends.

`./adhanctl doctor` checks all three, and tells you the exact fix for each.

## Behaviour worth knowing

- **Missed adhans are skipped, not replayed.** Anything more than 90s late is
  retired silently, so a Pi that was offline all morning stays quiet instead of
  firing four adhans in a row. Fired state is persisted, so a restart at 13:36
  does not replay the 13:20 Dhuhr.
- **Days the mosque admin has not filled in are omitted by the API**, not
  returned as nulls — a 9-day request over new year returned 4 days. Records
  are matched by date, never by position.
- **The cache survives outages.** A failed fetch leaves the existing window
  alone; the device keeps working with what it has.
- **Bluetooth reconnects on its own**, backing off 5s → 5min.
- **5GHz WiFi is preferred.** The Pi's WiFi and Bluetooth share one 2.4GHz
  antenna, so 2.4GHz traffic degrades A2DP audio. Ethernet is better still.

## Known gaps

- One adhan file for all waqts unless `AUDIO_PATH_FAJR` is set.
- Jummah is not used: the API exposes khutbah and iqamah for it, but no adhan.
- `iftar` is ignored — it *is* the Maghrib adhan, which already plays.
- The mosque's data currently ends at **2026-12-31**. After that the device
  logs a warning and stays silent until the admin fills in 2027.
