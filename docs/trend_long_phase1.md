# trend_long — Phase 1 checkpoint: costs, tax and execution

**Status: Phase 1 complete; stopped before Phase 2 strategy code.**

All future performance tables must be labelled **“backtest on a selected,
survivor-biased 209-name archive”**. This checkpoint contains infrastructure
and synthetic tests, **not strategy performance, validation or live claims**.
The Phase 0 report and evidence remain in [trend_long.md](trend_long.md) and
[trend_long_phase0/](trend_long_phase0/).

## Scope and approved amendments

The user authorized the current archive, a separate trend-only replay path,
monthly universe admission, a 540-day fundamental-staleness rule, and an
unscreened technical headline with the disqualifier screen only as an ablation.
Those **strategy rules are reserved for Phase 2**. Phase 3 will independently
report both capital scenarios, 200 placebo seeds, robustness, OOS and legacy
Investor references. No archive-widening ingestion was performed; optional
Phase 1b still requires permission.

The new infrastructure does not call or reuse the old Investor decision engine,
executor, metrics or backtest_v2 cost model. The existing package `__init__.py`
files still eagerly import legacy modules transitively; those initializers were
left untouched. No legacy behavior, live guard, settings mode validator,
DB table or paper/live adapter was changed. `STRATEGY_MODE=trend` validation is
reserved for the next phase; this checkpoint has no runnable trend strategy.
The existing engine `news_feed` argument remains stashed and uncommitted.

## Diff summary

| Path | Phase 1 change |
|---|---|
| `config/thresholds.yaml` | New, separate `trend_long:` infrastructure block; no existing values changed |
| `investor/trend_config.py` | Required, typed cost/execution/tax/yield/reporting/capital settings; no legacy fallbacks |
| `investor/trend_costs.py` | Delivery fee breakdown and standalone liquid-yield accrual |
| `investor/trend_tax.py` | Annual tax scenario, calendar-month holding boundary, losses and carry expiry |
| `backtest_investor/trend_execution.py` | Independent session clock, next-open orders, standing stops, fees and realized lots |
| `backtest_investor/trend_reporting.py` | Net-of-cost and tax-overlay XIRR, including opening capital and exact SIP dates |
| `tests/test_trend_{costs,execution,tax,reporting}.py` | Isolated synthetic tests, including an end-to-end fees/tax/XIRR reconciliation |
| `docs/trend_long.md`, this file | Current checkpoint, assumptions, source checks, limitations and next gate |

These are library components, not a signal generator or full strategy replay
runner. No ranking, admission, regime scoring, sizing, Chandelier computation,
news classifier, or tax-deferral trading logic has been written.

## Delivery cost model

All amounts are computed on the **slippage-adjusted fill notional**. Rates are
decimal fractions in YAML; quote results are unrounded model amounts rather
than an exact per-contract-note rupee rounding replica.

- STT **0.1% on both equity-delivery buys and sells**.
- Stamp **0.015% on buys only**.
- Exchange transaction rate configurable by venue; NSE default **0.00297%**,
  BSE default **0.00375%**. These are modelling assumptions, not a full historical
  tariff. BSE security-group charges and broker-specific schedules need overrides.
- SEBI **0.0001%** on both sides.
- GST **18%** on exchange, SEBI and brokerage. Brokerage is included in total
  costs itself, not just in the GST base; rate and per-order cap are configurable.
- Delivery brokerage defaults to zero. DP defaults to **₹15.34 inclusive of GST**,
  once per symbol per sell day. The amount/inclusive flag are broker assumptions.
  If DP is configured ex-GST, the model adds its GST once too.
- The executor deduplicates DP across partial exits and a same-day stop; another
  scrip or another sell day incurs its own charge. Failed fills incur no fee.
- Default slippage is **0.15% each side**. Cost multiplier changes every explicit
  fee; slippage has separate buy/sell parameters. Tests assert perturbations land.

