# CLI product plan and implementation notes

Reference: [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading). We borrow product themes, not its trading claims or code: clear provider status, explicit research/backtest workflows, auditability, bounded automation, and safety-first defaults.

## Implemented in this slice

- `vyuha providers` reports primary/fallback route readiness without displaying key values.
- Added first-class Mistral and NVIDIA API-key settings and custom OpenAI-compatible endpoint credentials/base URL.
- Provider endpoint resolution now honors `OPENAI_COMPATIBLE_BASE_URL`.
- Tests cover provider-key resolution, custom endpoints, and secret-free CLI output.

## Candidate product features

1. **CLI trust and usability:** `doctor` preflight, structured errors, `--json`, consistent exit codes, shell completion, and offline-safe help.
2. **Credential management:** keychain-backed secret storage, `provider test` health checks, key rotation/revoke, endpoint validation, and secret redaction from logs.
3. **Research workspace:** durable run IDs, immutable config/data manifests, citation/source provenance, and reproducible exportable reports.
4. **Strategy evaluation:** walk-forward and benchmark comparison, transaction-cost/slippage stress, sensitivity/multiple-testing disclosure, and paper-trading reports.
5. **Risk operations:** stale-data checks, exposure/sector/concentration limits, drawdown alerts, kill switch, and human approval before any execution integration.
6. **Reliability:** durable jobs, restart-safe state, idempotency keys, backup/restore commands, and explicit health checks.
7. **Security:** API authentication/authorization, local-only default binding, rate limits, audit log, hardened image/deployment settings, and isolated tests.

## Not implemented / important limitations

- `vyuha setup` currently configures the TradingAgents-specific provider settings; it does not yet set every `LLM_PROVIDER` route. For the shared LLM route, configure `LLM_PROVIDER`, the relevant key, `LLM_MODEL`, and, for custom endpoints, `OPENAI_COMPATIBLE_BASE_URL` in `.env` or the process environment.
- `vyuha providers` checks local configuration only; it does not make a provider request and cannot prove a key works, model exists, or quota is available.
- No live-order feature is added. Broker execution must remain disabled until accounting, persistence, authorization, and independent risk controls are signed off.
- This product roadmap and provider wiring do not establish investment performance or fund-management suitability.
