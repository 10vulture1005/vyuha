"""Quarterly statement refresh, then verified point-in-time watchlist scoring.

Run April/July/October/January 7 (seven days after quarter close).
Missing or incomplete statements never produce a synthetic snapshot.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from scripts.fetch_screener_snapshots import main as fetch_snapshots
from agents.fundamental_agent import FundamentalAgent


def main():
    result = fetch_snapshots(['--refresh'])
    if result:
        logger.error('Statement refresh incomplete; skipping watchlist update')
        return result
    active = FundamentalAgent().generate_watchlist_execution()
    logger.info(f'Quarterly refresh: {len(active)} qualifying symbols')
    return 0


if __name__ == '__main__':
    sys.exit(main())
