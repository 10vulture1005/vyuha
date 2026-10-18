# agents/sentiment_agent.py
"""Phase 3 — Sentiment Agent: Governance Veto & Risk Screening.

Implements the sentinel layer that prevents capital allocation into
governance traps, fraud risks, and regulatory landmines. Operates
exclusively on the ~15-40 ACTIVE watchlist survivors from Phase 2.

Pipeline:
    1. Load ACTIVE watchlist symbols (joined with Universe for company names)
    2. Fetch trailing 30-day headlines via the batched news providers
    3. Execute Two-Tier classification (regex -> LLM)
    4. VETO flagged symbols (mutate watchlist.status, write audit trail)
    5. Return surviving clean symbols for downstream Phase 4 (Technical Agent)
"""
from datetime import datetime, timezone
from typing import List

from loguru import logger

from db.session import get_session
from db.models import Watchlist, WatchlistStatus, SentimentFlag, Universe
from agents.tools.news_tools import (
    due_marketaux_slice,
    fetch_drishti_batch,
    fetch_marketaux_batch,
    classify_symbols_batch,
)


def apply_veto(
    session, symbol: str, reason: str, source_url: str = None
) -> None:
    """Mutates Watchlist status to VETOED and writes an immutable audit record.

    Args:
        session: Active SQLAlchemy session
        symbol: Stock ticker being vetoed
        reason: Human-readable veto justification
        source_url: Optional URL of the triggering news article
    """
    logger.warning(f"APPLYING VETO to {symbol}: {reason}")

    # 1. Insert audit trail record
    flag = SentimentFlag(
        symbol=symbol,
        checked_date=datetime.now(timezone.utc),
        red_flag=True,
        reason=reason,
        source_url=source_url,
    )
    session.add(flag)

    # 2. Mutate watchlist status
    w_item = session.get(Watchlist, symbol)
    if w_item:
        w_item.status = WatchlistStatus.VETOED.value
        w_item.last_updated = datetime.now(timezone.utc)


def run_sentiment_pass_execution() -> List[str]:
    """Iterates active watchlist items, executes 2-tier vetting, and returns surviving clean symbols.

    Returns:
        List of symbols that survived governance vetting (still ACTIVE).
    """
    logger.info("Starting Phase 3 Sentiment & Governance Veto Scan...")
    surviving_symbols: List[str] = []
    headlines_by_symbol: dict = {}

    with get_session() as session:
        # Strict funnel: only scan active watchlist candidates from Phase 2
        active_items = (
            session.query(Watchlist.symbol, Universe.company_name)
            .outerjoin(Universe, Watchlist.symbol == Universe.symbol)
            .filter(Watchlist.status == WatchlistStatus.ACTIVE.value)
            .all()
        )

        logger.info(
            f"Loaded {len(active_items)} ACTIVE candidates for governance vetting."
        )

        symbols = [s for s, _ in active_items]

        # Provider order:
        #   1. MarketAux — primary. Its free tier is 100 req/day at 3 articles
        #      each, so only an alternating slice of the watchlist is scanned
        #      per run and the slice is persisted (see due_marketaux_slice).
        #   2. Drishti — optional secondary; far more articles per request, so
        #      switch NEWS_PROVIDER=drishti once you have a paid plan.
        #   2. Drishti — optional secondary, for when you have a paid plan.
        #
        # There is no RSS fallback any more. A symbol the providers do not
        # cover gets an empty headline list and is recorded SCREENING_FAILED:
        # explicitly unscreened rather than silently treated as clean.
        slice_symbols = due_marketaux_slice(symbols)
        if slice_symbols:
            headlines_by_symbol.update(
                fetch_marketaux_batch(slice_symbols, lookback_days=30)
            )
        headlines_by_symbol.update(
            fetch_drishti_batch(symbols, lookback_days=30)
        )
        uncovered = [s for s in symbols if s not in headlines_by_symbol]
        if uncovered:
            logger.warning(
                f"No provider covers {len(uncovered)}/{len(symbols)} symbols "
                f"({', '.join(uncovered[:8])}{'...' if len(uncovered) > 8 else ''}). "
                f"They will be recorded SCREENING_FAILED, not clean."
            )
            for sym in uncovered:
                headlines_by_symbol[sym] = []

        # Classify every symbol in one pass: regex + FinBERT gate + chunked
        # LLM. Calling classify_red_flags per symbol issued one LLM request
        # per name, which was the pipeline's entire runtime cost.
        verdicts = classify_symbols_batch(list(headlines_by_symbol.items()))

        screened = 0
        unscreened = 0
        for symbol, _comp_name in active_items:
            verdict = verdicts.get(symbol)
            if verdict is None:
                surviving_symbols.append(symbol)
                unscreened += 1
                continue

            if verdict.is_red_flag and verdict.severity in ("HIGH", "MEDIUM"):
                apply_veto(session, symbol, verdict.reason, verdict.source_url)
                continue

            # Key off actual coverage, not the outcome string: "Tier 2
            # disabled" also returns SCREENING_FAILED, but those names WERE
            # screened by the regex/FinBERT tiers.
            if not headlines_by_symbol.get(symbol):
                unscreened += 1
            else:
                screened += 1
            # Log for audit completeness either way.
            clean_flag = SentimentFlag(
                symbol=symbol,
                checked_date=datetime.now(timezone.utc),
                red_flag=False,
                reason=verdict.reason,
                source_url=None,
            )
            session.add(clean_flag)
            surviving_symbols.append(symbol)

        logger.info(
            f"Sentiment scan complete. {len(surviving_symbols)}/{len(active_items)} "
            f"not vetoed — {screened} screened, {unscreened} UNSCREENED (no "
            f"provider coverage; not cleared on merit)."
        )
        return surviving_symbols
