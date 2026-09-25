# Shop Guard

Self-hosted AI CCTV for two Tapo cameras. It records 24/7, reviews every person
event that matters with Claude vision, keeps evidence in a form that shows if anyone
has altered it, and sends alerts to your phone.

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
| Decides what to review | `rules.yaml`: anyone in a **sensitive zone** (till, S8 cabinet, stockroom, back door), anyone **after hours**, and **every appearance of a watchlisted person**. |
| AI theft review | 10 frames per event go to Claude. It returns: a summary, a timestamped action list, theft indicators (cash pocketed, till opened with no customer, concealment, goods handed over without payment, camera tampering…), a 0-10 suspicion score, innocent explanations, and what to check in the full clip. |
| Evidence locker | Clips scoring ≥ 4 (and every watchlist clip) are copied as read-only files, SHA-256 hashed, and chained in `ledger.jsonl`. `GET /api/evidence/verify` detects any edit, deletion or re-ordering. The clip is also exempted from Frigate's retention cleanup. |
| Alerts | Telegram photo alert for scores ≥ 6. Instant ping when a person appears after hours. A daily summary at 21:30. |
| Ask the footage | Type "What did Ravi do near the till this week?" and Claude answers from the incident log, citing incident ids. |

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

**Two sites?** Your Tapo app lists the cameras under two homes ("SARKER MEDICAL
HALL" and "Office"). If those are different buildings on different networks, the
mini PC can't reach the second camera directly. You have two options:
- Put a Raspberry Pi (about A$80) at the second site running Tailscale as a
  [subnet router](https://tailscale.com/kb/1019/subnets), then use that camera's LAN IP as usual.
- Run a second copy of Shop Guard at the second site.

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
1. **Settings → Camera configuration → Masks / Zones.** Draw `cash_counter`, `s8_cabinet`, `stockroom` and `back_door`. The names must match `sensitive_zones` in `rules.yaml`.
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

## Legal (Australia; check your state)

- **Notice.** NSW (*Workplace Surveillance Act 2005*) requires 14 days' written notice
  to employees before camera surveillance starts, cameras clearly visible, and signs
  at the entrances. The ACT has similar rules. Covert surveillance of staff in NSW
  needs a magistrate's covert surveillance authority. Hidden cameras without one make
  your evidence a liability.
- **No audio.** Recording private conversations without consent is an offence under
  the state surveillance-devices Acts. Frigate is configured with
  `preset-record-generic`, which records video only. Keep it that way.
- **No cameras in toilets, change rooms or staff break rooms.** This is prohibited
  outright in NSW and VIC.
- **Face recognition.** Pharmacies are health service providers, so the *Privacy Act
  1988* applies whatever your turnover. Biometric data is "sensitive information". The
  OAIC found Bunnings in breach in 2024 for scanning customers' faces. So:
  - enroll **staff only**, with their written consent;
  - keep customer faces out of the Face Library;
  - consider lowering `save_attempts` in `frigate/config/config.yml`.

This is not legal advice. For a theft matter heading to police or dismissal, an hour
with an employment lawyer is cheap insurance.

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f sentinel` |
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
