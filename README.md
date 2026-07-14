# 3% Strategy — SPX Options Chain Viewer

Live at [henriksondergaard.github.io/3-percent-strategy](https://henriksondergaard.github.io/3-percent-strategy/)

A static SPX options chain viewer with a weekly credit-spread strategy panel. The
page is plain HTML/JS served from GitHub Pages and reads its options data **live
from Cloud Firestore** — no rebuilds, no commits on every refresh.

## Architecture

```
┌─────────────────────────────────────┐      ┌──────────────────┐      ┌───────────────┐
│  Always-on Ubuntu box                │      │  Cloud Firestore  │      │  GitHub Pages │
│                                       │ write│  meta/expirations │ read │  index.html   │
│  Docker: IB Gateway (gnzsnz/…)        │─────▶│  meta/vix         │◀─────│  (live, via   │
│    ▲ socket :4001                     │      │  chains/<date>    │      │   Firebase    │
│    │                                   │      │                   │      │   web SDK)    │
│  systemd: ibkr_to_firebase.py          │      └──────────────────┘      └───────────────┘
│    (ib_async streaming, flush ~5 s)    │
└─────────────────────────────────────┘

The economic calendar is still maintained by GitHub Actions and committed to the repo:
  fetch_calendar.py  -> data/calendar.json  (CPI/FOMC/NFP, weekly)
```

**Why IBKR + Firestore?** The strategy needs **real-time** SPX option quotes and
**Greeks (delta, IV)**. Tradier delays SPX *index* data ~15 minutes unless you hold
the right entitlement, so the feed was migrated to **Interactive Brokers**, which
delivers real-time data over the TWS API given a funded account, the OPRA option
subscription and a signed Market Data API Acknowledgement. Decoupling the data fetch
from the site build keeps refreshes fast (Firestore pushes updates to open pages in
real time).

The Firestore schema and write layer live in [`store.py`](store.py); both the IBKR
fetcher and the (kept-as-fallback) Tradier script import it, so the documents are
identical whichever feed produced them — the frontend never changes.

## WEM (Weekly Expected Move)

Computed per expiration, **asymmetric** to capture skew:

- **High side** uses the IV of the **call** whose delta is closest to **+0.15**.
- **Low side** uses the IV of the **put** whose delta is closest to **−0.15**.

```
WEM High = Spot × (IV_call@Δ0.15 / 100) × √(DTE/252)   → Upper = Spot + WEM High
WEM Low  = Spot × (IV_put@Δ0.15  / 100) × √(DTE/252)   → Lower = Spot − WEM Low
```

IV is the option's model implied volatility (IBKR `modelGreeks.impliedVol`, stored
as a percentage in the `iv` field).

## File structure

```
index.html                  Frontend (single file, reads Firestore live)
config.js                   Firebase web config (public; safe to commit)
firestore.rules             Firestore security rules (read requires sign-in, no client write)
store.py                    Shared: canonical options schema + Firestore writes
ibkr_to_firebase.py         PRIMARY server script: IBKR streaming -> Firestore
ibkr_probe.py               Pre-flight: confirm real-time data before deploying
requirements-ibkr.txt       Deps for the IBKR scripts (ib_async, firebase-admin)
docker-compose.yml          IB Gateway (headless) via gnzsnz/ib-gateway
.env.example                Gateway secrets template -> copy to .env
ibkr-fetcher.service        systemd unit for the fetcher
ibkr-fetcher.env.example    Fetcher env template -> copy to ibkr-fetcher.env
test_schema.py              Schema/transform tests (IBKR output == Tradier output)
tradier_to_firebase.py      FALLBACK server script: Tradier chains+Greeks -> Firestore
requirements-tradier.txt    Deps for the Tradier fallback (requests, firebase-admin)
fetch_calendar.py           Economic calendar -> data/calendar.json (GitHub Actions)
requirements.txt            Deps for fetch_calendar.py (beautifulsoup4, requests)
data/calendar.json          CPI/FOMC/NFP events
.github/workflows/update-calendar.yml   Updates the calendar weekly
```

## Team access

The dashboard is private: the page is gated by **Firebase Authentication (email/password)**
and the Firestore read rules require a signed-in user, so the data can't be read without an
account — not even directly from the API with the public web config.

**Add or remove a team member** (no code change or deploy needed):

1. Firebase console → **Authentication → Users**.
2. **Add user** (email + password) to grant access, or delete a user to revoke it.
3. Share the credentials with that person; they sign in at the live site.

The sign-in method (Email/Password) is enabled under **Authentication → Sign-in method**.
The matching read rule lives in [`firestore.rules`](firestore.rules) (`allow read: if
request.auth != null`); deploy rule changes with `firebase deploy --only firestore:rules`.

## Setup

### 1. Firebase / Firestore

1. Create a Firebase project and enable **Cloud Firestore** (production mode).
2. Add a **Web app** in Project settings → copy the config object into `config.js`
   (replace the `REPLACE_ME` placeholders). This config is public and safe to commit.
