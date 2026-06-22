#!/usr/bin/env python3
"""Pre-flight diagnostic for the IBKR market-data setup. THROWAWAY — delete once
ibkr_to_firebase.py is confirmed working.

Run this FIRST, against a logged-in IB Gateway, to answer the one question the
research could not settle without your account:

    Does OPRA actually deliver REAL-TIME SPX options + index data to the API,
    or is something delayed?

It connects, requests real-time data (reqMarketDataType(1)) for the SPX index and
a few near-the-money SPXW options, and prints the ticks, the model Greeks, and —
critically — each ticker's `marketDataType`:

    1 = real-time (live)      <- what we want
    2 = frozen (last live close)
    3 = delayed (~15 min)     <- the problem we are escaping
    4 = delayed-frozen

It also prints every IB error/info message (e.g. 354 = "market data not
subscribed", 10089/10167 = delayed-data notices, 2104/2106/2158 = data farm OK).

Usage (on the Ubuntu box, gateway running):
    python3 ibkr_probe.py                      # live gateway on :4001
    python3 ibkr_probe.py --port 4002          # paper gateway
    python3 ibkr_probe.py --host 127.0.0.1 --client-id 11
"""

import argparse
from datetime import date

try:
    from ib_async import IB, Index, Option, util
except ImportError:
    raise SystemExit("Missing dependency: pip install ib_async  (see requirements-ibkr.txt)")


MDT = {1: "REAL-TIME", 2: "frozen", 3: "DELAYED (~15min)", 4: "delayed-frozen", 0: "unknown"}


def fmt(v):
    return "—" if v is None or v != v else round(v, 4)


def main():
    ap = argparse.ArgumentParser(description="IBKR real-time data pre-flight probe")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4001, help="4001=live, 4002=paper")
    ap.add_argument("--client-id", type=int, default=11)
    ap.add_argument("--symbol", default="SPX")
    ap.add_argument("--exchange", default="CBOE", help="index exchange (SPX -> CBOE)")
    ap.add_argument("--settle", default=8, type=int, help="seconds to let ticks arrive")
    ap.add_argument("--spot", type=float, default=None,
                    help="current SPX level to center strike selection on "
                         "(helpful when the index isn't subscribed)")
    args = ap.parse_args()

    ib = IB()
    # Print every error/info the server sends — these codes are the real diagnosis.
    # Flexible signature: ib_async passes (reqId, code, msg, contract[, ...]).
    ib.errorEvent += lambda *a: print(f"  [IB {a[1]}] {a[2]}")

    print(f"Connecting to {args.host}:{args.port} (clientId={args.client_id}) ...")
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=15)
    print(f"  connected: server v{ib.client.serverVersion()}  account(s)={ib.managedAccounts()}")

    # 1 = real-time. If you are not entitled, the server downgrades and/or errors.
    ib.reqMarketDataType(1)

    # ── Underlying index ─────────────────────────────────────────────────────
    idx = Index(args.symbol, args.exchange, "USD")
    ib.qualifyContracts(idx)
    idx_t = ib.reqMktData(idx, "", False, False)
    ib.sleep(args.settle)
    spot = idx_t.last if (idx_t.last == idx_t.last) else idx_t.close
    print("\n── Underlying index ──────────────────────────────────────")
    print(f"  {args.symbol}: last={fmt(idx_t.last)} close={fmt(idx_t.close)} "
          f"-> spot≈{fmt(spot)}")
    print(f"  marketDataType = {idx_t.marketDataType} ({MDT.get(idx_t.marketDataType, '?')})")

    if spot is None or spot != spot:
        print("  WARNING: no index price. SPX index data may need a separate index "
              "subscription (Cboe/S&P). Spot can be derived via put-call parity instead.")
        spot = 5400.0  # placeholder so we can still probe a few option strikes

    # ── Option chain params ────────────────────────────────────────────────────
    params = ib.reqSecDefOptParams(idx.symbol, "", idx.secType, idx.conId)
    spxw = [p for p in params if p.tradingClass == "SPXW"] or params
    chain = spxw[0]
    today = date.today().strftime("%Y%m%d")
    exps = sorted(e for e in chain.expirations if e >= today)
    if not exps:
        print("\n  ERROR: no expirations returned from reqSecDefOptParams.")
        ib.disconnect()
        return
    exp = exps[0]
    print("\n── Options (SPXW) ────────────────────────────────────────")
    print(f"  expiration={exp}  tradingClass={chain.tradingClass}  exchange={chain.exchange}")

    # Enumerate the strikes ACTUALLY listed for THIS expiration. The union returned
    # by reqSecDefOptParams contains strikes that don't exist for every expiry, which
    # is what produced the Error 200 / "no security definition" above.
    cds = ib.reqContractDetails(
        Option(idx.symbol, exp, 0, "C", chain.exchange,
               tradingClass=chain.tradingClass, currency="USD"))
    listed = sorted({cd.contract.strike for cd in cds})
    if not listed:
        print("  ERROR: no strikes listed for this expiration.")
        ib.disconnect()
        return
    center = args.spot or (spot if (spot and spot == spot) else listed[len(listed) // 2])
    strikes = sorted(listed, key=lambda s: abs(s - center))[:3]
    print(f"  {len(listed)} strikes listed ({listed[0]:.0f}..{listed[-1]:.0f}); "
          f"probing nearest to {center:.0f}: {[f'{s:.0f}' for s in sorted(strikes)]}")

    contracts = [Option(idx.symbol, exp, k, right, chain.exchange,
                        tradingClass=chain.tradingClass, currency="USD")
                 for k in sorted(strikes) for right in ("C", "P")]
    ib.qualifyContracts(*contracts)
    contracts = [c for c in contracts if c.conId]   # keep only those that resolved
    if not contracts:
        print("  ERROR: none of the option contracts qualified.")
        ib.disconnect()
        return
    tickers = [ib.reqMktData(c, "", False, False) for c in contracts]
    ib.sleep(args.settle)

    for c, t in zip(contracts, tickers):
        g = t.modelGreeks
        iv = (g.impliedVol if g else None)
        delta = (g.delta if g else None)
        print(f"  {int(c.strike):>5} {c.right}  bid={fmt(t.bid):>7} ask={fmt(t.ask):>7} "
              f"last={fmt(t.last):>7} vol={fmt(t.volume):>6}  "
              f"delta={fmt(delta):>7} iv={fmt(iv):>7}  mdt={t.marketDataType}({MDT.get(t.marketDataType,'?')})")

    # ── Verdict ────────────────────────────────────────────────────────────────
    opt_live = any(t.marketDataType == 1 for t in tickers)
    idx_live = idx_t.marketDataType == 1
    print("\n── Verdict ───────────────────────────────────────────────")
    print(f"  SPXW options real-time? {'YES' if opt_live else 'NO — check OPRA / acknowledgement'}")
    print(f"  SPX index real-time?    {'YES' if idx_live else 'NO — add index sub OR use put-call parity for spot'}")
    print("  (marketDataType must read 1 for the data to be live, not delayed.)")

    ib.disconnect()


if __name__ == "__main__":
    util.logToConsole("ERROR")
    main()
