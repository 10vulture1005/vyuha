# agents/tools/tradingagents_tools.py
"""CrewAI tool wrappers around the TradingAgents LangGraph pipeline.

These tools let vyuha's existing ``crew_definition`` Crew (which
already orchestrates a CrewAI Fundamental/Sentiment/Technical/Risk/PM
chain) call into the TradingAgents LangGraph state machine as
additional tools. Two integration patterns are supported:

1. ``run_tradingagents_pipeline`` — invokes the full pipeline for a
   single ticker and returns a JSON-friendly action + rationale.
   Useful when the PM wants an independent LLM view on a specific
   watchlist candidate.

2. ``run_tradingagents_cross_check`` — fans out across the current
   ACTIVE watchlist, runs the pipeline for each symbol, and returns a
   summary of how many symbols the LLM agrees / disagrees with the
   rule-based signals.

Both tools are best-effort — they never raise out of the CrewAI tool
boundary because tool failures would otherwise abort the whole crew
kickoff. Errors are returned as ``{"error": "...", "action": "HOLD"}``
so the surrounding agent sees a consistent JSON shape.

Note: importing this module requires the ``crewai`` extras (and the
underlying langgraph deps in ``pyproject.toml``'s ``[tradingagents]``
group). It is not imported eagerly from ``agents/tools/__init__.py``
to keep the rule-based-only path free of LLM deps.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from crewai.tools import tool

logger = logging.getLogger(__name__)


def _to_json(obj: Any) -> str:
    """Serialise any object to a JSON string the CrewAI agent can read."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    if hasattr(obj, "__dict__"):
        # Dataclass-like object — dump the public attributes.
        obj = {
            k: v for k, v in obj.__dict__.items()
            if not k.startswith("_")
        }
    try:
        return json.dumps(obj, default=str, indent=2)
    except Exception:
        return json.dumps({"error": "serialise_failed", "raw": str(obj)}, default=str)


def _err(symbol: str, msg: str) -> str:
    """Build a JSON error envelope that the agent can reason about."""
    return json.dumps(
        {
            "symbol": symbol,
            "action": "HOLD",
            "error": msg,
            "rationale": f"TradingAgents pipeline unavailable: {msg}",
        },
        indent=2,
    )


@tool("Run TradingAgents Pipeline")
def run_tradingagents_pipeline(symbol: str, trade_date: str = "") -> str:
    """Invoke the full TradingAgents LangGraph multi-agent pipeline (4 analysts + bull/bear researchers + trader + 3 risk debators + portfolio manager) for one Indian ticker.

    Args:
        symbol: NSE/BSE ticker symbol (bare ``RELIANCE`` or suffixed
            ``RELIANCE.NS``). The tool appends ``.NS`` automatically.
        trade_date: ISO date ``YYYY-MM-DD`` (defaults to today when blank).

    Returns:
        JSON object with keys ``action`` (BUY / HOLD / SELL),
        ``raw_signal`` (TradingAgents 5-tier rating),
        ``rationale`` (Portfolio Manager prose), and
        ``confirmed_by_rules`` (set later by the cross-check tool).
        Errors come back as ``{"action": "HOLD", "error": "..."}``.
    """
    try:
        from agents.llm_trader import LLMPipelineAgent, normalise_indian_ticker
    except Exception as exc:
        return _err(symbol, f"bridge import failed: {exc}")

    try:
        agent = LLMPipelineAgent()
        result = agent.evaluate_symbol(symbol, trade_date or None)
    except Exception as exc:
        logger.exception("TradingAgents pipeline failed for %s", symbol)
        return _err(symbol, f"pipeline failed: {exc}")

    payload = {
        "symbol": result.ticker,
        "trade_date": result.trade_date.isoformat(),
        "action": result.action,
        "raw_signal": result.raw_signal,
        "rationale": (result.final_decision_text or "")[:500],
        "trader_plan_excerpt": (result.trader_plan or "")[:300],
        "duration_ms": result.duration_ms,
        "confirmed_by_rules": False,
        "error": result.error,
    }
    return _to_json(payload)


