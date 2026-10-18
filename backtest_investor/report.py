# backtest_investor/report.py
"""Report generation: stats table, equity-curve PNG, QuantStats, markdown.

The markdown deliberately leads with WHAT WAS AND WAS NOT MEASURABLE — the data
limits found while building this (fundamentals only exist from 2021, no
historical news, no LIQUIDBEES/GOLDBEES prices) matter more to a reader deciding
whether to trust the CAGR than the CAGR itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest_investor.benchmarks import TRI_NOTE
from investor.config import investor_config

RESULTS_SUBDIR = "runs"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Equity curve
# ─────────────────────────────────────────────────────────────────────────────

def _equity_series(res) -> Optional[pd.Series]:
    """Extracts just the equity line from a run or benchmark result.

    `BacktestResult.equity` is a DataFrame (equity/cash/invested/...), so
    passing it straight to `ax.plot(df.index, df.values)` draws one line PER
    COLUMN — which is how an earlier version produced a 21-entry legend with
    each series repeated seven times.
    """
    eq = getattr(res, "equity", None)
    if eq is None:
        return None
    if isinstance(eq, pd.DataFrame):
        if "equity" not in eq.columns:
            return None
        eq = eq["equity"]
    eq = pd.Series(eq).dropna()
    return None if eq.empty else eq


def plot_equity_curve(
    runs: Dict[str, object],
    benchmarks: Dict[str, object],
    out_path: Path,
    title: str = "Investor Mode — out-of-sample vs benchmarks (matched cash flows)",
) -> Optional[Path]:
    """Saves the OOS equity curve against its same-cash-flow benchmarks.

    Only the out-of-sample lump-sum run is plotted against the benchmarks.
    Putting a ₹10k/month SIP run on the same axis as a ₹10L lump sum would make
    the two incomparable, and the earlier version plotted every run at once.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning(f"matplotlib unavailable, skipping PNG: {exc}")
        return None

    fig, axes = plt.subplots(2, 1, figsize=(13, 9), height_ratios=[3, 1.2])
    ax, axdd = axes

    plotted: List[str] = []
    primary = runs.get("oos_lump")
    for label, res in [("Investor Mode (OOS)", primary)] + list(benchmarks.items()):
        if res is None:
            continue
        s = _equity_series(res)
        if s is None or s.empty:
            continue
        style = {"linewidth": 2.4, "color": "#c0392b"} if res is primary else {}
        ax.plot(s.index, s.values, label=label, **style)
        plotted.append(label)

    ax.set_title(title)
    ax.set_ylabel("Portfolio value (₹)")
    ax.set_xlabel("")
    if plotted:
        ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(alpha=0.25)
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"{v/1_000_000:,.1f}M")
    )

    s = _equity_series(primary)
    if s is not None and len(s) > 1:
        dd = (s / s.cummax() - 1.0) * 100.0
        axdd.fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.45)
        axdd.set_ylabel("Drawdown %")
        axdd.grid(alpha=0.25)
        axdd.set_title(
            f"Drawdown — Investor Mode (OOS), max {dd.min():.2f}%", fontsize=9
        )

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path


def write_quantstats(
    equity: pd.Series,
    out_path: Path,
    title: str = "Investor Mode",
) -> Optional[Path]:
    """QuantStats HTML tearsheet. Returns None when quantstats is missing."""
    try:
        import quantstats as qs
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning(f"quantstats unavailable, skipping tearsheet: {exc}")
        return None
    try:
        if len(equity) < 2:
            return None
        rets = equity.pct_change().dropna()
        if len(rets) < 2:
            return None
        qs.reports.html(rets, output=str(out_path), title=title)
        return out_path
    except Exception as exc:
        logger.warning(f"QuantStats tearsheet failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Tables
# ─────────────────────────────────────────────────────────────────────────────

def stats_table(stats: Dict[str, object]) -> pd.DataFrame:
    rows = []
    for label, s in stats.items():
        rows.append({
            "Run": label,
            "CAGR %": round(s.cagr_pct, 2) if s.cagr_pct else "—",
            "XIRR %": round(s.xirr_pct, 2) if s.xirr_pct is not None else "—",
            "Final ₹": f"{s.final_value:,.0f}",
            "Contributed ₹": f"{s.total_contributed:,.0f}",
            "MaxDD %": round(s.max_drawdown_pct, 2),
            "Vol %": round(s.volatility_pct, 2),
            "Sharpe": round(s.sharpe, 2),
            "Sortino": round(s.sortino, 2),
            "Calmar": round(s.calmar, 2),
            "R3y min %": round(s.rolling_3y_cagr_min, 1) if s.rolling_3y_cagr_min is not None else "—",
            "R3y med %": round(s.rolling_3y_cagr_median, 1) if s.rolling_3y_cagr_median is not None else "—",
            "R3y max %": round(s.rolling_3y_cagr_max, 1) if s.rolling_3y_cagr_max is not None else "—",
            "Turnover x/yr": round(s.turnover_pct, 2),
            "AvgHoldings": round(s.avg_holdings, 1),
            "%Cash": round(s.pct_time_cash, 1),
            "WinRate %": round(s.win_rate_pct, 1),
            "AvgHoldDays": round(s.avg_holding_days, 0),
            "Costs ₹": f"{s.total_costs:,.0f}",
        })
    return pd.DataFrame(rows)


def _md_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(no data)_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in cols) + " |")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Markdown
# ─────────────────────────────────────────────────────────────────────────────

