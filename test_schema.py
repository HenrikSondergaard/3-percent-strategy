#!/usr/bin/env python3
"""Schema/transform tests — the safety net for the Tradier -> IBKR migration.

The whole point of store.py is that both fetchers emit byte-for-byte identical
Firestore documents. These tests pin that contract down, plus the IBKR-specific
pure helpers (expiration/strike selection, ticker mapping, market-hours gate).

Run standalone (no pytest needed):   python3 test_schema.py
Or with pytest:                       pytest test_schema.py
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import store
import ibkr_to_firebase as ibkr


# ── Fakes mimicking an ib_async option Ticker ────────────────────────────────
class FakeGreeks:
    def __init__(self, delta, iv):
        self.delta = delta
        self.impliedVol = iv


class FakeTicker:
    def __init__(self, bid, ask, last, volume, delta=None, iv=None,
                 call_oi=None, put_oi=None, oi=None):
        self.bid, self.ask, self.last, self.volume = bid, ask, last, volume
        self.modelGreeks = FakeGreeks(delta, iv) if (delta is not None or iv is not None) else None
        self.callOpenInterest, self.putOpenInterest, self.openInterest = call_oi, put_oi, oi


# Reproduce the Tradier transform locally (build_option without importing the
# requests-dependent module), so we can assert IBKR == Tradier on equal inputs.
def tradier_build_option(o, spot, is_call):
    g = o.get("greeks") or {}
    mid_iv = store.safe_float(g.get("mid_iv"))
    strike = store.safe_float(o.get("strike"))
    return store.option_dict(
        bid=o.get("bid"), ask=o.get("ask"), last=o.get("last"),
        volume=o.get("volume"), oi=o.get("open_interest"),
        iv_pct=(mid_iv * 100 if mid_iv else None),
        delta=g.get("delta"), itm=store.itm_flag(strike, spot, is_call),
    )


# ── store: option_dict schema ────────────────────────────────────────────────
def test_option_dict_fields_and_rounding():
    d = store.option_dict(bid=1.234567, ask=1.3, last=1.25, volume=10, oi=500,
                          iv_pct=14.234, delta=0.8345, itm=True)
    assert set(d) == {"bid", "ask", "last", "volume", "oi", "iv", "delta", "itm"}
    assert d["bid"] == 1.2346          # 4 dp
    assert d["iv"] == 14.23            # 2 dp
    assert d["delta"] == 0.8345
    assert d["itm"] is True


def test_option_dict_defaults():
    d = store.option_dict(bid=None, ask=None, last=None, volume=None, oi=None,
                          iv_pct=None, delta=None, itm=False)
    assert d["volume"] == 0 and d["oi"] == 0     # default to 0
    assert d["bid"] is None and d["iv"] is None and d["delta"] is None


def test_safe_float_handles_nan_and_garbage():
    assert store.safe_float(float("nan")) is None
    assert store.safe_float("x", 0) == 0
    assert store.safe_float(None) is None
    assert store.safe_float("3.14159") == 3.1416


def test_itm_flag():
    assert store.itm_flag(5400, 5430, is_call=True) is True
    assert store.itm_flag(5400, 5430, is_call=False) is False
    assert store.itm_flag(5400, None, is_call=True) is False   # unknown spot -> False


# ── The core guarantee: IBKR transform == Tradier transform ──────────────────
def test_ibkr_call_matches_tradier_call():
    spot, strike = 5430.0, 5400.0
    tradier = tradier_build_option(
        {"strike": strike, "bid": 31.55, "ask": 32.05, "last": 31.80,
         "volume": 123, "open_interest": 4567, "greeks": {"mid_iv": 0.1423, "delta": 0.834}},
        spot, is_call=True)
    t = FakeTicker(bid=31.55, ask=32.05, last=31.80, volume=123, delta=0.834,
                   iv=0.1423, call_oi=4567)
    assert ibkr.leg_from_ticker(t, spot, strike, is_call=True) == tradier


def test_ibkr_put_matches_tradier_put():
    spot, strike = 5430.0, 5500.0
    tradier = tradier_build_option(
        {"strike": strike, "bid": 0.05, "ask": 0.10, "last": 0.07,
         "volume": 89, "open_interest": 2345, "greeks": {"mid_iv": 0.0856, "delta": -0.051}},
        spot, is_call=False)
    t = FakeTicker(bid=0.05, ask=0.10, last=0.07, volume=89, delta=-0.051,
                   iv=0.0856, put_oi=2345)
    leg = ibkr.leg_from_ticker(t, spot, strike, is_call=False)
    assert leg == tradier
    assert leg["delta"] == -0.051 and leg["itm"] is True   # put ITM above spot


def test_ibkr_missing_data_sanitised():
    # IBKR uses -1 / NaN for "no data"; those must become None / 0, not leak through.
    t = FakeTicker(bid=-1.0, ask=float("nan"), last=0.0, volume=-1, delta=None, iv=None)
    leg = ibkr.leg_from_ticker(t, 5430.0, 5400.0, is_call=True)
    assert leg["bid"] is None and leg["ask"] is None
    assert leg["last"] == 0.0            # a real 0 last is kept
    assert leg["volume"] == 0
    assert leg["delta"] is None and leg["iv"] is None


def test_leg_from_ticker_none():
    assert ibkr.leg_from_ticker(None, 5430.0, 5400.0, is_call=True) is None


# ── IBKR expiration / strike selection ───────────────────────────────────────
def test_target_expirations_picks_friday():
    avail = {"20260622", "20260623", "20260624", "20260625", "20260626", "20260703"}
    monday = date(2026, 6, 22)
    assert ibkr.target_expirations(avail, monday, 1) == ["20260626"]
    assert ibkr.target_expirations(avail, monday, 2) == ["20260626", "20260703"]


def test_target_expirations_handles_holiday():
    # Friday 26th missing (holiday) -> fall back to Thursday 25th, the week's last day.
    avail = {"20260622", "20260623", "20260624", "20260625", "20260703"}
    assert ibkr.target_expirations(avail, date(2026, 6, 22), 1) == ["20260625"]


def test_target_expirations_rolls_forward_on_expiry_day():
    # Friday 26th is a holiday, so Thursday 25th is the week's last trading day.
    avail = {"20260622", "20260623", "20260624", "20260625",
             "20260629", "20260630", "20260701", "20260702", "20260703"}
    # Day before expiry: still this week's last day.
    assert ibkr.target_expirations(avail, date(2026, 6, 24), 1) == ["20260625"]
    # ON expiry day -> roll to next week's last trading day (Fri 3rd).
    assert ibkr.target_expirations(avail, date(2026, 6, 25), 1) == ["20260703"]
    # After expiry (Fri, market already moved on) -> next week too.
    assert ibkr.target_expirations(avail, date(2026, 6, 26), 1) == ["20260703"]


def test_select_strikes_band_and_count():
    strikes = [5300, 5350, 5400, 5450, 5500]
    # spot 5430, ±150 keeps all; closest 3 are 5450, 5400, 5500 -> sorted asc.
    assert ibkr.select_strikes(strikes, 5430, 150, 3) == [5400, 5450, 5500]
    # tight band drops the far ones.
    assert ibkr.select_strikes(strikes, 5430, 25, 10) == [5450]


# ── Delta-aware (skew) strike selection ──────────────────────────────────────
def _skew_otm_delta(measured, spot):
    """Synthetic OTM |delta| curve with downside skew: at equal distance a put sits
    at a larger |delta| than a call (puts decay slower)."""
    d = {}
    for s in measured:
        if s < spot:
            d[s] = round(0.5 - (spot - s) * 0.03, 4)              # puts: gentle slope
        else:
            d[s] = round(max(0.5 - (s - spot) * 0.05, 0.01), 4)   # calls: steeper
    return d


def test_select_by_delta_cold_start_falls_back_to_points():
    listed = list(range(60, 141))
    # No greeks yet -> identical to the point-band bootstrap.
    assert (ibkr.select_strikes_by_delta(listed, 100, {}, 0.10, 100, 40)
            == ibkr.select_strikes(listed, 100, 40, 100))


def test_select_by_delta_trims_calls_and_extends_puts():
    listed = list(range(60, 141))
    measured = range(88, 113)                       # the currently-subscribed window
    otm = _skew_otm_delta(measured, 100)
    out = ibkr.select_strikes_by_delta(listed, 100, otm, 0.10, 100, 40, extend=4)
    # Call wing stops at the floor (109 = 0.05) — deeper measured calls are dropped.
    assert 109 in out and 110 not in out and 112 not in out
    # Put wing grows past the measured edge (88) by `extend` so next cycle learns them.
    assert 88 in out and 84 in out and 83 not in out
    # Skew asymmetry: the put wing reaches further from spot than the call wing.
    assert (100 - min(out)) > (max(out) - 100)
    assert out == sorted(set(out))


def test_select_by_delta_caps_to_budget_nearest_in_delta():
    listed = list(range(60, 141))
    otm = _skew_otm_delta(range(88, 113), 100)
    out = ibkr.select_strikes_by_delta(listed, 100, otm, 0.10, 8, 40, extend=4)
    assert len(out) == 8
    assert 100 in out                # ATM kept
    assert 84 not in out and 109 not in out   # deep wings culled first


def test_select_by_delta_clamped_to_band():
    listed = list(range(60, 141))
    otm = _skew_otm_delta(range(88, 113), 100)
    out = ibkr.select_strikes_by_delta(listed, 100, otm, 0.10, 100, 5, extend=4)
    assert min(out) == 95 and max(out) == 105   # never selects or extends past ±band


def test_walk_wing_extends_when_still_rich():
    # ATM -> OTM order; measured to 96, still above floor -> extend by 3.
    side = [99, 98, 97, 96, 95, 94, 93, 92]
    known = {99: 0.40, 98: 0.34, 97: 0.28, 96: 0.22}
    assert ibkr._walk_wing(side, known, 0.10, 3) == [99, 98, 97, 96, 95, 94, 93]


def test_walk_wing_stops_at_floor_inside_measured():
    side = [99, 98, 97, 96, 95, 94]
    known = {99: 0.40, 98: 0.22, 97: 0.12, 96: 0.08, 95: 0.05}
    # 96 dips below 0.10 -> kept for margin, then stop (deeper 95 dropped).
    assert ibkr._walk_wing(side, known, 0.10, 3) == [99, 98, 97, 96]


def test_fmt_exp():
    assert ibkr.fmt_exp("20260626") == "2026-06-26"


def test_otm_right():
    # Puts are the OTM leg below spot, calls above — so we never subscribe deep ITM.
    assert ibkr.otm_right(7400, 7477) == "P"
    assert ibkr.otm_right(7500, 7477) == "C"
    assert ibkr.otm_right(7477, 7477) == "C"


def test_market_open_gate():
    ny = ZoneInfo("America/New_York")
    assert ibkr.market_open(datetime(2026, 6, 22, 10, 0, tzinfo=ny)) is True    # Mon 10:00
    assert ibkr.market_open(datetime(2026, 6, 22, 16, 30, tzinfo=ny)) is False  # after close
    assert ibkr.market_open(datetime(2026, 6, 22, 9, 0, tzinfo=ny)) is False    # pre-open
    assert ibkr.market_open(datetime(2026, 6, 20, 11, 0, tzinfo=ny)) is False   # Saturday


def test_chain_doc_shape():
    doc = store.build_chain_doc("2026-06-26", 5430.0, rows=[{"strike": 5400}])
    assert set(doc) == {"fetched_at", "spot", "expiration", "ttm", "rows"}
    assert doc["expiration"] == "2026-06-26" and doc["spot"] == 5430.0
    assert doc["ttm"] > 0


# ── standalone runner ────────────────────────────────────────────────────────
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nAll {len(fns)} schema tests passed.")
