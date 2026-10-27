# trend_long sweep — pre-registration and bug-fix record

**Everything in this folder is a BACKTEST ON A SELECTED, SURVIVOR-BIASED ARCHIVE
(209 Nifty-500 constituents + 3 index CSVs).** It is not live evidence, not a
validated strategy, and not investment advice.

Written and committed **before** any 2022+ number was computed for the sweep.

---

## 1. The five fixes, and what each one changed

The exploratory run in [../trend_long_exploratory/REPORT.md](../trend_long_exploratory/REPORT.md)
listed seven things to distrust. Five are fixed here; the fixes are in
`trend_long_backtest.py` (docstring F1–F5) and pinned by
`tests/test_trend_long_backtest.py` (15 tests).

| # | Defect | Fix | Pinned by |
|---|---|---|---|
| F1 | `DATA_END` sold a name at the last close in the archive, using the **future** knowledge that its data ends there. | No hindsight exit. When a held symbol reaches its **last available bar** the position can no longer be priced, so it is marked out at that last close with a **flagged haircut** (`data_end_haircut` = 2% on top of sell slippage) and tagged `DATA_END_HAIRCUT`, reported on its own line. A haircut is a penalty, not a measurement of the look-ahead's value. | `test_f1_data_end_exit_is_haircut_and_flagged` |
| F2 | VIX was forward-filled with **no limit** (the feed ends 2026-09-11; the archive runs 23 sessions past it) and a *missing* VIX file scored **+1**, the most bullish possible value. | `ffill(limit=3)`. Beyond that the feed is stale: the VIX leg scores **0**, no new entry signal is taken, and an entry already queued for the next open is **cancelled**. Exits, stops and position management are never blocked. A missing VIX file fails **closed** (every session stale). | `test_f2_*` (3 tests) |
| F3 | Session order was stops → discretionary sells → buys, so an **intraday stop** on day *t* funded a buy that had already filled at day *t*'s open. | Order is now: (1) discretionary opening sells, (2) opening buys, (3) protective stops (gap at the open, else the low touching the stop), (4) data-end marks. Only step 1 funds step 2. **This is stricter than the Phase 1 contract** (`docs/trend_long_phase1.md` item 4), which allowed an opening gap-stop fill to fund opening buys; the deviation is deliberate and flagged. | `test_f3_*` (2 tests) |
| F4 | The terminal book was sold at the final close and the result was reported as if it were trading performance. | `END_LIQUIDATION` is tagged and reported separately: number of names, gross P&L, costs, plus the same account **marked at the final close with no liquidation cost** and the difference between the two. | `test_f4_*` (2 tests) |
| F5 | Initial stops were re-anchored to a gapped opening fill without saying so. | Stated exactly: `initial_stop = actual fill price including buy slippage − chandelier_atr × ATR(signal session)`. Realised risk per share is therefore **exactly** `chandelier_atr × ATR(t)` whatever the overnight gap, the size formula uses the same number, no fill-day ATR is ever read, and a fill whose stop would be ≤ 0 is rejected. Verified on all 695 full-window trades: max deviation 7e-12. | `test_f5_*` (2 tests) |
| F6 | The long-term tax boundary was a flat 365 days. | Strictly more than **12 calendar months** (`F6` is not requested; it was wrong in leap years). | `test_f6_*` |

What the fixes did to the headline numbers (full window, same archive, same config):

| | exploratory (buggy) | fixed |
|---|---:|---:|
| ₹1 lakh + ₹1,000/month, net-of-cost XIRR | 7.19% | **6.61%** |
| ₹10 lakh lump sum, net-of-cost XIRR | 10.48% | **11.92%** |

These are **not** a performance improvement. They are different mechanics: one
account got more conservative and one got luckier. Neither number is a return.

Not fixed, and still true: survivorship, stale prices forward-filled into
indicators, `pct_change()` padding, the sector cap being bypassed for unknown-sector
names, the ±5%-of-equity tolerance on regime trims, fill-time relaxation of the
minimum position to 60%, no circuit/price-band model, and a simplified end-charged
tax model.

## 2. Entry funnel (fix + diagnostics)

