# 3% Strategy — SPX Options Chain Viewer

Live at [henriksondergaard.github.io/3-percent-strategy](https://henriksondergaard.github.io/3-percent-strategy/)

A static SPX options chain viewer with a weekly credit-spread strategy panel. The
page is plain HTML/JS served from GitHub Pages and reads its options data **live
from Cloud Firestore** — no rebuilds, no commits on every refresh.

## Architecture

```
┌──────────────────────┐        ┌──────────────────┐        ┌───────────────┐
│  Always-on server     │        │  Cloud Firestore  │        │  GitHub Pages │
│  (Ubuntu, cron)        │ write  │  meta/expirations │  read  │  index.html   │
│  tradier_to_firebase  │───────▶│  meta/vix         │◀───────│  (live, via   │
│  Tradier live API      │        │  chains/<date>    │        │   Firebase    │
│  (chains+Greeks+VIX)   │        │                   │        │   web SDK)    │
└──────────────────────┘        └──────────────────┘        └───────────────┘

The economic calendar is still maintained by GitHub Actions and committed to the repo:
  fetch_calendar.py  -> data/calendar.json  (CPI/FOMC/NFP, weekly)
```

**Why Tradier + Firestore?** Tradier returns option **Greeks (delta, IV)**, which
the WEM formula needs and yfinance does not provide. Decoupling the data fetch
from the site build makes refreshes fast (Firestore pushes updates to open pages
in real time) and the data more trustworthy than the unofficial yfinance feed.

## WEM (Weekly Expected Move)

Computed per expiration, **asymmetric** to capture skew:

- **High side** uses the IV of the **call** whose delta is closest to **+0.15**.
- **Low side** uses the IV of the **put** whose delta is closest to **−0.15**.

```
WEM High = Spot × (IV_call@Δ0.15 / 100) × √(DTE/252)   → Upper = Spot + WEM High
WEM Low  = Spot × (IV_put@Δ0.15  / 100) × √(DTE/252)   → Lower = Spot − WEM Low
```

IV uses Tradier's `mid_iv`.

## File structure

```
index.html                  Frontend (single file, reads Firestore live)
config.js                   Firebase web config (public; safe to commit)
firestore.rules             Firestore security rules (public read, no client write)
tradier_to_firebase.py      Server script: Tradier chains+Greeks+VIX -> Firestore
requirements-tradier.txt    Deps for the server script (requests, firebase-admin)
fetch_calendar.py           Economic calendar -> data/calendar.json (GitHub Actions)
requirements.txt            Deps for fetch_calendar.py (beautifulsoup4, requests)
data/
  calendar.json             CPI/FOMC/NFP events
.github/workflows/
  update-calendar.yml       Updates the calendar weekly
```

## Setup

### 1. Firebase / Firestore

1. Create a Firebase project and enable **Cloud Firestore** (production mode).
2. Add a **Web app** in Project settings → copy the config object into `config.js`
   (replace the `REPLACE_ME` placeholders). This config is public and safe to commit.
3. Deploy the security rules (public read, writes only via Admin SDK):
   ```bash
   firebase deploy --only firestore:rules    # uses firestore.rules
   ```
4. Create a **service account** key: Project settings → Service accounts →
   *Generate new private key*. Save the JSON on the server (see below). **Never
   commit it** — `.gitignore` already blocks the common filenames.

### 2. The server script (Ubuntu, always on)

Requires a **Tradier** account with an API token. For real-time (not 15-min
delayed) data the account needs a market-data subscription; the script uses the
live endpoint `https://api.tradier.com/v1`.

```bash
git clone https://github.com/HenrikSondergaard/3-percent-strategy.git
cd 3-percent-strategy
python3 -m venv venv
./venv/bin/pip install -r requirements-tradier.txt

# Provide secrets via environment (e.g. an EnvironmentFile or wrapper script):
export TRADIER_TOKEN="your-tradier-token"
export GOOGLE_APPLICATION_CREDENTIALS="/home/you/secrets/serviceAccount.json"

# One-off run (nearest 20 expirations, includes weeklies/dailies)
./venv/bin/python tradier_to_firebase.py

# Other modes
./venv/bin/python tradier_to_firebase.py --nearest 5
./venv/bin/python tradier_to_firebase.py --expiration 2026-06-26
./venv/bin/python tradier_to_firebase.py --market-hours-only   # skip when closed
```

#### Cron (every 5 min during US market hours)

US equity/index options trade 09:30–16:15 ET ≈ 15:30–22:15 Europe/Stockholm.
With the cron timezone set to Europe/Stockholm:

```cron
*/5 15-22 * * 1-5  TRADIER_TOKEN=... GOOGLE_APPLICATION_CREDENTIALS=/home/you/secrets/serviceAccount.json \
  /home/you/3-percent-strategy/venv/bin/python /home/you/3-percent-strategy/tradier_to_firebase.py --market-hours-only \
  >> /home/you/tradier.log 2>&1
```

`--market-hours-only` queries Tradier's market clock and exits when the session is
closed, so off-hours ticks are cheap no-ops (and DST/holidays are handled).

### 3. GitHub Pages + calendar

- Enable GitHub Pages: Settings → Pages → Source `main` / root.
- The calendar workflow (`update-calendar.yml`) runs weekly with no extra setup.

## Local development

```bash
python3 -m http.server          # then open http://localhost:8000
```

With placeholder `config.js` the page loads and shows "Not configured". Once
`config.js` has a real Firebase config and Firestore has data, the chain appears
and updates live.

## License

[MIT](LICENSE)
