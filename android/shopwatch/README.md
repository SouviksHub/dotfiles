# shopwatch — theft and intrusion alerts from Tapo cameras, running on the phone

```
Tapo cam ──RTSP stream2 (640x360)──▶ ffmpeg ─2 fps─▶ motion gate ─▶ YOLO11n person detector ─▶ rules ─▶ Telegram 📷
         └─RTSP stream1 (HD)───────▶ ffmpeg -c copy ─▶ 60 s segments (rolling 20 min) ──hard-link on event──▶ evidence/
                                                                          events.db ─▶ local LLM ─▶ daily report
```

## What it catches, and what it doesn't

| Rule | Fires when | Reliability |
|---|---|---|
| `INTRUSION` | a person is detected in 2 consecutive frames while armed (outside `open_hours`, or `/arm`) | **High.** This is the main use. |
| `ZONE` | a person's feet stay inside a polygon for `dwell_seconds` (cash drawer, back door, high-value shelf) | High for presence; the zone has to be drawn well |
| `TAMPER` | the image is almost uniform for 20 s (lens covered or sprayed, camera turned to a wall) | High |
| `CAMERA_DOWN` | no frames for 60 s (camera unplugged, Wi-Fi jammed, router off) | High. Thieves cut power to cameras first. |

**It does not see someone pocketing an item during opening hours.** Recognising concealment needs
pose and action models trained on shoplifting footage. Commercial systems that claim it have high
false-alarm rates, and a phone CPU can't run those models across several cameras. What this system does give you:

1. a guaranteed alert for after-hours entry and tampering;
2. a timestamped HD clip for every event, so you review 2 minutes of video instead of 12 hours;
3. zone rules for the places where theft actually happens: the till, the stockroom door, high-value shelves.

The on-device LLM **does not watch video**. It turns the day's event log into a short report ("3 cash-drawer dwells
between 14:00 and 15:00, all on camera `counter`; review those first").

## Cameras

- Works with Tapo models that support RTSP/ONVIF: C100, C110, C200, C210, C220, C310, C320WS, C500, C520WS and similar.
  **Not supported:** battery cameras (C400/C420/C425), models connected through the H200 hub, and most doorbells, because they have no RTSP stream.
- Tapo app → camera → Settings → Advanced Settings → **Camera Account** → create a username and password. This is separate
  from your TP-Link cloud login. Tapo notifications and SD-card recording keep working alongside shopwatch; leave an SD card in each camera as a second copy of the footage.
- Router: give each camera a **fixed IP** (DHCP reservation). Also block the cameras from the internet if you
  don't need the Tapo cloud; RTSP works entirely on your local network.

## Install (on the phone, after `android/bootstrap.sh`)

```bash
bash ~/dotfiles/android/shopwatch/setup.sh
nano ~/shopwatch/config.toml          # camera URLs, hours, Telegram token + chat id
# run in the foreground once and watch the log:
proot-distro login debian --termux-home -- /root/shopwatch/venv/bin/python \
  /root/dotfiles/android/shopwatch/shopwatch.py /root/shopwatch/config.toml
sv-enable shopwatch                   # then it runs under runit and starts at boot
```

Drawing zones: send `/snap counter` to the bot, open the image, read the pixel corners and divide them by 640 (x) and 360 (y).

Telegram commands (accepted only from your `chat_id`): `/status` `/snap [cam]` `/arm` `/disarm` `/auto` `/report`.

## Budget on an 8 GB phone

- **CPU:** YOLO11n at 320 px is about 1.6 GFLOP per frame, which takes 25–60 ms on an old Snapdragon 8xx.
  Frames are only sent to the detector when something moves (plus a check every 30 s),
  so 2 cameras at 2 fps use about 5–15% CPU most of the day. HD recording is `-c copy` (no decoding), so it's close to free.
- **Storage:** a Tapo stream1 runs at about 1.5–2 Mbps, so the 20-minute buffer is about 300 MB per camera. Each event keeps about 3 minutes
  (40–50 MB). Clips are hard links, so an event costs no extra space until the buffer rotates past it.
- **LLM:** idle all day; it wakes once at `daily_report_time`.

## Running a phone 24/7

- Set the charge limit to 80% if the phone has one; otherwise use a smart plug on a timer. A lithium cell held at 100%
  and warm swells. **Check the phone every month for a bulging back.**
- Keep it somewhere cool, out of its case and away from sunlight. Detection throttles if the phone overheats; recording doesn't.
- Put the phone and router on a small UPS. Then a power cut still produces a `CAMERA_DOWN` alert
  (the cameras lose power but the phone and internet stay up), and the phone keeps its record of what happened.
- Put up a visible CCTV sign. Footage from a clearly signed system is easier to use as evidence, and the sign
  deters theft on its own.