**Legacy incident recorded, not repaired:** backtest_v2 omits buy delivery STT,
undercharging by ~0.1% of buy notional per round trip. Its 0.003% stamp is also
below delivery's 0.015%. It computes GST on assumed brokerage without adding
that brokerage to total costs, and charges DP per sell transaction. Calling
buy-side STT merely “conservative” for delivery is incorrect. The existing code
and published Investor figures remain unchanged.

## Execution contract (trend path only)

1. `run_session(t)` executes before `queue_close(t, orders, stops)` publishes
   one immutable close decision. Repeated sessions, future signal dates and
   duplicate close publication are rejected. The first session cannot fill an
   order that has not yet been published.
2. At t+1, pending discretionary sells fill at the open; an already-gapped
   protective stop fills at the open. Those opening proceeds can fund buys.
3. New buys fill at the open plus buy slippage, subject to cash including fees.
   An unaffordable order is skipped, not silently resized. Adds are rejected,
   including an attempted add whose holding was just removed by a gap stop.
4. For a standing stop computed at close t: if **open(t+1) <= stop**, use the
   open; otherwise if **low(t+1) <= stop**, use the stop. Apply sell slippage to
   either basis. Intraday-stop proceeds **cannot fund earlier opening buys**.
5. No symbol bar on t+1 means **no fill**. Day orders expire; hard SELL orders
   persist across later close publications until a valid bar permits execution.
   Stops stay with held positions. No DB order persistence/live wiring is implied.
6. A new order carries the **absolute initial stop published at close t**.
   It is not re-anchored to a gapped opening fill; no fill-day ATR is read or
   invented. Initial actual risk is fill minus that known stop. If the entry
   opens below its stop, an immediate gap-stop sale uses the open, not an
   invented above-open stop fill. `None` leaves the stop unset. Published stops
   cannot decrease; an unavailable update cannot erase a known stop. Actual
   Chandelier calculations and breakeven rules are Phase 2 work.
7. The final session cancels pending buys and liquidates held shares **at the
   final open**, with sell slippage/fees. Its later close/range cannot gate that
   opening sale. No final bar/open/volume means no liquidation: unresolved names
   are returned explicitly, and realized-terminal XIRR reporting refuses the run.

Every price read is sliced `.loc[:as_of]` before use; exact dates, positive finite
prices/volume and valid bar ranges are required (final open requires only its
open/volume). Callers must explicitly supply `market_ready`; anything other
than `True` blocks new buys. Hard exits are never blocked by that permission.
Required index/VIX freshness and completed-bar regime evaluation will be supplied
by Phase 2, not guessed or made fail-open inside the execution library.

OHLC cannot reconstruct order-book depth, intrabar sequencing or circuit-lock
sellability. A modelled stop fill is not a guarantee of execution in an open
circuit or overnight gap. Same-day entry-stop exits still use this profile's
configured delivery friction; no mixed intraday tariff is inferred.

## Liquid sleeve and capital scenarios

The liquid sleeve accrues **6% effective annual yield by ACT/365 calendar days**,
configurable. `accrue_liquid` returns interest, not a tradable ETF price. The
replay must accrue the old balance before crediting a new SIP, so the deposit
cannot earn yield before it arrived. This is tested as a pure function; deployment
at monthly rebalance and full sleeve bookkeeping are Phase 2 work.

This matches the cash benchmark's nominal 6% assumption, **not its exact clock**:
legacy `cash_benchmark` uses business-day steps with 252 periods/year. That
implementation is unchanged; any residual day-count difference must be reported.
No LIQUIDBEES/GOLDBEES price history or gold return has been invented here.

Two independent YAML scenarios, not pooled:

| Scenario | Initial capital | Monthly SIP |
|---|---:|---:|
| `lump` | ₹10,00,000 | ₹0 |
| `account_sip` | ₹1,00,000 | ₹1,000 |

No real or archive strategy run has yet been performed for either new profile.

## Tax model and return-reporting convention

