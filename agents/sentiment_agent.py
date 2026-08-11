# agents/sentiment_agent.py
"""Phase 3 — Sentiment Agent: Governance Veto & Risk Screening.

Implements the sentinel layer that prevents capital allocation into
governance traps, fraud risks, and regulatory landmines. Operates
exclusively on the ~15-40 ACTIVE watchlist survivors from Phase 2.

Pipeline:
    1. Load ACTIVE watchlist symbols (joined with Universe for company names)
    2. Fetch trailing 30-day headlines via Google News RSS
    3. Execute Two-Tier classification (regex -> LLM)
    4. VETO flagged symbols (mutate watchlist.status, write audit trail)
    5. Return surviving clean symbols for downstream Phase 4 (Technical Agent)
"""
from datetime import datetime, timezone
from typing import List

from loguru import logger

from db.session import get_session
from db.models import Watchlist, WatchlistStatus, SentimentFlag, Universe
from agents.tools.news_tools import fetch_recent_headlines, classify_red_flags


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
    surviving_symbols = []

    with get_session() as session:
        # Strict funnel: only scan active watchlist candidates from Phase 2
        active_items = (
            session.query(Watchlist.symbol, Universe.name)
            .join(Universe, Watchlist.symbol == Universe.symbol)
            .filter(Watchlist.status == WatchlistStatus.ACTIVE.value)
            .all()
        )

        logger.info(
            f"Loaded {len(active_items)} ACTIVE candidates for governance vetting."
        )

        for symbol, comp_name in active_items:
            logger.debug(f"Vetting governance for {symbol} ({comp_name})...")
            headlines = fetch_recent_headlines(
                symbol, comp_name, lookback_days=30
            )
            verdict = classify_red_flags(symbol, headlines)

            if verdict.is_red_flag and verdict.severity in ("HIGH", "MEDIUM"):
                apply_veto(session, symbol, verdict.reason, verdict.source_url)
            else:
                # Log clean check for audit completeness
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
            f"survived governance vetting."
        )
        return surviving_symbols
