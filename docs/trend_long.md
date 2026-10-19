# `trend_long` — backtest-only implementation checkpoints

**Current status: Phase 1 costs, tax scenario and execution infrastructure are
implemented and tested; stopped before Phase 2 strategy code.** See
[Phase 1 diff summary, assumptions and source verification](trend_long_phase1.md).
The approved implementation has no strategy performance or live claims. A
subsequently supplied standalone script was run unchanged at the user's request:
[exploratory results, 200-seed controls and caveats](trend_long_exploratory/REPORT.md).
It is separate from the approved executor and does not complete Phase 2/3.
All results are labelled **“backtest on a selected, survivor-biased 209-name archive”**.

The user approved continuing on the current archive with a separate trend replay,
monthly admission, stale fundamentals treated as unknown after 540 days, and the
screen excluded from the headline strategy. The two capital configurations remain
separate. Optional archive widening has not been authorized or performed.

## Phase 0 findings (preserved diagnostic record)

The original brief required stopping if the hypothesis was refuted or the entry
funnel looked materially different. Both the archive and replay universe selection
differed from the handoff, so work stopped after these diagnostics. The subsequent
user confirmation resolved that gate. No parameter tuning took place.

## 1. Finding: empty early book confirmed, proposed sole cause not confirmed

The hypothesis is **true for the 28-name universe the full-history replay
actually admits**, but **false as a description of the entire current archive**.

- Current fundamentals: **209 symbols, 1,815 records**: 1,560 yfinance,
  225 Screener, 30 merged. The earliest modelled usable date is
  **2006-05-30**, not 2022-03-01. There are **no actual filing dates**;
  every usable date uses the existing period-end + 60 days fallback.
- On 2016-09-12, **7 / 209** archive names have usable annual statements;
  **2** pass the fundamental hard filters: COLPAL and TATAELXSI.
- On 2022-01-01, **15 / 209** have usable annual statements; the same **2**
  pass. On 2023-06-01 coverage rises to **204 / 209**; on 2025-06-16 it is
  **209 / 209**, with **35** hard-pass names and **20** standing eligible names.
- `InvestorBacktester.__init__` permanently filters the input universe to names
  with *any bar at or before the requested start*. It does not admit later
  listings or names whose downloaded history starts later. A nominally identical
  universe therefore becomes **28 names** at 2016-09-12, but **119 names** at
  2022-01-01. Neither replay has the 179 names in the published report.
- All **28 / 28** full-history names have `no_filing_before_date` at the early
  refreshes. Their first usable annual statement is indeed **2022-03-01**.
  **No standing eligible names exist throughout 2016–2022**; growth/history
  gates also delay eligibility after the first statement. First actual buy:
  **2023-06-08**.
- COLPAL and TATAELXSI are excluded from that 28-name replay, but included in the
  119-name independent OOS replay. Its first buy is **2022-01-20**: the whole
  current archive is not structurally untradeable before March 2022.

This supports the broad diagnosis of severely restricted exposure, not the
claim that *all* names lack fundamentals before 2022. Start-date universe
freezing and the changed statement archive materially affect the result.
The old annual records are **not certified historical point-in-time data**:
COLPAL's latest usable annual at 2022-01-01 is dated 2010-03-31, and
TATAELXSI's is 2015-03-31. The old pipeline does not reject stale fundamentals.
The early dates demonstrate what the loader admits, not verified disclosure
availability or data correctness. No stale-record policy was changed here.

## 2. Actual invested days and capital weights

Unchanged full stack: fundamentals + technicals + news proxy + regime; initial
capital ₹10 lakh, **no SIP**. This deliberately reproduces the published
lump-sum setup without crediting contributions to returns.

“Invested days” means end-of-day holdings > 0. “Mean cash weight” is the daily
average cash/equity fraction. They are different measurements: a tiny holding
makes a day invested without deploying much capital. In particular, the old
report's “% time in cash” is a mean capital weight, not a zero-holdings day rate.
2026 is partial through September 11; 2016 starts October 17 because the
current Nifty500 file starts then, despite the requested September 12 start.

