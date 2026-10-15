# VYUHA Production Readiness and Security Audit

**Audit date:** 2026-10-15  
**Verdict:** **Not production-ready**  
**Scope:** Current working tree, including uncommitted Investor Mode changes.

## Executive summary

The audit found security exposures, reproducible accounting corruption, broken deployment configuration, and safety controls that are not enforced.

**Do not expose the API publicly or rely on this system for real-money execution until the release blockers are resolved.** The execution paths reviewed primarily mutate paper-trading/database state. This report does not claim that these defects currently send unauthorized orders to a broker.

The most serious findings are:

- Unauthenticated API access to financial data and pipeline execution.
- Test fixtures that can delete the configured application's database tables.
- Concurrent transactions that spend the same cash twice.
- Sell operations that credit cash for unowned shares or create negative holdings.
- Investor Mode fills that do not update the cash ledger.
- Pending orders that disappear when the daily CLI process exits.
- Governance and confirmation controls that can be bypassed or fail open.
- Docker and database migrations that do not support a clean production deployment.

## Methodology and limitations

The review covered application configuration, API routes, execution/accounting code, strategy orchestration, deployment files, migrations, and tests.

Verification used temporary databases and blocked external networking. Source files and the working application database were not modified during the audit.

### Test results

| Result | Count |
|---|---:|
| Passed | 219 |
| Failed | 23 |
| Collection errors | 1 |

These results apply to the non-API tests, run with collection errors allowed so the remaining tests could execute.

- API tests were excluded: FastAPI was not installed in the current virtual environment, and one API test triggers the real pipeline without mocking it.
- Several failures reflect stale tests or configuration assumptions, rather than independent application defects. One routing test assumes an API-key value exists, which fails in the isolated no-key environment.
- Docker/PostgreSQL deployment was not executed. Deployment findings are based on configuration inspection and an isolated SQLite migration test.
- A dependency CVE scan, external penetration test, secret-history scan, and production infrastructure assessment were not performed.
- File references describe the audited working tree and may shift after edits.

---

## 1. Security and destructive-operation findings

### SEC-01 — Unauthenticated API exposes financial state and pipeline execution

**Severity:** High; release blocker if network-accessible  
**Evidence:** Source inspection  
**Locations:**

- `api/main.py:11–30`
- `api/routers/pipeline.py:8–25`
- `api/routers/portfolio.py:14–49`
- `docker/docker-compose.yml:36`

There is no authentication or authorization protecting portfolio holdings, trades, ledger, watchlist, or `POST /pipeline/run`.

The pipeline endpoint invokes execution workflows. The supplied Compose configuration publishes port 8000 on all host interfaces. There is no application-level rate limit or execution lock.

**Impact:** Anyone who can reach the service can read financial state and repeatedly trigger database-changing workflows and potentially paid LLM calls.

Wildcard CORS broadens browser access; CORS is not authentication.

**Remediation:**

- Require authentication and explicit permissions for execution.
- Restrict network exposure and allowed browser origins.
- Apply request limits and execution quotas.
- Enforce one active execution per account/strategy.

### SEC-02 — Running tests can delete application database tables

**Severity:** Critical operational risk  
**Evidence:** Source inspection; tests were deliberately isolated to avoid destruction  
**Locations:**

- `tests/test_capital_allocator.py:6–14`
- `tests/test_observability.py:14–22`

Both fixtures import the application `sync_engine`, create tables, and call the following during teardown:

```python
Base.metadata.drop_all(bind=sync_engine)
```

The engine uses `DATABASE_URL` from application settings, including `.env`.

**Impact:** Running ordinary `pytest` in a configured environment can delete application tables. These tests also write into that database before teardown.

**Remediation:**

- Use dedicated temporary test engines and dependency overrides.
- Refuse destructive test operations against non-test databases.
- Ensure tests cannot load production database credentials.

**Warning:** Do not run the existing suite against the normal application `.env`.

### SEC-03 — Docker build context includes secrets and application data

