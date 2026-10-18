# investor/portfolio.py
"""Portfolio construction: weights, caps, rebalance cadence, SIP deployment.

Rules from the brief:
    20-30 holdings, inverse-ATR-volatility weights, 8% per stock, 25% per sector,
    2% minimum position, 3 new buys per day, full rebalance on the first
    trading day of each month, minimum 60-day hold.

Sizing is target-WEIGHT based, not risk-based (that is the swing engine's
job). Weight drift is only acted on between rebalances when a position exceeds
1.5x its target, which keeps monthly turnover low — the single biggest drag in
any high-frequency Indian strategy once STT and the ₹15 DP charge per sell are
counted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from investor.config import investor_config
from investor.data import SectorMap


@dataclass
class TargetWeight:
    symbol: str
    weight: float
    sector: str
    rank: int = 0
    score: float = 0.0
    atr: Optional[float] = None
    capped: bool = False          # hit the 8% single-name cap
    sector_capped: bool = False   # hit the 25% sector cap
    dropped_min_position: bool = False


@dataclass
class PortfolioState:
    """What the book looks like right now, in rupees and in weights."""

    equity: float
    cash: float
    weights: Dict[str, float] = field(default_factory=dict)
    sectors: Dict[str, str] = field(default_factory=dict)
    defensive_weights: Dict[str, float] = field(default_factory=dict)

    @property
    def invested(self) -> float:
        return max(0.0, self.equity - self.cash)


def inverse_atr_vol_weights(
    symbols: Sequence[str],
    atrs: Dict[str, Optional[float]],
    prices: Dict[str, float],
) -> Dict[str, float]:
    """Raw inverse-ATR weights, normalized to sum to 1.

    ATR (absolute rupees) is only comparable across names after dividing by
    price — a ₹600 stock with ₹20 ATR is far less volatile than a ₹3,000 stock
    with the same ₹20 ATR. Weighting on raw ATR would therefore hand the
    smallest, cheapest names an enormous share.
    """
    inv_vol: Dict[str, float] = {}
    for sym in symbols:
        a = atrs.get(sym)
        p = prices.get(sym)
        if not a or not p or a <= 0 or p <= 0:
            continue
        vol_pct = a / p           # ATR as a fraction of price = relative vol
        inv_vol[sym] = 1.0 / vol_pct
    total = sum(inv_vol.values())
    if total <= 0:
        return {}
    return {sym: w / total for sym, w in inv_vol.items()}


def apply_caps(
    raw_weights: Dict[str, float],
    sectors: Dict[str, str],
    max_position_pct: Optional[float] = None,
    max_sector_pct: Optional[float] = None,
    min_position_pct: Optional[float] = None,
) -> Tuple[List[TargetWeight], Dict[str, float]]:
    """Caps single names and sectors.

    Three properties this implementation guarantees, each of which an earlier
    version got wrong:

    1.  **No renormalisation after capping.** Normalising to 1.0 at the end
        silently undoes every cap: scaling a sector down and then dividing by
        the new total puts the weight straight back.
    2.  **Weight is only moved into available headroom.** When a sector cap
        frees weight, it is redistributed to other sectors only up to their
        single-name caps. Whatever has nowhere to go is left UNALLOCATED, and
        the book's weights then sum to less than 1.0 — the residue is cash.
        That is the honest answer for a book too small to satisfy its own caps
        (e.g. 8 names in one sector cannot fill a 25% sector cap at an 8% name
        cap: at most 3 positions fit).
    3.  **Caps are hard.** A single name at 95% is clipped to 8% even when
        there is nothing else to redistribute into.

    The two caps fight each other — scaling one sector down pushes others over
    their single-name limit — so they are iterated together until neither
    changes.
    """
    cfg = investor_config.portfolio
    max_pos = float(max_position_pct if max_position_pct is not None else cfg.get("max_position_pct", 8.0)) / 100.0
    max_sec = float(max_sector_pct if max_sector_pct is not None else cfg.get("max_sector_pct", 25.0)) / 100.0
    min_pos = float(min_position_pct if min_position_pct is not None else cfg.get("min_position_pct", 2.0)) / 100.0

    weights = {s: max(0.0, w) for s, w in raw_weights.items() if w > 0}
    if not weights:
        return [], {}

    total = sum(weights.values())
    if total <= 0:
        return [], {}
    # Normalise ONCE, up front. From here on the sum is allowed to fall below
    # 1.0, and that shortfall is cash.
    weights = {s: w / total for s, w in weights.items()}

    # Drop names too small to be worth holding, then renormalise.
    weights = {s: w for s, w in weights.items() if w >= min_pos}
    if not weights:
        return [], {}
    total = sum(weights.values())
    weights = {s: w / total for s, w in weights.items()}

    def _headroom(exclude: str) -> Dict[str, float]:
        return {s: max(0.0, max_pos - w) for s, w in weights.items()
                if s != exclude and w > 0}

    def _spread(amount: float, candidates: Dict[str, float]) -> float:
        """Places `amount` into the candidates' remaining headroom.

        Geometric water-filling: fill every candidate in proportion to its
        headroom, repeat until the amount is placed or the headroom is
        exhausted. A single proportional pass under-places badly (it truncates
        each candidate at its own headroom mid-way) and leaves the book
        artificially short of what the caps actually allow.

        Returns the amount placed; anything left over is unallocated (cash).
        """
        if amount <= 0 or not candidates:
            return 0.0
        remaining = float(amount)
        placed = 0.0
        for _ in range(60):
            heads = {s: max(0.0, max_pos - weights[s])
                     for s in candidates if max_pos - weights[s] > 1e-15}
            pool = sum(heads.values())
            if pool <= 1e-15 or remaining <= 1e-12:
                break
            scale = min(1.0, remaining / pool)
            for s, h in heads.items():
                give = h * scale
                weights[s] += give
                remaining -= give
                placed += give
        return placed

    for _ in range(60):
        changed = False

        # ── single-name cap (hard) ───────────────────────────────────────
        for sym in list(weights.keys()):
            if weights[sym] <= max_pos + 1e-12:
                continue
            excess = weights[sym] - max_pos
            weights[sym] = max_pos          # hard clip
            _spread(excess, _headroom(sym))
            changed = True

        # ── sector cap ───────────────────────────────────────────────────
        # Expressed as an ABSOLUTE fraction of the full book, not of the
        # current total. Measuring against a total that shrinks as caps bind
        # tightens the constraint after the fact and produces a final book
        # that violates its own sector cap.
        total = sum(weights.values())
        if total <= 0:
            break
        by_sector: Dict[str, float] = {}
        for sym, w in weights.items():
            sec = sectors.get(sym, "UNKNOWN")
            by_sector[sec] = by_sector.get(sec, 0.0) + w

        for sec, amt in by_sector.items():
            if amt <= max_sec + 1e-12:
                continue
            members = [s for s in weights if sectors.get(s, "UNKNOWN") == sec and weights[s] > 0]
            if not members:
                continue
            limit = max_sec
            scale = limit / amt if amt > 0 else 0.0
            before = amt
            for s in members:
                weights[s] *= scale
            freed = before - sum(weights[s] for s in members)
            # Freed weight may only go to OUTSIDE the sector. If there are no
            # outsiders it has nowhere to go and stays as cash — spreading it
            # back into the members would undo the cap we just applied.
            outsiders = {s: w for s, w in weights.items() if s not in members and w > 0}
            _spread(freed, {s: max(0.0, max_pos - weights[s]) for s in outsiders})
            changed = True

        if not changed:
            break

    # ── fill to capacity ──────────────────────────────────────────────────
    # The cap iterations above can leave weight stranded: scaling a sector down
    # frees weight that may not fit into the OTHER sectors right now, but often
    # would fit back into the same sector up to its remaining name-level
    # headroom. Without this pass a perfectly normal 20-30 name book ends up
    # ~60% deployed purely because of iteration order.
    leftover = max(0.0, 1.0 - sum(weights.values()))
    for _ in range(60):
        if leftover <= 1e-12:
            break
        total = sum(weights.values())
        if total <= 0:
            break
        expo: Dict[str, float] = {}
        for sym, w in weights.items():
            sec = sectors.get(sym, "UNKNOWN")
            expo[sec] = expo.get(sec, 0.0) + w
        head: Dict[str, float] = {}
        for sym, w in weights.items():
            sec = sectors.get(sym, "UNKNOWN")
            sector_slack = max(0.0, max_sec - expo[sec])
            head[sym] = min(max_pos - w, sector_slack)
        pool = sum(h for h in head.values() if h > 1e-15)
        if pool <= 1e-15:
            break
        scale = min(1.0, leftover / pool)
        for sym, h in head.items():
            if h <= 1e-15:
                continue
            give = h * scale
            weights[sym] += give
            leftover -= give

    total = sum(weights.values())
    by_sector = {}
    for sym, w in weights.items():
        sec = sectors.get(sym, "UNKNOWN")
        by_sector[sec] = by_sector.get(sec, 0.0) + w

    final: List[TargetWeight] = []
    for sym, w in weights.items():
        if w <= 0:
            continue
        sec = sectors.get(sym, "UNKNOWN")
        final.append(TargetWeight(
            symbol=sym,
            weight=w,
            sector=sec,
            capped=(w >= max_pos - 1e-9),
            sector_capped=(by_sector.get(sec, 0.0) >= max_sec - 1e-6),
        ))
    return final, {t.symbol: t.weight for t in final}


def build_targets(
    candidates: Sequence[str],
    atrs: Dict[str, Optional[float]],
    prices: Dict[str, float],
    sector_map: SectorMap,
    scores: Optional[Dict[str, float]] = None,
    min_holdings: Optional[int] = None,
    max_holdings: Optional[int] = None,
) -> Dict[str, float]:
    """Full target-weight construction for a candidate list.

    Enforces the 20-30 name band from both ends: the book is trimmed to
    `max_holdings` and padded toward `min_holdings` with the next-best
    candidates. A 12-name book is not this strategy — its risk profile is
    nothing like a 25-name book's, and reporting a CAGR off one is misleading.
    """
    cfg = investor_config.portfolio
    min_names = int(min_holdings if min_holdings is not None else cfg.get("min_holdings", 20))
    max_names = int(max_holdings if max_holdings is not None else cfg.get("max_holdings", 30))

    scores = scores or {}
    ordered = sorted(
        [c for c in candidates if prices.get(c, 0.0) > 0],
        key=lambda s: scores.get(s, 0.0),
        reverse=True,
    )
    selected = ordered[:max_names]
    if len(selected) < min_names:
        # Pad from the wider candidate list (caller passes the full eligible
        # set; anything genuinely available gets pulled in).
        for sym in ordered[max_names:]:
            selected.append(sym)
            if len(selected) >= min_names:
                break

    sectors = {s: sector_map.get(s) for s in selected}
    raw = inverse_atr_vol_weights(selected, atrs, prices)
    targets, weights = apply_caps(raw, sectors)

    # Re-attach scores in rank order.
    scored = sorted(targets, key=lambda t: scores.get(t.symbol, 0.0), reverse=True)
    for i, t in enumerate(scored):
        t.rank = i + 1
        t.score = scores.get(t.symbol, 0.0)
    return {t.symbol: t.weight for t in scored}


# ─────────────────────────────────────────────────────────────────────────────
# Rebalance cadence
# ─────────────────────────────────────────────────────────────────────────────

def is_rebalance_day(
    current: pd.Timestamp,
    previous_trading_day: Optional[pd.Timestamp],
    day_of_month: Optional[int] = None,
) -> bool:
    """First trading day of a calendar month, or the first day we ever see."""
    if previous_trading_day is None:
        return True
    cfg = investor_config.portfolio
    dom = int(day_of_month if day_of_month is not None else cfg.get("rebalance_day_of_month", 1))
    if current.day >= dom:
        return current.month != previous_trading_day.month or current.day == dom
    return False


def months_since(reference: pd.Timestamp, current: pd.Timestamp) -> int:
    return (current.year - reference.year) * 12 + (current.month - reference.month)


def min_hold_satisfied(
    days_held: int,
    forced: bool = False,
) -> bool:
    """The 60-day floor, waived only for stops and hard-negative vetoes.

    `forced` is set by the caller when the reason is a stop or a governance
    veto — those are the only two situations the brief allows to override the
    floor, and they are not overridable by an underweight or a quarterly
    score refresh.
    """
    if forced:
        return True
    need = int(investor_config.portfolio.get("min_hold_days", 60))
    return days_held >= need


def overweighted(
    current_weights: Dict[str, float],
    target_weights: Dict[str, float],
    threshold: Optional[float] = None,
) -> List[Tuple[str, float, float]]:
    """Positions above `threshold` x their target weight.

    Returns [(symbol, current_w, target_w)]. Between monthly rebalances this is
    the only discretionary trimming trigger.
    """
    thr = float(threshold if threshold is not None
                else investor_config.portfolio.get("overweight_trigger", 1.5))
    out = []
    for sym, w in current_weights.items():
        tgt = target_weights.get(sym, 0.0)
        if tgt <= 0:
            # Not a target at all -> trim to zero (subject to the 60-day floor).
            out.append((sym, w, 0.0))
            continue
        if w > tgt * thr:
            out.append((sym, w, tgt))
    return out


def sip_deployment(
    target_weights: Dict[str, float],
    current_weights: Dict[str, float],
    defensive_weights: Dict[str, float],
    eligible: Sequence[str],
) -> Tuple[str, float]:
    """Where the monthly SIP goes.

    Into the most underweight eligible name (gap to target, weighted by target
    size so we don't drip-feed into a 2% position), or into the defensive sleeve
    when nothing qualifies.
    """
    best: Optional[str] = None
    best_gap = 0.0
    for sym in eligible:
        tgt = target_weights.get(sym, 0.0)
        if tgt <= 0:
            continue
        cur = current_weights.get(sym, 0.0)
        gap = (tgt - cur) * tgt  # size-aware underweight
        if gap > best_gap:
            best_gap, best = gap, sym
    if best is not None:
        return best, best_gap

    if defensive_weights:
        proxy = max(defensive_weights.items(), key=lambda kv: kv[1])[0]
        return proxy, 1.0
    return "CASH", 1.0


def sector_exposure(
    weights: Dict[str, float],
    sector_map: SectorMap,
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sym, w in weights.items():
        sec = sector_map.get(sym)
        out[sec] = out.get(sec, 0.0) + w
    return out


__all__ = [
    "TargetWeight", "PortfolioState",
    "inverse_atr_vol_weights", "apply_caps", "build_targets",
    "is_rebalance_day", "months_since", "min_hold_satisfied", "overweighted",
    "sip_deployment", "sector_exposure",
]