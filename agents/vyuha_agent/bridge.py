# agents/vyuha_agent/bridge.py
"""Bridge between the VYUHA pipeline and the vendored TradingAgents framework (Tauric Research).

The TradingAgents framework (vendored at ``tradingagents/``) is a LangGraph state machine that runs
four analysts, two researchers, a trader, three risk debators, and a
portfolio manager to reach a BUY / HOLD / SELL decision. We expose it
as a vyuha-friendly ``LLMPipelineAgent`` so the existing daily
pipeline can call it either as a cross-check (``Phase 7``) or as a
standalone CLI entrypoint (``scripts/run_vyuha_agent.py``).

Usage
-----
    from agents.vyuha_agent import LLMPipelineAgent

    agent = LLMPipelineAgent()
    decision = agent.evaluate_symbol("RELIANCE", "2026-01-15")
    if decision.is_buy:
        ...

The class is intentionally thin — every line of LangGraph topology,
prompt template, or data vendor lives in ``tradingagents/``. This file
just stitches the framework into vyuha: normalises tickers to
``.NS`` / ``.BO``, persists the run to ``tradingagents_decisions``,
maps the 5-tier rating back to vyuha's BUY/HOLD_CASH vocabulary, and
honours ``Settings.TRADINGAGENTS_REQUIRE_CONFIRMATION`` so the
rule-based pipeline remains the source of execution truth.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from config import settings
from db.models import (
    TradingAgentsDecision,
    TradingAgentsOutcome,
)
from db.session import get_session

from .config_builder import build_vyuha_agent_config

logger = logging.getLogger(__name__)


# ─── Errors ──────────────────────────────────────────────────────────────────


class VyuhaAgentBridgeError(RuntimeError):
    """Raised when the Vyuha Agent pipeline cannot run or its output
    can't be mapped back to vyuha's vocabulary."""


# ─── Translation helpers ─────────────────────────────────────────────────────


# 5-tier framework rating (Buy/Overweight/Hold/Underweight/Sell) → vyuha action. The LLM emits one of the
# canonical ratings (Buy / Overweight / Hold / Underweight / Sell) plus
# a REVIEW sentinel when no rating is parseable. We collapse to vyuha's
# 3-action vocabulary so the rest of the pipeline keeps its existing
# BUY / HOLD_CASH / SELL semantics.
_RATING_TO_ACTION: dict[str, str] = {
    "Buy":         "BUY",
    "Overweight":  "BUY",
    "Hold":        "HOLD",
    "Underweight": "SELL",
    "Sell":        "SELL",
    "REVIEW":      "HOLD",
}


def normalise_indian_ticker(symbol: str) -> str:
    """Append a Yahoo Finance suffix if the symbol is bare.

    The framework's data vendors query yfinance, which expects
    ``RELIANCE.NS`` / ``TATASTEEL.BO`` for Indian tickers. Vyuha's
    universe sometimes carries the bare symbol (e.g. ``RELIANCE``) so
    we add the NSE suffix as the default fallback.

    BSE-only tickers already carry ``.BO``; existing ``.NS`` tickers
    pass through untouched; everything else (US, etc.) is left as-is.
    """
    if not symbol:
        raise VyuhaAgentBridgeError("Ticker symbol is empty")
    upper = symbol.strip().upper()
    if "." in upper:
        return upper
    return f"{upper}.NS"


def signal_to_vyuha_action(signal: str) -> str:
    """Map a framework rating/signal to vyuha's BUY/HOLD/SELL vocabulary."""
    if signal is None:
        return "HOLD"
    cleaned = signal.strip()
    if not cleaned:
        return "HOLD"
    return _RATING_TO_ACTION.get(cleaned, "HOLD")


# ─── Decision dataclass ──────────────────────────────────────────────────────


