# scripts/run_backtest.py
"""CLI entrypoint for executing multi-year VYUHA historical backtests.

Usage:
    python scripts/run_backtest.py --start 2016-01-01 --end 2024-12-31 --universe legacy-megacap
"""
import sys
import argparse
from loguru import logger
from backtest.engine import EventDrivenBacktester
from backtest.report import generate_tearsheet_report

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run VYUHA Historical Backtest")
    parser.add_argument(
        "--start", default="2016-01-01", help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end", default="2024-12-31", help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--universe",
        choices=["legacy-megacap"],
        default="legacy-megacap",
        help="Universe selection mode (point-in-time is currently blocked)",
    )
    args = parser.parse_args()

    # Universe mapping
    symbols = []
    if args.universe == "legacy-megacap":
        symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]

    try:
        logger.info(
            f"Launching VYUHA backtest: {args.start} to {args.end} | "
            f"Universe: {args.universe} | Symbols: {symbols}"
        )
        tester = EventDrivenBacktester(args.start, args.end, symbols)
        result = tester.run()
        report = generate_tearsheet_report(result)
        print(report)
    except Exception as e:
        logger.exception(f"Fatal error during backtest execution: {e}")
        sys.exit(1)
