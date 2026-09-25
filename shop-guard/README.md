# Shop Guard

Self-hosted anti-theft system for a pharmacy run remotely. It has three parts:

- **Cameras + AI review** (Frigate + Sentinel): records 24/7, reviews every person
  event that matters with Claude vision, keeps evidence in a form that shows if anyone
  has altered it, and sends alerts to your phone.
- **POS** (`pos/`): a till where the cash drawer only opens when a sale is rung up.
  Every sale, void and cash count is tied to a named cashier.
- **Drawer sensor** (`firmware/`): an ESP32 that reports every physical opening of
  the drawer. An opening with no sale behind it is caught on camera within a minute.

```
Tapo cameras ──RTSP──▶ Frigate 0.18 ──MQTT events──▶ Sentinel ──▶ Claude (vision review)
 (same LAN)            • 24/7 recording              • rules: zone / after-hours / watchlist
                       • person detection (iGPU)     • evidence locker (SHA-256 hash chain)
                       • face recognition            • Telegram alerts + daily report
                                                     • dashboard + "ask the footage"
                  Your phone ──Tailscale VPN──▶ dashboard :8080, Frigate UI :8971
```

**Why the Tapo app lost your footage:** Tapo Care only uploads short clips when the
camera's own motion detection fires. It never records all day. Frigate records
everything on the mini PC, whether or not a subscription is active.

## What it does

