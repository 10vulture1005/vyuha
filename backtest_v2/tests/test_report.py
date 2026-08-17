import pytest
import pandas as pd
from datetime import date
from backtest_v2.report import calculate_cagr, calculate_xirr, generate_eoy_returns

def test_cagr():
    # 100 to 200 in 1 year = 100% return
    assert calculate_cagr(100.0, 200.0, 1.0) == pytest.approx(1.0)
    # 100 to 121 in 2 years = 10% return
    assert calculate_cagr(100.0, 121.0, 2.0) == pytest.approx(0.1)

def test_xirr():
    # Invest 100 on Jan 1
    # End with 110 on Dec 31 (1 year)
    # XIRR should be ~10%
    cashflows = [
        (date(2023, 1, 1), -100.0),
        (date(2023, 12, 31), 110.0)
    ]
    xirr = calculate_xirr(cashflows)
    assert round(xirr, 2) == 0.10

def test_eoy_returns():
    dates = pd.date_range('2021-12-31', periods=400) # Spans 2021, 2022, 2023
    equity = pd.Series(100.0, index=dates)
    
    # 2021 last day is 100
    # Make 2022 end at 120 (20% return for 2022)
    equity[dates.year == 2022] = 120.0 
    
    # Make 2023 end at 180 (50% return on 120 for 2023)
    equity[dates.year == 2023] = 180.0
    
    eoy = generate_eoy_returns(equity)
    
    # 2021 should be 0 return (only 1 day)
    assert eoy.loc[2021, 'Return'] == 0.0
    
    # 2022 should be 20%
    assert round(eoy.loc[2022, 'Return'], 2) == 0.20
    
    # 2023 should be 50%
    assert round(eoy.loc[2023, 'Return'], 2) == 0.50
