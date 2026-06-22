#!/usr/bin/env python3
"""Fetch SPX options data (with Greeks) from Tradier and write it to Cloud Firestore.

This replaces the old yfinance + GitHub Actions pipeline. It is meant to run on an
always-on machine (e.g. an Ubuntu box) via cron, so the website can read fresh data
straight from Firestore without rebuilding/committing anything.

Why Tradier instead of yfinance:
  * Tradier returns option Greeks (delta, IV) directly, which the WEM formula needs.
  * Faster and more trustworthy than the unofficial yfinance interface.

Firestore layout (read by index.html):
    meta/expirations            -> { expirations: [...], fetched_at }
    chains/<YYYY-MM-DD>          -> { spot, expiration, ttm, fetched_at, rows: [...] }

Each row:
    { strike, call: <opt|null>, put: <opt|null> }
Each opt:
    { bid, ask, last, volume, oi, iv (pct), delta, itm }

Configuration (environment variables, never hard-code secrets):
    TRADIER_TOKEN                  Tradier *live* API access token (required)
    GOOGLE_APPLICATION_CREDENTIALS Path to a Firebase service-account JSON (required)
    TRADIER_SYMBOL                 Underlying symbol (default: SPX)
    MAX_EXPIRATIONS                Nearest N expirations to fetch (default: 20)

Usage:
    python3 tradier_to_firebase.py                     # nearest MAX_EXPIRATIONS
    python3 tradier_to_firebase.py --nearest 5         # nearest 5 expirations
    python3 tradier_to_firebase.py --expiration 2026-06-26   # one expiration only
    python3 tradier_to_firebase.py --market-hours-only # skip run when market closed
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import requests

# firebase_admin is imported lazily in init_firestore() so the data-transform code
# stays importable/testable without the dependency installed.

# ── Configuration ────────────────────────────────────────────────────────────
BASE_URL = "https://api.tradier.com/v1"          # live (delayed without a data sub)
SYMBOL = os.environ.get("TRADIER_SYMBOL", "SPX")
MAX_EXPIRATIONS = int(os.environ.get("MAX_EXPIRATIONS", "20"))
THROTTLE_SECONDS = 0.4                            # pause between chain calls
TOKEN = os.environ.get("TRADIER_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)


# ── Tradier API ──────────────────────────────────────────────────────────────
def _get(path: str, params: dict) -> dict:
    resp = session.get(f"{BASE_URL}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_expirations(symbol: str) -> list[str]:
    """Available expiration dates for a symbol (ascending).

    includeAllRoots=true is required for SPX: without it Tradier returns only the
    monthly SPX expirations and omits the daily/weekly SPXW dates that the weekly
    strategy relies on.
    """
    data = _get(
        "/markets/options/expirations",
        {"symbol": symbol, "includeAllRoots": "true"},
    )
    exp = (data.get("expirations") or {}).get("date") or []
    return exp if isinstance(exp, list) else [exp]


def get_options_chain(symbol: str, expiration: str) -> list[dict]:
    """Full options chain (calls + puts) with Greeks for one expiration."""
    data = _get(
        "/markets/options/chains",
        {"symbol": symbol, "expiration": expiration, "greeks": "true"},
    )
    options = (data.get("options") or {}).get("option") or []
    return options if isinstance(options, list) else [options]


def get_spot(symbol: str) -> float | None:
    """Last price of the underlying index from Tradier quotes."""
    try:
        data = _get("/markets/quotes", {"symbols": symbol})
        q = (data.get("quotes") or {}).get("quote")
        if isinstance(q, list):
            q = q[0] if q else None
        if q:
            for key in ("last", "close", "prevclose"):
                v = safe_float(q.get(key))
                if v:
                    return v
    except Exception as e:
        print(f"  WARNING: could not fetch spot quote: {e}")
    return None


def market_is_open() -> bool:
    """True if the US market is in its regular session right now."""
    try:
        data = _get("/markets/clock", {})
        state = (data.get("clock") or {}).get("state")
        return state == "open"
    except Exception as e:
        print(f"  WARNING: could not fetch market clock: {e}")
        return True  # fail open: better to fetch than to silently skip


def get_vix() -> dict | None:
    """Current VIX level + daily % change from Tradier (CBOE Volatility Index)."""
    try:
        data = _get("/markets/quotes", {"symbols": "VIX"})
        q = (data.get("quotes") or {}).get("quote")
        if isinstance(q, list):
            q = q[0] if q else None
        if not q:
            return None
        last = safe_float(q.get("last")) or safe_float(q.get("prevclose"))
        if last is None:
            return None
        return {
            "vix": round(last, 2),
            "vix_change_pct": safe_float(q.get("change_percentage")),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"  WARNING: could not fetch VIX: {e}")
        return None


# ── Transform ────────────────────────────────────────────────────────────────
def safe_float(v, default=None):
    if v is None:
        return default
    try:
        val = float(v)
        if val != val:  # NaN
            return default
        return round(val, 4)
    except (ValueError, TypeError):
        return default


def build_option(o: dict | None, spot: float, is_call: bool) -> dict | None:
    if not o:
        return None
    g = o.get("greeks") or {}
    mid_iv = safe_float(g.get("mid_iv"))
    delta = safe_float(g.get("delta"))
    strike = safe_float(o.get("strike"))
    itm = (strike < spot) if is_call else (strike > spot)
    return {
        "bid": safe_float(o.get("bid")),
        "ask": safe_float(o.get("ask")),
        "last": safe_float(o.get("last")),
        "volume": safe_float(o.get("volume"), 0),
        "oi": safe_float(o.get("open_interest"), 0),
        "iv": round(mid_iv * 100, 2) if mid_iv else None,
        "delta": delta,
        "itm": bool(itm),
    }


def approx_spot(calls: dict, puts: dict) -> float | None:
    """Estimate spot via put-call parity midpoint when no quote is available."""
    mids = []
    for strike in set(calls) & set(puts):
        c_last = safe_float(calls[strike].get("last"))
        p_last = safe_float(puts[strike].get("last"))
        if c_last is not None and p_last is not None:
            mids.append(strike + c_last - p_last)
    if not mids:
        all_strikes = sorted(set(calls) | set(puts))
        return all_strikes[len(all_strikes) // 2] if all_strikes else None
    mids.sort()
    return round(mids[len(mids) // 2], 2)


def build_chain(symbol: str, expiration: str, spot: float | None) -> dict:
    options = get_options_chain(symbol, expiration)
    calls = {safe_float(o.get("strike")): o for o in options if o.get("option_type") == "call"}
    puts = {safe_float(o.get("strike")): o for o in options if o.get("option_type") == "put"}
    calls.pop(None, None)
    puts.pop(None, None)

    if spot is None:
        spot = approx_spot(calls, puts)

    exp_date = datetime.strptime(expiration, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ttm = max((exp_date - datetime.now(timezone.utc)).total_seconds() / (365.25 * 86400), 1e-6)

    strikes = sorted(set(calls) | set(puts))
    rows = [
        {
            "strike": k,
            "call": build_option(calls.get(k), spot, is_call=True),
            "put": build_option(puts.get(k), spot, is_call=False),
        }
        for k in strikes
    ]
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "spot": spot,
        "expiration": expiration,
        "ttm": round(ttm, 6),
        "rows": rows,
    }


# ── Firestore ────────────────────────────────────────────────────────────────
def init_firestore():
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        sys.exit("Missing dependency: pip install firebase-admin  (see requirements-tradier.txt)")

    if not firebase_admin._apps:
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
        else:
            # Falls back to GOOGLE_APPLICATION_CREDENTIALS / metadata server if set.
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return firestore.client()


def write_chain(db, chain: dict):
    db.collection("chains").document(chain["expiration"]).set(chain)


def write_expirations(db, expirations: list[str]):
    db.collection("meta").document("expirations").set(
        {
            "expirations": expirations,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def write_vix(db, vix: dict):
    db.collection("meta").document("vix").set(vix)


def cleanup_stale(db, keep: set[str]):
    """Delete chain docs we no longer maintain (expired / outside the window)."""
    for doc in db.collection("chains").stream():
        if doc.id not in keep:
            doc.reference.delete()
            print(f"  Removed stale chain {doc.id}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Tradier SPX options -> Firestore")
    parser.add_argument("--nearest", type=int, default=MAX_EXPIRATIONS,
                        help=f"fetch nearest N expirations (default {MAX_EXPIRATIONS})")
    parser.add_argument("--expiration", help="fetch only this expiration (YYYY-MM-DD)")
    parser.add_argument("--market-hours-only", action="store_true",
                        help="exit early if the US market is closed")
    args = parser.parse_args()

    if not TOKEN:
        sys.exit("ERROR: set TRADIER_TOKEN in the environment.")

    if args.market_hours_only and not market_is_open():
        print("Market closed — skipping run.")
        return

    db = init_firestore()

    # VIX gauge (cheap single quote) -> meta/vix
    vix = get_vix()
    if vix:
        write_vix(db, vix)
        print(f"  VIX: {vix['vix']} ({vix['vix_change_pct']}%) -> meta/vix")

    print(f"Fetching expirations for {SYMBOL}...")
    all_expirations = get_expirations(SYMBOL)
    print(f"  Found {len(all_expirations)} expirations")

    spot = get_spot(SYMBOL)
    print(f"  Spot ({SYMBOL}): {spot if spot is not None else 'unknown — will use parity'}")

    single = args.expiration
    if single:
        if single not in all_expirations:
            sys.exit(f"ERROR: {single} is not a valid expiration. "
                     f"Available: {all_expirations[:5]} ...")
        to_fetch = [single]
    else:
        to_fetch = all_expirations[: args.nearest]

    for i, exp in enumerate(to_fetch):
        print(f"  [{i + 1}/{len(to_fetch)}] {exp}...")
        try:
            chain = build_chain(SYMBOL, exp, spot)
            write_chain(db, chain)
            print(f"    wrote chains/{exp} ({len(chain['rows'])} strikes)")
        except Exception as e:
            print(f"    ERROR fetching {exp}: {e}")
        if i < len(to_fetch) - 1:
            time.sleep(THROTTLE_SECONDS)

    # Update the expiration list + clean up stale chains only on normal (full) runs.
    if not single:
        write_expirations(db, to_fetch)
        print(f"  wrote meta/expirations ({len(to_fetch)} dates)")
        cleanup_stale(db, keep=set(to_fetch))

    print("Done.")


if __name__ == "__main__":
    main()