### Full-history backtest (28-name frozen universe)

| Year / split | Trading days | Days invested % | Mean cash weight % | Average holdings |
|---|---:|---:|---:|---:|
| 2016 | 53 | 0.00 | 100.00 | 0.00 |
| 2017 | 248 | 0.00 | 100.00 | 0.00 |
| 2018 | 246 | 0.00 | 100.00 | 0.00 |
| 2019 | 242 | 0.00 | 100.00 | 0.00 |
| 2020 | 250 | 0.00 | 100.00 | 0.00 |
| 2021 | 247 | 0.00 | 100.00 | 0.00 |
| 2022 | 247 | 0.00 | 100.00 | 0.00 |
| 2023 | 245 | 54.29 | 96.86 | 0.54 |
| 2024 | 244 | 42.62 | 96.37 | 0.64 |
| 2025 | 247 | 40.89 | 96.84 | 0.62 |
| 2026 | 171 | 59.65 | 96.49 | 0.94 |
| **Pre-2022** | **1,286** | **0.00** | **100.00** | **0.00** |
| **2022 onward, continuing full-history book** | **1,154** | **38.13** | **97.37** | **0.52** |

### Independent out-of-sample backtest (119-name frozen universe)

This is a fresh replay from 2022-01-01, not a slice of the full-history book.

| Year / split | Trading days | Days invested % | Mean cash weight % | Average holdings |
|---|---:|---:|---:|---:|
| 2022 | 247 | 49.39 | 97.95 | 0.49 |
| 2023 | 245 | 56.73 | 92.23 | 1.36 |
| 2024 | 244 | 100.00 | 75.07 | 6.46 |
| 2025 | 247 | 70.45 | 90.25 | 3.42 |
| 2026 | 171 | 100.00 | 85.79 | 6.20 |
| **2022 onward** | **1,154** | **73.66** | **88.45** | **3.41** |

### Baseline results — backtest only, not `trend_long` performance

| Run | Actual dates | Universe | Lump-sum CAGR, net of existing modelled costs | Final value | Fills | Explicit costs |
|---|---|---:|---:|---:|---:|---:|
| Full-history backtest | 2016-10-17 → 2026-09-11 | 28 | +0.076% | ₹1,007,545.62 | 47 | ₹1,268.30 |
| Independent out-of-sample backtest | 2022-01-03 → 2026-09-11 | 119 | +2.556% | ₹1,125,593.32 | 320 | ₹6,445.59 |

The old OOS result was **−0.31%**, with 92.0% mean cash and 2.7 holdings.
It does **not** reproduce with the current inputs. These diagnostic returns
use the old, known-undercharging cost model and are not post-tax. No SIP was
used, so CAGR here does not mix deposits with returns. XIRR/post-tax XIRR,
placebos, new-profile ablations and robustness remain **not run**, not passes.

## 3. Entry-funnel measurement

Denominator: standing eligible name-days, before held/target/news/buy-limit
constraints. A missing fresh technical snapshot fails the gate. Breakout and
pullback counts below include both trend and momentum gates. “Any entry” is
the existing technical entry function, not actual orders or fills. All reads
are observations of the existing replay; no gate has been loosened.

### 2025 comparison

| Gate | Published handoff | Full-history continuation, current archive | Independent OOS, current archive |
|---|---:|---:|---:|
| Eligible name-days | 4,806 | 863 | 4,940 |
| Trend | 43.1% | 45.42% | 32.71% |
| Momentum | 24.2% | 41.95% | 31.15% |
| Both | 22.8% | 25.84% | 22.00% |
| Both + breakout | 1.3% | 1.74% | 1.11% |
| Both + pullback | 1.1% | 1.27% | 1.26% |
| **Any entry** | **2.4%** | **3.01% (26 / 863)** | **2.37% (117 / 4,940)** |

