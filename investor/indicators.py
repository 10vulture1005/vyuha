# investor/indicators.py
"""Vectorised indicator pre-computation shared by every layer.

Why this exists: the first working version of the pipeline called
`technical.compute_snapshot()` once per symbol per day, and each call rolled a
200-day SMA, a 22-period ATR and a 14-period RSI from scratch over the whole
history. That is O(days x symbols x history) and a 3-year, 180-name replay did
not finish in two minutes.

Pre-computing each indicator ONCE per symbol over the full history and then
indexing the row for date t is mathematically identical — every indicator here
is strictly backward-looking — and turns the same replay into a lookup.

The look-ahead guarantee is unchanged: `row_at(symbol, day)` returns the row
for `day`, and that row was built only from bars at or before `day`.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from investor.config import investor_config
from investor.data import PricePanel

#: ~21 trading days per Indian trading month.
TRADING_DAYS_PER_MONTH = 21


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing (EMA with alpha = 1/period)."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


class IndicatorCache:
    """Per-symbol indicator frames, computed once and sliced per day."""

    def __init__(
        self,
        panel: PricePanel,
        nifty_symbol: Optional[str] = None,
    ):
        self.panel = panel
        self.nifty_symbol = nifty_symbol or investor_config.regime.get("nifty_proxy", "INDEX_NIFTY500")
        t = investor_config.technical
        r = investor_config.regime

        self.slow_n = int(t.get("trend_ma_days", 200))
        self.fast_n = int(t.get("fast_ma_days", 50))
        self.exit_n = int(t.get("exit_ma_days", 200))
        self.breakout_n = int(t.get("breakout_days", 55))
        self.atr_n = int(t.get("atr_period", 22))
        self.rsi_n = int(t.get("rsi_period", 14))
        self.regime_ma_n = int(r.get("trend_ma_days", 200))

        mom = t.get("momentum", {})
        self.mom_lookback = int(mom.get("lookback_months", 12)) * TRADING_DAYS_PER_MONTH
        self.mom_skip = int(mom.get("skip_months", 1)) * TRADING_DAYS_PER_MONTH
        self.rs_months = int(mom.get("rel_strength_months", 6))
        self.rs_days = self.rs_months * TRADING_DAYS_PER_MONTH

        self._frames: Dict[str, pd.DataFrame] = {}
        self._nifty_frame: Optional[pd.DataFrame] = None

    # ── construction ────────────────────────────────────────────────────────
    def _nifty(self) -> Optional[pd.DataFrame]:
        if self._nifty_frame is None:
            df = self.panel.load(self.nifty_symbol)
            if df.empty:
                return None
            out = pd.DataFrame({"Close": df["Close"]})
            out["ret"] = out["Close"].pct_change(self.rs_days)
            self._nifty_frame = out
        return self._nifty_frame

    def frame(self, symbol: str) -> pd.DataFrame:
        """Full-history indicator frame for one symbol (empty if no data)."""
        if symbol in self._frames:
            return self._frames[symbol]

        df = self.panel.load(symbol)
        if df.empty:
            self._frames[symbol] = df
            return df

        close = df["Close"]
        prev_close = close.shift()
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ], axis=1).max(axis=1)

        out = pd.DataFrame(index=df.index)
        out["Close"] = close
        out["Open"] = df["Open"]
        out["High"] = df["High"]
        out["Low"] = df["Low"]
        out["Volume"] = df["Volume"]

        out["sma_fast"] = close.rolling(self.fast_n).mean()
        out["sma_slow"] = close.rolling(self.slow_n).mean()
        out["sma_exit"] = close.rolling(self.exit_n).mean()
        out["ma_regime"] = close.rolling(self.regime_ma_n).mean()
        out["atr"] = _wilder(tr, self.atr_n)

        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = _wilder(gain, self.rsi_n)
        avg_loss = _wilder(loss, self.rsi_n)
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        out["rsi"] = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)
        # Prior bars' RSI so the pullback rule can detect a genuine crossover
        # through 40 ("turning up through 40"), not merely "RSI is above 40".
        # Three prior bars are kept: requiring the crossover on the exact same
        # bar as the 50DMA touch fired on 0 of 211 eligible name-days, because
        # the two conditions almost never coincide on one session.
        for lag in range(1, 4):
            out[f"rsi_prev{lag}"] = out["rsi"].shift(lag)

        # Breakout high EXCLUDES the current bar: "close > 55-day high" must
        # compare against the previous 55 closes. Including the current bar
        # makes the condition unfireable (close can never exceed a window that
        # contains it).
        out["breakout_high"] = close.shift(1).rolling(self.breakout_n).max()
        # Volume ratio likewise excludes the current bar from its own average.
        out["vol_ratio"] = df["Volume"] / df["Volume"].shift(1).rolling(20).mean().replace(0.0, np.nan)

        # 12-1 month momentum.
        out["mom_12_1"] = close.shift(self.mom_skip) / close.shift(self.mom_lookback) - 1.0

        # Consecutive closes below the exit MA (running count, per symbol).
        below = (close < out["sma_exit"]).astype("int64")
        grp = (~below.astype(bool)).cumsum()
        out["below_sma_run"] = below.groupby(grp).cumsum()

        # Relative strength vs the benchmark, aligned on common timestamps.
        nifty = self._nifty()
        if nifty is not None:
            aligned = out.index.intersection(nifty.index)
            sym_ret = close.loc[aligned] / close.loc[aligned].shift(self.rs_days) - 1.0
            idx_ret = nifty.loc[aligned, "ret"]
            out["rel_strength"] = (sym_ret - idx_ret).reindex(out.index)

        self._frames[symbol] = out
        return out

    def precompute(self, symbols: Sequence[str]) -> None:
        for s in symbols:
            self.frame(s)

    # ── access ──────────────────────────────────────────────────────────────
    def row_at(self, symbol: str, day: pd.Timestamp) -> Optional[pd.Series]:
        """The indicator row for `day`, or None if the stock did not trade."""
        f = self.frame(symbol)
        if f.empty or day not in f.index:
            return None
        return f.loc[day]

    def breadth_series(self, symbols: Sequence[str]) -> pd.Series:
        """Daily % of `symbols` above their own regime MA.

        Pre-computed rather than recomputed per day for the same O(n^2) reason
        as the per-symbol frames. Symbols lacking history are NaN and are
        excluded from both numerator and denominator.
        """
        frames = []
        for sym in symbols:
            f = self.frame(sym)
            if f.empty or "ma_regime" not in f.columns:
                continue
            ok = f["ma_regime"].notna() & (f["ma_regime"] > 0)
            frames.append((f["Close"] > f["ma_regime"]) & ok)
        if not frames:
            return pd.Series(dtype="float64")
        stacked = pd.concat(frames, axis=1)
        counted = stacked.notna().sum(axis=1)
        above = stacked.sum(axis=1)
        out = (100.0 * above / counted).where(counted > 0)
        return out.dropna()

    def atr_series(self, symbol: str) -> pd.Series:
        f = self.frame(symbol)
        return f["atr"] if "atr" in f.columns else pd.Series(dtype="float64")


@lru_cache(maxsize=4)
def get_cache(panel_id: int, nifty_symbol: Optional[str] = None) -> IndicatorCache:
    """Process-level cache keyed on the panel identity."""
    return IndicatorCache(_PANELS[panel_id], nifty_symbol)


_PANELS: Dict[int, PricePanel] = {}


def register_panel(panel: PricePanel) -> int:
    key = id(panel)
    _PANELS[key] = panel
    return key


__all__ = ["IndicatorCache", "get_cache", "register_panel", "TRADING_DAYS_PER_MONTH"]