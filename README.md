# 3% Strategy — SPX Options Chain Viewer

Live at [henriksondergaard.github.io/3-percent-strategy](https://henriksondergaard.github.io/3-percent-strategy/)

A static, serverless SPX options chain viewer. Browse calls/puts across all expirations with implied volatility and Black-Scholes delta, all computed client-side. No backend, no API keys, no running costs.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  GitHub      │     │  GitHub Actions   │     │  Cloudflare     │
│  Pages       │     │  (fetch_data.py)  │     │  Worker         │
│              │     │                   │     │                 │
│  Serves      │◄────│  Fetches SPX data │     │  Proxies refresh│
│  static HTML │     │  via yfinance     │     │  button clicks  │
│  + JSON      │     │  Commits JSON     │     │  → triggers     │
│              │     │  to repo          │◄────│  workflow_dispatch│
└─────────────┘     └──────────────────┘     └─────────────────┘
       ▲                                               ▲
       │                                               │
       └───────────── Browser ─────────────────────────┘
                      fetches JSON,
                      triggers refresh via Worker
```

**Cost: $0/month.** Everything runs on free tiers.

| Component | Service | Purpose |
|---|---|---|
| Frontend | GitHub Pages | Static HTML/JS/CSS + JSON data files |
| Data pipeline | GitHub Actions | Scheduled + on-demand SPX data fetch |
| Refresh proxy | Cloudflare Workers | Securely triggers Actions (holds GitHub PAT) |

## Features

- Full SPX options chain (calls + puts) across all available expirations
- Strike filtering (10/20/50/100/200/All strikes from ATM)
- Implied volatility and Black-Scholes delta per contract
- ATM row highlighting
- Days-to-expiry (DTE) shown per expiration
- **Refresh Data** button fetches only the selected expiration (~30s)
- Auto-reload: page detects updated data and reloads the table automatically
- Scheduled data updates twice daily on weekdays (10:15 AM + 2:15 PM ET)

## File Structure

```
index.html                          Frontend (single file, no build step)
config.js                           Cloudflare Worker URL
fetch_data.py                       Data fetcher (yfinance → JSON)
requirements.txt                    Python dependencies
data/
  expirations.json                  List of all expiration dates + timestamp
  chain_YYYY-MM-DD.json             Full chain per expiration
worker/
  index.js                          Cloudflare Worker (refresh proxy)
  wrangler.toml                     Worker config
  package.json                      Worker npm deps
.github/workflows/
  update-data.yml                   GitHub Actions workflow
```

## Data Source

Options data comes from Yahoo Finance via [yfinance](https://github.com/ranaroussi/yfinance). SPX options are fetched using the `^SPX` ticker. This is an unofficial interface — no API key is required, but it may break if Yahoo changes their API.

Columns available per contract: bid, ask, last, volume, open interest, implied volatility, and computed Black-Scholes delta (using a 4.5% risk-free rate).

## Setup

### Prerequisites

- GitHub account (free)
- Cloudflare account (free, no credit card)
- One GitHub Personal Access Token with `repo` scope

### Deploy

1. **Fork or clone this repo** (must be public for free GitHub Pages)

2. **Enable GitHub Pages**: Settings → Pages → Source: `main` branch, `/ (root)`

3. **Deploy the Cloudflare Worker**:
   ```bash
   cd worker
   npm install
   npx wrangler login
   npx wrangler deploy
   ```
   Note the output URL (e.g. `https://spx-refresh.<your-subdomain>.workers.dev`).

4. **Set the GitHub PAT as a Worker secret**:
   ```bash
   npx wrangler secret put GITHUB_TOKEN
   ```

5. **Update `config.js`** with your Worker URL:
   ```js
   WORKER_URL = 'https://spx-refresh.<your-subdomain>.workers.dev';
   ```

6. **Trigger a data fetch** to populate the site:
   - Go to Actions → "Update SPX Options Data" → Run workflow

### Scheduled Updates

The workflow runs automatically twice daily on weekdays (10:15 AM and 2:15 PM ET). It fetches the nearest 20 expirations. To change this, edit the cron schedule in `.github/workflows/update-data.yml`.

### On-Demand Refresh

Clicking "Refresh Data" on the page fetches only the currently selected expiration (much faster than a full run). The page polls for updated data every 30 seconds and auto-reloads when fresh data appears.

You can also trigger a full refresh manually from the Actions tab with configurable inputs.

## Development

### Local testing

```bash
python3 -m http.server
# Open http://localhost:8000
```

### Fetch data locally

```bash
pip install -r requirements.txt

# All expirations
python3 fetch_data.py

# Nearest 5 only
python3 fetch_data.py --nearest 5

# Single expiration
python3 fetch_data.py --expiration 2026-06-12
```

## License

[MIT](LICENSE)
