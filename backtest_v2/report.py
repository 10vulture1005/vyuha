import pandas as pd
import numpy as np
from datetime import date
from typing import List, Dict, Tuple

def calculate_cagr(start_equity: float, end_equity: float, years: float) -> float:
    """Calculate Compound Annual Growth Rate."""
    if start_equity <= 0 or years <= 0:
        return 0.0
    return float((end_equity / start_equity) ** (1 / years) - 1)

def calculate_xirr(cashflows: List[Tuple[date, float]]) -> float:
    """
    Calculate XIRR using scipy optimize.
    cashflows: list of (date, amount) tuples.
    Positive amount = cash inflow (withdrawal / final equity)
    Negative amount = cash outflow (investment / SIP)
    """
    from scipy.optimize import brentq
    
    if not cashflows:
        return 0.0
        
    cashflows.sort(key=lambda x: x[0])
    
    def xnpv(rate):
        if rate <= -1.0:
            return float('inf')
        t0 = cashflows[0][0]
        return sum([cf / (1 + rate)**((d - t0).days / 365.25) for d, cf in cashflows])
        
    try:
        # Use brentq with bounded search to handle massive drawdowns safely
        return brentq(xnpv, -0.99999, 100.0)
    except (RuntimeError, ValueError):
        # Fallback to 0 if no root found in bounds
        return 0.0

def generate_eoy_returns(daily_equity: pd.Series, cashflows: List[Tuple[date, float]] = None) -> pd.DataFrame:
    """
    Generate End-of-Year returns table correctly using XIRR (Money-Weighted Return).
    Handles partial years properly and eliminates SIP distortion.
    """
    df = daily_equity.to_frame('equity')
    df.index = pd.to_datetime(df.index)
    df['year'] = df.index.year
    
    yearly_returns = []
    
    # Group cashflows by year if provided
    cf_by_year = {}
    if cashflows:
        for d, amt in cashflows:
            y = d.year
            if y not in cf_by_year:
                cf_by_year[y] = []
            # Only include actual external cashflows (not the start/end equity markers we generated for the whole run)
            # A true SIP cashflow is usually negative.
            # We'll filter out the synthetic start/end markers in the loop.
            cf_by_year[y].append((d, amt))
    
    for year in df['year'].unique():
        year_data = df[df['year'] == year]
        
        first_day = year_data.index.min()
        last_day = year_data.index.max()
        
        # Determine starting equity for the year
        prev_year_data = df[df.index < first_day]
        if not prev_year_data.empty:
            start_equity = prev_year_data.iloc[-1]['equity']
        else:
            start_equity = year_data.iloc[0]['equity']
            
        end_equity = year_data.iloc[-1]['equity']
        
        if cashflows:
            # Build year-specific cashflows
            year_cf = [(first_day.date(), -start_equity)]
            for d, amt in cf_by_year.get(year, []):
                # Ignore the global start/end markers from the full backtest
                if d == first_day.date() and amt == -start_equity: continue
                if d == last_day.date() and amt == end_equity: continue
                # Add real intra-year cashflows (like SIP)
                if amt < 0 and d != first_day.date(): # SIPs are negative
                    year_cf.append((d, amt))
            year_cf.append((last_day.date(), end_equity))
            ret = calculate_xirr(year_cf)
        else:
            # Simple Time-Weighted Return (fallback if no cashflows)
            ret = (end_equity / start_equity) - 1
            
        yearly_returns.append({'Year': year, 'Return': ret})
        
    return pd.DataFrame(yearly_returns).set_index('Year')
