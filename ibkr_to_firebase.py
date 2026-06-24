#!/usr/bin/env python3
"""Stream real-time SPX options (with Greeks) from Interactive Brokers and write
them to Cloud Firestore, in the exact same schema as tradier_to_firebase.py.

Why IBKR instead of Tradier: Tradier delays SPX *index* data ~15 minutes unless you
hold the right real-time entitlement. With a funded IBKR account, the OPRA option
subscription and a signed Market Data API Acknowledgement, IBKR delivers real-time
option quotes + model Greeks through the TWS API.

This is a PERSISTENT service (not a cron one-shot): the TWS API needs a logged-in
IB Gateway to connect to, and re-subscribing thousands of times is wasteful and
slow. So we connect once, hold a small set of streaming subscriptions (kept well
under the free 100 market-data-line limit), and flush a Firestore snapshot every
PUBLISH_INTERVAL seconds. Run it under systemd with Restart=always; point it at the
Dockerised gateway's local API port.

Architecture & schema details live in store.py and README.md.

Configuration (environment variables):
    GOOGLE_APPLICATION_CREDENTIALS  Firebase service-account JSON (required)
    IB_HOST            Gateway host (default 127.0.0.1)
    IB_PORT            Gateway API port (default 4001 live; 4002 paper)
    IB_CLIENT_ID       TWS API client id (default 7)
    IBKR_SYMBOL        Underlying (default SPX)
    IBKR_EXCHANGE      Index exchange (default CBOE)
    IBKR_WEEKS         How many upcoming weekly expirations to cover (default 1)
    IBKR_DELTA_FLOOR   Walk each OTM wing out to ~this |delta| (default 0.10) so the
                       Δ0.15 short strikes sit comfortably inside both wings.
    IBKR_BAND_POINTS   Safety clamp on the strike half-window, in index points
                       (default 300). Bounds the delta-driven walk; also the
                       cold-start window before greeks have arrived.
    IBKR_MAX_LINES     Hard cap on simultaneous market-data lines (default 90)
    PUBLISH_INTERVAL   Seconds between Firestore flushes (default 5). Data streams in
                       continuously; this is only the snapshot cadence, not a poll rate.
    IGNORE_MARKET_HOURS  If set to 1, publish even when the US market is closed

Usage:
    python3 ibkr_to_firebase.py            # run forever (service mode)
    python3 ibkr_to_firebase.py --once     # one publish cycle then exit (debugging)
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from store import (
    approx_spot,
    build_chain_doc,
    cleanup_stale,
    init_firestore,
    itm_flag,
    now_iso,
    option_dict,
    safe_float,
    write_chain,
    write_expirations,
    write_vix,
)

# Imported lazily-tolerant: the pure helpers below stay importable (and unit-testable)
# even where ib_async isn't installed. The service entry points check for it at runtime.
try:
    from ib_async import IB, Index, Option, util
except ImportError:  # pragma: no cover
    IB = Index = Option = util = None


def _require_ib_async():
    if IB is None:
        sys.exit("Missing dependency: pip install ib_async  (see requirements-ibkr.txt)")

# ── Configuration ────────────────────────────────────────────────────────────
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4001"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "7"))
SYMBOL = os.environ.get("IBKR_SYMBOL", "SPX")
EXCHANGE = os.environ.get("IBKR_EXCHANGE", "CBOE")
WEEKS = int(os.environ.get("IBKR_WEEKS", "1"))
BAND_POINTS = float(os.environ.get("IBKR_BAND_POINTS", "300"))
MAX_LINES = int(os.environ.get("IBKR_MAX_LINES", "90"))
# OTM |delta| each wing is walked out to. The delta-aware walk (select_strikes_by_delta)
# replaces symmetric point selection so the put wing runs deeper than the call wing to
# match SPX vol skew; BAND_POINTS is only the safety clamp around it.
DELTA_FLOOR = float(os.environ.get("IBKR_DELTA_FLOOR", "0.10"))
# Seconds between Firestore flushes. Data streams from IBKR continuously; this only sets
# how often we write a snapshot (the frontend gets it live via onSnapshot). ~5s is
# near-real-time and still fits the free Firestore tier — market-hours-only writes are
# ~14.6k/day (3 docs × 405 min × 60 / 5) < the 20k/day limit. Don't drop below ~3s
# (Firestore allows ~1 write/s per document) without enabling Blaze billing.
PUBLISH_INTERVAL = int(os.environ.get("PUBLISH_INTERVAL", "5"))
IGNORE_MARKET_HOURS = os.environ.get("IGNORE_MARKET_HOURS") == "1"
# Bootstrap spot for strike selection when the SPX index isn't ticking (no index
# subscription). Only needed for the first cycle — parity self-corrects after that.
SPOT_HINT = float(os.environ.get("IBKR_SPOT_HINT", "0")) or None

NY = ZoneInfo("America/New_York")
RECONNECT_BACKOFF = [5, 15, 30, 60, 120]  # seconds, then capped at the last value


# ── Pure helpers (no IB connection needed — unit-testable) ────────────────────
def _pos(v):
    """Keep a non-negative number, else None. IBKR uses -1 / NaN for 'no data'."""
    f = safe_float(v)
    return f if (f is not None and f >= 0) else None


def _mid(ticker):
    """Bid-ask midpoint (real-time, synchronised); falls back to last."""
    b, a = _pos(ticker.bid), _pos(ticker.ask)
    if b is not None and a is not None:
        return round((b + a) / 2, 4)
    return _pos(ticker.last)


def target_expirations(available: set[str], today: date, weeks: int) -> list[str]:
    """Pick the last trading day (≈ Friday) of each of the next `weeks` weeks.

    `available` is the set of YYYYMMDD strings IBKR lists. For each upcoming week we
    take the latest available date within Mon–Fri of that week, which gracefully
    handles holidays (Friday closed -> Thursday) and mid-week starts.
    """
    avail = sorted(datetime.strptime(d, "%Y%m%d").date() for d in available)
    avail = [d for d in avail if d >= today]
    out: list[date] = []
    offset = 0
    while len(out) < weeks and offset < weeks + 5:
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7 * offset)
        friday = monday + timedelta(days=4)
        cands = [d for d in avail if monday <= d <= friday]
        if cands:
            out.append(max(cands))
        offset += 1
    return [d.strftime("%Y%m%d") for d in out]


def select_strikes(strikes, spot: float, band: float, max_count: int) -> list[float]:
    """Strikes within ±band of spot, the `max_count` closest to spot, sorted asc.

    Bootstrap / fallback selection. `select_strikes_by_delta` is the primary path
    once greeks are flowing — this one only runs on a cold start (no delta data)."""
    in_band = [s for s in strikes if abs(s - spot) <= band]
    closest = sorted(in_band, key=lambda s: abs(s - spot))[:max_count]
    return sorted(closest)


def _walk_wing(side_strikes, known: dict, floor: float, extend: int) -> list[float]:
    """Strikes to keep on one OTM wing, given `side_strikes` ordered ATM -> deep OTM.

    Keep everything out to the measured edge, stopping early at the first strike whose
    |delta| drops below `floor` (kept, for one strike of margin). If the wing reaches
    the edge of what we've measured while still above the floor, tack on up to `extend`
    further-OTM strikes so the next cycle measures them — this is how a wing grows
    outward toward `floor`. `known` is {strike: |delta|} for measured strikes."""
    measured = [i for i, s in enumerate(side_strikes) if s in known]
    last_measured = measured[-1] if measured else -1
    keep: list[float] = []
    for i, s in enumerate(side_strikes):
        if i <= last_measured:
            keep.append(s)
            d = known.get(s)
            if d is not None and d < floor:
                return keep           # bracketed the floor inside the measured range
            continue
        keep.extend(side_strikes[i:i + extend])   # past the edge, still rich — extend
        break
    return keep


def select_strikes_by_delta(strikes, spot: float, otm_delta: dict, floor: float,
                            max_count: int, band: float, extend: int = 10) -> list[float]:
    """Skew-aware OTM strike window: walk each wing out to ~`floor` |delta|.

    `select_strikes` picks the strikes closest to spot in POINTS, which over-covers
    the call wing and under-covers the put wing because SPX puts carry higher IV (vol
    skew): at equal point distance a put sits at a larger |delta| than a call. Here we
    walk the live delta curve (`otm_delta`: {strike: |delta| of the OTM leg}) out to the
    same delta on both wings instead, so the Δ0.15 short strikes sit comfortably inside
    each side. Falls back to the point band on a cold start (no greeks yet). Clamped to
    ±`band` and capped at `max_count`."""
    in_band = sorted(s for s in strikes if abs(s - spot) <= band)
    known = {s: otm_delta[s] for s in in_band if s in otm_delta}
    if len(known) < 4:                         # too little curve to judge — bootstrap
        return select_strikes(strikes, spot, band, max_count)

    puts = [s for s in reversed(in_band) if s < spot]    # ATM -> low  (OTM puts)
    calls = [s for s in in_band if s >= spot]            # ATM -> high (OTM calls)
    keep = set(_walk_wing(puts, known, floor, extend))
    keep |= set(_walk_wing(calls, known, floor, extend))
    keep |= set(sorted(in_band, key=lambda s: abs(s - spot))[:3])   # always hold ATM

    if len(keep) > max_count:
        # Over budget: keep the strikes nearest the money in DELTA terms. Unmeasured
        # extension strikes rank at the floor so a freshly grown wing isn't culled first.
        keep = set(sorted(keep, key=lambda s: known.get(s, floor), reverse=True)[:max_count])
    return sorted(keep)


def otm_right(strike, spot) -> str:
    """The out-of-the-money side at this strike: put below spot, call above."""
    return "P" if strike < spot else "C"


def leg_from_ticker(ticker, spot, strike, is_call: bool) -> dict | None:
    """Map a live ib_async option Ticker to the canonical option schema."""
    if ticker is None:
        return None
    g = ticker.modelGreeks
    iv = g.impliedVol if g else None
    delta = g.delta if g else None
    iv_pct = iv * 100 if (iv is not None and iv == iv) else None
    return option_dict(
        bid=_pos(ticker.bid),
        ask=_pos(ticker.ask),
        last=_pos(ticker.last),
        volume=_pos(ticker.volume),
        oi=_open_interest(ticker, is_call),
        iv_pct=iv_pct,
        delta=delta,  # may be negative for puts — do not clamp
        itm=itm_flag(strike, spot, is_call),
    )


def _open_interest(ticker, is_call: bool):
    for attr in (("callOpenInterest" if is_call else "putOpenInterest"), "openInterest"):
        v = getattr(ticker, attr, None)
        f = safe_float(v)
        if f:
            return f
    return 0


def fmt_exp(yyyymmdd: str) -> str:
    """IBKR '20260626' -> Firestore doc id / schema '2026-06-26'."""
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def market_open(now_ny: datetime) -> bool:
    """US index-options regular session: Mon–Fri, 09:30–16:15 ET (holidays ignored)."""
    if now_ny.weekday() >= 5:
        return False
    t = now_ny.time()
    return (now_ny.replace(hour=9, minute=30, second=0).time()
            <= t <= now_ny.replace(hour=16, minute=15, second=0).time())


# ── The service ──────────────────────────────────────────────────────────────
class Fetcher:
    def __init__(self, db):
        self.db = db
        self.ib = IB()
        self.ib.errorEvent += self._on_error
        self.idx = None          # qualified SPX index contract
        self.vix = None          # qualified VIX index contract
        self.idx_ticker = None
        self.vix_ticker = None
        self.params = []         # cached reqSecDefOptParams result
        self._strikes_cache = {} # (exp, tradingClass) -> sorted real listed strikes
        self.subs = {}           # (exp, strike, right) -> Ticker

    def _on_error(self, *a):
        # Tolerate ib_async signature drift (reqId, code, msg, contract[, ...]).
        # 2104/2106/2158 = data farm OK (info). Everything else is worth logging:
        # 354 = not subscribed, 10089/10167 = delayed-data notices, 502 = no gateway.
        code = a[1] if len(a) > 1 else None
        msg = a[2] if len(a) > 2 else ""
        # 2104/2106/2108/2158/2107/2119 = data-farm status (info).
        # 10090 = "part of market data not subscribed" — expected per option while
        # the SPX index isn't subscribed; would otherwise flood the log every cycle.
        if code not in (2104, 2106, 2108, 2158, 2107, 2119, 10090):
            print(f"  [IB {code}] {msg}", flush=True)

    # ── connection ──
    def connect(self):
        attempt = 0
        while True:
            try:
                self.ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=15)
                self.ib.reqMarketDataType(1)  # 1 = real-time
                print(f"Connected to {IB_HOST}:{IB_PORT} (clientId={IB_CLIENT_ID}); "
                      f"accounts={self.ib.managedAccounts()}", flush=True)
                return
            except Exception as e:
                wait = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                print(f"  connect failed ({e}); retrying in {wait}s", flush=True)
                self.ib.sleep(wait)
                attempt += 1

    def ensure_connected(self):
        if not self.ib.isConnected():
            print("Lost gateway connection — reconnecting and resubscribing.", flush=True)
            self._reset_state()
            self.connect()
            self.setup_underlyings()

    def _reset_state(self):
        # Tickers from the old session are dead; drop them so we resubscribe fresh.
        self.subs = {}
        self.params = []
        self._strikes_cache = {}
        self.idx = self.vix = self.idx_ticker = self.vix_ticker = None

    # ── subscriptions ──
    def setup_underlyings(self):
        self.idx = Index(SYMBOL, EXCHANGE, "USD")
        self.vix = Index("VIX", "CBOE", "USD")
        self.ib.qualifyContracts(self.idx, self.vix)
        self.idx_ticker = self.ib.reqMktData(self.idx, "", False, False)
        self.vix_ticker = self.ib.reqMktData(self.vix, "", False, False)
        self.params = self.ib.reqSecDefOptParams(
            self.idx.symbol, "", self.idx.secType, self.idx.conId
        )
        self.ib.sleep(2)

    def _chain_for(self, exp: str):
        """Pick the option-params entry that lists `exp`, preferring SPXW weeklies."""
        matches = [p for p in self.params if exp in p.expirations]
        if not matches:
            return None
        spxw = [p for p in matches if p.tradingClass == "SPXW"]
        return (spxw or matches)[0]

    def _listed_strikes(self, exp: str, chain):
        """Strikes ACTUALLY listed for this expiration (cached).

        reqSecDefOptParams returns a union of strikes across all expirations; many
        don't exist for a given expiry and would error. reqContractDetails on the
        specific expiration returns only the real ones.
        """
        key = (exp, chain.tradingClass)
        if key not in self._strikes_cache:
            cds = self.ib.reqContractDetails(
                Option(self.idx.symbol, exp, 0, "C", chain.exchange,
                       tradingClass=chain.tradingClass, currency="USD"))
            self._strikes_cache[key] = sorted({cd.contract.strike for cd in cds})
        return self._strikes_cache[key]

    def _spot(self) -> float | None:
        if self.idx_ticker is not None:
            for v in (self.idx_ticker.last, self.idx_ticker.close):
                f = safe_float(v)
                if f:
                    return f
        # Fallback: derive spot from current option mids via put-call parity.
        calls, puts = {}, {}
        for (exp, strike, right), t in self.subs.items():
            (calls if right == "C" else puts)[strike] = _mid(t)
        return approx_spot(calls, puts) if (calls and puts) else None

    def _otm_delta(self, exp: str, spot: float) -> dict:
        """{strike: |delta|} of the OTM leg for `exp`, from the live greeks. Drives the
        skew-aware strike walk; empty on the first cycle (then selection bootstraps by
        points)."""
        out = {}
        for (e, strike, right), t in self.subs.items():
            if e != exp or right != otm_right(strike, spot):
                continue
            g = t.modelGreeks
            if g and g.delta is not None and g.delta == g.delta:
                out[strike] = abs(g.delta)
        return out

    def ensure_subscriptions(self, spot: float):
        """Diff desired contracts against live subscriptions; add/cancel the delta."""
        targets = target_expirations(
            {e for p in self.params for e in p.expirations}, date.today(), WEEKS
        )
        if not targets:
            print("  no upcoming expirations available from IBKR", flush=True)
            return targets

        budget = max(MAX_LINES - 2, 2)  # reserve 2 lines for SPX + VIX
        # One line per strike (OTM leg only) buys ~twice the strikes; leave room for a
        # few ATM strikes we double up as the parity spot anchor (see below).
        per_exp = max(budget // len(targets) - 3, 1)

        desired = {}  # key -> Option contract
        for exp in targets:
            chain = self._chain_for(exp)
            if not chain:
                continue
            chosen = select_strikes_by_delta(
                self._listed_strikes(exp, chain), spot, self._otm_delta(exp, spot),
                DELTA_FLOOR, per_exp, BAND_POINTS,
            )
            # The 3 strikes nearest spot carry BOTH legs so put-call parity can price
            # spot — OTM-only legs never share a strike, which parity needs.
            anchors = set(sorted(chosen, key=lambda s: abs(s - spot))[:3])
            for k in chosen:
                # Elsewhere only the OTM leg is useful (and where the 0.15 short strikes
                # live): puts below spot, calls above. Skipping deep-ITM legs doubles
                # strike coverage for the same number of market-data lines.
                rights = ("C", "P") if k in anchors else (otm_right(k, spot),)
                for right in rights:
                    desired[(exp, k, right)] = Option(
                        self.idx.symbol, exp, k, right, chain.exchange,
                        tradingClass=chain.tradingClass, currency="USD",
                    )

        # Cancel lines we no longer want.
        for key in list(self.subs):
            if key not in desired:
                self.ib.cancelMktData(self.subs[key].contract)
                del self.subs[key]

        # Subscribe to new lines (qualify in one batch, then request streaming data).
        new_keys = [k for k in desired if k not in self.subs]
        if new_keys:
            contracts = [desired[k] for k in new_keys]
            self.ib.qualifyContracts(*contracts)
            added = []
            for key, c in zip(new_keys, contracts):
                if c.conId:  # qualified OK
                    self.subs[key] = self.ib.reqMktData(c, "101", False, False)
                    added.append(key)
            self._await_greeks([self.subs[k] for k in added])
        return targets

    def _await_greeks(self, tickers, timeout=30):
        """Model greeks arrive asynchronously after subscribing; wait until most are
        in before we publish, so we don't write a half-populated chain."""
        if not tickers:
            return
        for _ in range(timeout):
            ready = sum(1 for t in tickers if t.modelGreeks
                        and t.modelGreeks.delta is not None
                        and t.modelGreeks.delta == t.modelGreeks.delta)
            if ready >= 0.9 * len(tickers):
                return
            self.ib.sleep(1)

    # ── publish ──
    def publish_once(self):
        spot = self._spot()
        if spot is None:
            self.ib.sleep(2)            # give the index ticker a moment on cold start
            spot = self._spot() or SPOT_HINT
        if spot is None:
            print("  no spot yet (SPX index not ticking and no IBKR_SPOT_HINT) — "
                  "skipping; add an index subscription or set IBKR_SPOT_HINT to "
                  "bootstrap put-call parity", flush=True)
            return
        targets = self.ensure_subscriptions(spot)
        if not targets:
            return
        spot = self._spot() or spot  # refine now that option subscriptions exist

        published = []
        for exp in targets:
            rows = self._rows_for(exp, spot)
            if not rows:
                continue
            doc_id = fmt_exp(exp)
            write_chain(self.db, build_chain_doc(doc_id, spot, rows))
            published.append(doc_id)
            print(f"  wrote chains/{doc_id} ({len(rows)} strikes, spot={spot})", flush=True)

        if published:
            write_expirations(self.db, published)
            cleanup_stale(self.db, keep=set(published))

        vix = self._vix_doc()
        if vix:
            write_vix(self.db, vix)
            print(f"  VIX: {vix['vix']} ({vix['vix_change_pct']}%) -> meta/vix", flush=True)

    def _rows_for(self, exp: str, spot) -> list[dict]:
        by_strike = {}
        for (e, strike, right), t in self.subs.items():
            if e != exp:
                continue
            by_strike.setdefault(strike, {})[right] = t
        rows = []
        for strike in sorted(by_strike):
            legs = by_strike[strike]
            rows.append({
                "strike": strike,
                "call": leg_from_ticker(legs.get("C"), spot, strike, is_call=True),
                "put": leg_from_ticker(legs.get("P"), spot, strike, is_call=False),
            })
        return rows

    def _vix_doc(self):
        if self.vix_ticker is None:
            return None
        last = safe_float(self.vix_ticker.last) or safe_float(self.vix_ticker.close)
        if last is None:
            return None
        close = safe_float(self.vix_ticker.close)
        change_pct = round((last - close) / close * 100, 2) if close else None
        return {"vix": round(last, 2), "vix_change_pct": change_pct, "fetched_at": now_iso()}

    # ── loop ──
    def run(self, once: bool = False):
        self.connect()
        self.setup_underlyings()
        while True:
            self.ensure_connected()
            now_ny = datetime.now(NY)
            if IGNORE_MARKET_HOURS or market_open(now_ny):
                try:
                    self.publish_once()
                except Exception as e:
                    print(f"  publish error: {e}", flush=True)
            else:
                print(f"  market closed ({now_ny:%Y-%m-%d %H:%M %Z}) — skipping", flush=True)
            if once:
                return
            self.ib.sleep(PUBLISH_INTERVAL)


def main():
    ap = argparse.ArgumentParser(description="IBKR SPX options -> Firestore (streaming)")
    ap.add_argument("--once", action="store_true", help="one publish cycle then exit")
    args = ap.parse_args()

    _require_ib_async()
    db = init_firestore()
    fetcher = Fetcher(db)
    try:
        fetcher.run(once=args.once)
    except KeyboardInterrupt:
        pass
    finally:
        if fetcher.ib.isConnected():
            fetcher.ib.disconnect()


if __name__ == "__main__":
    if util is not None:
        util.logToConsole("ERROR")
    main()