| Feature | How |
|---|---|
| 24/7 recording | Frigate keeps continuous video 14 days, motion 30 days, person events 60-90 days. Video only: no audio (see Legal). |
| Knows who it is | Frigate face recognition tags enrolled staff by name. |
| Decides what to review | `rules.yaml`: anyone in a **sensitive zone** (till, medicine cabinet, stockroom, back door), anyone **after hours**, and **every appearance of a watchlisted person**. |
| AI theft review | 10 frames per event go to Claude. It returns: a summary, a timestamped action list, theft indicators (cash pocketed, till opened with no customer, concealment, goods handed over without payment, camera tampering…), a 0-10 suspicion score, innocent explanations, and what to check in the full clip. |
| Evidence locker | Clips scoring ≥ 4 (and every watchlist clip) are copied as read-only files, SHA-256 hashed, and chained in `ledger.jsonl`. `GET /api/evidence/verify` detects any edit, deletion or re-ordering. The clip is also exempted from Frigate's retention cleanup. |
| Alerts | Telegram photo alert for scores ≥ 6. Instant ping when a person appears after hours. A daily summary at 21:30. |
| Ask the footage | Type "What did Ravi do near the till this week?" and Claude answers from the incident log, citing incident ids. |
| Tamper watchdog | Telegram alert within 2-3 minutes if a camera goes **offline**, is **covered** or blinded, or is **moved** (its edges no longer match the saved reference views), or if the recorder stops responding. |
| Dead-man switch | Pings [healthchecks.io](https://healthchecks.io) every minute. If the box is unplugged, loses power or is stolen, healthchecks.io alerts you, because a dead box can't send its own alert. |
| Off-site copies | Every 15 min the evidence locker plus a daily database snapshot is copied to Backblaze B2 using a key that **cannot delete**. Wiping the shop box can't wipe the copy. |
| Shift log | Every face-recognised appearance of enrolled staff. `/shifts` shows who was in each day; `/api/present?at=2026-02-10T14:30` lists who was there around a given moment. |

## POS: how cash theft becomes visible

| Rule | Enforced by |
|---|---|
| The drawer opens only for a cash sale, a payout, or an owner "no sale". The printer sends the open pulse (the drawer plugs into it) and the POS only asks at those moments. | `pos/service.py`, `pos/printer.py` |
| **Any physical opening without a POS request in the previous 6 s = alert, plus a Claude review of the till camera**: 20 s before to 40 s after. It covers key, force and a cut sensor wire, because a cut wire reads as "open". | `pos/drawer.py`, `firmware/drawer_sensor` |
| Drawer left open more than 60 s, sensor silent more than 90 s, or the POS asked for an opening but the sensor never saw one (sensor bypassed) = alert. | `pos/drawer.py` |
| Prices come from the catalogue on the server; the till can't send its own price. | `Pos.sell` |
| Each cashier logs in with their own PIN (5 wrong tries = 5-minute lock + alert). One open shift at a time. | `Pos.login`, `Pos.open_shift` |
| **Blind shift close**: the cashier enters the counted cash and never sees the expected amount, so there's no "surplus" to pocket. Any shortfall alerts you. | `Pos.close_shift` |
| Voids: a cashier can void only their own sale within 10 minutes; anything else waits for your approval. **Every void is alerted and the original sale's footage reviewed.** | `Pos.void` |
| Payouts (cash out for expenses) need a reason and show on your dashboard. | `Pos.payout` |
| Blind stock counts value missing stock at cost. | `Pos.record_count` |
| Every action is written to a hash-chained audit log, so records can't be quietly edited. The POS database is snapshotted hourly into the evidence tree and goes off-site. | `pos/db.py` |

**Claude in the POS**
- *Add product from photo*: photograph a box and Claude fills in brand, generic,
  strength, form, manufacturer, MRP and barcode for you to check.
- *AI loss analysis*: reads shifts, voids, payouts, drawer alerts, stock losses and
  the face-recognition shift log, then ranks where money is leaking and whose shifts
  it lines up with.

**Screens**
- Till: `http://<box>:8090`, on a counter PC or tablet with a USB barcode scanner.
- Owner dashboard: `http://<box>:8090/owner`, from Australia over Tailscale.

### POS hardware (AUD, approximate)

| Item | Price |
|---|---|
| 80 mm LAN thermal receipt printer with a drawer port (e.g. Xprinter XP-N160II LAN, sold in Dhaka) | A$80-120 |
| Cash drawer with an RJ11 printer-trigger cable and a **lock** (you keep the key) | A$60-100 |
| USB barcode scanner | A$30-50 |
| Counter PC or 10" tablet for the till screen | A$150-300 |
| Olimex ESP32-POE-ISO + normally-open reed switch + magnet | A$50 |

**Wiring the drawer sensor:**
1. Mount the reed switch on the cabinet frame and the magnet on the drawer, so the
   switch is closed when the drawer is shut.
2. Wire it between GPIO 4 and GND.
3. Run the cable inside the counter, out of reach.
4. Power the ESP32 from a PoE port on the camera switch, so it rides the same UPS.

### POS setup
1. In `.env`, set `POS_SECRET`, `POS_OWNER_NAME`, `POS_OWNER_PIN` (6-8 digits) and `PRINTER_HOST`.
2. Create the drawer sensor's MQTT login, then reload the broker:
   `docker compose exec mqtt mosquitto_passwd -b /mosquitto/data/passwd drawer <password>`
   `docker compose kill -s SIGHUP mqtt`
3. Flash `firmware/drawer_sensor/drawer_sensor.ino`. Set `MQTT_HOST` to the mini PC's
   LAN IP and `MQTT_PASS` to the password above. The sketch has **not** been compiled
   yet, so verify it on your test board.
4. Set `till_camera:` in `sentinel/rules.yaml` to the camera above the drawer.
5. On the owner dashboard, add each cashier with the same name they have in
   Frigate's Face Library. Add products; the photo button speeds this up.

## Hardware (AUD, approximate Sep 2026 retail)

| Item | Why | Price |
|---|---|---|
| Intel N150/N100 mini PC, 16 GB RAM, 512 GB SSD (e.g. Beelink EQ14, GMKtec G3 Plus) | The iGPU runs detection, face recognition and video decode at about 10 W | A$250-350 |
| 2 TB SSD or HDD (USB or second internal slot) | About 45 GB/day for 2 cameras at ~2 Mbps each; covers the retention above | A$120-180 |
| Small UPS (650 VA) | A thief pulling the power plug shouldn't end the recording | A$120-180 |
| 128 GB microSD per camera | On-camera backup recording | A$20-25 each |
| **Total** | | **≈ A$530-760** |

**Running cost:** Tailscale is free. Claude costs about A$0.13 per reviewed event on
`claude-opus-5` (10 frames). At 50 flagged events a day that is about **A$6.50/day**.
Ways to cut it:
- `FRAMES_PER_EVENT=6`
- `CLAUDE_MODEL=claude-sonnet-5` (about 60% cheaper)
- tighter zones, so fewer events are flagged

## Running a shop in Bangladesh from Australia

The camera's RTSP video stream only exists inside the shop's network. A MAC address
never leaves the local network, and TP-Link doesn't let other apps use its cloud, so
**one device must sit on the shop's network**. The recommended layout:

- The **mini PC lives at the shop** and does all the recording and AI work. The shop
  keeps recording even when the internet drops.
- **You connect from Australia over Tailscale**: dashboard, Frigate UI, SSH and the
  shop router's settings page (advertise the shop LAN as a
  [subnet route](https://tailscale.com/kb/1019/subnets)).
- **Only small things cross the internet:** alerts, evidence clips going off-site,
  and the clips you choose to watch.

**Build it in Australia, install it in February:**
1. Set up the mini PC completely at home: Ubuntu, Docker, Tailscale (with key expiry
   disabled for this machine), this repo, `.env`, `rules.yaml`.
2. Test it with one camera of the same model and try to defeat it (see Pre-flight
   test below).
3. Buy locally in Dhaka: the UPS (UPS batteries can't go on a flight), Cat6 cable
   and conduit, and ideally the cameras (TP-Link VIGI is sold there).
4. On site: put the box in the locked cabinet, plug in power and Ethernet. It joins
   your Tailscale network by itself.
5. Reserve each camera's IP on the shop router, using the camera's MAC address.

**Before February, from Australia:** in the Tapo app, rotate the Pan camera 360° in
live view and screenshot each direction. That gives you the floor plan for placing
the new cameras. You can also set the Tapo Camera Account remotely.

## Setup

### 1. Cameras (Tapo app)
1. For each camera: **Settings → Advanced Settings → Camera Account**. Create a username and password; use the same pair on both cameras.
2. **Pan camera:** turn off **Auto Tracking**, **Patrol** and **Privacy Mode**, then aim it at the till so it sees hands and the cash drawer, not just faces. If the camera moves, the zones you draw stop lining up with the real counter.
3. Router: give both cameras a **DHCP reservation**, so their IPs never change.
4. Put the microSD cards in and set recording to **Continuous** as a backup.

### 2. Mini PC
```bash
# Ubuntu Server 24.04
curl -fsSL https://get.docker.com | sh
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
git clone <this repo> && cd shop-guard
cp .env.example .env                       # fill in camera IPs/account, API key, Telegram
cp sentinel/rules.example.yaml sentinel/rules.yaml   # hours, zones, watchlist
docker compose up -d --build
docker compose logs frigate | grep -i password       # first-run Frigate admin password
```
Install Tailscale on your phone and sign in with the same account.

### 3. Frigate UI: `https://<mini-pc-tailscale-name>:8971`
1. **Settings → Camera configuration → Masks / Zones.** Draw `cash_counter`, `medicine_cabinet`, `stockroom` and `back_door`. The names must match `sensitive_zones` in `rules.yaml`.
2. **Face Library.** Create a face named exactly as in `watchlist` (for example `Ravi`) and upload 5-10 clear, front-on photos. Add other staff too, so they are told apart.
3. Restart Sentinel after editing rules: `docker compose restart sentinel`.

### 4. Test
Walk into the till zone. Within about a minute the event should appear on the
dashboard (`http://<mini-pc>:8080`), and a Telegram alert follows if it scores 6+.
To trigger the daily report on demand: `curl -u owner:<pw> -X POST http://<mini-pc>:8080/api/report`.

## Catching an employee: what actually works

1. **Reconcile the till against the footage.** Most staff theft is no-sale drawer
   opens, voids, refunds with no customer, or under-ringing ("sweethearting"). Pull
   your POS's no-sale/void/refund log, then check each timestamp on the dashboard. The
   `till_opened_without_customer` indicator exists for exactly this.
2. **Build a pattern, not one clip.** Filter the dashboard by the person and 30 days.
   A single ambiguous clip proves nothing. Five clips showing the same till → pocket
   motion on days the till came up short is a case.
3. **Treat the AI score as a reason to look, not as proof.** Watch the full clip every
   time. A still-frame sample can miss the decisive second, and the model lists
   innocent explanations for a reason.
4. **Don't tip them off.** You must tell staff the cameras exist (see Legal). You
   don't have to say who you suspect or that AI review is running.
5. **When you have it:** export the clips and `ledger.jsonl` to a USB drive, run
   `/api/evidence/verify`, and give both to police together with the till records.
   Follow a documented process before dismissal (Fair Work unfair-dismissal rules
   still apply to theft).

## Legal

The shop, its staff and any police case are in **Bangladesh**, so Bangladeshi law
applies, not Australian law. Check with a local lawyer before relying on footage
against an employee. Safe defaults this system already follows:
- cameras are visible, not hidden;
- **no audio** (`preset-record-generic` records video only);
- no cameras in toilets or changing areas;
- the Face Library holds **staff only**, enrolled with their knowledge.

This is not legal advice.

## Pre-flight test (do this at home before February)

Nothing ships until every row triggers the expected alert:

| Attack | Expected result |
|---|---|
| Unplug a camera's network cable | "OFFLINE" alert within about 2 minutes |
| Cover a lens with tape or your hand | "COVERED" alert within about 3 minutes |
| Turn a camera away | "MOVED" alert within about 3 minutes |
| `docker compose stop frigate` | "recorder not responding" alert |
| Pull the mini PC's power (UPS unplugged) | healthchecks.io alert after its grace period |
| Delete a clip in `evidence/` | `/api/evidence/verify` reports it; the B2 copy survives |
| Walk into the till zone after hours | Instant ping, then an AI-reviewed alert |
| Open the cash drawer with its key (no sale) | "unauthorized open" alert, then an AI review of the till clip about a minute later |
| Pull the drawer sensor's cable | "unauthorized open" alert (cut wire = open) |
| Unplug the ESP32 | "sensor offline" alert within about 90 s |
| Close a shift with cash missing | "cash short" alert; the variance shows on the dashboard |

After aiming each camera, save its view as known-good. Do it again at night so the
infrared view is also recognised:
`curl -u owner:<pw> -X POST "http://<box>:8080/api/watchdog/reference/<camera>?reset=true"`
(drop `reset=true` for the night-time call).

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f sentinel` |
| Watchdog status | `curl -u owner:<pw> http://<box>:8080/api/watchdog` |
| Verify evidence integrity | `curl -u owner:<pw> http://<mini-pc>:8080/api/evidence/verify` |
| Incident JSON | `GET /api/incidents?days=30&person=Ravi&min_score=6` |
| Run tests | `cd sentinel && pip install -r requirements.txt pytest && pytest` |
| Update Frigate | change the image tag in `docker-compose.yml`, read the release notes, `docker compose pull && docker compose up -d` |

Security: only ports 8971 (authenticated Frigate UI) and 8080 (Sentinel, basic auth)
are published. Don't port-forward them on your router; use Tailscale. Frigate's
unauthenticated API port 5000 stays inside the Docker network.

## Moving to its own repository
This project lives in `shop-guard/` of the dotfiles repo for now. To give it its own repo:
```bash
git subtree split --prefix shop-guard -b shop-guard-main
git push git@github.com:<you>/shop-guard.git shop-guard-main:main
```
