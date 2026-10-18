# investor/__init__.py
"""Investor Mode — long-horizon portfolio strategy (STRATEGY_MODE=investor).

This package holds the *only* implementation of the Investor Mode rules. Both
the live daily engine (`core/investor_engine.py`) and the backtest replay
(`backtest_investor/engine.py`) import from here, because the repo's history
of behaviour drift between `backtest/engine.py` and `core/paper_engine.py`
came precisely from having two copies of one rule.

Layers:
    L0 regime.py        exposure target + defensive sleeve
    L1 fundamentals.py  what to own (hard filters + sector-neutral score)
    L2 technical.py     when to buy and sell
    L3 news.py          event-driven overlay

Everything here is pure: pandas in, pandas/dataclasses out, no DB, no network.
That is what makes the same code path replayable and testable.
"""

from investor.config import investor_config, InvestorConfig

__all__ = ["investor_config", "InvestorConfig"]