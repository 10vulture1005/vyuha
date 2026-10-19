"""Regression tests for the fixes made to the exploratory trend_long script.

Each test names the defect it pins (F1..F6 in trend_long_backtest.py's docstring).
These are synthetic and fast: they hand-build the feature panel `run()` consumes, or
a tiny CSV archive for the regime builder. Nothing here downloads or touches the DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import trend_long_backtest as tl  # noqa: E402


def cfg(**over):
    """Test config: no cash yield, tiny slot count, sizes that fit the exposure cap."""
    c = dict(tl.CFG)
    c.update(dict(cash_yield=0.0, inr_per_name=100.0, n_min=2, n_max=2,
                  min_position_inr=100.0, max_name=10.0, heat_cap=1.0, sector_cap=1.0,
                  risk_pct=0.02, slip=0.0, data_end_haircut=0.02, max_new_per_day=2))
    c.update(over)
    return c


def panel(n=8, syms=("A", "B"), price=100.0, atr=1.0, **over):
    """Minimal panel: flat prices, everything eligible and triggered by default."""
    cal = pd.bdate_range("2022-01-03", periods=n)
    k = len(syms)
    P = dict(
        cal=cal, syms=list(syms), K=k, T=n,
        O=np.full((n, k), float(price)), H=np.full((n, k), float(price)),
        L=np.full((n, k), float(price)), C=np.full((n, k), float(price)),
        Cf=np.full((n, k), float(price)), atr=np.full((n, k), float(atr)),
        sma50=np.full((n, k), float(price)), elig=np.ones((n, k), bool),
        trig=np.ones((n, k), bool), near=np.zeros((n, k), bool),
        valid=np.ones((n, k), bool), adm=np.ones((n, k), bool),
        s_pct=np.ones((n, k)), cap=np.ones(n), level=np.ones(n),
        is_first=np.r_[True, np.zeros(n - 1, bool)], sess=np.zeros(n, int),
        month_id=np.zeros(n, int), last_idx=np.full(k, n - 1),
        sector=np.array(["UNK"] * k), n5=pd.Series(float(price), index=cal),
        vix_stale=np.zeros(n, bool), vix_present=True)
    for key, value in over.items():
        assert key in P, key
        P[key] = value
    return P


def only_A(P):
    P["elig"][:, 1] = False
    return P


# --------------------------------------------------------------------------- F5
def test_f5_initial_stop_is_actual_fill_minus_signal_atr():
    """The stop rides the actual fill, so risk/share is exactly k*ATR(sig) even on a gap."""
    flat, gapped = only_A(panel()), only_A(panel())
    for key in ("O", "H", "L", "C", "Cf"):        # +20% gap into the entry
        gapped[key][:, 0] = 120.0
    f = tl.run(flat, cfg(), 1000.0, 0.0)["trades"]
    g = tl.run(gapped, cfg(), 1000.0, 0.0)["trades"]
    assert len(f) == 1 and len(g) == 1
    k_mult = cfg()["chandelier_atr"]
    assert f.entry_px.iloc[0] == pytest.approx(100.0)
    assert g.entry_px.iloc[0] == pytest.approx(120.0)
    assert g.init_stop.iloc[0] == pytest.approx(120.0 - k_mult * g.init_atr.iloc[0])
    assert f.init_stop.iloc[0] == pytest.approx(100.0 - k_mult * f.init_atr.iloc[0])
    # the point of the rule: the overnight gap moves the stop, but not the risk, the
    # size, or the ATR used (signal day's, never the fill day's).
    assert g.init_risk.iloc[0] == pytest.approx(k_mult * g.init_atr.iloc[0])
    assert g.init_risk.iloc[0] == pytest.approx(f.init_risk.iloc[0])
    assert g.qty.iloc[0] == f.qty.iloc[0]
    assert g.init_stop.iloc[0] > f.init_stop.iloc[0]
    assert pd.Timestamp(g.sig_date.iloc[0]) < pd.Timestamp(g.entry.iloc[0])


def test_f5_gap_down_entry_still_buys_and_still_carries_a_stop():
    P = only_A(panel())
    P["O"][1, 0] = 90.0
    P["L"][1, 0] = 95.0                 # day low above the stop: not stopped out
    t = tl.run(P, cfg(), 1000.0, 0.0)["trades"]
    assert len(t) == 1
    assert t.entry_px.iloc[0] == pytest.approx(90.0)
    assert t.init_stop.iloc[0] < 90.0
    assert t.init_stop.iloc[0] == pytest.approx(90.0 - cfg()["chandelier_atr"] * t.init_atr.iloc[0])


# --------------------------------------------------------------------------- F3
def _stop_and_buy_same_day(b_open_mult):
    """A is stopped intraday on d2 while B's opening buy (gapped by b_open_mult) fills."""
    P = only_A(panel(n=9))
    P["elig"][:, 1] = False
    P["elig"][1, 1] = True             # B becomes a candidate at d1, fills at d2's open
    P["trig"][1, 1] = True
    P["atr"][:, 1] = 1.4               # B sizes larger than A for the same risk budget
    P["L"][1, 0] = 97.0                # A survives its entry session (its stop is 96.5)
    P["O"][2, 0] = 100.0
    P["L"][2, 0] = 90.0                # A is stopped INTRADAY on d2, after the open fills
    P["O"][2, 1] = 100.0 * b_open_mult
    return P


