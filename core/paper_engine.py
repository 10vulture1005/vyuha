# core/paper_engine.py
"""Live forward-testing execution harness (zero-risk paper fills).

Executes the full VYUHA pipeline against real-time market prices via yfinance
without transmitting any orders to a broker. All state mutations (capital
ledger, holdings, trade log) are persisted to the same database as production,
enabling the Phase 13 dashboard to render live forward-test performance.

Safety Guard:
    ForwardTestEngine refuses to instantiate if LIVE_TRADING_ENABLED is True.
"""
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Dict, Any, List, Optional
from loguru import logger

from db.session import get_session
from db.models import (
    PortfolioHolding,
    CapitalLedger,
    TradeLog,
    Watchlist,
    HoldingStatus,
    WatchlistStatus,
    TechnicalSignal,
    PortfolioValueHistory,
)
from core.capital_allocator import (
    credit_monthly_sip_execution,
    select_and_execute_buy_candidate,
    execute_sell,
    get_current_cash,
)
from core.stop_loss_engine import compute_new_trailing_stop, is_stop_breached
from config import thresholds
from config.settings import settings


class ForwardTestEngine:
    """Executes live forward-testing runs against real-time market prices without broker execution.

    Usage:
        engine = ForwardTestEngine()
        result = engine.run_daily_paper_cycle()
    """

    def __init__(self):
        if settings.LIVE_TRADING_ENABLED:
            raise RuntimeError(
                "CRITICAL: ForwardTestEngine cannot run when LIVE_TRADING_ENABLED is True."
            )
        logger.info("Initialized ForwardTestEngine in PAPER TRADING mode.")

    def fetch_live_eod_price(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Fetches today's live EOD close price and approximate 14-day ATR using yfinance.

        Args:
            symbol: NSE ticker symbol (e.g., "RELIANCE"). ".NS" suffix is added automatically.

        Returns:
            Tuple of (latest_close_price, atr_14_estimate) as Decimals.

        Raises:
            ValueError: If no price history is returned.
            ImportError: If yfinance is not installed.
        """
        try:
            import yfinance as yf
        except ImportError:
            raise ImportError(
                "yfinance is required for live forward testing. "
                "Install with: pip install yfinance"
            )

        ticker_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
        try:
            ticker = yf.Ticker(ticker_sym)
            df = ticker.history(period="1mo")
            if df.empty or len(df) < 5:
                raise ValueError(f"No price history returned for {ticker_sym}")

            latest_close = Decimal(str(round(df["Close"].iloc[-1], 2)))
            # FIX: full 3-component True Range (was High-Low only, which
            # understated ATR, tightened stops, and caused premature stop-outs).
            # Matches ta_tools.get_atr and backtest/engine.py:243-247.
            import pandas as _pd
            tr = _pd.concat([
                df["High"] - df["Low"],
                (df["High"] - df["Close"].shift()).abs(),
                (df["Low"] - df["Close"].shift()).abs()
            ], axis=1).max(axis=1)
            atr_val = Decimal(str(round(tr.tail(14).mean(), 2)))
            return latest_close, atr_val
        except Exception as e:
            logger.error(
                f"Failed to fetch live price for {symbol} via yfinance: {e}"
            )
            raise

    def run_daily_paper_cycle(self) -> Dict[str, Any]:
        """Executes a complete daily forward-testing evaluation cycle.

        Steps:
            1. Credit SIP on 1st of month
            2. Review open holdings against ATR trailing stops
            3. Execute capital allocation synthesis
            4. Record daily portfolio valuation to portfolio_value_history

        Returns:
            Dict with the allocation decision result.
        """
        logger.info("Starting Daily Forward Test Paper Cycle...")

        # 1. Check if today is the 1st of the month -> Credit ₹1,000 SIP
        today = date.today()
        if today.day == 1:
            credit_monthly_sip_execution(settings.MONTHLY_SIP_AMOUNT)
            logger.info(f"Credited monthly SIP: ₹{settings.MONTHLY_SIP_AMOUNT}")

        # 2. Review Open Holdings against Trailing Stops
        with get_session() as session:
            holdings = (
                session.query(PortfolioHolding)
                .filter(PortfolioHolding.status == HoldingStatus.OPEN.value)
                .all()
            )
            for h in holdings:
                try:
                    close_price, atr_val = self.fetch_live_eod_price(h.symbol)
                except Exception:
                    continue  # Skip evaluation if price feed temporarily fails

                # Volatility-Scaled ATR Multiplier (matches backtest engine)
                dyn_mult = None
                if thresholds.get("technical", {}).get("use_volatility_scaled_atr", False):
                    try:
                        dyn_mult = self._compute_volatility_scaled_multiplier(h.symbol, atr_val)
                    except Exception as e:
                        logger.debug(f"Volatility scaling unavailable for {h.symbol}: {e}")

                new_stop = compute_new_trailing_stop(
                    close_price, atr_val, h.trailing_stop_price, atr_multiplier=dyn_mult
                )
                if new_stop > h.trailing_stop_price:
                    h.trailing_stop_price = new_stop
                    session.add(h)

                if is_stop_breached(close_price, h.trailing_stop_price):
                    rat = (
                        f"Forward Test Stop Breached: Live Close ₹{close_price} "
                        f"< Stop ₹{h.trailing_stop_price}"
                    )
                    execute_sell(session, h.symbol, h.qty, close_price, rat)
                    continue

                # EXIT_MODE gate: time-stop and profit-taking only when mode is "full"
                if settings.EXIT_MODE == "full":
                    self._evaluate_full_exits(session, h, close_price)

        # 3. Execute Capital Allocation Synthesis
        decision = select_and_execute_buy_candidate()

        # 4. Record Daily Portfolio Valuation
        self._record_daily_valuation()

        logger.info(f"Forward test allocation cycle complete. Result: {decision}")
        return decision.model_dump()

    def _record_daily_valuation(self):
        """Writes a daily mark-to-market snapshot to portfolio_value_history."""
        with get_session() as session:
            cash = get_current_cash(session)
            holdings = (
                session.query(PortfolioHolding)
                .filter(PortfolioHolding.status == HoldingStatus.OPEN.value)
                .all()
            )

            invested_val = Decimal("0")
            for h in holdings:
                try:
                    close_price, _ = self.fetch_live_eod_price(h.symbol)
                    invested_val += close_price * Decimal(str(h.qty))
                except Exception:
                    # Fall back to avg buy price if live price unavailable
                    # FIX: loud warning — silent fallback previously masked real
                    # drawdowns as 0 when the feed failed.
                    logger.warning(f"Price feed failed for {h.symbol}: valuing at cost, drawdown understated.")
                    invested_val += h.avg_buy_price * Decimal(str(h.qty))

            total_val = cash + invested_val

            # Compute drawdown against historical peak
            from sqlalchemy import func

            peak_row = (
                session.query(func.max(PortfolioValueHistory.total_value))
                .scalar()
            )
            peak_val = peak_row if peak_row else total_val
            drawdown_pct = (
                ((total_val - peak_val) / peak_val * Decimal("100"))
                if peak_val > 0
                else Decimal("0")
            )

            snapshot = PortfolioValueHistory(
                date=date.today(),
                total_value=total_val,
                cash_balance=cash,
                invested_value=invested_val,
                drawdown_pct=drawdown_pct,
            )
            session.merge(snapshot)  # merge to handle re-runs on same day

    def _compute_volatility_scaled_multiplier(self, symbol: str, current_atr: Decimal) -> Optional[Decimal]:
        """Computes a dynamic ATR multiplier based on recent 60-day volatility percentile."""
        try:
            import yfinance as yf
            import pandas as pd
            from scipy import stats
        except ImportError:
            logger.debug("Missing dependencies (yfinance/pandas/scipy) for volatility scaling.")
            return None

        ticker_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
        try:
            ticker = yf.Ticker(ticker_sym)
            df = ticker.history(period="3mo") # Need at least 60 days + 14 days for rolling ATR
            if df.empty or len(df) < 75:
                return None

            tr = pd.concat([
                df["High"] - df["Low"],
                (df["High"] - df["Close"].shift()).abs(),
                (df["Low"] - df["Close"].shift()).abs()
            ], axis=1).max(axis=1)
            atr_series = tr.rolling(14).mean()
            
            recent_60d_atr = atr_series.tail(60).dropna()
            if recent_60d_atr.empty:
                return None
                
            pctile = stats.percentileofscore(recent_60d_atr, float(current_atr))
            mult = 1.5 + (pctile / 100.0) * (3.0 - 1.5)
            return Decimal(str(round(mult, 2)))
        except Exception as e:
            logger.debug(f"Failed to compute volatility multiplier for {symbol}: {e}")
            return None

    def _evaluate_full_exits(self, session, holding: PortfolioHolding, current_close: Decimal):
        """Time-stops and partial profit-taking for EXIT_MODE='full'.

        FIX: previously a `pass` no-op, so forward-test holdings never
        time-stopped (SCHNEIDER held 24d past the 12d stop) and never trailed
        to breakeven — diverging from both risk_exit_agent and the backtest.
        Mirrors backtest/engine.py:269-311: 12d stagnant band -0.5R..+0.5R,
        one profit tier per day, breakeven ratchet on first hit.
        """
        from core.capital_allocator import execute_sell, execute_partial_sell
        risk = thresholds.get("risk", {}) or {}
        # — Time stop (weekend-safe trading-day count) —
        try:
            import yfinance as _yf
            import pandas as _pd
            ticker_sym = f"{holding.symbol}.NS" if not holding.symbol.endswith(".NS") else holding.symbol
            df = _yf.Ticker(ticker_sym).history(period="3mo")
            if df is not None and not df.empty:
                buy_ts = _pd.Timestamp(holding.first_buy_date)
                try:
                    idx = df.index.searchsorted(buy_ts)
                    bars_since = len(df) - int(idx)
                except Exception:
                    bars_since = 0
                if bars_since >= int(risk.get("time_stop_days", 12)):
                    denom = holding.initial_risk if holding.initial_risk and holding.initial_risk > 0 else None
                    r_mult = (current_close - holding.avg_buy_price) / denom if denom else Decimal("0")
                    if Decimal("-0.5") <= r_mult <= Decimal("0.5"):
                        execute_sell(session, holding.symbol, holding.qty, current_close,
                                     f"Time stop: {bars_since} trading days stagnant at {r_mult:.2f}R")
                        return
        except Exception as e:
            logger.debug(f"Paper time-stop unavailable for {holding.symbol}: {e}")
        # — Profit tiers (both key spellings) + breakeven ratchet —
        try:
            tiers = risk.get("profit_tiers", []) or risk.get("profit_taking", {}).get("tiers", [])
            breakeven_cfg = bool(risk.get("profit_taking", {}).get("trail_to_breakeven_on_first_target", True))
            hit_list = list(holding.tiers_hit) if holding.tiers_hit else []
            for tier_idx, tier in enumerate(tiers):
                if tier_idx in hit_list:
                    continue
                r_target = Decimal(str(tier.get("r_multiple", 3.0)))
                if not holding.initial_risk or holding.initial_risk <= 0:
                    break
                target = holding.avg_buy_price + r_target * holding.initial_risk
                if current_close >= target:
                    frac_raw = tier.get("sell_fraction", tier.get("sell_pct", 33.0))
                    frac = Decimal(str(frac_raw))
                    if frac > 1:
                        frac = frac / Decimal("100")
                    qty_to_sell = int(holding.initial_qty * frac)
                    if qty_to_sell > holding.qty:
                        qty_to_sell = holding.qty
                    if qty_to_sell > 0:
                        execute_partial_sell(session, holding.symbol, qty_to_sell, current_close,
                                             f"Paper profit target {r_target}R hit")
                        hit_list.append(tier_idx)
                        holding.tiers_hit = list(hit_list)
                        if tier_idx == 0 and breakeven_cfg:
                            holding.trailing_stop_price = max(holding.trailing_stop_price, holding.avg_buy_price)
                        session.add(holding)
                    break  # one tier per day
        except Exception as e:
            logger.debug(f"Paper profit-tier evaluation failed for {holding.symbol}: {e}")

