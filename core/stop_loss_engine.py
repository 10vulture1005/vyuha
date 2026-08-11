# core/stop_loss_engine.py
from decimal import Decimal
from typing import Optional
from loguru import logger

from config import thresholds

def compute_new_trailing_stop(
    current_close: Decimal,
    current_atr: Decimal,
    existing_stop: Optional[Decimal],
    is_bull_regime: bool = True,
    atr_multiplier: Optional[Decimal] = None
) -> Decimal:
    """
    Calculates ratcheting ATR trailing stop.
    Rule: new_stop = max(existing_stop, current_close - (multiplier * current_atr))
    Stops NEVER decrease.
    """
    if atr_multiplier is not None:
        mult = atr_multiplier
    else:
        if is_bull_regime:
            mult = Decimal(str(thresholds["technical"]["atr_multiplier_bull"]))
        else:
            mult = Decimal(str(thresholds["technical"]["atr_multiplier_bear"]))
        
    raw_stop = current_close - (mult * current_atr)
    
    if existing_stop is None or existing_stop == Decimal("0"):
        return round(raw_stop, 4)
        
    # Ratchet upwards only
    ratcheted = max(existing_stop, raw_stop)
    if ratcheted > existing_stop:
        logger.debug(f"Stop ratcheted UP from {existing_stop} to {ratcheted}")
    return round(ratcheted, 4)

def is_stop_breached(current_close: Decimal, trailing_stop: Decimal) -> bool:
    """Evaluates if daily EOD close has breached the trailing stop."""
    breached = current_close < trailing_stop
    if breached:
        logger.warning(f"STOP BREACHED: Close {current_close} < Stop {trailing_stop}")
    return breached
