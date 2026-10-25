"""Pi의 시장필터 ON 무거래 경로를 검증한다(읽기 전용, 운영 모듈 import 없음).

관측된 후보 전체가 모든 거래일 장중에도 차단되고, 초기 보유가 없으며
외부/수동 매수가 없을 때만 현금 유지 결과를 산출한다. 일부라도 게이트가
열리면 기존 거래를 빼서 가짜 포트폴리오 수익률을 만들지 않고 미확정으로 남긴다.

python3 -B tools/audit_pi_filter_on.py --output data/audit_runs/20261025_pi_filter_on.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3

try:
    from .audit_pi_trade_evidence import DEFAULT_DB, DEFAULT_LOGS, build_dataset
    from .audit_pi_market_hypotheses import MARKETS
except ImportError:
    from audit_pi_trade_evidence import DEFAULT_DB, DEFAULT_LOGS, build_dataset
    from audit_pi_market_hypotheses import MARKETS


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = ROOT / "data/krx_openapi.db"
CANDIDATE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})[^]]*\] \[분석\] (.+?)\(([A-Z0-9]{6})\):")


def intraday_bounds(rows, start, end, period, band_pct):
    """Evaluate the daily HIGH against SMA including that high (maximum optimism).

    Price - lower_band*SMA(price) is increasing for period>=2. If even the
    maximum possible price is below the block boundary, every intraday price
    is blocked regardless of the prior hysteresis state. Today's low/close is
    not used to decide a historical order; daily high is an ex-post proof bound.
    """
    if period < 2 or not 0 <= band_pct < 100:
        raise ValueError("Expected period>=2 and band in [0,100)")
    output = []
    for i, row in enumerate(rows):
        if not start <= row["date"] <= end:
            continue
        if i < period - 1:
            raise ValueError("Insufficient preceding index history")
        previous_sum = sum(r["close"] for r in rows[i-period+1:i])
        low_factor, high_factor = 1-band_pct/100, 1+band_pct/100
        if period <= high_factor:
            raise ValueError("Invalid release boundary")
        block_price = low_factor * previous_sum / (period-low_factor)
        release_price = high_factor * previous_sum / (period-high_factor)
        high = row["high"]
        output.append({"date": row["date"], "high": high,
                       "block_boundary_price": block_price,
                       "release_boundary_price": release_price,
                       "high_vs_block_pct": (high/block_price-1)*100,
                       "high_vs_release_pct": (high/release_price-1)*100,
                       "blocked_for_entire_session": high < block_price})
    return output


def candidates(logs, start, end):
    counts, by_day, first_sources = Counter(), defaultdict(set), {}
    for path in sorted(Path(logs).glob("autotrade*.log")):
        with path.open(encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                match = CANDIDATE.match(line)
                if not match:
                    continue
                day, name, code = match.groups()
                day = day.replace("-", "")
                if not start <= day <= end:
                    continue
                counts[code] += 1
                by_day[day].add(code)
                first_sources.setdefault(code, {"name": name, "file": str(path), "line": number})
    if not counts:
        raise ValueError("No candidate universe evidence in selected Pi logs")
    return {"counts": dict(counts), "by_day": {day: sorted(v) for day, v in by_day.items()},
            "first_sources": first_sources, "analysis_lines": sum(counts.values())}


def cash_path(seed, trading_days):
    return {"initial_equity": seed, "final_equity": seed, "profit": 0.0,
            "return_pct": 0.0, "mdd_pct": 0.0, "buys": 0, "pyramid_buys": 0,
            "sells": 0, "fees": 0.0, "open_positions": 0,
            "daily": [{"date": day, "cash": seed, "stock_value": 0.0, "equity": seed}
                      for day in trading_days]}


def run(db=DEFAULT_DB, logs=DEFAULT_LOGS, store=DEFAULT_STORE, period=80, band_pct=1.0):
    dataset = build_dataset(db, logs)
    start = min(e["start"][:10].replace("-", "") for e in dataset["episodes"])
    end = max(r["date"].replace("-", "") for r in dataset["equity"])
    universe = candidates(logs, start, end)
    codes = sorted(universe["counts"])
    for e in dataset["episodes"]:
        if e["code"] not in codes:
            raise ValueError(f"Filled symbol missing from candidate universe: {e['code']}")
    con = sqlite3.connect(Path(store).resolve().as_uri()+"?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    membership, index_rows = {}, {}
    try:
        con.execute("PRAGMA query_only=ON")
        for code in codes:
            markets = {r[0] for r in con.execute(
                "SELECT DISTINCT market FROM stock_daily WHERE bas_dd>=? AND bas_dd<=? AND code=?",
                (start, end, code))}
            markets = {"KOSDAQ" if m == "KOSDAQ GLOBAL" else m for m in markets}
            if len(markets) != 1 or not markets.issubset(MARKETS):
                raise ValueError(f"Missing or ambiguous same-period market for {code}: {markets}")
            membership[code] = next(iter(markets))
        for name in sorted(set(membership.values())):
            api_id, index_name = MARKETS[name]
            index_rows[name] = [dict(r) for r in con.execute(
                "SELECT bas_dd AS date,high,close FROM index_daily WHERE api_id=? AND name=? "
                "AND bas_dd<=? ORDER BY bas_dd", (api_id, index_name, end))]
    finally:
        con.close()
    bounds = {name: intraday_bounds(rows, start, end, period, band_pct)
              for name, rows in index_rows.items()}
    days = sorted({r["date"] for rows in bounds.values() for r in rows})
    if set(days) != set(universe["by_day"]):
        raise ValueError("Candidate analysis evidence does not cover all index trading sessions")
    automatic_buys = all(fill["order_type"] == "buy(AUTO)"
                         for e in dataset["episodes"] for fill in e["buys"])
    # build_dataset reconciles the whole ledger from seed to ending cash and
    # rejects any sale without an earlier recorded purchase, including opening holdings.
    whole_period_blocked = all(rows and all(r["blocked_for_entire_session"] for r in rows)
                               for rows in bounds.values())
    proven = whole_period_blocked and automatic_buys
    seed = dataset["meta"]["cash_reconstruction"]["seed"]
    actual = dataset["equity"][-1]
    return {
        "scenario": {"start": start, "end": end, "market_filter_on": True,
                     "ma_period": period, "band_pct": band_pct, "bear_release": False,
                     "settings_are_assumptions_not_recovered_pi_profile": True,
                     "initial_cash": seed, "initial_holdings": 0, "cash_interest": 0,
                     "candidate_universe": "same candidates observed in Pi analysis logs"},
        "evidence": {"analysis_lines": universe["analysis_lines"], "candidate_count": len(codes),
                     "analysis_days": len(universe["by_day"]), "market_membership": membership,
                     "first_sources": universe["first_sources"],
                     "all_recorded_buys_automatic": automatic_buys,
                     "seed_state": dataset["meta"]["state"],
                     "cash_reconciliation": dataset["meta"]["cash_reconstruction"]},
        "intraday_blocking_bounds": bounds,
        "entire_period_blocking_proven": proven,
        "filter_on": cash_path(seed, days) if proven else None,
        "actual_filter_off": {"final_equity": actual["total"], "cash": actual["cash"],
                              "stock_value": actual["stock_value"],
                              "return_pct": (actual["total"]/seed-1)*100,
                              "buys": sum(len(e["buys"]) for e in dataset["episodes"]),
                              "sells": sum(len(e["sells"]) for e in dataset["episodes"])},
        "equity_difference_vs_actual": seed-actual["total"] if proven else None,
        "interpretation": (
            "All candidate markets are blocked throughout every session, so flat initial state stays all-cash. "
            "This proves a no-trade path without freezing the original trades or ignoring alternative candidates."
            if proven else "Gate opens somewhere or a manual buy exists; a full alternative portfolio replay is required. "
            "No return estimate is inferred by subtracting filtered actual trades."),
        "limits": ["Assumes accurate official index prices and stated filter settings, not a full live API replay.",
                   "Daily highs are used only to bound all possible intraday filter decisions, not as trade signals.",
                   "Assumes same observed candidate universe and no new discretionary/external buying.",
                   "This is a specified-period counterfactual, not evidence of long-term profitability."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOGS)
    parser.add_argument("--daily-db", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--ma", type=int, default=80)
    parser.add_argument("--band", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.db, args.logs, args.daily_db, args.ma, args.band)
    if args.output:
        if args.output.suffix.lower() != ".json":
            parser.error("--output must be a .json audit artifact")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    compact = {k: report[k] for k in ("scenario", "entire_period_blocking_proven", "actual_filter_off",
                                     "equity_difference_vs_actual", "interpretation")}
    compact["filter_on"] = {k: v for k, v in (report["filter_on"] or {}).items() if k != "daily"}
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
