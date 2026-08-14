# backtest/engine.py
"""Event-driven chronological backtester replaying exact production allocator logic.

Key Design Decisions:
    - Creates an isolated in-memory SQLite database so backtest state never
      contaminates the live database.
    - Monkey-patches `db.session.get_session` to route all production-module
      DB calls through the backtest-isolated session factory.
    - Steps day-by-day through the historical trading calendar, invoking:
        1. Monthly SIP credits (₹1,000 on first trading day of each month)
        2. ATR trailing stop ratcheting and breach evaluation
        3. Simplified breakout signal generation (5% surge + 1.8x volume proxy)
        4. Production capital allocator buy candidate selection
        5. Daily mark-to-market portfolio valuation
"""
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import List, Dict, Any, Optional
import pandas as pd
from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from db.models import (
    Base,
    Universe,
    Watchlist,
    WatchlistStatus,
    TechnicalSignal,
    PatternType,
    PortfolioHolding,
    CapitalLedger,
    HoldingStatus,
    TradeLog,
)
from core.stop_loss_engine import compute_new_trailing_stop, is_stop_breached
from backtest.data_loader import HistoricalDataLoader
from config import thresholds
from config.settings import settings
import numpy as np
import pandas as pd
from agents.tools.ta_tools import detect_w_bottom, detect_bb_squeeze
from agents.tools.regime_tools import get_regime, detect_mean_reversion
from universe.computed_point_in_time import PointInTimeUniverse
from data.fundamentals_provider import FundamentalsProvider


class BacktestResult:
    """Container for backtest simulation outputs."""

    def __init__(self, start_date: str, end_date: str, initial_cash: float):
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = initial_cash
        self.daily_equity_curve: List[Dict[str, Any]] = []
        self.trade_history: List[Dict[str, Any]] = []
        self.final_metrics: Dict[str, Any] = {}


