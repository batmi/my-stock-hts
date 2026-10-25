"""Read-only KOSPI SMA gate audit; no application/config imports or live trading.

Primary design fixed before examining results: SMA periods below, 1% band,
10 bp one-way friction, prior close signal / next open execution, cash yield 0.
Annual walk-forward selects trailing-five-year net Sharpe; Calmar and CAGR
selectors are separately labelled sensitivity. 2026 is a previously observed
confirmation period, NOT pristine out-of-sample evidence.

This index/cash switching experiment exits on a blocked gate. Production only
blocks entries: use audit_kospi_entry_gate.py for a separate entry-only proxy.
Neither experiment is the live stock portfolio's performance.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PRIMARY = (20, 40, 50, 60, 80, 100, 120, 150, 200, 250)
PERIODS = tuple(range(20, 251, 10))
BANDS = (0.0, 0.5, 1.0, 1.5, 2.0)
OUT = ROOT / "data/audit_runs/kospi_filter_20261025"


def load_rows(db):
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        rows = [dict(r) for r in con.execute(
            "SELECT bas_dd AS date,open,high,low,close FROM index_daily "
            "WHERE api_id='kospi_dd_trd' AND name='코스피' ORDER BY bas_dd")]
    finally:
        con.close()
    validate_rows(rows)
    return rows


def validate_rows(rows):
    if not rows:
        raise ValueError("No KOSPI history")
    previous = ""
    for r in rows:
        d = datetime.strptime(r["date"], "%Y%m%d")
        if r["date"] <= previous or d.weekday() > 4:
            raise ValueError(f"Invalid index session {r['date']}")
        vals = [r[k] for k in ("open", "high", "low", "close")]
        if not all(v is not None and math.isfinite(v) and v > 0 for v in vals):
            raise ValueError(f"Non-positive/nonfinite OHLC {r['date']}")
        if r["low"] > min(r["open"], r["close"]) or r["high"] < max(r["open"], r["close"]):
            raise ValueError(f"Inconsistent OHLC {r['date']}")
        previous = r["date"]


def sma_gate(closes, period, band=1.0, initial_blocked=False):
    """Production strict inequalities; unknown warmup is not allowed to trade."""
    closes = np.asarray(closes, dtype=float)
    if period < 1 or band < 0 or not np.isfinite(closes).all():
        raise ValueError("Invalid signal input")
    # Exactly the production rolling mean. Weighted convolution can create
    # 100.00000000000003 on constant 100 prices and falsely cross a zero band.
    sma = pd.Series(closes).rolling(period).mean().to_numpy()
    blocked, allowed = bool(initial_blocked), np.zeros(len(closes), dtype=bool)
    for i in range(period-1, len(closes)):
        if closes[i] < sma[i]*(1-band/100):
            blocked = True
        elif closes[i] > sma[i]*(1+band/100):
            blocked = False
        allowed[i] = not blocked
    return allowed, sma


def positions(signal, start, lag=1):
    if lag < 1 or start < lag:
        raise ValueError("Signal must precede the execution session")
    return np.asarray(signal[start-lag:len(signal)-lag], dtype=float)


def simulate(rows, start, pos, cost_bps=10.0, cash_yield=0.0):
    """At each next OPEN switch position; value at that day's CLOSE.

    Old position bears the overnight gap. New position receives only open-close
    return. Calendar-day cash interest accrues overnight to the old cash holding.
    First interval starts all cash at its open. No forced terminal liquidation.
    Full or zero index exposure only; no leverage, shorts or assumed dividends.
    """
    pos = np.asarray(pos, dtype=float)
    if len(pos) != len(rows)-start or not np.isin(pos, (0.0, 1.0)).all():
        raise ValueError("Expected one binary position per evaluation session")
    selected = rows[start:]
    op = np.array([r["open"] for r in selected])
    cl = np.array([r["close"] for r in selected])
    prevcl = np.r_[op[0], cl[:-1]]
    old = np.r_[0.0, pos[:-1]]
    gaps = np.array([0] + [(datetime.strptime(b["date"], "%Y%m%d")-
                            datetime.strptime(a["date"], "%Y%m%d")).days
                           for a, b in zip(selected, selected[1:])])
    cash = np.expm1(np.log1p(cash_yield)*gaps/365.25)
    turnover = np.abs(pos-old)
    overnight = 1 + old*(op/prevcl-1) + (1-old)*cash
    fee = cost_bps/10000
    # Buy fee is charged on purchased notional: cash = notional*(1+fee).
    # Sell fee is charged on sale notional. No repeated fee while holding.
    fee_factor = np.where(pos>old,1/(1+fee),np.where(pos<old,1-fee,1.0))
    factors = overnight*fee_factor*(1+pos*(cl/op-1))
    return {"returns": factors-1, "position": pos, "turnover": turnover,
            "cash_daily": cash}


def metrics(dates, returns, pos=None, turnover=None):
    returns = np.asarray(returns, dtype=float)
    if not len(returns):
        return None
    years = max(1, (datetime.strptime(dates[-1], "%Y%m%d")-
                    datetime.strptime(dates[0], "%Y%m%d")).days+1)/365.25
    equity = np.cumprod(1+returns)
    peaks = np.maximum.accumulate(np.r_[1.0, equity])[1:]
    mdd = float(np.min(equity/peaks-1))
    cagr = float(equity[-1]**(1/years)-1)
    sd = float(np.std(returns, ddof=1)) if len(returns)>1 else 0.0
    return {"start": dates[0], "end": dates[-1], "days": len(returns),
            "return_pct": float((equity[-1]-1)*100), "cagr_pct": cagr*100,
            "mdd_pct": mdd*100, "volatility_pct": sd*math.sqrt(252)*100,
            "sharpe_zero_rf": float(np.mean(returns)/sd*math.sqrt(252)) if sd else None,
            "calmar": cagr/abs(mdd) if mdd < 0 else None,
            "exposure_pct": float(np.mean(pos)*100) if pos is not None else None,
            "one_way_orders": int(np.sum(turnover)) if turnover is not None else None,
            "orders_per_year": float(np.sum(turnover)/years) if turnover is not None else None}


def subset(dates, sim, start="00000000", end="99999999"):
    ids = np.flatnonzero((np.asarray(dates)>=start)&(np.asarray(dates)<=end))
    return metrics([dates[i] for i in ids], sim["returns"][ids],
                   sim["position"][ids], sim["turnover"][ids]) if len(ids) else None


def periods_report(dates, sim):
    windows = {"all": ("00000000", "99999999"), "pre2026": ("00000000", "20251231"),
               "2011_2015": ("20110101", "20151231"), "2016_2020": ("20160101", "20201231"),
               "2021_2025": ("20210101", "20251231"), "observed_2026": ("20260101", "20261231")}
    out = {k: subset(dates, sim, a, b) for k, (a,b) in windows.items()}
    out["annual"] = {year: subset(dates, sim, year+"0101", year+"1231")
                     for year in sorted({d[:4] for d in dates})}
    return out


def walk_forward(rows, start, signals, fixed_sims, objective="sharpe_zero_rf", train_years=5):
    dates = [r["date"] for r in rows[start:]]
    ds = np.asarray(dates)
    first_year = int(dates[0][:4])+train_years
    chosen_pos = np.zeros(len(dates))
    choices = []
    for year in range(first_year, int(dates[-1][:4])+1):
        train = (ds >= f"{year-train_years}0101") & (ds < f"{year}0101")
        test = (ds >= f"{year}0101") & (ds < f"{year+1}0101")
        if not test.any() or train.sum() < 200*train_years:
            continue
        scores = {}
        for p, sim in fixed_sims.items():
            m = metrics(list(ds[train]), sim["returns"][train], sim["position"][train])
            score = m[objective]
            scores[p] = score if score is not None and np.sum(sim["position"][train])>=50 else -1e9
        # Deterministic fixed tie break. No preference inferred from test data.
        chosen = sorted(scores, key=lambda p: (-scores[p], p))[0]
        chosen_pos[test] = positions(signals[chosen], start)[test]
        choices.append({"test_year": year, "training_start": ds[train][0],
                        "training_end": ds[train][-1], "chosen_ma": chosen,
                        "train_score": scores[chosen], "all_train_scores": scores})
    live_start = int(np.flatnonzero(ds>=f"{first_year}0101")[0])
    # Keep chosen positions continuously across annual changes. No artificial
    # year-end close/reopen, and no test-year opening quote enters training.
    sim = simulate(rows, start+live_start, chosen_pos[live_start:])
    out_dates = dates[live_start:]
    return {"objective": objective, "train_years": train_years, "choices": choices,
            "metrics": periods_report(out_dates, sim)}, sim, out_dates


def rolling_windows(dates, sim, years=3):
    result = []
    # Quarterly starts limit overlapping reports; overlap is still explicit.
    for year in range(int(dates[0][:4]), int(dates[-1][:4])-years+2):
        for month in (1,4,7,10):
            lo, hi = f"{year}{month:02}01", f"{year+years}{month:02}01"
            if hi > dates[-1]:
                continue
            ids = [i for i,d in enumerate(dates) if lo<=d<hi]
            if len(ids)<years*200:
                continue
            result.append(metrics([dates[i] for i in ids], sim["returns"][ids],
                                  sim["position"][ids], sim["turnover"][ids]))
    return result


def gate_episodes(rows, allowed, start):
    episodes, begin = [], None
    for i in range(start, len(rows)):
        blocked = not allowed[i]
        if blocked and begin is None:
            begin = i
        if begin is not None and (not blocked or i==len(rows)-1):
            finished = not blocked
            end = i
            episodes.append({"start": rows[begin]["date"], "end": rows[end]["date"],
                             "sessions": end-begin if finished else end-begin+1,
                             "completed": finished,
                             "index_change_pct": (rows[end]["close"]/rows[begin]["close"]-1)*100})
            begin = None
    done = [e for e in episodes if e["completed"]]
    return {"episodes": episodes, "completed": len(done),
            "short_at_most_5_sessions": sum(e["sessions"]<=5 for e in done),
            "index_rose_while_blocked": sum(e["index_change_pct"]>0 for e in done),
            "median_completed_sessions": float(np.median([e["sessions"] for e in done])) if done else None,
            "interpretation": "Descriptive blocked episodes, not a count of demonstrably wrong decisions."}


def drawdown_events(rows, start, signals):
    """Disjoint peak-to-recovery episodes; the final unrecovered event is censored."""
    peak = start
    trough = start
    result = []
    def emit(end, recovered):
        depth = rows[trough]["close"]/rows[peak]["close"]-1
        if depth > -0.10:
            return
        obs = {"peak": rows[peak]["date"], "trough": rows[trough]["date"],
               "end": rows[end]["date"], "recovered": recovered, "drawdown_pct": depth*100,
               "filters": {}}
        for p, gate in signals.items():
            blocked = [i for i in range(peak, trough+1) if not gate[i]]
            first = blocked[0] if blocked else None
            already_allowed = bool(gate[trough])
            reopen = trough if already_allowed else next((i for i in range(trough+1,end+1) if gate[i]), None)
            obs["filters"][str(p)] = {
                "first_block_signal": rows[first]["date"] if first is not None else None,
                "drop_at_first_block_pct": (rows[first]["close"]/rows[peak]["close"]-1)*100 if first is not None else None,
                "peak_to_trough_blocked_pct": len(blocked)/(trough-peak+1)*100,
                "already_allowed_at_trough": already_allowed,
                "was_ever_blocked_before_trough": bool(blocked),
                "first_allowed_after_trough": rows[reopen]["date"] if reopen is not None else None,
                "sessions_to_allowed_from_trough": reopen-trough if reopen is not None else None,
                "rebound_before_first_allowed_pct": (rows[reopen]["close"]/rows[trough]["close"]-1)*100 if reopen is not None else None}
        result.append(obs)
    for i in range(start+1, len(rows)):
        if rows[i]["close"]>=rows[peak]["close"]:
            if trough != peak:
                emit(i, True)
            peak = trough = i
        elif rows[i]["close"]<rows[trough]["close"]:
            trough = i
    if trough != peak:
        emit(len(rows)-1, False)
    return result


def paired_bootstrap(returns_by_p, dates, repetitions=2000, seed=20261025):
    """Circular moving-block bootstrap of paired daily excess returns vs SMA80.

    CI estimates annualized arithmetic mean difference, not CAGR or Calmar.
    Simultaneous intervals use a centered max absolute t statistic across the
    primary alternatives. They do not correct all historical research choices.
    """
    ids = np.array([i for i,d in enumerate(dates) if d<"20260101"])
    keys = [p for p in PRIMARY if p != 80]
    base = returns_by_p[80][ids]
    diff = np.stack([returns_by_p[p][ids]-base for p in keys], axis=1)
    means = np.mean(diff, axis=0)
    result = {}
    for block in (20,60,120):
        rng = np.random.default_rng(seed+block)
        samples = np.empty((repetitions,len(keys)))
        # Identical sampled dates for every parameter, preserving pairing.
        for b in range(repetitions):
            starts = rng.integers(0,len(ids),size=math.ceil(len(ids)/block))
            draw = ((starts[:,None]+np.arange(block))%len(ids)).ravel()[:len(ids)]
            samples[b] = diff[draw].mean(axis=0)
        se = np.std(samples,axis=0,ddof=1)
        safe = np.where(se>0,se,1.0)
        critical = float(np.quantile(np.max(np.abs((samples-means)/safe),axis=1),.95))
        result[str(block)] = {str(p): {
            "mean_difference_pp_per_year": means[j]*252*100,
            "paired_percentile_95": (np.quantile(samples[:,j],[.025,.975])*252*100).tolist(),
            "simultaneous_95": ((means[j]+np.array([-1,1])*critical*se[j])*252*100).tolist()
        } for j,p in enumerate(keys)}
    return {"sample": "pre2026", "repetitions": repetitions, "seed": seed,
            "statistic": "annualized arithmetic mean daily net return difference vs SMA80",
            "method": "paired circular blocks, pointwise percentile and centered max-t simultaneous CI",
            "blocks": result}


def rolling_history_check(closes, signals, start, bars=260):
    result = {}
    for p in PRIMARY:
        mismatch = []
        for i in range(start-1,len(closes)):
            short, _ = sma_gate(closes[max(0,i-bars+1):i+1],p,1.0)
            if short[-1] != signals[p][i]:
                mismatch.append(i)
        result[str(p)] = {"different_signal_days": len(mismatch), "indices": mismatch}
    return result


def production_parity(closes):
    """Extract only pure function AST, never import live config/application."""
    import pandas as pd
    source = ROOT/"core/indicators.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="get_market_filter_blocked")
    env = {"np": np, "pd": pd}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),"exec"),env)
    mismatches = {}
    for p in PERIODS:
        for band in BANDS:
            ours,_ = sma_gate(closes,p,band)
            actual = ~env["get_market_filter_blocked"](closes,p,band,False).to_numpy()
            n = int(np.sum(ours[p-1:] != actual[p-1:]))
            if n:
                mismatches[f"{p}:{band}"] = n
    if mismatches:
        raise ValueError(f"Production signal parity failed: {mismatches}")
    return {"combinations": len(PERIODS)*len(BANDS), "valid_bar_mismatches": 0,
            "production_function_sha256": hashlib.sha256(ast.unparse(node).encode()).hexdigest()}


def run(db, output, repetitions=2000):
    rows = load_rows(db)
    closes = np.array([r["close"] for r in rows])
    start = next(i for i,r in enumerate(rows) if r["date"]>="20110101" and i>=max(PRIMARY))
    dates = [r["date"] for r in rows[start:]]
    output.mkdir(parents=True,exist_ok=True)
    signal_map = {p: sma_gate(closes,p,1.0)[0] for p in PRIMARY}
    init_diff = {str(p): int(np.sum(signal_map[p][start-1:] != sma_gate(closes,p,1.0,True)[0][start-1:])) for p in PRIMARY}
    print("Data loaded; checking production signal parity",flush=True)
    parity = production_parity(closes)
    sims = {p: simulate(rows,start,positions(gate,start)) for p,gate in signal_map.items()}
    bh = simulate(rows,start,np.ones(len(dates)))
    cash = simulate(rows,start,np.zeros(len(dates)))
    fixed = {str(p): periods_report(dates,s) for p,s in sims.items()}
    print("Running period/band/cost/delay sensitivity",flush=True)
    grid = []
    for p in PERIODS:
        for band in BANDS:
            signal,_ = sma_gate(closes,p,band)
            sim = simulate(rows,start,positions(signal,start))
            grid.append({"period":p,"band":band,"pre2026":subset(dates,sim,end="20251231"),
                         "observed_2026":subset(dates,sim,start="20260101")})
    sensitivity = {}
    for cost,lag,yield_ in [(0,1,0),(25,1,0),(10,2,0),(10,1,.03)]:
        label=f"cost{cost}_lag{lag}_cash{yield_}"
        sensitivity[label] = {str(p): periods_report(dates,simulate(rows,start,positions(gate,start,lag),cost,yield_))
                              for p,gate in signal_map.items()}
    wf = {}
    wf_daily = {}
    for objective in ("sharpe_zero_rf","calmar","cagr_pct"):
        report, sim, ds = walk_forward(rows,start,signal_map,sims,objective)
        wf[objective] = report
        wf_daily[objective] = {"dates":ds,"returns":sim["returns"].tolist(),"position":sim["position"].tolist()}
    wf_start = next(i for i,r in enumerate(rows) if r["date"]>=wf_daily["sharpe_zero_rf"]["dates"][0])
    wf_dates = [r["date"] for r in rows[wf_start:]]
    wf_comparison = {str(p):periods_report(wf_dates,simulate(rows,wf_start,positions(gate,wf_start)))
                     for p,gate in signal_map.items()}
    wf_comparison["buy_hold"] = periods_report(wf_dates,simulate(rows,wf_start,np.ones(len(wf_dates))))
    rolling = {str(p):rolling_windows(dates,sim) for p,sim in sims.items()}
    # Constant exposure reference is descriptive, selected from the measured
    # period's own exposure, and is not an implementable ex-ante selection rule.
    matched = {}
    for p,sim in sims.items():
        pre = np.array([d<"20260101" for d in dates])
        exposure = float(np.mean(sim["position"][pre]))
        ref = bh["returns"]*exposure
        matched[str(p)] = {"fraction":exposure,"pre2026":metrics(list(np.array(dates)[pre]),ref[pre]),
                           "definition":"ex-post mean exposure times index daily returns; cash0, illustrative only"}
    print("Running paired block bootstrap and truncated-history checks",flush=True)
    bootstrap = paired_bootstrap({p:s["returns"] for p,s in sims.items()},dates,repetitions)
    truncated = rolling_history_check(closes,signal_map,start)
    for obj in truncated.values():
        obj["first_different_dates"]=[rows[i]["date"] for i in obj.pop("indices")[:10]]
    source_data = json.dumps(rows,sort_keys=True,separators=(",",":"))
    report = {
        "design": {"primary_periods":PRIMARY,"primary_band_pct":1,"one_way_cost_bps":10,
                   "cash_yield":0,"cash_yield_sensitivity":.03,"bear_release":False,
                   "execution":"prior close signal; next open switch; old holding bears opening gap; close valuation",
                   "warmup":"all 2010 history, common evaluation starts 2011; no trades in warmup",
                   "primary_selection":"five previous calendar years net Sharpe, annual selection; smaller period ties",
                   "secondary_selectors":["calmar","cagr_pct"],
                   "2026_status":"previously observed confirmation, not pristine holdout",
                   "not_modelled":["dividends","ETF tracking fees","actual cash rates","stock selection/ATR exits/pyramids"],
                   "limits":"index cash switching is distinct from production entry gate; same historical market reused across parameters"},
        "provenance":{"db":str(db.resolve()),"first":rows[0]["date"],"last":rows[-1]["date"],
                      "bars":len(rows),"data_sha256":hashlib.sha256(source_data.encode()).hexdigest(),
                      "script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      "evaluation_start":dates[0],"evaluation_bars":len(dates),
                      "year_counts":dict(Counter(r["date"][:4] for r in rows))},
        "validation":{"production_parity":parity,"initial_state_differences":init_diff},
        "benchmark":{"buy_hold":periods_report(dates,bh),"cash":periods_report(dates,cash)},
        "fixed":fixed,"sensitivity_grid":grid,"execution_sensitivity":sensitivity,
        "walk_forward":wf,"walk_forward_same_start_fixed_comparisons":wf_comparison,
        "rolling_3year_quarterly_starts":rolling,
        "matched_exposure_reference":matched,"paired_bootstrap":bootstrap,
        "blocked_episodes":{str(p):gate_episodes(rows,s,start) for p,s in signal_map.items()},
        "disjoint_drawdown_events":drawdown_events(rows,start,signal_map),
        "last_260_bars_vs_full_history":truncated,
    }
    (output/"index_filter.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    daily={"dates":dates,"closes":closes[start:].tolist(),
           "fixed":{str(p):{k:s[k].tolist() for k in ("returns","position","turnover")} for p,s in sims.items()},
           "buy_hold":{k:bh[k].tolist() for k in ("returns","position","turnover")},"walk_forward":wf_daily}
    (output/"index_daily_paths.json").write_text(json.dumps(daily,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    (output/"kospi_input.json").write_text(json.dumps(rows,ensure_ascii=False),encoding="utf-8")
    print(json.dumps({"data":report["provenance"],"pre2026":{p:r["pre2026"] for p,r in fixed.items()},
                      "wf":{k:v["metrics"]["pre2026"] for k,v in wf.items()}},ensure_ascii=False,indent=2))
    return report


if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db",type=Path,default=ROOT/"data/krx_openapi.db")
    ap.add_argument("--output",type=Path,default=OUT)
    ap.add_argument("--bootstrap",type=int,default=2000)
    args=ap.parse_args()
    run(args.db,args.output,args.bootstrap)
