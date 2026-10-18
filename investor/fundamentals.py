# investor/fundamentals.py
"""Layer 1 — what to own.

Pipeline: raw point-in-time statements -> derived ratios -> hard filters ->
sector-neutral percentile scores -> composite -> top-30% eligible list,
refreshed quarterly.

Three properties this module exists to guarantee:

*   **No stored multiples.** P/E, ROCE, P/B and every 5y median are computed
    here, against the price panel, as of the scoring date. A multiple read out
    of a CSV column would carry the vendor's *current* valuation into 2018.
*   **Unknown is not "pass".** If a required metric cannot be computed the name
    is rejected with a `*_unknown` reason and counted as such in coverage
    stats. The one deliberate exception is promoter pledge and GNPA, which this
    data source does not carry at all — see `PLEDGE_REQUIRED` / `GNPA_REQUIRED`
    in thresholds.yaml and the data-gap note in the config.
*   **Fiscal years, not calendar years.** Indian companies report on a March
    year end, so growth and loss-year rules run on `frequency == "yearly"`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from investor.config import investor_config
from investor.data import (
    FundamentalRecord, PointInTimeFundamentals, PricePanel, SectorMap,
)

YEARS_FOR_CAGR = 3


# ─────────────────────────────────────────────────────────────────────────────
# Derived metric bundle
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    """All Layer 1 inputs for one symbol at one date."""

    symbol: str
    roce: Optional[float] = None
    roe: Optional[float] = None
    debt_to_equity: Optional[float] = None
    interest_coverage: Optional[float] = None
    ocf_to_pat: Optional[float] = None
    promoter_pledge_pct: Optional[float] = None
    revenue_cagr_3y: Optional[float] = None
    eps_cagr_3y: Optional[float] = None
    loss_years_3y: Optional[int] = None
    netmargin: Optional[float] = None
    margin_stability: Optional[float] = None      # lower = steadier (already negated)
    eps_yoy: Optional[float] = None               # latest quarter YoY
    revenue_yoy: Optional[float] = None
    eps_accel: Optional[float] = None             # change in YoY across 2 quarters
    revenue_accel: Optional[float] = None
    gnpa_pct: Optional[float] = None              # not carried by this data source
    pe: Optional[float] = None
    pb: Optional[float] = None
    pe_5y_median: Optional[float] = None
    pb_5y_median: Optional[float] = None
    earnings_yield: Optional[float] = None
    peg: Optional[float] = None
    n_years: int = 0
    period_end: Optional[pd.Timestamp] = None
    usable_from: Optional[pd.Timestamp] = None

    def as_row(self) -> Dict:
        return {
            "roce": self.roce, "roe": self.roe,
            "debt_to_equity": self.debt_to_equity,
            "interest_coverage": self.interest_coverage,
            "ocf_to_pat": self.ocf_to_pat,
            "promoter_pledge_pct": self.promoter_pledge_pct,
            "revenue_cagr_3y": self.revenue_cagr_3y,
            "eps_cagr_3y": self.eps_cagr_3y,
            "loss_years_3y": self.loss_years_3y,
            "netmargin": self.netmargin,
            "margin_stability": self.margin_stability,
            "eps_yoy": self.eps_yoy, "revenue_yoy": self.revenue_yoy,
            "eps_accel": self.eps_accel, "revenue_accel": self.revenue_accel,
            "pe": self.pe, "pb": self.pb,
            "pe_5y_median": self.pe_5y_median, "pb_5y_median": self.pb_5y_median,
            "earnings_yield": self.earnings_yield, "peg": self.peg,
            "n_years": self.n_years,
        }


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    try:
        v = float(a) / float(b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None if not np.isfinite(v) else v


def _cagr(first: Optional[float], last: Optional[float], years: int) -> Optional[float]:
    """Compound annual growth rate. Returns None on a non-positive base.

    A negative or near-zero base makes the CAGR mathematically meaningless (and
    a sign-flipping profit from a loss year can produce a spectacularly fake
    "growth" number), so those cases return None rather than a huge value.
    """
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    try:
        return ((float(last) / float(first)) ** (1.0 / years) - 1.0) * 100.0
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def compute_metrics(
    store: PointInTimeFundamentals,
    panel: PricePanel,
    symbol: str,
    as_of,
) -> Metrics:
    """Derives every Layer 1 metric from filing-lagged statements + prices."""
    m = Metrics(symbol=symbol)
    annual = store.annual(symbol, as_of)
    if not annual:
        return m
    latest = annual[-1]
    m.n_years = len(annual)
    m.period_end = latest.period_end
    m.usable_from = latest.usable_from

    # ── point-in-time profitability ────────────────────────────────────────
    assets = latest.get("total_assets")
    cl = latest.get("current_liabilities")
    capital = None
    if assets is not None and cl is not None:
        capital = assets - cl
    elif assets is not None:
        capital = assets
    m.roce = _pct(_safe_div(latest.get("ebit"), capital))
    m.roe = _pct(_safe_div(latest.get("pat"), latest.get("equity")))
    m.debt_to_equity = _safe_div(latest.get("debt"), latest.get("equity"))
    m.interest_coverage = _safe_div(latest.get("ebit"), latest.get("interest_expense"))
    m.netmargin = _pct(_safe_div(latest.get("pat"), latest.get("revenue")))
    m.promoter_pledge_pct = latest.get("promoter_pledge_pct")

    # ── 3-year cash conversion (average, per the brief) ─────────────────────
    ratios = []
    for rec in annual[-YEARS_FOR_CAGR:]:
        r = _safe_div(rec.get("ocf"), rec.get("pat"))
        if r is not None and r > 0:
            ratios.append(r)
    m.ocf_to_pat = float(np.mean(ratios)) if ratios else None

    # ── 3-year growth ──────────────────────────────────────────────────────
    window = annual[-(YEARS_FOR_CAGR + 1):]
    if len(window) >= 2:
        m.revenue_cagr_3y = _cagr(
            window[0].get("revenue"), window[-1].get("revenue"), len(window) - 1
        )
        eps_first = window[0].get("eps_diluted") or window[0].get("eps_basic")
        eps_last = window[-1].get("eps_diluted") or window[-1].get("eps_basic")
        m.eps_cagr_3y = _cagr(eps_first, eps_last, len(window) - 1)

    # ── loss-making years and margin stability over the same window ─────────
    last3 = annual[-YEARS_FOR_CAGR:]
    if len(last3) >= 2:
        m.loss_years_3y = sum(1 for r in last3 if (r.get("pat") or 0) < 0)
        margins = [
            _pct(_safe_div(r.get("pat"), r.get("revenue")))
            for r in last3
        ]
        margins = [x for x in margins if x is not None]
        if len(margins) >= 2:
            # Negated so that "higher is better" once percentile-ranked.
            m.margin_stability = -float(np.std(margins))

    # ── quarterly YoY and acceleration ─────────────────────────────────────
    quarters = store.quarterly(symbol, as_of)
    if quarters:
        latest_q = quarters[-1]
        m.eps_yoy = _latest_yoy(quarters, "eps_diluted") or _latest_yoy(quarters, "eps_basic")
        m.revenue_yoy = _latest_yoy(quarters, "revenue")

        # Most names in this universe carry only 4 calendar quarters of history
        # — one short of the 5 a true 4-quarters-back YoY needs. When that is
        # the case, fall back to comparing the latest quarter against the ANNUAL
        # period roughly a year earlier. Both sides are still filing-lagged, so
        # no look-ahead is introduced; the comparison is merely a year-over-year
        # growth rate measured across two different reporting grains.
        if m.revenue_yoy is None:
            m.revenue_yoy = _yoy_vs_annual(quarters[-1], annual, "revenue")
        if m.eps_yoy is None:
            m.eps_yoy = (
                _yoy_vs_annual(quarters[-1], annual, "eps_diluted")
                or _yoy_vs_annual(quarters[-1], annual, "eps_basic")
            )

        rev_yoy = [_yoy_at(quarters, i, "revenue") for i in range(-2, 0)]
        rev_yoy = [x for x in rev_yoy if x is not None]
        if len(rev_yoy) >= 2:
            m.revenue_accel = rev_yoy[-1] - rev_yoy[-2]
        elif m.revenue_yoy is not None and len(quarters) >= 2:
            prev = _yoy_vs_annual(quarters[-2], annual, "revenue")
            if prev is not None:
                m.revenue_accel = m.revenue_yoy - prev

        eps_yoy = [_yoy_at(quarters, i, "eps_diluted") for i in range(-2, 0)]
        eps_yoy = [x for x in eps_yoy if x is not None]
        if len(eps_yoy) >= 2:
            m.eps_accel = eps_yoy[-1] - eps_yoy[-2]
        elif m.eps_yoy is not None and len(quarters) >= 2:
            prev = (_yoy_vs_annual(quarters[-2], annual, "eps_diluted")
                    or _yoy_vs_annual(quarters[-2], annual, "eps_basic"))
            if prev is not None:
                m.eps_accel = m.eps_yoy - prev

    # ── valuation, computed against the price AS OF `as_of` ────────────────
    price = panel.close(symbol, as_of)
    eps_ttm = _eps_ttm(annual, quarters)
    book_ps = _safe_div(latest.get("equity"), latest.get("shares"))

    if price and price > 0:
        if eps_ttm and eps_ttm > 0:
            m.pe = price / eps_ttm
            m.earnings_yield = 100.0 / m.pe
        if book_ps and book_ps > 0:
            m.pb = price / book_ps
        if m.eps_cagr_3y and m.eps_cagr_3y > 0 and m.pe and m.pe > 0:
            m.peg = m.pe / m.eps_cagr_3y

    m.pe_5y_median = _trailing_median_multiple(panel, symbol, as_of, annual, quarters, "pe")
    m.pb_5y_median = _trailing_median_multiple(panel, symbol, as_of, annual, quarters, "pb")
    return m


def _pct(v: Optional[float]) -> Optional[float]:
    return None if v is None else v * 100.0


def _yoy_at(quarters: Sequence[FundamentalRecord], idx: int, field: str) -> Optional[float]:
    """YoY % change of `field` for quarter `idx` (negative indexes from the end).

    Compares against the record four quarters earlier, not the previous quarter —
    "YoY" means year over year, and using QoQ here would make the acceleration
    signal meaningless for Indian companies with strong seasonality (most of
    them — Q2 and Q3 are usually the big ones).
    """
    if len(quarters) < 5:
        return None
    cur = quarters[idx].get(field)
    ref_idx = idx - 4
    if ref_idx < -len(quarters):
        return None
    prev = quarters[ref_idx].get(field)
    if cur is None or prev is None or prev == 0:
        return None
    try:
        return (float(cur) / float(prev) - 1.0) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _latest_yoy(quarters: Sequence[FundamentalRecord], field: str) -> Optional[float]:
    if not quarters:
        return None
    return _yoy_at(quarters, -1, field)


def _yoy_vs_annual(
    quarter: FundamentalRecord,
    annual: Sequence[FundamentalRecord],
    field: str,
) -> Optional[float]:
    """YoY growth of `quarter[field]` against the annual period ~1 year earlier."""
    if quarter is None or not annual:
        return None
    cur = quarter.get(field)
    if cur is None:
        return None
    target = quarter.period_end - pd.DateOffset(years=1)
    best = min(annual, key=lambda r: abs((r.period_end - target).days))
    if abs((best.period_end - target).days) > 120:
        return None     # no annual period anywhere near a year back
    ref = best.get(field)
    if ref is None or ref == 0:
        return None
    try:
        return (float(cur) / float(ref) - 1.0) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _eps_ttm(
    annual: Sequence[FundamentalRecord], quarters: Sequence[FundamentalRecord]
) -> Optional[float]:
    """Trailing EPS: four calendar quarters if available, else last FY EPS."""
    eps_q = [
        r.get("eps_diluted") or r.get("eps_basic") for r in quarters[-4:]
    ]
    if len(eps_q) >= 4 and all(e is not None for e in eps_q):
        return float(sum(eps_q))
    if annual:
        last = annual[-1]
        return last.get("eps_diluted") or last.get("eps_basic")
    return None


def _trailing_median_multiple(
    panel: PricePanel,
    symbol: str,
    as_of,
    annual: Sequence[FundamentalRecord],
    quarters: Sequence[FundamentalRecord],
    kind: str,
    years: int = 5,
) -> Optional[float]:
    """Median of the multiple over the trailing `years`, using only data that
    was usable at each point in time.

    This is the honest version of "P/E vs its own 5y median". Using today's
    EPS against a 5-year price history (or vice versa) is the single most
    common way a backtest manufactures cheapness.
    """
    df = panel.get(symbol, as_of)
    if df.empty or len(df) < 60:
        return None
    end = df.index[-1]
    start = end - pd.Timedelta(days=int(365.25 * years))
    prices = df.loc[start:end]

    samples: List[float] = []
    # Periods whose fundamentals were usable at the time we sample them.
    periods = list(quarters) + list(annual)
    for rec in periods:
        if not (start <= rec.usable_from <= end):
            continue
        if rec.usable_from not in prices.index:
            eligible = prices.index[prices.index <= rec.usable_from]
            if len(eligible) == 0:
                continue
            ts = eligible[-1]
        else:
            ts = rec.usable_from
        price = float(prices.loc[ts, "Close"])

        if kind == "pe":
            eps = rec.get("eps_diluted") or rec.get("eps_basic")
            if eps and eps > 0 and price > 0:
                samples.append(price / eps)
        else:
            bvps = _safe_div(rec.get("equity"), rec.get("shares"))
            if bvps and bvps > 0 and price > 0:
                samples.append(price / bvps)

    if len(samples) < 3:
        return None
    return float(np.median(samples))


# ─────────────────────────────────────────────────────────────────────────────
# Hard filters
# ─────────────────────────────────────────────────────────────────────────────

def passes_non_financial(m: Metrics) -> Optional[str]:
    """Returns None if the name clears every non-financial filter, else why not."""
    f = investor_config.non_financial_filters

    if m.roce is None:
        return "roce_unknown"
    if m.roce < float(f.get("min_roce", 15.0)):
        return f"roce_{m.roce:.1f}<{f.get('min_roce', 15.0)}"

    max_de = float(f.get("max_debt_equity", 1.0))
    min_cov = float(f.get("min_interest_coverage", 4.0))
    if m.debt_to_equity is None and m.interest_coverage is None:
        return "leverage_unknown"
    if not (
        (m.debt_to_equity is not None and m.debt_to_equity < max_de)
        or (m.interest_coverage is not None and m.interest_coverage > min_cov)
    ):
        return f"leverage(de={m.debt_to_equity},cov={m.interest_coverage})"

    if m.ocf_to_pat is None:
        return "ocf_pat_unknown"
    if m.ocf_to_pat < float(f.get("min_ocf_to_pat_3y", 0.8)):
        return f"ocf_pat_{m.ocf_to_pat:.2f}"

    if m.promoter_pledge_pct is None:
        # Documented data gap: this source carries no pledge data for most
        # names. Config decides whether that is disqualifying.
        if bool(f.get("pledge_required", False)):
            return "pledge_unknown"
    elif m.promoter_pledge_pct >= float(f.get("max_promoter_pledge_pct", 10.0)):
        return f"pledge_{m.promoter_pledge_pct:.1f}"

    if m.revenue_cagr_3y is None:
        return "revenue_cagr_unknown"
    if m.revenue_cagr_3y < float(f.get("min_revenue_cagr_3y", 10.0)):
        return f"revenue_cagr_{m.revenue_cagr_3y:.1f}"

    if m.eps_cagr_3y is None:
        return "eps_cagr_unknown"
    if m.eps_cagr_3y < float(f.get("min_eps_cagr_3y", 12.0)):
        return f"eps_cagr_{m.eps_cagr_3y:.1f}"

    if bool(f.get("require_no_loss_year_3y", True)):
        if m.loss_years_3y is None:
            return "loss_years_unknown"
        if m.loss_years_3y > 0:
            return f"loss_years_{m.loss_years_3y}"

    return None


def passes_financial(m: Metrics) -> Optional[str]:
    """Financials get their own gate: ROE >= 14%, GNPA < 3%, P/B vs own 5y median."""
    f = investor_config.financial_filters

    roe = m.roe
    if roe is None:
        return "roe_unknown"
    if roe < float(f.get("min_roe", 14.0)):
        return f"roe_{roe:.1f}<{f.get('min_roe', 14.0)}"

    gnpa = m.gnpa_pct
    if gnpa is None:
        if bool(f.get("gnpa_required", False)):
            return "gnpa_unknown"
    elif gnpa >= float(f.get("max_gnpa_pct", 3.0)):
        return f"gnpa_{gnpa:.2f}"

    if m.pb is None or m.pb_5y_median is None or m.pb_5y_median <= 0:
        return "pb_median_unknown"
    if m.pb / m.pb_5y_median > float(f.get("pb_vs_5y_median_max", 1.30)):
        return f"pb_vs_median_{m.pb / m.pb_5y_median:.2f}"

    return None


def hard_filter_reason(m: Metrics, is_financial: bool) -> Optional[str]:
    return passes_financial(m) if is_financial else passes_non_financial(m)


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """Percentile rank in [0, 100]. NaNs stay NaN, never become a 50."""
    if series.dropna().empty:
        return pd.Series(0.0, index=series.index)
    return series.rank(pct=True, na_option="keep", ascending=ascending) * 100.0


def sector_neutral_percentile(df: pd.DataFrame, column: str, ascending: bool = True) -> pd.Series:
    """Percentile of `column` computed WITHIN each sector.

    A pharma at ROCE 22 should not automatically beat a bank at ROCE 26 purely
    because banks structurally run lower capital turns. Scoring sector-neutrally
    is what stops the composite degenerating into "own one sector" — and with a
    25% sector cap, a non-neutral score would just fight the cap.
    """
    if column not in df.columns:
        return pd.Series(0.0, index=df.index)
    out = pd.Series(index=df.index, dtype="float64")
    sectors = df["_sector"] if "_sector" in df.columns else pd.Series("ALL", index=df.index)
    for sec in sectors.unique():
        mask = sectors == sec
        out.loc[mask] = _pct_rank(df.loc[mask, column], ascending=ascending)
    return out.fillna(0.0)


def quality_raw(m: Metrics, is_financial: bool) -> Optional[float]:
    """Quality inputs (40% weight): returns/returns, margin stability, cash conversion."""
    if is_financial:
        vals = [m.roe, -(m.debt_to_equity or 0.0) if m.debt_to_equity else None]
        return _mean_available(vals)
    vals = [m.roce, m.margin_stability, m.ocf_to_pat]
    return _mean_available(vals)


def growth_raw(m: Metrics) -> Optional[float]:
    """Growth inputs (30%): 3y CAGRs plus last-two-quarter YoY acceleration."""
    vals = [m.eps_cagr_3y, m.revenue_cagr_3y, m.eps_accel, m.revenue_accel]
    return _mean_available(vals)


def valuation_raw(m: Metrics) -> Optional[float]:
    """Valuation inputs (30%): earnings yield, PEG, P/E vs own 5y median.

    `pe_vs_5y` is expressed as median/current so that HIGHER means cheaper —
    keeps the whole leg uniformly "bigger is better" before ranking.
    """
    pe_vs = None
    if m.pe and m.pe_5y_median and m.pe_5y_median > 0 and m.pe > 0:
        pe_vs = m.pe_5y_median / m.pe
    vals = [m.earnings_yield, _negate(m.peg), pe_vs]
    return _mean_available(vals)


def _negate(v: Optional[float]) -> Optional[float]:
    return None if v is None else -float(v)


def _mean_available(values: Sequence[Optional[float]]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and np.isfinite(v)]
    return float(np.mean(vals)) if vals else None


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class FundamentalEngine:
    """Scores the investable cross-section and produces the eligible list."""

    def __init__(
        self,
        store: PointInTimeFundamentals,
        sector_map: SectorMap,
        panel: Optional[PricePanel] = None,
    ):
        self.store = store
        self.sectors = sector_map
        self.panel = panel

    def metrics_for(self, symbol: str, as_of) -> Metrics:
        if self.panel is None:
            return Metrics(symbol=symbol)
        return compute_metrics(self.store, self.panel, symbol, as_of)

    def score_universe(
        self,
        as_of,
        symbols: Optional[Sequence[str]] = None,
        apply_eligibility: bool = True,
    ) -> pd.DataFrame:
        """One row per symbol: components, composite score, eligibility, reason.

        `apply_eligibility=False` lets the Technical-only ablation run over the
        full universe instead of only the names Layer 1 would have picked.
        """
        syms = list(symbols) if symbols is not None else self.store.symbols()
        rows: Dict[str, Dict] = {}
        sectors: Dict[str, str] = {}
        is_fin: Dict[str, bool] = {}

        for sym in syms:
            fin = self.sectors.is_financial(sym)
            is_fin[sym] = fin
            sectors[sym] = self.sectors.get(sym)
            m = self.metrics_for(sym, as_of)
            if m.n_years == 0:
                rows[sym] = {
                    **m.as_row(), "reject_reason": "no_filing_before_date",
                    "period_end": None, "usable_from": None,
                }
                continue
            rows[sym] = {
                **m.as_row(),
                "reject_reason": hard_filter_reason(m, fin),
                "period_end": m.period_end, "usable_from": m.usable_from,
            }

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame.from_dict(rows, orient="index")
        df.index.name = "symbol"
        df["_sector"] = pd.Series(sectors)
        df["_is_financial"] = pd.Series(is_fin)

        metrics_by_symbol = {s: self.metrics_for(s, as_of) for s in rows}
        df["_q_raw"] = [
            quality_raw(metrics_by_symbol[s], is_fin[s]) for s in df.index
        ]
        df["_g_raw"] = [growth_raw(metrics_by_symbol[s]) for s in df.index]
        df["_v_raw"] = [valuation_raw(metrics_by_symbol[s]) for s in df.index]

        if investor_config.fundamentals.get("sector_neutral", True):
            df["quality"] = sector_neutral_percentile(df, "_q_raw", ascending=True)
            df["growth"] = sector_neutral_percentile(df, "_g_raw", ascending=True)
            df["valuation"] = sector_neutral_percentile(df, "_v_raw", ascending=True)
        else:
            df["quality"] = _pct_rank(df["_q_raw"], ascending=True).fillna(0.0)
            df["growth"] = _pct_rank(df["_g_raw"], ascending=True).fillna(0.0)
            df["valuation"] = _pct_rank(df["_v_raw"], ascending=True).fillna(0.0)

        w = investor_config.score_weights
        df["score"] = (
            df["quality"].fillna(0.0) * float(w.get("quality", 0.40))
            + df["growth"].fillna(0.0) * float(w.get("growth", 0.30))
            + df["valuation"].fillna(0.0) * float(w.get("valuation", 0.30))
        ).clip(0.0, 100.0)

        # Only names that cleared the hard filters may be eligible. A stock that
        # fails ROCE does not become "eligible but low scoring" — that would let
        # a deep-value junk name into the book whenever quality names are scarce.
        df["_passes_hard"] = df["reject_reason"].isna()
        df["eligible"] = df["_passes_hard"] & (df["score"] > 0)

        if apply_eligibility:
            cutoff = float(investor_config.fundamentals.get("eligible_percentile", 70.0))
            passing = df[df["eligible"]]
            if len(passing) >= 3:
                threshold = float(np.percentile(passing["score"], cutoff))
                drop = df["eligible"] & (df["score"] < threshold)
                df.loc[drop, "eligible"] = False
                df.loc[drop, "reject_reason"] = "below_top30pct"

        df["rank"] = df["score"].rank(ascending=False, method="first")
        return df.sort_values("rank")

    # ── eligible list helpers ───────────────────────────────────────────────
    def eligible_list(self, scored: pd.DataFrame, max_names: Optional[int] = None) -> List[str]:
        """Top-30%-by-score names, padded up to the portfolio floor.

        The brief defines the eligible list as the top 30% of the cross-section,
        which on a mid-cap-heavy universe can be fewer names than the 20-30
        holding band the portfolio is supposed to run. When that happens the
        list is topped up to `min_holdings` using the NEXT-BEST names that
        still cleared the hard filters.

        The padding deliberately cannot reach past the hard-filter gate: a book
        of 20 names is a different risk profile from one of 10, but a book that
        includes ROCE-8% banks is not this strategy at all.
        """
        if scored.empty:
            return []
        cap = int(max_names or investor_config.portfolio.get("max_holdings", 30))
        floor = int(investor_config.portfolio.get("min_holdings", 20))

        passing = scored[scored["_passes_hard"] & (scored["score"] > 0)]
        if passing.empty:
            return []
        ordered = list(passing.sort_values("score", ascending=False).index)

        cutoff_pct = float(investor_config.fundamentals.get("eligible_percentile", 70.0))
        keep = max(3, int(round(len(ordered) * (100.0 - cutoff_pct) / 100.0)))
        keep = min(keep, cap)
        names = ordered[:keep]

        if len(names) < min(floor, cap) and len(ordered) > len(names):
            names = ordered[: min(floor, cap)]
        return names[:cap]

    def refresh_due(self, last_refresh: Optional[pd.Timestamp], as_of) -> bool:
        """True when the quarterly eligible-list refresh is due."""
        if last_refresh is None:
            return True
        months = int(investor_config.fundamentals.get("refresh_months", 3))
        return (pd.Timestamp(as_of) - pd.Timestamp(last_refresh)).days >= months * 30

    def quarterly_cutoff(self, scored: pd.DataFrame) -> Optional[float]:
        """Score threshold separating the top 50% — drives the quarterly drop rule.

        Computed over names that cleared the HARD filters, not over the
        post-top-30% eligible list. Using the eligible list instead would make
        the cutoff always sit at its own median, which by construction keeps
        every currently-eligible name eligible and makes the quarterly drop rule
        a no-op that silently never fires.
        """
        if scored.empty or "_passes_hard" not in scored.columns:
            return None
        passing = scored[scored["_passes_hard"]]
        if len(passing) < 4:
            return None
        return float(np.percentile(passing["score"], 50.0))

    def coverage(self, scored: pd.DataFrame) -> Dict[str, int]:
        """Why names were excluded — the honest picture of data gaps."""
        if scored.empty:
            return {}
        reasons = scored["reject_reason"].fillna("eligible").value_counts()
        unknown = int(sum(
            n for r, n in reasons.items()
            if isinstance(r, str) and r.endswith("_unknown")
        ))
        return {
            "total": int(len(scored)),
            "eligible": int(scored["eligible"].sum()),
            "unknown_data_rejects": unknown,
        }


__all__ = [
    "Metrics", "FundamentalEngine", "compute_metrics",
    "passes_non_financial", "passes_financial", "hard_filter_reason",
    "sector_neutral_percentile", "quality_raw", "growth_raw", "valuation_raw",
    "_cagr", "_safe_div", "_pct_rank",
]