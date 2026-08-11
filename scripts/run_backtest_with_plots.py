import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np

from backtest.engine import EventDrivenBacktester
from config.settings import settings

def main():
    logger.info("Starting Walk-Forward Validation Backtest with Visualization")
    
    ohlc_dir = Path(__file__).resolve().parent.parent / "data" / "raw" / "ohlc"
    symbols = []
    for f in ohlc_dir.glob("*.csv"):
        if f.stem not in ["INDEX_NIFTY50", "INDEX_NIFTY500", "INDEX_INDIAVIX", "^CRSLDX"]:
            symbols.append(f.stem)
            
    logger.info(f"Loaded {len(symbols)} stocks for full universe backtest.")
    
    tester = EventDrivenBacktester(
        start_date="2021-01-01",
        end_date="2026-01-01",
        symbols=symbols,
        use_computed_universe=False, 
        use_regime_filter=True,      
        use_multi_tier=True,         
        use_cross_sectional=False,
        use_fcs_gate=False,
        use_cash_regime=True         
    )
    
    try:
        result = tester.run()
    
        print("\n" + "="*50)
        print("BACKTEST VALIDATION COMPLETED (Honest Setup)")
        print("="*50)
        print(f"Start Date : {result.start_date}")
        print(f"End Date   : {result.end_date}")
        print(f"Initial Cap: {settings.INITIAL_CAPITAL}")
        print(f"Monthly SIP: {settings.MONTHLY_SIP_AMOUNT}")
        print(f"Final Val  : {result.daily_equity_curve[-1]['total_value'] if result.daily_equity_curve else 0:.2f}")
        print(f"Total Trades: {len(result.trade_history)}")
        print("\nMETRICS:")
        for k, v in result.final_metrics.items():
            print(f"  {k}: {v:.2f}")
            
        # Plotting
        if result.daily_equity_curve:
            dates = pd.to_datetime([d["date"] for d in result.daily_equity_curve])
            equity = [d["total_value"] for d in result.daily_equity_curve]
            
            # Reconstruct invested capital baseline
            invested = []
            current_invested = settings.INITIAL_CAPITAL
            current_month = dates[0].month
            for d in dates:
                if d.month != current_month:
                    current_invested += settings.MONTHLY_SIP_AMOUNT
                    current_month = d.month
                invested.append(current_invested)
                
            sns.set_theme(style="darkgrid")
            plt.figure(figsize=(12, 6))
            plt.plot(dates, equity, label="Vyuha System Equity", color="#00ffcc", linewidth=2)
            plt.plot(dates, invested, label="Total Invested Capital (SIP Adjusted)", color="#ff3366", linestyle="--", linewidth=1.5)
            
            plt.title("Vyuha Systematic Swing Engine - Backtest Equity Curve (Net of all Friction)", fontsize=14, pad=15)
            plt.xlabel("Date", fontsize=12)
            plt.ylabel("Portfolio Value (INR)", fontsize=12)
            plt.fill_between(dates, invested, equity, where=np.array(equity) >= np.array(invested), facecolor="#00ffcc", alpha=0.1)
            plt.fill_between(dates, invested, equity, where=np.array(equity) < np.array(invested), facecolor="#ff3366", alpha=0.1)
            
            plt.legend(loc="upper left")
            plt.tight_layout()
            
            # Save plot
            artifacts_dir = Path("/home/vulture/.gemini/antigravity/brain/7b485a7d-9bb3-42cb-b12a-3ca8b6655757")
            out_path = artifacts_dir / "backtest_results.png"
            plt.savefig(out_path, dpi=300)
            logger.info(f"Saved performance graph to {out_path}")
            
    except Exception as e:
        logger.exception("Backtest failed!")

if __name__ == "__main__":
    main()