**Severity:** High  
**Evidence:** Source and directory inspection  
**Locations:**

- `docker/Dockerfile:40`
- `docker/docker-compose.yml:10,51`
- Missing root `.dockerignore`

The Dockerfile uses:

```dockerfile
COPY . .
```

There is no root `.dockerignore`. The working directory contains `.env`, `.git`, `.venv`, logs, and `vyuha.db`.

**Impact:** Once the build blockers are fixed, building from this directory would copy those files into the image. `.gitignore` does not protect Docker build contexts.

The Compose configuration also contains fixed database and pgAdmin passwords.

**Remediation:**

- Add `.dockerignore` and copy only required application files.
- Inject secrets at runtime rather than embedding them in images.
- Replace fixed passwords with externally supplied secrets.
- Rotate credentials if any previously distributed images contained them.

### SEC-04 — Dashboard HTML injection through insecure upstream market data

**Severity:** High, conditional on malicious or intercepted upstream data  
**Evidence:** Source inspection; no browser exploitation performed  
**Locations:**

- `data/live_market_api.py:11`
- `api/routers/dashboard.py:122–147`

Market data comes from a hard-coded HTTP endpoint rather than HTTPS. The dashboard interpolates upstream values such as volume, market-cap values, and units directly into HTML without escaping. Database-sourced symbols are also unescaped.

**Impact:** A compromised provider or network interceptor could return markup or script in those fields, which would be rendered in the dashboard.

**Remediation:**

- Use a trusted HTTPS provider.
- Validate upstream response types and ranges.
- Render through an autoescaping template engine.
- Add a restrictive Content Security Policy as defense in depth.

---

## 2. Confirmed financial-state corruption

### FIN-01 — Concurrent transactions can spend the same cash twice

**Severity:** Critical accounting defect  
**Evidence:** Reproduced with two concurrent transactions against a temporary database  
**Location:** `core/capital_allocator.py:112–139`

The allocator reads the latest running balance, calculates a new balance, and inserts a ledger entry without locking an account balance or checking a version.

### Reproduction result

| Item | Result |
|---|---:|
| Starting cash | ₹1,000 |
| First purchase | ₹800 |
| Second purchase | ₹800 |
| Reported final cash | **₹200** |
| Actual ledger sum | **−₹600** |
| Both holdings created | **Yes** |

Transactions alone do not prevent this lost-update problem.

**Remediation:**

- Serialize execution per account.
- Use a locked or versioned account balance with atomic updates.
- Add idempotency keys.
- Reconcile ledger movements, holdings, and balances.

### FIN-02 — Sell operations allow nonexistent holdings and overselling

**Severity:** High  
**Evidence:** Reproduced against a temporary database  
**Location:** `core/capital_allocator.py:185–277`

`execute_sell()` credits proceeds without first requiring a valid open holding. `execute_partial_sell()` does not enforce:

```text
0 < sell_qty <= holding.qty
```

### Reproduction results

- Selling five shares of an unowned symbol credited **₹484.48**.
- Partially selling ten shares from a five-share holding produced **qty = −5**.

Additionally, `execute_sell()` closes the entire holding regardless of whether the supplied quantity equals the holding quantity.

**Remediation:** Validate ownership, status, quantity, finite positive price, and available quantity before any mutation. Add matching database constraints and regression tests.

### FIN-03 — Investor Mode fills do not update the cash ledger

**Severity:** Critical accounting defect  
**Evidence:** Reproduced against a temporary database  
**Location:** `core/investor_engine.py:406–498`

`paper_fill()` adjusts a local `cash` variable and persists holdings/trades, but writes no corresponding `CapitalLedger` entries.

### Reproduction result

A successful Investor Mode buy created a holding/trade while:

- Cash balance changed by **₹0**.
- Ledger row count changed by **0**.

**Impact:** Purchased assets appear without deducting cash. Subsequent runs can reuse already-spent money and overstate equity.

**Remediation:** Persist cash, holdings, and trade records atomically through one shared execution/accounting service.