3. Deploy the security rules (read requires sign-in, writes only via Admin SDK — see
   [Team access](#team-access)):
   ```bash
   firebase deploy --only firestore:rules
   ```
4. Create a **service account** key: Project settings → Service accounts →
   *Generate new private key*. Save the JSON on the server. **Never commit it** —
   `.gitignore` already blocks the common filenames.

### 2. IBKR market-data prerequisites

On your IBKR **live** account (real-time entitlements don't apply to paper):

- Fund the account (IBKR requires ≈ $500 of equity to enable market-data subs).
- Subscribe to **OPRA** (US options) under User Settings → Market Data Subscriptions.
- Sign the **Market Data API Acknowledgement** (without it, API data requests error).
- SPX is an *index*: the real-time **index value** (spot) may need a separate index
  subscription (e.g. Cboe/S&P indices). If you skip it, the fetcher derives spot from
  option mids via put-call parity. The pre-flight probe (next step) tells you which.

### 3. Pre-flight probe (do this first)

Before wiring everything up, confirm the data is actually real-time. With the gateway
running (step 4) and the venv installed:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements-ibkr.txt
./venv/bin/python ibkr_probe.py            # live gateway on :4001
```

It prints each instrument's `marketDataType` — **1 = real-time**, **3 = delayed**.
The verdict lines tell you whether SPX options and the SPX index are live. If the
index is delayed, either add the index subscription or rely on parity (the fetcher
handles both). Delete `ibkr_probe.py` once you're satisfied.

### 4. IB Gateway (Docker, headless)

The TWS API needs a logged-in gateway to connect to. `gnzsnz/ib-gateway` bundles
IB Gateway + IBC + Xvfb and manages the daily auto-restart and 2FA timeout flow.

```bash
cp .env.example .env          # fill in TWS_USERID / TWS_PASSWORD (live), TIME_ZONE
docker compose up -d
docker compose logs -f        # watch the login; approve the IBKR Mobile 2FA push
```

- API is bound to `127.0.0.1:4001` (live) — only reachable from this box.
- With 2FA, the nightly `AUTO_RESTART_TIME` reuses the session; you re-approve the
  push roughly **weekly**. For the first login or troubleshooting, connect a VNC
  viewer to `127.0.0.1:5900`.
- `READ_ONLY_API=yes` blocks order placement over the API — we only read data.

### 5. The fetcher (systemd, always on)

```bash
cp ibkr-fetcher.env.example ibkr-fetcher.env   # set GOOGLE_APPLICATION_CREDENTIALS, scope
# edit ibkr-fetcher.service: User=, WorkingDirectory=, ExecStart=, EnvironmentFile= paths
sudo cp ibkr-fetcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ibkr-fetcher
journalctl -u ibkr-fetcher -f
```

The service connects once, holds a small set of streaming subscriptions (kept under
the free 100 market-data-line limit via `IBKR_WEEKS` / `IBKR_BAND_POINTS` /
`IBKR_MAX_LINES`). Strikes are chosen by **delta**, not by a symmetric point window:
each OTM wing is walked out to ~`IBKR_DELTA_FLOOR` |delta|, so the put wing runs
deeper than the call wing to match SPX vol skew and the Δ0.15 short strikes sit
comfortably inside both sides. Data streams in continuously; it flushes a Firestore
snapshot every `PUBLISH_INTERVAL` seconds (default 5 — the frontend gets it live via
`onSnapshot`). Writing only during US index-options hours (09:30–16:15 ET) keeps this
within the free Firestore tier (~14.6k writes/day < 20k); below ~3s enable Blaze. It
skips writes outside market hours unless `IGNORE_MARKET_HOURS=1`. **Remove the old
Tradier cron line** when you cut over.

Debug a single cycle without the loop:

```bash
IGNORE_MARKET_HOURS=1 ./venv/bin/python ibkr_to_firebase.py --once
```

#### Weekly Sunday re-authentication

IBKR invalidates the gateway's session token every **Sunday ~01:00 ET** (≈07:00
Europe/Stockholm). After that a fresh 2FA login is required once before the feed
works again; the rest of the week the nightly `AUTO_RESTART_TIME` reuses the token
silently (no 2FA). Left alone, that weekly 2FA first surfaces on the Sunday-night
auto-restart and, if unapproved overnight, leaves the feed down Monday morning.

`sunday_reauth.sh` forces the login attempt (and the IBKR Mobile push) at a civil
Sunday hour and re-sends it every 15 min until you approve — then it goes quiet for
the rest of the day. On any other day it only acts on a clear "connect failed"
signal, so a manual run against a live feed is a harmless no-op:

```bash
crontab -e
# add:
CRON_TZ=Europe/Stockholm
*/15 9-23 * * 0  cd /home/you/3-percent-strategy && bash sunday_reauth.sh >> /home/you/sunday_reauth.log 2>&1
```

Invoking it via `bash` (rather than the script path directly) means no execute bit
is needed, so a `git pull` never trips over a local `chmod +x`. The health check
reads the fetcher's journal: a live gateway prints "market closed"
every few seconds on a (closed) Sunday, a dead one only "connect failed". Pair it
with a feed-health alert so you're reminded to open the app.

### 6. Tradier fallback (optional)

`tradier_to_firebase.py` still works and writes the identical schema. Keep it as a
backstop if the gateway is down. See `requirements-tradier.txt`; it needs
`TRADIER_TOKEN` and the live endpoint (note: SPX index data is 15-min delayed without
a Tradier real-time subscription — the reason for the IBKR migration).

### 7. GitHub Pages + calendar

- Enable GitHub Pages: Settings → Pages → Source `main` / root.
- The calendar workflow (`update-calendar.yml`) runs weekly with no extra setup.

## Tests

```bash
python3 test_schema.py        # or: pytest test_schema.py
```

Pins the Firestore schema and proves the IBKR transform produces byte-for-byte the
same option dict as the Tradier transform on equal inputs.

## Local development

```bash
python3 -m http.server          # then open http://localhost:8000
```

With placeholder `config.js` the page loads and shows "Not configured". Once
`config.js` has a real Firebase config and Firestore has data, the chain appears
and updates live.

## License

[MIT](LICENSE)
