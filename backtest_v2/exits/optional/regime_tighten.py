from loguru import logger

from backtest_v2.config import breakout_v2_config


class RegimeTightening:
    """
    Regime-based stop tightening (OFF by default).
    
    If Nifty drops below SMA_200 while position is open:
    - Tighten trailing multiplier from 3 ATR to 2 ATR
    - Does NOT force-close position
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.enabled = self.config.exits.optional_modules.regime_tighten
        self.normal_mult = self.config.exits.chandelier_atr_mult
        self.tight_mult = 2.0  # Tightened multiplier
    
    def check_regime_flip(self, nifty_df, current_date) -> bool:
        """
        Check if regime has flipped (Nifty below SMA_200).
        
        Returns True if regime is bearish (should tighten).
        """
        if not self.enabled:
            return False
        
        if nifty_df is None or nifty_df.empty:
            return False
        
        idx_slice = nifty_df.loc[:current_date]
        if len(idx_slice) < 200:
            return False
        
        close = idx_slice['Close'].iloc[-1]
        sma200 = idx_slice['Close'].rolling(200).mean().iloc[-1]
        
        return close < sma200
    
    def get_atr_multiplier(self, nifty_df, current_date) -> float:
        """Get appropriate ATR multiplier based on regime."""
        if self.check_regime_flip(nifty_df, current_date):
            logger.debug("Regime flip detected: tightening stops to 2 ATR")
            return self.tight_mult
        return self.normal_mult


def create_regime_tightening(config=None) -> RegimeTightening:
    return RegimeTightening(config)