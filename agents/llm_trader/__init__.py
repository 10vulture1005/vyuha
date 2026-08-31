# agents/llm_trader/__init__.py
"""VYUHA ↔ TradingAgents integration layer.

Exposes the TradingAgents LangGraph pipeline as a callable agent that
fits vyuha's existing multi-agent orchestration:

    from agents.llm_trader import LLMPipelineAgent, run_cross_check
    agent = LLMPipelineAgent()
    decision = agent.evaluate_symbol("RELIANCE", "2026-01-15")

The actual TradingAgents package lives in ``tradingagents/`` at the
project root (vendored unchanged from upstream so updates can be
merged mechanically). This subpackage is the thin glue: it imports the
framework, applies vyuha's Indian-market defaults, runs the graph,
translates the resulting BUY/HOLD/SELL signal into a vyuha action,
and persists the full state to the ``tradingagents_decisions`` table.

Public surface
--------------
LLMPipelineAgent       High-level runner (one ticker + one date).
TradingAgentsDecision  Typed view of a completed pipeline run.
signal_to_vyuha_action Translation between TradingAgents ratings and
                        vyuha's BUY/HOLD_CASH vocabulary.
run_cross_check        Loops over a list of symbols, runs the pipeline,
                        persists results, returns confirmation flags
                        for the rule-based pipeline.

The framework remains the single source of truth for the graph
topology (analysts → researchers → trader → risk debators →
portfolio manager); this layer only configures it for Indian markets
and stitches it into vyuha's daily pipeline.
"""
from .bridge import (
    LLMPipelineAgent,
    TradingAgentsBridgeError,
    TradingAgentsResult,
    signal_to_vyuha_action,
    normalise_indian_ticker,
    run_cross_check,
    resolve_pending_outcomes,
)
from .config_builder import build_tradingagents_config

__all__ = [
    "LLMPipelineAgent",
    "TradingAgentsBridgeError",
    "TradingAgentsResult",
    "signal_to_vyuha_action",
    "normalise_indian_ticker",
    "run_cross_check",
    "resolve_pending_outcomes",
    "build_tradingagents_config",
]