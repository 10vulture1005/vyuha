"""Observe the unchanged Investor replay before implementing trend_long.

Run with .venv/bin/python scripts/diagnose_investor_entry_funnel.py.
No downloads, DB queries, model loading, or strategy/config mutations. Artifacts
are separate from the published backtest so a new archive cannot overwrite the
old evidence. Rates use eligible name-days, not only unheld target candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def exposure_summary(equity: pd.DataFrame, split: str) -> pd.DataFrame:
    """Separate time with ANY stock from average capital deployed.

    The handoff's '% time in cash' is mean cash weight, not the fraction of
    days with zero holdings. Confusing those measures hides small books.
    """
    rows = []
    groups = [(str(y), g) for y, g in equity.groupby(equity.index.year)]
    groups += [("pre_2022", equity.loc[equity.index < split]),
               ("post_2022", equity.loc[equity.index >= split])]
    for label, frame in groups:
        if frame.empty:
            continue
        rows.append({
            "period": label, "days": len(frame),
            "days_invested_pct": 100 * frame.holdings.gt(0).mean(),
            "fully_cash_days_pct": 100 * frame.holdings.eq(0).mean(),
            "mean_cash_weight_pct": 100 * frame.cash_pct.mean(),
            "mean_equity_weight_pct": 100 * (frame.invested / frame.equity).mean(),
            "avg_holdings": frame.holdings.mean(),
        })
    return pd.DataFrame(rows)


def funnel_summary(daily: pd.DataFrame, split: str) -> pd.DataFrame:
    rows = []
    gates = ["snapshots", "trend", "momentum", "both", "breakout", "pullback", "any_entry"]
    groups = [(str(y), g) for y, g in daily.groupby(daily.index.year)]
    groups += [("pre_2022", daily.loc[daily.index < split]),
               ("post_2022", daily.loc[daily.index >= split])]
    for label, frame in groups:
        if frame.empty:
            continue
        denom = int(frame.eligible.sum())
        row = {"period": label, "decision_days": len(frame), "eligible_name_days": denom,
               "days_with_eligible": int(frame.eligible.gt(0).sum()),
               "buy_orders": int(frame.buy_orders.sum())}
        for gate in gates:
            row[gate + "_name_days"] = int(frame[gate].sum())
            row[gate + "_pct"] = 100 * frame[gate].sum() / denom if denom else None
        rows.append(row)
    return pd.DataFrame(rows)


def archive_manifest(panel) -> pd.DataFrame:
    rows = []
    for symbol in panel.available_symbols():
        path = panel.dir / f"{symbol}.csv"
        frame = panel.load(symbol)
        rows.append({
            "symbol": symbol, "is_index": symbol.startswith("INDEX_"),
            "bars": len(frame),
            "first": frame.index.min() if len(frame) else None,
            "last": frame.index.max() if len(frame) else None,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return pd.DataFrame(rows)


def replay(start, end, label, panel, store, sectors, universe, out):
    from backtest_investor.engine import InvestorBacktester
    from investor import technical
    from investor.config import investor_config

    bt = InvestorBacktester(start=start, end=end, universe=universe, panel=panel,
                            store=store, sector_map=sectors, monthly_sip=0,
                            initial_capital=investor_config.backtest["initial_capital"],
                            label=label)
    daily, refreshes = [], []
    evaluate = bt.engine.evaluate_day

    def observe(as_of, equity, cash, **kwargs):
        # Observe decisions after the original method returns. In particular,
        # do not remove news proxies, widen targets, or silently fix stops.
        previous_refresh = bt.engine.last_refresh
        decision = evaluate(as_of, equity, cash, **kwargs)
        row = {"date": as_of, "eligible": len(decision.eligible),
               "snapshots": 0, "trend": 0, "momentum": 0, "both": 0,
               "breakout": 0, "pullback": 0, "any_entry": 0,
               "buy_orders": sum(o.side == "BUY" for o in decision.orders),
               "holdings": len(bt.engine.positions), "exposure_cap": decision.exposure}
        for symbol in decision.eligible:
            snap = decision.snapshots.get(symbol)
            if snap is None:
                continue
            both = snap.trend_ok and snap.momentum_ok
            row["snapshots"] += 1
            row["trend"] += int(snap.trend_ok)
            row["momentum"] += int(snap.momentum_ok)
            row["both"] += int(both)
            row["breakout"] += int(both and snap.breakout_setup)
            row["pullback"] += int(both and snap.pullback_setup)
            row["any_entry"] += int(both and technical.entry_signal(snap, eligible=True) is not None)
        daily.append(row)
        if previous_refresh != bt.engine.last_refresh and decision.scored is not None:
            scored = decision.scored
            refreshes.append({"date": as_of, "universe": len(scored),
                              "hard_pass": int(scored._passes_hard.sum()),
                              "scored_top30": int(scored.eligible.sum()),
                              "standing_eligible": len(decision.eligible),
                              "reasons": json.dumps(scored.reject_reason.fillna("PASS").value_counts().to_dict())})
        return decision

    bt.engine.evaluate_day = observe
    result = bt.run(verbose=True)
    result.equity.to_csv(out / f"{label}_equity.csv")
    result.fills.to_csv(out / f"{label}_fills.csv", index=False)
    result.closed_trades.to_csv(out / f"{label}_closed.csv", index=False)
    daily = pd.DataFrame(daily).set_index("date")
    daily.to_csv(out / f"{label}_funnel_daily.csv")
    pd.DataFrame(refreshes).to_csv(out / f"{label}_refreshes.csv", index=False)
    split = investor_config.backtest["oos_start"]
    exposure = exposure_summary(result.equity, split)
    funnel = funnel_summary(daily, split)
    exposure.to_csv(out / f"{label}_exposure.csv", index=False)
    funnel.to_csv(out / f"{label}_funnel.csv", index=False)
    print(f"\n{label}: {len(bt.universe)} start-date names / {len(universe)} archive equities")
    print(exposure.round(4).to_string(index=False))
    print(funnel.round(4).to_string(index=False))
    years = (result.end - result.start).days / 365.25
    return {"label": label, "start": str(result.start), "end": str(result.end),
            "archive_equities": len(universe), "start_date_universe": len(bt.universe),
            "first_usable_annual_in_replay_universe": str(min(
                r.usable_from for s in bt.universe for r in store.history(s)
                if r.frequency == "yearly")),
            "initial_capital": result.initial_capital, "monthly_sip": 0,
            "final_value": result.final_value, "costs": result.total_costs,
            "fills": len(result.fills), "closed_trades": len(result.closed_trades),
            "cagr_pct_lump_only": 100 * ((result.final_value / result.initial_capital) ** (1 / years) - 1),
            "first_buy": str(result.fills.loc[result.fills.side.eq("BUY"), "date"].min())
            if not result.fills.empty else None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", default="docs/trend_long_phase0")
    args = ap.parse_args()
    # SEC-02: isolate BEFORE importing application settings. This command does
    # not use a DB, but a future transitive import must not touch vyuha.db.
    with tempfile.TemporaryDirectory(prefix="vyuha-phase0-") as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/diagnostics.db"
        from config.settings import settings
        from investor.config import investor_config
        from investor.data import PricePanel, PointInTimeFundamentals, SectorMap
        from investor.fundamentals import FundamentalEngine

        if settings.LIVE_TRADING_ENABLED:
            raise RuntimeError("Phase 0 is paper-only; live trading must remain disabled")
        panel, store, sectors = PricePanel(), PointInTimeFundamentals(), SectorMap()
        cfg = investor_config.backtest
        out = Path(args.outdir)
        out.mkdir(parents=True, exist_ok=True)
        manifest = archive_manifest(panel)
        manifest.to_csv(out / "archive_manifest.csv", index=False)
        universe = sorted(manifest.loc[~manifest.is_index, "symbol"])
        dates = sorted({cfg["start"], cfg["oos_start"], "2022-03-01", "2022-06-01",
                        "2023-06-01", "2025-06-16", cfg["end"]})
        fundamental = FundamentalEngine(store, sectors, panel)
        coverage = []
        for date in dates:
            scored = fundamental.score_universe(date, universe)
            scored.to_csv(out / f"fundamental_{date}.csv")
            coverage.append({"as_of": date, "archive_equities": len(universe),
                             "visible_any_statement": sum(store.as_of(s, date) is not None for s in universe),
                             "visible_annual": sum(store.latest_annual(s, date) is not None for s in universe),
                             "hard_pass": int(scored._passes_hard.sum()),
                             "standing_eligible": len(fundamental.eligible_list(scored)),
                             "no_filing": int(scored.reject_reason.eq("no_filing_before_date").sum())})
        coverage = pd.DataFrame(coverage)
        coverage.to_csv(out / "fundamental_coverage.csv", index=False)
        print("\nFundamental coverage:\n" + coverage.to_string(index=False))
        equities = manifest.loc[~manifest.is_index]
        last = equities["last"].max()
        raw_fundamentals = pd.read_csv(store.path)
        summary = {
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "thresholds_sha256": hashlib.sha256((ROOT / "config/thresholds.yaml").read_bytes()).hexdigest(),
            "fundamental_source_rows": raw_fundamentals.data_source.value_counts().to_dict(),
            "configured_window": cfg,
            "fundamental_file_sha256": hashlib.sha256(store.path.read_bytes()).hexdigest(),
            "fundamental_symbols": len(store.symbols()),
            "fundamental_records": sum(len(rs) for rs in store.records.values()),
            "first_usable_statement": str(min(r.usable_from for rs in store.records.values() for r in rs)),
            "first_usable_annual": str(min(r.usable_from for rs in store.records.values() for r in rs if r.frequency == "yearly")),
            "filing_dates_present": sum(r.filing_date is not None for rs in store.records.values() for r in rs),
            "csvs": len(manifest), "index_csvs": int(manifest.is_index.sum()),
            "equity_csvs": len(equities), "first_equity_bar": str(equities["first"].min()),
            "last_equity_bar": str(last),
            "equities_missing_last_10_calendar_days": equities.loc[equities["last"] < last - pd.Timedelta(days=10), "symbol"].tolist(),
            "equities_missing_last_365_calendar_days": equities.loc[equities["last"] < last - pd.Timedelta(days=365), "symbol"].tolist(),
            "constituents_directory_exists": (ROOT / "data/raw/constituents").exists(),
        }
        summary["runs"] = [replay(cfg["start"], cfg["end"], "full_sample_lump", panel,
                                  store, sectors, universe, out),
                           replay(cfg["oos_start"], cfg["end"], "oos_lump", panel,
                                  store, sectors, universe, out)]
        (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
        print("\n" + json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
