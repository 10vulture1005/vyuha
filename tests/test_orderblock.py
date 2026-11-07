# tests/test_orderblock.py
"""Order-Block detector tests (nse-orderblock, vendored).

A block = origin bearish candle + bullish displacement (body >= 1.0xATR,
close in top 25%). Entry = first-touch limit at zone top (Mode A).
Stop = entry - 2.0 x lagged ATR(14) (Experiment E winner).
"""
import numpy as np
import pandas as pd

from agents.tools.orderblock_tools import detect_orderblock
from agents.tools.ta_tools import PatternType


def _synthetic_ob(length=260):
    """Bearish origin, strong bullish displacement, then pullback touch."""
    dates = pd.date_range("2024-01-01", periods=length, freq="D")
    close = np.full(length, 100.0)
    opens = np.full(length, 100.0)
    highs = np.full(length, 101.0)
    lows = np.full(length, 99.0)
    vols = np.full(length, 100000.0)
    # Origin bearish candle (zone = high 100 / low 95)
    i0 = length - 4
    opens[i0] = 99.0
    highs[i0] = 100.0
    lows[i0] = 95.0
    close[i0] = 96.0
    # Displacement bar: close 105, top-25% close, body >= ATR (~2) -> ok
    i1 = length - 3
    opens[i1] = 96.5
    highs[i1] = 105.5
    lows[i1] = 95.8
    close[i1] = 105.2
    vols[i1] = 250000.0
    for idx in range(i1 + 1, length):
        opens[idx] = 104.0
        highs[idx] = 105.0
        lows[idx] = 99.0
        close[idx] = 102.0
    df = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": close, "Volume": vols},
        index=dates,
    )
    df.index.name = "Date"
    return df


def test_orderblock_first_touch():
    res = detect_orderblock(_synthetic_ob())
    assert res is not None
    assert res.pattern_type == PatternType.ORDER_BLOCK
    assert 0 < res.structural_stop_price < res.entry_price
    assert res.atr_14 > 0
    # Entry should sit at the touched zone top (origin high 100) or better (gap open).
    assert res.entry_price <= 100.0 + 1e-9


def test_orderblock_no_displacement_no_signal():
    df = _synthetic_ob()
    # Kill the displacement bar: weak close, not in top 25%.
    df.iloc[-3, df.columns.get_loc("Close")] = 96.6
    assert detect_orderblock(df) is None


def test_orderblock_causality_detection_day_not_touchable():
    # Only origin + displacement bars (no pullback touch yet) -> no signal.
    df = _synthetic_ob().iloc[:-1].copy()
    # Ensure last bar does not touch zone top (low 101 > zone top 100).
    df.iloc[-1, df.columns.get_loc("Low")] = 101.0
    df.iloc[-1, df.columns.get_loc("High")] = 102.0
    res = detect_orderblock(df)
    # Without a touch there is no entry candidate.
    assert res is None
