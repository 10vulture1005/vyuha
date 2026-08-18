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
from backtest_v2.exits.optional.regime_tighten import RegimeTightening
from backtest_v2.risk.portfolio import PortfolioRiskManager
from backtest_v2.costs.tax_and_fees import TransactionCostModel
from backtest_v2.models import V2PortfolioHolding, V2TradeLog, V2DailyMetrics
from config.settings import settings

class BacktestEngineV2:
    """
    Main orchestration engine for VYUHA Breakout v2.
    Coordinates signals, execution, exits, and portfolio risk.
    
    v2.2 optimizations:
    - Adaptive trailing stop (tightens with profit)
    - Partial profit-taking at configurable R-multiple
    - Regime-aware stop tightening
    - Drawdown circuit breaker
    - Equity updated before entries for accurate sizing
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
        self.regime_tightening = RegimeTightening(self.config)
        
        # State
        self.current_date: Optional[date] = None
        self.equity = float(settings.INITIAL_CAPITAL)
        self.cash = self.equity
        self.peak_equity = self.equity  # For drawdown circuit breaker
        
        # Tracking
        self.active_positions: Dict[str, PositionState] = {}
        self.trade_log: List[dict] = []
        self.daily_metrics: List[dict] = []
        
        # Nifty data for regime checks (set externally or loaded)
        self.nifty_df: Optional[pd.DataFrame] = None
        
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
    
    def _check_drawdown_breaker(self) -> bool:
        """
        Drawdown circuit breaker: halt new entries when peak-to-trough
        drawdown exceeds threshold.
        
        Returns:
            True if entries should be halted.
        """
        self.peak_equity = max(self.peak_equity, self.equity)
        if self.peak_equity <= 0:
            return False
        
        current_dd = (self.peak_equity - self.equity) / self.peak_equity
        
        if current_dd > self.config.portfolio_risk.drawdown_halt_pct:
            logger.warning(
                f"Drawdown circuit breaker ACTIVE: {current_dd:.2%} > "
                f"{self.config.portfolio_risk.drawdown_halt_pct:.2%} — skipping new entries"
            )
            return True
        return False
    
    def _is_regime_bearish(self, current_dt: pd.Timestamp) -> bool:
        """Check if market regime is bearish (Nifty < SMA200)."""
        return self.regime_tightening.check_regime_flip(self.nifty_df, current_dt)
    
    def process_partial_exits(self, current_date_data: Dict[str, dict]):
        """Check all active positions for partial profit-taking."""
        for symbol, state in list(self.active_positions.items()):
            if symbol not in current_date_data:
                continue
            
            bar = current_date_data[symbol]
            current_close = bar['Close']
            
            should_partial, shares_to_sell = self.exit_engine.check_partial_exit(
                state, current_close
            )
            
            if should_partial and shares_to_sell > 0:
                exit_price = current_close  # Partial exits at close
                
                # Calculate costs
                notional = exit_price * shares_to_sell
                costs = self.cost_model.calculate_sell_costs(notional)
                net_value = notional - costs.total
                
                self.cash += net_value
                
                # Update position
                state.shares -= shares_to_sell
                state.partial_exit_triggered = True
                
                # Update risk manager shares
                self.risk_manager.update_shares(symbol, state.shares)
                
                # Log partial exit
                realized_r = self.exit_engine.calculate_r_multiple(state, exit_price)
                self.trade_log.append({
                    'symbol': symbol,
                    'action': 'PARTIAL_SELL',
                    'date': self.current_date.isoformat(),
                    'price': exit_price,
                    'shares': shares_to_sell,
                    'remaining_shares': state.shares,
                    'realized_r': realized_r,
                    'reason': f'Partial profit-take at {realized_r:.1f}R'
                })
                
                logger.info(
                    f"Partial exit {symbol}: sold {shares_to_sell} @ {exit_price:.2f} "
                    f"({realized_r:.1f}R), {state.shares} remaining"
                )
        
    def process_exits(self, current_date_data: Dict[str, dict], current_dt: pd.Timestamp):
        """Check all active positions for exit conditions."""
        exited = []
        regime_bearish = self._is_regime_bearish(current_dt)
        
        for symbol, state in list(self.active_positions.items()):
            if symbol not in current_date_data:
                continue
                
            bar = current_date_data[symbol]
            
            # Check exit FIRST (uses active stop with breakeven floor)
            should_exit, reason = self.exit_engine.check_exit(
                state, bar['High'], bar['Low'], bar['Close']
            )
            
            if should_exit:
                # Execute exit
                active_stop = self.exit_engine.get_active_stop(state, bar['Close'])
                exit_price = active_stop if self.config.exits.exit_trigger == "intraday_low" else bar['Close']
                
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
                
                # Start cooldown (only for stop-loss exits, not risk-cap exits)
                self.entry_manager.record_stop_out(symbol, self.current_date.isoformat())
                
                self.risk_manager.remove_position(symbol)
                exited.append(symbol)
            else:
                # Update trailing stop for tomorrow, with regime awareness
                self.exit_engine.update_trailing_stop(
                    state, bar['Close'], bar['High'], bar['Low'],
                    bar.get('ATR', state.atr_at_entry),
                    regime_tightened=regime_bearish
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
        
        # Try to extract Nifty data for regime checks
        for key in ['INDEX_NIFTY50', 'INDEX_NIFTY500', 'NIFTY50', 'NIFTY_50']:
            if key in ohlc_data:
                self.nifty_df = ohlc_data[key]
                logger.info(f"Using {key} for regime filter")
                break
        
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
            
            # 1. Process Exits (using today's prices, with regime awareness)
            self.process_exits(current_date_data, current_dt)
            
            # 1.5 Process Partial Exits (profit-taking at configured R-multiple)
            self.process_partial_exits(current_date_data)
            
            # 2. Update equity BEFORE entries for accurate sizing
            active_equity = sum(
                pos.shares * current_date_data[sym]['Close'] 
                for sym, pos in self.active_positions.items() 
                if sym in current_date_data
            )
            self.equity = self.cash + active_equity
            
            # 2.5 Check drawdown circuit breaker
            entries_halted = self._check_drawdown_breaker()
            
            # 3. Process Entries (using today's signals, executing at tomorrow's open logic)
            if not entries_halted:
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
            
            # 4. Update Cooldowns
            self.entry_manager.reduce_cooldowns()
            
            # 5. Final equity snapshot (after any entries)
            active_equity = sum(
                pos.shares * current_date_data[sym]['Close'] 
                for sym, pos in self.active_positions.items() 
                if sym in current_date_data
            )
            total_equity = self.cash + active_equity
            self.equity = total_equity
            
            # Update peak for circuit breaker
            self.peak_equity = max(self.peak_equity, total_equity)
            
            self.daily_metrics.append({
                'date': self.current_date,
                'equity': total_equity,
                'cash': self.cash,
                'invested': active_equity,
                'open_positions': len(self.active_positions),
                'drawdown_pct': (self.peak_equity - total_equity) / self.peak_equity if self.peak_equity > 0 else 0
            })
            
        logger.info(f"Backtest completed. Final equity: {total_equity:.2f}")
        return {
            'trade_log': pd.DataFrame(self.trade_log),
            'daily_metrics': pd.DataFrame(self.daily_metrics).set_index('date')
        }
