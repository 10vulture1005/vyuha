#!/usr/bin/env python3
"""Pre-registered variant sweep for the fixed trend_long exploratory backtest.

BACKTEST ON A SELECTED, SURVIVOR-BIASED ARCHIVE. Nothing here is live evidence.

Discipline is enforced by the stage CLI (see docs/trend_long_sweep/PREREGISTRATION.md):

    --stage tune   2016-10-17 .. 2021-12-31 ONLY. Picks the combination arm (f1) from
                   that window and nothing else. No 2022+ number is ever computed here.
    --stage oos    the SINGLE look at 2022+. Runs every pre-registered arm and every
                   +/-20% perturbation arm, then applies the acceptance tests. Nothing
                   observed here may feed back into the arm list.

Six pre-registered variant families (a)-(f); nine configurations including baseline:

    a  risk_pct              0.010 and 0.0125        (baseline 0.0075)
    b  chandelier_atr        4.5  and 5.0           (baseline 3.5)
    c  tax.defer_soft_exits  on                     (baseline off)
    d  regime exposure cap   disabled               (baseline on)
    e  universe top_n        150 vs 300             (baseline 300)
    f  combination of the best of (a)-(c), picked on 2016-2021 ONLY

ACCEPT a variant only if ALL four hold:
    A1 beats the Nifty500 same-flow XIRR net of costs, in BOTH windows, at BOTH
       capital scenarios;
    A2 clears the 200-seed placebo p90, at BOTH capital scenarios, in BOTH windows;
    A3 survives +/-20% on each changed parameter: every perturbed arm must itself pass
       A1 and A2. A binary switch has no +/-20%; its counterpart arm is the control and
       is reported as such;
    A4 with the five best trades removed it still beats Nifty500, in both windows at
       both capital scenarios - reported twice, with that cash idle and with it at the
       modelled 6% cash yield.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import trend_long_backtest as tl  # noqa: E402

TUNE = ("2016-10-17", "2021-12-31")
OOS = ("2022-01-01", None)
WINDOWS = {"tune": TUNE, "oos": OOS}
CAPITALS = {"lump_10L": (1_000_000.0, 0.0), "account_1L_SIP": (100_000.0, 1_000.0)}
SEEDS = list(range(1000, 1200))

ARMS: dict[str, dict] = {
    "baseline": {},
    "a1_risk_0100": {"risk_pct": 0.010},
    "a2_risk_0125": {"risk_pct": 0.0125},
    "b1_chandelier_45": {"chandelier_atr": 4.5},
    "b2_chandelier_50": {"chandelier_atr": 5.0},
    # defer_from_days=0: the [335,366] window from config/thresholds.yaml can never
    # fire here - no trade in the whole archive is held 335 days (max 241) - so the
    # Phase 1 default would make this arm a no-op rather than a test of tax deferral.
    "c1_defer_soft_exits": {"defer_soft_exits": True, "defer_from_days": 0},
    "d1_no_regime_cap": {"regime_cap_enabled": False},
    "e1_topn_150": {"top_n": 150},
}
COMBO = "f1_combo_best_of_a_b_c"
NUMERIC_KNOBS = ("risk_pct", "chandelier_atr", "top_n")
_P_CACHE: dict[int, dict] = {}


# --------------------------------------------------------------------------- engine glue
def arm_over(arm: str, combo: dict | None) -> dict:
    return dict(combo or {}) if arm == COMBO else dict(ARMS.get(arm, {}))


def arm_cfg(arm: str, combo: dict | None = None) -> dict:
    c = dict(tl.CFG)
    c.update(arm_over(arm, combo))
    return c


def get_panel(top_n: int):
    """Build the feature panel for a universe size (once per worker process)."""
    if top_n not in _P_CACHE:
        cal, stocks, idx = tl.load_archive(str(ROOT / "data" / "raw" / "ohlc"), None, None)
        _P_CACHE[top_n] = tl.build(cal, stocks, idx, arm_cfg("baseline", {"top_n": top_n}),
                                   str(ROOT / "config"))
    return _P_CACHE[top_n]


def _one(job):
    arm, combo, top_n, window, cap_key, seed = job
    cfg = arm_cfg(arm, combo)
    P = tl.slice_window(get_panel(top_n), start=window[0], end=window[1])
    capital, sip = CAPITALS[cap_key]
    res = tl.run(P, cfg, capital, sip, seed=seed)
    s = tl.stats(P["cal"], res["eq"], res["flow"], cfg["cash_yield"])
    bench = tl.benchmark(P["cal"], P["n5"], res["flow"])
    return s["xirr_pct"], bench["xirr_pct"]


def placebo_batch(arms, combo, window, cap_key, procs, label):
    jobs = [(a, combo if a == COMBO else None,
             arm_cfg(a, combo)["top_n"], window, cap_key, s) for a in arms for s in SEEDS]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=procs) as pool:
        out = list(pool.map(_one, jobs, chunksize=16))
    print(f"  {label}: {len(jobs)} runs in {time.time() - t0:.0f}s on {procs} processes")
    return [dict(arm=j[0], capital=cap_key, seed=j[5], xirr=o[0], bench_xirr=o[1])
            for j, o in zip(jobs, out)]


def full_eval(arm, combo, window_name, cap_key, keep_funnel=False):
    """Real run plus everything the acceptance table and the report need."""
    cfg = arm_cfg(arm, combo)
    P = tl.slice_window(get_panel(cfg["top_n"]), start=WINDOWS[window_name][0],
                        end=WINDOWS[window_name][1])
    capital, sip = CAPITALS[cap_key]
    res = tl.run(P, cfg, capital, sip)
    s = tl.stats(P["cal"], res["eq"], res["flow"], cfg["cash_yield"])
    bench = tl.benchmark(P["cal"], P["n5"], res["flow"])
    rob = tl.top_trade_robustness(P, res, cfg)
    tr = res["trades"]
    de = tr[tr.reason == "DATA_END_HAIRCUT"] if not tr.empty else tr
    en = tr[tr.reason == "END_LIQUIDATION"] if not tr.empty else tr
    row = dict(
        arm=arm, window=window_name, capital=cap_key,
        xirr=s["xirr_pct"], bench_xirr=bench["xirr_pct"], final=s["final"],
        invested=s["invested"], twr_cagr=s["twr_cagr_pct"], maxdd=s["max_dd_pct"],
        sharpe=s["sharpe"], costs=res["costs"], trades=int(len(tr)),
        win_rate=None if tr.empty else 100 * (tr["gain"] > 0).mean(),
        end_liq_n=int(len(en)), end_liq_pnl=float(en["gain"].sum()) if len(en) else 0.0,
        data_end_n=int(len(de)), data_end_pnl=float(de["gain"].sum()) if len(de) else 0.0,
        final_mark=res["final_mark"], xirr_no_top5_idle=rob["xirr_idle"],
        xirr_no_top5_cash=rob["xirr_cash"], vix_stale_days=int(P["vix_stale"].sum()),
        deferred_soft_exits=res["deferred_soft_exits"])
    if keep_funnel:
        row["funnel"] = tl.funnel_table(P, res)
    return row


def run_stage(tag, arms, combo, window_name, procs, out: Path, append=False):
    rows, placebos = [], []
    for cap_key in CAPITALS:
        for arm in arms:
            rows.append(full_eval(arm, combo, window_name, cap_key))
        placebos += placebo_batch(arms, combo, WINDOWS[window_name], cap_key, procs,
                                  f"placebo {window_name}/{cap_key}")
    for cap_key in CAPITALS:
        for arm in arms:
            if arm not in ("baseline", COMBO):
                continue
            table = full_eval(arm, combo, window_name, cap_key, keep_funnel=True).get("funnel")
            table.to_csv(out / f"funnel_{window_name}_{cap_key}.csv", index=False)
    real = pd.DataFrame(rows)
    mode = "a" if append else "w"
    real.to_csv(out / f"{tag}_real.csv", index=mode, header=not append)
    pd.DataFrame(placebos).to_csv(out / f"{tag}_placebo.csv", index=mode, header=not append)
    return real, pd.DataFrame(placebos)


# --------------------------------------------------------------------------- stages
def tune_stage(procs, out: Path):
    arms = list(ARMS)
    print("STAGE 1 - TUNE WINDOW 2016-10-17..2021-12-31 ONLY (no 2022+ number is computed)")
    real, _ = run_stage("tune", arms, None, "tune", procs, out)
    # pre-registered selection rule for (f): for each knob, the level with the higher
    # mean tune-window net-of-cost XIRR across the two capital scenarios; ties break
    # toward the baseline level.
    mean_x = real.groupby("arm")["xirr"].mean()
    families = {"risk_pct": {tl.CFG["risk_pct"]: "baseline", 0.010: "a1_risk_0100",
                             0.0125: "a2_risk_0125"},
                "chandelier_atr": {tl.CFG["chandelier_atr"]: "baseline", 4.5: "b1_chandelier_45",
                                   5.0: "b2_chandelier_50"},
                "defer_soft_exits": {False: "baseline", True: "c1_defer_soft_exits"}}
    combo = {knob: max(levels, key=lambda v: (mean_x[levels[v]],
                                              -abs((float(v)) - float(tl.CFG[knob]))))
             for knob, levels in families.items()}
    print("  selection made on the tune window only:", combo)
    (out / "tune_selection.json").write_text(json.dumps(combo, indent=2, default=bool))
    print(f"  wrote {out}/tune_real.csv, tune_placebo.csv, tune_selection.json")
    return combo


def perturb(arms):
    """Register every +/-20% arm in ARMS so arm_cfg() resolves it everywhere."""
    out = dict(arms)
    for name, over in arms.items():
        for knob in NUMERIC_KNOBS:
            if isinstance(over.get(knob), (int, float)) and not isinstance(over.get(knob), bool):
                for tag, mult in (("minus20", 0.8), ("plus20", 1.2)):
                    p = dict(over)
                    p[knob] = round(over[knob] * mult, 8)
                    out[f"{name}~{knob}_{tag}"] = p
    ARMS.update(out)
    return out


def oos_stage(procs, out: Path, combo: dict):
    all_arms = {**ARMS, COMBO: combo}
    kids = perturb(all_arms)
    print(f"STAGE 2 - SINGLE LOOK AT 2022+ ({len(all_arms)} arms + {len(kids)} perturbation arms)")
    print("  combination arm fixed by the tune window only:", combo)
    # A3 needs the +/-20% arms judged in both windows. The tune window is not held out
    # and nothing here is used to re-pick an arm, so running them there leaks nothing.
    run_stage("tune", kids, combo, "tune", procs, out, append=True)
    run_stage("oos", list(all_arms), combo, "oos", procs, out)


def evaluate(out: Path, seeds: int):
    real = pd.concat([pd.read_csv(out / "tune_real.csv").assign(window="tune"),
                      pd.read_csv(out / "oos_real.csv").assign(window="oos")],
                     ignore_index=True)
    pb = pd.concat([pd.read_csv(out / "tune_placebo.csv"),
                    pd.read_csv(out / "oos_placebo.csv")], ignore_index=True)
    p90 = pb.groupby(["window", "capital", "arm"])["xirr"].quantile(0.9)
    real["placebo_p90"] = [p90.get((w, c, a), np.nan) for w, c, a
                           in zip(real.window, real.capital, real.arm)]
    base_arms = [a for a in dict.fromkeys(real.arm) if "~" not in a]

    def a1(df):
        return bool((df.xirr > df.bench_xirr).all() and len(df) == 4)

    def a2(df):
        return bool((df.xirr > df.placebo_p90).all() and df.placebo_p90.notna().all())

    def a4(df):
        if df[["xirr_no_top5_idle", "xirr_no_top5_cash"]].isna().any().any():
            return False
        return bool((df.xirr_no_top5_idle > df.bench_xirr).all()
                    and (df.xirr_no_top5_cash > df.bench_xirr).all())

    rows = []
    for arm in base_arms:
        sub = real[real.arm == arm]
        kids = real[real.arm.str.startswith(arm + "~")]
        rows.append(dict(arm=arm,
                         A1_beats_benchmark=a1(sub), A2_beats_placebo_p90=a2(sub),
                         # vacuously true when the arm changes no numeric parameter
                         A3_survives_pm20=(all(a1(k) and a2(k) for _, k in kids)
                                           if len(kids) else True),
                         A4_survives_no_top5=a4(sub),
                         n_perturbation_arms=int(len(kids)),
                         perturb_arms=";".join(sorted({a.split("~")[1] for a in kids.arm}))))
    acc = pd.DataFrame(rows)
    acc["ACCEPTED"] = acc[["A1_beats_benchmark", "A2_beats_placebo_p90",
                           "A3_survives_pm20", "A4_survives_no_top5"]].all(axis=1)
    acc.to_csv(out / "acceptance.csv", index=False)
    real.to_csv(out / "all_real.csv", index=False)
    pb.to_csv(out / "all_placebo.csv", index=False)
    print(acc.to_string(index=False))
    print(f"\naccepted: {sorted(acc.loc[acc.ACCEPTED, 'arm']) or 'NONE'}")
    return acc, real


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=["tune", "oos", "eval"], required=True)
    ap.add_argument("--procs", type=int, default=20)
    ap.add_argument("--out", default="docs/trend_long_sweep")
    ap.add_argument("--seeds", type=int, default=200)
    a = ap.parse_args()
    global SEEDS
    SEEDS = list(range(1000, 1000 + a.seeds))
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    if a.stage == "tune":
        tune_stage(a.procs, out)
    elif a.stage == "oos":
        combo = json.loads((out / "tune_selection.json").read_text())
        combo["defer_soft_exits"] = bool(combo["defer_soft_exits"])
        oos_stage(a.procs, out, combo)
    else:
        evaluate(out, a.seeds)


if __name__ == "__main__":
    main()