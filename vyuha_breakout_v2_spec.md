# VYUHA Engine — Breakout Overlay v2.2
### Percentile-Normalized, Regime-Filtered, Structurally-Stopped Trend/Breakout System — with Entry, Portfolio, and Exit Management

Refinement of v2: same core philosophy (rolling-percentile normalization, structural stop, regime filter), with internal inconsistencies resolved, plus a proper entry/re-entry framework, portfolio-level risk controls, and an expanded exit layer — all of which were missing from the original spec, which only really had a trigger and a trailing stop.

---

## 1. Percentile Normalization (Core Primitive)

$$P_N(X_t) = \frac{1}{N}\sum_{i=t-N+1}^{t} \mathbb{1}[X_i \le X_t]$$

- Self-referential (each stock ranked against **its own** trailing history), not cross-sectional.
- Window is trailing-inclusive of bar `t` — uses only data available at close of day `t`, so no lookahead.
- **Warm-up cost:** `T`, `M`, `B` need `SMA_200` (200 bars) *plus* a 252-bar percentile window before the first valid reading ≈ **~450 trading days (~1.8 years)** of history per stock before it's tradable. For Nifty 500 this materially shrinks the tradable universe early in a backtest and interacts with your existing survivorship-bias issue. Track "excluded for warmup" separately from "excluded for no signal" in diagnostics.
- ⚠️ **Regime-relative side effect:** percentile normalization is relative to *recent own history*, not absolute strength. A stock in its 10th consecutive strong month needs an even more extreme move just to stay in the 90th percentile, while a choppy flat stock can hit the 90th percentile on a trivial move. Not a bug, but worth a diagnostic: plot `T` percentile vs. forward trend continuation, split by "already-trending" vs "newly-trending" stocks.

---

## 2. Component Scores

### Trend
$$T_{raw} = 0.5\frac{C_t - SMA_{20,t}}{ATR_{14,t}} + 0.3\frac{SMA_{20,t}-SMA_{50,t}}{ATR_{14,t}} + 0.2\frac{SMA_{50,t}-SMA_{200,t}}{ATR_{14,t}}, \quad T = P_{252}(T_{raw})$$

### Momentum
$$M_{raw} = 0.6\frac{R_{20}}{\sigma_{20}} + 0.4\frac{R_{60}}{\sigma_{60}}, \quad M = P_{252}(M_{raw})$$
Assumption to document in code: `R_N` = cumulative return over trailing N days, `σ_N` = std dev of daily returns over the same window. Whether σ is annualized doesn't change the final percentile (it's a constant scalar for all `t`) — pick one, document it, move on.

### Compression *(unchanged)*
$$C = 1 - P_{120}(BBW_t)$$
Window is 120 vs. 252 for everything else — reasonable (compression cycles are shorter-horizon) but untested here. Treat as a hyperparameter, sensitivity-test (60/120/252) before locking in.

### Breakout
$$B_{raw} = \left(\frac{C_t - H_{20,t}}{ATR_{14,t}}\right)\times\left(\frac{V_t}{SMA_{20}(V)_t}\right), \quad B = P_{252}(B_{raw})$$

🐛 **Critical:** `H_{20,t}` must be the highest close/high over `[t-20, t-1]`, excluding bar `t`. If it includes the current bar, `C_t > H_{20,t}` is impossible or trivial. Add an explicit unit test for this — classic off-by-one in breakout systems.

---

## 3. Composite Score — corrected

$$S_{raw} = 0.30T + 0.25M + 0.20C + 0.25B$$

`S_raw` is a weighted sum of four (roughly) uniform[0,1] variables — that sum is **not** uniform. Under an i.i.d.-uniform approximation with these weights, `S_raw` is approximately bell-shaped: mean ≈ 0.5, std ≈ 0.146, `P(S_raw > 0.75) ≈ 4–5%`, not 25%. Real components are likely positively correlated during genuine breakouts (fattens the tail somewhat) but the core claim — "0.75 = top quartile" — is false as originally written.

**Fix — re-percentile the composite:**
$$S_{tech} = P_{252}(S_{raw})$$

