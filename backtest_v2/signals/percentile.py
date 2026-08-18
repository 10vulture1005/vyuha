from typing import Union, Optional
import pandas as pd
import numpy as np
from loguru import logger


def trailing_percentile(
    series: Union[pd.Series, np.ndarray], 
    window: int,
    min_periods: Optional[int] = None
) -> pd.Series:
    """
    Compute trailing empirical percentile (CDF) for each point.
    
    P_N(X_t) = (1/N) * sum_{i=t-N+1}^t 1[X_i <= X_t]
    
    Window is [t-N+1, t] - includes current bar, no lookahead.
    
    Args:
        series: Input time series
        window: Rolling window size (N)
        min_periods: Minimum observations required (default: window)
    
    Returns:
        Series of percentiles in [0, 1], NaN during warmup
    """
    if isinstance(series, np.ndarray):
        series = pd.Series(series)
    
    if min_periods is None:
        min_periods = window
    
    def _percentile_rank(arr: np.ndarray) -> float:
        """Percentile rank of last element in array."""
        if len(arr) == 0:
            return np.nan
        last = arr[-1]
        if np.isnan(last):
            return np.nan
        # Count values <= last (including last itself)
        count_le = np.sum(arr <= last)
        return count_le / len(arr)
    
    result = series.rolling(window=window, min_periods=min_periods).apply(
        _percentile_rank, raw=True
    )
    
    return result


def expanding_percentile(series: Union[pd.Series, np.ndarray]) -> pd.Series:
    """
    Expanding percentile rank - uses all history up to each point.
    """
    if isinstance(series, np.ndarray):
        series = pd.Series(series)
    
    def _percentile_rank(arr: np.ndarray) -> float:
        if len(arr) == 0:
            return np.nan
        last = arr[-1]
        if np.isnan(last):
            return np.nan
        count_le = np.sum(arr <= last)
        return count_le / len(arr)
    
    return series.expanding().apply(_percentile_rank, raw=True)


def trailing_zscore(
    series: Union[pd.Series, np.ndarray], 
    window: int,
    min_periods: Optional[int] = None
) -> pd.Series:
    """
    Trailing z-score for comparison/validation.
    """
    if isinstance(series, np.ndarray):
        series = pd.Series(series)
    
    if min_periods is None:
        min_periods = window
    
    rolling_mean = series.rolling(window=window, min_periods=min_periods).mean()
    rolling_std = series.rolling(window=window, min_periods=min_periods).std()
    
    return (series - rolling_mean) / (rolling_std + 1e-12)


# For testing: verify no lookahead
def verify_no_lookahead(percentile_func, test_series: pd.Series, window: int) -> bool:
    """
    Verify that percentile at index i only uses data up to i.
    """
    result = percentile_func(test_series, window)
    
    # For each point, manually compute using only historical data
    for i in range(window - 1, len(test_series)):
        hist = test_series.iloc[i - window + 1:i + 1]
        expected = (hist <= test_series.iloc[i]).mean()
        actual = result.iloc[i]
        if not np.isclose(expected, actual, rtol=1e-10):
            logger.error(f"Lookahead detected at index {i}: expected {expected}, got {actual}")
            return False
    
    return True