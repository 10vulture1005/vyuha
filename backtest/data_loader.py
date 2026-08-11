# backtest/data_loader.py
"""Historical OHLCV data loader with trading calendar alignment and bar-level access.

Provides:
    - Multi-year OHLCV ingestion from CSV archives
    - Union trading calendar across all loaded symbols
    - Point-in-time bar access for the event-driven replay loop
    - Rolling window slicing for indicator computation during backtest
"""
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
from loguru import logger
from config.settings import BASE_DIR

OHLC_DIR = BASE_DIR / "data" / "raw" / "ohlc"


class HistoricalDataLoader:
    """Loads and aligns multi-year historical OHLCV data across the seed universe."""

    def __init__(self, start_date: str, end_date: str):
        self.start_date = pd.to_datetime(start_date)
        self.end_date = pd.to_datetime(end_date)
        self.trading_calendar: List[pd.Timestamp] = []
        self.ohlc_matrix: Dict[str, pd.DataFrame] = {}

    def load_universe_data(self, symbols: List[str]) -> bool:
        """Loads OHLCV CSVs for each symbol, filters to date range, and builds union trading calendar."""
        if "^CRSLDX" not in symbols:
            symbols = list(symbols) + ["^CRSLDX"]
            
        logger.info(
            f"Loading historical data for {len(symbols)} symbols "
            f"from {self.start_date.date()} to {self.end_date.date()}..."
        )
        master_dates = set()

        for sym in symbols:
            file_path = OHLC_DIR / f"{sym}.csv"
            if not file_path.exists():
                logger.warning(f"OHLCV CSV not found for {sym} at {file_path}")
                continue
            try:
                df = pd.read_csv(file_path, parse_dates=["Date"], index_col="Date")
                # Ensure columns are properly capitalized for consistency
                df = df.rename(columns={c: c.capitalize() for c in df.columns})
                df = df.loc[self.start_date : self.end_date].sort_index()
                if len(df) > 100:
                    self.ohlc_matrix[sym] = df
                    master_dates.update(df.index.tolist())
            except Exception as e:
                logger.warning(f"Failed to load historical CSV for {sym}: {e}")

        if not self.ohlc_matrix:
            logger.error(
                "No historical data loaded. Ensure OHLCV CSVs exist in data/raw/ohlc/."
            )
            return False

        self.trading_calendar = sorted(list(master_dates))
        logger.info(
            f"Successfully loaded {len(self.ohlc_matrix)} symbols "
            f"across {len(self.trading_calendar)} trading days."
        )
        return True

    def get_bar(self, symbol: str, current_date: pd.Timestamp) -> Optional[pd.Series]:
        """Returns the specific OHLCV bar for a symbol on a given trading day.

        Returns None if the symbol has no data for the given date.
        """
        df = self.ohlc_matrix.get(symbol)
        if df is None or current_date not in df.index:
            return None
        return df.loc[current_date]

    def get_slice(
        self, symbol: str, end_date: pd.Timestamp, lookback: int = 30
    ) -> Optional[pd.DataFrame]:
        """Returns a rolling window of OHLCV data ending at `end_date`.

        Used during the backtest event loop to compute indicators (e.g., volume MA)
        without introducing lookahead bias.

        Args:
            symbol: Ticker symbol.
            end_date: The current simulation date (inclusive upper bound).
            lookback: Number of trailing bars to return.

        Returns:
            DataFrame slice or None if insufficient data.
        """
        df = self.ohlc_matrix.get(symbol)
        if df is None:
            return None
        # Select all rows up to and including end_date
        df_slice = df.loc[:end_date]
        if len(df_slice) < lookback:
            return None
        return df_slice.iloc[-lookback:]
