# VYUHA Audit Remediation Implementation Plan

Source audit: `AUDIT.md` (2026-10-15, verdict: not production-ready).

## Ground rules
- Work on feature branch; never test against `vyuha.db` or real `DATABASE_URL`.
- Force isolated temporary test DB in CI/local tests.
- Preserve paper-trading-only posture; no live broker order path.
- Land each phase behind passing tests.

## Phases
(See chat plan for full detail; summary follows.)

Phase 0: Harness/safety guardrails.
Phase 1: Exposure containment (SEC).
Phase 2: Accounting repair (FIN).
Phase 3: Execution lifecycle/safety (STR/ADD).
Phase 4: Deployment/verification (DEP/ADD).
Phase 5: Production assurance.

## Phase 0
1. Add test DB isolation in `conftest.py`.
2. Guard destructive teardown against non-test DBs.
3. Add `.dockerignore`.
4. Migration smoke-test fixture.

## Phase 1
- SEC-01 API auth/permissions/rate limits/execution lock/CORS.
- SEC-02 temp DB fixtures.
- SEC-03 Docker hygiene/secrets.
- SEC-04 HTTPS + escaping + CSP.

## Phase 2
- FIN-01 serialized atomic balance updates + idempotency.
- FIN-02 sell validity/oversell prevention.
- FIN-03 investor cash ledger writes.
- FIN-04 Decimal consistency and rollback safety.
- FIN-05 opening capital + idempotent SIP.
- ADD-03 fee allocation.

## Phase 3
- STR-01 persistent pending orders/state + ordering fix.
- STR-02 governance revalidation at execution.
- STR-03 confirmation before allocation.
- STR-04 screening unknown/fail-closed.
- ADD-02 strategy ownership.
- ADD-04 durable job system.
- ADD-05 fail-closed risk data.

## Phase 4
- DEP-01 supported Python + locked deps + smoke.
- DEP-02 baseline migrations.
- ADD-01 real metrics.
- ADD-06 pagination bounds.

## Phase 5
- Security/API tests, CVE/secret scans, backups, staging validation.

## Exit criteria
AUDIT.md release acceptance criteria 1-10 pass with isolated automated tests.
" 