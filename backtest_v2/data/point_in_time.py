from datetime import date
from typing import List, Dict, Tuple, Optional
from pathlib import Path
import pandas as pd
from loguru import logger

from config.settings import BASE_DIR
from backtest_v2.config import breakout_v2_config
from backtest_v2.data.sector_taxonomy import get_sector_taxonomy
from backtest_v2.data.mock_generator import MockDataGenerator


class PointInTimeUniverseV2:
    """
    Provides point-in-time Nifty 500 constituents with warmup eligibility tracking.
    
    Key features:
    - Uses historical constituent files if available (eliminates survivorship bias)
    - Falls back to volume/momentum proxy from OHLC data
    - Tracks 450-day warmup requirement (SMA_200 + 252 percentile window)
    - Separates "excluded for warmup" from "excluded for no signal"
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.warmup_days = self.config.universe.warmup_days
        self.ohlc_dir = BASE_DIR / self.config.data.ohlc_dir
        self.constituents_dir = BASE_DIR / self.config.data.constituents_dir
        self._ohlc_cache: Dict[str, pd.DataFrame] = {}
        self._constituents_cache: Dict[str, List[str]] = {}
        self._sector_taxonomy = get_sector_taxonomy(self.config.data.sector_mapping_file)
        
        # Stats tracking
        self.warmup_excluded_count = 0
        self.no_signal_excluded_count = 0
    
    def _get_ohlc_df(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._ohlc_cache:
            path = self.ohlc_dir / f"{symbol}.csv"
            if path.exists():
                try:
                    df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
                    df = df.rename(columns={c: c.capitalize() for c in df.columns})
                    df = df.sort_index()
                    self._ohlc_cache[symbol] = df
                except Exception as e:
                    logger.warning(f"Failed to load OHLC for {symbol}: {e}")
                    self._ohlc_cache[symbol] = pd.DataFrame()
            else:
                self._ohlc_cache[symbol] = pd.DataFrame()
        return self._ohlc_cache[symbol]
    
    def _get_constituents(self, current_date: date) -> List[str]:
        month_key = f"{current_date.year}-{current_date.month:02d}"
        
        if month_key in self._constituents_cache:
            return self._constituents_cache[month_key]
        
        # Try PIT constituents file first
        pit_file = self.constituents_dir / f"NIFTY500_{month_key}.csv"
        if pit_file.exists():
            try:
                df = pd.read_csv(pit_file)
                if "symbol" in df.columns:
                    symbols = df["symbol"].tolist()
                    self._constituents_cache[month_key] = symbols
                    return symbols
            except Exception as e:
                logger.warning(f"Failed to load constituents from {pit_file}: {e}")
        
        # Fallback: volume-based proxy using ALL available OHLC data
        logger.debug(f"Using volume proxy for {month_key}")
        liquidities = []
        
        for csv_file in self.ohlc_dir.glob("*.csv"):
            symbol = csv_file.stem
            if symbol.startswith("INDEX_"):
                continue
            df = self._get_ohlc_df(symbol)
            if df.empty or pd.Timestamp(current_date) not in df.index:
                continue
            
            # Trailing 20-day traded value
            slice_df = df.loc[:current_date].tail(20)
            if len(slice_df) == 0:
                continue
            avg_val = (slice_df["Close"] * slice_df["Volume"]).mean()
            
            # 6-month momentum as secondary sort
            momentum = 0.0
            if len(df.loc[:current_date]) >= 126:
                past_close = df.loc[:current_date].iloc[-126]["Close"]
                curr_close = df.loc[:current_date].iloc[-1]["Close"]
                momentum = (curr_close / past_close) - 1.0
            
            liquidities.append({
                "symbol": symbol,
                "traded_value": avg_val,
                "momentum": momentum
            })
        
        if not liquidities:
            self._constituents_cache[month_key] = []
            return []
        
        res_df = pd.DataFrame(liquidities).sort_values("traded_value", ascending=False)
        top_500 = res_df.head(500)
        final_symbols = top_500.sort_values("momentum", ascending=False).head(150)["symbol"].tolist()
        
        self._constituents_cache[month_key] = final_symbols
        return final_symbols
    
    def get_constituents(self, current_date: date) -> List[str]:
        """Get PIT Nifty 500 constituents for a given date."""
        return self._get_constituents(current_date)
    
    def get_warmup_eligible(
        self, 
        current_date: date, 
        symbols: Optional[List[str]] = None
    ) -> Tuple[List[str], List[str], List[str]]:
        """
        Filter symbols by warmup requirement (450 days of history).
        
        Returns:
            Tuple of (eligible_symbols, excluded_warmup, excluded_no_data)
        """
        if symbols is None:
            symbols = self.get_constituents(current_date)
        
        eligible = []
        excluded_warmup = []
        excluded_no_data = []
        
        for symbol in symbols:
            df = self._get_ohlc_df(symbol)
            if df.empty:
                excluded_no_data.append(symbol)
                continue
            
            # Check if we have data up to current_date
            if pd.Timestamp(current_date) not in df.index:
                excluded_no_data.append(symbol)
                continue
            
            # Count trading days up to current_date
            df_slice = df.loc[:current_date]
            if len(df_slice) >= self.warmup_days:
                eligible.append(symbol)
            else:
                excluded_warmup.append(symbol)
        
        self.warmup_excluded_count = len(excluded_warmup)
        self.no_signal_excluded_count = len(excluded_no_data)
        
        return eligible, excluded_warmup, excluded_no_data
    
    def get_sector(self, symbol: str) -> str:
        return self._sector_taxonomy.get_sector(symbol)
    
    def get_warmup_stats(self) -> Dict[str, int]:
        return {
            "warmup_excluded": self.warmup_excluded_count,
            "no_data_excluded": self.no_signal_excluded_count
        }


def create_pit_universe(config=None) -> PointInTimeUniverseV2:
    return PointInTimeUniverseV2(config)


# For testing: create mock PIT universe if real data not available
def ensure_mock_data_exists(start_date: date, end_date: date, n_symbols: int = 100):
    """Generate mock data if constituents directory is empty."""
    const_dir = BASE_DIR / "data" / "raw" / "constituents"
    if not any(const_dir.glob("*.csv")):
        logger.info("No PIT constituents found, generating mock data...")
        gen = MockDataGenerator(seed=42)
        gen.generate_and_save(start_date, end_date, n_symbols, "data/raw")