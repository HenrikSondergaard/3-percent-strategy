#!/usr/bin/env python3
"""Source-agnostic Firestore layer + the canonical options schema.

Both data fetchers import from here so the documents they write are byte-for-byte
identical regardless of the source (Tradier or IBKR). The frontend (index.html)
therefore never needs to know which feed produced the data.

Firestore layout (read by index.html):
    meta/expirations    -> { expirations: [...], fetched_at }
    meta/vix            -> { vix, vix_change_pct, fetched_at }
    chains/<YYYY-MM-DD> -> { spot, expiration, ttm, fetched_at, rows: [...] }

Each row:  { strike, call: <opt|null>, put: <opt|null> }
Each opt:  { bid, ask, last, volume, oi, iv (pct), delta, itm }
"""

import os
import sys
from datetime import datetime, timezone


def now_iso() -> str:
    """Current UTC time as an RFC 3339 string (the `fetched_at` value)."""
    return datetime.now(timezone.utc).isoformat()


# ── Schema helpers ───────────────────────────────────────────────────────────
def safe_float(v, default=None):
    """Cast to float rounded to 4 decimals; None/NaN/garbage -> default."""
    if v is None:
        return default
    try:
        val = float(v)
        if val != val:  # NaN
            return default
        return round(val, 4)
    except (ValueError, TypeError):
        return default


def itm_flag(strike, spot, is_call: bool) -> bool:
    """In-the-money test. Falls back to False when spot/strike is unknown."""
    if strike is None or spot is None:
        return False
    return bool((strike < spot) if is_call else (strike > spot))


def option_dict(*, bid, ask, last, volume, oi, iv_pct, delta, itm) -> dict:
    """Build one option leg in the canonical schema.

    Callers pass already-extracted values; `iv_pct` is implied volatility as a
    percentage (e.g. 14.23), not a fraction. Mirrors the original Tradier
    transform exactly: bid/ask/last/delta rounded to 4 dp, volume/oi default 0,
    iv rounded to 2 dp.
    """
    return {
        "bid": safe_float(bid),
        "ask": safe_float(ask),
        "last": safe_float(last),
        "volume": safe_float(volume, 0),
        "oi": safe_float(oi, 0),
        "iv": round(iv_pct, 2) if iv_pct else None,
        "delta": safe_float(delta),
        "itm": bool(itm),
    }


def approx_spot(call_last_by_strike: dict, put_last_by_strike: dict):
    """Estimate spot via put-call parity midpoint when no quote is available.

    Args are {strike: last_price} maps for calls and puts. Returns the median of
    (strike + call_last - put_last) over shared strikes, or the middle strike as a
    last resort.
    """
    mids = []
    for strike in set(call_last_by_strike) & set(put_last_by_strike):
        c_last = safe_float(call_last_by_strike[strike])
        p_last = safe_float(put_last_by_strike[strike])
        if c_last is not None and p_last is not None:
            mids.append(strike + c_last - p_last)
    if not mids:
        all_strikes = sorted(set(call_last_by_strike) | set(put_last_by_strike))
        return all_strikes[len(all_strikes) // 2] if all_strikes else None
    mids.sort()
    return round(mids[len(mids) // 2], 2)


def ttm_years(expiration: str) -> float:
    """Time-to-maturity in years for a YYYY-MM-DD expiration (floored > 0)."""
    exp_date = datetime.strptime(expiration, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    secs = (exp_date - datetime.now(timezone.utc)).total_seconds()
    return max(secs / (365.25 * 86400), 1e-6)


def build_chain_doc(expiration: str, spot, rows: list[dict]) -> dict:
    """Wrap rows into a `chains/<expiration>` document."""
    return {
        "fetched_at": now_iso(),
        "spot": spot,
        "expiration": expiration,
        "ttm": round(ttm_years(expiration), 6),
        "rows": rows,
    }


# ── Firestore ────────────────────────────────────────────────────────────────
def init_firestore():
    """Lazy-init firebase_admin and return a Firestore client.

    Uses GOOGLE_APPLICATION_CREDENTIALS (service-account JSON) when set, otherwise
    application-default credentials.
    """
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        sys.exit("Missing dependency: pip install firebase-admin")

    if not firebase_admin._apps:
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return firestore.client()


def write_chain(db, chain: dict):
    db.collection("chains").document(chain["expiration"]).set(chain)


def write_expirations(db, expirations: list[str]):
    db.collection("meta").document("expirations").set(
        {"expirations": expirations, "fetched_at": now_iso()}
    )


def write_vix(db, vix: dict):
    db.collection("meta").document("vix").set(vix)


def cleanup_stale(db, keep: set[str]):
    """Delete chain docs we no longer maintain (expired / outside the window)."""
    for doc in db.collection("chains").stream():
        if doc.id not in keep:
            doc.reference.delete()
            print(f"  Removed stale chain {doc.id}")