### FIN-04 — Adding to an Investor Mode holding crashes

**Severity:** High  
**Evidence:** Reproduced on a second purchase  
**Location:** `core/investor_engine.py:511–518`

This expression mixes `Decimal` and `float`:

```python
total_cost = holding.avg_buy_price * holding.qty + gross
```

Observed error:

```text
TypeError: unsupported operand type(s) for +: 'decimal.Decimal' and 'float'
```

The in-memory engine is updated before the failing database operation. A failure can therefore leave memory inconsistent with the rolled-back database.

**Remediation:** Use consistent monetary types. Update in-memory execution state only after successful persistence, or explicitly restore it on rollback.

### FIN-05 — SIP accounting loses initial capital and permits duplicate credits

**Severity:** High  
**Evidence:** Reproduced against a fresh temporary database  
**Location:** `core/capital_allocator.py:92–116`

Two functions disagree about an empty ledger:

- `get_current_cash()` returns `INITIAL_CAPITAL`.
- `credit_monthly_sip_execution()` starts from zero.

### Reproduction result

```text
Cash before first SIP:       ₹100,000
Cash after ₹1,000 SIP:       ₹1,000
After same-month retry:      ₹2,000
```

The swing monthly credit has no unique month/account constraint. Retrying a first-of-month job credits it again.

**Remediation:** Persist an explicit opening-capital transaction. Make monthly contributions idempotent with a unique account/month reference.

---

## 3. Strategy execution and safety controls

### STR-01 — Investor Mode loses pending orders on CLI restart

**Severity:** High  
**Evidence:** Source inspection  
**Locations:**

- `core/investor_engine.py:163,595–598`
- `scripts/invest_cli.py:47–56`

Pending next-open orders exist only in:

```python
self._pending_orders
```

The daily CLI constructs a new engine every invocation.

**Impact:** The ordinary daily process starts with no pending orders, creates new ones, and exits without persisting them. Yesterday's orders are not available to fill tomorrow.

Other strategy state, including rebalance timing, tranche flags, and news-review state, is also incompletely restored.

There is an additional ordering problem: the live orchestrator evaluates today's decisions before filling yesterday's orders. The backtest fills first, so live and backtest behavior diverge.

**Remediation:**

- Persist orders and strategy state.
- Restore pending orders and relevant state on startup.
- Fill due orders before evaluating new decisions.
- Enforce unique execution IDs and test process-restart recovery.

### STR-02 — A VETOED stock can still be bought

**Severity:** High  
**Evidence:** Reproduced against a temporary database  
**Location:** `core/capital_allocator.py:293–360`

The allocator selects today's technical signals but does not recheck watchlist status.

### Reproduction result

A `VETOED` watchlist entry with a same-day technical signal resulted in:

```text
BUY 100 shares of VETOED
```

Similarly, a later technical scan returning early because of a blocked regime does not invalidate previously stored same-day signals.

**Remediation:** Revalidate governance eligibility, regime, data freshness, and signal validity at the execution boundary. Old signals must not bypass current risk decisions.

### STR-03 — Required LLM confirmation is not an execution gate

**Severity:** High  
**Evidence:** Source inspection  
**Locations:**

- `crew/flow_runner.py:166–176`
- `agents/tools/ledger_tools.py:9–13`
- `agents/llm_crosscheck_agent.py:120–152`

The crew can execute the capital allocator during `crew.kickoff()`. The cross-check runs afterward, and its confirmed-symbol list is only logged.

**Impact:** `TRADINGAGENTS_REQUIRE_CONFIRMATION=True` does not reliably stop an unconfirmed purchase. The `acted_on` field is populated from confirmation membership rather than verified execution.

**Remediation:** Run confirmation before allocation and enforce its result inside the execution service, not through an optional tool or prompt. Record actual execution separately from confirmation.

### STR-04 — Governance screening treats classifier failure as clean

