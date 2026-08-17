from typing import Optional, Tuple
from decimal import Decimal
from loguru import logger

from backtest_v2.config import breakout_v2_config
from backtest_v2.execution.sizing import PositionSizer


class EntryManager:
    """
    Manages entry logic: market-at-open vs gap-capped limit.
    
    Handles:
    - Slippage modeling
    - Gap capping (skip if gap > threshold)
    - Skip if open <= SL_0 (signal invalidated)
    - Re-entry cooldown tracking (10 days after stop-out only)
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.order_type = self.config.entry.order_type
        self.gap_cap_pct = self.config.entry.gap_cap_pct
        self.cooldown_days = self.config.entry.re_entry_cooldown_days
        
        # Track cooldown per symbol: symbol -> days_remaining
        self.cooldown_tracker: dict = {}
        
        # Track stop-out dates for cooldown logic
        self.stop_out_dates: dict = {}  # symbol -> last_stop_out_date
    
    def set_cooldown(self, symbol: str, days: int = None):
        """Set cooldown for a symbol (called after stop-out)."""
        if days is None:
            days = self.cooldown_days
        self.cooldown_tracker[symbol] = days
        logger.debug(f"Cooldown set for {symbol}: {days} days")
    
    def reduce_cooldowns(self):
        """Call at end of each trading day to reduce cooldowns."""
        to_remove = []
        for symbol, days in self.cooldown_tracker.items():
            if days <= 1:
                to_remove.append(symbol)
            else:
                self.cooldown_tracker[symbol] = days - 1
        for s in to_remove:
            del self.cooldown_tracker[s]
    
    def is_in_cooldown(self, symbol: str) -> bool:
        """Check if symbol is in re-entry cooldown."""
        return symbol in self.cooldown_tracker
    
    def record_stop_out(self, symbol: str, date):
        """Record stop-out date for cooldown tracking."""
        self.stop_out_dates[symbol] = date
        self.set_cooldown(symbol)
    
    def record_risk_cap_exit(self, symbol: str):
        """Record exit due to portfolio risk cap - NO cooldown."""
        # Explicitly do NOT set cooldown for risk-cap exits
        logger.debug(f"{symbol} exited due to risk cap - no cooldown applied")
    
    def calculate_slippage(self, bar: dict, side: str = "buy") -> float:
        """
        Calculate slippage as percentage.
        Uses 10% of daily spread as proxy.
        """
        high = bar['High']
        low = bar['Low']
        close = bar['Close']
        
        if close <= 0:
            return 0.01  # 1% fallback
        
        spread = (high - low) / close
        slippage = spread * 0.10  # 10% of spread
        
        return slippage
    
    def market_at_open_fill(self, signal_price: float, next_open: float, bar: dict) -> float:
        """
        Market-at-open fill: buy at next day's open with slippage.
        """
        slippage = self.calculate_slippage(bar, "buy")
        fill_price = next_open * (1 + slippage)
        return fill_price
    
    def gap_capped_limit_fill(
        self, 
        signal_price: float, 
        next_open: float, 
        bar: dict
    ) -> Optional[float]:
        """
        Gap-capped limit fill: 
        - If next_open <= signal_price * (1 + gap_cap_pct), fill at next_open + slippage
        - If next_open > signal_price * (1 + gap_cap_pct), skip (return None)
        """
        max_price = signal_price * (1 + self.gap_cap_pct)
        
        if next_open > max_price:
            logger.debug(f"Gap cap exceeded: open={next_open:.2f} > max={max_price:.2f}")
            return None
        
        slippage = self.calculate_slippage(bar, "buy")
        fill_price = next_open * (1 + slippage)
        
        # Don't exceed cap even with slippage
        if fill_price > max_price:
            fill_price = max_price
        
        return fill_price
    
    def get_fill_price(
        self,
        signal_price: float,
        next_open: float,
        bar: dict,
        sl_0: float
    ) -> Optional[float]:
        """
        Get fill price based on configured order type.
        Returns None if entry should be skipped.
        """
        # Check if signal invalidated (open <= SL_0)
        if next_open <= sl_0:
            logger.debug(f"Signal invalidated: open={next_open:.2f} <= SL_0={sl_0:.2f}")
            return None
        
        if self.order_type == "market_at_open":
            return self.market_at_open_fill(signal_price, next_open, bar)
        elif self.order_type == "gap_capped_limit":
            return self.gap_capped_limit_fill(signal_price, next_open, bar)
        else:
            return self.market_at_open_fill(signal_price, next_open, bar)
    
    def compute_sl_0(self, signal_low: float, entry_price: float, atr: float) -> float:
        """
        Compute initial structural stop loss SL_0.
        
        SL_0 = max(L_t - 0.2*ATR, P_entry - 2.0*ATR)
        
        Where L_t is the LOW of the signal day candle.
        """
        fail_safe_mult = 2.0
        buffer_mult = 0.2
        
        stop1 = signal_low - buffer_mult * atr
        stop2 = entry_price - fail_safe_mult * atr
        
        return max(stop1, stop2)


def create_entry_manager(config=None) -> EntryManager:
    return EntryManager(config)