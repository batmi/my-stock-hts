"""Pi 가상투자 가설 감사: 운영 코드·설정·원장을 수정하지 않는 통합 실행기.

체결/관측가격은 Pi 사본만, 보조 일봉은 명시한 KRX 저장소만 읽는다.
추가 청산은 실제 진입 경로를 고정한 관찰 재생이며 포트폴리오 백테스트가
아니다. 청산 이후의 새 진입·현금 재사용은 모형화하지 않는다.

python3 -B tools/audit_pi_hypotheses.py \
    --output data/audit_runs/20261025_pi_hypotheses.json
"""
from __future__ import annotations

import argparse
import bisect
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

try:
    from . import audit_pi_trade_evidence as evidence
    from . import audit_pi_exit_hypotheses as exits
    from . import audit_pi_market_hypotheses as market
except ImportError:
    import audit_pi_trade_evidence as evidence
    import audit_pi_exit_hypotheses as exits
    import audit_pi_market_hypotheses as market


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = ROOT / "data/krx_openapi.db"


def prepare_price_history(rows):
    """EMA/ATR are causal; the querying rule must still select a prior date."""
    result, previous = [], None
    for source in rows:
        row = dict(source)
        if not all(row.get(k) is not None and math.isfinite(float(row[k])) and row[k] > 0
                   for k in ("high", "low", "close")):
            raise ValueError(f"Invalid OHLC: {row.get('date')}")
        if previous and row["date"] <= previous["date"]:
            raise ValueError("Price dates must be unique and ascending")
        price = row["close"]
        for length in (20, 60):
            alpha = 2 / (length + 1)
            row[f"ema{length}"] = (price if previous is None else
                                      alpha * price + (1 - alpha) * previous[f"ema{length}"])
        tr = row["high"] - row["low"]
        if previous:
            tr = max(tr, abs(row["high"] - previous["close"]),
                     abs(row["low"] - previous["close"]))
        row["atr14"] = tr if previous is None else (previous["atr14"] * 13 + tr) / 14
        result.append(row)
        row["low10"] = min(r["low"] for r in result[-10:]) if len(result) >= 10 else None
        previous = row
    return result


def load_prices(dataset, store):
    start = min(e["start"][:10].replace("-", "") for e in dataset["episodes"])
    end = max(row["date"].replace("-", "") for row in dataset["equity"])
    # More than a year of warmup before any inspected order; no future indicator seed.
    warmup = str(int(start[:4]) - 2) + start[4:]
    codes = sorted({e["code"] for e in dataset["episodes"]})
    placeholders = ",".join("?" for _ in codes)
    con = sqlite3.connect(Path(store).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    result = {code: [] for code in codes}
    try:
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            "SELECT bas_dd AS date,code,open,high,low,close FROM stock_daily "
            f"WHERE bas_dd>=? AND bas_dd<=? AND code IN ({placeholders}) ORDER BY bas_dd",
            (warmup, end, *codes),
        )
        for row in rows:
            valid = all(row[k] is not None and math.isfinite(float(row[k])) and row[k] > 0
                        for k in ("high", "low", "close"))
            if not valid:
                if row["date"] < start:
                    # Suspended/non-traded records may contain zeros. Only skip in
                    # warmup; never silently remove a bar in the evaluated period.
                    continue
                raise ValueError(f"Invalid OHLC during audit interval: {row['code']} {row['date']}")
            result[row["code"]].append(dict(row))
    finally:
        con.close()
    for code, rows in result.items():
        if len(rows) < 150:
            raise ValueError(f"Insufficient historical warmup for {code}")
        result[code] = prepare_price_history(rows)
    return result


class ConfirmedPrices:
    def __init__(self, histories):
        self.histories = histories
        self.days = {code: [row["date"] for row in rows] for code, rows in histories.items()}
        self.cache = {}

    def previous(self, code, timestamp):
        day = timestamp[:10].replace("-", "")
        key = code, day
        if key not in self.cache:
            at = bisect.bisect_left(self.days.get(code, []), day) - 1
            self.cache[key] = self.histories[code][at] if at >= 120 else None
        return self.cache[key]


STRUCTURE_ARMS = {
    "ema20_after_5": "After observed +5%, current quote below previous completed day's EMA20",
    "ema20_confirmed_after_5": "After observed +5%, previous completed daily close below its EMA20",
    "low10_after_5": "After observed +5%, current quote below lowest low of 10 completed trading days",
}


