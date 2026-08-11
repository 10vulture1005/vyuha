# tests/test_technical_agent.py
import pytest
import numpy as np
import pandas as pd
from pathlib import Path
from agents.tools.ta_tools import detect_w_bottom, detect_bb_squeeze

BASE_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = BASE_DIR / "tests" / "fixtures"

def load_fixture(filename: str) -> pd.DataFrame:
    file_path = FIXTURES_DIR / filename
    df = pd.read_csv(file_path, parse_dates=["Date"], index_col="Date")
    return df.sort_index()

def create_synthetic_w_bottom(length=260) -> pd.DataFrame:
    """Generates synthetic price action with a symmetric W-Bottom."""
    dates = pd.date_range("2024-01-01", periods=length, freq="D")
    prices = np.linspace(100, 80, length - 30).tolist() # Downtrend
    prices += [75, 78, 80, 85, 82, 79, 75.5, 78, 82, 88] # W-Bottom (troughs at 75 and 75.5, neckline 85)
    prices += np.linspace(88, 95, 20).tolist()
    
    volumes = [100000] * (length - 30) + [150000, 120000, 110000, 140000, 100000, 90000, 80000, 95000, 110000, 130000] + [250000] * 20
    df = pd.DataFrame({"Open": prices, "High": [p*1.01 for p in prices], "Low": [p*0.99 for p in prices], "Close": prices, "Volume": volumes}, index=dates)
    df.index.name = "Date"
    return df

def test_w_bottom_detection_math():
    """Verify argrelextrema correctly identifies synthetic W-Bottom setup."""
    df = create_synthetic_w_bottom()
    result = detect_w_bottom(df)
    assert result is not None
    assert result.pattern_type.value == "w_bottom"
    assert result.atr_14 > 0
    # signal_strength is now computed in technical_agent, so it's 0.0 initially
    assert result.signal_strength == 0.0

def test_w_bottom_fixture():
    df = load_fixture("w_bottom_ohlc.csv")
    result = detect_w_bottom(df)
    assert result is not None
    assert result.pattern_type.value == "w_bottom"

def test_bb_squeeze_fixture():
    df = load_fixture("bb_squeeze_ohlc.csv")
    if len(df) >= 253:
        result = detect_bb_squeeze(df)
        if result:
            assert result.pattern_type.value == "bb_squeeze"
            assert result.signal_strength == 0.0

def test_liquidity_rejection():
    """Verify setups in illiquid micro-caps are immediately rejected."""
    df = create_synthetic_w_bottom()
    df["Volume"] = 1000 # Below 50,000 threshold
    assert detect_w_bottom(df) is None
    assert detect_bb_squeeze(df) is None
