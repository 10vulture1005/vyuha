# data/fundamentals_provider.py
from datetime import date
from typing import Dict, Optional
import pandas as pd
from loguru import logger

from config.settings import BASE_DIR
from config import thresholds

class FundamentalsProvider:
    """
    Provides point-in-time fundamental metrics for backtesting.
    Reads from a historical dataset (e.g., fundamentals.csv) containing explicit
    filing dates to completely prevent look-ahead bias.
    Falls back to a seeded stylized generator if the real data file is absent.
    """
    def __init__(self):
        self._cache = {}
        self.historical_data = pd.DataFrame()
        self.quarters = ["03-31", "06-30", "09-30", "12-31"]
        
        fund_path = BASE_DIR / "data" / "raw" / "fundamentals.csv"
        if fund_path.exists():
            self.historical_data = pd.read_csv(fund_path, parse_dates=["filing_date"])
            logger.info(f"Loaded Point-in-Time fundamental data from {fund_path}")
        else:
            logger.warning(f"Failed to load fundamentals from {fund_path}: File not found. Using fallback stylized metrics.")

    def _generate_stylized_metrics(self, symbol: str, year: int, quarter: str) -> Dict[str, float]:
        """Generates deterministic mock fundamentals using a hash of the symbol and quarter."""
        import zlib
        seed_str = f"{symbol}-{year}-{quarter}"
        h = zlib.adler32(seed_str.encode())
        
        roce = 5.0 + (h % 30)
        eps_g = -10.0 + (h % 50)
        de = (h % 200) / 100.0
        pledge = (h % 100) / 1.0 if (h % 10) == 0 else 0.0
        rev = -5.0 + (h % 40)
        margin = -2.0 + (h % 10)
        
        return {
            "roce_ttm": float(roce),
            "eps_growth_yoy": float(eps_g),
            "de_ratio": float(de),
            "promoter_pledge_pct": float(pledge),
            "revenue_growth_yoy": float(rev),
            "margin_expansion": float(margin),
        }

    def get_latest_fundamentals(self, symbol: str, current_date: date) -> Dict[str, float]:
        """Returns the fundamental metrics legally knowable on current_date."""
        if not self.historical_data.empty:
            df = self.historical_data[(self.historical_data["symbol"] == symbol) & (self.historical_data["filing_date"] <= pd.Timestamp(current_date))]
            if not df.empty:
                latest = df.sort_values(by="filing_date").iloc[-1]
                return {
                    "roce_ttm": float(latest.get("roce_ttm", 0.0)),
                    "eps_growth_yoy": float(latest.get("eps_growth_yoy", 0.0)),
                    "de_ratio": float(latest.get("de_ratio", 0.0)),
                    "promoter_pledge_pct": float(latest.get("promoter_pledge_pct", 0.0)),
                    "revenue_growth_yoy": float(latest.get("revenue_growth_yoy", 0.0)),
                    "margin_expansion": float(latest.get("margin_expansion", 0.0)),
                }
                
        # Fallback to stylized if no historical data
        q = "12-31"
        for q_str in reversed(self.quarters):
            q_date = pd.Timestamp(f"{current_date.year}-{q_str}")
            if q_date + pd.Timedelta(days=45) <= pd.Timestamp(current_date):
                q = q_str
                break
                
        return self._generate_stylized_metrics(symbol, current_date.year, q)

    def compute_fcs_for_universe(self, symbols: list, current_date: date) -> Dict[str, float]:
        """Computes the Fundamental Composite Score (FCS) for a cross-section of symbols."""
        fcs_scores = {}
        t = thresholds.get("fundamental", {})
        
        for sym in symbols:
            metrics = self.get_latest_fundamentals(sym, current_date)
            
            # Simple scoring
            score = 0.0
            if metrics["roce_ttm"] >= t.get("min_roe", 15.0): score += 30.0
            if metrics["eps_growth_yoy"] >= t.get("min_eps_cagr_3y", 10.0): score += 30.0
            if metrics["de_ratio"] <= t.get("max_debt_equity", 1.0): score += 20.0
            if metrics["promoter_pledge_pct"] == 0.0: score += 20.0
            
            fcs_scores[sym] = score
            
        return fcs_scores
