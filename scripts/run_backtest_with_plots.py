import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np

try:
    import quantstats as qs
except ImportError:
    qs = None

from backtest.engine import EventDrivenBacktester
from config.settings import settings

import argparse

def main():
    parser = argparse.ArgumentParser(description="Vyuha Engine Backtest")
    parser.add_argument("--no-fundamentals", action="store_true", help="Run without fundamental FCS gate")
    parser.add_argument("--compare", action="store_true", help="Run both with and without fundamentals and compare")
    parser.add_argument("--exit-mode", type=str, choices=["full", "trailing_stop_only"], default="full", help="Exit strategy mode")
    parser.add_argument("--compare-exits", action="store_true", help="Run backtest with 'full' and 'trailing_stop_only' exit modes and compare")
    args = parser.parse_args()

    logger.info("Starting Walk-Forward Validation Backtest with Visualization")
    
    ohlc_dir = Path(__file__).resolve().parent.parent / "data" / "raw" / "ohlc"
    symbols = []
    for f in ohlc_dir.glob("*.csv"):
        if f.stem not in ["INDEX_NIFTY50", "INDEX_NIFTY500", "INDEX_INDIAVIX", "^CRSLDX"]:
            symbols.append(f.stem)
            
    logger.info(f"Loaded {len(symbols)} stocks for full universe backtest.")
    
    def run_backtest(use_fundamentals: bool, exit_mode: str, label: str):
        tester = EventDrivenBacktester(
            start_date="2021-01-01",
            end_date="2026-01-01",
            symbols=symbols,
            use_computed_universe=True, 
            use_regime_filter=True,      
            use_multi_tier=True,         
            use_cross_sectional=False,
            use_fcs_gate=use_fundamentals,
            use_cash_regime=True,
            exit_mode=exit_mode
        )
        result = tester.run()
        print("\n" + "="*50)
        print(f"BACKTEST VALIDATION COMPLETED ({label})")
        print("="*50)
        print(f"Total Trades: {len(result.trade_history)}")
        print("\nMETRICS:")
        for k, v in result.final_metrics.items():
            print(f"  {k}: {v:.2f}")
        return result

    try:
        if args.compare_exits:
            use_fund = not args.no_fundamentals
            res_full = run_backtest(use_fund, "full", "Full Exits")
            res_ts = run_backtest(use_fund, "trailing_stop_only", "Trailing Stop Only")
            
            # Plotting both
            dates = pd.to_datetime([d["date"] for d in res_full.daily_equity_curve])
            eq_full = [d["total_value"] for d in res_full.daily_equity_curve]
            eq_ts = [d["total_value"] for d in res_ts.daily_equity_curve]
            
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
            plt.plot(dates, eq_full, label="Full Exit Logic", color="#00ffcc", linewidth=2)
            plt.plot(dates, eq_ts, label="Trailing Stop Only", color="#ff9900", linewidth=2)
            plt.plot(dates, invested, label="Total Invested Capital (SIP Adjusted)", color="#ff3366", linestyle="--", linewidth=1.5)
            
            plt.title("Vyuha Engine - Exit Logic Comparison", fontsize=14, pad=15)
            plt.xlabel("Date", fontsize=12)
            plt.ylabel("Portfolio Value (INR)", fontsize=12)
            plt.legend(loc="upper left")
            plt.tight_layout()
            
            out_path = Path("/home/vulture/.gemini/antigravity/brain/06c22121-77ae-468c-b5e1-e35292e9ad6f/scratch/compare_exits_plot.png")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(out_path, dpi=300)
            logger.info(f"Saved exit comparison graph to {out_path}")
            
        elif args.compare:
            res_fund = run_backtest(True, args.exit_mode, "With Fundamentals")
            res_tech = run_backtest(False, args.exit_mode, "Without Fundamentals")
            
            # Plotting both
            dates = pd.to_datetime([d["date"] for d in res_fund.daily_equity_curve])
            eq_fund = [d["total_value"] for d in res_fund.daily_equity_curve]
            eq_tech = [d["total_value"] for d in res_tech.daily_equity_curve]
            
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
            plt.plot(dates, eq_fund, label="With Fundamentals (FCS Gate)", color="#00ffcc", linewidth=2)
            plt.plot(dates, eq_tech, label="Without Fundamentals (Tech Only)", color="#ff9900", linewidth=2)
            plt.plot(dates, invested, label="Total Invested Capital (SIP Adjusted)", color="#ff3366", linestyle="--", linewidth=1.5)
            
            plt.title("Vyuha Engine - Fundamentals vs. Technical Only Comparison", fontsize=14, pad=15)
            plt.xlabel("Date", fontsize=12)
            plt.ylabel("Portfolio Value (INR)", fontsize=12)
            plt.legend(loc="upper left")
            plt.tight_layout()
            
            out_path = Path("/home/vulture/.gemini/antigravity/brain/06c22121-77ae-468c-b5e1-e35292e9ad6f/scratch/comparison_plot.png")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(out_path, dpi=300)
            logger.info(f"Saved comparison graph to {out_path}")
            
        else:
            use_fund = not args.no_fundamentals
            res = run_backtest(use_fund, args.exit_mode, "With Fundamentals" if use_fund else "Without Fundamentals")
            
            if res.daily_equity_curve:
                dates = pd.to_datetime([d["date"] for d in res.daily_equity_curve])
                equity = [d["total_value"] for d in res.daily_equity_curve]
                
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
                
                plt.title(f"Vyuha Systematic Swing Engine - {'With' if use_fund else 'Without'} Fundamentals", fontsize=14, pad=15)
                plt.xlabel("Date", fontsize=12)
                plt.ylabel("Portfolio Value (INR)", fontsize=12)
                plt.fill_between(dates, invested, equity, where=np.array(equity) >= np.array(invested), facecolor="#00ffcc", alpha=0.1)
                plt.fill_between(dates, invested, equity, where=np.array(equity) < np.array(invested), facecolor="#ff3366", alpha=0.1)
                
                plt.legend(loc="upper left")
                plt.tight_layout()
                
                out_path = Path("/home/vulture/.gemini/antigravity/brain/06c22121-77ae-468c-b5e1-e35292e9ad6f/scratch/single_plot.png")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                plt.savefig(out_path, dpi=300)
                logger.info(f"Saved performance graph to {out_path}")
                
                # Generate QuantStats HTML Tearsheet
                if qs is not None:
                    try:
                        eq_series = pd.Series(equity, index=dates)
                        returns = eq_series.pct_change().dropna()
                        stats_out_path = out_path.parent / "stats_report.html"
                        qs.reports.html(returns, benchmark=None, output=str(stats_out_path), title="Vyuha Engine - Performance Report")
                        logger.info(f"Saved detailed QuantStats report to {stats_out_path}")
                    except Exception as e:
                        logger.warning(f"Failed to generate QuantStats report: {e}")
            
    except Exception as e:
        logger.exception("Backtest failed!")

if __name__ == "__main__":
    main()
