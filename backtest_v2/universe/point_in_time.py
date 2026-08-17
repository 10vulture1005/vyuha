import pandas as pd
from datetime import date
from typing import List, Dict, Optional
from loguru import logger

class UniverseManager:
    """
    Manages Point-in-Time (PIT) constituent universe and warmup eligibility logic.
    """
    def __init__(self, config):
        self.config = config
        self.required_warmup_days = self.config.universe.warmup_days
        self._pit_cache: Dict[date, List[str]] = {}
        self._load_pit_data()

    def _load_pit_data(self):
        """
        Loads PIT constituents from the configured file if required.
        If pit_data_required is False, relies on fallback (all provided symbols).
        """
        if not self.config.universe.pit_data_required:
            logger.info("PIT data not required by config. Universe will use all available OHLC symbols.")
            return

        pit_path = self.config.data.constituents_dir
        # TODO: Implement actual CSV/Parquet loading logic once data format is provided.
        # For now, it will silently pass, and get_active_symbols will return all available if not found.
        logger.warning(f"PIT data loading from {pit_path} is stubbed. Waiting for real data format.")

    def get_active_symbols(self, current_date: date, fallback_symbols: List[str] = None) -> List[str]:
        """
        Returns the list of valid constituent symbols for a specific date.
        """
        if not self.config.universe.pit_data_required:
            return fallback_symbols or []

        if current_date in self._pit_cache:
            return self._pit_cache[current_date]
        
        # If no PIT data for the date but fallback is provided, warn and use fallback
        return fallback_symbols or []

    def filter_warmup_eligible(
        self, 
        symbols: List[str], 
        ohlc_data: Dict[str, pd.DataFrame], 
        current_date: date
    ) -> tuple[List[str], List[str]]:
        """
        Filters symbols to ensure they have the strictly required warmup history.
        Returns:
            (eligible_symbols, excluded_for_warmup)
        """
        eligible = []
        excluded = []
        
        for symbol in symbols:
            df = ohlc_data.get(symbol)
            if df is None or df.empty:
                excluded.append(symbol)
                continue
                
            # Count trading days strictly *before or on* current_date
            history_subset = df[df.index.date <= current_date]
            
            if len(history_subset) >= self.required_warmup_days:
                eligible.append(symbol)
            else:
                excluded.append(symbol)
                
        return eligible, excluded
