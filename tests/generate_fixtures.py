import pandas as pd
import numpy as np
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = BASE_DIR / "tests" / "fixtures"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

def create_synthetic_w_bottom(length=100) -> pd.DataFrame:
    """Generates synthetic price action with a symmetric W-Bottom at days 80 and 90."""
    dates = pd.date_range("2025-01-01", periods=length, freq="D")
    prices = np.linspace(100, 80, 70).tolist() # Downtrend
    prices += [75, 78, 80, 85, 82, 79, 75.5, 78, 82, 88] # W-Bottom (troughs at 75 and 75.5, neckline 85)
    prices += np.linspace(88, 95, length - len(prices)).tolist()
    
    volumes = [100000] * 70 + [150000, 120000, 110000, 140000, 100000, 90000, 80000, 95000, 110000, 130000] + [100000] * (length - 80)
    df = pd.DataFrame({"Open": prices, "High": [p*1.01 for p in prices], "Low": [p*0.99 for p in prices], "Close": prices, "Volume": volumes}, index=dates)
    df.index.name = "Date"
    return df

def create_synthetic_bb_squeeze(length=200):
    dates = pd.date_range("2025-01-01", periods=length, freq="D")
    prices = np.linspace(100, 120, 100).tolist()
    prices += np.linspace(120, 120, 80).tolist() # Flat
    prices += np.linspace(120, 130, length - 180).tolist() # Breakout
    volumes = [100000] * length
    df = pd.DataFrame({"Open": prices, "High": [p*1.05 for p in prices], "Low": [p*0.95 for p in prices], "Close": prices, "Volume": volumes}, index=dates)
    
    # Compress volatility
    for i in range(130, 180):
        df.loc[df.index[i], "High"] = df.loc[df.index[i], "Close"] * 1.005
        df.loc[df.index[i], "Low"] = df.loc[df.index[i], "Close"] * 0.995
        
    df.index.name = "Date"
    return df

if __name__ == "__main__":
    df_w = create_synthetic_w_bottom()
    df_w.to_csv(FIXTURES_DIR / "w_bottom_ohlc.csv")
    
    df_bb = create_synthetic_bb_squeeze()
    df_bb.to_csv(FIXTURES_DIR / "bb_squeeze_ohlc.csv")
    print("Fixtures generated successfully.")
