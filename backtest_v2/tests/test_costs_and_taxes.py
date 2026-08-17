import pytest
from datetime import date
from backtest_v2.costs.tax_and_fees import TransactionCostModel

def test_stcg_20_percent():
    """
    Test that STCG is applied at 20% (Budget 2024 rule) for < 365 days.
    """
    model = TransactionCostModel()
    model.stcg_rate = 0.20
    
    # Holding for 10 days
    entry_date = date(2023, 1, 1)
    exit_date = date(2023, 1, 11)
    
    res = model.calculate_stcg_tax(realized_pnl=1000.0, entry_date=entry_date, exit_date=exit_date)
    
    assert res.is_stcg
    assert res.holding_days == 10
    assert res.stcg_tax == 200.0  # 20% of 1000

def test_ltcg_rule():
    """
    Test LTCG > 365 days.
    """
    model = TransactionCostModel()
    
    # Holding for 400 days
    entry_date = date(2022, 1, 1)
    exit_date = date(2023, 2, 5)
    
    res = model.calculate_stcg_tax(realized_pnl=200000.0, entry_date=entry_date, exit_date=exit_date)
    
    assert not res.is_stcg
    assert res.holding_days == 400
    
    # LTCG on gains > 1.25L @ 12.5%
    # Taxable = 200,000 - 125,000 = 75,000
    # Tax = 75,000 * 0.125 = 9375.0
    assert res.stcg_tax == 9375.0
