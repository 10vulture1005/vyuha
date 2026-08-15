from datetime import datetime, timezone
import pandas as pd
from decimal import Decimal
from loguru import logger

from db.session import get_session
from db.models import PortfolioHolding, HoldingStatus
from core.stop_loss_engine import compute_new_trailing_stop
from core.capital_allocator import execute_sell, execute_partial_sell
from config import thresholds
from config.settings import settings
from agents.tools.ta_tools import load_ohlc_df, get_atr

class RiskExitAgent:
    def __init__(self):
        pass

    def evaluate_exits(self):
        logger.info("Evaluating stops and exits...")
        with get_session() as session:
            open_holdings = session.query(PortfolioHolding).filter(PortfolioHolding.status == HoldingStatus.OPEN.value).all()
            
            for h in open_holdings:
                df = load_ohlc_df(h.symbol)
                if df.empty:
                    continue
                    
                current_close = Decimal(str(df['Close'].iloc[-1]))
                current_atr = Decimal(str(get_atr(df, 14)))
                
                # Check trailing stop breach
                if current_close < h.trailing_stop_price:
                    execute_sell(session, h.symbol, h.qty, current_close, f"Trailing stop breached at {current_close}")
                    continue
                    
                # Trailing stop ratcheting
                t = thresholds.get("technical", {})
                
                dyn_mult = None
                if t.get("use_volatility_scaled_atr", False):
                    try:
                        from scipy import stats
                        tr = pd.concat([
                            df["High"] - df["Low"],
                            (df["High"] - df["Close"].shift()).abs(),
                            (df["Low"] - df["Close"].shift()).abs()
                        ], axis=1).max(axis=1)
                        atr_series = tr.rolling(14).mean()
                        recent_60d_atr = atr_series.tail(60).dropna()
                        if not recent_60d_atr.empty:
                            pctile = stats.percentileofscore(recent_60d_atr, float(current_atr))
                            mult = 1.5 + (pctile / 100.0) * (3.0 - 1.5)
                            dyn_mult = Decimal(str(round(mult, 2)))
                    except Exception as e:
                        logger.debug(f"Failed to compute volatility multiplier for {h.symbol}: {e}")
                
                atr_mult = dyn_mult if dyn_mult else Decimal(str(t.get("atr_multiplier", 2.5)))
                new_stop = compute_new_trailing_stop(current_close, current_atr, h.trailing_stop_price, atr_mult)
                if new_stop > h.trailing_stop_price:
                    h.trailing_stop_price = new_stop
                    
                if settings.EXIT_MODE == "full":
                    # Time Stop
                    risk = thresholds.get("risk", {})
                    time_stop_days = risk.get("time_stop_days", 12)
                    buy_date = pd.Timestamp(h.first_buy_date)
                    if buy_date in df.index:
                        bars_since = len(df.loc[buy_date:])
                        if bars_since >= time_stop_days:
                            r_mult = (current_close - h.avg_buy_price) / h.initial_risk if h.initial_risk > 0 else Decimal("0")
                            if r_mult < Decimal(str(risk.get("time_stop_progress_r", 1.0))):
                                execute_sell(session, h.symbol, h.qty, current_close, f"Time stop: {bars_since} days, only {r_mult:.2f}R")
                                continue
                                
                    # Profit taking tiers
                    pt_tiers = risk.get("profit_tiers", [])
                    hit_list = h.tiers_hit if h.tiers_hit else []
                    for tier_idx, tier in enumerate(pt_tiers):
                        if tier_idx in hit_list:
                            continue
                        r_target = Decimal(str(tier.get("r_multiple", 3.0)))
                        target_price = h.avg_buy_price + (r_target * h.initial_risk)
                        
                        if current_close >= target_price:
                            sell_pct = Decimal(str(tier.get("sell_fraction", 0.33)))
                            qty_to_sell = int(h.initial_qty * sell_pct)
                            if qty_to_sell > h.qty:
                                qty_to_sell = h.qty
                                
                            if qty_to_sell > 0:
                                execute_partial_sell(session, h.symbol, qty_to_sell, current_close, f"Profit target {r_target}R hit")
                                hit_list.append(tier_idx)
                                
                    h.tiers_hit = list(hit_list)
                session.add(h)
                
            session.commit()