@dataclass
class VyuhaAgentResult:
    """Typed view of a completed Vyuha Agent pipeline run."""

    ticker: str
    trade_date: date
    action: str                           # BUY / HOLD / SELL (vyuha vocabulary)
    raw_signal: str                       # Framework 5-tier rating (Buy/Overweight/Hold/Underweight/Sell) or REVIEW
    final_decision_text: str = ""         # Portfolio Manager's prose
    trader_plan: str = ""
    market_report: str = ""
    sentiment_report: str = ""
    news_report: str = ""
    fundamentals_report: str = ""
    investment_plan: str = ""
    debate_history: dict[str, Any] = field(default_factory=dict)
    risk_history: dict[str, Any] = field(default_factory=dict)
    confirmed_by_rules: bool = False      # Set later by run_cross_check
    acted_on: bool = False
    duration_ms: int = 0
    error: str | None = None
    raw_state: dict[str, Any] = field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        return self.action == "BUY"

    @property
    def is_sell(self) -> bool:
        return self.action == "SELL"

    @property
    def is_hold(self) -> bool:
        return self.action == "HOLD"

    @property
    def is_error(self) -> bool:
        return self.error is not None


# ─── Pipeline runner ─────────────────────────────────────────────────────────


class LLMPipelineAgent:
    """High-level wrapper that runs the framework's LangGraph graph for one symbol.

    The class is cheap to instantiate — the expensive parts (LLM
    construction, graph compilation) are cached on the
    ``TradingAgentsGraph`` instance for the life of the agent object.
    Call ``evaluate_symbol`` once per ticker; reuse the agent across
    multiple tickers in the same pipeline cycle to amortise the
    one-time LLM/graph setup cost.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        # Lazy import: ``tradingagents`` (the framework package) pulls in langgraph/langchain,
        # which can take several seconds to import. Doing it lazily lets
        # the rest of vyuha (rule-based agents, dashboard, CLI) keep
        # working when the framework's LLM deps are absent.
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        self._config = config or build_vyuha_agent_config()
        self._selected_analysts = tuple(self._config.pop("_vyuha_selected_analysts", ()))

        # ``debug=False`` keeps stdout quiet — vyuha uses loguru and
        # doesn't need the LangGraph trace pretty-printer. Pass
        # debug=True from the standalone CLI when humans are watching.
        logger.info(
            "Initialising Vyuha Agent framework graph: provider=%s deep=%s quick=%s",
            self._config["llm_provider"],
            self._config["deep_think_llm"],
            self._config["quick_think_llm"],
        )

        self._graph = TradingAgentsGraph(
            selected_analysts=self._selected_analysts,
            debug=False,
            config=self._config,
        )

    def evaluate_symbol(self, symbol: str, trade_date: date | str | None = None) -> VyuhaAgentResult:
        """Run the full pipeline for ``symbol`` on ``trade_date``.

        ``trade_date`` defaults to today. Strings are accepted for
        convenience (``"2026-01-15"``) but converted to ``date``.
        """
        if trade_date is None:
            trade_date = date.today()
        if isinstance(trade_date, str):
            trade_date = date.fromisoformat(trade_date)

        normalised = normalise_indian_ticker(symbol)
        logger.info("LLMPipelineAgent: running graph for %s on %s", normalised, trade_date)

        start = time.perf_counter()
        try:
            final_state, signal = self._graph.propagate(normalised, trade_date.isoformat())
            duration_ms = int((time.perf_counter() - start) * 1000)
        except Exception as exc:
            logger.exception("Vyuha Agent pipeline failed for %s", normalised)
            return VyuhaAgentResult(
                ticker=normalised,
                trade_date=trade_date,
                action="HOLD",
                raw_signal="REVIEW",
                error=str(exc),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        result = self._extract_result(normalised, trade_date, final_state, signal)
        result.duration_ms = duration_ms
        result.raw_state = final_state or {}

        self._persist(result)
        return result

    @staticmethod
    def _extract_result(
        ticker: str,
        trade_date: date,
        final_state: dict[str, Any],
        signal: str,
    ) -> VyuhaAgentResult:
        """Translate the raw graph state into a typed ``VyuhaAgentResult``."""
        debate = final_state.get("investment_debate_state", {}) or {}
        risk = final_state.get("risk_debate_state", {}) or {}

        return VyuhaAgentResult(
            ticker=ticker,
            trade_date=trade_date,
            action=signal_to_vyuha_action(signal),
            raw_signal=signal or "REVIEW",
            final_decision_text=final_state.get("final_trade_decision", "") or "",
            trader_plan=final_state.get("trader_investment_plan", "") or "",
            market_report=final_state.get("market_report", "") or "",
            sentiment_report=final_state.get("sentiment_report", "") or "",
            news_report=final_state.get("news_report", "") or "",
            fundamentals_report=final_state.get("fundamentals_report", "") or "",
            investment_plan=final_state.get("investment_plan", "") or "",
            debate_history={
                "bull_history": debate.get("bull_history", ""),
                "bear_history": debate.get("bear_history", ""),
                "judge_decision": debate.get("judge_decision", ""),
            },
            risk_history={
                "aggressive_history": risk.get("aggressive_history", ""),
                "conservative_history": risk.get("conservative_history", ""),
                "neutral_history": risk.get("neutral_history", ""),
                "judge_decision": risk.get("judge_decision", ""),
            },
        )

    @staticmethod
    def _persist(result: VyuhaAgentResult) -> None:
        """Write a completed run to ``tradingagents_decisions``.

        Errors are logged but never raised — persistence is best-effort.
        A failed DB write shouldn't take down the rule-based pipeline.
        """
        try:
            with get_session() as session:
                session.add(
                    TradingAgentsDecision(
                        ticker=result.ticker,
                        trade_date=result.trade_date,
                        trade_decision=result.action,
                        final_signal=result.raw_signal,
                        confirmed_by_rules=result.confirmed_by_rules,
                        acted_on=result.acted_on,
                        llm_provider=settings.TRADINGAGENTS_LLM_PROVIDER,
                        deep_model=settings.TRADINGAGENTS_DEEP_THINK_LLM,
                        quick_model=settings.TRADINGAGENTS_QUICK_THINK_LLM,
                        market_report=result.market_report,
                        sentiment_report=result.sentiment_report,
                        news_report=result.news_report,
                        fundamentals_report=result.fundamentals_report,
                        investment_plan=result.investment_plan,
                        trader_plan=result.trader_plan,
                        debate_history=result.debate_history,
                        risk_history=result.risk_history,
                        raw_state=result.raw_state,
                        error_msg=result.error,
                        duration_ms=result.duration_ms,
                        created_at=datetime.now(timezone.utc),
                    )
                )
        except Exception:
            logger.exception("Failed to persist Vyuha Agent run for %s", result.ticker)


# ─── Cross-check helpers ─────────────────────────────────────────────────────


def run_cross_check(
    symbols: Iterable[str],
    rule_based_actions: dict[str, str] | None = None,
    trade_date: date | None = None,
) -> list[VyuhaAgentResult]:
    """Run TradingAgents for each symbol and flag agreement with the rule-based pipeline.

    Args:
        symbols: tickers to evaluate. Bare symbols get ``.NS`` appended.
        rule_based_actions: optional ``{symbol: "BUY"|"HOLD"|"SELL"}`` map
            from the rule-based pipeline. When provided, each result's
            ``confirmed_by_rules`` flag is set if actions agree.
        trade_date: passed through to ``evaluate_symbol``.

    Returns:
        List of ``VyuhaAgentResult`` objects, in the same order as
        ``symbols``. Caller decides whether to act on the LLM signal —
        when ``Settings.TRADINGAGENTS_REQUIRE_CONFIRMATION`` is True
        and rule-based disagrees, the result is treated as advisory
        only.

    Notes:
        The function never raises — each symbol is wrapped in try/except
        so a single failed LLM call doesn't break the cross-check loop.
    """
    if trade_date is None:
        trade_date = date.today()

    rule_based_actions = rule_based_actions or {}
    results: list[VyuhaAgentResult] = []

    # Instantiate the agent once and reuse across all symbols. The
    # TradingAgentsGraph's internal state isn't ticker-specific (it's
    # passed into the graph at propagation time).
    try:
        agent = LLMPipelineAgent()
    except Exception as exc:
        logger.exception("Could not initialise Vyuha Agent")
        # Return one error result per symbol so the caller's loop is uniform.
        return [
            VyuhaAgentResult(
                ticker=normalise_indian_ticker(s),
                trade_date=trade_date,
                action="HOLD",
                raw_signal="REVIEW",
                error=f"Agent init failed: {exc}",
            )
            for s in symbols
        ]

    for symbol in symbols:
        try:
            result = agent.evaluate_symbol(symbol, trade_date)
        except Exception as exc:
            logger.exception("Vyuha Agent cross-check failed for %s", symbol)
            result = VyuhaAgentResult(
                ticker=normalise_indian_ticker(symbol),
                trade_date=trade_date,
                action="HOLD",
                raw_signal="REVIEW",
                error=f"Run failed: {exc}",
            )

        rule_action = rule_based_actions.get(symbol)
        if rule_action is not None:
            result.confirmed_by_rules = (rule_action == result.action)
            if not result.confirmed_by_rules and settings.TRADINGAGENTS_REQUIRE_CONFIRMATION:
                logger.info(
                    "LLM/rule disagreement for %s: llm=%s rule=%s — holding (confirmation required)",
                    symbol, result.action, rule_action,
                )

        results.append(result)

    return results


def resolve_pending_outcomes(holding_days: int | None = None) -> int:
    """Fill in ``tradingagents_outcomes`` for past runs whose holding window has elapsed.

    Mirrors Tauric Research's upstream ``Reflector`` flow but writes
    outcomes to vyuha's DB instead of (or in addition to) the
    on-disk markdown memory log. Returns the number of outcomes
    recorded.
    """
    from tradingagents.dataflows.symbol_utils import normalize_symbol
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.agents.utils.memory import TradingMemoryLog

    if holding_days is None:
        holding_days = settings.TRADINGAGENTS_HOLDING_DAYS

    cfg = build_vyuha_agent_config()
    memory = TradingMemoryLog(cfg)
    graph = TradingAgentsGraph(config=cfg)

    written = 0
    try:
        # ``get_pending_entries`` is per-ticker upstream; iterate over
        # all tickers we've ever recorded to catch up.
        with get_session() as session:
            tickers = [
                row[0]
                for row in session.query(TradingAgentsDecision.ticker)
                .distinct()
                .all()
            ]

        from datetime import timedelta
        from tradingagents.dataflows.utils import safe_ticker_component

        for ticker in tickers:
            pending = [e for e in memory.get_pending_entries() if e["ticker"] == ticker]
            if not pending:
                continue

            benchmark = graph._resolve_benchmark(ticker)
            updates = []
            for entry in pending:
                start = datetime.strptime(entry["date"], "%Y-%m-%d")
                end = start + timedelta(days=holding_days + 7)
                try:
                    import yfinance as yf
                    stock = yf.Ticker(normalize_symbol(ticker)).history(
                        start=entry["date"], end=end.strftime("%Y-%m-%d"),
                    )
                    bench = yf.Ticker(benchmark).history(
                        start=entry["date"], end=end.strftime("%Y-%m-%d"),
                    )
                    if len(stock) <= holding_days or len(bench) <= holding_days:
                        continue
                    raw = float(
                        (stock["Close"].iloc[holding_days] - stock["Close"].iloc[0])
                        / stock["Close"].iloc[0]
                    )
                    bench_ret = float(
                        (bench["Close"].iloc[holding_days] - bench["Close"].iloc[0])
                        / bench["Close"].iloc[0]
                    )
                    alpha = raw - bench_ret
                    resolution_date = stock.index[holding_days].strftime("%Y-%m-%d")
                    reflection = graph.reflector.reflect_on_final_decision(
                        final_decision=entry.get("decision", ""),
                        raw_return=raw,
                        alpha_return=alpha,
                        benchmark_name=benchmark,
                    )
                    updates.append((entry, raw, alpha, resolution_date, reflection))
                except Exception:
                    logger.exception("Could not resolve outcome for %s on %s", ticker, entry["date"])

            # Persist outcomes + push reflections upstream in one batch.
            with get_session() as session:
                for entry, raw, alpha, res_date, reflection in updates:
                    session.add(
                        TradingAgentsOutcome(
                            ticker=ticker,
                            trade_date=date.fromisoformat(entry["date"]),
                            resolution_date=date.fromisoformat(res_date),
                            holding_days=holding_days,
                            raw_return=Decimal(str(round(raw, 6))),
                            alpha_return=Decimal(str(round(alpha, 6))),
                            benchmark=safe_ticker_component(benchmark),
                            reflection=reflection,
                        )
                    )
                    written += 1

            if updates:
                memory.batch_update_with_outcomes([
                    {
                        "ticker": ticker,
                        "trade_date": entry["date"],
                        "raw_return": raw,
                        "alpha_return": alpha,
                        "holding_days": holding_days,
                        "reflection": reflection,
                        "resolution_date": res_date,
                    }
                    for entry, raw, alpha, res_date, reflection in updates
                ])
    finally:
        # Avoid leaving any per-ticker checkpoint context alive between runs.
        graph.end_checkpoint()

    return written