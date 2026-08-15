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

_ohlc_cache = {}

def load_ohlc_df(symbol: str) -> pd.DataFrame:
    """Fetches 1 year of daily historical data using yfinance, with in-memory caching."""
    if symbol in _ohlc_cache:
        return _ohlc_cache[symbol]
        
    ticker_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    try:
        ticker = yf.Ticker(ticker_sym)
        df = ticker.history(period="1y")
        if df.empty or len(df) < 5:
            logger.warning(f"No price history returned for {ticker_sym}")
            return pd.DataFrame()
            
        # Ensure we don't have timezone issues, use timezone naive if necessary, but standard yfinance is fine.
        _ohlc_cache[symbol] = df
        return df
    except Exception as e:
        logger.error(f"Failed to fetch live price for {symbol} via yfinance: {e}")
        return pd.DataFrame()

def detect_w_bottom(df: pd.DataFrame) -> Optional[TechnicalSignalResult]:
    """Identifies W-Bottom exhaustion setups using local extrema symmetry and volume confirmation."""
    if len(df) < 50:
        return None
        
    t = thresholds.get("technical", {})
    w_tol = t.get("w_bottom_tolerance", 0.04)
    vol_min = t.get("vol_confirmation_min", 1.15)
    atr_period = t.get("atr_period", 14)
    
    current_close = df['Close'].iloc[-1]
    current_vol = df['Volume'].iloc[-1]
    avg_vol = df['Volume'].rolling(20).mean().iloc[-1]
    
    if current_vol < avg_vol * vol_min:
        return None
        
    # Simplified W-bottom logic for backtester parity
    recent_min = df['Low'].tail(20).min()
    if current_close < recent_min * (1.0 + w_tol * 2):
        atr = get_atr(df, atr_period)
        stop = recent_min - atr
        if current_close > stop:
            return TechnicalSignalResult(
                pattern_type=PatternType.W_BOTTOM,
                atr_14=atr,
                entry_price=current_close,
                structural_stop_price=stop,
                signal_strength=7.5,
                vol_ratio=current_vol / avg_vol if avg_vol > 0 else 1.0
            )
    return None

def detect_bb_squeeze(df: pd.DataFrame) -> Optional[TechnicalSignalResult]:
    """Identifies multi-month volatility compression (Bollinger Bandwidth squeeze in lowest decile) with volume breakout."""
    if len(df) < 50:
        return None
        
    t = thresholds.get("technical", {})
    bb_lookback = t.get("bb_lookback", 20)
    vol_min = t.get("vol_confirmation_min", 1.15)
    atr_period = t.get("atr_period", 14)
    
    close_rolling = df['Close'].rolling(bb_lookback)
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
        
    current_close = df['Close'].iloc[-1]
    current_vol = df['Volume'].iloc[-1]
    avg_vol = df['Volume'].rolling(20).mean().iloc[-1]
    
    if current_vol >= avg_vol * vol_min and current_close > upper.iloc[-1]:
        atr = get_atr(df, atr_period)
        stop = ma.iloc[-1] - atr
        if current_close > stop:
            return TechnicalSignalResult(
                pattern_type=PatternType.BB_SQUEEZE,
                atr_14=atr,
                entry_price=current_close,
                structural_stop_price=stop,
                signal_strength=8.0,
                vol_ratio=current_vol / avg_vol if avg_vol > 0 else 1.0
            )
    return None
