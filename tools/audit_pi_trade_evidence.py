"""Pi 가상투자 원장과 장중 로그를 읽기만 하여 포지션별 관측 근거를 만든다.

운영 모듈/config를 import하지 않으며 SQLite는 mode=ro로 연다. 접수/체결이
중복 기록되는 trades 대신 paper_fills를 수량·현금의 기준으로 삼는다.
최고/최저 수익은 실제 매수 뒤 관측한 잔고표 가격이다. 일봉 고가, 보간한
가격, 관측하지 않은 체결을 섞지 않는다. 따라서 진짜 장중 극값의 하한 표본이다.

python tools/audit_pi_trade_evidence.py --output tmp/pi_hypotheses/episodes.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3
import statistics


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db/RasPi3B/paper_trading.db"
DEFAULT_LOGS = ROOT / "logs/RasPi3B"
# Rates are checked against EVERY actual fill below, not taken from Mac settings.
BUY_FEE_RATE = 0.000177
SELL_FEE_RATE = 0.002177
NUMBER = r"([+\-]?[\d,]+(?:\.\d+)?)"
TABLE_RE = re.compile(
    r"^\[([^]]+)\]\s+(.+?)\s+\(([A-Z0-9]{6})\)\s+"
    + NUMBER + r"주\s+" + NUMBER + r"원\s+" + NUMBER + r"원\s+"
    + NUMBER + r"원\s+" + NUMBER + r"원\s+" + NUMBER + r"원\s+" + NUMBER + r"%"
)
ANALYSIS_RE = re.compile(
    r"^\[([^]]+)\] \[보유분석\] (.*?)\(([A-Z0-9]{6})\): "
    r"수익률=([+-]?[\d.]+)%, 점수=([\d.]+), 상태=([^,]+), 판단=([^,]+)"
)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _json_snapshot(value):
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def _source_path(path):
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def build_dataset(db_path=DEFAULT_DB, logs_path=DEFAULT_LOGS):
    """Return {meta,equity,episodes}; no output files or application side effects.

    Timestamps are local Asia/Seoul strings: fills have second resolution, observed
    quotes millisecond resolution. Ticks sharing a second with ANY fill for that
    episode are excluded, because the fill's subsecond ordering is unknown.
    """
    db_path, logs_path = Path(db_path), Path(logs_path)
    _require(db_path.is_file(), f"Missing DB: {db_path}")
    _require(logs_path.is_dir(), f"Missing logs directory: {logs_path}")
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    episodes, fills, active = [], [], {}
    try:
        for row in connection.execute("SELECT * FROM paper_fills ORDER BY time,id"):
            raw = dict(row)
            fill = {key: raw[key] for key in (
                "id", "time", "type", "code", "name", "qty", "price", "amount",
                "fee", "profit_amt", "profit_rate",
            )}
            _require(fill["qty"] > 0 and fill["price"] > 0, "Invalid fill quantity/price")
            _require(abs(fill["amount"] - fill["qty"] * fill["price"]) < .01,
                     f"Fill amount mismatch: {fill['id']}")
            order = connection.execute(
                "SELECT id,type,reason,stop_loss_rate,snapshot FROM trades "
                "WHERE odno=? AND order_status='접수' ORDER BY id LIMIT 1", (raw["odno"],)
            ).fetchone()
            fill.update({
                "order_type": order["type"] if order else None,
                "reason": order["reason"] if order else None,
                "stop_loss_rate": order["stop_loss_rate"] if order else None,
                "snapshot": _json_snapshot(order["snapshot"]) if order else None,
                "source": {"db": _source_path(db_path), "table": "paper_fills",
                           "id": raw["id"], "trades_order_id": order["id"] if order else None},
            })
            fills.append(fill)
            code = fill["code"]
            if fill["type"] == "매수":
                if code not in active:
                    episode = {
                        "id": f"{code}_{fill['time'].replace(' ', 'T')}",
                        "code": code, "name": fill["name"], "start": fill["time"],
                        "end": None, "qty": 0, "buys": [], "sells": [], "ticks": [],
                        "observations": [], "source_events": [],
                        "initial_stop_rate": fill["stop_loss_rate"],
                        "entry_is_auto": fill["order_type"] == "buy(AUTO)",
                    }
                    active[code] = episode
                    episodes.append(episode)
                episode = active[code]
                episode["qty"] += fill["qty"]
                episode["buys"].append(fill)
            else:
                _require(fill["type"] == "매도", f"Unsupported fill type: {fill['type']}")
                _require(code in active, f"Sell without an opening buy: fill {fill['id']}")
                episode = active[code]
                _require(not episode["sells"], "Partial exits need separate position-basis logic")
                _require(fill["qty"] == episode["qty"], "Partial exits need separate position-basis logic")
                episode["qty"] -= fill["qty"]
                episode["sells"].append(fill)
                episode["end"] = fill["time"]
                active.pop(code)

        equity = [dict(row) for row in connection.execute(
            "SELECT date,cash,stock_value,total,seed FROM paper_equity ORDER BY date"
        )]
        state = dict(connection.execute(
            "SELECT key,value FROM paper_state WHERE key IN ('seed','cash','started_at')"
        ))
        positions = [dict(row) for row in connection.execute(
            "SELECT code,name,qty,avg_price,first_buy_at,last_buy_at FROM paper_positions"
        )]
    finally:
        connection.close()

    _require(bool(fills), "No fills in selected DB")
    expected_positions = {code: episode["qty"] for code, episode in active.items()}
    _require(expected_positions == {row["code"]: row["qty"] for row in positions},
             "Reconstructed quantities differ from paper_positions")
    for row in positions:
        episode = active[row["code"]]
        cost = sum(fill["amount"] for fill in episode["buys"])
        _require(abs(cost / row["qty"] - row["avg_price"]) < .01,
                 "Reconstructed average price differs from paper_positions")

    by_code = defaultdict(list)
    fill_seconds = {}
    for episode in episodes:
        by_code[episode["code"]].append(episode)
        fill_seconds[episode["id"]] = {
            fill["time"] for fill in episode["buys"] + episode["sells"]
        }
    excluded_same_second = 0
    first_date = fills[0]["time"][:10].replace("-", "")
    files = sorted(logs_path.glob("autotrade*.log"))
    source_logs = []
    for path in files:
        date_match = re.search(r"autotrade_(\d{8})", path.name)
        if date_match and date_match.group(1) < first_date:
            continue
        source_name = _source_path(path)
        source_logs.append(source_name)
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                source = {"file": source_name, "line": line_number}
                match = TABLE_RE.match(line)
                if match:
                    timestamp, _name, code, *values = match.groups()
                    values = [float(value.replace(",", "")) for value in values]
                    second = timestamp[:19]
                    for episode in by_code[code]:
                        if not (episode["start"] <= second and
                                (episode["end"] is None or second <= episode["end"])):
                            continue
                        if second in fill_seconds[episode["id"]]:
                            excluded_same_second += 1
                            continue
                        buys = [fill for fill in episode["buys"] if fill["time"] < second]
                        qty = sum(fill["qty"] for fill in buys)
                        cost = sum(fill["amount"] for fill in buys)
                        _require(qty > 0, "Quote before initial fill")
                        _require(qty == int(values[0]), f"Quantity mismatch at {source}")
                        _require(abs(cost / qty - values[1]) <= 1, f"Average price mismatch at {source}")
                        episode["ticks"].append({
                            "time": timestamp, "price": values[2],
                            "logged_qty": int(values[0]), "logged_avg": values[1],
                            "logged_cost": values[3], "logged_value": values[4],
                            "logged_profit": values[5], "logged_return": values[6],
                            "qty": qty, "cost": cost, "avg": cost / qty,
                            "buy_fees_to_date": sum(fill["fee"] for fill in buys),
                            "gross_return": 100 * (values[2] / (cost / qty) - 1),
                            "gross_profit": values[2] * qty - cost,
                            "regular_session": "09:00:00" <= timestamp[11:19] <= "15:30:00",
                            "source": source,
                        })
                    continue
                match = ANALYSIS_RE.match(line)
                if match:
                    timestamp, _name, code, rate, score, state_name, decision = match.groups()
                    for episode in by_code[code]:
                        if episode["start"] <= timestamp[:19] and (
                                episode["end"] is None or timestamp[:19] <= episode["end"]):
                            episode["observations"].append({
                                "time": timestamp, "logged_return": float(rate),
                                "score": float(score), "state": state_name,
                                "decision": decision, "source": source,
                            })
                if "매도 실행:" in line or ("피라미딩" in line and ("매수" in line or "수익률" in line)):
                    timestamp = line[1:24]
                    for episode in episodes:
                        if episode["name"] in line and episode["start"] <= timestamp[:19] and (
                                episode["end"] is None or timestamp[:19] <= episode["end"]):
                            episode["source_events"].append({
                                "time": timestamp, "text": line.strip(), "source": source,
                            })
    for episode in episodes:
        episode["ticks"].sort(key=lambda tick: tick["time"])
        episode["observations"].sort(key=lambda tick: tick["time"])
        episode["buy_count"] = len(episode["buys"])
        episode["actual_net_profit"] = sum(fill["profit_amt"] for fill in episode["sells"])
        episode["cash_flow_net_profit"] = (
            sum(fill["amount"] - fill["fee"] for fill in episode["sells"])
            - sum(fill["amount"] + fill["fee"] for fill in episode["buys"])
        ) if episode["end"] else None
        episode["exit_reason"] = episode["sells"][-1]["reason"] if episode["sells"] else None
        episode["exit_is_auto"] = episode["sells"][-1]["order_type"] == "sell(AUTO)" if episode["sells"] else None

    fees = []
    for side, rate in (("매수", BUY_FEE_RATE), ("매도", SELL_FEE_RATE)):
        relevant = [fill for fill in fills if fill["type"] == side]
        mismatches = [{"fill_id": fill["id"], "amount": fill["amount"],
                       "fee": fill["fee"], "expected": int(fill["amount"] * rate)}
                      for fill in relevant if fill["fee"] != int(fill["amount"] * rate)]
        _require(not mismatches, f"Fee schedule differs from actual fills: {mismatches}")
        fees.append({"side": side, "rate": rate, "matched": len(relevant), "mismatches": []})
    cash_flow = sum(fill["amount"] - fill["fee"] if fill["type"] == "매도"
                    else -(fill["amount"] + fill["fee"]) for fill in fills)
    seed, cash = float(state["seed"]), float(state["cash"])
    residual = cash - seed - cash_flow
    _require(abs(residual) < .01, f"Cash does not reconcile; deposits/reset or missing fills: {residual}")
    return {
        "meta": {
            "source_db": _source_path(db_path), "source_logs": _source_path(logs_path),
            "time_zone": "Asia/Seoul", "currency": "KRW",
            "precision": "fills second; log timestamps millisecond",
            "price_source": "exact holdings-table current price after fill; same-second fill ticks excluded",
            "regular_session_flag": "09:00:00 through 15:30:00; other quotes retained and flagged false",
            "atr_coverage": "order snapshots only; no time-varying ATR reconstructed",
            "fee_validation": fees, "state": state,
            "cash_reconstruction": {"seed": seed, "fill_net_cashflow": cash_flow,
                                    "expected_cash": seed + cash_flow, "actual_cash": cash,
                                    "residual": residual},
            "tick_count": sum(len(episode["ticks"]) for episode in episodes),
            "episode_count": len(episodes), "same_second_quotes_excluded": excluded_same_second,
            "files_read": source_logs,
        },
        "equity": equity, "episodes": episodes,
    }


def summarize(dataset):
    """Compact cash-consistent statistics and per-position observations."""
    episodes = dataset["episodes"]
    closed = [episode for episode in episodes if episode["end"]]
    auto = [episode for episode in closed if episode["exit_is_auto"]]
    rows = []
    monthly = defaultdict(lambda: {"exits": 0, "auto_exits": 0, "cash_net": 0.0})
    monthly_equity = {}
    for point in dataset["equity"]:
        monthly_equity[point["date"][:7]] = point
    for episode in episodes:
        ticks = episode["ticks"]
        peak = max(ticks, key=lambda tick: tick["gross_return"]) if ticks else None
        trough = min(ticks, key=lambda tick: tick["gross_return"]) if ticks else None
        currency_peak = max(ticks, key=lambda tick: tick["gross_profit"]) if ticks else None
        analysis_peak = max(episode["observations"], key=lambda tick: tick["logged_return"]) if episode["observations"] else None
        first = episode["buys"][0]
        initial_risk = first["amount"] * abs(episode["initial_stop_rate"] or 0) / 100
        row = {
            "id": episode["id"], "name": episode["name"], "start": episode["start"],
            "end": episode["end"], "buy_count": episode["buy_count"],
            "exit_is_auto": episode["exit_is_auto"], "exit_reason": episode["exit_reason"],
            "cash_net": episode["cash_flow_net_profit"], "reported_net": episode["actual_net_profit"],
            "initial_risk_krw": initial_risk,
            "mfe_pct": peak["gross_return"] if peak else None,
            "mae_pct": trough["gross_return"] if trough else None,
            "analysis_peak": analysis_peak,
            "peak": peak, "trough": trough, "currency_peak": currency_peak,
            "mfe_initial_r": peak["gross_return"] / abs(episode["initial_stop_rate"])
            if peak and episode["initial_stop_rate"] else None,
        }
        if episode["end"]:
            final = episode["sells"][-1]
            initial_only_net = (
                first["qty"] * final["price"] - int(first["qty"] * final["price"] * SELL_FEE_RATE)
                - first["amount"] - first["fee"]
            )
            row.update({
                "fixed_exit_initial_lot_net": initial_only_net,
                "fixed_exit_added_lots_net": episode["cash_flow_net_profit"] - initial_only_net,
                "final_net_initial_r": episode["cash_flow_net_profit"] / initial_risk if initial_risk else None,
                "exit_net_rate": final["profit_rate"],
                "exit_gross_rate": 100 * (final["amount"] / sum(fill["amount"] for fill in episode["buys"]) - 1),
            })
            row["mae_including_exit_fill_pct"] = min(
                row["mae_pct"] if row["mae_pct"] is not None else 0.0, row["exit_gross_rate"]
            )
            month = monthly[episode["end"][:7]]
            month["exits"] += 1
            month["auto_exits"] += bool(episode["exit_is_auto"])
            month["cash_net"] += episode["cash_flow_net_profit"]
        rows.append(row)
    auto_rows = [row for row in rows if row["exit_is_auto"]]
    above_five = [row for row in auto_rows if row["mfe_pct"] is not None and row["mfe_pct"] >= 5]
    last_equity = dataset["equity"][-1] if dataset["equity"] else None
    fill_events = sorted(
        [(fill, episode["id"]) for episode in episodes for fill in episode["buys"] + episode["sells"]],
        key=lambda item: (item[0]["time"], item[0]["id"]),
    )
    seed = dataset["meta"]["cash_reconstruction"]["seed"]
    position_costs = defaultdict(float)
    max_total_cost = {"time": None, "cost": 0.0, "pct_initial_seed": 0.0}
    max_single_cost = {"time": None, "cost": 0.0, "pct_initial_seed": 0.0}
    for fill, episode_id in fill_events:
        if fill["type"] == "매수":
            position_costs[episode_id] += fill["amount"]
            total = sum(position_costs.values())
            if total > max_total_cost["cost"]:
                max_total_cost = {"time": fill["time"], "cost": total,
                                  "pct_initial_seed": total / seed * 100,
                                  "position_costs": dict(position_costs)}
            if position_costs[episode_id] > max_single_cost["cost"]:
                max_single_cost = {"time": fill["time"], "id": episode_id,
                                   "cost": position_costs[episode_id],
                                   "pct_initial_seed": position_costs[episode_id] / seed * 100}
        else:
            position_costs.pop(episode_id)
    pyramid_rows = [row for row in rows if row["end"] and row["buy_count"] > 1]
    return {
        "closed_count": len(closed), "closed_auto_count": len(auto),
        "closed_manual_count": len(closed) - len(auto), "open_count": len(episodes) - len(closed),
        "closed_auto_wins": sum(episode["cash_flow_net_profit"] > 0 for episode in auto),
        "closed_auto_mfe_ge5_count": len(above_five),
        "closed_auto_mfe_ge5_losses": sum(row["cash_net"] < 0 for row in above_five),
        "realized_reported_net": sum(episode["actual_net_profit"] for episode in closed),
        "realized_cash_net": sum(episode["cash_flow_net_profit"] for episode in closed),
        "total_fees": sum(fill["fee"] for episode in episodes for fill in episode["buys"] + episode["sells"]),
        "latest_equity": last_equity, "monthly_realized": dict(monthly),
        "month_end_equity": monthly_equity,
        "max_total_book_cost": max_total_cost,
        "max_single_position_book_cost": max_single_cost,
        "pyramiding_attribution": {
            "episodes": len(pyramid_rows),
            "actual_cash_net": sum(row["cash_net"] for row in pyramid_rows),
            "initial_lot_fixed_exit_net": sum(row["fixed_exit_initial_lot_net"] for row in pyramid_rows),
            "added_lots_fixed_exit_net": sum(row["fixed_exit_added_lots_net"] for row in pyramid_rows),
            "all_closed_initial_lots_fixed_exit_net": sum(row.get("fixed_exit_initial_lot_net", 0) for row in rows),
            "same_day_multiple_adds": [
                {"id": episode["id"], "date": date, "count": count}
                for episode in episodes
                for date, count in Counter(fill["time"][:10] for fill in episode["buys"][1:]).items()
                if count > 1
            ],
        },
        "after_2026_10_16_cohort": {
            "entries": sum(episode["start"] >= "2026-10-16" for episode in episodes),
            "closed_entries": sum(episode["start"] >= "2026-10-16" and episode["end"] is not None for episode in episodes),
            "exits": sum(episode["end"] >= "2026-10-16" for episode in closed),
            "cash_net_exits": sum(episode["cash_flow_net_profit"] for episode in closed if episode["end"] >= "2026-10-16"),
        },
        "initial_stop_median_pct": statistics.median(abs(episode["initial_stop_rate"]) for episode in episodes),
        "episodes": rows,
        "limits": [
            "MFE/MAE are observed quote extremes, not continuous market extremes.",
            "mae_pct excludes the actual exit fill; mae_including_exit_fill_pct also includes that endpoint.",
            "Analysis-log peak can exceed exact-table peak around same-second pyramiding; both are retained.",
            "Gross percent uses contemporaneous position average; pyramiding changes that denominator.",
            "Initial-lot-only figures hold actual entry and exit fixed; no capital reallocation or new trades.",
            "No time-varying ATR is reconstructed from order-only snapshots.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOGS)
    parser.add_argument("--output", type=Path, help="Optional explicit destination for full evidence JSON")
    parser.add_argument("--summary-output", type=Path, help="Optional explicit destination for summary JSON")
    args = parser.parse_args()
    dataset = build_dataset(args.db, args.logs)
    summary = summarize(dataset)
    for target, value in ((args.output, dataset), (args.summary_output, summary)):
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    compact = {key: value for key, value in summary.items() if key != "episodes"}
    compact["cash_reconstruction"] = dataset["meta"]["cash_reconstruction"]
    compact["tick_count"] = dataset["meta"]["tick_count"]
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
