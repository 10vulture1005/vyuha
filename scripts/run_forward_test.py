# scripts/run_forward_test.py
"""CLI entrypoint for executing VYUHA forward-testing paper cycles.

Modes:
    --mode once:       Run a single daily paper cycle and exit.
    --mode continuous:  Run continuously, executing one cycle per day.

Usage:
    python scripts/run_forward_test.py --mode once
    python scripts/run_forward_test.py --mode continuous
"""
import sys
import argparse
import time
from datetime import datetime
from loguru import logger
from core.observability import setup_logging


def main():
    parser = argparse.ArgumentParser(
        description="Run VYUHA Forward Test Paper Engine"
    )
    parser.add_argument(
        "--mode",
        choices=["once", "continuous"],
        default="once",
        help="Execution mode: 'once' for single cycle, 'continuous' for daily loop",
    )
    args = parser.parse_args()

    setup_logging()

    from core.paper_engine import ForwardTestEngine

    try:
        engine = ForwardTestEngine()
    except RuntimeError as e:
        logger.critical(str(e))
        sys.exit(1)

    if args.mode == "once":
        logger.info("Running single forward-test paper cycle...")
        result = engine.run_daily_paper_cycle()
        logger.info(f"Cycle result: {result}")
    elif args.mode == "continuous":
        logger.info("Starting continuous forward-test loop (1 cycle/day)...")
        while True:
            try:
                result = engine.run_daily_paper_cycle()
                logger.info(f"Daily cycle complete: {result}")
            except Exception as e:
                logger.exception(f"Error during daily forward-test cycle: {e}")

            # Sleep until next market day (~18 hours)
            next_run = 18 * 3600  # 18 hours
            logger.info(
                f"Next cycle at {datetime.now().strftime('%H:%M')} + 18h. Sleeping..."
            )
            time.sleep(next_run)


if __name__ == "__main__":
    main()
