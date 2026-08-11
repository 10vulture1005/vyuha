import pandas as pd
import numpy as np

def compute_adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Computes Wilder's ADX (Average Directional Index) manually."""
    high = df['High']
    low = df['Low']
    close = df['Close']
    
    # Plus Directional Movement (+DM) and Minus Directional Movement (-DM)
    plus_dm = high.diff()
    minus_dm = low.diff(-1).shift() # low(t-1) - low(t)
    
    plus_dm[plus_dm < 0] = 0
    plus_dm[plus_dm < minus_dm] = 0
    
    minus_dm[minus_dm < 0] = 0
    minus_dm[minus_dm < plus_dm] = 0
    
    # True Range (TR)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Smoothed TR, +DM, -DM (Wilder's Smoothing)
    def wilder_smooth(series, w):
        res = np.full_like(series, fill_value=np.nan)
        if len(series) < w:
            return pd.Series(res, index=series.index)
        # Seed the first value
        res[w-1] = series[:w].sum()
        for i in range(w, len(series)):
            res[i] = res[i-1] - (res[i-1]/w) + series.iloc[i]
        return pd.Series(res, index=series.index)

    atr = wilder_smooth(tr, window)
    plus_di = 100 * (wilder_smooth(plus_dm, window) / atr)
    minus_di = 100 * (wilder_smooth(minus_dm, window) / atr)
    
    # Directional Movement Index (DX)
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
    
    # ADX is Wilder's smoothed DX
    adx = wilder_smooth(dx, window)
    
    return adx

def get_regime(df: pd.DataFrame) -> str:
    """Returns 'TRENDING', 'RANGE_BOUND', or 'NEUTRAL' based on ADX(14)."""
    if len(df) < 30:
        return "NEUTRAL"
        
    adx_series = compute_adx(df, 14)
    if adx_series.empty or pd.isna(adx_series.iloc[-1]):
        return "NEUTRAL"
        
    current_adx = adx_series.iloc[-1]
    
    if current_adx < 20:
        return "RANGE_BOUND"
    elif current_adx > 25:
        return "TRENDING"
    else:
        return "NEUTRAL"

def compute_rsi(series: pd.Series, window: int = 3) -> pd.Series:
    """Computes Relative Strength Index."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def detect_mean_reversion(df: pd.DataFrame) -> dict:
    """
    Mean-Reversion Sleeve Logic:
    Entry on RSI(3) oversold (<30) AND Price > 200 DMA.
    """
    if len(df) < 205:
        return None
        
    rsi = compute_rsi(df["Close"], 3)
    sma_200 = df["Close"].rolling(200).mean()
    
    current_close = df["Close"].iloc[-1]
    current_rsi = rsi.iloc[-1]
    current_sma200 = sma_200.iloc[-1]
    
    if current_rsi < 30 and current_close > current_sma200:
        # Also compute 10 DMA for exit later
        sma_10 = df["Close"].rolling(10).mean().iloc[-1]
        
        return {
            "pattern": "MEAN_REVERSION_DIP",
            "entry_price": current_close,
            "sma_10": sma_10
        }
        
    return None