Now `S_tech > 0.75` genuinely means top quartile of the stock's own historical composite-score distribution. Diagnostic requirement: plot the empirical distribution of `S_raw` and confirm it isn't flat — that's the proof the fix mattered.

---

## 4. Trading Logic

### Condition 1 — Regime Filter
$$Nifty_{C,t} > Nifty_{SMA200,t}$$
Evaluated at close of signal day `t`, gates **new entries** at `t+1` only. Does not force-close existing positions — see Section 8 for how regime is instead used to manage risk on open trades.

### Condition 2 — Trigger
$$C_t > H_{20,t} \quad \text{(20-day high excluding bar } t\text{)}$$

### Condition 3 — Filter
$$S_{tech} > 0.75$$

### Condition 4 — Liquidity
$$SMA_{20}(V)_t \times C_t \ge \text{MinTurnover}$$
`B_raw` rewards volume *spikes*, not absolute liquidity. Set `MinTurnover` relative to account size / slippage tolerance.

### Selection Rule
When same-day candidates passing all filters exceed open portfolio slots (target 3–5 concentrated positions): rank by `S_tech` descending, fill best-first, then apply the sector/correlation cap in Section 7 before finalizing.

---

## 4A. Entry Management

The original spec had exactly one entry rule ("buy at open"). That's not enough given this strategy explicitly selects for volume-spike, gap-prone setups.

### Order type
- **Baseline:** market-at-open. Simplest, but this strategy selects specifically for the setups most prone to opening-gap slippage — `B_raw` rewards volume spikes, and volume spikes correlate with wider gaps.
- **Alternative to backtest as a config branch:** cap entry price at `Open_{t+1} × (1 + X%)` (e.g., X = 1%) — if the gap exceeds that, skip the trade rather than chase. This trades a small number of missed (usually the most extended) breakouts for materially lower average entry slippage. Report both branches side by side rather than assuming one is better; a "let the trend breathe" system benefits more from cheap entries than from catching every gap.

### Entry skip
- Unchanged from v2.1: if `Open_{t+1} ≤ SL_0`, skip entirely — the signal has already been invalidated before the fill.

### Re-entry / cooldown (missing from the original spec entirely)
Without this rule, a stock sitting right at a breakout level can generate repeated stop-out → re-entry → stop-out cycles in quick succession, bleeding fees and slippage on pure chop.
- **Rule:** after a stop-out, the same stock is ineligible for re-entry for `N` trading days (default 10 — roughly half the ATR-stop's typical mean-reversion window; treat as a tunable parameter).
- No cooldown after a position is closed by portfolio-level risk management (Section 7) rather than a stop — that's a capital-allocation exit, not a signal failure, and shouldn't penalize the stock's re-eligibility.

### Pyramiding / add-on entries (optional, off by default — flag clearly as an experiment)
Given the known CAGR shortfall in the existing engine, one lever that specifically targets *that* problem (rather than just tightening risk further) is adding to winners:
- Allow **one** add-on entry if price has moved `≥ +2 ATR_{14}` favorably from original entry, `S_tech` is still `> 0.75`, and the position isn't already at the portfolio heat cap (Section 7).
- Size the add-on with the same 0.5%-risk formula, but reference risk to the *current* ratcheted stop (`ActiveStop_t`), not the original entry stop — the add-on's own risk should be measured from where it actually gets stopped out, not from the first tranche's entry price.
- This increases winner exposure without increasing *initial* risk, but does increase correlation and concentration within the position — test as an explicit A/B branch against no-pyramiding, don't assume it helps.

---

## 5. Structural Stop Loss

$$SL_0 = \max\big(L_t - 0.2\,ATR_{14,t},\ \ P_{entry} - 2.0\,ATR_{14,t}\big)$$

where `L_t` = low of the breakout candle (signal day `t`, not the entry day). Using **2.0 ATR** for the fail-safe cap (prose said 2, formula said 2.5 in the original — pick one, this doc uses 2.0; change if you intended otherwise).

---

## 6. Position Sizing

$$Shares = \left\lfloor \frac{0.005A}{P_{entry} - SL_0} \right\rfloor, \quad \text{subject to } Shares \times P_{entry} \le \text{available capital for the slot}$$

⚠️ **Needs reconciling with the existing SIP-style fixed-monthly-contribution + whole-share-constraint capital model already in the engine**, not just noted. Decide explicitly, as a config flag: does risk-sizing cap the trade, does available SIP cash cap the trade, or does risk-sizing determine how much of already-available cash to deploy (never exceeding it)?

---

## 7. Portfolio-Level Risk Management (new — not in the original spec at all)

A concentrated 3–5 position book with per-trade 0.5% risk sizing says nothing about *aggregate* risk or correlation, and breakout signals cluster — a genuine sector rotation can pass the selection filter for 3+ names on the same day, at which point "5 positions" is really one correlated bet with extra steps.

### Aggregate open-risk cap
$$\sum_{\text{open positions}} \frac{(P_{entry,i} - SL_{0,i}) \times Shares_i}{Account} \le 2.5\%$$
(~5× the per-trade 0.5% risk, consistent with a 3–5 position book, but caps the case where several positions are stopped simultaneously.)

### Sector / correlation concentration cap
No more than 2 of the (3–5) slots in the same sector at once, even if a 3rd candidate outranks something else on `S_tech`. Enforce this **after** the Section 4 selection ranking, as a filter on it — don't let a high-ranking correlated candidate silently override the cap.

### Slot replacement priority
When a slot frees up, re-run the Section 4 selection rule against the current candidate queue, then apply the sector cap before confirming the fill.

---

## 8. Trade & Exit Management

The original spec's *only* exit mechanism was the chandelier trail. Its formula could also technically loosen (if ATR spiked while price was flat, since `HighestClose − 3×ATR` isn't inherently monotonic). Both issues addressed below.