def structural_trigger(arm, prices):
    if arm not in STRUCTURE_ARMS:
        raise ValueError(f"Unknown structure arm: {arm}")

    def trigger(state, tick):
        # Only the historical running maximum is available here, never episode MFE.
        if state["max_observed_return"] < 5:
            return False
        state.setdefault("structure_armed_date", tick["time"][:10].replace("-", ""))
        prior = prices.previous(state["code"], tick["time"])
        if prior is None:
            return False
        if arm == "ema20_after_5":
            return tick["price"] < prior["ema20"]
        if arm == "ema20_confirmed_after_5":
            return (prior["date"] >= state["structure_armed_date"]
                    and prior["close"] < prior["ema20"])
        return prior["low10"] is not None and tick["price"] < prior["low10"]
    return trigger


def snapshot_validation(dataset, prices):
    records = []
    for episode in dataset["episodes"]:
        for fill in episode["buys"] + episode["sells"]:
            snap = fill.get("snapshot") or {}
            if not isinstance(snap, dict):
                continue
            observed = (snap.get("indicators") or {}).get("ema_20")
            price = snap.get("price")
            prior = prices.previous(episode["code"], fill["time"])
            if not observed or not price or prior is None:
                continue
            calculated = prior["ema20"] * 19 / 21 + price * 2 / 21
            records.append({"code": episode["code"], "time": fill["time"],
                            "snapshot_ema20": observed, "reconstructed_ema20": calculated,
                            "absolute_relative_error_pct": abs(calculated / observed - 1) * 100})
    errors = [r["absolute_relative_error_pct"] for r in records]
    if not errors or max(errors) > 0.1:
        raise ValueError("Supplemental daily prices do not reproduce Pi EMA20 snapshots within 0.1%")
    return {"count": len(errors), "median_absolute_relative_error_pct": statistics.median(errors),
            "max_absolute_relative_error_pct": max(errors), "records": records,
            "method": "EMA20 through prior completed close, then one update using snapshot price"}


def structure_report(dataset, histories):
    prices = ConfirmedPrices(histories)
    validation = snapshot_validation(dataset, prices)
    simulations = {}
    for arm in ("actual", *STRUCTURE_ARMS):
        rows = [exits.simulate_episode(e, None if arm == "actual" else structural_trigger(arm, prices))
                for e in dataset["episodes"]]
        simulations[arm] = {"description": STRUCTURE_ARMS.get(arm, "Actual schedule control"),
                            "summary": exits._groups(rows), "episodes": rows}
    stress = {}
    for bps in (20.0, 40.0):
        stress[f"{int(bps)}bps"] = {}
        for arm in ("actual", *STRUCTURE_ARMS):
            rows = [exits.simulate_episode(e, None if arm == "actual" else structural_trigger(arm, prices),
                                          overlay_execution_bps=bps)
                    for e in dataset["episodes"]]
            stress[f"{int(bps)}bps"][arm] = exits._groups(rows)
    return {"snapshot_validation": validation, "arms": simulations,
            "overlay_execution_stress": stress,
            "limitations": [
                "Signals use observed running +5% maximum, not future episode maximum.",
                "Daily indicators and low10 use strictly earlier completed sessions.",
                "Confirmed daily breakdown must be on or after the first observed +5% day.",
                "Execution is next observed regular-session quote, not trigger price.",
                "Actual future entry schedule is fixed; early exits skip later adds only within that position.",
                "These are exit overlays; actual earlier exits remain in force. Not replacements of the entire strategy.",
                "No reused cash, new entries, market impact or after-hours alternative exits are modeled.",
            ]}


