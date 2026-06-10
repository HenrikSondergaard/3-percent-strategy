#!/usr/bin/env python3
"""Fetch SPX options data via yfinance and write JSON files for GitHub Pages.

Usage:
    python3 fetch_data.py                        # fetch all expirations
    python3 fetch_data.py --nearest 5            # fetch only nearest 5 expirations
    python3 fetch_data.py --expiration 2026-06-12  # fetch only one expiration

Outputs:
    data/expirations.json          - list of expiration dates + timestamp
    data/chain_YYYY-MM-DD.json     - full options chain per expiration
"""

import json
import sys
import time
from datetime import datetime, timezone
from math import erf, exp, log, sqrt
from pathlib import Path

import yfinance as yf

TICKER = "^SPX"
RISK_FREE_RATE = 0.045
DATA_DIR = Path(__file__).parent / "data"
THROTTLE_SECONDS = 1.5  # delay between chain fetches to avoid Yahoo throttling


def main():
    max_expirations = None
    single_expiration = None
    if "--nearest" in sys.argv:
        idx = sys.argv.index("--nearest")
        max_expirations = int(sys.argv[idx + 1])
    if "--expiration" in sys.argv:
        idx = sys.argv.index("--expiration")
        single_expiration = sys.argv[idx + 1]

    DATA_DIR.mkdir(exist_ok=True)

    print(f"Fetching expirations for {TICKER}...")
    t = yf.Ticker(TICKER)
    all_expirations = list(t.options or [])
    print(f"  Found {len(all_expirations)} expirations")

    # Always update expirations.json with the full list
    exp_data = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "expirations": all_expirations,
    }
    exp_path = DATA_DIR / "expirations.json"
    with open(exp_path, "w") as f:
        json.dump(exp_data, f)
    print(f"  Wrote {exp_path}")

    if single_expiration:
        # Fetch only the requested expiration
        if single_expiration not in all_expirations:
            print(f"  ERROR: {single_expiration} is not a valid expiration")
            print(f"  Available: {all_expirations[:5]} ...")
            sys.exit(1)
        expirations = [single_expiration]
        print(f"  Single expiry mode: {single_expiration}")
    elif max_expirations:
        expirations = all_expirations[:max_expirations]
        print(f"  Limited to nearest {max_expirations}")
    else:
        expirations = all_expirations

    # Fetch chains
    for i, exp in enumerate(expirations):
        print(f"  [{i+1}/{len(expirations)}] Fetching {exp}...")
        try:
            chain_data = fetch_chain(t, exp)
            chain_path = DATA_DIR / f"chain_{exp}.json"
            with open(chain_path, "w") as f:
                json.dump(chain_data, f, default=str)
            print(f"    Wrote {chain_path} ({len(chain_data['rows'])} strikes)")
        except Exception as e:
            print(f"    ERROR fetching {exp}: {e}")

        # Throttle between requests
        if i < len(expirations) - 1:
            time.sleep(THROTTLE_SECONDS)

    # Clean up chain files for expirations no longer available (full runs only)
    if not single_expiration and not max_expirations:
        existing = set(all_expirations)
        for p in DATA_DIR.glob("chain_*.json"):
            exp_from_file = p.stem.replace("chain_", "")
            if exp_from_file not in existing:
                p.unlink()
                print(f"  Removed stale {p.name}")

    print("Done.")


def fetch_chain(ticker, expiration: str) -> dict:
    chain = ticker.option_chain(expiration)
    calls = chain.calls
    puts = chain.puts

    spot = approx_spot(calls, puts)

    # Time to expiry in years
    exp_date = datetime.strptime(expiration, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ttm = max((exp_date - datetime.now(timezone.utc)).total_seconds() / (365.25 * 86400), 1e-6)

    all_strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))

    rows = []
    for strike in all_strikes:
        c = row_to_dict(calls, strike, spot, ttm, is_call=True)
        p = row_to_dict(puts, strike, spot, ttm, is_call=False)
        rows.append({"strike": strike, "call": c, "put": p})

    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "spot": spot, "expiration": expiration, "ttm": round(ttm, 6), "rows": rows}


def approx_spot(calls, puts):
    """Estimate spot from put-call parity midpoint."""
    merged = calls[["strike", "lastPrice"]].copy()
    merged.columns = ["strike", "c_last"]
    p_last = puts[["strike", "lastPrice"]].copy()
    p_last.columns = ["strike", "p_last"]
    merged = merged.merge(p_last, on="strike", how="inner")
    if len(merged) == 0:
        return calls["strike"].median()
    merged["mid"] = merged["strike"] + merged["c_last"] - merged["p_last"]
    return round(float(merged["mid"].median()), 2)


def row_to_dict(df, strike, spot, ttm, is_call):
    match = df[df["strike"] == strike]
    if len(match) == 0:
        return None
    r = match.iloc[0]

    iv_decimal = safe_float(r.get("impliedVolatility"))
    delta = bs_delta(spot, strike, ttm, iv_decimal, is_call) if iv_decimal else None

    return {
        "bid": safe_float(r.get("bid")),
        "ask": safe_float(r.get("ask")),
        "last": safe_float(r.get("lastPrice")),
        "volume": safe_float(r.get("volume"), 0),
        "oi": safe_float(r.get("openInterest"), 0),
        "iv": round(iv_decimal * 100, 2) if iv_decimal else None,
        "delta": delta,
        "itm": bool(r.get("inTheMoney", False)),
    }


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


def bs_delta(spot, strike, ttm, iv, is_call):
    if not all([spot, strike, ttm, iv]) or ttm <= 0 or iv <= 0:
        return None
    try:
        d1 = (log(spot / strike) + (RISK_FREE_RATE + 0.5 * iv * iv) * ttm) / (
            iv * sqrt(ttm)
        )
        nd1 = 0.5 * (1.0 + erf(d1 / sqrt(2.0)))
        return round(nd1 if is_call else nd1 - 1.0, 4)
    except (ValueError, ZeroDivisionError):
        return None


if __name__ == "__main__":
    main()