The restrictive trigger diagnosis is reproduced approximately in the independent
OOS run, but not its exact cross-section or component gates. Across 2022 onward,
the full-history continuation has **79 / 2,002 = 3.95%** entry name-days; the
independent OOS run has **532 / 12,562 = 4.24%**. Before 2022 the full-history
run has **zero eligible name-days**, so its trigger rate is **undefined**, not
0%: the fundamental gate has already emptied the funnel.

Yearly counts, component percentages and actual buy-order counts are in the
`*_funnel.csv` files. Decision-day totals exclude the final bar (no next open);
exposure-day totals include it. Buy-order counts include tranche-2 orders and
are not counts of newly opened positions.

## 4. Survivorship and archive provenance

- **212 CSVs = 209 equities + 3 indices.** The 213 directory entries also include
  `.gitkeep`. The files span 2016-09-12 to 2026-10-16, but not uniformly.
- All **209 equities have bars within the last 365 calendar days** of the
  archive's end. There is **no evident long-dead/delisted cohort**. This does
  not prove every ever-delisted security is absent: delisting status/master
  data is unavailable. It means this archive cannot establish delisting
  coverage, and there is no evidence it captures the necessary failures.
- **28 names end on 2026-09-11**, 73 on 2026-10-15, 108 on 2026-10-16.
  Thus 28 lack bars in the final ten calendar days; they are stale downloads,
  not evidence of 28 delistings. They include RELIANCE, INFY and HDFCBANK.
- 102 equity files start in 2016; **73 start in 2024**, another **12 in 2025**.
  Starts can reflect download truncation as well as IPOs; do not label them
  all newly listed companies. The start-date replay gate never later admits
  any such names.
- `data/raw/constituents/` is absent. The OHLC ingestion code targets active
  watchlist names; the seed configs contain 30 nominal “Nifty500” names and
  150 midcap names. The archive includes 150/150 midcap seed names and 29/30
  of the other seed list, plus 30 names outside their union.
- Although a PIT universe resolver exists elsewhere, **the inspected Investor
  replay does not call it**: it accepts the passed list and freezes its own
  start-date filter. The old report's assertion that this replay is
  point-in-time-resolved is not supported by this code path.

**Conclusion:** treat this as a strongly survivor-selected, incomplete sample,
not historical NSE/BSE membership. Re-ranking it by historical traded value
cannot restore missing delisted names. A top-300 selector would have at most
209 equities even before price/liquidity/history gates. No BSE-completeness
claim is supported by the `.NS` ingestion path.

## 5. Conflicts and hazards identified at the Phase 0 stop

This section preserves the original questions. The user subsequently authorized
a separate trend-only execution path and monthly universe admission on the current
archive; the stale-screen amendment and headline/ablation split are recorded above.
Legacy quirks, AUDIT items and execution code remain unchanged. Phase 1 implements
only the approved execution/cost/tax infrastructure, described in its linked report.

1. **Execution-contract conflict.** The existing `_execute` fills sells from
   the next bar's open without testing that bar's low or a gap-through stop.
   `_emit_exits` instead tests the signal day's low against a close-updated
   stop and queues a next-open sale. `apply_buy` also initializes its stop from
   the fill-day indicator row. This is not the intrabar t+1 stop contract
   described in the brief/docs. Please authorize a **trend-only replay path**
   enforcing the requested contract, leaving legacy Investor behavior intact,
   or specify a different interpretation. Silently changing the shared legacy
   executor would violate the no-quirk-fixes constraint.
2. **Frozen universe.** Please confirm the new trend-only replay should admit
   names monthly as their history becomes available, rather than inherit the
   old start-date freeze. The latter is incompatible with the proposed
   price-derived monthly universe. Existing full-stack diagnostics should
   remain explicitly labelled with their actual, different samples.
3. **Fundamental ablation coverage.** “2022+ where data exists” is no longer
   uniform: 15 names are visible on 2022-01-01, 204 by June 2023, and some
   records are extremely old. Please confirm whether to use the current mixed
   source archive with coverage/staleness disclosed or provide the intended
   historical input snapshot. There is no independent assurance of filing
   dates or historical restatements in this data.
