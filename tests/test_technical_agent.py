# tests/test_technical_agent.py
import numpy as np
import pandas as pd
from pathlib import Path
from agents.tools.ta_tools import detect_w_bottom, detect_bb_squeeze
from db.models import PatternType

BASE_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = BASE_DIR / "tests" / "fixtures"

def load_fixture(filename: str) -> pd.DataFrame:
    file_path = FIXTURES_DIR / filename
    df = pd.read_csv(file_path, parse_dates=["Date"], index_col="Date")
    return df.sort_index()

def create_synthetic_w_bottom(length=260) -> pd.DataFrame:
    """Generates synthetic price action with a symmetric W-Bottom.

    Volume matters: since commit 8d975a0 the detector requires the breakout
    bar to clear ``vol_confirmation_min`` x the 20-bar average *excluding*
    itself. The old fixture gave the final 20 bars identical volume, so the
    breakout bar could never register a spike and the pattern was rejected —
    which is exactly the "100% of live signals were W_BOTTOM, i.e. buying
    falling knives" failure the volume gate was added to prevent. Here the
    recovery leg trades on ordinary volume and only the breakout bar spikes.
    """
    dates = pd.date_range("2024-01-01", periods=length, freq="D")
    prices = np.linspace(100, 80, length - 30).tolist() # Downtrend
    prices += [75, 78, 80, 85, 82, 79, 75.5, 78, 82, 88] # W-Bottom (troughs at 75 and 75.5, neckline 85)
    prices += np.linspace(88, 95, 20).tolist()

    w_leg_volumes = [150000, 120000, 110000, 140000, 100000, 90000, 80000, 95000, 110000, 130000]
    # Ordinary volume across the recovery leg...
    recovery_volumes = [110000] * 19
    # ...then a genuine spike on the breakout bar itself.
    breakout_volume = 400000
    volumes = (
        [100000] * (length - 30)
        + w_leg_volumes
        + recovery_volumes
        + [breakout_volume]
    )
    df = pd.DataFrame({"Open": prices, "High": [p*1.01 for p in prices], "Low": [p*0.99 for p in prices], "Close": prices, "Volume": volumes}, index=dates)
    df.index.name = "Date"
    return df

def test_w_bottom_detection_math():
    """Verify the detector correctly identifies a synthetic W-Bottom setup."""
    df = create_synthetic_w_bottom()
    result = detect_w_bottom(df)
    assert result is not None
    assert result.pattern_type == PatternType.W_BOTTOM
    assert result.atr_14 > 0
    assert result.structural_stop_price > 0
    assert result.entry_price > 0

def test_w_bottom_requires_volume_confirmation():
    """The same price shape without a volume spike must be rejected.

    Guards the falling-knife regression: price geometry alone is not a signal.
    """
    df = create_synthetic_w_bottom()
    df["Volume"] = 100000  # flat volume -> no bar can beat the 20-bar average
    assert detect_w_bottom(df) is None

def test_w_bottom_fixture():
    df = load_fixture("w_bottom_ohlc.csv")
    result = detect_w_bottom(df)
    assert result is not None
    assert result.pattern_type == PatternType.W_BOTTOM

def test_bb_squeeze_fixture():
    """Exercise the squeeze detector against its fixture.

    The old version wrapped this in ``if len(df) >= 253`` and an inner
    ``if result:``, so it passed vacuously: the fixture has 200 rows, the
    detector requires only 100, and a non-detecting fixture still counted as
    green. It also asserted ``signal_strength == 0.0``, but strength is graded
    in the detector now.
    """
    df = load_fixture("bb_squeeze_ohlc.csv")
    assert len(df) >= 100, "fixture must be long enough for the detector to run"
    result = detect_bb_squeeze(df)
    assert result is not None, "squeeze fixture should produce a signal"
    assert result.pattern_type == PatternType.BB_SQUEEZE
    assert result.structural_stop_price > 0
    assert result.entry_price > 0

def test_liquidity_rejection():
    """Verify setups in illiquid micro-caps are immediately rejected."""
    df = create_synthetic_w_bottom()
    df["Volume"] = 1000 # Below 50,000 threshold
    assert detect_w_bottom(df) is None
    assert detect_bb_squeeze(df) is None
