# Investor Mode — strategy, data limits and how to reproduce the numbers

Investor Mode is Vyuha's long-horizon portfolio profile, activated with
`STRATEGY_MODE=investor`. It is a **separate strategy**, not a parameter of the
swing engine: it holds 20–30 names, rebalances monthly, and computes signals on
daily closes that execute at the next open.

The swing strategy remains the default and is unchanged. Nothing in `backtest/`,
`backtest_v2/`, `core/capital_allocator.py` or the swing agents was modified.

---

## 1. What it is

| Layer | Question it answers | Where |
|---|---|---|
| **L0 Regime** | How much equity risk should we carry right now? | `investor/regime.py` |
| **L1 Fundamentals** | *What* should we own? | `investor/fundamentals.py` |
| **L2 Technicals** | *When* do we buy and sell? | `investor/technical.py` |
| **L3 News** | What just happened that changes the plan? | `investor/news.py` |
| **Portfolio** | How much of each name, and when do we act? | `investor/portfolio.py` |
| **Decisions** | The single daily decision function | `investor/pipeline.py` |

`investor/pipeline.InvestorEngine.evaluate_day()` is the **only** place strategy
decisions are made. The live engine (`core/investor_engine.py`) and the backtest
(`backtest_investor/engine.py`) both call it.

> This is deliberate. The repo's existing swing strategy once had two
> implementations — `backtest/engine.py` and `core/paper_engine.py` — that
> disagreed about stops, time stops and profit tiers. That divergence was
> documented in the README as an "alignment" fix. Having one copy of the rules
> makes the class of bug structurally impossible rather than merely fixed.

---

## 2. The execution contract

* Signals are computed from the **close of day t**.
* Orders are **queued** and filled at the **open of day t+1**.
* A stop is checked against day t+1's low; if the open gaps through it, the
  fill is the open, not the stop.
* A symbol with no bar on day t+1 is **not** filled — never at a stale price.

This is enforced structurally: `backtest_investor/engine.py::_execute` is the
only function that touches cash in the backtest, and it only ever reads
`Open`. `tests/test_investor_pipeline.py::TestExecutionContract` asserts it.

Indicators are pre-computed once per symbol and sliced per day
(`investor/indicators.py`), which is mathematically identical — every indicator
is strictly backward-looking — and roughly 1000× faster than re-rolling a
200-day SMA per symbol per day.

---

## 3. Point-in-time discipline

A quarter's numbers become usable only from **`filing_date + 1 day`**, falling
back to **`period_end + 60 days`** when no filing date is known. The fallback is
deliberately pessimistic: an early `usable_from` is the one error that silently
manufactures returns.

`investor/data.py::PointInTimeFundamentals.as_of()` is the single entry point.
There is no "latest row" code path anywhere in Investor Mode, so the guarantee
is enforced by construction rather than by convention. Coverage is also
recorded in `investor_fundamental_snapshots.usable_from`, making the rule
checkable in SQL.

**Valuation multiples are never stored.** P/E, ROCE, P/B and every 5-year
median are recomputed against the price panel at scoring time. A stored
"current P/E" read in 2018 is a look-ahead the moment you query it.

---

## 4. Data limits — read this before trusting any number

These were found while building the backtest and are not fixable without new
data sources. The generated report repeats them at the top of
`docs/investor_backtest/REPORT.md`.

| Spec asked for | What the repo actually has | Consequence |
|---|---|---|
| Backtest from 2012 | OHLC starts **2016-09-12** (182 files) | Window is 2016-09 → 2026-09 |
| Filing-date fundamentals for the universe | **179 symbols, from 2021-Q4 onward**; no filing dates, only period ends | F layer uses the `period_end + 60d` fallback and only exists from ~2022-03 |
| Point-in-time Nifty 500 constituents | No `data/raw/constituents/` | Falls back to the existing liquidity/momentum proxy in `universe/computed_point_in_time.py` |
| LIQUIDBEES / GOLDBEES defensive sleeve | **No price history for either** | Sleeve accrues at the configured cash yield; gold is modelled, not price-accurate |
| Historical headlines | **None exist** | `news=proxy`: the earnings-day gap/volume signature stands in for FinBERT/LLM |
| Promoter pledge % | Carried for **29 symbols** only | See `pledge_required` below |
| GNPA % | **Absent for every symbol** | See `gnpa_required` below |

### Promoter pledge and GNPA

The brief lists `promoter pledge < 10%` and `GNPA < 3%` as hard filters. The
statement source carries neither field for Indian names. Rather than silently
pass them (which would make the filters decorative) or auto-reject the entire
financial sector (which would remove every bank from the book), both are
**armed and enforced wherever data exists**, and names with unknown values are
admitted by explicit configuration:

```yaml
non_financial:
  pledge_required: false   # true => unknown pledge is disqualifying
financial:
  gnpa_required: false     # true => unknown GNPA is disqualifying
```

Flip either to `true` for a strict gate. The default is a documented deviation,
not an oversight.

### Data generation

```bash
python scripts/fetch_investor_fundamentals.py
```

