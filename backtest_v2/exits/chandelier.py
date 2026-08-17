from typing import Optional, Tuple
from decimal import Decimal
from dataclasses import dataclass, field
from loguru import logger

from backtest_v2.config import breakout_v2_config


@dataclass
class PositionState:
    """Tracks state for a single position for exit management."""
    symbol: str
    entry_price: float
    initial_stop: float  # SL_0
    current_stop: float  # Trailing stop (TS_t)
    highest_close: float
    entry_date: str
    atr_at_entry: float
    signal_low: float  # L_t for SL_0 calculation
    shares: int
    
    # For breakeven floor
    breakeven_triggered: bool = False
    
    # For pyramiding
    pyramid_entries: list = field(default_factory=list)  # List of (price, shares, stop_at_entry)
    
    # Exit tracking
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_r: Optional[float] = None


class ChandelierExit:
    """
    Ratcheted Chandelier Exit with breakeven floor.
    
    SL_0 = max(L_t - 0.2*ATR, P_entry - 2.0*ATR)
    TS_t = max(TS_{t-1}, HighestClose_t - 3*ATR)  [monotonic]
    If unrealized >= 1R: ActiveStop = max(TS_t, P_entry)
    Exit when price breaches ActiveStop.
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.fail_safe_mult = self.config.exits.fail_safe_atr_mult
        self.chandelier_mult = self.config.exits.chandelier_atr_mult
        self.buffer_mult = 0.2  # Fixed per spec
        self.breakeven_floor_r = self.config.exits.breakeven_floor_r
        self.exit_trigger = self.config.exits.exit_trigger  # "intraday_low" or "close_only"
    
    def compute_sl_0(self, signal_low: float, entry_price: float, atr: float) -> float:
        """
        Compute initial structural stop SL_0.
        
        SL_0 = max(L_t - 0.2*ATR, P_entry - 2.0*ATR)
        """
        stop1 = signal_low - self.buffer_mult * atr
        stop2 = entry_price - self.fail_safe_mult * atr
        return max(stop1, stop2)
    
    def initialize_position(
        self,
        symbol: str,
        entry_price: float,
        signal_low: float,
        atr: float,
        shares: int,
        entry_date: str
    ) -> PositionState:
        """Initialize position state with SL_0."""
        sl_0 = self.compute_sl_0(signal_low, entry_price, atr)
        
        state = PositionState(
            symbol=symbol,
            entry_price=entry_price,
            initial_stop=sl_0,
            current_stop=sl_0,
            highest_close=entry_price,
            entry_date=entry_date,
            atr_at_entry=atr,
            signal_low=signal_low,
            shares=shares
        )
        
        logger.debug(f"Initialized {symbol}: entry={entry_price:.2f}, SL_0={sl_0:.2f}, ATR={atr:.2f}")
        return state
    
    def update_trailing_stop(
        self,
        state: PositionState,
        current_close: float,
        current_high: float,
        current_low: float,
        current_atr: float,
        regime_tightened: bool = False
    ) -> Tuple[float, bool]:
        """
        Update trailing stop for a position.
        
        Returns:
            (new_stop, stop_moved_up)
        """
        # Update highest close
        if current_close > state.highest_close:
            state.highest_close = current_close
        
        # Determine ATR multiplier (regime tightening)
        if regime_tightened:
            mult = self.config.exits.optional_modules.regime_tighten_mult if hasattr(
                self.config.exits.optional_modules, 'regime_tighten_mult') else 2.0
        else:
            mult = self.chandelier_mult
        
        # Chandelier stop: HighestClose - mult * ATR
        chandelier_stop = state.highest_close - mult * current_atr
        
        # Ratchet: stop never decreases
        new_stop = max(state.current_stop, chandelier_stop)
        stop_moved = new_stop > state.current_stop
        
        if stop_moved:
            logger.debug(f"{state.symbol}: Stop ratcheted {state.current_stop:.2f} -> {new_stop:.2f}")
        
        state.current_stop = new_stop
        return new_stop, stop_moved
    
    def check_breakeven_floor(self, state: PositionState, current_close: float) -> float:
        """
        Apply breakeven floor if unrealized gain >= 1R.
        
        ActiveStop = max(TS_t, P_entry) once unrealized >= 1R
        """
        risk_per_share = state.entry_price - state.initial_stop
        if risk_per_share <= 0:
            return state.current_stop
        
        unrealized_r = (current_close - state.entry_price) / risk_per_share
        
        if unrealized_r >= self.breakeven_floor_r and not state.breakeven_triggered:
            state.breakeven_triggered = True
            logger.debug(f"{state.symbol}: Breakeven floor triggered at {unrealized_r:.2f}R")
        
        if state.breakeven_triggered:
            return max(state.current_stop, state.entry_price)
        
        return state.current_stop
    
    def get_active_stop(self, state: PositionState, current_close: float) -> float:
        """Get the active stop price (with breakeven floor)."""
        return self.check_breakeven_floor(state, current_close)
    
    def check_exit(
        self,
        state: PositionState,
        current_high: float,
        current_low: float,
        current_close: float
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if position should be exited.
        
        Returns:
            (should_exit, reason)
        """
        active_stop = state.current_stop
        
        if self.exit_trigger == "intraday_low":
            # Exit if intraday low breaches stop
            if current_low <= active_stop:
                return True, f"Stop breached (intraday low {current_low:.2f} <= {active_stop:.2f})"
        elif self.exit_trigger == "close_only":
            # Exit only on close breach
            if current_close <= active_stop:
                return True, f"Stop breached (close {current_close:.2f} <= {active_stop:.2f})"
        
        return False, None
    
    def calculate_r_multiple(self, state: PositionState, exit_price: float) -> float:
        """Calculate realized R-multiple."""
        risk_per_share = state.entry_price - state.initial_stop
        if risk_per_share <= 0:
            return 0.0
        return (exit_price - state.entry_price) / risk_per_share
    
    def close_position(
        self,
        state: PositionState,
        exit_price: float,
        exit_date: str,
        reason: str
    ) -> dict:
        """Finalize position closure."""
        state.exit_date = exit_date
        state.exit_price = exit_price
        state.exit_reason = reason
        state.realized_r = self.calculate_r_multiple(state, exit_price)
        
        return {
            'symbol': state.symbol,
            'entry_date': state.entry_date,
            'exit_date': exit_date,
            'entry_price': state.entry_price,
            'exit_price': exit_price,
            'shares': state.shares,
            'initial_stop': state.initial_stop,
            'final_stop': state.current_stop,
            'realized_r': state.realized_r,
            'reason': reason,
            'highest_close': state.highest_close,
            'breakeven_triggered': state.breakeven_triggered
        }


def create_chandelier_exit(config=None) -> ChandelierExit:
    return ChandelierExit(config)