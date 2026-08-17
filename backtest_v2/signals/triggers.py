from typing import Dict, List, Tuple, Optional
import pandas as pd
import numpy as np
from loguru import logger

from backtest_v2.config import breakout_v2_config
from backtest_v2.data.point_in_time import PointInTimeUniverseV2


class SignalFilter:
    """
    Applies filters and selects candidates for entry.
    
    Filters:
    1. Regime filter: Nifty Close > SMA_200
    2. Breakout trigger: Close > H_20 (20-day high excluding current bar)
    3. Liquidity: SMA_20(Volume) * Close >= min_turnover
    4. S_tech > 0.75
    
    Selection: Rank by S_tech descending, apply sector/risk caps
    """
    
    def __init__(self, config=None, pit_universe: PointInTimeUniverseV2 = None):
        self.config = config or breakout_v2_config
        self.pit_universe = pit_universe
        self.regime_filter_enabled = self.config.filters.regime_filter
        self.min_turnover = self.config.filters.liquidity_min_turnover_inr
        self.turnover_multiplier = self.config.filters.min_turnover_multiplier
    
    def check_regime_filter(self, nifty_df: pd.DataFrame, current_date: pd.Timestamp) -> bool:
        """
        Regime filter: Nifty Close > SMA_200.
        Evaluated at close of signal day t, gates new entries at t+1 only.
        """
        if not self.regime_filter_enabled:
            return True
        
        if nifty_df is None or nifty_df.empty:
            return True
        
        idx_slice = nifty_df.loc[:current_date]
        if len(idx_slice) < 200:
            return True  # Not enough data, allow
        
        close = idx_slice['Close'].iloc[-1]
        sma200 = idx_slice['Close'].rolling(200).mean().iloc[-1]
        
        return close > sma200
    
    def check_breakout_trigger(self, df: pd.DataFrame, current_date: pd.Timestamp) -> bool:
        """
        Breakout trigger: Close > H_20.
        H_20 is 20-day high EXCLUDING current bar (shift(1).rolling(20).max()).
        """
        if current_date not in df.index:
            return False
        
        # Get data up to and including current_date
        df_slice = df.loc[:current_date]
        if len(df_slice) < 21:  # Need 20 prior + current
            return False
        
        close = df_slice['Close'].iloc[-1]
        high = df_slice['High']
        
        # H_20 excluding current bar
        H_20 = high.shift(1).rolling(20).max().iloc[-1]
        
        return close > H_20
    
    def check_liquidity(self, df: pd.DataFrame, current_date: pd.Timestamp, 
                        max_notional: float = None) -> bool:
        """
        Liquidity filter: SMA_20(Volume) * Close >= threshold.
        Threshold = max(1Cr, 20 * MaxNotional) where MaxNotional is max position size.
        """
        if current_date not in df.index:
            return False
        
        df_slice = df.loc[:current_date]
        if len(df_slice) < 20:
            return False
        
        close = df_slice['Close'].iloc[-1]
        vol_sma20 = df_slice['Volume'].rolling(20).mean().iloc[-1]
        
        turnover = vol_sma20 * close
        
        # Dynamic threshold
        if max_notional:
            threshold = max(self.min_turnover, self.turnover_multiplier * max_notional)
        else:
            threshold = self.min_turnover
        
        return turnover >= threshold
    
    def check_s_tech(self, S_tech: pd.Series, current_date: pd.Timestamp) -> bool:
        """Check if S_tech > threshold at current date."""
        if current_date not in S_tech.index:
            return False
        return S_tech.loc[current_date] > self.config.signals.s_tech_threshold
    
    def filter_symbol(self, symbol: str, df: pd.DataFrame, 
                      S_tech: pd.Series, nifty_df: pd.DataFrame,
                      current_date: pd.Timestamp,
                      max_notional: float = None) -> bool:
        """Apply all filters for a single symbol."""
        if not self.check_regime_filter(nifty_df, current_date):
            return False
        
        if not self.check_breakout_trigger(df, current_date):
            return False
        
        if not self.check_liquidity(df, current_date, max_notional):
            return False
        
        if not self.check_s_tech(S_tech, current_date):
            return False
        
        return True


