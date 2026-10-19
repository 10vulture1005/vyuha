# User-supplied standalone trend_long — exploratory results

**Backtest on a selected, survivor-biased 209-name archive. Not live results.**

Executed the supplied `trend_long_backtest.py` without changing its strategy,
execution, costs, parameters or tax calculations. Output directories were made
separate to avoid overwriting one account with the other. Supplementary analysis
reran both baselines and asserted their equity curves matched the persisted runs.
This is **not** the approved Phase 1 executor or a completed Phase 2/3 validation.

## Run identity

- Actual window: **2016-10-17 → 2026-10-16**, 2,463 sessions.
- Archive: **209 equities + 3 indices**. All 212 CSV hashes still match Phase 0;
  no source prices were changed or downloaded. The later end date uses data
  already present in the same archive, not a refreshed universe.
- Python 3.12.14, pandas 2.3.3, NumPy 2.2.6.
- Script SHA-256: `3fe1cd2162f83e29db7efdd4f68d7fffacdd889843bf9240cf467c8619bef2f0`.
- No parameter tuning, DB access or live/paper integration for the standalone
  runs. No models/FinBERT are used. Legacy references below also used an isolated
  temporary database URL before application imports and made no DB queries.
- No fundamental or news screen, no separate gold leg. All residual cash accrues
  the script's 6% ACT/365 yield assumption. Monthly admitted count ranges from
  0 during warm-up to **200**; it is 172 in October 2026.

## Full-window results — two independent capital configurations

| Metric | ₹1 lakh + ₹1,000/month | ₹10 lakh lump sum |
|---|---:|---:|
| Total external contributions | ₹2,20,000 | ₹10,00,000 |
| Final value, after script costs | **₹3,73,546** | **₹27,10,859** |
| Gain over contributed capital | ₹1,53,546 | ₹17,10,859 |
| Cumulative gain / contributions | +69.79% | +171.09% |
| **Net-of-cost XIRR** | **7.19%** | **10.48%** |
| Script-estimated post-tax XIRR* | 6.17% | 8.81% |
| Script tax estimate* | ₹27,116 | ₹3,83,595 |
| Explicit transaction costs | ₹31,836 | ₹2,27,637 |
| Costs / total contributions | 14.47% | 22.76% |
| Max drawdown, flow-adjusted | −18.35% | −21.79% |
| Drawdown peak → trough | 2021-10-18 → 2023-02-28 | 2024-10-15 → 2026-03-30 |
| Sharpe, 6% nominal risk-free | 0.17 | 0.42 |
| Sortino (supplementary) | 0.23 | 0.57 |
| Calmar using TWR (supplementary) | 0.39 | 0.48 |
| One-way turnover / year (supplementary) | 4.66× | 5.21× |
| Average holdings | 5.91 | 8.18 |
| Days with any holding | 82.87% | 89.16% |
| Mean cash weight | 54.37% | 42.64% |
| Closed trades | 543 | 708 |
| Win rate | 38.12% | 40.96% |
| Average calendar holding days | 39.70 | 42.15 |

*Tax uses the supplied simplified model, **not** the Phase 1 annual-tax model.
All tax is charged at the end; carry-forward is missing, the holding-period
boundary is a fixed 365 days, STT is deducted as an expense, and sleeve yield
is untaxed. The source comment claiming FY2026–27 verification is **not** an
independent legal verification. These are illustrative estimates, not reliable
post-tax account returns.

The SIP headline is **XIRR, not CAGR**. The script also prints a flow-adjusted
TWR annualization (7.12% / 10.49%), which is a different return measure, not CAGR
on the SIP ending balance. Supplementary turnover = half of gross buys plus
sells, divided by average equity and elapsed years. Sortino uses RMS negative
excess returns over all sessions; Calmar uses annualized TWR / absolute TWR DD.

## Out-of-sample segment, 2022 onward

Actual segment: **2022-01-03 → 2026-10-16**. This is a slice of the continuing
portfolio, re-based at its first OOS closing equity; it is **not an independently
initialized 2022 replay** and does not validate the supplied parameters' provenance.

| Metric | ₹1 lakh + SIP configuration | ₹10 lakh configuration |
|---|---:|---:|
| Net-of-cost XIRR | **5.90%** | **4.32%** |
| TWR annualized return | 5.93% | 4.32% |
| Max drawdown | −16.07% | −21.79% |
| Drawdown peak → trough | 2024-10-15 → 2025-12-10 | 2024-10-15 → 2026-03-30 |
| Sharpe | 0.07 | −0.05 |
| Sortino | 0.09 | −0.06 |

