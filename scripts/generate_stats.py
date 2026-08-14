import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
import pandas as pd
import numpy as np
import quantstats as qs

from backtest.engine import EventDrivenBacktester
from config.settings import settings

def main():
    logger.info("Generating complete stats as stats.html (Trailing Stop Only exit mode)")
    
    ohlc_dir = Path(__file__).resolve().parent.parent / "data" / "raw" / "ohlc"
    symbols = []
    for f in ohlc_dir.glob("*.csv"):
        if f.stem not in ["INDEX_NIFTY50", "INDEX_NIFTY500", "INDEX_INDIAVIX", "^CRSLDX"]:
            symbols.append(f.stem)
            
    tester = EventDrivenBacktester(
        start_date="2021-01-01",
        end_date="2026-01-01",
        symbols=symbols,
        use_computed_universe=True, 
        use_regime_filter=True,      
        use_multi_tier=True,         
        use_cross_sectional=False,
        use_fcs_gate=True,
        use_cash_regime=True,
        exit_mode="trailing_stop_only"
    )
    result = tester.run()
    
    if result.daily_equity_curve:
        dates = pd.to_datetime([d["date"] for d in result.daily_equity_curve])
        equity = [d["total_value"] for d in result.daily_equity_curve]
        
        eq_series = pd.Series(equity, index=dates)
        returns = eq_series.pct_change().dropna()
        
        out_path = Path(__file__).resolve().parent.parent / "stats.html"
        qs.reports.html(returns, benchmark=None, output=str(out_path), title="Vyuha Engine - Performance Report")
        logger.info(f"Successfully generated {out_path}")

if __name__ == "__main__":
    main()
