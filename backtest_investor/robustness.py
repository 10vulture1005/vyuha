# backtest_investor/robustness.py
"""Robustness sweeps — does the result survive being stressed?

Four families:

    costs        2x transaction costs and slippage
    thresholds   +/-20% on the 200DMA length, the Chandelier ATR multiple and
                 the top-N (holdings) count
    subsample    random universe subsamples — if the edge lives in three
                 specific stocks, a random 60% universe should destroy it
    window       in-sample vs out-of-sample

`run_robustness` returns a table with a `COLLAPSES` flag per scenario. The
flag is not decorative: the brief asks for results that collapse under stress
to be called out, so a scenario that loses more than `collapse_threshold` of
its CAGR relative to the baseline is marked.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import pandas as pd
from loguru import logger

from backtest_investor.ablations import stats_from_result
from backtest_investor.engine import InvestorBacktester
from investor.config import reload_investor_config, investor_config
from investor.data import PricePanel, PointInTimeFundamentals, SectorMap

#: Fraction of baseline CAGR that may be lost before a scenario is flagged.
COLLAPSE_THRESHOLD = 0.50


@dataclass
class RobustnessRow:
    family: str
    scenario: str
    cagr: Optional[float]
    max_drawdown: Optional[float]
    sharpe: Optional[float]
    baseline_cagr: Optional[float]
    delta_cagr: Optional[float]
    collapses: bool
    note: str = ""


def _row(family, scenario, stats, baseline_cagr, note="") -> RobustnessRow:
    cagr = stats.cagr_pct if stats.cagr_pct else (stats.xirr_pct or 0.0)
    delta = (cagr - baseline_cagr) if baseline_cagr is not None else None
    collapses = False
    if delta is not None and baseline_cagr and baseline_cagr > 0:
        collapses = (delta / baseline_cagr) < -COLLAPSE_THRESHOLD
    return RobustnessRow(
        family=family, scenario=scenario, cagr=cagr,
        max_drawdown=stats.max_drawdown_pct, sharpe=stats.sharpe,
        baseline_cagr=baseline_cagr, delta_cagr=delta,
        collapses=collapses, note=note,
    )


def run_robustness(
    start,
    end,
    universe: Sequence[str],
    panel: Optional[PricePanel] = None,
    store: Optional[PointInTimeFundamentals] = None,
    sector_map: Optional[SectorMap] = None,
    initial_capital: Optional[float] = None,
    monthly_sip: float = 0.0,
    rf: float = 0.06,
    subsamples: int = 5,
    subsample_frac: float = 0.6,
    seed: int = 7,
    skip_thresholds: bool = False,
) -> pd.DataFrame:
    """Runs every stress scenario and returns a DataFrame with collapse flags."""
    panel = panel or PricePanel()
    store = store or PointInTimeFundamentals()
    sector_map = sector_map or SectorMap()
    rng = random.Random(seed)

    def _run(label, **kw):
        # `rf` is a metrics parameter, not an engine parameter — it reaches
        # stats_from_result below, never InvestorBacktester.
        opts = dict(
            start=start, end=end, universe=kw.pop("universe", universe),
            panel=panel, store=store, sector_map=sector_map,
            initial_capital=initial_capital, monthly_sip=monthly_sip,
            label=label,
        )
        opts.update(kw)
        bt = InvestorBacktester(**opts)
        res = bt.run()
        return res, stats_from_result(res, rf=rf)

    rows: List[RobustnessRow] = []

    # ── baseline ────────────────────────────────────────────────────────────
    logger.info("── robustness: baseline")
    _, base_stats = _run("baseline")
    base_cagr = base_stats.cagr_pct or base_stats.xirr_pct or 0.0
    rows.append(_row("baseline", "as configured", base_stats, base_cagr,
                     note="reference run"))

    # ── 2x costs and slippage ───────────────────────────────────────────────
    mult = float(investor_config.costs.get("robustness_cost_multiple", 2.0))
    logger.info(f"── robustness: {mult}x costs")
    _, s = _run("costs_2x", cost_multiplier=mult, slippage_multiplier=mult)
    rows.append(_row("costs", f"{mult:g}x costs + slippage", s, base_cagr))

    # ── threshold perturbation ──────────────────────────────────────────────
    if not skip_thresholds:
        for label, path, value in _threshold_variants():
            logger.info(f"── robustness: {label} = {value}")
            try:
                # Order matters: reload FIRST to get a clean config, then
                # perturb. Reloading after the override silently restores the
                # baseline and every variant reports an identical result —
                # which reads as "robust" and is entirely fake.
                reload_investor_config()
                _apply_override(path, value)
                if not _override_active(path, value):
                    raise RuntimeError(
                        f"override for {path} did not take effect — the "
                        f"threshold sweep would report a meaningless result"
                    )
                _, s = _run(label)
                rows.append(_row("thresholds", label, s, base_cagr,
                                 note=f"{path}={value}"))
            finally:
                reload_investor_config()

    # ── random universe subsamples ──────────────────────────────────────────
    syms = list(universe)
    if len(syms) > 20:
        k = int(len(syms) * subsample_frac)
        for i in range(subsamples):
            subset = rng.sample(syms, k)
            logger.info(f"── robustness: subsample {i+1}/{subsamples} (n={len(subset)})")
            _, s = _run(f"subsample_{i+1}", universe=subset)
            rows.append(_row("subsample", f"random {int(subsample_frac*100)}% #{i+1}",
                             s, base_cagr, note=f"n={len(subset)} seed={seed}"))

    df = pd.DataFrame([r.__dict__ for r in rows])
    return df


def _threshold_variants():
    """(label, config path, value) for each +/-20% threshold perturbation.

    Paths are rooted at `investor.` because that is where the block lives inside
    thresholds.yaml — omitting the prefix silently yields None for every
    variant and the whole threshold sweep is skipped without a word.
    """
    out = []
    specs = [
        ("dma_200_-20%", "investor.technical.trend_ma_days",
         lambda v: int(round(v * 0.8))),
        ("dma_200_+20%", "investor.technical.trend_ma_days",
         lambda v: int(round(v * 1.2))),
        ("atr_mult_-20%", "investor.technical.chandelier_atr_multiple",
         lambda v: round(v * 0.8, 2)),
        ("atr_mult_+20%", "investor.technical.chandelier_atr_multiple",
         lambda v: round(v * 1.2, 2)),
        ("topN_-20%", "investor.portfolio.max_holdings",
         lambda v: max(5, int(round(v * 0.8)))),
        ("topN_+20%", "investor.portfolio.max_holdings",
         lambda v: int(round(v * 1.2))),
    ]
    for label, path, fn in specs:
        current = _get_by_path(path)
        if current is None:
            logger.warning(f"robustness: config path '{path}' not found, skipping {label}")
            continue
        out.append((label, path, fn(float(current))))
    return out


def _get_by_path(path: str):
    from investor.config import thresholds as _t

    cur = _t
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _apply_override(path: str, value) -> None:
    """Temporarily sets a config value. Mutates the shared thresholds dict, so
    every caller in this process sees it — which is what makes the run use the
    perturbed value rather than a silently-ignored copy."""
    from investor.config import thresholds as _t

    parts = path.split(".")
    cur = _t
    for part in parts[:-1]:
        cur = cur[part]
    cur[parts[-1]] = value


def _override_active(path: str, value) -> bool:
    """Confirms a perturbation actually reached the live config.

    Without this check a threshold sweep can run entirely at baseline settings
    and still produce a confident "robust" verdict — which is worse than no
    sweep at all, because it looks like evidence.
    """
    return _get_by_path(path) == value


def summarize(df: pd.DataFrame) -> Dict[str, object]:
    """Headline verdict used by the markdown report."""
    if df.empty:
        return {"n_scenarios": 0, "n_collapsed": 0, "verdict": "no scenarios run"}
    collapsed = df[df["collapses"]]
    return {
        "n_scenarios": int(len(df)),
        "n_collapsed": int(len(collapsed)),
        "collapsed_scenarios": collapsed["scenario"].tolist(),
        "worst_scenario": str(df.loc[df["delta_cagr"].astype(float).idxmin(), "scenario"])
        if df["delta_cagr"].notna().any() else None,
        "verdict": (
            "ROBUST — no scenario loses more than half the baseline CAGR"
            if collapsed.empty else
            f"FRAGILE — {len(collapsed)} scenario(s) lose >50% of baseline CAGR"
        ),
    }


__all__ = ["RobustnessRow", "run_robustness", "summarize", "COLLAPSE_THRESHOLD"]