Both OOS XIRRs are below an ACT/365 cash@6% same-flow reference. No OOS
post-tax or OOS placebo distribution was computed by the supplied script.

## Same-flow benchmarks, full window

Nifty500 is printed by the supplied script. Nifty50 and the exact ACT/365
cash reference were added as separate diagnostic calculations using the same
script benchmark function and the strategy's exact recorded contribution schedule.
Benchmarks are before personal taxes and do not include strategy transaction fees.

| Reference | SIP final value | SIP XIRR | Lump-sum final value | Lump-sum XIRR |
|---|---:|---:|---:|---:|
| Supplied strategy | ₹3,73,546 | 7.19% | ₹27,10,859 | 10.48% |
| Nifty500 | ₹5,16,168 | **11.55%** | ₹30,15,787 | **11.67%** |
| Nifty50 (supplement) | ₹4,63,854 | 10.11% | ₹27,16,557 | 10.51% |
| Script's nominal cash@6%, 252-session clock | ₹3,38,308 | 5.85% | ₹17,66,991 | 5.86% |
| Cash@6%, ACT/365 (supplement) | ₹3,41,993 | **6.00%** | ₹17,91,134 | **6.00%** |

Index maximum drawdowns are −38.30% (Nifty500) and −38.44% (Nifty50). The
strategy underperforms both index references on full-window XIRR, with smaller
modelled drawdowns. It carries a large cash allocation, so these are not
exposure-matched index controls.

**The selected survivor-biased stock archive biases the benchmark comparison
in the strategy's favour.** Auto-adjusted stock data and the exact index
price/TRI classification are additional limitations: no independent historical
TRI audit was performed here. `auto_adjust=True` alone does not establish that
a Yahoo index series is a true total-return index. Do not use this comparison
as proof of alpha. The script's cash reference is also not exactly 6% annually
because Indian trading-session counts do not equal 252 every year.

## Placebo: requested 30 seeds and original 200-seed requirement

All use the supplied random **month-stable ranks**, with the same code and no
parameter changes. Seeds are **1000–1029** and **1000–1199** respectively; the
30-seed batch is the first subset of the 200-seed batch, not an independent test.
Reported win percentiles are rounded by the supplied script.

| Capital configuration | Seeds | Placebo median XIRR | Placebo p90 XIRR | Actual XIRR | Approx. seeds beaten | Beats p90? |
|---|---:|---:|---:|---:|---:|---|
| ₹10 lakh lump sum — requested run | 30 | 8.12% | 10.57% | 10.48% | 87% | **No** |
| ₹10 lakh lump sum — supplement | 200 | 7.60% | 9.50% | 10.48% | 94% | **Yes, in this model** |
| ₹1 lakh + ₹1,000/month — supplement | 200 | 5.17% | 7.51% | 7.19% | 85% | **No** |

**The actual-account configuration does not demonstrate ranker edge by the
specified p90 threshold.** The lump-sum case clears the threshold in 200 seeds,
but not in 30. That sample sensitivity should be shown, not hidden by choosing
the favorable batch. The 200-seed test was pre-specified in the original task.

This placebo is also **not exactly exposure-matched**: random ranks change entry
selection, rank-dependent exits, holdings and actual deployment. Sharing a regime
cap and sizing algorithm does not hold realized exposure or individual trade
sizes fixed. Full-window placebo performance here is therefore exploratory,
not the requested final isolated ranker test or evidence of OOS edge. The
technical+screen, tax-deferral and parameter/cost/universe ablations were not run.

## Cash weight by calendar year

Cash weight is mean cash/equity, not percentage of days with no holding.
2016 and 2026 are partial years. Complete invested-day and holding counts are
in `annual_diagnostics.csv`.

| Year | SIP mean cash weight | Lump-sum mean cash weight |
|---|---:|---:|
| 2016 | 100.00% | 100.00% |
| 2017 | 95.59% | 86.53% |
| 2018 | 67.96% | 37.76% |
| 2019 | 56.46% | 44.21% |
| 2020 | 78.06% | 44.13% |
| 2021 | 53.12% | 27.74% |
| 2022 | 50.69% | 44.05% |
| 2023 | 18.94% | 20.60% |
| 2024 | 25.15% | 23.89% |
| 2025 | 38.68% | 39.48% |
| 2026 | 46.60% | 45.72% |

## Legacy Investor reference, rerun on this same archive

The reference's original full-history requested start remains 2016-09-12,
preserving its **28-name start-date freeze**. Its actual index calendar begins
2016-10-17. The OOS reference is independently initialized at 2022-01-01 and
freezes **119** names. Both now run through **2026-10-16**, matching the supplied
script's latest calendar date. No legacy execution, costs or sleeve accrual was
fixed; these are references, not clean single-variable ablations.