### Core ratcheted stop (unified structural + trailing)
$$TS_{entry} = SL_0$$
$$HighestClose_t = \max(C_{entry}, \dots, C_t)$$
$$TS_t = \max\big(TS_{t-1},\ \ HighestClose_t - 3\,ATR_{14,t}\big)$$

This single monotonic series subsumes both the structural and trailing stop — it can never move against the position.

### Breakeven floor (recommended default — doesn't conflict with "let it breathe")
Once unrealized gain reaches `+1R` (`C_t ≥ P_entry + (P_entry - SL_0)`), add a floor:
$$ActiveStop_t = \max(TS_t,\ P_{entry})$$
This is a strict addition to the existing ratchet, not a replacement — it just guarantees a trade that's already proven itself by 1R can't round-trip into a loss. Cheap to implement, meaningfully cuts the tail of "worked, then didn't" trades.

### Regime-based stop tightening (optional, not a forced exit)
If `Nifty_{C,t}` drops below `Nifty_{SMA200,t}` while a position is open, tighten the trailing multiplier from 3 ATR to 2 ATR rather than closing the position outright. Manages risk down without the whipsaw of a hard regime-flip exit.

### Signal-decay exit — optional, **off by default**
Exit if the underlying trend signal itself deteriorates (e.g., `T` falls below its own 0.25 percentile) even before price touches the trailing stop. This can catch a character change in the trend earlier than a pure price-based stop.
**Caution:** signal-based exits are a common source of exactly the CAGR shortfall already diagnosed in this engine — they tend to exit real trend trades too early on normal pullbacks. Backtest as a separate `EXIT_MODE` branch and compare against stop-only before considering it a default; do not enable it on the strength of intuition alone.

### Explicitly excluded by design
No fixed profit target. No time-based / failed-breakout exit. These were deliberately removed in the prior revision specifically to let winners run — stating this explicitly so a future session doesn't quietly reintroduce a profit cap and reproduce the CAGR shortfall this whole framework exists to fix.

---

## 9. Open Design Decisions (resolve before coding)

