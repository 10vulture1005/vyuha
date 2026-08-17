import pandas as pd
from datetime import date
from backtest_v2.engine import BacktestEngineV2
from backtest_v2.config import breakout_v2_config
from backtest_v2.data.mock_generator import MockDataGenerator

generator = MockDataGenerator(seed=42)
symbols = [f"STOCK{i:04d}" for i in range(50)]
ohlc_data = generator.generate_ohlc(symbols, date(2020,1,1), date(2024,1,1))

engine = BacktestEngineV2(breakout_v2_config)
results = engine.run_daily_loop(ohlc_data)
trades = results['trade_log']
metrics = results['daily_metrics']

if not trades.empty:
    exits = trades[trades['action'] != 'BUY'].copy()
    if not exits.empty:
        # P_entry - SL_0 isn't in exit log directly, but we have entry_price and exit_price
        exits['loss_pct'] = (exits['price'] / exits['entry_price']) - 1
        exits['notional'] = exits['shares'] * exits['entry_price']
        print("\nTop 5 worst loss percentages:")
        print(exits[['symbol', 'entry_date', 'date', 'loss_pct', 'notional']].sort_values('loss_pct').head())
        
        print("\nTop 5 largest notional allocations:")
        print(exits[['symbol', 'entry_date', 'date', 'loss_pct', 'notional']].sort_values('notional', ascending=False).head())

