from loguru import logger

from backtest_v2.config import breakout_v2_config


class SignalDecayExit:
    """
    Signal-decay exit (OFF by default).
    
    Exits if trend signal T falls below its own 25th percentile
    even before price touches trailing stop.
    
    WARNING: Can cut winners early on normal pullbacks.
    Use only as separate EXIT_MODE branch for comparison.
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.enabled = self.config.exits.optional_modules.signal_decay
        self.decay_percentile = self.config.exits.optional_modules.signal_decay_percentile if hasattr(
            self.config.exits.optional_modules, 'signal_decay_percentile') else 0.25
    
    def check_signal_decay(self, T_percentile: float) -> bool:
        """
        Check if trend signal has decayed.
        
        Args:
            T_percentile: Current percentile of T_raw (0-1)
        
        Returns:
            True if should exit due to signal decay
        """
        if not self.enabled:
            return False
        
        if T_percentile < self.decay_percentile:
            logger.debug(f"Signal decay: T percentile {T_percentile:.3f} < {self.decay_percentile}")
            return True
        
        return False


def create_signal_decay_exit(config=None) -> SignalDecayExit:
    return SignalDecayExit(config)