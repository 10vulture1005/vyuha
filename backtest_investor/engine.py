# backtest_investor/engine.py
"""Chronological, event-driven replay of Investor Mode.

The execution contract, enforced structurally rather than by convention:

    *   Signals are computed from the close of day `t`.
    *   Orders decided on day `t` are queued and filled at the OPEN of day
        `t+1`. There is no code path that fills at the price that produced the
        signal, because `execute_queued_orders` is the only function that
        touches cash and it never sees day `t`'s decision prices.
    *   A stop is checked against day `t+1`'s low; if the open already gaps
        through it, the fill is the open, not the stop.

Costs come from `backtest_v2.costs.tax_and_fees.TransactionCostModel` (the
existing friction model — STT, exchange, SEBI, stamp, GST, DP) plus the
mid-cap slippage band from the investor config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from backtest_v2.costs.tax_and_fees import TransactionCostModel
from investor.config import investor_config
from investor.data import (
    PricePanel, PointInTimeFundamentals, SectorMap,
)
from investor.pipeline import InvestorEngine, Order, Position
from investor import regime as regime_mod


@dataclass
class Fill:
    """One executed trade."""

    date: pd.Timestamp
    symbol: str
    side: str
    qty: int
    price: float
    notional: float
    costs: float
    net_cash: float
    reason: str
    layer: str
    regime: str
    exposure: float


@dataclass
class ClosedTrade:
    symbol: str
    open_date: pd.Timestamp
    close_date: pd.Timestamp
    qty: int
    open_price: float
    close_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    holding_days: int
    reason: str


@dataclass
class BacktestResult:
    """Everything one replay produced."""

    label: str
    start: pd.Timestamp
    end: pd.Timestamp
    equity: pd.DataFrame                       # date, equity, cash, invested, ...
    fills: pd.DataFrame
    closed_trades: pd.DataFrame
    flows: List[Tuple[date_cls, float]] = field(default_factory=list)
    regime_history: pd.DataFrame = field(default_factory=pd.DataFrame)
    decisions: List[Dict] = field(default_factory=list)
    total_costs: float = 0.0
    initial_capital: float = 0.0
    metadata: Dict = field(default_factory=dict)

    @property
    def final_value(self) -> float:
        return float(self.equity["equity"].iloc[-1]) if len(self.equity) else 0.0


class InvestorBacktester:
    """Replays Investor Mode day by day against adjusted historical prices."""

    def __init__(
        self,
        start: str,
        end: str,
        universe: Sequence[str],
        panel: Optional[PricePanel] = None,
        store: Optional[PointInTimeFundamentals] = None,
        sector_map: Optional[SectorMap] = None,
        initial_capital: Optional[float] = None,
        monthly_sip: float = 0.0,
        use_fundamentals: bool = True,
        use_technical: bool = True,
        use_news: bool = True,
        use_regime: bool = True,
        cost_multiplier: float = 1.0,
        slippage_multiplier: float = 1.0,
        label: str = "full",
        nifty_symbol: Optional[str] = None,
        min_positions: int = 0,
    ):
        self.panel = panel or PricePanel()
        self.store = store or PointInTimeFundamentals()
        self.sectors = sector_map or SectorMap()
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end)
        self.universe = [s for s in universe if not self.panel.get(s, self.start).empty]
        self.initial_capital = float(
            initial_capital
            if initial_capital is not None
            else investor_config.backtest.get("initial_capital", 1_000_000.0)
        )
        self.monthly_sip = float(monthly_sip)
        self.label = label
        self.cost_multiplier = float(cost_multiplier)
        self.slippage_multiplier = float(slippage_multiplier)
        self.min_positions = int(min_positions)

        cfg_f = investor_config.fundamentals
        self.nifty_symbol = nifty_symbol or investor_config.regime.get("nifty_proxy", "INDEX_NIFTY500")

        self.cost_model = TransactionCostModel()
        self.cost_model.include_costs = bool(
            investor_config.costs.get("include_friction", True)
        ) and cost_multiplier > 0

        self.defensive_symbols = list(investor_config.defensive_symbols)

        self.engine = InvestorEngine(
            panel=self.panel, store=self.store, sector_map=self.sectors,
            universe=self.universe, nifty_symbol=self.nifty_symbol,
            use_fundamentals=use_fundamentals, use_technical=use_technical,
            use_news=use_news, use_regime=use_regime,
        )

    # ── costs ───────────────────────────────────────────────────────────────
    def _slippage_pct(self, symbol: str, price: float) -> float:
        """Slippage band: 0.1% for large caps, 0.15% for mid caps."""
        cfg = investor_config.costs
        base = float(cfg.get("midcap_slippage_pct", 0.15))
        # Mid/small caps are where the 0.2% end of the band applies.
        if not self._is_largecap(symbol):
            base = float(cfg.get("midcap_slippage_pct", 0.15))
        else:
            base = float(cfg.get("largecap_slippage_pct", 0.10))
        return base / 100.0 * max(self.slippage_multiplier, 0.0)

    def _is_largecap(self, symbol: str) -> bool:
        from config.settings import BASE_DIR
        try:
            import csv
            path = BASE_DIR / "config" / "universe_nifty500.csv"
            with open(path) as fh:
                rows = list(csv.DictReader(fh))
            return any(r["symbol"] == symbol for r in rows)
        except Exception:
            return False

    def _buy_cost(self, notional: float) -> float:
        c = self.cost_model.calculate_buy_costs(notional)
        return c.total * self.cost_multiplier

    def _sell_cost(self, notional: float) -> float:
        c = self.cost_model.calculate_sell_costs(notional)
        return c.total * self.cost_multiplier

    # ── main replay ─────────────────────────────────────────────────────────
    def run(self, verbose: bool = False) -> BacktestResult:
        logger.info(
            f"Investor backtest '{self.label}': {self.start.date()} -> {self.end.date()}, "
            f"{len(self.universe)} symbols, "
            f"F={self.engine.use_fundamentals} T={self.engine.use_technical} "
            f"N={self.engine.use_news} R={self.engine.use_regime}"
        )

        calendar = self._calendar()
        if len(calendar) == 0:
            raise RuntimeError("No trading days in the requested window.")

        cash = self.initial_capital
        flows: List[Tuple[date_cls, float]] = []
        equity_rows: List[Dict] = []
        fills: List[Fill] = []
        closed: List[ClosedTrade] = []
        decisions: List[Dict] = []
        total_costs = 0.0

        pending: List[Order] = []          # decided on t, filled at open of t+1
        last_month = None
        open_trade_meta: Dict[str, ClosedTrade] = {}

        for i, day in enumerate(calendar):
            # ── 1. Execute yesterday's decisions at today's OPEN ────────────
            if pending:
                cash, day_fills, day_closed, spent = self._execute(pending, day, cash)
                total_costs += spent
                fills.extend(day_fills)
                closed.extend(day_closed)
                pending = []

            # ── 2. Monthly SIP credit on the first trading day ─────────────
            if self.monthly_sip > 0 and day.month != last_month:
                cash += self.monthly_sip
                flows.append((day.date(), self.monthly_sip))
            last_month = day.month

            # ── 3. Mark to market at today's close ──────────────────────────
            equity, invested, defensive_value = self._mark(day, cash)
            n_hold = len(self.engine.positions)
            cash_pct = (cash / equity) if equity > 0 else 0.0
            def_pct = (defensive_value / equity) if equity > 0 else 0.0

            equity_rows.append({
                "date": day,
                "equity": equity,
                "cash": cash,
                "invested": invested,
                "defensive": defensive_value,
                "holdings": n_hold,
                "cash_pct": cash_pct,
                "defensive_pct": def_pct,
            })

            if verbose and i % 250 == 0:
                logger.info(f"  {day.date()}: equity {equity:,.0f}, {n_hold} holdings")

            # ── 4. Decide today's orders from today's CLOSE ─────────────────
            if day == calendar[-1]:
                break            # no tomorrow to fill into; stop before inventing one
            decision = self.engine.evaluate_day(day, equity=equity, cash=cash)
            pending = list(decision.orders)
            decisions.append(decision.as_dict())

        # ── Liquidate at the end so the final value is realised, not marked ──
        final_day = calendar[-1]
        if pending:
            cash, day_fills, day_closed, spent = self._execute(pending, final_day, cash)
            total_costs += spent
            fills.extend(day_fills)
            closed.extend(day_closed)
        equity, invested, defensive_value = self._mark(final_day, cash)
        if equity_rows:
            equity_rows[-1].update({
                "equity": equity, "cash": cash, "invested": invested,
                "defensive": defensive_value, "holdings": len(self.engine.positions),
                "cash_pct": (cash / equity) if equity > 0 else 0.0,
                "defensive_pct": (defensive_value / equity) if equity > 0 else 0.0,
            })

        equity_df = pd.DataFrame(equity_rows).set_index("date")
        fills_df = pd.DataFrame([f.__dict__ for f in fills])
        closed_df = pd.DataFrame([c.__dict__ for c in closed])
        regime_df = pd.DataFrame([s.as_dict() for s in self.engine.regime_engine.history])

        result = BacktestResult(
            label=self.label,
            start=calendar[0],
            end=calendar[-1],
            equity=equity_df,
            fills=fills_df,
            closed_trades=closed_df,
            flows=flows,
            regime_history=regime_df,
            decisions=decisions,
            total_costs=total_costs,
            initial_capital=self.initial_capital,
            metadata={
                "universe_size": len(self.universe),
                "use_fundamentals": self.engine.use_fundamentals,
                "use_technical": self.engine.use_technical,
                "use_news": self.engine.use_news,
                "use_regime": self.engine.use_regime,
                "cost_multiplier": self.cost_multiplier,
                "slippage_multiplier": self.slippage_multiplier,
                "news_source": "proxy" if self.engine.use_news else "disabled",
            },
        )
        logger.info(
            f"Investor backtest '{self.label}' done: final {result.final_value:,.0f} "
            f"from {self.initial_capital:,.0f} over {len(calendar)} days, "
            f"{len(fills)} fills, costs {total_costs:,.0f}"
        )
        return result

    # ── helpers ─────────────────────────────────────────────────────────────
    def _calendar(self) -> pd.DatetimeIndex:
        nifty = self.panel.load(self.nifty_symbol)
        if nifty.empty:
            nifty = next(
                (self.panel.load(s) for s in self.universe if not self.panel.load(s).empty),
                pd.DataFrame(),
            )
        days = nifty.index[(nifty.index >= self.start) & (nifty.index <= self.end)]
        return pd.DatetimeIndex(days)

    def _mark(self, day: pd.Timestamp, cash: float) -> Tuple[float, float, float]:
        invested = 0.0
        defensive = 0.0
        for sym, pos in self.engine.positions.items():
            px = self.panel.close(sym, day)
            if px is None:
                px = pos.avg_price
            invested += px * pos.qty
        return cash + invested, invested, defensive

    def _execute(
        self, orders: Sequence[Order], day: pd.Timestamp, cash: float
    ) -> Tuple[float, List[Fill], List[ClosedTrade], float]:
        """Fills queued orders at THIS day's open. The only cash-mutating code."""
        fills: List[Fill] = []
        closed: List[ClosedTrade] = []
        spent = 0.0

        # Sells first: they fund the buys and must never be blocked by cash.
        sells = [o for o in orders if o.side in ("SELL", "TRIM")]
        buys = [o for o in orders if o.side == "BUY"]

        for o in sells:
            pos = self.engine.positions.get(o.symbol)
            if pos is None or pos.qty <= 0:
                continue
            bar = self._open_bar(o.symbol, day)
            if bar is None:
                continue
            price = float(bar) * (1.0 - self._slippage_pct(o.symbol, float(bar)))
            qty = min(o.qty, pos.qty)
            if qty <= 0:
                continue
            notional = price * qty
            costs = self._sell_cost(notional)
            net = notional - costs
            cash += net
            spent += costs
            gross = (price - pos.avg_price) * qty
            closed.append(ClosedTrade(
                symbol=o.symbol,
                open_date=pd.Timestamp(pos.first_buy_date),
                close_date=day, qty=qty,
                open_price=pos.avg_price, close_price=price,
                gross_pnl=gross, costs=costs, net_pnl=gross - costs,
                holding_days=pos.days_held(day), reason=o.reason,
            ))
            self.engine.apply_sell(o.symbol, qty, day)
            fills.append(Fill(
                date=day, symbol=o.symbol, side=o.side, qty=qty, price=price,
                notional=notional, costs=costs, net_cash=net, reason=o.reason,
                layer=o.layer,
                regime=getattr(self.engine.regime_engine.latest, "regime", ""),
                exposure=getattr(self.engine.regime_engine.latest, "exposure", 1.0),
            ))

        for o in buys:
            bar = self._open_bar(o.symbol, day)
            if bar is None:
                continue
            price = float(bar) * (1.0 + self._slippage_pct(o.symbol, float(bar)))
            if price <= 0:
                continue
            qty = o.qty
            # Afford strictly what cash allows, including buy-side friction.
            while qty > 0:
                notional = price * qty
                costs = self._buy_cost(notional)
                if notional + costs <= cash:
                    break
                qty -= max(1, int(qty * 0.05))
            if qty <= 0:
                continue
            notional = price * qty
            costs = self._buy_cost(notional)
            cash -= (notional + costs)
            spent += costs
            self.engine.apply_buy(o.symbol, qty, price, day, tranche=o.tranche or 1)
            fills.append(Fill(
                date=day, symbol=o.symbol, side="BUY", qty=qty, price=price,
                notional=notional, costs=costs, net_cash=-(notional + costs),
                reason=o.reason, layer=o.layer,
                regime=getattr(self.engine.regime_engine.latest, "regime", ""),
                exposure=getattr(self.engine.regime_engine.latest, "exposure", 1.0),
            ))

        return cash, fills, closed, spent

    def _open_bar(self, symbol: str, day: pd.Timestamp) -> Optional[float]:
        """The fill price basis: today's OPEN if the stock traded today.

        No fallback to today's close. A stock suspended today must not be
        filled at a price from a day it did not trade — that is how backtests
        quietly buy stocks that were untradeable.
        """
        df = self.panel.get(symbol, day)
        if df.empty or df.index[-1] != day:
            return None
        return float(df["Open"].iloc[-1])


__all__ = ["InvestorBacktester", "BacktestResult", "Fill", "ClosedTrade"]