def _data_limits(earliest_fundamentals: Optional[str]) -> List[str]:
    """The honest caveats. These belong at the TOP of the report."""
    lines = [
        "**News is a proxy.** No historical headline archive exists for Indian "
        "equities, so every backtest run tags `news=proxy` and uses the "
        "earnings-day gap/volume signature in place of FinBERT/LLM "
        "classification. The news layer is forward-test only until enough "
        "real headlines have been persisted to `news_events`.",
    ]
    if earliest_fundamentals:
        lines.append(
            f"**Fundamentals only exist from {earliest_fundamentals}.** The "
            "statement source provides annual periods from 2021 onward, so the "
            "F layer is unavailable before that date. Runs that use F are "
            "therefore only meaningful from that point on; earlier history "
            "exercises the Technical/Regime/News layers alone."
        )
    lines.append(
        "**Defensive sleeve is cash.** LIQUIDBEES and GOLDBEES have no price "
        "history in this repo, so when the regime layer cuts exposure the "
        "remainder accrues at the configured cash yield instead of tracking "
        "those ETFs. The gold sleeve is modelled but not price-accurate."
    )
    lines.append(
        "**Price history starts 2016-09-12**, not 2012. The OHLC archive in "
        "`data/raw/ohlc` begins then, so the requested 2012 start is not "
        "achievable with clean data."
    )
    lines.append(
        "**Promoter pledge and GNPA are not in the data source.** Both filters "
        "are armed in `thresholds.yaml` and enforced wherever data exists, but "
        "the vendor endpoint carries neither for Indian names, so names with "
        "unknown values pass those two checks by configuration rather than by "
        "evidence. See `pledge_required` / `gnpa_required`."
    )
    return lines


