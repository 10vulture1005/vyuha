"""Synthetic market generators for strategy stress testing (not return forecasts).

Scenarios deliberately include distinct regimes and correlated assets. Synthetic
results test implementation behavior under controlled conditions; they cannot
establish real-world alpha or substitute for point-in-time historical data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from investor.data import PricePanel


@dataclass(frozen=True)
class SyntheticScenario:
    name: str
    annual_drift: float
    annual_volatility: float
    crash_probability: float = 0.0
    crash_daily_return: float = -0.08
    correlation: float = 0.45


SCENARIOS = (
    SyntheticScenario("bull", 0.16, 0.20),
    SyntheticScenario("bear", -0.12, 0.30),
    SyntheticScenario("sideways", 0.01, 0.18),
    SyntheticScenario("high_vol", 0.08, 0.48),
    SyntheticScenario("crash_risk", 0.10, 0.25, 0.015, -0.10),
)


class MemoryPricePanel(PricePanel):
    """PricePanel backed by generated frames; no filesystem/network access."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        super().__init__(ohlc_dir=".")
        self._frames = frames

    def available_symbols(self) -> list[str]:
        return sorted(self._frames)


def generate_market(
    symbols: Sequence[str], start: str = "2010-01-01", sessions: int = 2520,
    scenario: SyntheticScenario = SCENARIOS[0], seed: int = 7,
) -> MemoryPricePanel:
    """Generate reproducible correlated OHLCV paths with gap/crash events."""
    if not symbols or sessions < 2:
        raise ValueError("symbols must be non-empty and sessions must be >= 2")
    if scenario.annual_volatility < 0 or not -1 <= scenario.correlation <= 1:
        raise ValueError("invalid volatility or correlation")
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=sessions)
    n = len(symbols)
    common = rng.standard_normal(sessions)
    idio = rng.standard_normal((sessions, n))
    shocks = (np.sqrt(scenario.correlation) * common[:, None]
              + np.sqrt(1.0 - scenario.correlation) * idio)
    vol = scenario.annual_volatility / np.sqrt(252)
    drift = scenario.annual_drift / 252 - 0.5 * vol * vol
    returns = drift + vol * shocks
    if scenario.crash_probability:
        crash_days = rng.random(sessions) < scenario.crash_probability
        returns[crash_days, :] += scenario.crash_daily_return
    returns = np.clip(returns, -0.85, 1.0)
    frames = {}
    def make_frame(log_returns, volume):
        close = 100.0 * np.exp(np.cumsum(log_returns))
        overnight = rng.normal(0, vol * 0.25, sessions)
        open_ = close * np.exp(overnight)
        high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.025, sessions))
        low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.025, sessions))
        return pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": close,
             "Volume": volume}, index=dates
        )

    for j, symbol in enumerate(symbols):
        volume = rng.integers(100_000, 5_000_000, sessions)
        frames[symbol] = make_frame(returns[:, j], volume)
    # Investor technical entries require relative strength versus the configured
    # Nifty proxy. Include it as market context, but not in the tradable universe.
    index_returns = drift + vol * common
    if scenario.crash_probability:
        index_returns[crash_days] += scenario.crash_daily_return
    frames["INDEX_NIFTY500"] = make_frame(
        index_returns, rng.integers(10_000_000, 100_000_000, sessions)
    )
    return MemoryPricePanel(frames)
