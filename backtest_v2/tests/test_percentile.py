import pytest
import pandas as pd
import numpy as np

from backtest_v2.signals.percentile import trailing_percentile, verify_no_lookahead

def test_trailing_percentile_no_lookahead():
    """
    Test P_252 / P_120 to ensure they do not use future data.
    """
    # Create test series: linear increasing, length 300
    dates = pd.date_range('2020-01-01', periods=300)
    data = np.linspace(10, 100, 300)
    test_series = pd.Series(data, index=dates)

    window = 120
    
    # 1. Run the existing verify_no_lookahead function which validates mathematically
    assert verify_no_lookahead(trailing_percentile, test_series, window)

    # 2. Manual specific check:
    # At index 150, the value should only depend on indices 150-120+1 = 31 to 150.
    res = trailing_percentile(test_series, window)
    
    # Check that warmup period is NaN
    assert res.iloc[:window - 1].isna().all()
    
    # Since data is strictly increasing, the percentile of the current bar relative to 
    # the last 120 bars (including itself) should always be 1.0.
    assert np.isclose(res.iloc[window - 1], 1.0)
    assert np.isclose(res.iloc[200], 1.0)
    
    # Let's create a random series to verify actual percentile ranks
    np.random.seed(42)
    random_series = pd.Series(np.random.randn(300), index=dates)
    res_rand = trailing_percentile(random_series, window)
    
    # At index 150:
    hist = random_series.iloc[150 - window + 1 : 151]
    expected_pct = (hist <= random_series.iloc[150]).mean()
    assert np.isclose(res_rand.iloc[150], expected_pct)

def test_trailing_percentile_handles_nan():
    dates = pd.date_range('2020-01-01', periods=10)
    data = [1.0, 2.0, np.nan, 4.0, 5.0, 2.5, 3.5, np.nan, 1.0, 6.0]
    series = pd.Series(data, index=dates)
    
    res = trailing_percentile(series, window=5, min_periods=3)
    # The percentile rank of NaN should be NaN
    assert np.isnan(res.iloc[2])
    assert np.isnan(res.iloc[7])
