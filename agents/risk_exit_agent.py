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
                
                # FIX: keyword arg. Previously `atr_mult` was passed positionally
                # into `is_bull_regime` (truthy Decimal → always bull 2.5x),
                # silently killing volatility-scaled stops. Matches backtest
                # engine.py:262-264 which passes atr_multiplier= correctly.
                atr_mult = dyn_mult if dyn_mult else Decimal(str(t.get("atr_multiplier", 2.5)))
                new_stop = compute_new_trailing_stop(current_close, current_atr, h.trailing_stop_price, atr_multiplier=atr_mult)
                if new_stop > h.trailing_stop_price:
                    h.trailing_stop_price = new_stop
                    
                if settings.EXIT_MODE == "full":
                    # Time Stop — FIX: (a) weekend/holiday buy_dates are never
                    # `in df.index`, so the old exact-match gate never fired;
                    # use searchsorted trading-day count. (b) band aligned with
                    # backtest engine.py:274-280 (-0.5R..+0.5R); the old
                    # `< 1.0R` sold fresh winners prematurely.
                    risk = thresholds.get("risk", {})
                    time_stop_days = int(risk.get("time_stop_days", 12))
                    buy_date = pd.Timestamp(h.first_buy_date)
                    try:
                        idx = df.index.searchsorted(buy_date)
                        bars_since = len(df) - int(idx)
                    except Exception:
                        bars_since = 0
                    if bars_since >= time_stop_days:
                        r_mult = (current_close - h.avg_buy_price) / h.initial_risk if h.initial_risk and h.initial_risk > 0 else Decimal("0")
                        if Decimal("-0.5") <= r_mult <= Decimal("0.5"):
                            execute_sell(session, h.symbol, h.qty, current_close, f"Time stop: {bars_since} trading days stagnant at {r_mult:.2f}R")
                            continue
                                
                    # Profit taking tiers — FIX: accept both live `risk.profit_tiers`
                    # (sell_fraction) and backtest `risk.profit_taking.tiers`
                    # (sell_pct) keys; one tier per day; ratchet to breakeven on
                    # first hit per thresholds.yaml (previously never moved).
                    pt_tiers = risk.get("profit_tiers", []) or risk.get("profit_taking", {}).get("tiers", [])
                    breakeven_cfg = bool(risk.get("profit_taking", {}).get("trail_to_breakeven_on_first_target", True))
                    hit_list = list(h.tiers_hit) if h.tiers_hit else []
                    for tier_idx, tier in enumerate(pt_tiers):
                        if tier_idx in hit_list:
                            continue
                        r_target = Decimal(str(tier.get("r_multiple", 3.0)))
                        target_price = h.avg_buy_price + (r_target * h.initial_risk) if h.initial_risk else None
                        if target_price is None:
                            break
                        if current_close >= target_price:
                            frac_raw = tier.get("sell_fraction", tier.get("sell_pct", 33.0))
                            sell_frac = Decimal(str(frac_raw))
                            if sell_frac > 1:  # sell_pct (33.0) vs sell_fraction (0.33)
                                sell_frac = sell_frac / Decimal("100")
                            # initial_qty is now stable (no pyramiding overwrite)
                            qty_to_sell = int(h.initial_qty * sell_frac)
                            if qty_to_sell > h.qty:
                                qty_to_sell = h.qty
                            if qty_to_sell > 0:
                                execute_partial_sell(session, h.symbol, qty_to_sell, current_close, f"Profit target {r_target}R hit")
                                hit_list.append(tier_idx)
                                if tier_idx == 0 and breakeven_cfg:
                                    h.trailing_stop_price = max(h.trailing_stop_price, h.avg_buy_price)
                                break  # one tier per day (matches backtest)
                                
                    h.tiers_hit = list(hit_list)
                session.add(h)
                
            session.commit()
