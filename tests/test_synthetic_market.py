import numpy as np
import pandas as pd
import pytest

from backtest_investor.synthetic import SCENARIOS, generate_market


def test_synthetic_generation_reproducible_and_valid():
    a = generate_market(["A", "B"], sessions=100, seed=12)
    b = generate_market(["A", "B"], sessions=100, seed=12)
    pd.testing.assert_frame_equal(a.get("A"), b.get("A"))
    assert "INDEX_NIFTY500" in a.available_symbols()
    for symbol in ("A", "B"):
        frame = a.get(symbol)
        assert len(frame) == 100
        assert np.isfinite(frame.to_numpy()).all()
        assert (frame["Low"] <= frame[["Open", "Close"]].min(axis=1)).all()
        assert (frame["High"] >= frame[["Open", "Close"]].max(axis=1)).all()
        assert (frame["Volume"] > 0).all()


def test_generator_rejects_invalid_dimensions():
    with pytest.raises(ValueError):
        generate_market([], sessions=100)
    with pytest.raises(ValueError):
        generate_market(["A"], sessions=1)


def test_stress_scenarios_cover_distinct_market_conditions():
    assert {s.name for s in SCENARIOS} >= {"bull", "bear", "sideways", "high_vol", "crash_risk"}
    assert SCENARIOS[1].annual_drift < 0
    assert SCENARIOS[-1].crash_probability > 0
