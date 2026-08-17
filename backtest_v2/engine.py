import pandas as pd
from typing import Dict, List, Optional
from datetime import date
from loguru import logger

from backtest_v2.config import breakout_v2_config, BreakoutV2Config
from backtest_v2.signals.raw_scores import RawScoreCalculator
from backtest_v2.signals.composite import CompositeScorer
from backtest_v2.execution.entry import EntryManager
from backtest_v2.execution.sizing import PositionSizer
from backtest_v2.exits.chandelier import ChandelierExit, PositionState
from backtest_v2.risk.portfolio import PortfolioRiskManager
from backtest_v2.costs.tax_and_fees import TransactionCostModel
from backtest_v2.models import V2PortfolioHolding, V2TradeLog, V2DailyMetrics
from config.settings import settings

class BacktestEngineV2:
    """
    Main orchestration engine for VYUHA Breakout v2.
    Coordinates signals, execution, exits, and portfolio risk.
    """
    
    def __init__(self, config: Optional[BreakoutV2Config] = None):
        self.config = config or breakout_v2_config
        
        # Initialize modules
        self.raw_scorer = RawScoreCalculator(self.config)
        self.composite_scorer = CompositeScorer(self.config)
        self.entry_manager = EntryManager(self.config)
        self.exit_engine = ChandelierExit(self.config)
        self.risk_manager = PortfolioRiskManager(self.config)
        self.cost_model = TransactionCostModel(self.config)
        self.position_sizer = PositionSizer(self.config)
        
        # State
        self.current_date: Optional[date] = None
        self.equity = float(settings.INITIAL_CAPITAL)
        self.cash = self.equity
        
        # Tracking
        self.active_positions: Dict[str, PositionState] = {}
        self.trade_log: List[dict] = []
        self.daily_metrics: List[dict] = []
        
    def generate_signals(self, ohlc_data: Dict[str, pd.DataFrame]) -> Dict[str, dict]:
        """Generate entry signals for the current date."""
        # This is a simplified signal generation loop
        # In a real vectorized setup, this would be computed over the entire matrix first
        signals = {}
        
        for symbol, df in ohlc_data.items():
            if len(df) < self.config.universe.warmup_days:
                continue
                
            raw_scores = self.raw_scorer.compute_all_raw(df)
            percentiles = self.raw_scorer.compute_percentile_components(raw_scores)
            
            S_raw, S_tech = self.composite_scorer.compute_all(percentiles)
            
            # Use last valid value
            if S_tech.empty or pd.isna(S_tech.iloc[-1]):
                continue
                
            current_s_tech = S_tech.iloc[-1]
            
            if current_s_tech > self.config.signals.s_tech_threshold:
                # Check cooldown
                if self.entry_manager.is_in_cooldown(symbol):
                    continue
                    
                signals[symbol] = {
                    's_tech': current_s_tech,
                    'close': df['Close'].iloc[-1],
                    'low': df['Low'].iloc[-1],
                    'atr': self.raw_scorer.compute_atr(df).iloc[-1]
                }
                
        return signals
        
    def process_exits(self, current_date_data: Dict[str, dict]):
        """Check all active positions for exit conditions."""
        exited = []
        
        for symbol, state in list(self.active_positions.items()):
            if symbol not in current_date_data:
                continue
                
            bar = current_date_data[symbol]
            
            # Update trailing stop based on yesterday's close/atr? Or today's?
            # Normally we check exit first, then update stop for tomorrow
            
            should_exit, reason = self.exit_engine.check_exit(
                state, bar['High'], bar['Low'], bar['Close']
            )
            
            if should_exit:
                # Execute exit
                exit_price = state.current_stop if self.config.exits.exit_trigger == "intraday_low" else bar['Close']
                
                # Account for gap-down through the stop
                if bar['Open'] < exit_price:
                    exit_price = bar['Open']
                
                # Check slippage/costs
                costs = self.cost_model.calculate_sell_costs(exit_price * state.shares)
                net_exit_value = (exit_price * state.shares) - costs.total
                
                self.cash += net_exit_value
                
                # Record
                exit_record = self.exit_engine.close_position(
                    state, exit_price, self.current_date.isoformat(), reason
                )
                self.trade_log.append(exit_record)
                
                # Start cooldown
                self.entry_manager.record_stop_out(symbol, self.current_date.isoformat())
                
                self.risk_manager.remove_position(symbol)
                exited.append(symbol)
            else:
                # Update stop for tomorrow
                self.exit_engine.update_trailing_stop(
                    state, bar['Close'], bar['High'], bar['Low'], bar.get('ATR', state.atr_at_entry)
                )
                
        for symbol in exited:
            del self.active_positions[symbol]
            
    def process_entries(self, signals: Dict[str, dict], current_date_data: Dict[str, dict]):
        """Process new signals and attempt entry."""
        # Sort signals by S_tech descending
        sorted_signals = sorted(signals.items(), key=lambda x: x[1]['s_tech'], reverse=True)
        
        for symbol, sig in sorted_signals:
            if symbol not in current_date_data:
                continue
                
            bar = current_date_data[symbol]
            sl_0 = self.entry_manager.compute_sl_0(sig['low'], sig['close'], sig['atr'])
            
            fill_price = self.entry_manager.get_fill_price(
                sig['close'], bar['Open'], bar, sl_0
            )
            
            if fill_price is None:
                continue
                
            # Check risk and size position
            risk_per_share = fill_price - sl_0
            if risk_per_share <= 0:
                continue
                
            # Sizing using PositionSizer
            shares = self.position_sizer.calculate_size(
                account_equity=self.equity,
                entry_price=fill_price,
                stop_price=sl_0,
                available_cash=self.cash
            )
            
            if shares == 0:
                continue
                
            notional = self.position_sizer.calculate_notional(shares, fill_price)
            costs = self.cost_model.calculate_buy_costs(notional)
            total_cost = notional + costs.total
            
            if total_cost > self.cash:
                # Strictly afford what we can
                shares = int(self.cash / (fill_price * 1.01))
                if shares == 0:
                    continue
                notional = self.position_sizer.calculate_notional(shares, fill_price)
                total_cost = notional + self.cost_model.calculate_buy_costs(notional).total
                
            # Aggregate risk check
            position_risk = self.position_sizer.calculate_risk_amount(shares, fill_price, sl_0)
            if self.risk_manager.would_breach_aggregate_risk(self.equity, position_risk):
                logger.debug(f"Aggregate risk breached for {symbol}")
                continue
                
            # Sector and Max Positions check
            if not self.risk_manager.add_position(symbol, fill_price, sl_0, shares):
                continue
                
            # Execute Entry
            self.cash -= total_cost
            
            state = self.exit_engine.initialize_position(
                symbol, fill_price, sig['low'], sig['atr'], shares, self.current_date.isoformat()
            )
            self.active_positions[symbol] = state
            
            self.trade_log.append({
                'symbol': symbol,
                'action': 'BUY',
                'date': self.current_date.isoformat(),
                'price': fill_price,
                'shares': shares,
                'sl_0': sl_0
            })
            
    def run_daily_loop(self, ohlc_data: Dict[str, pd.DataFrame]):
        """Execute full backtest over provided data."""
        # Find all unique trading dates across all symbols
        all_dates = pd.DatetimeIndex([])
        for df in ohlc_data.values():
            all_dates = all_dates.union(df.index)
        all_dates = all_dates.sort_values()
        
        logger.info(f"Starting backtest from {all_dates.min().date()} to {all_dates.max().date()}")
        
        # Pre-compute signals for speed (normally we would do this day by day in live trading)
        # But for backtesting, vectorizing the signal generation is much faster
        signals_cache = {}
        logger.info("Pre-computing signals...")
        for symbol, df in ohlc_data.items():
            if len(df) < self.config.universe.warmup_days:
                continue
                
            raw_scores = self.raw_scorer.compute_all_raw(df)
            percentiles = self.raw_scorer.compute_percentile_components(raw_scores)
            S_raw, S_tech = self.composite_scorer.compute_all(percentiles)
            atr = self.raw_scorer.compute_atr(df)
            
            signals_cache[symbol] = pd.DataFrame({
                's_tech': S_tech,
                'close': df['Close'],
                'low': df['Low'],
                'atr': atr,
                'open': df['Open'],
                'high': df['High']
            })
            
        logger.info(f"Signals computed for {len(signals_cache)} symbols. Starting daily loop.")
        
        current_month = None
        
        for current_dt in all_dates:
            self.current_date = current_dt.date()
            
            # SIP Logic
            if current_month is not None and self.current_date.month != current_month:
                self.cash += settings.MONTHLY_SIP_AMOUNT
                logger.debug(f"SIP Injected: {settings.MONTHLY_SIP_AMOUNT}. New Cash: {self.cash:.2f}")
            current_month = self.current_date.month
            
            # Prepare current day's data slice
            current_date_data = {}
            daily_signals = {}
            
            for symbol, sig_df in signals_cache.items():
                if current_dt in sig_df.index:
                    row = sig_df.loc[current_dt]
                    if pd.isna(row['close']):
                        continue
                        
                    current_date_data[symbol] = {
                        'Open': row['open'],
                        'High': row['high'],
                        'Low': row['low'],
                        'Close': row['close'],
                        'ATR': row['atr']
                    }
                    
                    if row['s_tech'] > self.config.signals.s_tech_threshold:
                        if not self.entry_manager.is_in_cooldown(symbol):
                            daily_signals[symbol] = {
                                's_tech': row['s_tech'],
                                'close': row['close'],
                                'low': row['low'],
                                'atr': row['atr']
                            }
            
            # 1. Process Exits (using today's prices)
            self.process_exits(current_date_data)
            
            # 2. Process Entries (using today's signals, executing at tomorrow's open logic)
            # In a true vectorized backtester, entry execution happens tomorrow. 
            # Our `process_entries` handles it by expecting `next_open`.
            # To simulate correctly, we need the NEXT day's open.
            # So let's look ahead 1 day for `next_open`.
            next_idx = all_dates.get_loc(current_dt) + 1
            if next_idx < len(all_dates) and daily_signals:
                next_dt = all_dates[next_idx]
                next_date_data = {}
                for symbol in daily_signals.keys():
                    if symbol in signals_cache and next_dt in signals_cache[symbol].index:
                        next_date_data[symbol] = {
                            'Open': signals_cache[symbol].loc[next_dt, 'open'],
                            'High': signals_cache[symbol].loc[next_dt, 'high'],
                            'Low': signals_cache[symbol].loc[next_dt, 'low'],
                            'Close': signals_cache[symbol].loc[next_dt, 'close']
                        }
                self.process_entries(daily_signals, next_date_data)
            
            # 3. Update Cooldowns
            self.entry_manager.reduce_cooldowns()
            
            # 4. Record Daily Metrics
            active_equity = sum(
                pos.shares * current_date_data[sym]['Close'] 
                for sym, pos in self.active_positions.items() 
                if sym in current_date_data
            )
            total_equity = self.cash + active_equity
            self.equity = total_equity
            
            self.daily_metrics.append({
                'date': self.current_date,
                'equity': total_equity,
                'cash': self.cash,
                'invested': active_equity,
                'open_positions': len(self.active_positions)
            })
            
        logger.info(f"Backtest completed. Final equity: {total_equity:.2f}")
        return {
            'trade_log': pd.DataFrame(self.trade_log),
            'daily_metrics': pd.DataFrame(self.daily_metrics).set_index('date')
        }
