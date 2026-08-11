# universe/computed_point_in_time.py
from datetime import date
from typing import List
from pathlib import Path
from loguru import logger
import pandas as pd

from config.settings import BASE_DIR

class PointInTimeUniverse:
    """
    Provides point-in-time index constituents.
    Reads historical index constituents if provided (e.g., NIFTY500_2021-01.csv) 
    to completely eliminate survivorship bias. Falls back to a volume-based proxy 
    using ALL available OHLC data (including delisted) if constituents aren't present.
    """
    
    def __init__(self):
        self.df_cache = {}
        self.metadata = {}
        self.monthly_cache = {}
        self.ohlc_dir = BASE_DIR / "data" / "raw" / "ohlc"
        
    def _get_df(self, symbol: str) -> pd.DataFrame:
        if symbol not in self.df_cache:
            path = self.ohlc_dir / f"{symbol}.csv"
            if path.exists():
                self.df_cache[symbol] = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            else:
                self.df_cache[symbol] = pd.DataFrame()
        return self.df_cache[symbol]

    def get_midcap_universe(self, current_date: date, top_n: int = 150) -> List[str]:
        """Returns the point-in-time universe for the given date."""
        month_key = f"{current_date.year}-{current_date.month:02d}"
        if month_key in self.monthly_cache:
            return self.monthly_cache[month_key]
            
        # Try to find a constituents file first
        constituents_dir = BASE_DIR / "data" / "raw" / "constituents"
        pit_file = constituents_dir / f"NIFTY500_{month_key}.csv"
        
        if pit_file.exists():
            df = pd.read_csv(pit_file)
            if "symbol" in df.columns:
                symbols = df["symbol"].tolist()
                self.monthly_cache[month_key] = symbols[:top_n]
                return self.monthly_cache[month_key]
                
        # Fallback to volume-based proxy
        if not self.ohlc_dir.exists():
            logger.warning(f"No universe data found for {month_key}. Ensure OHLC_DIR is populated.")
            return []
            
        liquidities = []
        for csv in self.ohlc_dir.glob("*.csv"):
            symbol = csv.stem
            df = self._get_df(symbol)
            if df.empty or current_date not in df.index:
                continue
                
            # Get trailing 20d traded value
            slice_df = df.loc[:current_date].tail(20)
            if len(slice_df) > 0:
                avg_val = (slice_df["Close"] * slice_df["Volume"]).mean()
                # Compute 6-month momentum as secondary sort
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
            return []
            
        # Sort by traded value (liquidity proxy for market cap)
        res_df = pd.DataFrame(liquidities).sort_values("traded_value", ascending=False)
        # Take top 500 liquid, then sort by momentum to find midcap leaders
        top_500 = res_df.head(500)
        final_symbols = top_500.sort_values("momentum", ascending=False).head(top_n)["symbol"].tolist()
        
        self.monthly_cache[month_key] = final_symbols
        return final_symbols