def test_f3_intraday_stop_proceeds_cannot_fund_a_same_day_open_buy():
    """A modest gap leaves B short of cash; A's intraday stop sale must not rescue it."""
    P = _stop_and_buy_same_day(1.3)
    res = tl.run(P, cfg(), 1000.0, 0.0)
    tr = res["trades"]
    a = tr[tr.symbol == "A"].iloc[0]
    b = tr[tr.symbol == "B"]
    assert a.reason == "STOP" and a.exit == P["cal"][2]
    assert len(b) == 1
    # B was sized against the cash that existed AT THE OPEN: not the 4 shares the signal
    # asked for, and not the 4 shares A's later stop sale would have paid for.
    assert b.qty.iloc[0] == 3
    assert b.entry_px.iloc[0] == pytest.approx(130.0)


def test_f3_stop_proceeds_do_not_rescue_a_gap_up_buy_either():
    P = _stop_and_buy_same_day(5.0)
    tr = tl.run(P, cfg(), 1000.0, 0.0)["trades"]
    assert (tr.symbol == "A").any()
    assert len(tr[tr.symbol == "B"]) == 0, "B was bought with intraday stop proceeds"


# --------------------------------------------------------------------------- F2
def test_f2_stale_vix_blocks_new_entries_but_not_exits():
    P = only_A(panel(n=9))
    P["vix_stale"][2:] = True          # the feed dies after A is already in the book
    P["L"][1, 0] = 97.0
    P["L"][2, 0] = 90.0                # the protective stop must still fire while stale
    res = tl.run(P, cfg(), 1000.0, 0.0)
    tr = res["trades"]
    assert len(tr) == 1 and tr.reason.iloc[0] == "STOP", "a stale feed must not disable exits"
    assert res["gate"]["vix_stale"].sum() > 0, "the funnel did not record the VIX block"
    assert res["gate"]["filled"].sum() == 1, "only the pre-stale signal may fill"
    assert len(tr) == 1, "the queued entry must be cancelled once the feed goes stale"


def test_f2_vix_ffill_is_capped_and_a_missing_vix_feed_fails_closed(tmp_path):
    d, n = tmp_path, 300
    cal = pd.bdate_range("2021-01-04", periods=n)
    up = 100.0 * (1.004 ** np.arange(n))
    for name, series, bars in (("INDEX_NIFTY500", up, n), ("INDEX_NIFTY50", up, n),
                               ("INDEX_INDIAVIX", up * 2, n - 12), ("AAA", up, n),
                               ("BBB", up * 1.2, n)):
        pd.DataFrame({"Date": cal[:bars].strftime("%Y-%m-%d"), "Open": series[:bars],
                      "High": series[:bars] * 1.01, "Low": series[:bars] * 0.99,
                      "Close": series[:bars], "Volume": np.full(bars, 2e6)}).to_csv(
            d / f"{name}.csv", index=False)
    cal_a, stocks, idx = tl.load_archive(str(d), None, None)
    P = tl.build(cal_a, stocks, idx, tl.CFG, None)
    stale = P["vix_stale"]
    last_bar, cap = n - 12, tl.CFG["vix_ffill_limit"]
    assert not stale[:last_bar + cap].any(), "stale before the ffill cap is exhausted"
    assert stale[last_bar + cap:].all()
    assert stale.sum() == 12 - cap

    (d / "INDEX_INDIAVIX.csv").unlink()
    cal_a, stocks, idx = tl.load_archive(str(d), None, None)
    P2 = tl.build(cal_a, stocks, idx, tl.CFG, None)
    assert not P2["vix_present"] and P2["vix_stale"].all(), "a missing VIX feed must fail closed"


# --------------------------------------------------------------------------- F1
def test_f1_data_end_exit_is_haircut_and_flagged():
    P = only_A(panel(n=8))
    P["last_idx"] = np.array([2, 7])   # A's archive ends at d2, long before the replay does
    c = cfg()
    tr = tl.run(P, c, 1000.0, 0.0)["trades"]
    assert (tr.reason == "DATA_END_HAIRCUT").sum() == 1
    row = tr[tr.reason == "DATA_END_HAIRCUT"].iloc[0]
    assert row.exit == P["cal"][2]
    assert row.exit_px == pytest.approx(100.0 * (1 - c["slip"] - c["data_end_haircut"]))
    assert row.gain == pytest.approx(row.qty * row.exit_px - row.cost - row.basis)
    assert (tr[tr.symbol == "B"].reason != "DATA_END_HAIRCUT").all()


