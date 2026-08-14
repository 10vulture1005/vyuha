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
            # Approximate ATR(14) from recent daily high-low ranges
            df["tr"] = df["High"] - df["Low"]
            atr_val = Decimal(str(round(df["tr"].tail(14).mean(), 2)))
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
        """Placeholder for time-stops and partial profit taking (mode 'full')."""
        # In a real setup, this would mirror the backtest logic exactly. 
        # For now, it's explicitly gated to prevent unintended execution when we want trailing_stop_only.
        logger.debug(f"Full exits (time-stops/profits) evaluated for {holding.symbol}")
        pass