| # | Item | Status |
|---|---|---|
| 1 | Fail-safe stop multiplier: 2.0 or 2.5 ATR? | Defaulted to 2.0 — confirm |
| 2 | Compression window: 120 vs. 252 for consistency? | Flagged, needs sensitivity test |
| 3 | Risk-sizing vs. SIP-cadence capital model conflict | **Unresolved — needs your decision** |
| 4 | Point-in-time Nifty 500 constituents (survivorship bias) | Known existing issue — applies here too |
| 5 | Transaction cost model (STT, GST, exchange charges, stamp duty) | Known existing gap — applies here too |
| 6 | STCG @ 20% (post-Budget 2024) on realized exits | Known existing bug — applies here too |
| 7 | Regime filter on exits (hold + tighten vs. force-close) | Default: hold + optional tighten, no force-close — confirm |
| 8 | EXIT_MODE integration | Ratcheted chandelier is the natural candidate for the trailing-stop-only EXIT_MODE branch from Phase 10 |
| 9 | Order type: market-at-open vs. gap-capped limit | Backtest both, don't assume |
| 10 | Re-entry cooldown length (default 10 days) | Tunable, needs sensitivity test |
| 11 | Pyramiding module (on/off) | Off by default — A/B test only |
| 12 | Aggregate open-risk cap (default 2.5%) | Needs your sign-off on the number |
| 13 | Sector concentration cap (default: max 2 of 3–5 slots/sector) | Needs sector taxonomy source (NSE classification vs. custom) |
| 14 | Signal-decay exit (on/off) | Off by default — separate EXIT_MODE branch only, high risk of reintroducing CAGR shortfall |

---

## 10. Backtest Implementation Prompt

