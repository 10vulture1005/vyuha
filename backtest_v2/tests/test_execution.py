import pytest
from backtest_v2.execution.entry import EntryManager

def test_gap_through_stop_skips_entry():
    """
    Verify that if the next day's open is <= SL_0, the entry is skipped entirely.
    """
    manager = EntryManager()
    
    # Signal parameters
    signal_close = 100.0
    signal_low = 98.0
    atr = 5.0
    
    # Let's say sl_0 is calculated beforehand
    # SL_0 = max(98 - 0.2*5, P_entry - 2.0*5)
    # The entry manager function requires next_open and pre-calculated sl_0
    
    # Suppose entry at open (if valid) would be 99.0
    # But SL_0 is 97.0 based on signal_low
    sl_0 = 97.0
    
    # Case 1: Gap down through stop
    next_open = 95.0
    bar = {'High': 96, 'Low': 94, 'Close': 95}
    
    fill = manager.get_fill_price(signal_price=signal_close, next_open=next_open, bar=bar, sl_0=sl_0)
    
    # Fill should be None because next_open (95) <= sl_0 (97)
    assert fill is None
    
    # Case 2: Normal open above stop
    next_open = 101.0
    bar = {'High': 102, 'Low': 100, 'Close': 101}
    fill = manager.get_fill_price(signal_price=signal_close, next_open=next_open, bar=bar, sl_0=sl_0)
    
    # Fill should be valid
    assert fill is not None
    assert fill >= 101.0  # open + slippage

def test_cooldown_tracking():
    manager = EntryManager()
    manager.cooldown_days = 10
    
    manager.record_stop_out("AAPL", "2023-01-01")
    assert manager.is_in_cooldown("AAPL")
    
    # Reduce for 10 days
    for _ in range(9):
        manager.reduce_cooldowns()
        assert manager.is_in_cooldown("AAPL")
        
    manager.reduce_cooldowns() # 10th day
    assert not manager.is_in_cooldown("AAPL")