# --------------------------------------------------------------------------- F4
def test_f4_terminal_liquidation_is_separable_from_trading_results():
    P = only_A(panel(n=8))
    res = tl.run(P, cfg(), 1000.0, 0.0)
    tr = res["trades"]
    end = tr[tr.reason == "END_LIQUIDATION"]
    assert len(end) == 1
    assert res["final_mark"] == pytest.approx(res["eq"][-1] + end.cost.iloc[0]), "slip is zero here"
    assert res["final_mark"] > res["eq"][-1]


def test_f4_final_mark_ignores_liquidation_costs_only():
    P = only_A(panel(n=8))
    res = tl.run(P, cfg(slip=0.005), 1000.0, 0.0)
    end = res["trades"][res["trades"].reason == "END_LIQUIDATION"]
    # the mark uses the final CLOSE, the sale uses the close less sell slippage
    assert res["final_mark"] - res["eq"][-1] == pytest.approx(
        end.cost.iloc[0] + end.qty.iloc[0] * (100.0 - end.exit_px.iloc[0]))


# --------------------------------------------------------------------------- F6
def test_f6_long_term_boundary_is_twelve_calendar_months():
    c = cfg(stcg=0.20, ltcg=0.125, ltcg_exempt=0.0)

    def trade(open_date, close_date, gain):
        return dict(symbol="A", entry=pd.Timestamp(open_date), exit=pd.Timestamp(close_date),
                    days=(pd.Timestamp(close_date) - pd.Timestamp(open_date)).days, gain=gain)

    on_anniversary = tl.post_tax(pd.DataFrame([trade("2023-01-01", "2024-01-01", 100.0)]), c)
    after_anniversary = tl.post_tax(pd.DataFrame([trade("2023-01-01", "2024-01-02", 100.0)]), c)
    assert on_anniversary == pytest.approx(0.20 * 100)    # 366 days, but not >12 months
    assert after_anniversary == pytest.approx(0.125 * 100)


# --------------------------------------------------------------------------- funnel
def test_funnel_gate_counts_partition_the_candidate_pool():
    P = panel(n=30)
    res = tl.run(P, cfg(min_position_inr=1e9), 1000.0, 0.0)   # nothing can be afforded
    ft = tl.funnel_table(P, res)
    assert int(ft["candidates_seen"].sum()) == sum(int(ft[g].sum()) for g in tl.GATES)
    assert int(ft["filled"].sum()) == 0
    assert int(ft["min_position"].sum()) > 0
    assert (ft["fill_rate_pct"] == 0.0).all()                 # slots open, nothing fills


def churn_panel(n=40, syms=("A", "B", "C", "D")):
    """Every session's low touches the stop, so names are sold and re-bought daily."""
    P = panel(n, syms=syms)
    P["L"][:] = 0.0
    return P


def test_funnel_reports_slots_filled_against_target_slots():
    P = only_A(panel(n=30))
    ft = tl.funnel_table(P, tl.run(P, cfg(), 1000.0, 0.0))
    assert (ft["mean_target_slots_N"] == cfg()["n_max"]).all()
    assert int(ft["entries_filled"].sum()) >= 1
    assert ft["slot_opportunities"].sum() > ft["entries_filled"].sum()


def test_top_trade_robustness_removes_the_five_best_trades():
    P = churn_panel()
    res = tl.run(P, cfg(), 1000.0, 0.0)
    tr = res["trades"]
    rob = tl.top_trade_robustness(P, res, cfg())
    assert rob["n"] == len(tr) > 5
    assert rob["top5_gain"] == pytest.approx(tr.nlargest(5, "gain")["gain"].sum())


def test_placebo_seeds_are_reproducible_and_differ():
    P = churn_panel(n=20, syms=tuple("ABCDEFGHIJKL"))
    a = tl.run(P, cfg(), 1000.0, 0.0, seed=7)["trades"]
    b = tl.run(P, cfg(), 1000.0, 0.0, seed=7)["trades"]
    c = tl.run(P, cfg(), 1000.0, 0.0, seed=8)["trades"]
    pd.testing.assert_frame_equal(a, b)
    assert not a.equals(c)


def test_slice_window_is_an_independent_replay():
    cal, stocks, idx = tl.load_archive("data/raw/ohlc", None, None)
    P = tl.build(cal, stocks, idx, tl.CFG, None)
    W = tl.slice_window(P, start="2022-01-01")
    assert W["cal"][0] >= pd.Timestamp("2022-01-01") and W["T"] < P["T"]
    assert W["K"] == P["K"]
    res = tl.run(W, tl.CFG, 1000000.0, 0.0)
    assert res["flow"].sum() == 1000000.0
    assert res["eq"][0] > 0 and np.isfinite(res["eq"]).all()
    assert (W["last_idx"] <= W["T"] - 1).all()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))