```
Implement and backtest the VYUHA breakout overlay (v2.2 spec) as a new signal module,
integrated with the existing VYUHA Engine (report.py, EXIT_MODE framework).

DATA & UNIVERSE
- Point-in-time Nifty 500 constituents only (no survivorship bias — do not apply the
  current constituent list retroactively; this was a known prior bug).
- Exclude any stock with < ~450 trading days of history at time t (needed for SMA_200 +
  252-bar percentile warm-up). Log excluded-for-warmup separately from excluded-for-no-signal.
- OHLCV daily bars, Nifty 50/500 index series for regime filter. Sector classification
  data for the sector concentration cap (Section 7).

SIGNAL CONSTRUCTION
1. Compute T_raw, M_raw, B_raw, BBW exactly as in Section 2. H_20,t excludes bar t —
   add an explicit unit test (e.g. assert it's built on shift(1).rolling(20).max(),
   not rolling(20).max() including the current bar).
2. Percentile-rank T, M, B via P_252 (trailing 252-bar empirical CDF, self-inclusive,
   no lookahead — window [t-251, t] using only data through close of day t).
3. C = 1 - P_120(BBW_t).
4. S_raw = 0.30T + 0.25M + 0.20C + 0.25B
5. S_tech = P_252(S_raw)  <-- re-percentile the composite, do not filter on S_raw directly.
   Diagnostic requirement: plot the empirical distribution of S_raw across the full
   backtest sample and confirm it is NOT approximately uniform. Log the actual observed
   P(S_raw > 0.75) as a sanity check against the ~4-5% theoretical estimate.

ENTRY LOGIC
- Regime filter: Nifty_C,t > Nifty_SMA200,t (close of day t), gates new entries at t+1
  only. Does not force-close existing positions.
- Trigger: C_t > H_20,t (20-day high/close over [t-20, t-1], excluding bar t).
- Filter: S_tech > 0.75. Liquidity filter: SMA_20(V)_t * C_t >= MinTurnover (config param).
- Selection rule: rank same-day passing candidates by S_tech descending, fill open slots
  best-first, then apply the sector concentration cap (max 2 of 3-5 slots per sector) as
  a filter on the ranked list.
- Order type: implement BOTH market-at-open (baseline) and gap-capped limit
  (Open_{t+1} * (1+X%) cap, default X=1%, config param) as separate backtest branches.
  Report entry slippage and fill-rate differences between the two explicitly.
- Skip entry entirely if Open_{t+1} <= SL_0 (signal invalidated pre-fill).
- Re-entry cooldown: after a stop-out, the same stock is ineligible for new entry for
  N trading days (default 10, config param). No cooldown after a portfolio-risk-driven
  exit (Section 7) — only after a stop-loss exit.
- Pyramiding (OFF by default, implement as a togglable module): one add-on entry allowed
  if price >= entry + 2*ATR_14 and S_tech still > 0.75 and position isn't at the
  portfolio heat cap; size via the same 0.5%-risk formula referenced to ActiveStop_t
  at time of add-on, not to original entry. Must be independently A/B tested against
  no-pyramiding baseline, not enabled by default.

STOP LOSS / EXIT
- SL_0 = max(L_t - 0.2*ATR_14,t, P_entry - 2.0*ATR_14,t)   [L_t = low of signal-day candle]
- TS_entry = SL_0
- TS_t = max(TS_{t-1}, HighestClose_t - 3*ATR_14,t)   [monotonic ratchet, never loosens]
- Breakeven floor: once unrealized gain >= 1R, ActiveStop_t = max(TS_t, P_entry).
- Exit when price breaches ActiveStop_t — explicitly document and test both intraday-low
  and close-only trigger variants, this materially affects fill realism.
- Regime-based tightening (config toggle, default ON, non-forcing): if Nifty flips below
  SMA200 while a position is open, switch the trailing multiplier from 3 ATR to 2 ATR.
  Do NOT force-close on regime flip.
- Signal-decay exit (config toggle, default OFF): exit if T < P_25 of its own trailing
  distribution even before ActiveStop_t is touched. Implement as a distinct EXIT_MODE
  branch and compare against stop-only — do not enable by default, flag explicitly if
  it reproduces the previously-diagnosed CAGR shortfall pattern (winners cut early).
- No fixed profit target. No time-based / failed-breakout exit. Confirm these remain
  absent from the default config in the run output.

PORTFOLIO-LEVEL RISK MANAGEMENT
- Aggregate open-risk cap: sum of (P_entry_i - SL_0_i)*Shares_i across all open positions,
  divided by Account, must stay <= 2.5% (config param). Block new entries that would
  breach this even if they otherwise pass all signal filters.
- Sector concentration cap: max 2 of 3-5 open slots in the same sector (config param,
  needs a sector taxonomy source — flag which one is used, NSE classification vs custom).

POSITION SIZING
- Shares = floor(0.005 * Account / (P_entry - SL_0)), capped by available capital for slot.
- Reconcile explicitly against the existing SIP-style fixed-monthly-contribution +
  whole-share-constraint capital model. Implement as an explicit config choice (risk-sizing
  caps trade size within available SIP cash; never let risk-sizing request more capital
  than the SIP schedule has deployed). State the choice clearly in run config / report output.

COSTS & TAXES
- Full transaction cost model: STT, GST, exchange charges, stamp duty, and slippage
  (especially on gap-up opens following volume-spike breakout signals — quantify this
  explicitly given the strategy selects for exactly these setups).
- STCG at 20% (post-Budget 2024) on exits within the short-term holding threshold.

METRICS & VALIDATION
- Correct CAGR/XIRR (cash-flow-aware — do not treat SIP contributions as returns; add
  a unit test guarding against this specific prior bug).
- Sharpe, max drawdown, expectancy, win rate, average R-multiple.
- Distribution diagnostics for S_raw and S_tech (see Signal Construction step 5).
- Walk-forward validation of component weights (0.30/0.25/0.20/0.25), compression window
  (120 vs alternatives), re-entry cooldown length, and aggregate risk cap — report
  sensitivity, flag fragility to small perturbations.
- Report the warmup-excluded universe size over time so the effective tradable universe
  at each point in the backtest is transparent.
- A/B comparisons required (not optional) for: market-at-open vs gap-capped-limit entry;
  pyramiding on vs off; signal-decay exit on vs off; regime-tightening on vs off.
- Compare full module against the current EXIT_MODE baseline from Phase 10 validation
  as a control.

OUTPUT
- Standard VYUHA report.py output, with the EOY return table bug (previously identified)
  fixed and covered by a regression test.
- Explicit run-config dump showing which items from the "Open Design Decisions" table
  (Section 9 of the spec) were resolved and how, so the run is reproducible and auditable.
```
