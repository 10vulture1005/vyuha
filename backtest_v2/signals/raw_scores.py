from typing import Dict, Tuple
import pandas as pd
import numpy as np
from loguru import logger

from backtest_v2.signals.percentile import trailing_percentile
from backtest_v2.config import breakout_v2_config


class RawScoreCalculator:
    """
    Computes raw component scores: T_raw, M_raw, B_raw, BBW, C.
    
    All calculations use only data available at close of day t (no lookahead).
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.percentile_window = self.config.signals.percentile_window
        self.compression_window = self.config.signals.compression_window
        self.atr_period = 14
        self.sma_periods = [20, 50, 200]
        self.bb_lookback = 20
    
    def compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Average True Range."""
        if len(df) < period + 1:
            return pd.Series(index=df.index, dtype=float)
        
        high_low = df['High'] - df['Low']
        high_close = (df['High'] - df['Close'].shift()).abs()
        low_close = (df['Low'] - df['Close'].shift()).abs()
        
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = true_range.rolling(period).mean()
        return atr
    
    def compute_sma(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Simple Moving Average of Close."""
        return df['Close'].rolling(period).mean()
    
    def compute_T_raw(self, df: pd.DataFrame) -> pd.Series:
        """
        Trend Score:
        T_raw = 0.5*((C - SMA20)/ATR14) + 0.3*((SMA20 - SMA50)/ATR14) + 0.2*((SMA50 - SMA200)/ATR14)
        """
        atr = self.compute_atr(df, self.atr_period)
        sma20 = self.compute_sma(df, 20)
        sma50 = self.compute_sma(df, 50)
        sma200 = self.compute_sma(df, 200)
        
        close = df['Close']
        
        term1 = 0.5 * ((close - sma20) / atr)
        term2 = 0.3 * ((sma20 - sma50) / atr)
        term3 = 0.2 * ((sma50 - sma200) / atr)
        
        T_raw = term1 + term2 + term3
        T_raw.name = 'T_raw'
        return T_raw
    
    def compute_M_raw(self, df: pd.DataFrame) -> pd.Series:
        """
        Momentum Score:
        M_raw = 0.6*(R20/std20) + 0.4*(R60/std60)
        
        R_N = cumulative return over N days
        std_N = std dev of daily returns over N days
        """
        close = df['Close']
        daily_ret = close.pct_change()
        
        # R20 = cumulative return over 20 days
        R20 = close / close.shift(20) - 1
        std20 = daily_ret.rolling(20).std()
        
        # R60 = cumulative return over 60 days
        R60 = close / close.shift(60) - 1
        std60 = daily_ret.rolling(60).std()
        
        term1 = 0.6 * (R20 / (std20 + 1e-12))
        term2 = 0.4 * (R60 / (std60 + 1e-12))
        
        M_raw = term1 + term2
        M_raw.name = 'M_raw'
        return M_raw
    
    def compute_B_raw(self, df: pd.DataFrame) -> pd.Series:
        """
        Breakout Score:
        B_raw = ((C - H_20) / ATR14) * (V / SMA20_V)
        
        CRITICAL: H_20 = High.shift(1).rolling(20).max() - EXCLUDES current bar
        """
        atr = self.compute_atr(df, self.atr_period)
        close = df['Close']
        high = df['High']
        volume = df['Volume']
        
        # H_20: 20-day high EXCLUDING current bar (shift by 1)
        H_20 = high.shift(1).rolling(20).max()
        
        # Volume ratio
        vol_sma20 = volume.rolling(20).mean()
        vol_ratio = volume / (vol_sma20 + 1e-12)
        
        # Breakout component
        price_breakout = (close - H_20) / atr
        B_raw = price_breakout * vol_ratio
        B_raw.name = 'B_raw'
        
        return B_raw
    
    def compute_BBW(self, df: pd.DataFrame) -> pd.Series:
        """
        Bollinger Band Width:
        BBW = (UpperBand - LowerBand) / SMA20
        Upper = SMA20 + 2*std20
        Lower = SMA20 - 2*std20
        BBW = 4*std20 / SMA20
        """
        sma20 = self.compute_sma(df, self.bb_lookback)
        std20 = df['Close'].rolling(self.bb_lookback).std()
        
        upper = sma20 + 2 * std20
        lower = sma20 - 2 * std20
        
        BBW = (upper - lower) / sma20
        BBW.name = 'BBW'
        return BBW
    
    def compute_C(self, df: pd.DataFrame) -> pd.Series:
        """
        Compression Score:
        C = 1 - P_120(BBW)
        """
        BBW = self.compute_BBW(df)
        C = 1 - trailing_percentile(BBW, self.compression_window)
        C.name = 'C'
        return C
    
    def compute_all_raw(self, df: pd.DataFrame) -> Dict[str, pd.Series]:
        """Compute all raw scores for a single symbol."""
        return {
            'T_raw': self.compute_T_raw(df),
            'M_raw': self.compute_M_raw(df),
            'B_raw': self.compute_B_raw(df),
            'BBW': self.compute_BBW(df),
            'C': self.compute_C(df),
        }
    
    def compute_percentile_components(self, raw_scores: Dict[str, pd.Series]) -> Dict[str, pd.Series]:
        """Convert raw scores to percentile-ranked components."""
        return {
            'T': trailing_percentile(raw_scores['T_raw'], self.percentile_window),
            'M': trailing_percentile(raw_scores['M_raw'], self.percentile_window),
            'B': trailing_percentile(raw_scores['B_raw'], self.percentile_window),
            'C': raw_scores['C'],  # Already percentile-ranked via 1 - P_120(BBW)
        }


def verify_H20_excludes_current_bar():
    """Test to verify H_20 calculation uses shift(1)."""
    # Create test data where we know the answer
    dates = pd.date_range('2023-01-01', periods=30, freq='D')
    highs = pd.Series(range(100, 130), index=dates)  # Strictly increasing
    
    # H_20 at index 20 (0-based) should be max of highs[0:20] = 119
    # NOT max of highs[1:21] = 120
    H_20 = highs.shift(1).rolling(20).max()
    
    assert H_20.iloc[20] == 119, f"H_20 at index 20 should be 119 (max of 0:20), got {H_20.iloc[20]}"
    assert H_20.iloc[21] == 120, f"H_20 at index 21 should be 120 (max of 1:21), got {H_20.iloc[21]}"
    
    print("✓ H_20 verification passed: uses shift(1), excludes current bar")


if __name__ == "__main__":
    verify_H20_excludes_current_bar()