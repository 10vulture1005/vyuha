# agents/llm_crosscheck_agent.py
"""Phase 7 — Vyuha Agent LLM Cross-Check.

Runs the Vyuha Agent LangGraph pipeline for each ACTIVE watchlist
symbol that has a fresh technical signal today, then compares the
LLM's BUY/HOLD/SELL verdict with the rule-based pipeline.

When ``Settings.TRADINGAGENTS_REQUIRE_CONFIRMATION`` is True (the
default), the LLM is treated as advisory only — a symbol only gets
traded when both pipelines agree on BUY. This keeps the rule-based
strategy's backtested statistics intact while still surfacing LLM
disagreements in the daily Telegram digest.

Disabled by default (``Settings.TRADINGAGENTS_ENABLED = False``). The
agent's ``run_cross_check_execution`` returns an empty list and logs
when disabled, so it's safe to call from the daily pipeline
unconditionally.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List

from config import settings
from db.models import (
    TechnicalSignal,
    TradingAgentsDecision,
    Watchlist,
    WatchlistStatus,
)
from db.session import get_session

logger = logging.getLogger(__name__)


def _today_symbols_with_signals(today: date | None = None) -> List[str]:
    """Return ACTIVE watchlist symbols that have a fresh signal today."""
    if today is None:
        today = date.today()
    with get_session() as session:
        rows = (
            session.query(TechnicalSignal.symbol)
            .filter(TechnicalSignal.signal_date == today)
            .distinct()
            .all()
        )
        signal_symbols = {row[0] for row in rows}

        rows = (
            session.query(Watchlist.symbol)
            .filter(Watchlist.status == WatchlistStatus.ACTIVE.value)
            .all()
        )
        active = {row[0] for row in rows}

    overlap = sorted(active & signal_symbols)
    logger.info(
        "Phase 7 LLM cross-check candidates: %d (active=%d, signals_today=%d)",
        len(overlap), len(active), len(signal_symbols),
    )
    return overlap


def _rule_based_actions(symbols: List[str], today: date | None = None) -> dict[str, str]:
    """Build the rule-based BUY/HOLD action map for the cross-check."""
    if today is None:
        today = date.today()
    actions: dict[str, str] = {}
    with get_session() as session:
        rows = (
            session.query(TechnicalSignal.symbol)
            .filter(TechnicalSignal.signal_date == today)
            .distinct()
            .all()
        )
        buy_signals = {row[0] for row in rows}
    for sym in symbols:
        actions[sym] = "BUY" if sym in buy_signals else "HOLD"
    return actions


def run_cross_check_execution(today: date | None = None) -> List[str]:
    """Run the Vyuha Agent LLM cross-check for today's setup candidates.

    Returns the list of symbols where the LLM and the rule-based
    pipeline agree on BUY. The caller (typically the daily pipeline)
    can use this list to whitelist rule-based BUY candidates — symbols
    where the LLM disagrees are dropped from execution.

    Disabled behaviour (when ``TRADINGAGENTS_ENABLED = False``) returns
    the input set unchanged so the rule-based pipeline runs as if the
    cross-check didn't happen.
    """
    if not settings.TRADINGAGENTS_ENABLED:
        logger.info(
            "Phase 7 disabled (Settings.TRADINGAGENTS_ENABLED=False). "
            "Returning rule-based candidates unchanged.",
        )
        return _today_symbols_with_signals(today)

    symbols = _today_symbols_with_signals(today)
    if not symbols:
        logger.info("Phase 7: no fresh setups to cross-check today.")
        return []

    # Lazy import — keeps the rule-based path free of LLM deps when
    # TRADINGAGENTS_ENABLED is False.
    try:
        from agents.vyuha_agent import run_cross_check
    except Exception as exc:
        logger.exception("Could not import Vyuha Agent bridge; falling back to rule-based.")
        return symbols

    rule_based = _rule_based_actions(symbols, today)
    results = run_cross_check(symbols, rule_based, today)

    confirmed: list[str] = []
    if settings.TRADINGAGENTS_REQUIRE_CONFIRMATION:
        for r in results:
            if r.is_error:
                logger.warning("LLM error for %s — treating as unconfirmed.", r.ticker)
                continue
            if r.confirmed_by_rules and r.action == "BUY":
                confirmed.append(r.ticker)
        logger.info(
            "Phase 7 confirmation: %d/%d symbols confirmed BUY.",
            len(confirmed), len(results),
        )
    else:
        # Without confirmation, the LLM is purely informational.
        confirmed = symbols
        logger.info(
            "Phase 7 advisory: %d/%d BUY signals surfaced; not gating rule-based pipeline.",
            sum(1 for r in results if r.is_buy), len(results),
        )

    # Persist a small "summary" run so the daily Telegram digest has
    # something to quote — one row per symbol already exists in
    # tradingagents_decisions, so this is just bookkeeping.
    try:
        with get_session() as session:
            for r in results:
                session.add(
                    TradingAgentsDecision(
                        ticker=r.ticker,
                        trade_date=r.trade_date,
                        trade_decision=r.action,
                        final_signal=r.raw_signal,
                        confirmed_by_rules=r.confirmed_by_rules,
                        acted_on=(r.ticker in confirmed),
                        llm_provider=settings.TRADINGAGENTS_LLM_PROVIDER,
                        deep_model=settings.TRADINGAGENTS_DEEP_THINK_LLM,
                        quick_model=settings.TRADINGAGENTS_QUICK_THINK_LLM,
                        duration_ms=r.duration_ms,
                        error_msg=r.error,
                        created_at=datetime.now(timezone.utc),
                    )
                )
    except Exception:
        logger.exception("Failed to record Phase 7 summary")

    return confirmed