**Severity:** High  
**Evidence:** Reproduced with a forced model failure and no xAI key  
**Location:** `agents/tools/news_tools.py:140–170,222–232`

If FinBERT fails and xAI is unavailable, the function returns:

```text
is_red_flag=False
severity=NONE
reason="Passed Tier 1; Tier 2 disabled."
```

This was reproduced using a headline describing a fraud investigation and auditor resignation while forcing the classifier to fail.

**Impact:** An unavailable screening system is misrepresented as successful clean screening.

**Remediation:** Introduce an explicit `UNKNOWN` or `SCREENING_FAILED` outcome. Block new exposure or require manual review when required screening is unavailable.

---

## 4. Deployment blockers

### DEP-01 — Supplied Docker image cannot build/run as configured

**Severity:** High; deployment blocker  
**Evidence:** Configuration inspection; Docker build not executed  
**Locations:**

- `docker/Dockerfile:1,32–40`
- `pyproject.toml:9,48–91`
- `docker/entrypoint.sh:18`

Independent problems include:

1. Docker uses Python 3.11, but the project requires Python ≥3.12.
2. `pip install .` does not request the `agents`, `api`, or `postgres` extras.
3. The next build step invokes `scrapling install`, despite Scrapling being optional.
4. Startup requires Uvicorn, PostgreSQL drivers, and `uvloop`; the install configuration does not explicitly provide them all.
5. API import pulls in the CrewAI pipeline, so API dependencies alone are insufficient.

**Remediation:** Build with a supported Python version, install an explicit deployment dependency set from a lockfile, and add a clean-image startup smoke test.

### DEP-02 — Fresh migrations do not create the application schema

**Severity:** High; deployment blocker  
**Evidence:** Confirmed with a fresh SQLite migration; PostgreSQL failure inferred from missing referenced tables  
**Location:** `db/migrations/versions/0001_investor_mode.py:24–25,151–159`

The only migration creates Investor Mode tables and assumes legacy tables already exist.

### Reproduction result

On a fresh SQLite database, `alembic upgrade head` created only:

- `alembic_version`
- `investor_fundamental_snapshots`
- `investor_news_events`
- `investor_rebalance_log`
- `investor_targets`

It did not create `universe`, `capital_ledger`, `portfolio_holdings`, or `trade_log`.

On fresh PostgreSQL, foreign keys referencing the missing `universe` table would also prevent successful migration.

**Remediation:** Add a complete baseline migration and test upgrades against both empty and existing databases.

---

## 5. Additional correctness and reliability findings

These findings are based on source inspection unless otherwise noted.

| ID | Severity | Finding | Location | Recommended action |
|---|---|---|---|---|
| ADD-01 | High | Performance API returns hard-coded zero CAGR, Sharpe, Sortino, and drawdown. Portfolio value uses purchase cost instead of current market value. | `core/metrics.py:22–32` | Compute metrics from validated equity history and current valuations; expose unavailable metrics explicitly rather than returning misleading zeros. |
| ADD-02 | High | Swing and Investor Mode share holdings/cash without strategy ownership. The daily swing pipeline has no `STRATEGY_MODE` guard, so running both can apply swing exits to investor holdings. | `scripts/run_daily_pipeline.py`, `agents/risk_exit_agent.py:20`, `core/investor_engine.py:201` | Partition ownership by account/strategy and enforce mode guards at entry points and execution boundaries. |
| ADD-03 | Medium | Realized P&L sums all historical buy friction for a symbol, including prior holding cycles. Partial exits can allocate the same friction repeatedly. | `core/capital_allocator.py:82–88,220,263–265` | Track fees per position/lot and allocate each fee exactly once across exits. |
| ADD-04 | Medium | Default synchronous pipeline execution runs blocking work directly inside an async route. Background execution is in-process and not durable across worker termination. | `api/routers/pipeline.py:9–25` | Use a durable job queue, execution status records, and bounded workers. |
| ADD-05 | Medium | Drawdown protection returns `False` on exceptions and relies on potentially stale valuation snapshots. Missing or broken risk data can allow buying rather than halt it. | `core/capital_allocator.py:53–79` | Validate valuation freshness, alert on errors, and fail closed for new exposure when required risk data is unavailable. |
| ADD-06 | Medium | Ledger/trade pagination accepts unrestricted limits. Very large limits, and SQLite's negative-limit behavior, can produce unbounded responses. | `api/routers/portfolio.py:21–32` | Validate limits with positive lower bounds and fixed upper bounds; use pagination. |

