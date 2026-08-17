# VYUHA Breakout Overlay v2.2 — Implementation Plan

Companion to `vyuha_breakout_v2_spec.md`. This is sequencing, not spec — read the spec for formulas, read this for build order.

---

## 0. Sequencing Philosophy

Three rules driving the order below:

1. **Fix the measurement before you trust anything measured by it.** The engine currently has a CAGR/XIRR bug, an EOY table bug, a wrong STCG rate, and no transaction cost model. If those aren't fixed first, every backtest number for the new module — good or bad — is meaningless. This is Milestone 0, and nothing else starts before it's done.
2. **Ship a stop-only baseline before any optional module.** Pyramiding, regime-tightening, and signal-decay exits are all explicitly optional in the spec. Build and validate the plain version (structural stop + ratcheted chandelier + breakeven floor) first, so every optional module has a real baseline to be A/B'd against — not a moving target.
3. **Every genuinely open decision gets resolved before the code that depends on it, not during.** Position sizing vs. SIP reconciliation, sector taxonomy source, risk cap %, cooldown length — these are flagged as decision gates below, at the point where they'd otherwise silently become an assumption baked into code.

---

## 1. Milestones

| # | Milestone | Depends on | Deliverable | Decision gate? |
|---|---|---|---|---|
| 0 | **Foundation repairs** | — | Corrected CAGR/XIRR, fixed EOY table bug, STCG @ 20%, point-in-time Nifty 500 list, full transaction cost model. Re-run existing engine against this as the new reference baseline. | No — but nothing else starts until this is re-validated |
| 1 | **Data & universe layer** | M0 | Per-date universe object with warmup-eligibility flags (450+ day requirement), warmup-excluded vs. no-signal logged separately | **Yes — sector taxonomy source (NSE vs. custom)** |
| 2 | **Core signal construction** | M1 | `T_raw, M_raw, B_raw, BBW`, `P_252`/`P_120` percentile functions, `S_raw`, `S_tech`. Unit tests: `H_20` off-by-one guard, no-lookahead percentile test, `S_raw` distribution diagnostic | No |
| 3 | **Trigger, filters, selection** | M2 | Regime filter, trigger condition, `S_tech > 0.75`, liquidity filter, selection/ranking for concentrated slots | No |
| 4 | **Core stop & exit engine (baseline)** | M2 | Structural stop, ratcheted chandelier, breakeven floor — as an isolated, independently testable module. Unit tests: monotonic ratchet (never loosens), gap-through-stop skip | No |
| 5 | **Position sizing** | M0 | Sizing function, config-driven | **Yes — risk-sizing vs. SIP-cadence reconciliation. Blocking.** |
| 6 | **Portfolio-level risk management** | M1, M5 | Aggregate open-risk cap, sector concentration cap, slot replacement priority | **Yes — aggregate risk cap % (default 2.5%, needs sign-off)** |
| 7 | **Entry management extensions** | M3, M4 | Market-at-open vs. gap-capped-limit branches, re-entry cooldown state tracking | **Yes — cooldown length (default 10d)** |
| 8 | **Optional modules (each isolated, none default-on)** | M4, M6, M7 | Pyramiding, regime-based tightening, signal-decay exit — each behind its own flag, each independently A/B'd against the M4 baseline | No (but each requires evidence before default-on) |
| 9 | **Full integration backtest + validation** | M0–M8 | Combined config-driven run, walk-forward validation of weights/window/cooldown/risk-cap, full metrics + diagnostics suite, comparison vs. M0 baseline and existing Phase 10 EXIT_MODE work | No |
| 10 | **Paper / shadow validation** | M9 | Forward shadow run (no real capital), implementation-parity check against backtest expectations, go/no-go readout | Effectively yes — this is the live-capital gate |

Note: these are called "Milestones" rather than "Phases" deliberately, to avoid collision with your existing Phase 10 backtest-validation numbering — Milestone 9 here is meant to slot alongside or after that existing work, not renumber it.

---

## 2. Config Schema (proposal)

Everything identified as a toggle or tunable should be config, not hardcoded — both to support the required A/B testing in M8/M9 and because several values here are explicitly unresolved.

```yaml
breakout_v2:
  windows:
    percentile_main: 252
    percentile_compression: 120
    sma: [20, 50, 200]
    atr: 14

  composite_weights:
    trend: 0.30
    momentum: 0.25
    compression: 0.20
    breakout: 0.25

  filters:
    s_tech_threshold: 0.75
    min_turnover: null          # DECISION: set relative to account size
    warmup_days_required: 450

  stops:
    structural_low_buffer_atr: 0.2
    structural_failsafe_atr: 2.0   # was ambiguous in original spec (2.0 vs 2.5) — confirm
    chandelier_atr: 3.0
    breakeven_floor: true
    regime_tightened_atr: 2.0
    regime_tightening_enabled: false   # optional module, off by default

  entry:
    order_type: "market_open"      # or "gap_capped_limit"
    gap_cap_pct: 0.01
    reentry_cooldown_days: 10      # DECISION: needs sign-off
    pyramiding_enabled: false      # optional module, off by default
    pyramid_trigger_atr: 2.0

  exits:
    signal_decay_enabled: false    # optional module, off by default
    signal_decay_percentile: 0.25

  sizing:
    risk_pct_per_trade: 0.005
    mode: "TBD"                    # DECISION: risk_capped_by_sip | sip_capped_by_risk | risk_only
    whole_share_only: true

  portfolio_risk:
    aggregate_open_risk_cap_pct: 0.025   # DECISION: needs sign-off
    max_positions: 5
    min_positions_target: 3
    sector_cap_per_sector: 2
    sector_source: "TBD"           # DECISION: NSE classification | custom taxonomy

  regime:
    index: "NIFTY50"
    sma_window: 200
```