Pulls real annual + quarterly statements for every symbol with OHLC history and
writes `data/raw/fundamentals_investor.csv` (raw statement lines only, never
ratios). Cached per symbol under `data/cache/fundamentals_raw/`.

---

## 5. Reproducing the backtest

```bash
STRATEGY_MODE=investor python scripts/run_investor_backtest.py
```

or

```bash
vyuha invest backtest            # full study incl. robustness
vyuha invest backtest --quick    # skip the robustness sweeps
```

Outputs to `docs/investor_backtest/`:

| File | Contents |
|---|---|
| `REPORT.md` | Full markdown summary, data limits first |
| `equity_curve.png` | Strategy vs benchmarks + drawdown panel |
| `quantstats.html` | QuantStats tearsheet |
| `stats.csv` | Statistics table |
| `ablations.csv` | Per-layer ablation deltas |
| `robustness.csv` | Stress scenarios with collapse flags |

### Reported separately, never mixed

* **Lump-sum CAGR** — only when the whole capital went in on day one.
* **SIP XIRR** — money-weighted return on dated contributions.

Once monthly contributions exist, CAGR on the ending balance silently credits
the investor's own deposits to the strategy. `compute_stats` refuses to report
CAGR on a contributed series.

### Ablations

`fundamental_only`, `technical_only`, `fundamental_technical`,
`fundamental_technical_news`, `full`. Each is an independent replay over the
same window and universe with identical capital, so the delta is attributable to
the layer rather than to a different sample.

### Robustness

2× costs and slippage; ±20% on the 200DMA length, the Chandelier ATR multiple
and the top-N count; five random 60% universe subsamples. Any scenario losing
more than half the baseline CAGR is flagged `collapses=True` and surfaced in the
report verdict.

---

## 6. Live / paper operation

```bash
STRATEGY_MODE=investor vyuha invest daily        # signals, targets, orders, Telegram
STRATEGY_MODE=investor vyuha invest daily --json # machine-readable
STRATEGY_MODE=investor vyuha invest status       # current book
STRATEGY_MODE=investor vyuha invest news --symbol RELIANCE
```

The daily cycle:

1. Credit the monthly SIP on the month's first trading day.
2. Fetch, classify and **persist** headlines for holdings + watchlist.
3. Run Layers 0–3 against the persisted news.
4. Write `investor_targets` and, on a rebalance day, `investor_rebalance_log`.
5. Emit an orders list for the **next** open.
6. Paper-fill the previous close's orders at today's open into `trade_log`.
7. Send the Telegram digest.

**`LIVE_TRADING_ENABLED` stays `false`.** `InvestorModeEngine` refuses to run
with live execution enabled. Nothing here places a real order.

Persisting every headline is the point: `investor_news_events.is_proxy`
distinguishes real classified news from backtest proxies, so a genuine news
backtest becomes possible once enough history accumulates.

---

## 7. Database

```bash
alembic upgrade head
```

Creates `investor_rebalance_log`, `investor_targets`, `investor_news_events`
and `investor_fundamental_snapshots`.

Investor Mode deliberately does **not** share the swing tables. `PortfolioHolding`
carries `initial_risk` and heat-budget concepts that have no meaning in a
25-name monthly book, and letting one profile's rows corrupt another's targets
is the failure mode worth designing out.

---

## 8. Known behavioural oddities (measured, not assumed)

These are properties of the specified rules on the available data. They are
reported rather than tuned away.

**The book runs small and holds a lot of cash.** The entry gate is the
conjunction of: in the top-30% eligible list, `close > 200DMA and 50DMA > 200DMA`,
positive 12-1 month momentum, positive 6-month relative strength, **and** a
breakout or pullback trigger. Measured over 4,806 eligible name-days in 2025:

| Gate | Pass rate |
|---|---|
| trend filter | 43.1% |
| momentum (12-1 and 6m RS) | 24.2% |
| both | 22.8% |
| + breakout trigger | 1.3% |
| + pullback trigger | 1.1% |
| **any entry signal** | **2.4%** |

At 2.4% that is roughly 118 entry signals per year across 20 eligible names —
enough to build a book, but only gradually, and the 3-buys-per-day cap plus the
60-day minimum hold keep the average book well below the 20–30 target.

**`RSI turning up through 40` is a crossover**, not "RSI is above 40". Requiring
the crossover on the *exact same bar* as the 50DMA touch fired on **0 of 211**
eligible name-days, because the two conditions rarely coincide in one session.
The crossover is therefore accepted within the last 3 sessions
(`pullback_ma_tolerance`, and `rsi_prev1..3` in `investor/indicators.py`). This
is an interpretation of the spec and is the single most consequential one here.

**Fundamentals-only is a genuinely bad standalone strategy** — it loses money
out of sample. That is a real result, not a bug: quality filtering without a
trend filter buys names that are already falling.

---

## 9. Configuration

Everything lives under `investor:` in `config/thresholds.yaml`. There are no
hardcoded thresholds in `investor/`. Changing a value there changes the live
engine and the backtest identically.