def build_markdown(
    stats: Dict[str, object],
    runs: Dict[str, object],
    benchmarks_lump: Dict[str, object],
    benchmarks_sip: Dict[str, object],
    ablations: Dict[str, object],
    robustness: Optional[pd.DataFrame],
    robustness_summary: Optional[Dict],
    universe: List[str],
    earliest_fundamentals: Optional[str],
    args: Dict,
) -> str:
    oos = stats.get("oos_lump") or next(iter(stats.values()))
    full = stats.get("full_sample_lump")

    md: List[str] = []
    md.append("# Investor Mode — Backtest Report\n")
    md.append(
        f"Generated by `vyuha invest backtest`. "
        f"Window: **{args.get('start')} → {args.get('end')}**, "
        f"out-of-sample from **{args.get('oos_start')}**. "
        f"News mode: **proxy**.\n"
    )

    md.append("\n## ⚠️ What this backtest can and cannot measure\n")
    for line in _data_limits(earliest_fundamentals):
        md.append(f"- {line}")
    md.append("")

    md.append("\n## Headline numbers\n")
    md.append(
        "> **Lump-sum CAGR and SIP XIRR are reported separately and never "
        "mixed.** CAGR on a lump sum is only meaningful when the whole capital "
        "went in on day one; once monthly contributions exist the money-weighted "
        "return (XIRR) is the honest figure.\n"
    )
    md.append(_md_table(stats_table(stats)))

    md.append("\n### Out-of-sample (the number to trust)\n")
    if oos:
        md.append(
            f"- **CAGR (lump sum): {oos.cagr_pct:.2f}%** over "
            f"{oos.years:.2f} years\n"
            f"- **Max drawdown: {oos.max_drawdown_pct:.2f}%** "
            f"(trough {oos.drawdown_trough})\n"
            f"- **Sharpe {oos.sharpe:.2f} / Sortino {oos.sortino:.2f} / "
            f"Calmar {oos.calmar:.2f}**\n"
            f"- **Volatility {oos.volatility_pct:.2f}%**\n"
            f"- **Average holdings {oos.avg_holdings:.1f}**, "
            f"**time in cash {oos.pct_time_cash:.1f}%**\n"
            f"- **Win rate {oos.win_rate_pct:.1f}%**, "
            f"**average hold {oos.avg_holding_days:.0f} days**\n"
            f"- **Total costs paid: ₹{oos.total_costs:,.0f}**\n"
        )
        if oos.rolling_3y_cagr_min is not None:
            md.append(
                f"- **Rolling 3y CAGR: min {oos.rolling_3y_cagr_min:.1f}% / "
                f"median {oos.rolling_3y_cagr_median:.1f}% / "
                f"max {oos.rolling_3y_cagr_max:.1f}%** — the *minimum* is the "
                f"one that says whether this survives a bad cycle.\n"
            )

    if full and oos is not full:
        md.append(
            f"\nFull-sample (in-sample + out-of-sample) CAGR was "
            f"**{full.cagr_pct:.2f}%** vs out-of-sample **{oos.cagr_pct:.2f}%**. "
            f"A large positive gap means the strategy was effectively tuned on "
            f"the in-sample period; it is reported rather than hidden.\n"
        )

    md.append("\n## Benchmarks (same cash flows)\n")
    md.append(f"_{TRI_NOTE}_\n")
    bench_rows = []
    for name, res in benchmarks_lump.items():
        if getattr(res, "stats", None) is None:
            continue
        bench_rows.append({
            "Benchmark": res.stats.label,
            "CAGR %": round(res.stats.cagr_pct, 2) if res.stats.cagr_pct else "—",
            "XIRR %": round(res.stats.xirr_pct, 2) if res.stats.xirr_pct is not None else "—",
            "MaxDD %": round(res.stats.max_drawdown_pct, 2),
            "Sharpe": round(res.stats.sharpe, 2),
            "Final ₹": f"{res.stats.final_value:,.0f}",
        })
    if oos:
        bench_rows.append({
            "Benchmark": "**Investor Mode (OOS)**",
            "CAGR %": round(oos.cagr_pct, 2),
            "XIRR %": round(oos.xirr_pct, 2) if oos.xirr_pct is not None else "—",
            "MaxDD %": round(oos.max_drawdown_pct, 2),
            "Sharpe": round(oos.sharpe, 2),
            "Final ₹": f"{oos.final_value:,.0f}",
        })
    md.append(_md_table(pd.DataFrame(bench_rows)))

    md.append("\n## Layer ablation\n")
    md.append(
        "Each row is an independent replay over the same window and universe "
        "with one or more layers disabled, so the delta is attributable to the "
        "layer rather than to a different sample.\n"
    )
    if ablations:
        from backtest_investor.ablations import ablation_table as _at
        md.append(_md_table(_at(ablations)))
    else:
        md.append("_(ablations skipped)_\n")

    md.append("\n## Robustness\n")
    if robustness is not None and not robustness.empty:
        md.append(_md_table(robustness.round(3).rename(columns={
            "cagr": "CAGR %", "max_drawdown": "MaxDD %",
            "baseline_cagr": "baseline CAGR", "delta_cagr": "Δ CAGR",
            "collapses": "COLLAPSES",
        })))
        if robustness_summary:
            md.append(
                f"\n**Verdict: {robustness_summary['verdict']}** "
                f"({robustness_summary['n_collapsed']} of "
                f"{robustness_summary['n_scenarios']} scenarios collapse).\n"
            )
    else:
        md.append("_(robustness skipped)_\n")

    md.append("\n## Artifacts\n")
    md.append(
        "- `equity_curve.png` — equity curves vs benchmarks + drawdown panel\n"
        "- `quantstats.html` — QuantStats tearsheet\n"
        "- `stats.csv` — the statistics table above\n"
    )

    md.append("\n## Universe\n")
    md.append(f"{len(universe)} symbols with OHLC history, point-in-time "
              f"resolved via `universe/computed_point_in_time.py`.\n")

    return "".join(md)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def write_report(
    outdir: str,
    stats: Dict[str, object],
    runs: Dict[str, object],
    benchmarks_lump: Dict[str, object],
    benchmarks_sip: Dict[str, object],
    ablations: Dict[str, object],
    robustness: Optional[pd.DataFrame],
    robustness_summary: Optional[Dict],
    universe: List[str],
    earliest_fundamentals: Optional[str],
    args: Dict,
) -> Path:
    """Writes every artifact and returns the output directory."""
    out = _ensure_dir(Path(outdir))

    primary = runs.get("oos_lump") or next(iter(runs.values()), None)
    plot_equity_curve(runs, benchmarks_lump, out / "equity_curve.png")
    if primary is not None:
        write_quantstats(primary.equity["equity"], out / "quantstats.html")

    df = stats_table(stats)
    df.to_csv(out / "stats.csv", index=False)
    if robustness is not None and not robustness.empty:
        robustness.to_csv(out / "robustness.csv", index=False)
    if ablations:
        from backtest_investor.ablations import ablation_table as _at
        _at(ablations).to_csv(out / "ablations.csv", index=False)

    md = build_markdown(
        stats, runs, benchmarks_lump, benchmarks_sip, ablations,
        robustness, robustness_summary, universe, earliest_fundamentals, args,
    )
    (out / "REPORT.md").write_text(md, encoding="utf-8")
    logger.info(f"Report artifacts written to {out}")
    return out


__all__ = [
    "plot_equity_curve", "write_quantstats", "stats_table",
    "build_markdown", "write_report",
]