4. **Delivery costs (reported, not fixed).** `backtest_v2/costs/tax_and_fees.py`
   charges buy STT = 0, undercharging delivery by approximately 0.1% of buy
   notional per round trip. It also uses 0.003% buy stamp duty rather than the
   requested 0.015%. It computes GST on assumed brokerage without adding the
   brokerage itself to total costs, and charges DP per sell transaction, not
   deduplicated per scrip/sell day. Buy-side delivery STT is not merely a
   conservative swing assumption. No exact “conservative” note was found in
   the inspected tracked code/docs. New costs and FY2026–27 tax-law verification
   are deferred to Phase 1; no current-law verification claim is made here.
5. **Defensive sleeve claims.** The existing replay leaves cash unaccrued;
   `_mark` returns defensive value zero. The published claim that the fallback
   cash sleeve accrues yield is not implemented on this path. Do not treat the
   old results as a tested LIQUIDBEES/GOLDBEES model.
6. **Live blockers remain:** FIN-03, FIN-04, STR-01 and ADD-02. No live/paper
   wiring, adds, strategy ownership or ledger fixes are part of this work.
   `LIVE_TRADING_ENABLED` and its guards are unchanged.

## 6. Reproduction, artifacts and safety

```bash
.venv/bin/python scripts/diagnose_investor_entry_funnel.py
.venv/bin/python -m pytest -q tests/test_investor_phase0.py
```

The command sets a fresh temporary SQLite URL **before application imports**.
It performs no DB access and does not download prices, invoke a classifier,
load FinBERT or alter source data. The three diagnostic aggregation tests use
the repository's isolated root `conftest.py`; **3 passed**. No broad legacy
suite was run. Thresholds and strategy code are unchanged.

Artifacts: [`trend_long_phase0/`](trend_long_phase0/).

- `summary.json`: source revision, input hashes/source counts, replay windows,
  actual universe sizes, capital/flows, first fills and results.
- `archive_manifest.csv`: every loaded CSV's hash, first/last date and bar count.
- `fundamental_coverage.csv`, `fundamental_<date>.csv`: coverage probes and
  complete hard-filter/score evidence (computed, not persisted vendor ratios).
- `*_refreshes.csv`, `*_funnel_daily.csv`, `*_funnel.csv`: quarterly rejection
  reasons, standing eligible counts and entry funnel by year/split.
- `*_equity.csv`, `*_exposure.csv`, `*_fills.csv`, `*_closed.csv`: daily book
  evidence, annual cash/invested summaries, and actual executions.

Branch: `feat/trend-long-profile`. The pre-existing engine edit was already
preserved in `stash@{0}` (`870b7de`): a single unused `news_feed` constructor
argument, with no caller or implementation. It was not reapplied or discarded.
Unrelated untracked notebooks/output files were left untouched.

## 7. What I'd distrust

- **Survivorship and selection:** no delisted cohort/master or historical
  membership; only 209 equities; start-date-dependent replay samples.
- **Fundamentals:** synthetic filing dates, very old statements, mixed sources,
  and unverified historical restatements; the earliest loader date is not a
  proof of true PIT availability.
- **News:** no real headline history; a gap/volume proxy cannot validate an
  exit-only hard-negative news rule. No news edge claim is supported.
- **Defensive sleeves:** no actual liquid/gold history; yield accrual and a
  separately reported gold leg remain unvalidated modelling assumptions.
- **Execution:** the legacy stop behavior differs from the claimed contract;
  OHLC cannot establish intrabar ordering, liquidity, open-circuit sellability
  or fills in a real gap. Even a corrected backtest cannot cap open-gap losses.
- **Historical published numbers:** current inputs do not reproduce them;
  baseline costs undercharge delivery. No claim of ranked strategy edge,
  placebo outperformance, post-tax results, or production readiness is made.