class EventDrivenBacktester:
    """Chronological event-driven simulator replaying exact production allocator math.

    Usage:
        tester = EventDrivenBacktester("2022-01-01", "2024-12-31", ["RELIANCE", "TCS"])
        result = tester.run()
    """

    def __init__(
        self, start_date: str, end_date: str, symbols: List[str],
        use_computed_universe: bool = False,
        use_regime_filter: bool = False,
        use_multi_tier: bool = False,
        use_cross_sectional: bool = False,
        use_fcs_gate: bool = False,
        use_cash_regime: bool = False,
        exit_mode: str = "full",
    ):
        self.loader = HistoricalDataLoader(start_date, end_date)
        self.symbols = symbols
        self.use_computed_universe = use_computed_universe
        self.use_regime_filter = use_regime_filter
        self.use_multi_tier = use_multi_tier
        self.use_cross_sectional = use_cross_sectional
        self.use_fcs_gate = use_fcs_gate
        self.use_cash_regime = use_cash_regime
        self.exit_mode = exit_mode
        
        self.pit_universe = PointInTimeUniverse() if use_computed_universe else None
        self.fund_provider = FundamentalsProvider() if use_fcs_gate else None

        # Create isolated in-memory SQLite database for backtest state
        self._engine = create_engine("sqlite:///:memory:", echo=False)
        self._SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self._engine
        )

    @contextmanager
    def _get_session(self):
        """Context manager mirroring db.session.get_session for backtest isolation."""
        session = self._SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _init_db(self):
        """Creates all ORM tables in the in-memory database and seeds the universe/watchlist."""
        Base.metadata.create_all(bind=self._engine)
        
        symbols_to_seed = self.symbols
            
        with self._get_session() as session:
            for sym in symbols_to_seed:
                session.add(
                    Universe(
                        symbol=sym,
                        company_name=f"PIT {sym}",
                        sector="Backtest",
                        exchange="NSE",
                    )
                )
                # In basic replay, assume symbols passed fundamental/sentiment filters
                session.add(
                    Watchlist(
                        symbol=sym,
                        status=WatchlistStatus.ACTIVE.value,
                        conviction_score=Decimal("75.00"),
                    )
                )
            
            # Seed INITIAL_CAPITAL as the first ledger entry
            session.add(
                CapitalLedger(
                    amount=Decimal(str(settings.INITIAL_CAPITAL)),
                    txn_type="SIP_CREDIT",
                    running_balance=Decimal(str(settings.INITIAL_CAPITAL)),
                )
            )

    def _inject_session(self):
        """Monkey-patches db.session.SyncSessionLocal to use backtest-isolated engine.

        This ensures that any module calling get_session() will receive
        a session bound to the in-memory backtest DB.
        """
        import db.session as db_session_module

        self._original_session_local = db_session_module.SyncSessionLocal
        db_session_module.SyncSessionLocal = self._SessionLocal

    def _restore_session(self):
        """Restores the original db.session.SyncSessionLocal after backtest completes."""
        import db.session as db_session_module

        db_session_module.SyncSessionLocal = self._original_session_local

    def run(self) -> BacktestResult:
        """Executes the full chronological event-driven backtest replay.

        Returns:
            BacktestResult containing equity curve, trade history, and final metrics.

        Raises:
            RuntimeError: If historical data loading fails.
        """
        symbols_to_load = self.symbols
            
        if not self.loader.load_universe_data(symbols_to_load):
            raise RuntimeError("Data loading failed.")

        self._init_db()
        self._inject_session()

        try:
            return self._execute_replay()
        finally:
            self._restore_session()

    def _execute_replay(self) -> BacktestResult:
        """Inner replay loop — called after session injection is active."""
        # Import production modules AFTER session injection so they use backtest DB
        from core.capital_allocator import (
            credit_monthly_sip_execution,
            select_and_execute_buy_candidate,
            execute_sell,
            execute_partial_sell,
            get_current_cash,
        )

        result = BacktestResult(
            str(self.loader.start_date.date()),
            str(self.loader.end_date.date()),
            settings.INITIAL_CAPITAL,
        )

        current_month = -1
        month_start_equity = Decimal(str(settings.INITIAL_CAPITAL))
        circuit_breaker_active = False
        logger.info("Starting chronological event loop replay...")

        for current_date in self.loader.trading_calendar:
            # ── Step 1: Monthly SIP Credit (1st trading day of each month) ──
            if current_date.month != current_month:
                current_month = current_date.month
                credit_monthly_sip_execution(settings.MONTHLY_SIP_AMOUNT)
                if result.daily_equity_curve:
                    month_start_equity = Decimal(str(result.daily_equity_curve[-1]["total_value"]))
                circuit_breaker_active = False # Reset circuit breaker at month start
                
            # Compute current MTD drawdown
            if result.daily_equity_curve:
                current_equity = Decimal(str(result.daily_equity_curve[-1]["total_value"]))
                if month_start_equity > 0:
                    mtd_drawdown = ((current_equity - month_start_equity) / month_start_equity) * 100
                    if mtd_drawdown <= -Decimal(str(settings.MAX_MONTHLY_DRAWDOWN_PCT)):
                        circuit_breaker_active = True

            with self._get_session() as session:
                # ── Step 2: Risk & Exit Review on Open Holdings ──
                holdings = (
                    session.query(PortfolioHolding)
                    .filter(PortfolioHolding.status == HoldingStatus.OPEN.value)
                    .all()
                )
                for h in holdings:
                    df_slice = self.loader.ohlc_matrix[h.symbol].loc[:current_date]
                    if df_slice.empty:
                        continue
                    bar = df_slice.iloc[-1]
                    current_close = Decimal(str(bar["Close"]))
                    
                    # Slippage Realism (Phase F)
                    spread_proxy = Decimal(str((bar["High"] - bar["Low"]) / bar["Close"])) if bar["Close"] > 0 else Decimal("0.01")
                    slippage_pct = spread_proxy * Decimal("0.10")  # 10% of daily spread
                    fill_price_sell = current_close * (Decimal("1") - slippage_pct)
                    
                    # ATR Scaling (Phase E)
                    tr = pd.concat([
                        df_slice["High"] - df_slice["Low"],
                        (df_slice["High"] - df_slice["Close"].shift()).abs(),
                        (df_slice["Low"] - df_slice["Close"].shift()).abs()
                    ], axis=1).max(axis=1)
                    atr_series = tr.rolling(14).mean()
                    current_atr = Decimal(str(atr_series.iloc[-1])) if atr_series is not None and not atr_series.empty and not pd.isna(atr_series.iloc[-1]) else (current_close * Decimal("0.03"))
                    
                    dyn_mult = None
                    if thresholds["technical"].get("use_volatility_scaled_atr", False) and len(atr_series) >= 60:
                        recent_60d_atr = atr_series.tail(60).dropna()
                        if not recent_60d_atr.empty:
                            from scipy import stats
                            # Percentile of current ATR within last 60 days
                            pctile = stats.percentileofscore(recent_60d_atr, float(current_atr))
                            # Map 0-100 percentile to 1.5 - 3.0 multiplier (higher vol -> wider stop)
                            mult = 1.5 + (pctile / 100.0) * (3.0 - 1.5)
                            dyn_mult = Decimal(str(round(mult, 2)))

                    new_stop = compute_new_trailing_stop(
                        current_close, current_atr, h.trailing_stop_price, atr_multiplier=dyn_mult
                    )
                    if new_stop > h.trailing_stop_price:
                        h.trailing_stop_price = new_stop
                        session.add(h)

                    # Phase 3.3: Time-in-Trade Stop
                    if self.exit_mode == "full":
                        time_stop_days = thresholds.get("risk", {}).get("time_stop_days", 12)
                        # use len of df_slice since first_buy_date to count exact trading days
                        buy_date = pd.Timestamp(h.first_buy_date)
                        bars_since_buy = len(df_slice.loc[buy_date:])
                        if bars_since_buy >= time_stop_days:
                            # Check unrealized PnL in R-multiples
                            r_mult = (current_close - h.avg_buy_price) / h.initial_risk if h.initial_risk > 0 else Decimal("0")
                            if Decimal("-0.5") <= r_mult <= Decimal("0.5"):
                                execute_sell(session, h.symbol, h.qty, fill_price_sell, f"Time-Stop Breached ({bars_since_buy} days stagnant at {r_mult:.2f}R)")
                                continue
    
                        # Profit Taking (Phase D & L)
                        pt = thresholds.get("risk", {}).get("profit_taking", thresholds.get("technical", {}).get("profit_taking", {}))
                        if pt and h.initial_risk > 0:
                            if self.use_multi_tier:
                                tiers = pt.get("tiers", [])
                                hit_list = h.tiers_hit if h.tiers_hit is not None else []
                                for tier_idx, tier in enumerate(tiers):
                                    if tier_idx in hit_list:
                                        continue
                                    r_mult = Decimal(str(tier.get("r_multiple", 1.0)))
                                    target_price = h.avg_buy_price + (r_mult * h.initial_risk)
                                    if current_close >= target_price:
                                        sell_pct = Decimal(str(tier.get("sell_pct", 33.0))) / Decimal("100")
                                        # Phase L: qty to sell is based on original qty
                                        qty_to_sell = int(h.initial_qty * sell_pct)
                                        if qty_to_sell > h.qty:
                                            qty_to_sell = h.qty
                                            
                                        if qty_to_sell > 0:
                                            execute_partial_sell(
                                                session, h.symbol, qty_to_sell, fill_price_sell,
                                                f"Partial Profit Taking {r_mult}R Tier {tier_idx} Hit"
                                            )
                                            # Update JSON array by copying list because SQLAlchemy JSON changes need re-assignment
                                            new_hit_list = list(hit_list)
                                            new_hit_list.append(tier_idx)
                                            h.tiers_hit = new_hit_list
                                            if pt.get("trail_to_breakeven_on_first_target", False):
                                                h.trailing_stop_price = max(h.trailing_stop_price, h.avg_buy_price)
                                            session.add(h)
                                            break # only process one tier per day
                            else:
                                k = Decimal(str(pt.get("k_multiple", 2)))
                                target_price = h.avg_buy_price + (k * h.initial_risk)
                                if current_close >= target_price:
                                    p_pct = Decimal(str(pt.get("p_percent", 50.0))) / Decimal("100")
                                    qty_to_sell = int(h.qty * p_pct)
                                    if qty_to_sell > 0:
                                        execute_partial_sell(
                                            session, h.symbol, qty_to_sell, fill_price_sell,
                                            f"Partial Profit Taking {k}R Target Hit"
                                        )
                                        trail_frac = Decimal(str(pt.get("trail_to_breakeven_fraction", 1.0)))
                                        breakeven_stop = h.avg_buy_price * trail_frac
                                        h.trailing_stop_price = max(h.trailing_stop_price, breakeven_stop)
                                        session.add(h)
                                        continue # Skip stop breach check this day if we took profit

                    if is_stop_breached(current_close, h.trailing_stop_price):
                        rat = (
                            f"Backtest Trailing Stop Breached @ {current_date.date()}"
                        )
                        execute_sell(session, h.symbol, h.qty, fill_price_sell, rat)

                # ── Step 3: Technical Signal Generation (Real TA Integration) ──
                # Phase 3.4: Cash Regime (Market Filter)
                skip_signal_generation = False
                if self.use_cash_regime:
                    nifty_df = self.loader.ohlc_matrix.get("INDEX_NIFTY500") # Using Nifty 500 proxy for Nifty 50 due to our index proxy
                    if nifty_df is not None:
                        idx_slice = nifty_df.loc[:current_date]
                        if len(idx_slice) > 252:
                            # Note: IndiaVIX data isn't natively ingested in our test harness, 
                            # we'll mock VIX via index realized volatility for the simulation.
                            realized_vix = idx_slice["Close"].pct_change().rolling(21).std() * np.sqrt(252) * 100
                            # VIX > 80th percentile trailing 1yr
                            vix_1yr = realized_vix.tail(252)
                            p80 = vix_1yr.quantile(0.8)
                            is_vix_high = realized_vix.iloc[-1] > p80
                            
                            sma200 = idx_slice["Close"].rolling(200).mean().iloc[-1]
                            is_nifty_below_sma = idx_slice["Close"].iloc[-1] < sma200
                            
                            if is_vix_high and is_nifty_below_sma:
                                # Cash Regime: skip generating new signals today
                                skip_signal_generation = True
                                
                if skip_signal_generation:
                    daily_signals = []
                else:
                  current_symbols = self.symbols
                  if self.use_computed_universe and self.pit_universe:
                      current_symbols = self.pit_universe.get_midcap_universe(current_date.date(), top_n=150)

                  # Phase 3.1: FCS Gate
                  fcs_scores = {}
                  if self.use_fcs_gate and self.fund_provider:
                      fcs_scores = self.fund_provider.compute_fcs_for_universe(current_symbols, current_date.date())
                    
                  daily_signals = []
                
                  for sym in current_symbols:
                      if self.use_fcs_gate:
                          # Only allow if FCS > 60th percentile
                          if fcs_scores.get(sym, 0) < 60.0:
                              continue
                            
                      if sym not in self.loader.ohlc_matrix:
                          continue
                      df_slice = self.loader.ohlc_matrix[sym].loc[:current_date]
                      if len(df_slice) < 253:
                          continue
                        
                      sig_res = None
                      if self.use_regime_filter:
                          regime = get_regime(df_slice)
                          if regime == "TRENDING":
                              sig_res = detect_w_bottom(df_slice) or detect_bb_squeeze(df_slice)
                          elif regime == "RANGE_BOUND":
                              # Use Mean-Reversion sleeve
                              mr_sig = detect_mean_reversion(df_slice)
                              if mr_sig:
                                  from agents.tools.ta_tools import TechnicalSignalResult as TSR, PatternType
                                  atr_val = float(df_slice["Close"].iloc[-1] * 0.05)
                                  sig_res = TSR(
                                      pattern_type=PatternType.MEAN_REVERSION,
                                      atr_14=atr_val,
                                      entry_price=float(mr_sig["entry_price"]),
                                      structural_stop_price=float(mr_sig["entry_price"]) - atr_val * 2.0,
                                      signal_strength=6.0,
                                      vol_ratio=1.0,
                                      momentum_composite=0.5,
                                  )
                      else:
                          sig_res = detect_w_bottom(df_slice) or detect_bb_squeeze(df_slice)
                    
                      if sig_res:
                          # Slippage Realism (Phase F) for Buy
                          bar = df_slice.iloc[-1]
                          spread_proxy = Decimal(str((bar["High"] - bar["Low"]) / bar["Close"])) if bar["Close"] > 0 else Decimal("0.01")
                          slippage_pct = spread_proxy * Decimal("0.10")
                          fill_price_buy = Decimal(str(sig_res.entry_price)) * (Decimal("1") + slippage_pct)
                        
                          daily_signals.append({
                              "sym": sym,
                              "sig_res": sig_res,
                              "fill_price_buy": fill_price_buy,
                              "fcs": fcs_scores.get(sym, 50.0) # Used in Phase 3 CSA
                          })
                        
                  # Phase 3.2: Cross-Sectional Alpha (CSA) Quality Filter
                  if self.use_cross_sectional and daily_signals:
                      from agents.tools.cross_sectional import compute_csa_scores
                      daily_signals = compute_csa_scores(daily_signals, self.loader.ohlc_matrix, current_date)
                      # Sort by CSA score
                      daily_signals.sort(key=lambda x: x.get("csa", -999), reverse=True)
                      # For a single slot system, we only need the top 1 or 2, but we keep the top quartile 
                      # in the daily signals pool for the allocator to pick from
                      top_n = max(1, len(daily_signals) // 4)
                      daily_signals = daily_signals[:top_n]
                            
                for d in daily_signals:
                    sym = d["sym"]
                    sig_res = d["sig_res"]
                    fill_price_buy = d["fill_price_buy"]
                    # Handle mocked enum or raw string from Phase K hack
                    pattern_val = getattr(sig_res.pattern_type, "value", sig_res.pattern_type)
                    
                    # Compute structural_stop_price with slippage
                    raw_stop = Decimal(str(sig_res.structural_stop_price)) if hasattr(sig_res, 'structural_stop_price') and sig_res.structural_stop_price else (fill_price_buy - Decimal(str(sig_res.atr_14)) * Decimal("2.5"))
                    
                    sig = TechnicalSignal(
                        symbol=sym,
                        signal_date=current_date.date(),
                        pattern_type=pattern_val,
                        atr_14=Decimal(str(sig_res.atr_14)),
                        entry_price=round(fill_price_buy, 4),
                        structural_stop_price=round(raw_stop, 4),
                        signal_strength=Decimal(str(round(sig_res.momentum_composite * 100, 2))) if getattr(sig_res, 'momentum_composite', None) else Decimal("50.00"),
                    )
                    session.add(sig)

            # ── Step 4: Capital Allocation Synthesis ──
            # Pass simulation date to the allocator so it queries signals for
            # the current backtest day (not the real system date)
            select_and_execute_buy_candidate(signal_date=current_date.date(), circuit_breaker_active=circuit_breaker_active)

            # ── Step 5: Daily Mark-to-Market Valuation ──
            with self._get_session() as session:
                cash = get_current_cash(session)
                open_holdings = (
                    session.query(PortfolioHolding)
                    .filter(PortfolioHolding.status == HoldingStatus.OPEN.value)
                    .all()
                )
                invested_val = Decimal("0")
                for h in open_holdings:
                    bar = self.loader.get_bar(h.symbol, current_date)
                    price = (
                        Decimal(str(bar["Close"]))
                        if bar is not None
                        else h.avg_buy_price
                    )
                    invested_val += price * Decimal(str(h.qty))
                total_val = cash + invested_val

                result.daily_equity_curve.append(
                    {
                        "date": current_date.date().isoformat(),
                        "total_value": float(total_val),
                        "cash_balance": float(cash),
                        "invested_value": float(invested_val),
                    }
                )

        # ── Capture Full Trade History ──
        with self._get_session() as session:
            trades = session.query(TradeLog).order_by(TradeLog.id.asc()).all()
            for t in trades:
                result.trade_history.append(
                    {
                        "id": t.id,
                        "symbol": t.symbol,
                        "type": t.txn_type.lower(),
                        "qty": t.qty,
                        "price": float(t.price),
                        "date": str(t.txn_date),
                        "dp_charge": float(t.dp_charge),
                        "friction_charge": float(t.friction_charge),
                        "realized_pnl": float(t.realized_pnl) if t.realized_pnl else 0.0,
                        "realized_r": float(t.realized_r) if t.realized_r else None,
                        "reason": t.reason,
                    }
                )

        # ── Compute Final Metrics ──
        if result.daily_equity_curve:
            # 1. CAGR (use true initial capital, not day-1 equity which includes SIP credit)
            start_eq = settings.INITIAL_CAPITAL
            end_eq = result.daily_equity_curve[-1]["total_value"]
            years = len(result.daily_equity_curve) / 252.0
            if years > 0 and start_eq > 0:
                result.final_metrics["cagr_pct"] = ((end_eq / start_eq) ** (1 / years) - 1) * 100
            else:
                result.final_metrics["cagr_pct"] = 0.0
                
            # 2. Max Drawdown
            eq_curve = pd.Series([d["total_value"] for d in result.daily_equity_curve])
            peak = eq_curve.cummax()
            drawdown = (eq_curve - peak) / peak
            result.final_metrics["max_drawdown_pct"] = drawdown.min() * 100
            
        # 3. Win Rate & Avg R-Multiple
        sell_trades = [t for t in result.trade_history if t["type"] == "sell"]
        if sell_trades:
            wins = sum(1 for t in sell_trades if t.get("realized_pnl", 0) > 0)
            result.final_metrics["win_rate_pct"] = (wins / len(sell_trades)) * 100
            
            r_mults = [t["realized_r"] for t in sell_trades if t.get("realized_r") is not None]
            result.final_metrics["avg_r_multiple"] = sum(r_mults) / len(r_mults) if r_mults else 0.0
        else:
            result.final_metrics["win_rate_pct"] = 0.0
            result.final_metrics["avg_r_multiple"] = 0.0

        logger.info(
            f"Chronological replay complete. "
            f"{len(result.daily_equity_curve)} trading days simulated, "
            f"{len(result.trade_history)} trades executed."
        )
        logger.info(f"Final Metrics: {result.final_metrics}")
        return result
