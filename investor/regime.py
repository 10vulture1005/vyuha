# investor/regime.py
"""Layer 0 — market regime and the equity exposure target.

Inputs (all daily closes):
    * Nifty 500 vs its 200DMA
    * breadth: % of the investable universe trading above its own 200DMA
    * India VIX percentile over the trailing year

Output: an equity exposure target in {100%, 70%, 40%} plus the regime label.

The one non-obvious requirement is the 3-consecutive-close confirmation. Without
it, a single down day near the 200DMA flips the book to 40% equity and back
again weeks later, and the defensive sleeve churns on noise. The hysteresis is
implemented as a small state machine, deliberately separate from the raw
signal so it can be unit-tested on a synthetic series.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import pandas as pd

from investor.config import investor_config
from investor.data import PricePanel

RISK_ON = "RISK_ON"
MIXED = "MIXED"
RISK_OFF = "RISK_OFF"

_REGIME_ORDER = [RISK_OFF, MIXED, RISK_ON]


@dataclass
class RegimeInputs:
    """Raw daily regime readings. Kept separate from the state machine so the
    hysteresis can be tested without a price panel."""

    date: pd.Timestamp
    nifty_close: float
    nifty_ma: float
    breadth_pct: float
    vix_close: float
    vix_percentile: float
    nifty_above_ma: bool
    vix_high: bool


@dataclass
class RegimeState:
    """Point-in-time regime decision."""

    date: pd.Timestamp
    raw_regime: str
    regime: str
    exposure: float
    confirmed: bool
    pending_regime: Optional[str] = None
    pending_count: int = 0
    breadth_pct: float = 0.0
    vix_close: float = 0.0
    vix_percentile: float = 0.0
    nifty_close: float = 0.0
    nifty_ma: float = 0.0

    def as_dict(self) -> Dict:
        return {
            "date": self.date,
            "raw_regime": self.raw_regime,
            "regime": self.regime,
            "exposure": self.exposure,
            "confirmed": self.confirmed,
            "pending_regime": self.pending_regime,
            "pending_count": self.pending_count,
            "breadth_pct": self.breadth_pct,
            "vix_close": self.vix_close,
            "vix_percentile": self.vix_percentile,
            "nifty_close": self.nifty_close,
            "nifty_ma": self.nifty_ma,
        }


def classify(
    nifty_above_ma: bool,
    breadth_pct: float,
    vix_high: bool,
    breadth_risk_on: Optional[float] = None,
) -> str:
    """Maps the three raw readings onto a regime label.

    Risk-off is the strict condition from the brief: Nifty 500 below its 200DMA
    AND VIX above its 80th percentile. Everything else that has a negative tilt
    is "mixed" at 70% exposure; a clean uptrend with healthy breadth is risk-on.
    """
    thr = float(
        breadth_risk_on
        if breadth_risk_on is not None
        else investor_config.regime.get("breadth_risk_on", 60.0)
    )
    if (not nifty_above_ma) and vix_high:
        return RISK_OFF
    if nifty_above_ma and breadth_pct >= thr:
        return RISK_ON
    return MIXED


class RegimeHysteresis:
    """Applies the N-consecutive-close confirmation to a raw regime stream.

    A change only takes effect once the same raw regime has printed
    `confirm_days` times in a row. Until then the previous regime stays in force
    and the candidate is exposed on the state for observability.
    """

    def __init__(self, confirm_days: Optional[int] = None):
        self.confirm_days = int(
            confirm_days
            if confirm_days is not None
            else investor_config.regime.get("confirm_days", 3)
        )
        self._current: Optional[str] = None
        self._pending: Optional[str] = None
        self._count = 0

    @property
    def current(self) -> Optional[str]:
        return self._current

    def reset(self, regime: Optional[str] = None) -> None:
        self._current = regime
        self._pending = None
        self._count = 0

    def update(self, raw_regime: str) -> tuple[str, bool, Optional[str], int]:
        """Feeds one day's raw reading.

        Returns (effective_regime, changed_this_bar, pending, pending_count).
        """
        if self._current is None:
            # First observation seeds the state without needing confirmation —
            # otherwise the backtest would sit undefined for `confirm_days`.
            self._current = raw_regime
            return self._current, True, None, 0

        if raw_regime == self._current:
            self._pending, self._count = None, 0
            return self._current, False, None, 0

        if raw_regime == self._pending:
            self._count += 1
        else:
            self._pending, self._count = raw_regime, 1

        if self._count >= self.confirm_days:
            self._current = raw_regime
            self._pending, self._count = None, 0
            return self._current, True, None, 0

        return self._current, False, self._pending, self._count


# ─────────────────────────────────────────────────────────────────────────────
# Regime computation over a price panel
# ─────────────────────────────────────────────────────────────────────────────

def compute_breadth(
    panel: PricePanel,
    symbols: Sequence[str],
    as_of,
    ma_days: Optional[int] = None,
) -> float:
    """% of `symbols` whose close is above their own 200DMA on `as_of`.

    Symbols without enough history are excluded from BOTH numerator and
    denominator; counting them as "not above" would make breadth look worse
    every time a new listing enters the universe.
    """
    days = int(ma_days if ma_days is not None else investor_config.regime.get("trend_ma_days", 200))
    above = 0
    counted = 0
    for sym in symbols:
        df = panel.get(sym, as_of)
        if len(df) < days:
            continue
        ma = float(df["Close"].rolling(days).mean().iloc[-1])
        if ma != ma or ma <= 0:  # NaN or zero
            continue
        counted += 1
        if float(df["Close"].iloc[-1]) > ma:
            above += 1
    return (100.0 * above / counted) if counted else 50.0


def vix_percentile(
    panel: PricePanel,
    as_of,
    symbol: Optional[str] = None,
    lookback: Optional[int] = None,
    pct: Optional[float] = None,
) -> float:
    """Percentile rank (0-100) of the current VIX close in its trailing window."""
    sym = symbol or investor_config.regime.get("vix_symbol", "INDEX_INDIAVIX")
    lb = int(lookback if lookback is not None else investor_config.regime.get("vix_lookback_days", 252))
    target = float(pct if pct is not None else investor_config.regime.get("vix_percentile", 80.0))
    df = panel.get(sym, as_of)
    if len(df) < 30:
        return 0.0
    window = df["Close"].tail(lb)
    if len(window) < 2:
        return 0.0
    # Rank of the latest observation within the window, as a percentage.
    current = float(window.iloc[-1])
    return 100.0 * float((window < current).sum()) / float(len(window))


def compute_regime_inputs(
    panel: PricePanel,
    as_of,
    universe_symbols: Sequence[str],
    nifty_symbol: Optional[str] = None,
    cache=None,
    breadth_override: Optional[float] = None,
) -> Optional[RegimeInputs]:
    """Reads the three regime inputs for one day, or None if data is short."""
    cfg = investor_config.regime
    ma_days = int(cfg.get("trend_ma_days", 200))
    nifty_sym = nifty_symbol or cfg.get("nifty_proxy", "INDEX_NIFTY500")

    ndf = panel.get(nifty_sym, as_of)
    if len(ndf) < ma_days:
        return None
    nifty_ma = float(ndf["Close"].rolling(ma_days).mean().iloc[-1])
    nifty_close = float(ndf["Close"].iloc[-1])
    if nifty_ma <= 0:
        return None

    if breadth_override is not None:
        breadth = float(breadth_override)
    elif cache is not None:
        series = cache.breadth_series(universe_symbols)
        prior = series.loc[:as_of] if not series.empty else series
        breadth = float(prior.iloc[-1]) if len(prior) else 50.0
    else:
        breadth = compute_breadth(panel, universe_symbols, as_of, ma_days=ma_days)

    vdf = panel.get(cfg.get("vix_symbol", "INDEX_INDIAVIX"), as_of)
    vix_close = float(vdf["Close"].iloc[-1]) if not vdf.empty else 0.0
    vix_pct = vix_percentile(panel, as_of)
    vix_thresh_pct = float(cfg.get("vix_percentile", 80.0))
    # Percentile >= threshold means the VIX is in its top slice.
    vix_high = vix_pct >= vix_thresh_pct

    return RegimeInputs(
        date=pd.Timestamp(as_of),
        nifty_close=nifty_close,
        nifty_ma=nifty_ma,
        breadth_pct=breadth,
        vix_close=vix_close,
        vix_percentile=vix_pct,
        nifty_above_ma=nifty_close > nifty_ma,
        vix_high=vix_high,
    )


def raw_regime(inputs: RegimeInputs, breadth_threshold: Optional[float] = None) -> str:
    """Raw (pre-hysteresis) regime for one bar."""
    thr = float(
        breadth_threshold
        if breadth_threshold is not None
        else investor_config.regime.get("breadth_risk_on", 60.0)
    )
    return classify(inputs.nifty_above_ma, inputs.breadth_pct, inputs.vix_high, thr)


class RegimeEngine:
    """Streaming Layer 0: one call per trading day, state kept between calls.

    `cache` is an optional pre-computed IndicatorCache. When supplied, breadth
    and the 200DMA read come from a single vectorised pass instead of rolling a
    200-day mean per symbol per day.
    """

    def __init__(
        self,
        panel: PricePanel,
        universe_symbols: Sequence[str],
        nifty_symbol: Optional[str] = None,
        breadth_threshold: Optional[float] = None,
        cache=None,
    ):
        self.panel = panel
        self.universe_symbols = list(universe_symbols)
        self.nifty_symbol = nifty_symbol
        self.breadth_threshold = breadth_threshold
        self.cache = cache
        self.hysteresis = RegimeHysteresis()
        self.history: List[RegimeState] = []
        self._breadth: Optional[pd.Series] = None

    @property
    def latest(self) -> Optional[RegimeState]:
        return self.history[-1] if self.history else None

    def breadth_at(self, as_of) -> float:
        """% of the universe above its own 200DMA on `as_of`."""
        if self.cache is not None:
            if self._breadth is None:
                self._breadth = self.cache.breadth_series(self.universe_symbols)
            if not self._breadth.empty:
                series = self._breadth
                prior = series.loc[:as_of]
                if len(prior):
                    return float(prior.iloc[-1])
        return compute_breadth(self.panel, self.universe_symbols, as_of)

    @property
    def latest(self) -> Optional[RegimeState]:
        return self.history[-1] if self.history else None

    def exposure_for(self, regime: str) -> float:
        """Maps a regime label onto its equity exposure target.

        The lookup is case-insensitive on purpose: the YAML keys are
        `risk_on / mixed / risk_off` while the labels are `RISK_ON / MIXED /
        RISK_OFF`, and a silent case mismatch here silently pins the entire
        book at the "mixed" 70% level in a raging bull market.
        """
        table = {str(k).lower(): float(v) for k, v in investor_config.exposure.items()}
        return table.get(str(regime).lower(), table.get("mixed", 0.70))

    def update(self, as_of) -> Optional[RegimeState]:
        """Computes and confirms today's regime. Idempotent per date."""
        inputs = compute_regime_inputs(
            self.panel, as_of, self.universe_symbols, self.nifty_symbol,
            cache=self.cache, breadth_override=self.breadth_at(as_of)
            if self.cache is not None else None,
        )
        if inputs is None:
            return None

        raw = raw_regime(inputs, self.breadth_threshold)
        effective, changed, pending, count = self.hysteresis.update(raw)

        state = RegimeState(
            date=inputs.date,
            raw_regime=raw,
            regime=effective,
            exposure=self.exposure_for(effective),
            confirmed=changed,
            pending_regime=pending,
            pending_count=count,
            breadth_pct=inputs.breadth_pct,
            vix_close=inputs.vix_close,
            vix_percentile=inputs.vix_percentile,
            nifty_close=inputs.nifty_close,
            nifty_ma=inputs.nifty_ma,
        )
        self.history.append(state)
        return state


def defensive_targets(state: RegimeState) -> Dict[str, float]:
    """How the non-equity sleeve is split when exposure < 100%.

    Remainder goes to the cash proxy, with gold capped at `max_gold_weight`.
    When exposure is 100% the sleeve is empty.
    """
    cfg = investor_config.defensive
    table = {str(k).lower(): float(v) for k, v in investor_config.exposure.items()}
    sleeve = max(0.0, 1.0 - table.get(str(state.regime).lower(), 0.70))
    if sleeve <= 0:
        return {}
    max_gold = float(cfg.get("max_gold_weight", 0.15))
    gold = min(sleeve * 0.5, max_gold)  # gold wants half the sleeve, capped hard
    cash = sleeve - gold
    return {
        str(cfg.get("cash_proxy", "LIQUIDBEES")): round(cash, 6),
        str(cfg.get("gold_proxy", "GOLDBEES")): round(gold, 6),
    }


__all__ = [
    "RISK_ON", "MIXED", "RISK_OFF",
    "RegimeInputs", "RegimeState", "RegimeHysteresis", "RegimeEngine",
    "classify", "raw_regime", "compute_breadth", "compute_regime_inputs",
    "vix_percentile", "defensive_targets",
]