def select_candidates(
    signals: Dict[str, Dict],  # symbol -> {S_tech, df, ...}
    max_positions: int,
    sector_map: Dict[str, str],
    max_per_sector: int = 2
) -> List[str]:
    """
    Select top candidates by S_tech, applying sector concentration cap.
    
    Args:
        signals: Dict of symbol -> {'S_tech': float, 'df': DataFrame, ...}
        max_positions: Maximum total positions
        sector_map: symbol -> sector
        max_per_sector: Max positions per sector
    
    Returns:
        List of selected symbols in priority order
    """
    # Sort by S_tech descending
    sorted_symbols = sorted(signals.keys(), 
                           key=lambda s: signals[s].get('S_tech', 0), 
                           reverse=True)
    
    selected = []
    sector_counts = {}
    
    for symbol in sorted_symbols:
        if len(selected) >= max_positions:
            break
        
        sector = sector_map.get(symbol, 'UNKNOWN')
        current_count = sector_counts.get(sector, 0)
        
        if current_count >= max_per_sector:
            continue
        
        selected.append(symbol)
        sector_counts[sector] = current_count + 1
    
    return selected


class SignalGenerator:
    """
    Full signal generation pipeline for a single symbol on a single day.
    """
    
    def __init__(self, config=None, pit_universe: PointInTimeUniverseV2 = None):
        self.config = config or breakout_v2_config
        self.pit_universe = pit_universe
        self.filter = SignalFilter(config, pit_universe)
    
    def generate_signals_for_date(
        self,
        current_date: pd.Timestamp,
        ohlc_data: Dict[str, pd.DataFrame],
        nifty_df: pd.DataFrame,
        raw_calculator,
        composite_scorer
    ) -> Dict[str, Dict]:
        """
        Generate signals for all eligible symbols on a given date.
        
        Returns:
            Dict of symbol -> signal_data including S_tech, raw scores, etc.
        """
        signals = {}
        
        # Get eligible universe
        if self.pit_universe:
            eligible, warmup_excl, nodata_excl = self.pit_universe.get_warmup_eligible(
                current_date.date()
            )
        else:
            # Use all available symbols
            eligible = [s for s in ohlc_data.keys() if not s.startswith('INDEX_')]
            warmup_excl = []
            nodata_excl = []
        
        for symbol in eligible:
            if symbol not in ohlc_data:
                continue
            
            df = ohlc_data[symbol]
            if current_date not in df.index:
                continue
            
            # Compute raw scores
            raw_scores = raw_calculator.compute_all_raw(df)
            components = raw_calculator.compute_percentile_components(raw_scores)
            
            # Compute composite
            S_raw, S_tech = composite_scorer.compute_all(components)
            
            # Get current values
            s_tech_val = S_tech.loc[current_date] if current_date in S_tech.index else np.nan
            s_raw_val = S_raw.loc[current_date] if current_date in S_raw.index else np.nan
            
            if np.isnan(s_tech_val):
                continue
            
            # Check filters
            max_notional = 10000000  # Will be refined by portfolio risk
            if not self.filter.filter_symbol(symbol, df, S_tech, nifty_df, 
                                             current_date, max_notional):
                continue
            
            # Get signal day data for SL_0 calculation
            bar = df.loc[current_date]
            signal_low = bar['Low']
            atr = raw_calculator.compute_atr(df).loc[current_date]
            
            signals[symbol] = {
                'S_tech': float(s_tech_val),
                'S_raw': float(s_raw_val),
                'T': float(components['T'].loc[current_date]) if current_date in components['T'].index else np.nan,
                'M': float(components['M'].loc[current_date]) if current_date in components['M'].index else np.nan,
                'B': float(components['B'].loc[current_date]) if current_date in components['B'].index else np.nan,
                'C': float(components['C'].loc[current_date]) if current_date in components['C'].index else np.nan,
                'signal_low': float(signal_low),
                'atr': float(atr),
                'close': float(bar['Close']),
                'open': float(bar['Open']),
                'high': float(bar['High']),
                'volume': float(bar['Volume']),
                'df': df,
            }
        
        return signals