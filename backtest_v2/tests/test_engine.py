import pytest
from backtest_v2.engine import BacktestEngineV2
from backtest_v2.config import breakout_v2_config

def test_engine_initialization():
    """Test that the engine initializes and connects to all managers."""
    engine = BacktestEngineV2(breakout_v2_config)
    assert engine.raw_scorer is not None
    assert engine.composite_scorer is not None
    assert engine.entry_manager is not None
    assert engine.exit_engine is not None
    assert engine.risk_manager is not None
    assert engine.cost_model is not None
    assert engine.cash == 100000.0
