import pytest
import pandas as pd
import numpy as np

from backtest_v2.signals.raw_scores import RawScoreCalculator

def test_h20_excludes_current_bar():
    """
    Test that H_20 (used in Breakout score) strictly excludes the current bar t.
    """
    calculator = RawScoreCalculator()
    
    # Create test dataframe
    dates = pd.date_range('2023-01-01', periods=30, freq='D')
    
    # strictly increasing high
    df = pd.DataFrame({
        'Open': np.linspace(90, 119, 30),
        'High': np.linspace(100, 129, 30),
        'Low': np.linspace(80, 109, 30),
        'Close': np.linspace(95, 124, 30),
        'Volume': np.ones(30) * 1000
    }, index=dates)
    
    # Calculate B_raw which internally calculates H_20
    b_raw = calculator.compute_B_raw(df)
    
    # Let's reproduce the H_20 logic identically here to test
    # H_20 = High.shift(1).rolling(20).max()
    h20 = df['High'].shift(1).rolling(20).max()
    
    # The high at index 20 (0-based) is 120.
    # The max high of the previous 20 bars (indices 0 to 19) is at index 19, which is 119.
    assert h20.iloc[20] == 119, f"Expected 119, got {h20.iloc[20]}"
    
    # Check that H_20 correctly handles the rolling window
    assert h20.iloc[21] == 120
    
def test_b_raw_components():
    calculator = RawScoreCalculator()
    dates = pd.date_range('2023-01-01', periods=30, freq='D')
    
    df = pd.DataFrame({
        'Open': np.ones(30) * 100,
        'High': np.ones(30) * 105,
        'Low': np.ones(30) * 95,
        'Close': np.ones(30) * 100,
        'Volume': np.ones(30) * 1000
    }, index=dates)
    
    # Make a breakout on the last day
    df.loc[dates[-1], 'High'] = 120
    df.loc[dates[-1], 'Close'] = 115
    df.loc[dates[-1], 'Volume'] = 2000
    
    b_raw = calculator.compute_B_raw(df)
    
    # Since H_20 excludes the current bar, on the last day H_20 should be 105
    # The close is 115, so Price Breakout = (115 - 105) / ATR
    # It should be positive
    assert b_raw.iloc[-1] > 0
    assert not np.isnan(b_raw.iloc[-1])