| Legacy reference, ₹10 lakh lump sum | Actual dates | Final marked value | Net-of-legacy-cost XIRR |
|---|---|---:|---:|
| Full-history frozen book | 2016-10-17 → 2026-10-16 | ₹10,07,546 | +0.075% |
| Independent OOS frozen book | 2022-01-03 → 2026-10-16 | ₹11,10,597 | **+2.216%** |

The published **−0.31% OOS figure does not reproduce**. Phase 0's +2.556% was
through September 11, so the change to +2.216% here is also a different horizon,
not parameter tuning. Legacy OOS ends with **eight unliquidated holdings**, and
its costs undercharge delivery. It earns no yield on idle cash. Those facts
prevent attributing the standalone-minus-legacy difference purely to the ranker.
No SIP legacy comparison was made, since its first-month SIP schedule differs.

## What I would distrust in these exact outputs

1. **Hindsight liquidation.** `last_idx` uses the full future history and sells
   at a stock's last available **close** as `DATA_END`. One LAURUSLABS trade in
   each baseline is closed this way on 2026-10-15, with gains of ~₹14,282 (SIP)
   and ~₹1,03,797 (lump). These are trades containing a hindsight exit, **not**
   estimates of how much profit the look-ahead created. A valid alternative
   cannot be inferred by subtracting their whole trade gains.
2. **Execution differs from the approved contract.** Final sales are at the
   final close, not final open. Intraday stops run before discretionary opening
   exits and opening buys, potentially funding buys with later-day proceeds or
   giving the wrong exit priority. Initial stops are re-anchored to opening
   fills instead of preserving absolute close-t stops.
3. **Stale regime data is traded.** VIX ends 2026-09-11 and is forward-filled
   across the remaining **23 sessions**. There are **five SIP entries and three
   lump-sum entries** after its last actual bar. Missing VIX also gets +1 instead
   of blocking buys. This directly violates fail-closed data requirements.
4. **Stale-price features and forward-fill semantics.** Stock prices are filled
   for indicators, and pandas warns that `pct_change()` additionally pads
   missing data by default. No attempt was made to suppress or silently repair
   that warning; pandas/NumPy versions are recorded for reproducibility.
5. **Sizing differs.** Fill-time affordability can relax the minimum position
   to 60% of ₹12,000; **two actual-account entries are below ₹12,000** (minimum
   ~₹11,649). Signal-time name/sector/heat caps are not fully rechecked at actual
   gapped fills. Sector caps are bypassed for **30 unknown-sector names**.
6. **Other strategy deviations.** Scheduled fill lasts five sessions instead
   of just rebalance day, EMA proximity is only an upper bound, regime trims
   have a +5%-of-equity tolerance and occur on overweight rather than only cap
   step-down, and breakeven costs use a fixed 0.4% buffer. No screen/news/tax
   deferral ablation or separately validated gold sleeve exists in this script.
7. **Tax and population uncertainty.** Simplified taxes, no real news history,
   no real defensive ETF history, survivor selection, unverified restatement
   history and unmodelled price-band/circuit/market-depth constraints remain.

These results are useful to inspect the hypothesis, but they **do not establish
that the specified, fail-closed trend_long profile has been validated**. The
prior Phase 1 tests do not certify this separate user-supplied script. No
integration or changes to the approved execution library were made here.

## Reproduction and artifacts

```bash
.venv/bin/python trend_long_backtest.py --ohlc-dir data/raw/ohlc \
  --capital 100000 --sip 1000 --out docs/trend_long_exploratory/account_sip
.venv/bin/python trend_long_backtest.py --ohlc-dir data/raw/ohlc \
  --capital 1000000 --placebo 30 --out docs/trend_long_exploratory/lump_placebo30
.venv/bin/python trend_long_backtest.py --ohlc-dir data/raw/ohlc \
  --capital 100000 --sip 1000 --placebo 200 \
  --out docs/trend_long_exploratory/account_sip_placebo200
.venv/bin/python trend_long_backtest.py --ohlc-dir data/raw/ohlc \
  --capital 1000000 --placebo 200 --out docs/trend_long_exploratory/lump_placebo200
```

Each directory contains the original `run.log`, `equity.csv`, `trades.csv`,
`summary.json`. The original script saves rounded placebo summaries to the log,
not individual seed returns. Additional files: `diagnostics.json`,
`annual_diagnostics.csv`, `monthly_admission.csv`, `*_daily_diagnostics.csv`,
`input_manifest.csv`, and `legacy_reference.json` with legacy equity/fill CSVs.
No original Phase 0/1 artifacts or unrelated working-tree files were overwritten.
