import pytest
from backtest_v2.exits.chandelier import ChandelierExit, PositionState

def test_chandelier_monotonic_ratchet():
    """
    Test that the chandelier stop never decreases (ratchets up only).
    """
    exit_engine = ChandelierExit()
    # Force some multipliers for testing
    exit_engine.fail_safe_mult = 2.0
    exit_engine.chandelier_mult = 3.0
    exit_engine.buffer_mult = 0.2
    
    # Initialize position
    state = exit_engine.initialize_position(
        symbol="TEST",
        entry_price=100.0,
        signal_low=98.0,
        atr=5.0,
        shares=10,
        entry_date="2023-01-01"
    )
    
    # SL_0 = max(98.0 - 0.2*5, 100 - 2.0*5) = max(97.0, 90.0) = 97.0
    assert state.initial_stop == 97.0
    assert state.current_stop == 97.0
    
    # Day 1: price goes up
    # highest_close becomes 105. Chandelier stop = 105 - 3*5 = 90. 
    # Max(97, 90) = 97
    new_stop, moved = exit_engine.update_trailing_stop(state, current_close=105.0, current_high=106.0, current_low=102.0, current_atr=5.0)
    assert new_stop == 97.0
    assert not moved
    
    # Day 2: price rockets up
    # highest_close = 120. Chandelier stop = 120 - 3*5 = 105.
    # Max(97, 105) = 105
    new_stop, moved = exit_engine.update_trailing_stop(state, current_close=120.0, current_high=125.0, current_low=115.0, current_atr=5.0)
    assert new_stop == 105.0
    assert moved
    
    # Day 3: price falls back
    # highest_close remains 120. Chandelier stop = 120 - 3*6 = 102.
    # Max(105, 102) = 105
    new_stop, moved = exit_engine.update_trailing_stop(state, current_close=115.0, current_high=118.0, current_low=110.0, current_atr=6.0)
    assert new_stop == 105.0
    assert not moved  # DID NOT DECREASE

def test_breakeven_floor():
    exit_engine = ChandelierExit()
    exit_engine.breakeven_floor_r = 1.0
    
    state = exit_engine.initialize_position(
        symbol="TEST",
        entry_price=100.0,
        signal_low=95.0,
        atr=5.0,
        shares=10,
        entry_date="2023-01-01"
    )
    
    # SL_0 = max(94.0, 90.0) = 94.0
    # Risk per share = 6.0
    # 1R = 106.0
    assert state.initial_stop == 94.0
    
    # Price hits 105 (not yet 1R)
    stop = exit_engine.get_active_stop(state, 105.0)
    assert not state.breakeven_triggered
    assert stop == 94.0
    
    # Price hits 106 (exactly 1R)
    stop = exit_engine.get_active_stop(state, 106.0)
    assert state.breakeven_triggered
    assert stop == 100.0  # entry_price
    
    # Price goes to 120, trailing stop might trigger to be > entry_price
    state.current_stop = 110.0
    stop = exit_engine.get_active_stop(state, 120.0)
    assert stop == 110.0 # trailing stop > entry_price