## 6. Test-suite maintenance findings

The observed failures include:

- Importing removed `TIER1_REGEX`.
- Calling `execute_buy()` without the required `init_stop` argument.
- Creating `Universe(name=...)` although that field does not exist.
- Omitting required `raw_json` in fundamental fixtures.
- Assertions based on outdated configuration and technical-pattern behavior.
- An LLM routing test that assumes an API-key value exists.

The test suite currently cannot serve as a reliable release gate. Fixing stale tests must not mean simply weakening assertions: each test should be reconciled with the intended production contract.

---

## 7. Recommended remediation order

### Phase 1 — Contain exposure

- [ ] Restrict API access until authentication and authorization are implemented.
- [ ] Replace destructive test fixtures with isolated databases.
- [ ] Exclude secrets and application state from Docker build contexts.
- [ ] Replace fixed deployment credentials.
- [ ] Remove unsafe HTML interpolation and insecure upstream transport.

### Phase 2 — Repair accounting

- [ ] Serialize account mutations and enforce atomic balance updates.
- [ ] Validate buy/sell quantities, ownership, prices, and position state.
- [ ] Persist opening capital explicitly.
- [ ] Make SIP credits and execution retries idempotent.
- [ ] Persist Investor Mode cash movements atomically with holdings/trades.
- [ ] Standardize monetary types and rollback behavior.
- [ ] Correct fee allocation and realized P&L.

### Phase 3 — Repair execution lifecycle and safety

- [ ] Persist pending orders and restart-sensitive strategy state.
- [ ] Align live and backtest event ordering.
- [ ] Separate swing/investor account or strategy ownership.
- [ ] Revalidate governance, regime, signal freshness, and confirmation at execution.
- [ ] Represent unavailable screening/risk data explicitly and fail closed for new exposure.
- [ ] Move pipeline execution to a durable, bounded job system.

### Phase 4 — Repair deployment and verification

- [ ] Use a supported Python version and locked deployment dependencies.
- [ ] Add complete baseline migrations.
- [ ] Test a clean PostgreSQL deployment and application startup.
- [ ] Make tests pass without production configuration or external credentials.
- [ ] Add concurrent execution, retry, restart, and accounting-invariant tests.
- [ ] Replace placeholder performance metrics.

### Phase 5 — Complete production assurance

- [ ] Run dependency CVE and secret-history scans.
- [ ] Perform authenticated API security testing.
- [ ] Test backup and restore procedures.
- [ ] Add reconciliation alerts, stale-data alerts, job-failure alerts, and execution audit trails.
- [ ] Validate staging behavior before considering real-money deployment.

## Suggested release acceptance criteria

Before production approval, demonstrate that:

1. Unauthorized clients cannot read financial data or trigger execution.
2. Tests cannot access or destroy application databases.
3. Concurrent and retried requests cannot double-spend or duplicate executions/contributions.
4. Holdings, trades, cash movements, and reported balances reconcile after every execution.
5. Invalid quantities and unowned sells fail without any state mutation.
6. Pending orders and strategy state survive process restarts.
7. Required safety gates are enforced immediately before execution.
8. Fresh database migrations and clean container startup succeed reproducibly.
9. Reported metrics reflect real valuations or explicitly indicate unavailable data.
10. Automated tests pass in isolated CI, including the regression cases documented here.

## Final assessment

The most serious problems are not cosmetic. The current code can create financially impossible state, bypass intended safety checks, and delete application tables through its test suite. These are release blockers, not optional hardening tasks.
