import argparse
from datetime import date
from loguru import logger
import pandas as pd

from backtest_v2.engine import BacktestEngineV2
from backtest_v2.config import breakout_v2_config
from backtest_v2.data.mock_generator import MockDataGenerator
from backtest_v2.report import calculate_cagr, calculate_xirr, generate_eoy_returns
from config.settings import settings

def main():
    parser = argparse.ArgumentParser(description="Run VYUHA V2 Breakout Backtest")
    parser.add_argument("--start", default="2020-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2024-01-01", help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", type=int, default=50, help="Number of mock symbols to generate")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    logger.info(f"Generating mock data for {args.symbols} symbols from {start_date} to {end_date}...")
    generator = MockDataGenerator(seed=42)
    # Generate OHLC
    symbols = [f"STOCK{i:04d}" for i in range(args.symbols)]
    ohlc_data = generator.generate_ohlc(symbols, start_date, end_date)
    
    logger.info("Initializing V2 Engine...")
    engine = BacktestEngineV2(breakout_v2_config)
    
    logger.info("Running backtest...")
    results = engine.run_daily_loop(ohlc_data)
    
    trade_log = results['trade_log']
    metrics = results['daily_metrics']
    
    print("\n" + "="*50)
    print("BACKTEST RESULTS (V2 Breakout)")
    print("="*50)
    
    if not metrics.empty:
        start_equity = metrics.iloc[0]['equity']
        end_equity = metrics.iloc[-1]['equity']
        
        # Build Cashflows for XIRR
        cashflows = [(start_date, -start_equity)]
        
        # Add monthly SIPs
        current = start_date
        current_month = start_date.month
        for dt, row in metrics.iterrows():
            if hasattr(dt, 'date'):
                dt_val = dt.date()
            else:
                dt_val = dt
                
            if dt_val.month != current_month:
                cashflows.append((dt_val, -float(settings.MONTHLY_SIP_AMOUNT)))
                current_month = dt_val.month
                
        # Final value
        cashflows.append((end_date, float(end_equity)))
        
        xirr = calculate_xirr(cashflows)
        
        print(f"Start Date:    {start_date}")
        print(f"End Date:      {end_date}")
        print(f"Start Equity:  {start_equity:,.2f}")
        print(f"End Equity:    {end_equity:,.2f}")
        print(f"Total Invested:{sum(-cf[1] for cf in cashflows[:-1]):,.2f}")
        print(f"XIRR (Annual): {xirr:.2%}")
        
        print("\nEnd of Year Returns:")
        eoy = generate_eoy_returns(metrics['equity'], cashflows=cashflows)
        for year, row in eoy.iterrows():
            print(f"  {year}: {row['Return']:.2%}")
            
    print("\nTrade Log Summary:")
    if not trade_log.empty:
        buys = len(trade_log[trade_log['action'] == 'BUY']) if 'action' in trade_log.columns else 0
        sells = len(trade_log[trade_log['action'] != 'BUY']) if 'action' in trade_log.columns else len(trade_log)
        print(f"Total Trades Entered: {buys}")
        print(f"Total Trades Exited:  {sells}")
    else:
        print("No trades executed.")
        
    print("="*50)

if __name__ == "__main__":
    main()
