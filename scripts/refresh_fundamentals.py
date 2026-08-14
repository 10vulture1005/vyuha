# scripts/refresh_fundamentals.py
"""Standalone cron entrypoint for the weekly fundamental refresh.

Scheduled via host cron: Sun 10:00 IST
    0 10 * * 0  python scripts/refresh_fundamentals.py

Invokes the Phase 2 Fundamental Agent to:
    1. Scrape all universe symbols from Screener.in
    2. Apply hard quantitative filters
    3. Compute conviction scores
    4. Upsert watchlist state
"""
import sys
from loguru import logger

from agents.fundamental_agent import FundamentalAgent

if __name__ == "__main__":
    try:
        agent = FundamentalAgent()
        active_list = agent.generate_watchlist_execution()
        logger.info(
            f"Weekly fundamental refresh successful. "
            f"{len(active_list)} ACTIVE symbols. Top 5: {active_list[:5]}"
        )
    except Exception as e:
        logger.exception(f"Fatal error during fundamental refresh: {e}")
        sys.exit(1)
