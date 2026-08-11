import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from backtest.engine import EventDrivenBacktester

def main():
    logger.info("Starting Walk-Forward Validation Backtest")
    
    # Load all available symbols from data/raw/ohlc
    ohlc_dir = Path(__file__).resolve().parent.parent / "data" / "raw" / "ohlc"
    symbols = []
    for f in ohlc_dir.glob("*.csv"):
        if f.stem not in ["INDEX_NIFTY50", "INDEX_NIFTY500", "INDEX_INDIAVIX", "^CRSLDX"]:
            symbols.append(f.stem)
            
    logger.info(f"Loaded {len(symbols)} stocks for full universe backtest.")
    
    # Configure backtester
    logger.info(f"Loaded {len(symbols)} stocks for full universe backtest.")
    
    tester = EventDrivenBacktester(
        start_date="2022-01-01",
        end_date="2024-01-01",
        symbols=symbols,
        use_computed_universe=False, # We are already passing all symbols directly
        use_regime_filter=True,      # Our new regime gating
        use_multi_tier=True,         # Our new multi-tier profit taking
        use_cross_sectional=False,
        use_fcs_gate=False,
        use_cash_regime=True         # Our new cash regime blocking
    )
    
    try:
        result = tester.run()
    
        print("\n" + "="*50)
        print("BACKTEST VALIDATION COMPLETED")
        print("="*50)
        print(f"Start Date : {result.start_date}")
        print(f"End Date   : {result.end_date}")
        print(f"Initial Cap: {result.initial_cash}")
        print(f"Final Val  : {result.daily_equity_curve[-1]['total_value'] if result.daily_equity_curve else 0:.2f}")
        print(f"Total Trades: {len(result.trade_history)}")
        print("\nMETRICS:")
        for k, v in result.final_metrics.items():
            print(f"  {k}: {v:.2f}")
            
    except Exception as e:
        logger.exception("Backtest failed!")

if __name__ == "__main__":
    main()
