# investor/technical.py
"""Layer 2 — when to buy and sell (daily timeframe).

Entry logic is deliberately *not* an OR of everything that looks bullish. Two
disjoint setups are allowed, each in two tranches:

    Tranche 1 (50%)  breakout: close > 55-day high AND volume >= 1.2x 20d avg
                 OR  pullback: price at the 50DMA with RSI(14) turning up
                     through 40
    Tranche 2 (50%)  ten trading days later, still above the 50DMA

Exits are Chandelier (highest close since entry - 3.5 x ATR(22), ratcheting
and never lowering) or three consecutive closes below the 200DMA.

Every function takes a `PricePanel` already sliced to `as_of`, so nothing here
can see day t+1 while deciding about day t.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from investor.config import investor_config


# ─────────────────────────────────────────────────────────────────────────────
# Indicators (vectorised over a full frame, then read at `as_of`)
# ─────────────────────────────────────────────────────────────────────────────

def sma(df: pd.DataFrame, days: int) -> pd.Series:
    return df["Close"].rolling(days).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift()
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 22) -> pd.Series:
    """Wilder-smoothed ATR.

    Uses an EMA with alpha = 1/period (Wilder's original smoothing) rather than
    a simple rolling mean; the SMA version of ATR systematically under-reads
    volatility after a shock, which makes trailing stops sit too tight.
    """
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's RSI on closes."""
    delta = df["Close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(100.0).where(avg_loss.ne(0.0) | avg_gain.ne(0.0), 50.0)


def trailing_high(df: pd.DataFrame, days: int) -> pd.Series:
    return df["Close"].rolling(days).max()


def volume_ratio(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Current volume vs the average of the PRIOR `window` days.

    The current bar is excluded from its own average. Including it makes a
    1.2x threshold harder to clear on genuine spikes, which quietly suppressed
    a large share of breakout entries in earlier versions of this engine.
    """
    prior_avg = df["Volume"].shift(1).rolling(window).mean()
    return df["Volume"] / prior_avg.replace(0.0, np.nan)


@dataclass
class TechSnapshot:
    """Every Layer 2 reading for one symbol on one day."""

    symbol: str
    date: pd.Timestamp
    close: float
    sma_fast: Optional[float] = None
    sma_slow: Optional[float] = None
    sma_exit: Optional[float] = None
    atr: Optional[float] = None
    rsi: Optional[float] = None
    rsi_prev1: Optional[float] = None
    rsi_prev2: Optional[float] = None
    rsi_prev3: Optional[float] = None
    breakout_high: Optional[float] = None
    vol_ratio: Optional[float] = None
    mom_12_1: Optional[float] = None
    rel_strength: Optional[float] = None
    closes_below_sma: int = 0
    ready: bool = True

    @classmethod
    def from_row(cls, symbol: str, row: pd.Series) -> "TechSnapshot":
        """Builds a snapshot from one pre-computed indicator row.

        This is the hot path: the pipeline uses it for every symbol on every
        day, so the whole indicator frame is computed once per symbol rather
        than rolled per (symbol, day) pair.
        """

        def _v(key):
            if key not in row.index:
                return None
            val = row[key]
            if val is None or pd.isna(val):
                return None
            return float(val)

        below = row["below_sma_run"] if "below_sma_run" in row.index else 0
        return cls(
            symbol=symbol,
            date=row.name,
            close=float(row["Close"]),
            sma_fast=_v("sma_fast"),
            sma_slow=_v("sma_slow"),
            sma_exit=_v("sma_exit"),
            atr=_v("atr"),
            rsi=_v("rsi"),
            rsi_prev1=_v("rsi_prev1"),
            rsi_prev2=_v("rsi_prev2"),
            rsi_prev3=_v("rsi_prev3"),
            breakout_high=_v("breakout_high"),
            vol_ratio=_v("vol_ratio"),
            mom_12_1=_v("mom_12_1"),
            rel_strength=_v("rel_strength"),
            closes_below_sma=int(below) if not pd.isna(below) else 0,
        )

    # ── derived predicates ──────────────────────────────────────────────────
    @property
    def trend_ok(self) -> bool:
        """close > 200DMA AND 50DMA > 200DMA."""
        if self.sma_slow is None or self.sma_fast is None:
            return False
        return self.close > self.sma_slow and self.sma_fast > self.sma_slow

    @property
    def above_slow_ma(self) -> bool:
        return self.sma_slow is not None and self.close > self.sma_slow

    @property
    def breakout_setup(self) -> bool:
        if self.breakout_high is None or self.vol_ratio is None:
            return False
        need_vol = float(investor_config.technical.get("breakout_vol_multiple", 1.2))
        return self.close > self.breakout_high and self.vol_ratio >= need_vol

    @property
    def pullback_setup(self) -> bool:
        """Price at the 50DMA with RSI(14) turning up through 40.

        Two details worth stating because both were got wrong at some point in
        this codebase:

        *   "Turning up through 40" is a CROSSOVER, not "RSI is above 40".
            A stock sitting at RSI 60 with price glued to the 50DMA is not
            turning up through anything; it is just trending. Requiring the
            crossing is what makes this a pullback setup rather than a
            momentum duplicate of the breakout.
        *   "To the 50DMA" is a tolerance band. An exact equality test
            essentially never fires on real data.
        """
        if self.sma_fast is None or self.rsi is None:
            return False
        level = float(investor_config.technical.get("rsi_turn_up_level", 40.0))
        tol = float(investor_config.technical.get("pullback_ma_tolerance", 0.03))
        near_fast = abs(self.close - self.sma_fast) / max(self.sma_fast, 1e-9) <= tol
        if not near_fast:
            return False
        if self.rsi < level:
            return False
        # A crossover is required, but it may have happened on any of the last
        # few sessions (see IndicatorCache for why the window exists).
        prevs = [p for p in (self.rsi_prev1, self.rsi_prev2, self.rsi_prev3) if p is not None]
        return any(p < level for p in prevs)

    @property
    def momentum_ok(self) -> bool:
        """12-1 month return positive AND 6m relative strength vs Nifty 500 > 0."""
        return (self.mom_12_1 or 0.0) > 0.0 and (self.rel_strength or 0.0) > 0.0

    @property
    def below_trend_days(self) -> int:
        return self.closes_below_sma


def snapshot_from_cache(
    cache, symbol: str, as_of: pd.Timestamp
) -> Optional[TechSnapshot]:
    """Snapshot for one symbol/day from a pre-computed IndicatorCache."""
    row = cache.row_at(symbol, pd.Timestamp(as_of))
    if row is None:
        return None
    if pd.isna(row.get("sma_slow")):
        return None      # not enough history for the 200DMA yet
    return TechSnapshot.from_row(symbol, row)


def compute_snapshot(
    symbol: str,
    df: pd.DataFrame,
    nifty: Optional[pd.DataFrame] = None,
    as_of: Optional[pd.Timestamp] = None,
) -> Optional[TechSnapshot]:
    """Computes the full Layer 2 snapshot from history ending at `as_of`.

    `df` must already be sliced to `as_of`; passing a longer frame is the one
    way to introduce look-ahead here, so the backtest always hands in the
    panel's own slice rather than the raw frame.
    """
    t = investor_config.technical
    slow_n = int(t.get("trend_ma_days", 200))
    fast_n = int(t.get("fast_ma_days", 50))
    bo_n = int(t.get("breakout_days", 55))
    atr_n = int(t.get("atr_period", 22))
    rsi_n = int(t.get("rsi_period", 14))
    mom = t.get("momentum", {})

    if df.empty or len(df) < max(slow_n, bo_n, atr_n):
        return None

    close = float(df["Close"].iloc[-1])
    slow_series = sma(df, slow_n)
    fast_series = sma(df, fast_n)
    atr_series = atr(df, atr_n)
    rsi_series = rsi(df, rsi_n)

    # Breakout high EXCLUDES the current bar: "close > 55-day high" must mean
    # the highest of the *previous* 55 closes, otherwise close can never exceed
    # a window that contains it and the setup is unfireable.
    bo_series = df["Close"].shift(1).rolling(bo_n).max()
    vr_series = volume_ratio(df, 20)

    # 12-1 month momentum: ~21 trading days/month.
    mom_lookback = int(mom.get("lookback_months", 12)) * 21
    mom_skip = int(mom.get("skip_months", 1)) * 21
    mom_12_1 = None
    if len(df) > mom_lookback:
        past = float(df["Close"].iloc[-mom_lookback])
        recent = float(df["Close"].iloc[-mom_skip]) if len(df) > mom_skip else close
        if past > 0 and recent > 0:
            mom_12_1 = recent / past - 1.0

    rel_strength = None
    rs_months = int(mom.get("rel_strength_months", 6))
    if nifty is not None and not nifty.empty and len(nifty) >= rs_months * 21 + 1:
        # Align on the last common timestamp so a missing stock bar does not
        # silently pair it with a stale index level.
        common = df.index.intersection(nifty.index)
        if len(common) > rs_months * 21:
            sym_ret = df.loc[common, "Close"]
            idx_ret = nifty.loc[common, "Close"]
            n = rs_months * 21
            if float(sym_ret.iloc[-n - 1]) > 0 and float(idx_ret.iloc[-n - 1]) > 0:
                rel_strength = (
                    float(sym_ret.iloc[-1]) / float(sym_ret.iloc[-n - 1])
                    - float(idx_ret.iloc[-1]) / float(idx_ret.iloc[-n - 1])
                )

    # Consecutive closes below the slow MA, counting back from the last bar.
    below = 0
    slow_vals = slow_series.dropna()
    if not slow_vals.empty:
        closes = df.loc[slow_vals.index, "Close"]
        for c, s in zip(closes.iloc[::-1], slow_vals.iloc[::-1]):
            if c < s:
                below += 1
            else:
                break

    def _last(series: pd.Series) -> Optional[float]:
        v = series.iloc[-1] if len(series) else np.nan
        return None if pd.isna(v) else float(v)

    return TechSnapshot(
        symbol=symbol,
        date=df.index[-1],
        close=close,
        sma_fast=_last(fast_series),
        sma_slow=_last(slow_series),
        atr=_last(atr_series),
        rsi=_last(rsi_series),
        breakout_high=_last(bo_series),
        vol_ratio=_last(vr_series),
        mom_12_1=mom_12_1,
        rel_strength=rel_strength,
        closes_below_sma=below,
        ready=True,
    )


def compute_atr_series(df: pd.DataFrame, period: Optional[int] = None) -> pd.Series:
    n = int(period if period is not None else investor_config.technical.get("atr_period", 22))
    return atr(df, n)


# ─────────────────────────────────────────────────────────────────────────────
# Entry / exit decisions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EntrySignal:
    symbol: str
    date: pd.Timestamp
    setup: str            # BREAKOUT | PULLBACK
    tranche: int          # 1 or 2
    size_fraction: float  # 0.50 / 0.50 / 1.25 (catalyst)
    close: float
    reason: str
    momentum_ok: bool = True
    trend_ok: bool = True

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol, "date": self.date, "setup": self.setup,
            "tranche": self.tranche, "size_fraction": self.size_fraction,
            "close": self.close, "reason": self.reason,
        }


def entry_signal(snapshot: TechSnapshot, eligible: bool, use_technical: bool = True) -> Optional[EntrySignal]:
    """Entry evaluation for one eligible name.

    With `use_technical=True` (the live strategy) an entry needs the full
    Layer 2 gate: trend filter plus one of the two setups.

    With `use_technical=False` this is the "Fundamentals only" ablation, where
    the point is to hold the qualifying names and buy nothing on a chart
    signal. In that mode every eligible name enters — returning None here
    instead would make the ablation produce an empty book and prove nothing.
    """
    t = investor_config.technical
    if not eligible:
        return None

    if not use_technical:
        return EntrySignal(
            symbol=snapshot.symbol,
            date=snapshot.date,
            setup="FUNDAMENTAL",
            tranche=1,
            size_fraction=1.0,
            close=snapshot.close,
            reason="Fundamentals-only ablation: buy and hold the qualifying name",
            momentum_ok=snapshot.momentum_ok,
            trend_ok=snapshot.trend_ok,
        )

    if not snapshot.trend_ok:
        return None

    if snapshot.breakout_setup:
        setup = "BREAKOUT"
    elif snapshot.pullback_setup:
        setup = "PULLBACK"
    else:
        return None

    return EntrySignal(
        symbol=snapshot.symbol,
        date=snapshot.date,
        setup=setup,
        tranche=1,
        size_fraction=float(t.get("tranches", {}).get("tranche1_pct", 0.5)),
        close=snapshot.close,
        reason=f"{setup} | close {snapshot.close:.1f} > 200DMA {snapshot.sma_slow or 0:.1f}",
        momentum_ok=snapshot.momentum_ok,
        trend_ok=snapshot.trend_ok,
    )


def tranche2_signal(
    snapshot: TechSnapshot,
    days_since_tranche1: int,
    fast_ma: Optional[float],
) -> Optional[EntrySignal]:
    """Tranche 2: ten trading days after tranche 1, still above the 50DMA."""
    t = investor_config.technical
    delay = int(t.get("tranches", {}).get("tranche2_delay_days", 10))
    if days_since_tranche1 < delay:
        return None
    if fast_ma is None or snapshot.close <= fast_ma:
        return None
    return EntrySignal(
        symbol=snapshot.symbol,
        date=snapshot.date,
        setup="TRANCH2",
        tranche=2,
        size_fraction=float(t.get("tranches", {}).get("tranche2_pct", 0.5)),
        close=snapshot.close,
        reason=f"Tranche 2 add | close {snapshot.close:.1f} still > 50DMA {fast_ma:.1f}",
    )


@dataclass
class ExitSignal:
    symbol: str
    date: pd.Timestamp
    reason: str
    kind: str   # CHANDELIER | TREND_BREAK | VETO | FUNDAMENTAL | VALUATION


class ChandelierTracker:
    """Per-position ratcheting Chandelier stop.

    State is deliberately the *highest close since entry* plus a stop that may
    only ever rise. Recomputing from scratch each day would let a stop fall
    back down after a pullback, which is exactly the bug the brief's "never
    lowers" clause exists to prevent.
    """

    def __init__(self, atr_period: Optional[int] = None, multiplier: Optional[float] = None):
        t = investor_config.technical
        self.atr_period = int(atr_period if atr_period is not None else t.get("atr_period", 22))
        self.multiplier = float(
            multiplier if multiplier is not None
            else t.get("chandelier_atr_multiple", 3.5)
        )

    def initial_stop(self, close: float, atr_value: Optional[float]) -> Optional[float]:
        if not atr_value or atr_value <= 0:
            return None
        return close - self.multiplier * atr_value

    def update(
        self,
        highest_close_since_entry: float,
        close: float,
        atr_value: Optional[float],
        current_stop: Optional[float],
    ) -> Tuple[Optional[float], float]:
        """Returns (new_stop, new_highest_close). The stop never decreases."""
        highest = max(highest_close_since_entry, close)
        if atr_value and atr_value > 0:
            candidate = highest - self.multiplier * atr_value
        else:
            candidate = current_stop
        if current_stop is None:
            new_stop = candidate
        else:
            new_stop = max(float(current_stop), float(candidate) if candidate is not None else -np.inf)
        return (None if new_stop == -np.inf else new_stop), highest

    def breached(self, low: float, stop: Optional[float]) -> bool:
        if stop is None:
            return False
        return float(low) <= float(stop)


def technical_exit(
    snapshot: TechSnapshot,
    stop: Optional[float],
    low: Optional[float] = None,
) -> Optional[ExitSignal]:
    """Stop-based exits evaluated on a single day's bar."""
    need = int(investor_config.technical.get("exit_consecutive_closes", 3))
    if stop is not None and low is not None and float(low) <= float(stop):
        return ExitSignal(
            symbol=snapshot.symbol, date=snapshot.date,
            reason=f"Chandelier stop breached: low {float(low):.1f} <= stop {float(stop):.1f}",
            kind="CHANDELIER",
        )
    if snapshot.closes_below_sma >= need:
        return ExitSignal(
            symbol=snapshot.symbol, date=snapshot.date,
            reason=f"{snapshot.closes_below_sma} consecutive closes below 200DMA "
                   f"({snapshot.close:.1f} < {snapshot.sma_slow or 0:.1f})",
            kind="TREND_BREAK",
        )
    return None


def momentum_rank_table(
    snapshots: Sequence[TechSnapshot],
) -> pd.DataFrame:
    """Ranks the cross-section by 12-1 momentum, for reporting and tie-breaks."""
    if not snapshots:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "symbol": s.symbol,
        "mom_12_1": s.mom_12_1,
        "rel_strength": s.rel_strength,
        "trend_ok": s.trend_ok,
        "rsi": s.rsi,
    } for s in snapshots]).set_index("symbol")
    df["mom_rank"] = df["mom_12_1"].rank(ascending=False)
    return df.sort_values("mom_rank")


__all__ = [
    "TechSnapshot", "EntrySignal", "ExitSignal", "ChandelierTracker",
    "sma", "atr", "rsi", "true_range", "volume_ratio", "trailing_high",
    "compute_snapshot", "snapshot_from_cache", "compute_atr_series",
    "entry_signal", "tranche2_signal", "technical_exit", "momentum_rank_table",
]