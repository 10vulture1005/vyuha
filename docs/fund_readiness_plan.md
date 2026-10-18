# Fund-readiness validation plan

## Decision standard

The project is **not approved for fund management or live execution** by this plan. Passing a test gate means only that the named engineering or research question has been addressed; it is not a promise of returns, regulatory approval, or investment suitability. Live trading remains disabled.

## Phase 0 — Reproducibility and safe test environment

- Run pytest only against isolated test databases; never use application `.env` or `vyuha.db` for destructive tests.
- Pin dependencies and capture code revision, configuration, data hashes, random seeds, and run parameters with every report.
- Mock external APIs in unit tests. No live LLM/provider call is permitted in a deterministic test run.
- Gate: repeat identical seeded runs and compare outputs; all tests pass without network access.

## Phase 1 — Accounting and execution invariants (blocking)

- Fix buy/sell/SIP cash ledger and holdings updates as a single atomic transaction.
- Reject sells without a position and quantities beyond available shares; enforce positive finite prices and quantities.
- Persist pending orders and make submissions/fills idempotent across restarts and retries.
- Add reconciliation checks: opening cash + flows + realized/unrealized P&L - costs equals ending equity; holdings equal fills; no negative cash/shares unless explicitly permitted.
- Exercise partial fills, rejects, gaps, duplicate events, concurrent calls, DB rollback, and process restart.
- Gate: invariant/property tests pass under randomized event sequences and concurrency tests; independent ledger reconciliation agrees exactly within currency rounding.

## Phase 2 — Synthetic behavioral stress (engineering only)

- Use seeded correlated synthetic OHLCV and a benchmark proxy. Exercise bull, bear, sideways, high-volatility and crash/gap conditions over many seeds.
- Assert known-entry, next-open fill, stop, gap-through-stop, costs, portfolio limits, and no-lookahead behavior. Record zero-trade runs as an explicit coverage failure, not a performance result.
- Run several hundred seeds and report trade coverage, loss distributions, drawdowns, exposures, costs, and operational invariant failures.
- Gate: every intended execution path is exercised and all accounting/timing invariants pass. Synthetic return distributions are not evidence of real-world alpha.

## Phase 3 — Historical research validation

- Validate data completeness, corporate-action adjustment, point-in-time universe membership, delistings, filing availability dates, benchmark TRI alignment, and stale/missing observations.
- Freeze strategy/configuration before evaluation. Use chronological development, validation, and untouched test periods; include walk-forward re-estimation only where specified in advance.
- Compare identical cash flows against appropriate passive benchmarks. Include realistic brokerage, taxes, slippage, market impact, liquidity constraints, and rejected/unfilled orders.
- Report CAGR/XIRR correctly, volatility, Sharpe/Sortino, max drawdown and duration, turnover, exposure, capacity, tail loss, and confidence intervals. Correct for selection/parameter search and report all tested variants.
- Gate: independent quant review confirms no material leakage or survivorship bias and that results remain acceptable under predeclared cost, parameter, universe, and period stresses. Thresholds must be set by the responsible investment committee before results are reviewed, not tuned afterward.

## Phase 4 — Paper operations and production controls

- Run the frozen strategy prospectively with persistent state, real market data, realistic order simulation, monitoring, alerting, recovery drills, and daily independent reconciliation.
- Add authenticated access, least privilege, secrets management, audit logs, kill switch, position/exposure limits, stale-data rejection, and fail-closed behavior.
- Gate: a predeclared extended paper period with zero unexplained ledger breaks, order-state loss, unauthorized actions, or control-limit violations; independent operational and security sign-off.

## Phase 5 — Governance and legal review

- Obtain advice on jurisdiction-specific licensing, fiduciary duties, custody, disclosures, recordkeeping, tax, and broker obligations.
- Define human approval, investment mandate, liquidity/redemption policy, valuation policy, conflicts policy, incident response, and independent risk oversight.
- Gate: written approval from qualified legal/compliance, operational-risk, and investment decision-makers. This cannot be replaced by code or backtest scores.

## Current synthetic runner

Run: `python scripts/run_synthetic_stress.py --seeds 100 --sessions 2520 --symbols 30`.

The generator supplies `INDEX_NIFTY500`, required by the strategy's relative-strength calculation. Per-run and summary CSVs are written under `docs/synthetic_stress/` by default. Treat generated return figures as descriptions of chosen assumptions only.
