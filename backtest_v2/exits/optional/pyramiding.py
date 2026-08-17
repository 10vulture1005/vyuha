from typing import Optional, Dict, List
from dataclasses import dataclass
from loguru import logger

from backtest_v2.config import breakout_v2_config


@dataclass
class PyramidEntry:
    """Records a pyramid add-on entry."""
    price: float
    shares: int
    stop_at_entry: float
    date: str
    atr_at_entry: float


class PyramidingManager:
    """
    Pyramiding module (OFF by default).
    
    Allows ONE add-on entry if:
    - Price has moved >= +2 ATR from original entry
    - S_tech still > 0.75
    - Position not at portfolio heat cap
    
    Sizes add-on using same risk formula referenced to current ActiveStop_t.
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.enabled = self.config.exits.optional_modules.pyramiding
        self.trigger_atr_mult = 2.0  # Price >= entry + 2*ATR
        self.max_pyramids = 1  # Only one add-on allowed
    
    def check_pyramid_conditions(
        self,
        position,
        current_close: float,
        current_atr: float,
        s_tech: float,
        portfolio_heat_pct: float,
        max_heat_pct: float
    ) -> bool:
        """Check if pyramid add-on is allowed."""
        if not self.enabled:
            return False
        
        # Already pyramided?
        if hasattr(position, 'pyramid_entries') and position.pyramid_entries:
            return False
        
        # Price trigger: >= +2 ATR from entry
        price_target = position.entry_price + self.trigger_atr_mult * current_atr
        if current_close < price_target:
            return False
        
        # S_tech still strong
        if s_tech <= self.config.signals.s_tech_threshold:
            return False
        
        # Not at portfolio heat cap
        if portfolio_heat_pct >= max_heat_pct:
            return False
        
        return True
    
    def calculate_pyramid_size(
        self,
        account_equity: float,
        current_price: float,
        active_stop: float,
        risk_per_trade: float,
        available_cash: float
    ) -> int:
        """
        Calculate pyramid add-on size.
        
        Uses current ActiveStop_t (not original entry stop) for risk calculation.
        """
        if current_price <= active_stop:
            return 0
        
        risk_amount = account_equity * risk_per_trade
        risk_per_share = current_price - active_stop
        
        if risk_per_share <= 0:
            return 0
        
        target_shares = int(risk_amount / risk_per_share)
        max_affordable = int(available_cash / current_price)
        
        return min(target_shares, max_affordable)
    
    def add_pyramid_entry(
        self,
        position,
        price: float,
        shares: int,
        stop: float,
        date: str,
        atr: float
    ):
        """Record pyramid entry."""
        entry = PyramidEntry(
            price=price,
            shares=shares,
            stop_at_entry=stop,
            date=date,
            atr_at_entry=atr
        )
        
        if not hasattr(position, 'pyramid_entries'):
            position.pyramid_entries = []
        
        position.pyramid_entries.append(entry)
        position.shares += shares
        position.initial_qty = position.shares  # Update total
        
        logger.info(f"Pyramid add-on for {position.symbol}: +{shares} shares @ {price:.2f}")
        
        return entry


def create_pyramiding_manager(config=None) -> PyramidingManager:
    return PyramidingManager(config)