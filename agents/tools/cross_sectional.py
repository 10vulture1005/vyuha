# agents/tools/cross_sectional.py
import pandas as pd
from typing import List, Dict, Any
from config import thresholds

def compute_csa_scores(daily_signals: List[Dict[str, Any]], ohlc_matrix: Dict[str, pd.DataFrame], current_date: pd.Timestamp) -> List[Dict[str, Any]]:
    """Computes the Cross-Sectional Alpha (CSA) for a list of daily signals to resolve tie-breakers.
    
    Formula:
    CSA = w1*z(RelStrength) + w2*z(BreakoutQuality) + w3*z(FCS) + w4*z(RewardRisk)
    """
    if not daily_signals:
        return []
        
    if len(daily_signals) == 1:
        daily_signals[0]["csa"] = 100.0
        return daily_signals

    index_df = ohlc_matrix.get("^CRSLDX")
    idx_3m = 0.0
    if index_df is not None and not index_df.empty:
        idx_slice = index_df.loc[:current_date]
        if len(idx_slice) >= 63:
            idx_3m = (idx_slice["Close"].iloc[-1] / idx_slice["Close"].iloc[-63]) - 1

    # Extract raw values for Z-scoring
    raw_data = []
    for d in daily_signals:
        sym = d["sym"]
        df_slice = ohlc_matrix[sym].loc[:current_date]
        
        # 1. RelStrength
        sym_3m = 0.0
        if len(df_slice) >= 63:
            sym_3m = (df_slice["Close"].iloc[-1] / df_slice["Close"].iloc[-63]) - 1
        rel_strength = sym_3m - idx_3m
        
        # 2. Breakout Quality
        # For BB-Squeeze, we want tight compression (BBW low).
        # For W-Bottom, maybe closer to SMA or volume confirmation.
        # We'll proxy this with inverse BBW percentile or just Volatility.
        # For simplicity across all signals, use (current volume / 20d avg volume)
        vol_20d = df_slice["Volume"].tail(20).mean()
        curr_vol = df_slice["Volume"].iloc[-1]
        breakout_quality = curr_vol / vol_20d if vol_20d > 0 else 1.0
        
        # 3. FCS
        fcs = d.get("fcs", 50.0)
        
        # 4. Reward/Risk
        sig_res = d["sig_res"]
        atr = getattr(sig_res, "atr_14", None)
        entry = getattr(sig_res, "entry_price", None)
        if not atr or not entry or atr == 0:
            # Fallback to dictionary if sig_res is a dict (during some test mocks)
            atr = sig_res.get("atr_14", 1.0) if isinstance(sig_res, dict) else 1.0
            entry = sig_res.get("entry_price", 100.0) if isinstance(sig_res, dict) else 100.0
            
        initial_risk = float(entry) * 0.05 # Approximate if ATR fails
        if atr and atr > 0:
            initial_risk = float(atr) * 2.0
            
        # Target is usually 2R or 3R depending on tier. We use a flat 2.0 proxy for Target
        reward = initial_risk * 2.0
        reward_risk = reward / initial_risk if initial_risk > 0 else 1.0
        
        raw_data.append({
            "sym": sym,
            "rel_strength": rel_strength,
            "breakout_quality": breakout_quality,
            "fcs": fcs,
            "reward_risk": reward_risk
        })
        
    df = pd.DataFrame(raw_data).set_index("sym")
    
    # Z-Score cross-sectionally
    z_df = (df - df.mean()) / df.std()
    z_df = z_df.fillna(0) # Handle NaN if std is 0
    
    csa_weights = thresholds.get("technical", {}).get("csa_weights", {})
    w_rs = csa_weights.get("w_rel_strength", 0.25)
    w_bq = csa_weights.get("w_breakout_quality", 0.25)
    w_fcs = csa_weights.get("w_fcs", 0.25)
    w_rr = csa_weights.get("w_reward_risk", 0.25)
    
    csa_scores = (
        z_df["rel_strength"] * w_rs +
        z_df["breakout_quality"] * w_bq +
        z_df["fcs"] * w_fcs +
        z_df["reward_risk"] * w_rr
    )
    
    # Map back to daily_signals
    csa_dict = csa_scores.to_dict()
    for d in daily_signals:
        d["csa"] = csa_dict.get(d["sym"], 0.0)
        
    return daily_signals