This is a **fixed FY2026–27 parameter scenario applied across the replay**, not
historical tax legislation or tax advice. `tax.law_verified` remains **false**;
`annual_tax` refuses to run without `acknowledge_unverified_law=True`. Outputs
carry the scenario and verification status. Do not relabel them verified tax.

- STCG **20%** for holdings **<= 12 calendar months**; LTCG **12.5%** for holdings
  strictly longer than that, on aggregate net LTCG above **₹1.25 lakh per FY**.
  Leap years and the exact anniversary are tested; 365 is not a substitute.
- FY starts April 1. Exemption is annual, **not per trade**, and is applied after
  permitted losses. Current-year losses are applied before brought-forward ones;
  within brought-forward losses LT is used against LT first, and older vintages
  are consumed first. ST losses can offset ST or LT; LT losses cannot offset ST.
- Unused capital losses carry for **eight succeeding FYs**, then expire. Timely
  loss-return filing is assumed. Unused exemption is not carried.
- **4% cess** on tax plus configured surcharge. Surcharge defaults to zero;
  no total-income thresholds, marginal relief, slab/basic-exemption/rebate model,
  other account's gains/losses, grandfathered 2018 basis or advance-tax interest
  is inferred from a strategy-only book.
- Economic realized P&L includes **all** allocated buy/sell costs exactly once.
  Taxable gains separately exclude STT from deductible expenses by default.
  Other modelled transaction costs are treated as deductible expenses; this is
  an explicit simplification, not a personal filing opinion.
- Sleeve accrual may be passed as dated income. It is taxed at a configurable
  **30% personal-rate assumption plus cess**, not as equity capital gains;
  capital losses cannot shelter that income. The same ordinary-tax assumption
  need not describe an actual liquid ETF distribution/redemption.
- Soft-exit tax deferral remains configured **OFF** and is not implemented yet.

**XIRR convention:** start with the initial capital outflow, add every actual
dated SIP outflow, then final-open net proceeds. Negative and flat XIRRs are
valid; a no-root/multiple-root/unconverged result is unavailable, not zero.
Same-day deposits are summed, never overwritten. No CAGR is mixed into a SIP
return. The new reporting path does not reuse legacy metrics, which omit opening
capital in contributed XIRR and return unavailable for loss-making cashflows.

**Post-tax XIRR is a reporting overlay:** tax is funded **outside the simulated
account** at each FY end; the last partial FY is reserved on the terminal date.
Tax payments are dated additional investor cash outflows, not strategy deposits,
and the strategy's trading cash/flow schedule is not altered. Thus this is **not**
a tax-debited, reinvestment-reduced replay. It cannot silently spend tax cash
inside a small account or require an unmodelled forced sale. The eventual report
must disclose this convention beside post-tax XIRR and show the annual taxes.

### Official source checks — accessed 2026-10-19

1. [NSE: SEBI fees, stamp, GST and STT](https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies),
   page updated **17 April 2026**. Confirms equity-delivery buy/sell STT 0.100%,
   delivery buy stamp 0.015%, SEBI 0.0001%, broker-service GST 18%. Its April 2026
   STT table distinguishes equity shares from ETF units/derivatives; the stock
   model must not be applied indiscriminately to a future traded defensive ETF.
2. [PIB / Ministry of Finance, 23 July 2024](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2035596):
   supports 20% STCG, 12.5% LTCG, ₹1.25 lakh annual allowance and listed assets
   held **more than one year** being long term. This is a Budget announcement,
   not a complete enacted FY2026–27 tax-law verification.