@tool("Run TradingAgents Cross-Check")
def run_tradingagents_cross_check(symbols_json: str) -> str:
    """Run the TradingAgents pipeline for every symbol in a JSON array and compare with the rule-based pipeline.

    Args:
        symbols_json: JSON string of the form ``["RELIANCE","TCS","INFY"]``.
            Each symbol is evaluated independently; the tool returns a
            summary plus the per-symbol results.

    Returns:
        JSON object with ``summary`` (counts of agreement /
        disagreement) and ``results`` (per-symbol action + raw signal).
        Designed to be read by the CrewAI Portfolio Manager agent as
        an advisory input.
    """
    try:
        from agents.llm_trader import run_cross_check
        from db.session import get_session
        from db.models import Watchlist, WatchlistStatus, TechnicalSignal
        from datetime import date
        from decimal import Decimal
    except Exception as exc:
        return _err("[]", f"cross-check imports failed: {exc}")

    try:
        symbols = json.loads(symbols_json)
        if not isinstance(symbols, list):
            raise ValueError("symbols_json must be a JSON array of strings")
    except Exception as exc:
        return _err("[]", f"invalid symbols_json: {exc}")

    # Pull today's rule-based actions so the cross-check can flag
    # agreement. We map symbols to "BUY" if a fresh TechnicalSignal
    # exists for them today, otherwise "HOLD".
    rule_based_actions: dict[str, str] = {}
    try:
        today = date.today()
        with get_session() as session:
            active = {
                row[0] for row in
                session.query(Watchlist.symbol)
                .filter(Watchlist.status == WatchlistStatus.ACTIVE.value)
                .all()
            }
            buy_signals = {
                row[0] for row in
                session.query(TechnicalSignal.symbol)
                .filter(TechnicalSignal.signal_date == today)
                .distinct()
                .all()
            }
        for sym in active:
            rule_based_actions[sym] = "BUY" if sym in buy_signals else "HOLD"
    except Exception:
        logger.exception("Failed to build rule-based actions for cross-check")

    try:
        results = run_cross_check(symbols, rule_based_actions)
    except Exception as exc:
        logger.exception("TradingAgents cross-check failed")
        return _err("[]", f"cross-check failed: {exc}")

    payload_results = []
    agree_count = 0
    disagree_count = 0
    error_count = 0
    for r in results:
        if r.is_error:
            error_count += 1
        elif r.confirmed_by_rules:
            agree_count += 1
        else:
            disagree_count += 1
        payload_results.append({
            "symbol": r.ticker,
            "action": r.action,
            "raw_signal": r.raw_signal,
            "confirmed_by_rules": r.confirmed_by_rules,
            "error": r.error,
        })

    return json.dumps(
        {
            "summary": {
                "total": len(results),
                "agreement": agree_count,
                "disagreement": disagree_count,
                "errors": error_count,
            },
            "results": payload_results,
        },
        indent=2,
    )


@tool("Resolve TradingAgents Outcomes")
def resolve_tradingagents_outcomes(holding_days: str = "") -> str:
    """Backfill realised PnL for past TradingAgents runs whose holding window has elapsed.

    Args:
        holding_days: optional positive integer. When blank, uses the
            value from ``Settings.TRADINGAGENTS_HOLDING_DAYS``.

    Returns:
        JSON object with ``written`` (count of new outcome rows).
    """
    try:
        from agents.llm_trader import resolve_pending_outcomes
    except Exception as exc:
        return _err("*", f"import failed: {exc}")

    try:
        n = resolve_pending_outcomes(int(holding_days)) if holding_days else resolve_pending_outcomes()
    except Exception as exc:
        logger.exception("TradingAgents outcome resolution failed")
        return _err("*", f"resolution failed: {exc}")

    return json.dumps({"written": n}, indent=2)