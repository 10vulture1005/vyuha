# agents/vyuha_agent/__init__.py
"""VYUHA Agent — LLM-powered multi-agent research pipeline.

Vyuha Agent is vyuha's integrated wrapper around the
**[TradingAgents](https://arxiv.org/abs/2412.20138)** LangGraph
framework (Tauric Research, v0.4.0). The framework itself is
vendored unchanged at ``tradingagents/`` at the project root so
upstream updates can be merged mechanically.

This subpackage is the thin glue that:

* Configures the framework for Indian markets (Nifty 50 benchmark,
  RBI macro queries, yfinance-only vendors, NSE/BSE ticker
  suffixing) via ``config/tradingagents.yaml``.
* Runs the full LangGraph pipeline (4 analysts → bull/bear debate →
  trader → 3 risk debators → portfolio manager) for any Indian
  ticker via ``LLMPipelineAgent``.
* Translates the framework's 5-tier rating (Buy / Overweight /
  Hold / Underweight / Sell) into vyuha's BUY / HOLD_CASH / SELL
  vocabulary so the rest of the pipeline can use it.
* Persists every run to the ``tradingagents_decisions`` /
  ``tradingagents_outcomes`` DB tables for audit and reflection.

Usage
-----
    from agents.vyuha_agent import LLMPipelineAgent
    agent = LLMPipelineAgent()
    result = agent.evaluate_symbol("RELIANCE", "2026-01-15")
    if result.action == "BUY":
        ...

Public surface
--------------
LLMPipelineAgent       High-level runner (one ticker + one date).
VyuhaAgentResult       Typed view of a completed pipeline run.
signal_to_vyuha_action Translation between framework ratings and
                       vyuha's BUY/HOLD_CASH vocabulary.
normalise_indian_ticker Append Ticker.NS / pass-through .NS/.BO.
run_cross_check        Fan-out + DB persist across many symbols.
resolve_pending_outcomes
                       Backfill realised PnL + LLM reflection rows.
build_vyuha_agent_config
                       Three-layer config merge (vendor → YAML → .env).
"""
from .bridge import (
    LLMPipelineAgent,
    VyuhaAgentBridgeError,
    VyuhaAgentResult,
    signal_to_vyuha_action,
    normalise_indian_ticker,
    run_cross_check,
    resolve_pending_outcomes,
)
from .config_builder import build_vyuha_agent_config

__all__ = [
    "LLMPipelineAgent",
    "VyuhaAgentBridgeError",
    "VyuhaAgentResult",
    "signal_to_vyuha_action",
    "normalise_indian_ticker",
    "run_cross_check",
    "resolve_pending_outcomes",
    "build_vyuha_agent_config",
]