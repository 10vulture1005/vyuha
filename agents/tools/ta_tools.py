# agents/tools/ta_tools.py
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel
import pandas as pd
import numpy as np
from loguru import logger
import yfinance as yf

from config import thresholds

class PatternType(str, Enum):
    VCP = "VCP"
    HTF = "HTF"
    W_BOTTOM = "W_BOTTOM"
    BB_SQUEEZE = "BB_SQUEEZE"
    BREAKOUT = "BREAKOUT"
    MEAN_REVERSION = "MEAN_REVERSION"

class TechnicalSignalResult(BaseModel):
    pattern_type: PatternType
    atr_14: float
    entry_price: float
    structural_stop_price: float
    signal_strength: float
    vol_ratio: float
    momentum_composite: Optional[float] = None

def get_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    atr = true_range.rolling(period).mean()
    return float(atr.iloc[-1])

_ohlc_cache: dict = {}
_ohlc_cache_ts: dict = {}
_OHLC_TTL_S = 900  # 15-min TTL: previously infinite, froze intraday bars and delayed exits
_MIN_CLOSED_BARS_W = 60
_MIN_CLOSED_BARS_BB = 100


def _closed_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Returns only confirmed-closed daily bars (drops today's forming candle).

    FIX: live code used df.iloc[-1] from yfinance which is today's incomplete
    bar intraday (partial volume, repainting High/Low/Close). Backtest uses
    closed loc[:date] slices. Dropping the forming bar aligns live with backtest
    and stops EOD spike-chasing on partial-volume signals.
    """
    if df is None or df.empty or len(df) < 2:
        return df
    try:
        from datetime import date as _date
        last_ts = df.index[-1]
        last_date = last_ts.date() if hasattr(last_ts, "date") else None
        if last_date is not None and last_date >= _date.today():
            return df.iloc[:-1]
    except Exception:
        pass
    return df


def _avg_vol_excl_current(vol: pd.Series, window: int = 20) -> float:
    """20-day average volume EXCLUDING the signal bar (no dilution)."""
    if len(vol) < window + 1:
        return float(vol.mean()) if len(vol) else 0.0
    return float(vol.iloc[-(window + 1):-1].mean())


def _slippage_adjusted_entry(bar_high: float, bar_low: float, close: float) -> float:
    """Matches backtest Phase-F realism: entry = close + 10% of daily spread.

    Approximates next-open slippage until true t+1 fills are wired.
    Previously entry_price was the raw same-bar close (untradable, no slippage).
    """
    try:
        if close and close > 0:
            spread = (bar_high - bar_low) / close
            return float(close * (1.0 + max(0.0, spread) * 0.10))
    except Exception:
        pass
    return float(close)

def load_ohlc_df(symbol: str) -> pd.DataFrame:
    """Fetches 1 year of daily historical data using yfinance, with TTL caching."""
    import time
    now = time.time()
    if symbol in _ohlc_cache:
        ts = _ohlc_cache_ts.get(symbol, 0.0)
        if now - ts < _OHLC_TTL_S:
            return _ohlc_cache[symbol]
        _ohlc_cache.pop(symbol, None)
        _ohlc_cache_ts.pop(symbol, None)
        
    ticker_mapping = {
        "INDEX_NIFTY50": "^NSEI",
        "INDEX_NIFTY500": "^CRSLDX",
        "INDEX_INDIAVIX": "^INDIAVIX"
    }
    
    ticker_sym = ticker_mapping.get(symbol)
    if not ticker_sym:
        ticker_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    try:
        ticker = yf.Ticker(ticker_sym)
        df = ticker.history(period="1y")
        if df.empty or len(df) < 5:
            logger.warning(f"No price history returned for {ticker_sym}")
            return pd.DataFrame()
            
        # Ensure we don't have timezone issues, use timezone naive if necessary, but standard yfinance is fine.
        import time as _time
        _ohlc_cache[symbol] = df
        _ohlc_cache_ts[symbol] = _time.time()
        return df
    except Exception as e:
        logger.error(f"Failed to fetch live price for {symbol} via yfinance: {e}")
        return pd.DataFrame()

def detect_w_bottom(df: pd.DataFrame) -> Optional[TechnicalSignalResult]:
    """Identifies confirmed W-Bottom reversals: two distinct troughs + neckline breakout.

    FIX: previously `recent_min = Low.tail(20).min()` INCLUDING the current bar
    with a 10% band fired on any downtrend dip (100% of live signals were
    W_BOTTOM/7.5 — buying falling knives). Now requires:
      1. closed bars only (no forming-candle repaint),
      2. two distinct troughs >= 5 bars apart within tolerance,
      3. close ABOVE the inter-trough neckline (confirmed reversal, not a dip),
      4. volume confirmation vs 20d average EXCLUDING the signal bar,
      5. graded strength (breaks the all-7.5 tie that starved diversification).
    """
    cdf = _closed_bars(df)
    if cdf is None or cdf.empty or len(cdf) < _MIN_CLOSED_BARS_W:
        return None

    t = thresholds.get("technical", {})
    w_tol = float(t.get("w_bottom_tolerance", 0.04))
    vol_min = float(t.get("vol_confirmation_min", 1.15))
    atr_period = int(t.get("atr_period", 14))

    # Reserve the last closed bar as the breakout bar; troughs must form before it.
    if len(cdf) < 45:
        window = cdf.iloc[:-1]
    else:
        window = cdf.iloc[-41:-1]  # 40 bars of structure, last closed bar = breakout candidate
    if len(window) < 30:
        return None

    lows = window["Low"]
    highs = window["High"]
    # Two lowest troughs, required to be distinct in time
    sorted_idx = lows.nsmallest(2).index.tolist()
    if len(sorted_idx) < 2:
        return None
    i1, i2 = sorted(window.index.get_loc(i) for i in sorted_idx)
    if abs(i1 - i2) < 5:
        # Troughs too close = same dip, not a W. Fall back to next-lowest distinct trough.
        ordered = lows.sort_values().index.tolist()
        found = None
        first_pos = window.index.get_loc(ordered[0])
        for cand in ordered[2:]:
            if abs(window.index.get_loc(cand) - first_pos) >= 5:
                found = cand
                break
        if found is None:
            return None
        i2 = window.index.get_loc(found)
        i1 = first_pos
    lo_pos, hi_pos = sorted([i1, i2])
    trough1 = float(lows.iloc[lo_pos])
    trough2 = float(lows.iloc[hi_pos])
    base = min(trough1, trough2)
    if base <= 0:
        return None
    # Symmetry: troughs within tolerance
    if abs(trough2 - trough1) / base > w_tol:
        return None
    # Neckline: highest high BETWEEN the troughs; breakout must clear it
    neckline = float(highs.iloc[lo_pos:hi_pos + 1].max())

    sig_bar = cdf.iloc[-1]
    current_close = float(sig_bar["Close"])
    current_vol = float(sig_bar["Volume"])
    avg_vol = _avg_vol_excl_current(cdf["Volume"], 20)
    if avg_vol and avg_vol > 0:
        if current_vol < avg_vol * vol_min:
            return None
    elif current_vol <= 0:
        return None

    if not (current_close > neckline):
        return None  # no confirmed reversal — still under the neckline

    atr = get_atr(cdf, atr_period)
    if not atr or atr <= 0:
        return None
    stop = base - atr
    if current_close <= stop:
        return None
    entry = _slippage_adjusted_entry(float(sig_bar["High"]), float(sig_bar["Low"]), current_close)
    vol_ratio = (current_vol / avg_vol) if avg_vol and avg_vol > 0 else 1.0
    breakout_pct = (current_close - neckline) / neckline if neckline > 0 else 0.0
    strength = 7.0 + min(1.0, max(0.0, vol_ratio - vol_min) * 0.5) + min(1.0, max(0.0, breakout_pct) * 25.0)
    strength = round(min(9.5, max(6.0, strength)), 2)
    return TechnicalSignalResult(
        pattern_type=PatternType.W_BOTTOM,
        atr_14=float(atr),
        entry_price=float(entry),
        structural_stop_price=float(stop),
        signal_strength=float(strength),
        vol_ratio=float(vol_ratio),
    )

def detect_bb_squeeze(df: pd.DataFrame) -> Optional[TechnicalSignalResult]:
    """Identifies volatility compression + closed-bar breakout above the upper band.

    FIX: previously used the forming bar (repaints intraday) and a volume
    average diluted by the incomplete bar. Now uses closed bars only and
    requires >=100 bars so the 100-bar bandwidth percentile is stable.
    """
    cdf = _closed_bars(df)
    if cdf is None or cdf.empty or len(cdf) < _MIN_CLOSED_BARS_BB:
        return None
        
    t = thresholds.get("technical", {})
    bb_lookback = t.get("bb_lookback", 20)
    vol_min = t.get("vol_confirmation_min", 1.15)
    atr_period = t.get("atr_period", 14)
    
    close_rolling = cdf['Close'].rolling(bb_lookback)
    ma = close_rolling.mean()
    std = close_rolling.std()
    
    upper = ma + (std * 2.0)
    lower = ma - (std * 2.0)
    bbw = (upper - lower) / ma
    
    current_bbw = bbw.iloc[-1]
    historical_bbw = bbw.tail(100)
    pctile = (historical_bbw < current_bbw).mean() * 100
    
    if pctile > t.get("bb_squeeze_percentile", 10.0):
        return None
        
    sig_bar = cdf.iloc[-1]
    current_close = float(sig_bar["Close"])
    current_vol = float(sig_bar["Volume"])
    avg_vol = _avg_vol_excl_current(cdf["Volume"], 20)
    
    if current_vol >= avg_vol * vol_min and current_close > float(upper.iloc[-1]):
        atr = get_atr(cdf, atr_period)
        if not atr or atr <= 0:
            return None
        stop = float(ma.iloc[-1]) - atr
        if current_close > stop:
            entry = _slippage_adjusted_entry(float(sig_bar["High"]), float(sig_bar["Low"]), current_close)
            vol_ratio = (current_vol / avg_vol) if avg_vol and avg_vol > 0 else 1.0
            # Grade strength so BB (compression+breakout) outranks weak W ties
            strength = round(min(9.8, 8.0 + min(1.0, max(0.0, vol_ratio - vol_min) * 0.5)), 2)
            return TechnicalSignalResult(
                pattern_type=PatternType.BB_SQUEEZE,
                atr_14=float(atr),
                entry_price=float(entry),
                structural_stop_price=float(stop),
                signal_strength=float(strength),
                vol_ratio=float(vol_ratio),
            )
    return None
