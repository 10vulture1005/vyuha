from datetime import date, timedelta
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from loguru import logger

from config.settings import BASE_DIR


class MockDataGenerator:
    def __init__(self, seed: int = 42):
        np.random.seed(seed)
        self.seed = seed
    
    def generate_constituents(
        self, 
        start_date: date, 
        end_date: date, 
        n_symbols: int = 500,
        churn_rate: float = 0.05
    ) -> Dict[str, List[str]]:
        """Generate monthly PIT constituent lists with realistic churn."""
        symbols = [f"STOCK{i:04d}" for i in range(n_symbols)]
        np.random.shuffle(symbols)
        
        constituents = {}
        current_constituents = symbols[:n_symbols]
        
        current = pd.Timestamp(start_date).to_period("M")
        end = pd.Timestamp(end_date).to_period("M")
        
        while current <= end:
            month_key = str(current)
            constituents[month_key] = current_constituents.copy()
            
            # Simulate index churn
            n_changes = int(n_symbols * churn_rate)
            if n_changes > 0:
                # Remove some
                remove_idx = np.random.choice(len(current_constituents), n_changes, replace=False)
                new_constituents = [s for i, s in enumerate(current_constituents) if i not in remove_idx]
                
                # Add new ones from remaining pool
                available = [s for s in symbols if s not in current_constituents]
                if len(available) >= n_changes:
                    add_idx = np.random.choice(len(available), n_changes, replace=False)
                    new_constituents.extend([available[i] for i in add_idx])
                
                current_constituents = new_constituents
            
            current += 1
        
        return constituents
    
    def generate_ohlc(
        self,
        symbols: List[str],
        start_date: date,
        end_date: date,
        base_price_range: tuple = (50, 5000),
        trend_strength: float = 0.0004,  # ~10% annual drift
        volatility_range: tuple = (0.015, 0.035),
        gap_prob: float = 0.015,
        gap_size_range: tuple = (-0.04, 0.04),
        trend_persistence: float = 0.95,  # High persistence for trending stocks
        breakout_prob: float = 0.03,  # Probability of breakout days
    ) -> Dict[str, pd.DataFrame]:
        """Generate realistic OHLCV data for symbols with trending behavior."""
        ohlc_data = {}
        
        trading_days = pd.bdate_range(start_date, end_date)
        n_days = len(trading_days)
        
        for symbol in symbols:
            # Random parameters per stock
            base_price = np.random.uniform(*base_price_range)
            
            # Assign stock "personality" - some trend, some chop
            stock_type = np.random.choice(['trend', 'chop', 'volatile'], p=[0.4, 0.4, 0.2])
            
            if stock_type == 'trend':
                daily_trend = np.random.uniform(0.0003, 0.0006)  # 7.5% - 15% annual
                daily_vol = np.random.uniform(0.012, 0.025)
                persistence = np.random.uniform(0.93, 0.97)
            elif stock_type == 'chop':
                daily_trend = np.random.uniform(-0.0001, 0.0002)  # -2.5% to 5% annual
                daily_vol = np.random.uniform(0.015, 0.03)
                persistence = np.random.uniform(0.7, 0.85)
            else:  # volatile
                daily_trend = np.random.uniform(-0.0002, 0.0004)
                daily_vol = np.random.uniform(0.025, 0.045)
                persistence = np.random.uniform(0.8, 0.92)
            
            # Generate returns with AR(1) process for trend persistence
            returns = np.zeros(n_days)
            returns[0] = np.random.normal(daily_trend, daily_vol)
            
            for i in range(1, n_days):
                # AR(1) with mean reversion to trend
                returns[i] = (persistence * returns[i-1] + 
                             (1 - persistence) * daily_trend + 
                             np.random.normal(0, daily_vol * np.sqrt(1 - persistence**2)))
            
            # Add regime shifts (bull/bear markets) - longer duration
            n_regimes = np.random.randint(1, 4)
            regime_boundaries = np.sort(np.random.choice(n_days, n_regimes, replace=False))
            
            for i, boundary in enumerate(regime_boundaries):
                regime_length = n_days - boundary if i == len(regime_boundaries) - 1 else regime_boundaries[i+1] - boundary
                if stock_type == 'trend':
                    # Trending stocks have milder regime shifts
                    regime_mult = np.random.uniform(0.7, 1.3)
                elif stock_type == 'chop':
                    regime_mult = np.random.uniform(-1.0, 1.0)
                else:
                    regime_mult = np.random.uniform(-1.5, 1.5)
                
                returns[boundary:] += regime_mult * daily_trend * 0.5
            
            # Add breakout days - large volume + price moves
            breakout_mask = np.random.random(n_days) < breakout_prob
            n_breakouts = breakout_mask.sum()
            if n_breakouts > 0:
                breakout_direction = np.random.choice([-1, 1], n_breakouts, p=[0.3, 0.7])  # More upside breakouts
                breakout_sizes = np.random.uniform(0.03, 0.08, n_breakouts) * breakout_direction
                returns[breakout_mask] += breakout_sizes
            
            # Add gaps
            gap_mask = np.random.random(n_days) < gap_prob
            gap_sizes = np.random.uniform(*gap_size_range, gap_mask.sum())
            returns[gap_mask] += gap_sizes
            
            # Build price series
            prices = base_price * np.exp(np.cumsum(returns))
            
            # Generate OHLC from close prices
            intraday_vol = daily_vol * 0.4
            
            # High/Low with correlation to daily return
            daily_range = np.abs(returns) * 0.5 + intraday_vol * np.abs(np.random.normal(0, 1, n_days))
            high = prices * np.exp(daily_range)
            low = prices * np.exp(-daily_range)
            
            # Open = previous close * (1 + overnight gap)
            opens = np.roll(prices, 1)
            opens[0] = base_price
            overnight_gap = np.random.normal(0, daily_vol * 0.2, n_days)
            opens = opens * (1 + overnight_gap)
            
            # Ensure OHLC consistency
            high = np.maximum(high, np.maximum(opens, prices))
            low = np.minimum(low, np.minimum(opens, prices))
            
            # Volume - lognormal with correlation to volatility AND breakouts
            base_volume = np.random.uniform(5e5, 5e7)
            volume = base_volume * np.exp(np.random.normal(0, 0.4, n_days))
            
            # Volume spikes on breakout days and high volatility days
            vol_multiplier = 1 + np.abs(returns) * 8
            vol_multiplier[breakout_mask] *= np.random.uniform(3, 8, n_breakouts)
            volume = volume * vol_multiplier
            
            df = pd.DataFrame({
                "Open": opens,
                "High": high,
                "Low": low,
                "Close": prices,
                "Volume": volume.astype(int)
            }, index=trading_days)
            
            df.index.name = "Date"
            ohlc_data[symbol] = df
        
        return ohlc_data
    
    def save_mock_data(
        self,
        constituents: Dict[str, List[str]],
        ohlc_data: Dict[str, pd.DataFrame],
        output_dir: str = "backtest_v2/tests/fixtures/mock"
    ):
        """Save mock data to CSV files for testing."""
        path = BASE_DIR / output_dir
        path.mkdir(parents=True, exist_ok=True)
        
        # Save constituents
        const_dir = path / "constituents"
        const_dir.mkdir(exist_ok=True)
        for month_key, symbols in constituents.items():
            df = pd.DataFrame({"symbol": symbols})
            df.to_csv(const_dir / f"NIFTY500_{month_key}.csv", index=False)
        
        # Save OHLC
        ohlc_dir = path / "ohlc"
        ohlc_dir.mkdir(exist_ok=True)
        for symbol, df in ohlc_data.items():
            df.to_csv(ohlc_dir / f"{symbol}.csv")
        
        logger.info(f"Saved mock data to {path}")
    
    def generate_and_save(
        self,
        start_date: date,
        end_date: date,
        n_symbols: int = 100,
        output_dir: str = "backtest_v2/tests/fixtures/mock"
    ):
        """Convenience method to generate and save all mock data."""
        logger.info(f"Generating mock data for {n_symbols} symbols from {start_date} to {end_date}")
        constituents = self.generate_constituents(start_date, end_date, n_symbols)
        all_symbols = set()
        for syms in constituents.values():
            all_symbols.update(syms)
        all_symbols = list(all_symbols)
        ohlc_data = self.generate_ohlc(all_symbols, start_date, end_date)
        self.save_mock_data(constituents, ohlc_data, output_dir)
        return constituents, ohlc_data


def create_sample_fixtures():
    """Create minimal sample fixtures for unit tests."""
    gen = MockDataGenerator(seed=123)
    start = date(2023, 1, 1)
    end = date(2023, 12, 31)
    
    # Small set for fast tests
    constituents, ohlc = gen.generate_and_save(start, end, n_symbols=20)
    
    # Also create a simple single-stock fixture for signal tests
    single_ohlc = gen.generate_ohlc(["TESTSTOCK"], start, end)
    df = single_ohlc["TESTSTOCK"]
    df.to_csv(BASE_DIR / "backtest_v2/tests/fixtures/sample_ohlc.csv")
    
    # Sample constituents JSON
    sample_const = {"2023-01": ["TESTSTOCK", "STOCK0001", "STOCK0002"]}
    import json
    with open(BASE_DIR / "backtest_v2/tests/fixtures/sample_constituents.json", "w") as f:
        json.dump(sample_const, f)


if __name__ == "__main__":
    create_sample_fixtures()