3. [Income Tax Department: ITR-2 FAQ, questions 12 and 14](https://www.incometax.gov.in/iec/foportal/help/e-filing-itr2-form-faq):
   listed-share holding-period rule, permitted ST/LT loss set-off, eight-year
   carry-forward, and timely-return requirement. The page also contains older
   rules for other asset classes, so it is not proof all FY2026–27 law is current.
4. [Income Tax Department: AY2026–27 guidance](https://www.incometax.gov.in/iec/foportal/help/individual/return-applicable-2),
   updated **29 May 2026**: 4% health/education cess on income tax plus surcharge.
   **AY2026–27 is not FY2026–27**; this distinction is why verification is not
   marked complete.
5. [Union Budget 2026 memorandum](https://www.indiabudget.gov.in/doc/memo.pdf),
   section II, PDF printed page 5: tax-year 2026–27 under the Income-tax Act 2025;
   states no change proposed to the described tax rates. A proposal and general
   continuation statement are not an audit of the final enacted special-rate,
   loss, deduction and cess provisions.

Attempts to retrieve CBDT's older capital-gains FAQ/tutorial PDFs returned HTTP
403. No third-party tax blog was substituted as legal authority. A complete
review of enacted FY2026–27 law, including Income-tax Act 2025 transition and
final Finance Act provisions, remains necessary before relying on the model.

SHA-256 of the fetched responses, for audit traceability (HTML may change):

| Source | SHA-256 |
|---|---|
| NSE | `4e7c0dc8bb3b59e17ba41e98f80fb54b3a8c10c5143dd9566f7214c97beeaadd` |
| PIB | `50154c9bf2124aee8e8b55dd2fa408617e4f71b1ef9f09ba6c928648dda6e3d0` |
| ITR-2 FAQ | `47dfaab7d42342e8169dce0f80f8d54d98431fa5d3c668860dad076daea0f8ef` |
| AY guidance | `3cf04ab40e8070c71b5dbc05a09037edbc244da25ca7bf35250b4a7abee110a5` |
| Budget memorandum PDF | `78f81425565e23b16424b113d6a91e13292d912a8d55b75c5387f195159f3416` |

## Validation at this checkpoint

```bash
.venv/bin/python -m pytest -q \
  tests/test_trend_costs.py tests/test_trend_execution.py \
  tests/test_trend_tax.py tests/test_trend_reporting.py \
  tests/test_investor_phase0.py tests/test_investor_lookahead.py \
  tests/test_investor_mode_config.py tests/test_investor_pipeline.py
```

**162 passed**, no warnings at the checkpoint run. Root `conftest.py` sets the
isolated temporary DB before application imports; a new test asserts the test
URL is not `vyuha.db`. No test downloads data, accesses a live broker, invokes a
classifier or loads FinBERT. No broad legacy suite was run. Source checks above
were separate HTTP document retrievals, not test dependencies.

Coverage includes costs/GST/DP, gap-through and exact-touch stops, no-bar/no-fill,
final-open liquidation/cancellation, missing-regime buy blocking, persistent hard
exits, stop update monotonicity/None, entry-day stops, adds disabled, open/intraday
cash ordering, partial-lot expense allocation, FY set-off/exemption/carry expiry,
calendar anniversaries, initial capital/SIP signs/dates, negative XIRR and an
end-to-end net-of-cost/post-tax reconciliation. Rank-feature look-ahead tests,
sizing/universe/regime rules and strategy robustness do not exist yet.

## Reporting obligations retained for Phase 3

The benchmark comparison is **biased in the strategy's favour** by the selected,
survivor-biased 209-name stock archive. Separately, the benchmark's auto-adjusted
TRI-like series is more generous than a raw price index; both caveats must remain
visible, not be used to cancel each other rhetorically. Random picks from the
same trend-eligible set, **200 seeds**, at matched exposure/sizing are the primary
ranker-edge evidence. Failure to beat their 90th percentile must be stated as
**no demonstrated ranker edge**. No placebo/ablation result is claimed here.

Re-run legacy Investor references on this archive and label their frozen sample
sizes; the published −0.31% OOS result does not reproduce in Phase 0. Keep the
new headline free of fundamental screening; report the screen's actual veto
name-days per year only in the separate, stale-record-aware ablation.

Still distrust survivorship, stale/restated fundamentals, no real news history,
synthetic liquid/gold sleeves, broker/circuit liquidity, and gap-through losses.
FIN-03, FIN-04, STR-01 and ADD-02 remain open. Nothing in this checkpoint fixes
those application issues or enables live/paper integration.