def execution_diagnostics(dataset, exit_report, structures):
    """Distinguish stop design from gaps/observed execution below that stop."""
    stop_rows = []
    for episode in dataset["episodes"]:
        if not episode.get("exit_is_auto") or "ATR손절" not in (episode.get("exit_reason") or ""):
            continue
        qty = sum(fill["qty"] for fill in episode["buys"])
        basis = sum(fill["amount"] for fill in episode["buys"])
        sl = sum(fill["stop_loss_rate"] * fill["qty"] for fill in episode["buys"]) / qty
        stop = basis / qty * (1 + sl / 100)
        sell = episode["sells"][-1]
        decision_price = (sell.get("snapshot") or {}).get("price")
        stop_rows.append({"id": episode["id"], "time": sell["time"], "name": episode["name"],
                          "quantity_weighted_stop_rate": sl, "reconstructed_stop_price": stop,
                          "actual_fill_price": sell["price"],
                          "price_pnl_below_stop_krw": (stop - sell["price"]) * qty,
                          "decision_snapshot_price": decision_price,
                          "fill_vs_decision_pct": (sell["price"] / decision_price - 1) * 100
                          if decision_price else None})
    waits = []
    for report in (exit_report, structures):
        for name, arm in report["arms"].items():
            for result in arm["episodes"]:
                if result["early_exit"]:
                    wait = (datetime.fromisoformat(result["exit_time"])
                            - datetime.fromisoformat(result["signal_time"])).total_seconds()
                    waits.append({"arm": name, "id": result["id"], "seconds": wait,
                                  "crosses_date": result["exit_time"][:10] != result["signal_time"][:10]})
    return {"atr_stop_exits": stop_rows,
            "price_pnl_below_reconstructed_stops_krw": sum(r["price_pnl_below_stop_krw"] for r in stop_rows),
            "counterfactual_signal_execution_delays": waits,
            "max_counterfactual_delay_seconds": max((r["seconds"] for r in waits), default=0),
            "counterfactual_cross_date_fills": sum(r["crosses_date"] for r in waits),
            "limitations": ["Below-stop price loss combines gaps, polling interval, and adverse fills; not a CPU attribution.",
                            "Reconstructed stops use actual entry/add stop rates and quantities."]}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(db_path=evidence.DEFAULT_DB, logs_path=evidence.DEFAULT_LOGS, store=DEFAULT_STORE):
    dataset = evidence.build_dataset(db_path, logs_path)
    histories = load_prices(dataset, store)
    daily_atr = {code: [{"date": r["date"], "atr": r["atr14"]} for r in rows]
                 for code, rows in histories.items()}
    sources = [Path(db_path), Path(str(db_path) + "-wal")]
    fingerprints = {str(path): sha256(path) for path in sources if path.is_file()}
    price_digest = hashlib.sha256(json.dumps(histories, sort_keys=True).encode()).hexdigest()
    source_scripts = ("audit_pi_hypotheses.py", "audit_pi_trade_evidence.py",
                      "audit_pi_exit_hypotheses.py", "audit_pi_market_hypotheses.py")
    exit_report = exits.analyze(dataset, daily_atr)
    structures = structure_report(dataset, histories)
    return {
        "audit": "Pi observational hypothesis audit; production code unchanged",
        "version": 1,
        "provenance": {"pi_source_fingerprints": fingerprints,
                       "audit_source_sha256": {name: sha256(ROOT / "tools" / name) for name in source_scripts},
                       "pi_observation_count": dataset["meta"]["tick_count"],
                       "cash_reconstruction": dataset["meta"]["cash_reconstruction"],
                       "fee_validation": dataset["meta"]["fee_validation"],
                       "supplemental_price_store": str(Path(store).resolve()),
                       "queried_stock_prices_sha256": price_digest,
                       "historical_prices": {code: {"start": rows[0]["date"], "end": rows[-1]["date"],
                                                    "bars": len(rows)} for code, rows in histories.items()},
                       "zero_ohlc_policy": "skip only pre-entry warmup records; reject invalid audit-period bars",
                       "runtime_imports_application_config": False,
                       "network_requests": False,
                       "operating_db_connections": "read-only SQLite"},
        "evidence": evidence.summarize(dataset),
        "exit_hypotheses": exit_report,
        "structure_hypotheses": structures,
        "execution_diagnostics": execution_diagnostics(dataset, exit_report, structures),
        "market_hypotheses": market.build_report(dataset, store),
    }


def print_summary(report):
    evidence_summary = report["evidence"]
    print("Pi hypothesis audit — fixed entry schedule, next observed quote executions")
    print("Evidence:", json.dumps({k: v for k, v in evidence_summary.items() if k != "episodes"},
                                  ensure_ascii=False, indent=2))
    print("Exit overlays (18 automatically closed episodes; amounts in KRW):")
    for arm, row in report["exit_hypotheses"]["arms"].items():
        print(arm, json.dumps(row["summary"]["closed_auto"], ensure_ascii=False))
    print("Structure overlays:")
    for arm, row in report["structure_hypotheses"]["arms"].items():
        print(arm, json.dumps(row["summary"]["closed_auto"], ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=evidence.DEFAULT_DB)
    parser.add_argument("--logs", type=Path, default=evidence.DEFAULT_LOGS)
    parser.add_argument("--daily-db", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.suffix.lower() != ".json":
        parser.error("--output must be a .json audit artifact")
    result = run(args.db, args.logs, args.daily_db)
    print_summary(result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Audit artifact: {args.output}")


if __name__ == "__main__":
    main()
