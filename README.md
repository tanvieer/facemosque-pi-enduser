# Facemosque Adhan

An always-on Raspberry Pi that fetches prayer times from the Facemosque API and
plays the adhan at each waqt through a Bluetooth speaker — an Amazon Echo, or
anything else that accepts A2DP.

```
Facemosque API ──30 day window, refreshed every 3 days──> local cache
                                                              │
   a year of times committed in data/ ───(when offline)───────┤
                                                              │
                              scheduler ────────────────────> mpv
                                                              │
                                              PipeWire ──A2DP──> Echo Dot
```

Python 3 standard library only — no pip packages, no venv, nothing for
Debian's PEP 668 to object to. Everything you configure lives in one `.env`
file, and only one line of it is mandatory. It also works with no internet at
all.

---

## What you need

- A Raspberry Pi (developed on a Pi 5; a Pi 3/4/Zero 2 W works too — anything
  with built-in Bluetooth)
- A microSD card and a power supply
- A Bluetooth speaker: an Echo Dot, or any speaker/headphone that pairs normally
- A Facemosque API key
- Internet on the Pi (Ethernet is best — see [Notes](#notes-worth-knowing))

---

## Install

### 1. Put Raspberry Pi OS on the card

Use **Raspberry Pi Imager**. Choose *Raspberry Pi OS Lite (64-bit)* — no
desktop needed. Before writing, open the settings (⚙ / "Edit settings") and
fill in:

- **hostname** — e.g. `adhan`
- **username and password** — remember these, you need them to log in
- **WiFi** — SSID and password (skip if you will use Ethernet)
- **Enable SSH** → *Use password authentication*

Write the card, put it in the Pi, power on, wait a minute.

### 2. Log in

```sh
ssh <username>@adhan.local
```

If `adhan.local` does not resolve, find the Pi's IP in your router's device
list and use that instead.

### 3. Put the speaker in pairing mode

Do this before the next step, so the installer can find it. On an Echo, say
**"Alexa, pair Bluetooth"**, or use the Alexa app → *Devices* → your Echo →
*Bluetooth Devices* → *Pair a New Device*.

### 4. Run the installer

```sh
sudo apt update && sudo apt install -y git
git clone https://github.com/tanvieer/facemosque-pi-enduser.git ~/adhan
cd ~/adhan && ./install.sh
```

That is the whole install. `install.sh` asks you two questions — your API key,
and which speaker to pair with — and does everything else itself: packages,
the three headless-Bluetooth fixes (see [below](#what-the-installer-fixes)),
`.env`, the systemd unit, the first fetch, and starting the service. It ends
by telling you when the next adhan is due.

It is safe to run again at any time; it never overwrites an existing `.env`.

**Your API key is the only thing you must supply.** Everything else already
has a working default. If your mosque is not Takaful in Chemnitz, open `.env`
afterwards and set `MOSQUE_ID` and `TIMEZONE` — that is what the defaults
point at. To find your mosque id (a plain number; the API rejects slugs):

```sh
curl -s -H "X-API-Key: YOUR_KEY" https://facemosque.com/api/v1/mosques \
  | python3 -m json.tool
```

### 5. Check it

```sh
adhanctl doctor    # every prerequisite, with the fix for each failure
adhanctl play      # you should hear the adhan — set the volume on the speaker
adhanctl next      # when the next one is due
```

The program has no volume control on purpose: turn the Echo itself up or down.

That is it. The Pi plays the adhan on its own from here, and starts again by
itself after a reboot or a power cut — nobody has to log in.

---

## Everyday commands

```
./adhanctl help      every command, with examples
./adhanctl doctor    every prerequisite, with the fix for each failure
./adhanctl pair      scan, pair, and write the MAC into .env
./adhanctl fetch     refresh the 30 day window now
./adhanctl show      print the schedule, including the jummah times
./adhanctl next      next adhan and how long until it
./adhanctl set       show every setting; `set KEY=value` to change one
./adhanctl jummah    which Friday prayer plays; `jummah 2` or `jummah all` to change
./adhanctl play      play the adhan now (speaker test)
./adhanctl stop      stop playback now
./adhanctl bundle    refresh the offline fallback (then commit it)
./adhanctl run       run the service in the foreground
```

Service control:

```sh
systemctl --user status adhan      # is it running?
systemctl --user restart adhan     # after editing .env
systemctl --user stop adhan        # silence it for a while
journalctl --user -u adhan -f      # follow the log
```

Updating:

```sh
cd ~/adhan && git pull && systemctl --user restart adhan
```

`.env` is never overwritten by a pull — it is not in the repository.

---

## Configuration

Everything lives in `.env`, in the directory you cloned into. You do not have
to open it in an editor:

```sh
adhanctl set                    # every setting and its current value
adhanctl set key fm_...         # change one
adhanctl set mosque 7
adhanctl set fajr=              # clear one
```

Settings have short names so you never type `EXPO_PUBLIC_API_KEY`: `key`,
`url`, `mosque`, `tz`, `audio`, `fajr`, `speaker`, `name`, `alexa`, `prayers`,
`jummah`, `days`, `refresh`, `fallback`. The full name works too, as does any
unambiguous part of one — `adhanctl set alexa_port 52001`. `adhanctl set` on
its own lists them with their short names.

`set` restarts the service so the change takes effect. Setting the API key on
a Pi where the service never started — because it had no key — starts it and
enables it for the next boot, which is the last step of an install that was
run without one.

See [.env.example](.env.example) for the full list with comments.

| Key | Meaning |
| --- | --- |
| `EXPO_PUBLIC_API_KEY` | Facemosque visitor key — **the only required value** |
| `MOSQUE_ID` | Numeric id — the API rejects slugs (default `4`, Takaful) |
| `TIMEZONE` | IANA zone (default `Europe/Berlin`) |
| `AUDIO_PATH` | Any format ffmpeg reads |
| `AUDIO_PATH_FAJR` | Optional separate Fajr adhan |
| `BT_SINK_MAC` | Written for you by `./adhanctl pair` |
| `PRAYERS` | Which waqts play, comma separated |
| `JUMMAH` | Which Friday prayer plays — see [below](#friday) |
| `FALLBACK_PATH` | Offline times — see [below](#it-keeps-working-without-internet) |
| `SCHEDULE_DAYS` | How many days to cache (default 30) |
| `REFRESH_INTERVAL_DAYS` | How often to re-fetch (default 3) |
| `ALEXA_ENABLED` | Experimental voice stop — see [below](#alexa-voice-control-does-not-work-and-why) |

A blank value means "use the default", so you can delete a line you do not
care about. A *wrong* value is still an error: `MOSQUE_ID=takaful` or an
unknown timezone stops the service with a message naming the key, rather than
guessing and playing another city's times.

Restart the service after any edit: `systemctl --user restart adhan`.

There is deliberately **no volume setting**. The adhan plays at unity gain and
the speaker's own volume is the only control. The service does pin the
PipeWire sink to 1.0 before each play, so a stray `wpctl set-volume` from some
earlier session cannot silently halve the output.

A copyright-free adhan recording ships in [`audio/`](audio/). To use a
different one, drop it in and point `AUDIO_PATH` at it — any format ffmpeg
reads works, with no transcoding step.

**The API's `timezone` field is not trustworthy.** Takaful Mosque reports
`"UTC"` but serves Chemnitz wall-clock times — sunrise `05:28` on 2026-08-02
is CEST, not UTC. The times are correct as printed; only the label is wrong.
So the zone is configured in `.env` and the payload's field is ignored.

---

## Friday

On Friday the **jummah adhan replaces Dhuhr** — there is one midday prayer, and
it is the jummah. A mosque often holds several: Takaful holds three, at 13:30
Arabic, 14:30 English and 14:45 Arabic.

```sh
adhanctl jummah        # what the mosque holds, and which ones will play
adhanctl jummah 2      # only the 2nd
adhanctl jummah all    # every one
```

```
Jummah — on Fridays these replace Dhuhr:
  13:30  Jummah 1 (Arabic)            skipped
  14:30  Jummah 2 (English)           plays
  14:45  Jummah 3 (Arabic)            skipped
```

It writes `JUMMAH` to `.env` and restarts the service for you — except mid-adhan,
when it tells you to do it afterwards instead of cutting the playback off.

| `JUMMAH` | What plays on Friday |
| --- | --- |
| *(blank)* | every jummah gets an adhan |
| `2` | only the 2nd |
| `9`, with only 3 jummahs | the last one — a wrong number never means silence |

The API gives jummah a khutbah time and an iqamah time but no adhan time —
there is nothing to compute, since the adhan is called as the khatib takes the
minbar. So the khutbah time is when it plays, with iqamah as the fallback for
a mosque that filled in only that one.

If the mosque has no jummah configured at all, Friday keeps its ordinary Dhuhr
adhan. Better the normal adhan than a silent Friday.

## It keeps working without internet

A full year of prayer times is committed in [`data/`](data/), so a Pi that
cannot reach the API still calls the adhan. Three layers, most recent first:

| | Source | Covers | Refreshed |
| --- | --- | --- | --- |
| 1 | `~/.local/state/adhan/schedule.json` | next 30 days | every 3 days |
| 2 | `~/.local/state/adhan/fallback.json` | this year and next | **1st of each month** |
| 3 | `data/mosque-<id>.json` (in the repo) | the year it was built | when you run `bundle` |

Layer 2 is the one that keeps a long-running device honest. **It is rebuilt on
the 1st of every month, and if the device is offline that day it stays due and
rebuilds within five minutes of the connection coming back.** Missing the
window is the failure that matters — this is the copy that has to be right
when nothing else works.

It is written to the state directory rather than over the committed file, so
`git pull` never conflicts with a file the service rewrote by itself.

Layer 3 is the baseline a fresh clone starts from, and updating it is a
deliberate act: run it and commit when the mosque publishes a new year, or
when you point the repo at a different mosque.

```sh
./adhanctl bundle && git add data/ && git commit -m "refresh prayer times"
```

Both bundles record the mosque id they were built for. Point `MOSQUE_ID` at a
different mosque and the file is ignored rather than played, so nobody ends up
hearing Chemnitz times in another city.

`./adhanctl show` prints when the fallback was last built and whether a
rebuild is due.

## Troubleshooting

Run `./adhanctl doctor` first — it names the exact fix for anything it finds.

| Symptom | Cause and fix |
| --- | --- |
| `config error: ... is not set` | A required key is missing from `.env`. |
| `pair` finds nothing | The speaker is not in pairing mode, or it dropped out — Echo pairing mode lasts about a minute. Put it back in and retry. |
| Pairs but no sound | `./adhanctl doctor` — most likely the A2DP endpoint check. Log out and back in after the first install, so the new `bluetooth` group membership takes effect. |
| Sound too quiet or too loud | Change it on the speaker. The program never touches volume. |
| Nothing plays at prayer time | `./adhanctl show` — the mosque admin may not have filled in those days; missing days are omitted by the API. |
| Silent after a reboot | `systemctl --user is-enabled adhan`. If it is disabled, run `systemctl --user enable --now adhan`. Also check lingering in `doctor`. |
| Audio stutters | The Pi's WiFi and Bluetooth share one antenna. Use Ethernet, or 5GHz WiFi. |

---

## Notes worth knowing

- **Missed adhans are skipped, not replayed.** Anything more than 90s late is
  retired silently, so a Pi that was offline all morning stays quiet instead of
  firing four adhans in a row. Fired state is persisted, so a restart at 13:36
  does not replay the 13:20 Dhuhr.
- **Days the mosque admin has not filled in are omitted by the API**, not
  returned as nulls — a 9-day request over new year returned 4 days. Records
  are matched by date, never by position.
- **The cache survives outages.** A failed fetch leaves the existing window
  alone, and a year of times ships in the repo underneath it — see
  [above](#it-keeps-working-without-internet).
- **Bluetooth reconnects on its own**, backing off 5s → 5min. Echo devices drop
  the link when idle, so this happens routinely.
- **5GHz WiFi is preferred.** The Pi's WiFi and Bluetooth share one 2.4GHz
  antenna, so 2.4GHz traffic degrades A2DP audio. Ethernet is better still.

---

## What the installer fixes

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

`./adhanctl doctor` checks all three.

---

## Alexa voice control does not work, and why

Two routes were tried and measured. Neither works with current Echo firmware,
so **there is no voice stop**. Use `./adhanctl stop`, or turn the speaker off.

**AVRCP.** The Echo advertises AVRCP (`0000110e`) and the protocol link is
fine, but Alexa's voice layer never converts "stop" or "pause" into an AVRCP
command for a Bluetooth *source*: 35 seconds of `btmon` captured 116,199
packets and not a single AVCTP frame, while the Echo answered *"I'm not sure
how to help you with that."* Protocol capability is not product behaviour.

**WeMo emulation.** [`adhan/alexa.py`](adhan/alexa.py) emulates a WeMo smart
plug over SSDP and SOAP — the classic no-account, no-cloud trick. The code is
correct (a Mac on the same LAN discovers it), but a 90-second capture caught
zero `M-SEARCH` probes from the Echo during a full app-driven device scan, and
the Echo never found it. Amazon appears to have dropped local WeMo discovery.

It ships disabled (`ALEXA_ENABLED=false`). Set it to `true` if you want to try
it with a different Echo generation — it costs one UDP socket and nothing
else. `mpris-proxy` is also installed and enabled, so a speaker that *does*
send AVRCP will just work.

---

## Why a Pi and not an ESP32

An ESP32-S3 cannot do this, and the reason is in Espressif's own
`soc_caps.h`: the S3 defines neither `SOC_BT_CLASSIC_SUPPORTED` nor
`SOC_DAC_SUPPORTED`. No Bluetooth Classic means no A2DP, and no DAC means no
analog output either — so an S3 cannot reach an Echo *or* drive a speaker
without extra hardware. Only the original ESP32 has Classic Bluetooth. The
Pi has both, plus a real filesystem, TLS trust store, NTP and systemd.

---

## Known gaps

- One adhan file for all waqts unless `AUDIO_PATH_FAJR` is set. There is no
  separate file for jummah.
- `iftar` is ignored — it *is* the Maghrib adhan, which already plays.
- `suhur` is ignored: it is only sent during Ramadan, and it is not an adhan.
- Takaful Mosque's data currently ends at **2026-12-31**, which is also where
  the bundled fallback ends. After that the device logs a warning and stays
  silent until the admin fills in 2027 — then run `./adhanctl bundle` again.
