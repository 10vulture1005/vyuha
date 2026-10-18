# investor/pipeline.py
"""The single daily decision engine for Investor Mode.

One function, `evaluate_day`, turns (date, portfolio state) into an ordered
list of orders. The live path (`core/investor_engine.py`) executes it against
the DB; the backtest (`backtest_investor/engine.py`) replays it chronologically.
Because both call THIS, the two cannot drift — which is how the existing
swing strategy ended up with different stop and exit behaviour in
`backtest/engine.py` versus `core/paper_engine.py`.

Layer toggles (`use_fundamentals`, `use_technical`, `use_news`, `use_regime`)
are what the ablation study flips; the live engine always runs all four.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from investor.config import investor_config
from investor.data import PricePanel, PointInTimeFundamentals, SectorMap
from investor.fundamentals import FundamentalEngine
from investor.indicators import IndicatorCache
from investor import news as news_mod
from investor import portfolio as pf_mod
from investor import regime as regime_mod
from investor import technical as tech_mod


# ─────────────────────────────────────────────────────────────────────────────
# Orders and positions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    """One intended trade. Fills happen at the NEXT open, never this close."""

    symbol: str
    side: str                 # BUY | SELL | TRIM
    qty: int
    limit_hint: float         # reference price (today's close); fill is t+1 open
    target_weight: float = 0.0
    reason: str = ""
    layer: str = ""           # REGIME | FUNDAMENTAL | TECHNICAL | NEWS | REBALANCE
    tranche: int = 0
    sector: str = "UNKNOWN"
    urgent: bool = False      # stops and vetoes fill at the very next open

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol, "side": self.side, "qty": self.qty,
            "limit_hint": round(self.limit_hint, 2),
            "target_weight": round(self.target_weight, 4),
            "reason": self.reason, "layer": self.layer,
            "tranche": self.tranche, "sector": self.sector,
            "urgent": self.urgent,
        }


@dataclass
class Position:
    """An open Investor Mode position, with the state Layer 2 needs."""

    symbol: str
    qty: int
    avg_price: float
    first_buy_date: pd.Timestamp
    highest_close: float
    stop: Optional[float] = None
    tranche1_date: Optional[pd.Timestamp] = None
    tranche1_done: bool = False
    tranche2_done: bool = False
    sector: str = "UNKNOWN"
    score: float = 0.0
    days_since_tranche1: int = 999

    def market_value(self, price: float) -> float:
        return self.qty * price

    def weight(self, price: float, equity: float) -> float:
        return (self.market_value(price) / equity) if equity > 0 else 0.0

    def days_held(self, as_of) -> int:
        return int((pd.Timestamp(as_of) - pd.Timestamp(self.first_buy_date)).days)


@dataclass
class DayDecision:
    """Everything decided on one trading day, based on that day's close."""

    date: pd.Timestamp
    exposure: float
    regime: str
    orders: List[Order] = field(default_factory=list)
    target_weights: Dict[str, float] = field(default_factory=dict)
    defensive_targets: Dict[str, float] = field(default_factory=dict)
    eligible: List[str] = field(default_factory=list)
    snapshots: Dict[str, tech_mod.TechSnapshot] = field(default_factory=dict)
    news_verdicts: Dict[str, news_mod.NewsVerdict] = field(default_factory=dict)
    scored: Optional[pd.DataFrame] = None
    equity: float = 0.0
    cash: float = 0.0
    is_rebalance: bool = False
    notes: List[str] = field(default_factory=list)
    #: Pre-classified headlines injected by the live path (per symbol). When
    #: present the engine uses them instead of the earnings-gap proxy.
    external_events: Dict[str, List] = field(default_factory=dict)

    def as_dict(self) -> Dict:
        return {
            "date": self.date,
            "exposure": self.exposure,
            "regime": self.regime,
            "orders": [o.as_dict() for o in self.orders],
            "n_orders": len(self.orders),
            "target_weights": {k: round(v, 4) for k, v in self.target_weights.items()},
            "defensive_targets": self.defensive_targets,
            "eligible": self.eligible,
            "is_rebalance": self.is_rebalance,
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# The engine
# ─────────────────────────────────────────────────────────────────────────────

class InvestorEngine:
    """Stateful daily decision engine.

    Stateful on purpose: regime hysteresis, the quarterly refresh schedule and
    the 60-day minimum hold all need memory across days. The backtest drives it
    chronologically; the live path drives it once a day.
    """

    def __init__(
        self,
        panel: PricePanel,
        store: PointInTimeFundamentals,
        sector_map: SectorMap,
        universe: Sequence[str],
        nifty_symbol: Optional[str] = None,
        # layer toggles (ablation study flips these)
        use_fundamentals: bool = True,
        use_technical: bool = True,
        use_news: bool = True,
        use_regime: bool = True,
        cache: Optional[IndicatorCache] = None,
    ):
        self.panel = panel
        self.store = store
        self.sectors = sector_map
        self.universe = list(universe)
        self.nifty_symbol = nifty_symbol

        self.use_fundamentals = use_fundamentals
        self.use_technical = use_technical
        self.use_news = use_news
        self.use_regime = use_regime

        # Indicators are computed once per symbol over the full history and
        # then sliced per day: identical maths, ~1000x faster on a multi-year
        # replay, and the same single-pass shape the live engine uses.
        self.cache = cache or IndicatorCache(panel, self.nifty_symbol)
        self.regime_engine = regime_mod.RegimeEngine(
            panel, self.universe, nifty_symbol, cache=self.cache
        )
        self.fund_engine = FundamentalEngine(store, sector_map, panel=panel)

        self.positions: Dict[str, Position] = {}
        self.last_refresh: Optional[pd.Timestamp] = None
        self.eligible: List[str] = []
        self.score_cutoff_50: Optional[float] = None
        self.review_until: Dict[str, pd.Timestamp] = {}     # 5-day news review state
        self.last_trading_day: Optional[pd.Timestamp] = None
        self._last_targets: Dict[str, float] = {}
        self._scored_cache: Optional[pd.DataFrame] = None

    # ── main entry point ────────────────────────────────────────────────────
    def evaluate_day(
        self,
        as_of,
        equity: float,
        cash: float,
        external_news: Optional[Dict[str, Sequence[news_mod.NewsEvent]]] = None,
    ) -> DayDecision:
        """Decides today's orders from today's close.

        `external_news` lets the live path inject already-classified headlines;
        when omitted the engine synthesises the earnings-gap proxy, which is
        what the backtest uses.
        """
        as_of = pd.Timestamp(as_of)
        nifty_df = self.panel.get(self.nifty_symbol or "INDEX_NIFTY500", as_of)

        # ── Layer 0 ─────────────────────────────────────────────────────────
        state = self.regime_engine.update(as_of)
        exposure = state.exposure if (state and self.use_regime) else 1.0
        regime = state.regime if state else regime_mod.MIXED
        defensive = (
            regime_mod.defensive_targets(state) if (state and self.use_regime) else {}
        )

        decision = DayDecision(
            date=as_of, exposure=exposure, regime=regime,
            equity=equity, cash=cash,
            defensive_targets=defensive,
        )
        if external_news:
            decision.external_events = dict(external_news)

        # ── Layer 1 ─────────────────────────────────────────────────────────
        self._refresh_eligible(as_of, decision)

        # ── Layer 2 ─────────────────────────────────────────────────────────
        snapshots = self._build_snapshots(as_of, nifty_df, decision)
        decision.snapshots = snapshots

        # ── Layer 3 ─────────────────────────────────────────────────────────
        verdicts = self._news_for_day(as_of, decision)
        decision.news_verdicts = verdicts

        # ── Portfolio construction ──────────────────────────────────────────
        invested = max(0.0, equity - cash)
        self._mark_to_market(as_of, nifty_df, equity)

        if pf_mod.is_rebalance_day(as_of, self.last_trading_day):
            decision.is_rebalance = True
            self._full_rebalance(as_of, equity, exposure, decision, snapshots)
        else:
            self._incremental_review(as_of, equity, exposure, decision, snapshots)

        self.last_trading_day = as_of
        return decision

    # ── Layer 1 refresh ─────────────────────────────────────────────────────
    def _refresh_eligible(self, as_of, decision: DayDecision) -> None:
        """Recompute the fundamental cross-section only on quarterly refresh days.

        Fundamentals are filed a few times a year, so rescoring the whole
        universe every trading day is both ~50x slower and conceptually wrong:
        between filings the scores cannot change, only prices can, and those
        move in Layer 2/Valuation which is read through the price panel.
        """
        if not self.use_fundamentals:
            # Ablation: technical-only runs over the whole investable universe.
            self.eligible = [s for s in self.universe if not self.panel.get(s, as_of).empty]
            decision.eligible = self.eligible
            return

        due = self.fund_engine.refresh_due(self.last_refresh, as_of)
        if due or self._scored_cache is None:
            scored = self.fund_engine.score_universe(as_of, self.universe)
            self._scored_cache = scored
            self.eligible = self.fund_engine.eligible_list(scored)
            self.score_cutoff_50 = self.fund_engine.quarterly_cutoff(scored)
            if due:
                self.last_refresh = as_of
                decision.notes.append(
                    f"Quarterly eligible-list refresh: {len(self.eligible)} names "
                    f"of {int(self._scored_cache['eligible'].sum())} passing filters"
                )

        decision.scored = self._scored_cache
        decision.eligible = list(self.eligible)

    # ── Layer 2 snapshots ───────────────────────────────────────────────────
    def _build_snapshots(
        self, as_of, nifty_df, decision: DayDecision
    ) -> Dict[str, tech_mod.TechSnapshot]:
        """Snapshots for holdings + watchlist candidates (not the whole market).

        Reads the pre-computed indicator row for `as_of`; a symbol that did not
        trade today is simply absent, so nothing downstream can act on a stale
        price.
        """
        watch = set(self.universe) if not self.use_fundamentals else set(self.eligible)
        for sym in list(self.positions.keys()):
            watch.add(sym)
        snaps: Dict[str, tech_mod.TechSnapshot] = {}
        for sym in sorted(watch):
            snap = tech_mod.snapshot_from_cache(self.cache, sym, as_of)
            if snap is not None:
                snaps[sym] = snap
        return snaps

    # ── Layer 3 ─────────────────────────────────────────────────────────────
    def _news_for_day(
        self, as_of, decision: DayDecision
    ) -> Dict[str, news_mod.NewsVerdict]:
        if not self.use_news:
            return {}
        if decision.external_events:
            # external_events arrives as Dict[str, List[NewsEvent]] from
            # InvestorModeEngine.collect_and_persist_news, but every caller
            # downstream expects Dict[str, NewsVerdict] and calls .blocking()
            # on the value. Returning it raw crashed _emit_entries as soon as
            # any headline was actually collected — which is why this only ever
            # showed up when news was working, never under --no-news.
            as_of_dt = pd.Timestamp(as_of).to_pydatetime().replace(
                hour=0, tzinfo=timezone.utc
            )
            aggregated: Dict[str, news_mod.NewsVerdict] = {}
            for sym, events in decision.external_events.items():
                if not events:
                    continue
                aggregated[sym] = news_mod.aggregate(sym, list(events), as_of_dt)
            return aggregated

        watch = set(self.positions.keys()) | set(self.eligible[:60])
        verdicts: Dict[str, news_mod.NewsVerdict] = {}
        as_of_dt = pd.Timestamp(as_of).to_pydatetime().replace(hour=0)
        for sym in sorted(watch):
            frame = self.cache.frame(sym)
            if frame.empty or pd.Timestamp(as_of) not in frame.index:
                continue
            row = frame.loc[pd.Timestamp(as_of)]
            prior = frame.index[frame.index < pd.Timestamp(as_of)]
            if len(prior) < 21:
                continue
            prev_row = frame.loc[prior[-1]]
            avg_vol = float(frame["Volume"].iloc[-21:-1].mean())
            proxy = news_mod.earnings_proxy(sym, row, prev_row, avg_vol)
            if proxy is None:
                continue
            proxy.date = as_of
            snap = decision.snapshots.get(sym)
            ev = self._proxy_to_events(proxy, snap)
            verdicts[sym] = news_mod.aggregate(sym, ev, as_of_dt)
            verdicts[sym].events = ev  # type: ignore[assignment]
        return verdicts

    @staticmethod
    def _proxy_to_events(proxy: news_mod.NewsProxy, snap) -> List[news_mod.NewsEvent]:
        """Turns a price/volume proxy into synthetic events.

        Deliberately labelled `[proxy]` and `source='earnings_gap_proxy'` so
        anything persisted to `news_events` from a backtest run is unmistakably
        not real classified news. A negative gap maps to OVERREACTION, never to
        HARD_NEGATIVE: without the headline there is no evidence the event is
        fundamental, and inventing a governance veto from a price move would be
        a look-ahead dressed up as a rule.
        """
        title = (
            f"[proxy] {proxy.symbol} gap {proxy.gap_pct:+.1f}% on "
            f"{proxy.volume_ratio:.1f}x volume"
        )
        if proxy.is_negative:
            ev_type = news_mod.OVERREACTION
            sentiment = -1.0
        elif proxy.is_catalyst:
            ev_type = news_mod.POSITIVE
            sentiment = 1.0
        else:
            ev_type = news_mod.NEUTRAL
            sentiment = 0.0
        return [news_mod.NewsEvent(
            symbol=proxy.symbol, title=title, url="",
            source="earnings_gap_proxy",
            published_at=None, event_type=ev_type, subtype="earnings_gap",
            materiality=news_mod.materiality(ev_type, "earnings_gap", title),
            sentiment=sentiment, tier="PROXY", raw_text=title,
        )]

    def _price_of(self, symbol: str, as_of) -> Optional[float]:
        """Close on `as_of`, or None if the stock did not trade that day."""
        row = self.cache.row_at(symbol, pd.Timestamp(as_of))
        if row is None:
            return None
        return float(row["Close"])

    # ── mark to market ──────────────────────────────────────────────────────
    def _mark_to_market(self, as_of, nifty_df, equity: float) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        for sym, pos in self.positions.items():
            px = self._price_of(sym, as_of)
            if px is None:
                continue
            pos.highest_close = max(pos.highest_close, px)
            prices[sym] = px
        return prices

    # ── full rebalance ──────────────────────────────────────────────────────
    def _full_rebalance(
        self, as_of, equity: float, exposure: float, decision: DayDecision,
        snaps: Dict[str, tech_mod.TechSnapshot],
    ) -> None:
        """First trading day of the month: rebuild the target book.

        Rebalancing sets TARGETS, not fills. A name enters the book only when
        Layer 2's entry trigger actually fires — otherwise the rebalance would
        buy stocks sitting below their 200DMA and the trend-break exit would
        sell them three sessions later, which is exactly the churn loop an
        earlier version of this engine fell into.
        """
        equity_for_equities = equity * exposure
        prices: Dict[str, float] = {}
        atrs: Dict[str, Optional[float]] = {}
        for sym in self.eligible:
            px = self._price_of(sym, as_of)
            if px is None:
                continue
            prices[sym] = px
            atrs[sym] = snaps[sym].atr if sym in snaps else None

        scores = {}
        if decision.scored is not None and not decision.scored.empty:
            scores = decision.scored["score"].to_dict()

        targets = pf_mod.build_targets(
            self.eligible, atrs, prices, self.sectors, scores
        )
        decision.target_weights = targets
        self._last_targets = dict(targets)

        # 1. Exits first (stops and vetoes).
        self._emit_exits(as_of, equity, decision, snaps)

        # 2. Quarterly fundamental drop-out -> sell at THIS rebalance, not now.
        for sym, pos in list(self.positions.items()):
            if sym in targets:
                continue
            days = pos.days_held(as_of)
            if not pf_mod.min_hold_satisfied(days, forced=False):
                continue  # floor holds it one more month
            decision.orders.append(Order(
                symbol=sym, side="SELL", qty=pos.qty,
                limit_hint=prices.get(sym, pos.avg_price),
                reason="Dropped out of quarterly eligible list (score below top 50%)",
                layer="FUNDAMENTAL", sector=pos.sector,
            ))

        # 3. Hard holdings cap. Without this the book drifts past max_holdings:
        # targets cap at 30 names, but positions carried over from last month
        # still count and each rebalance stacks new ones on top.
        max_names = int(investor_config.portfolio.get("max_holdings", 30))
        survivors = [s for s in self.positions if s in targets]
        if len(survivors) > max_names:
            ranked = sorted(survivors, key=lambda s: targets.get(s, 0.0), reverse=True)
            keep = set(ranked[:max_names])
            for sym in ranked[max_names:]:
                pos = self.positions[sym]
                if not pf_mod.min_hold_satisfied(pos.days_held(as_of), forced=False):
                    continue
                decision.orders.append(Order(
                    symbol=sym, side="SELL", qty=pos.qty,
                    limit_hint=prices.get(sym, pos.avg_price),
                    reason=f"Holdings cap: {len(survivors)} > {max_names}",
                    layer="REBALANCE", sector=pos.sector,
                ))
            targets = {s: w for s, w in targets.items() if s in keep}
            decision.target_weights = targets
            self._last_targets = dict(targets)

        # 4. Trim survivors that are above target (still honouring the floor).
        for sym, tgt_w in targets.items():
            pos = self.positions.get(sym)
            if pos is None:
                continue
            price = prices.get(sym) or self._price_of(sym, as_of)
            if price is None:
                continue
            cur_value = pos.market_value(price)
            target_value = tgt_w * equity_for_equities
            if cur_value <= target_value:
                continue
            if not pf_mod.min_hold_satisfied(pos.days_held(as_of), forced=False):
                continue
            qty = int((cur_value - target_value) / price)
            if qty > 0:
                decision.orders.append(Order(
                    symbol=sym, side="TRIM", qty=qty, limit_hint=price,
                    target_weight=tgt_w,
                    reason=f"Rebalance to {tgt_w:.1%} target",
                    layer="REBALANCE", sector=pos.sector,
                ))

        # 5. Entries for target names whose Layer 2 trigger fires today.
        self._emit_entries(as_of, equity, exposure, decision, snaps, targets, prices)
        self._emit_tranche2(as_of, equity, exposure, decision, targets, prices)

    # ── between-rebalance review ────────────────────────────────────────────
    def _incremental_review(
        self, as_of, equity: float, exposure: float, decision: DayDecision,
        snaps: Dict[str, tech_mod.TechSnapshot],
    ) -> None:
        """Between rebalances: stops, vetoes, 1.5x drift, and new entries."""
        # Stops and vetoes.
        self._emit_exits(as_of, equity, decision, snaps)

        prices = self._current_prices(as_of, extra=self.eligible)
        current_weights = {
            sym: pos.weight(prices.get(sym, pos.avg_price), equity)
            for sym, pos in self.positions.items()
        }

        # Keep the last known target weights; recompute only if we have none.
        targets = decision.target_weights or self._last_targets

        # Drift trim.
        for sym, cur_w, tgt_w in pf_mod.overweighted(current_weights, targets):
            pos = self.positions.get(sym)
            if pos is None:
                continue
            if not pf_mod.min_hold_satisfied(pos.days_held(as_of), forced=False):
                continue
            price = prices.get(sym, pos.avg_price)
            target_value = tgt_w * equity
            cur_value = cur_w * equity
            sell_value = max(0.0, cur_value - target_value)
            qty = int(sell_value / price) if price > 0 else 0
            if qty > 0:
                decision.orders.append(Order(
                    symbol=sym, side="TRIM", qty=qty, limit_hint=price,
                    target_weight=tgt_w,
                    reason=f"Drift trim: {cur_w:.1%} vs {tgt_w:.1%} target (>1.5x)",
                    layer="REBALANCE", sector=pos.sector,
                ))

        # Tranche 2 always runs: completing a position that was already started
        # is not "opening a new position", and the brief explicitly asks for the
        # second tranche to land 10 trading days after the first.
        self._emit_tranche2(as_of, equity, exposure, decision, targets, prices)

        # Entries are restricted to the standing target book. Between
        # rebalances that means finishing the target the last rebalance set,
        # not picking new discretionary ideas.
        if self.use_technical:
            self._emit_entries(as_of, equity, exposure, decision, snaps, targets, prices)

    def _emit_tranche2(
        self, as_of, equity: float, exposure: float, decision: DayDecision,
        targets: Dict[str, float], prices: Dict[str, float],
    ) -> None:
        """Tops up tranche-1 positions that are due their second half."""
        if not self.use_technical:
            # Two-tranche staging is a Layer 2 construct. In the
            # Fundamentals-only ablation every position is already full-size.
            return
        max_buys = int(investor_config.portfolio.get("max_new_buys_per_day", 3))
        snaps = decision.snapshots
        blocked = {s for s, v in decision.news_verdicts.items() if v.blocking()}
        n_buys = sum(1 for o in decision.orders if o.side == "BUY")
        for sym, pos in sorted(self.positions.items()):
            if n_buys >= max_buys:
                break
            if pos.tranche2_done or not pos.tranche1_done or pos.tranche1_date is None:
                continue
            if sym in blocked:
                continue
            snap = snaps.get(sym)
            if snap is None:
                continue
            days = int((as_of - pos.tranche1_date).days)
            sig2 = tech_mod.tranche2_signal(snap, days, snap.sma_fast)
            if sig2 is None:
                continue
            before = len(decision.orders)
            self._emit_buy(sig2, as_of, equity, exposure, decision, targets, prices)
            if len(decision.orders) > before:
                pos.days_since_tranche1 = days
                n_buys += 1

    def _current_prices(self, as_of, extra: Optional[Sequence[str]] = None) -> Dict[str, float]:
        """Latest close for holdings plus (optionally) a candidate list."""
        out: Dict[str, float] = {}
        syms = list(self.positions.keys())
        syms.extend(extra or [])
        for sym in dict.fromkeys(syms):
            px = self._price_of(sym, as_of)
            if px is not None:
                out[sym] = px
        return out

    # ── exits ───────────────────────────────────────────────────────────────
    def _emit_exits(
        self, as_of, equity: float, decision: DayDecision,
        snaps: Dict[str, tech_mod.TechSnapshot],
    ) -> None:
        tracker = tech_mod.ChandelierTracker()
        for sym, pos in list(self.positions.items()):
            snap = snaps.get(sym)
            row = self.cache.row_at(sym, as_of)
            if row is None:
                continue
            close = float(row["Close"])
            low = float(row["Low"])

            # Ratchet the Chandelier stop on the way in.
            atr_now = snap.atr if snap else None
            new_stop, new_high = tracker.update(
                pos.highest_close, close, atr_now, pos.stop
            )
            pos.stop = new_stop
            pos.highest_close = new_high

            verdict = decision.news_verdicts.get(sym)
            reason: Optional[str] = None
            urgent = False

            # News veto takes precedence over everything except a stop already
            # being hit on this bar (that is just a stop).
            if verdict and verdict.blocking():
                confirmed, why = self._confirm_negative(sym, as_of, snap)
                if confirmed:
                    reason = f"Hard-negative veto + price confirmation ({why}): {verdict.hard_negative_reason}"
                    urgent = True
                else:
                    self.review_until[sym] = as_of + pd.Timedelta(
                        days=int(investor_config.news.get("hard_negative", {}).get("review_days", 5))
                    )
                    decision.notes.append(f"{sym}: news review state for 5 days")

            if reason is None and pos.stop is not None and low <= pos.stop:
                reason = f"Chandelier stop: low {low:.1f} <= stop {pos.stop:.1f}"
                urgent = True

            if reason is None and snap and snap.closes_below_sma >= int(
                investor_config.technical.get("exit_consecutive_closes", 3)
            ):
                reason = (
                    f"{snap.closes_below_sma} consecutive closes below 200DMA "
                    f"({close:.1f} < {snap.sma_slow or 0:.1f})"
                )
                urgent = True

            # Valuation alone never sells unless the multiple is extreme AND
            # the trend is broken.
            if reason is None and decision.scored is not None and sym in decision.scored.index:
                row = decision.scored.loc[sym]
                pe = row.get("_v_raw")
                if (
                    snap is not None and not snap.above_slow_ma
                    and snap.sma_slow is not None and close < snap.sma_slow
                    and self._pe_extreme(sym, as_of)
                ):
                    reason = "P/E > 2x own 5y median AND close below 200DMA"
                    urgent = False

            if reason is None:
                continue

            forced = urgent or (verdict is not None and verdict.blocking())
            if not pf_mod.min_hold_satisfied(pos.days_held(as_of), forced=forced):
                continue

            decision.orders.append(Order(
                symbol=sym, side="SELL", qty=pos.qty, limit_hint=close,
                reason=reason, layer="TECHICAL" if not urgent else "NEWS",
                sector=pos.sector, urgent=urgent,
            ))

    def _confirm_negative(self, sym, as_of, snap) -> Tuple[bool, str]:
        """Price confirmation test applied to `as_of`'s bar."""
        frame = self.cache.frame(sym)
        day = pd.Timestamp(as_of)
        row = self.cache.row_at(sym, day)
        if row is None or len(frame) < 22:
            return False, ""
        prior = frame.index[frame.index < day]
        if len(prior) < 21:
            return False, ""
        prev_row = frame.loc[prior[-1]]
        avg_vol = float(frame["Volume"].iloc[-21:-1].mean())
        return news_mod.price_confirms_negative(
            float(row["Open"]), float(prev_row["Close"]), float(row["Close"]),
            snap.sma_fast if snap else None,
            float(row["Volume"]), avg_vol,
        )

    def _pe_extreme(self, sym, as_of) -> bool:
        """True when P/E exceeds 2x the stock's own 5-year median.

        Recomputed here rather than read from the stored score frame, because
        the score frame is only refreshed quarterly and the price it would use
        is stale by up to three months. The brief allows a valuation sale only
        when this is true AND the trend is broken.
        """
        m = self.fund_engine.metrics_for(sym, as_of)
        pe, med = m.pe, m.pe_5y_median
        if not pe or not med or med <= 0 or pe <= 0:
            return False
        mult = float(investor_config.fundamentals.get("valuation", {}).get(
            "pe_vs_5y_median_sale_multiple", 2.0
        ))
        return pe > mult * med

    # ── entries ─────────────────────────────────────────────────────────────
    def _emit_entries(
        self, as_of, equity: float, exposure: float, decision: DayDecision,
        snaps: Dict[str, tech_mod.TechSnapshot], targets: Dict[str, float],
        prices: Dict[str, float],
    ) -> None:
        """Buy target names whose Layer 2 entry trigger fires today.

        Only names in `targets` are considered (unless
        `allow_new_buys_outside_targets` is set). A target that is not bought
        today stays buyable tomorrow, so the book fills up as setups appear
        instead of only on the first of the month.
        """
        max_buys = int(investor_config.portfolio.get("max_new_buys_per_day", 3))
        max_holdings = int(investor_config.portfolio.get("max_holdings", 30))
        eligible_set = set(self.eligible) if self.use_fundamentals else set(self.universe)
        outside_ok = bool(
            investor_config.portfolio.get("allow_new_buys_outside_targets", False)
        )

        n_buys = sum(1 for o in decision.orders if o.side == "BUY")
        if n_buys >= max_buys:
            return
        if len(self.positions) >= max_holdings:
            decision.notes.append(f"Holding cap reached ({max_holdings}); no new buys")
            return

        # Blocked by a hard negative today?
        blocked = {
            s for s, v in decision.news_verdicts.items() if v.blocking()
        }

        candidates = []
        pool = list(targets.items())
        if outside_ok:
            extra = [(s, 0.0) for s in eligible_set if s not in targets]
            pool.extend(extra)
        for sym, tgt_w in pool:
            if sym in self.positions or sym not in eligible_set:
                continue
            if sym in blocked or tgt_w <= 0:
                continue
            snap = snaps.get(sym)
            # Momentum is a Layer 2 filter; the Fundamentals-only ablation must
            # not inherit it or it stops being an F-only comparison.
            if snap is None:
                continue
            if self.use_technical and not snap.momentum_ok:
                continue
            sig = tech_mod.entry_signal(
                snap, eligible=True, use_technical=self.use_technical
            )
            if sig is None:
                continue
            candidates.append((sig, snap))

        # Rank by composite score, falling back to momentum.
        def _rank(item):
            sig, snap = item
            sc = 0.0
            if decision.scored is not None and sig.symbol in decision.scored.index:
                sc = float(decision.scored.loc[sig.symbol, "score"])
            return (sc, snap.mom_12_1 or 0.0)

        candidates.sort(key=_rank, reverse=True)

        for sig, snap in candidates[: max_buys - n_buys]:
            before = len(decision.orders)
            self._emit_buy(sig, as_of, equity, exposure, decision, targets, prices)
            if len(decision.orders) > before:
                n_buys += 1

        # Catalyst entries: only WITH full confirmation, at 1.25x.
        for sym, verdict in decision.news_verdicts.items():
            if n_buys >= max_buys:
                break
            if not verdict.positive_catalyst or sym in self.positions:
                continue
            if sym not in targets or sym in blocked:
                continue
            snap = snaps.get(sym)
            if snap is None or not snap.trend_ok or not snap.momentum_ok:
                continue
            frame = self.cache.frame(sym)

        # Catalyst entries: only WITH full confirmation, at 1.25x.
        for sym, verdict in decision.news_verdicts.items():
            if not verdict.positive_catalyst or sym in self.positions:
                continue
            if len(decision.orders) >= max_buys:
                break
            snap = snaps.get(sym)
            if snap is None or sym not in eligible_set or not snap.trend_ok:
                continue
            df = self.cache.frame(sym)
            day = pd.Timestamp(as_of)
            if len(df) < 22 or day not in df.index:
                continue
            row = df.loc[day]
            prior = df.index[df.index < day]
            if len(prior) < 21:
                continue
            prev_row = df.loc[prior[-1]]
            avg_vol = float(df["Volume"].iloc[-21:-1].mean())
            ok, why = news_mod.catalyst_entry_allowed(
                float(row["High"]), float(row["Low"]), float(row["Close"]),
                float(prev_row["Close"]), float(row["Volume"]), avg_vol,
            )
            if not ok:
                continue
            sig = tech_mod.EntrySignal(
                symbol=sym, date=as_of, setup="CATALYST", tranche=1,
                size_fraction=float(
                    investor_config.news.get("positive_catalyst", {}).get("size_multiple", 1.25)
                ),
                close=float(row["Close"]),
                reason=f"Post-event-drift entry ({why})",
            )
            self._emit_buy(sig, as_of, equity, exposure, decision, targets, prices)

    def _emit_buy(
        self, sig, as_of, equity: float, exposure: float, decision: DayDecision,
        targets: Dict[str, float], prices: Dict[str, float],
    ) -> None:
        sym = sig.symbol
        close = self._price_of(sym, as_of)
        if close is None or close <= 0:
            return
        snap = decision.snapshots.get(sym)

        equity_for_equities = equity * exposure
        target_w = targets.get(sym, 0.0)
        max_pos = float(investor_config.portfolio.get("max_position_pct", 8.0)) / 100.0
        if target_w <= 0:
            # Not in the target book: size it as an average position.
            n = max(int(investor_config.portfolio.get("min_holdings", 20)), 1)
            target_w = min(1.0 / n, max_pos)

        # Size off the GAP to target, not off a flat fraction of it, so
        # tranche 2 tops the position up to its full target rather than
        # overshooting it.
        pos = self.positions.get(sym)
        current_value = pos.market_value(close) if pos else 0.0
        target_value = target_w * equity_for_equities
        gap = target_value - current_value
        if gap <= 0:
            return

        value = min(gap, target_value) * float(sig.size_fraction)
        # Never exceed the 8% single-name cap even at 1.25x catalyst size.
        cap_value = max_pos * equity_for_equities
        if current_value + value > cap_value:
            value = max(0.0, cap_value - current_value)
        if value <= 0:
            return

        qty = int(value / close)
        if qty <= 0:
            return
        decision.orders.append(Order(
            symbol=sym, side="BUY", qty=qty, limit_hint=close,
            target_weight=target_w, reason=sig.reason,
            layer="TECHICAL" if sig.setup != "CATALYST" else "NEWS",
            tranche=sig.tranche, sector=self.sectors.get(sym),
        ))

    # ── rebalance trade generation ──────────────────────────────────────────
    # ── applying fills (used by both live and backtest) ─────────────────────
    def apply_buy(
        self, symbol: str, qty: int, price: float, as_of, tranche: int = 1
    ) -> Position:
        snap_date = pd.Timestamp(as_of)
        existing = self.positions.get(symbol)
        if existing:
            existing.qty += qty
            existing.avg_price = (
                existing.avg_price * (existing.qty - qty) + price * qty
            ) / max(1, existing.qty)
            if tranche == 2:
                existing.tranche2_done = True
            return existing
        pos = Position(
            symbol=symbol, qty=qty, avg_price=price,
            first_buy_date=snap_date, highest_close=price,
            sector=self.sectors.get(symbol),
            tranche1_date=snap_date if tranche == 1 else None,
            tranche1_done=(tranche == 1),
        )
        tracker = tech_mod.ChandelierTracker()
        atr_now = None
        row = self.cache.row_at(symbol, as_of) if self.cache is not None else None
        if row is not None:
            candidate = row.get("atr")
            if candidate is not None and not pd.isna(candidate):
                atr_now = float(candidate)
        # No ATR yet (fresh listing, or a unit test with no panel): leave the
        # stop unset rather than inventing one from a price fraction. An
        # invented stop is indistinguishable from a real one once it is in the
        # DB, and it would silently widen or tighten every exit afterwards.
        pos.stop = tracker.initial_stop(price, atr_now)
        self.positions[symbol] = pos
        return pos

    def apply_sell(self, symbol: str, qty: int, as_of) -> Optional[Position]:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        pos.qty -= qty
        if pos.qty <= 0:
            self.positions.pop(symbol, None)
        return pos


__all__ = ["Order", "Position", "DayDecision", "InvestorEngine"]