`run()` now attributes every candidate that wanted an entry to the **first** gate
that stopped it, in this order: `vix_stale`, `regime_cap`, `no_trigger`,
`slots_full`, `exposure_cap`, `min_position`, `heat_cap`, `sector_cap`,
`per_day_cap`, `filled`. `funnel_table()` reports per year: decision days, mean
target slots **N**, **slot opportunities** (Σ max(0, N − held)), **entries filled**,
fill rate, candidates seen, and each gate's count and share. Written to
`funnel_<window>_<capital>.csv`.

## 3. The sweep — pre-registered

**Windows.** Tune: **2016-10-17 → 2021-12-31**, an independent replay with fresh
capital. Holdout: **2022-01-01 → 2026-10-16**, a *second independent replay* (empty
book, fresh capital), not a slice of the continuing book. Features are trailing, so
computing them on the full archive leaks nothing into either window. The holdout is
looked at **once**, in stage `oos`. No parameter, threshold or arm may be changed
after that look.

**Capital scenarios.** `lump_10L` = ₹10,00,000; `account_1L_SIP` = ₹1,00,000 +
₹1,000/month, exactly the two pre-registered scenarios in
`config/thresholds.yaml → trend_long.backtest.capital_scenarios`.

**Placebo.** 200 seeds (1000–1199), random **month-stable** ranks, identical
triggers/sizing/exits/regime. Identical seed set in both windows and both capital
scenarios. The placebo is not exposure-matched — random ranks change holdings and
deployment — which is a known weakness of this test, not a reason to relax it.

**Arms — six pre-registered variant families, nine configurations with baseline.**

| family | arm | change vs baseline |
|---|---|---|
| baseline | `baseline` | risk_pct 0.0075, chandelier_atr 3.5, defer off, regime cap on, top_n 300 |
| a | `a1_risk_0100`, `a2_risk_0125` | risk_pct 0.010 / 0.0125 |
| b | `b1_chandelier_45`, `b2_chandelier_50` | chandelier_atr 4.5 / 5.0 |
| c | `c1_defer_soft_exits` | `defer_soft_exits` on, `defer_from_days = 0` |
| d | `d1_no_regime_cap` | regime exposure cap disabled |
| e | `e1_topn_150` | universe top_n 150 (vs baseline 300) |
| f | `f1_combo_best_of_a_b_c` | combination of the best of (a)–(c), chosen on the tune window only |

Two pre-registered notes, both established **before** the holdout look:

- **(c) deferral window.** `config/thresholds.yaml` sets `defer_window_days
  [335,366]`. No trade in the entire archive is held 335 days (longest 241), so that
  window can never fire and the arm would be a no-op. The arm therefore defers from
  day 0: any soft exit (REGIME trim, RANK_DECAY, DEAD_MONEY) that would realise a
  short-term **gain** is held until the 12-month boundary. Stops, data-end marks and
  terminal liquidation are never deferred. Deferring a REGIME trim knowingly leaves
  the book above its cap while it lasts.
- **(f) selection rule, fixed now.** For each of the three knobs, take the level with
  the higher **mean tune-window net-of-cost XIRR across the two capital scenarios**;
  ties break toward the baseline level. The combination is the three winners
  together. Selection uses 2016–2021 only.

**Acceptance — a variant is accepted only if ALL FOUR hold:**

- **A1** beats the Nifty500 same-flow XIRR net of costs, in **both** windows, at
  **both** capital scenarios (4 checks).
- **A2** clears the **200-seed placebo p90**, at **both** capital scenarios, in
  **both** windows (4 checks).
- **A3** survives **±20%** on each changed parameter: every perturbation arm
  (`p×0.8`, `p×1.2` — 16 arms) must itself pass A1 and A2. A binary switch has no
  ±20%; its counterpart arm is the control and is reported as such.
- **A4** still beats Nifty500 in both windows at both capital scenarios with the
  **five best trades removed**, reported twice: with that cash idle, and with it at
  the modelled 6% cash yield for each trade's own holding period.

If no variant passes all four, the result is **no demonstrated edge**, and that is
what gets reported.

## 4. Reproduction

```bash
.venv/bin/python -m pytest -q tests/test_trend_long_backtest.py     # the fixes
.venv/bin/python scripts/trend_long_sweep.py --stage tune --procs 20   # 2016-2021 only
.venv/bin/python scripts/trend_long_sweep.py --stage oos  --procs 20   # the single look
.venv/bin/python scripts/trend_long_sweep.py --stage eval --procs 20   # acceptance table
```