---

## 3. Suggested Module Layout

Illustrative — map onto your actual repo structure, don't take file paths literally.

```
vyuha/
  signals/
    breakout_v2/
      raw_scores.py        # T_raw, M_raw, B_raw, BBW  (M2)
      percentile.py        # P_N, no-lookahead-tested   (M2)
      composite.py         # S_raw, S_tech              (M2)
      triggers.py          # regime, trigger, liquidity, selection (M3)
  execution/
    entry.py               # order type branches, cooldown state   (M7)
    sizing.py               (M5)
  exits/
    chandelier.py          # ratchet, breakeven floor    (M4)
    optional/
      pyramiding.py         (M8)
      regime_tighten.py     (M8)
      signal_decay.py       (M8)
  risk/
    portfolio.py           # aggregate cap, sector cap   (M6)
  universe/
    point_in_time.py       # constituents, warmup flags  (M1)
  costs/
    tax_and_fees.py        # STT/GST/stamp/STCG          (M0)
config/
  breakout_v2.yaml
tests/
  test_percentile_no_lookahead.py
  test_h20_exclusion.py
  test_chandelier_monotonic.py
  test_gap_through_stop_skip.py
  test_cagr_xirr_regression.py
  test_eoy_table_regression.py
```

---

## 4. Decision Gates — Consolidated

Everything blocking, in one place, so nothing gets defaulted silently during implementation:

1. **Position sizing mode** (M5) — does risk-sizing cap the SIP tranche, does the SIP tranche cap risk-sizing, or is it risk-only? This changes trade sizes materially.
2. **Sector taxonomy source** (M1) — NSE classification vs. a custom mapping. Needed before the sector cap (M6) can be built.
3. **Aggregate open-risk cap %** (M6) — proposed default 2.5%, needs explicit sign-off, not just acceptance of the default.
4. **Re-entry cooldown length** (M7) — proposed default 10 trading days, tunable, should be walk-forward validated in M9 rather than fixed permanently at the proposed default.
5. **Fail-safe stop multiplier** (M4) — spec originally had both 2.0 and 2.5 ATR; this plan assumes 2.0, confirm before M4 locks it in.
6. **MinTurnover liquidity threshold** (M3) — has no sensible default; must be set relative to actual account size and slippage tolerance before M3 is usable.

---

## 5. Testing Checklist

- [ ] `H_20,t` excludes bar `t` (unit test, not just code review — this is the classic silent-failure bug)
- [ ] `P_252`/`P_120` use no future data at any point `t` (no-lookahead test)
- [ ] `S_raw` distribution is empirically non-uniform; `S_tech` distribution is empirically ~uniform (validates the composite-score fix from the spec)
- [ ] Chandelier stop series is monotonic non-decreasing per position (never loosens)
- [ ] Entry is skipped, not filled-then-immediately-stopped, when `Open_{t+1} ≤ SL_0`
- [ ] CAGR/XIRR regression test: SIP contributions are never counted as returns
- [ ] EOY return table regression test (previously identified bug in `report.py`)
- [ ] STCG applied at 20%, not 15%
- [ ] Point-in-time universe test: a stock delisted/removed mid-backtest doesn't silently vanish from historical dates where it was actually a constituent
- [ ] Re-entry cooldown correctly blocks re-entry only after stop-loss exits, not after portfolio-risk-driven exits
- [ ] Each optional module (pyramiding, regime-tightening, signal-decay) has a standalone A/B result vs. the M4 baseline before being considered for default-on

---

## 6. Effort & Risk Notes (qualitative)

| Milestone | Rough effort | Highest risk |
|---|---|---|
| 0 | M–L | Easy to think it's "done" without a real regression test suite; these are exactly the bugs that hide until a specific edge case |
| 1 | S–M | Point-in-time constituent data may simply not be available/clean at the granularity needed — verify data availability before committing to the design |
| 2 | M | The `H_20` off-by-one is the single most likely silent bug in the whole plan — budget real test time here, not just implementation time |
| 3 | S | Low risk if M2 is solid |
| 4 | M | Get the intraday-low vs. close-only stop-trigger decision explicit and tested both ways — this alone can materially change results |
| 5 | S (code), but **blocked** until decision gate #1 is resolved | Don't let this get implemented around an assumed default — it's a real product decision |
| 6 | M | Sector data quality/coverage for smaller Nifty 500 names may be inconsistent |
| 7 | S–M | Cooldown state needs to persist correctly across the backtest loop — easy to get subtly wrong in a vectorized backtest engine |
| 8 | M each, ×3 | Keep these genuinely isolated — the temptation is to let pyramiding and the aggregate risk cap interact in ad hoc ways rather than through the defined portfolio-risk layer |
| 9 | L | This is where most of the calendar time should go — walk-forward validation done properly is slow by design |
| 10 | Ongoing | Not really "effort" in the coding sense — it's calendar time before live capital, budget for it explicitly rather than treating M9 as the finish line |
