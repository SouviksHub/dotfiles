# shopwatch — theft and intrusion alerts from Tapo cameras, running on the phone

```
Tapo cam ──RTSP stream2 (640x360)──▶ ffmpeg ─2 fps─▶ motion gate ─▶ YOLO11n person detector ─▶ rules ─▶ Telegram 📷
         └─RTSP stream1 (HD)───────▶ ffmpeg -c copy ─▶ 60 s segments (rolling 20 min) ──hard-link on event──▶ evidence/
                                                                          events.db ─▶ local LLM ─▶ daily report
```

## What it catches

| Rule | Fires when | Severity |
|---|---|---|
| `INTRUSION` | a person is detected in 2 consecutive frames while armed (outside `open_hours`, or after `/arm`) | alert |
| `ZONE` | a person's feet stay inside a polygon for `dwell_seconds` | alert |
| `TAMPER` | the image is almost uniform for 20 s (lens covered or sprayed, camera turned to a wall) | alert |
| `CAMERA_DOWN` | no frames for 60 s (camera unplugged, Wi-Fi jammed, router off) | alert |
| `CASH_TO_POCKET` | the cashier takes payment at the counter, then puts a hand to a pocket **before** the drawer | alert + clip |
| `DRAWER_TO_POCKET` | hand goes from the drawer to a pocket within 10 s, with no hand-over to a customer in between | alert + clip |
| `NO_SALE_OPEN` | the drawer opens with no customer within ±20 s | alert + clip |
| `HANDS_UP` | the cashier holds both hands above head for ≥ 2 s (armed robbery) | alert + clip |
| `PANIC` | the hidden button is held for 1 s | alert + snapshots |
| `DRAWER_LEFT_OPEN` | the drawer sensor reports open for > 60 s | alert |
| `POCKET`, `TXN` | every pocket touch and every transaction, with or without the drawer | daily stats only |

## Cash counter: how the skim detection works

A 640x360 CCTV stream can't see a ₹100 note in a hand. It can see **where the hands go**. Every cash-handling
act is a sequence of wrist visits to four places, tracked with YOLO11n-pose (17 body keypoints) at 6 fps:

```
EXCHANGE  wrist in the counter-top hand-over area while a customer is present
DRAWER    wrist in the drawer area (or reed-switch sensor: drawer open)
POCKET    wrist within 0.40 × torso length of a hip (trouser pocket) or 0.22 × torso of the chest-pocket point
          (distances scale with torso length, so it works at any distance from the camera)

honest sale:   EXCHANGE → DRAWER → EXCHANGE (change) → …
skim:          EXCHANGE → POCKET  → (DRAWER) …          ⇒ CASH_TO_POCKET
drawer theft:  DRAWER   → POCKET  (no EXCHANGE between) ⇒ DRAWER_TO_POCKET
no-sale:       DRAWER with no customer ±20 s            ⇒ NO_SALE_OPEN
```

**A single pocket touch proves nothing.** People keep phones and handkerchiefs in pockets, and folded arms look like a hand
at a chest pocket (I checked this on a real photo). So:

- An alert only fires on a **sequence** (a pocket touch right after payment or right after the drawer), never on a pocket touch alone.
- Every pocket touch is still logged. The daily report compares each counter with **its own 7-day average**
  and flags days with 1.5× the usual count. Someone skimming touches their pockets more often than on honest days.
- Nothing here is proof. Every alert comes with an HD clip; **watch it before you act**.

### Camera placement decides everything

- Use **one camera dedicated to the counter**, mounted 2–2.5 m high, **behind and to one side of the cashier**
  (over their shoulder, about 45°). It must see in one frame: the cashier's hips and chest, the drawer, and the counter top.
  A camera on the customer side sees the counter front and **no hips**, so the pocket rules can't work.
- Send `/status`: it reports `staff hips visible N% of frames`. Below 60%, move the camera.
- Set `roi` to crop just the counter area. The pose model then sees the cashier at full resolution instead of scaled down.
- Draw the four zones on a `/snap` image. Keep `drawer_zone` tight around the drawer opening and `exchange_zone`
  on the counter surface between cashier and customer.
- Good light at the counter, no strong backlight (a window behind the customer ruins keypoints).

### Drawer sensor + panic button (recommended, ~₹400)

Camera-based drawer detection guesses from hand position. A **reed switch on the drawer is exact**, and it
catches drawer openings the camera misses. `esp32_drawer/esp32_drawer.ino` posts drawer open/close and a
hidden panic button to shopwatch (`[sensors]` token, `drawer_sensor = true`). A Tapo T110 contact sensor
can't be used for this: its events only go through the TP-Link cloud.

## The controls that matter more than the camera

Detection catches theft after it happens. These make it impossible or obvious from the start; use both.

| Control | What it defeats |
|---|---|
| **Payment by UPI QR + soundbox** at the counter, with notifications on the owner's phone | Cash skimming. Digital payments leave a record on their own. Offer a small discount for UPI. |
| **Pocketless uniform/apron + locker** for the staff's personal cash and phone | The pocket route is gone. Any hand-to-body movement near cash becomes suspicious, and a staff member with no reason to keep cash has none on them. |
| **Every sale entered in the billing app, receipt printed or shown**; "no bill = free" sign for customers | Unrecorded sales: customers make sure the sale gets entered |
| **Daily reconciliation**: `opening float + cash sales in the billing app − cash paid out = cash counted`. Count **blind** (staff count and write it down without seeing the expected figure). | Drawer skimming shows up as a daily shortfall. Track it per shift or per cashier; a shortfall that appears only on one person's shifts points to that person. |
| **Transaction count check**: the daily report's `transactions seen` vs the number of bills in the billing app | "No-ring" theft (cash taken, sale never entered): drawer and bills match, but the camera saw more transactions |
| **Stock reconciliation** (weekly count of high-value items vs sales) | Goods handed to friends for free or under-billed |
| **Fixed float**, cash taken out of the drawer several times a day into a drop safe | Limits how much can be lost in one theft or robbery |
| **Visible camera + monitor showing the feed** at the counter | Most staff theft happens only when the person thinks nobody is watching |

## Robbery

`HANDS_UP` (staff holding hands above head for 2+ s), the hidden `PANIC` button, and after-hours `INTRUSION`, `TAMPER` and `CAMERA_DOWN` alerts
all reach your phone within seconds. Clips are stored **on the phone**, so pulling a camera off the wall doesn't destroy
the evidence; hide the phone somewhere out of sight (a locked cabinet with airflow). For a remote copy, sync
`~/shopwatch/data/events` to Google Drive with `rclone` in Termux.

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

- **Counter camera:** pose on every frame at 6 fps × about 40–70 ms ≈ 1–1.5 cores continuously while the shop is open. This is the
  expensive part; keep it to one counter camera per phone.
- **CPU (other cameras):** YOLO11n at 320 px is about 1.6 GFLOP per frame, which takes 25–60 ms on an old Snapdragon 8xx.
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
