# backtest_investor/__init__.py
"""Investor Mode backtesting package.

Chronological, event-driven replay with the Layer 0-3 ablation study,
benchmark comparison on matched cash flows, and robustness sweeps.
"""

from backtest_investor.engine import (
    InvestorBacktester, BacktestResult, Fill, ClosedTrade,
)

__all__ = ["InvestorBacktester", "BacktestResult", "Fill", "ClosedTrade"]