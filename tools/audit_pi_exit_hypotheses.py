"""Read-only, fixed-entry exit counterfactuals for Raspberry Pi paper trading.

This is an observational replay, not a portfolio backtest. Actual entry times and
quantities remain fixed; an early exit cancels subsequent additions in that
episode. It cannot model capital reuse, changed future signals, or market impact.
No application modules or local operator settings are imported.

Percentage rules use observed post-entry holdings quotes. ATR variants use the
PREVIOUS completed day's Wilder ATR14, never today's eventual high/low, and are
explicit approximations of the live, intraday-updated ATR. A same-proxy control
is reported alongside the alternatives. Signals execute at the next observed
regular-session quote, including gaps, rather than at the requested stop price.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path
import sqlite3
import statistics
from typing import Callable

try:
    from .audit_pi_trade_evidence import (
        BUY_FEE_RATE, SELL_FEE_RATE, DEFAULT_DB, DEFAULT_LOGS, build_dataset,
    )
except ImportError:  # Direct script execution.
    from audit_pi_trade_evidence import (
        BUY_FEE_RATE, SELL_FEE_RATE, DEFAULT_DB, DEFAULT_LOGS, build_dataset,
    )


DEFAULT_DAILY_DB = Path(__file__).resolve().parents[1] / "data/krx_openapi.db"


def _quotes(episode: dict, regular_only: bool) -> list[dict]:
    """Keep the first observation at each timestamp; do not invent missing bars."""
    result, seen = [], set()
    for tick in sorted(episode.get("ticks", []), key=lambda row: row["time"]):
        if regular_only and not tick.get("regular_session", True):
            continue
        if tick["time"] in seen or float(tick["price"]) <= 0:
            continue
        seen.add(tick["time"])
        result.append(tick)
    return result


def _sale(price: float, qty: int, extra_exit_bps: float) -> tuple[float, float, float]:
    execution = float(price) * (1 - extra_exit_bps / 10_000)
    amount = execution * qty
    return execution, amount, float(int(amount * SELL_FEE_RATE))


def _baseline(episode: dict, extra_exit_bps: float, regular_only: bool) -> dict:
    buys, sells = episode["buys"], episode["sells"]
    cost = sum(float(row["amount"]) for row in buys)
    buy_fees = sum(float(row["fee"]) for row in buys)
    proceeds = sell_fees = 0.0
    for row in sells:
        _, amount, fee = _sale(row["price"], int(row["qty"]), extra_exit_bps)
        proceeds += amount
        sell_fees += float(row["fee"]) if not extra_exit_bps else fee
    qty_left = sum(int(row["qty"]) for row in buys) - sum(int(row["qty"]) for row in sells)
    quotes = _quotes(episode, regular_only)
    mark = None
    if qty_left:
        if not quotes:
            raise ValueError(f"Open episode has no eligible price: {episode['id']}")
        mark = quotes[-1]
        _, amount, fee = _sale(mark["price"], qty_left, extra_exit_bps)
        proceeds += amount
        sell_fees += fee
    last = mark or sells[-1]
    net = proceeds - sell_fees - cost - buy_fees
    return {
        "id": episode["id"], "code": episode["code"], "name": episode["name"],
        "start": episode["start"], "original_end": episode["end"],
        "exit_is_auto": episode.get("exit_is_auto"), "early_exit": False,
        "exit_time": last["time"],
        "execution_price": float(last["price"]) * (1 - extra_exit_bps / 10_000),
        "net_profit": net, "buy_amount": cost, "buy_fees": buy_fees,
        "sell_fees": sell_fees, "return_on_buy_amount_pct": net / cost * 100,
        "skipped_adds": 0, "skipped_add_amount": 0.0,
        "valuation": "endpoint_net_liquidation" if mark else "actual_closed",
        "signal_source": None, "exit_source": last.get("source"),
        "reported_actual_net_profit": episode.get("actual_net_profit"),
    }


def simulate_episode(
    episode: dict,
    trigger: Callable[[dict, dict], bool] | None = None,
    *,
    extra_exit_bps: float = 0.0,
    overlay_execution_bps: float = 0.0,
    regular_only: bool = True,
) -> dict:
    """Apply one exit overlay; callable state persists between observations.

    State: qty, cost, buy_fees, avg, high, max_observed_return (percent), atr
    (latest buy/add snapshot), bought_count. Additional rule-specific keys are
    allowed. A signal at one observation executes at the NEXT eligible quote.
    If none precedes the actual sale, use the actual sale. An open episode is
    marked at its last quote, including hypothetical exit costs for comparability.
    overlay_execution_bps penalizes ONLY counterfactual early executions; actual
    fill prices remain unchanged. extra_exit_bps instead stresses every exit.
    """
    if not 0 <= extra_exit_bps < 10_000:
        raise ValueError("extra_exit_bps must be between 0 and 10000")
    if overlay_execution_bps < 0 or extra_exit_bps + overlay_execution_bps >= 10_000:
        raise ValueError("combined exit penalties must be between 0 and 10000")
    baseline = _baseline(episode, extra_exit_bps, regular_only)
    if trigger is None:
        return baseline
    if len(episode["sells"]) > 1:
        raise ValueError("Partial-exit episodes require separate inventory replay")
    buys = sorted(episode["buys"], key=lambda row: row["time"])
    state = {"qty": 0, "cost": 0.0, "buy_fees": 0.0, "avg": 0.0,
             "high": 0.0, "max_observed_return": -math.inf,
             "atr": None, "bought_count": 0, "code": episode["code"]}
    buy_index, pending = 0, None
    for tick in _quotes(episode, regular_only):
        if episode["end"] and tick["time"][:19] >= episode["end"]:
            break
        while buy_index < len(buys) and buys[buy_index]["time"] <= tick["time"]:
            buy = buys[buy_index]
            state["qty"] += int(buy["qty"])
            state["cost"] += float(buy["amount"])
            state["buy_fees"] += float(buy["fee"])
            state["high"] = max(state["high"], float(buy["price"]))
            snapshot = buy.get("snapshot") or {}
            if isinstance(snapshot, dict):
                atr = (snapshot.get("indicators") or {}).get("atr")
                if atr and float(atr) > 0:
                    state["atr"] = float(atr)
            buy_index += 1
            state["bought_count"] = buy_index
        if not state["qty"]:
            continue
        state["avg"] = state["cost"] / state["qty"]
        price = float(tick["price"])
        state["high"] = max(state["high"], price)
        state["max_observed_return"] = max(
            state["max_observed_return"], (price / state["avg"] - 1) * 100,
        )
        if pending is not None:
            execution, amount, fee = _sale(price, state["qty"], extra_exit_bps + overlay_execution_bps)
            net = amount - fee - state["cost"] - state["buy_fees"]
            return {
                **baseline, "early_exit": True, "exit_time": tick["time"],
                "execution_price": execution, "net_profit": net,
                "buy_amount": state["cost"], "buy_fees": state["buy_fees"],
                "sell_fees": fee, "return_on_buy_amount_pct": net / state["cost"] * 100,
                "skipped_adds": len(buys) - buy_index,
                "skipped_add_amount": sum(float(row["amount"]) for row in buys[buy_index:]),
                "valuation": "counterfactual_closed", "signal_time": pending["time"],
                "signal_source": pending.get("source"), "exit_source": tick.get("source"),
                "observed_signal_price": float(pending["price"]),
            }
        if trigger(state, tick):
            pending = tick
    return baseline


def load_daily_atr(db_path: str | Path, codes: list[str]) -> dict[str, list[dict]]:
    """Calculate Wilder ATR14 from official OHLC, with ample historical warmup."""
    path = Path(db_path).resolve()
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    result = {}
    try:
        for code in sorted(set(codes)):
            rows = con.execute(
                "SELECT bas_dd, high, low, close FROM stock_daily "
                "WHERE code=? ORDER BY bas_dd", (code,),
            ).fetchall()
            previous_close = atr = None
            history = []
            for date, high, low, close in rows:
                if not all(v is not None and math.isfinite(float(v)) and float(v) > 0
                           for v in (high, low, close)):
                    continue
                tr = float(high) - float(low)
                if previous_close is not None:
                    tr = max(tr, abs(float(high) - previous_close), abs(float(low) - previous_close))
                atr = tr if atr is None else (atr * 13 + tr) / 14
                previous_close = float(close)
                history.append({"date": str(date).replace("-", ""), "atr": atr})
            result[code] = history
    finally:
        con.close()
    return result


class PreviousDayATR:
    def __init__(self, histories: dict[str, list[dict]]):
        self.histories = {
            code: [{"date": row["date"], "atr": row.get("atr", row.get("atr14"))}
                   for row in rows] for code, rows in histories.items()
        }
        self.dates = {code: [row["date"] for row in rows] for code, rows in histories.items()}
        self.cache = {}

    def get(self, code: str, time: str) -> float | None:
        date = time[:10].replace("-", "")
        key = code, date
        if key not in self.cache:
            i = bisect.bisect_left(self.dates.get(code, []), date) - 1
            # Warmup must precede the queried day; today's final range is never read.
            self.cache[key] = self.histories[code][i]["atr"] if i >= 60 else None
        return self.cache[key]


ARM_DESCRIPTIONS = {
    "actual": "Actual fixed entries/exits; open position marked net liquidation",
    "take_profit_5": "Diagnostic: sell after observed gross return reaches +5%",
    "bep_after_5": "After observed +5%, exit on net break-even breach including actual buy and modeled sell fees",
    "atr_control_proxy": "Proxy control: previous-day ATR; activation 3.0, callback 3.5, floor 5%, uncapped",
    "atr_activation_2_proxy": "Previous-day ATR: activation 2.0, callback 3.5, floor 5%, uncapped",
    "atr_callback_3_proxy": "Previous-day ATR: activation 3.0, callback 3.0, floor 5%, uncapped",
    "atr_activation2_callback3_proxy": "Previous-day ATR: activation 2.0 AND callback 3.0, floor 5%, uncapped",
    "atr_fixed5_proxy": "Previous-day ATR: fixed +5% activation, callback 3.5, floor 5%",
}


def make_trigger(arm: str, daily: PreviousDayATR | None = None):
    if arm == "actual":
        return None
    if arm == "take_profit_5":
        return lambda state, tick: float(tick["price"]) >= state["avg"] * 1.05
    if arm == "bep_after_5":
        def bep(state, tick):
            amount = float(tick["price"]) * state["qty"]
            net = amount - int(amount * SELL_FEE_RATE) - state["cost"] - state["buy_fees"]
            return state["max_observed_return"] >= 5 and net <= 0
        return bep
    if arm not in ARM_DESCRIPTIONS or daily is None:
        raise ValueError(f"Unknown arm or missing daily ATR: {arm}")

    def atr_rule(state, tick):
        atr = daily.get(state["code"], tick["time"])
        if atr is None:
            return False
        activation_mult = 2.0 if arm in ("atr_activation_2_proxy", "atr_activation2_callback3_proxy") else 3.0
        callback_mult = 3.0 if arm in ("atr_callback_3_proxy", "atr_activation2_callback3_proxy") else 3.5
        cb_at_cost = min(60.0, max(5.0, atr * activation_mult / state["avg"] * 100))
        activation = 5.0 if arm == "atr_fixed5_proxy" else cb_at_cost / (100 - cb_at_cost) * 100
        mfe = (state["high"] / state["avg"] - 1) * 100
        stop = min(state["high"] * 0.95, state["high"] - atr * callback_mult)
        state["atr_rule_last"] = {"atr": atr, "activation": activation,
                                  "mfe": mfe, "armed": mfe >= activation, "stop": stop}
        return mfe >= activation and float(tick["price"]) <= stop
    return atr_rule


def _aggregate(rows: list[dict]) -> dict:
    pnl = [row["net_profit"] for row in rows]
    return {"episodes": len(rows), "net_profit": sum(pnl),
            "winners": sum(value > 0 for value in pnl),
            "losers": sum(value < 0 for value in pnl),
            "early_exits": sum(row["early_exit"] for row in rows),
            "buy_amount": sum(row["buy_amount"] for row in rows),
            "skipped_adds": sum(row["skipped_adds"] for row in rows)}


def _groups(rows: list[dict]) -> dict:
    auto = [row for row in rows if row["original_end"] and row["exit_is_auto"]]
    manual = [row for row in rows if row["original_end"] and not row["exit_is_auto"]]
    opened = [row for row in rows if not row["original_end"]]
    ordered = sorted(auto, key=lambda row: row["start"])
    middle = len(ordered) // 2
    months = sorted({row["start"][:7] for row in auto})
    return {"closed_auto": _aggregate(auto), "closed_manual": _aggregate(manual),
            "closed_all": _aggregate(auto + manual),
            "open_endpoint": _aggregate(opened), "all": _aggregate(rows),
            "first_half_auto_entries": _aggregate(ordered[:middle]),
            "second_half_auto_entries": _aggregate(ordered[middle:]),
            "auto_by_entry_month": {month: _aggregate([r for r in auto if r["start"].startswith(month)])
                                    for month in months}}


def atr_snapshot_validation(dataset: dict, daily: PreviousDayATR) -> dict:
    rows = []
    for episode in dataset["episodes"]:
        for fill in episode["buys"] + episode["sells"]:
            snapshot = fill.get("snapshot") or {}
            observed = (snapshot.get("indicators") or {}).get("atr") if isinstance(snapshot, dict) else None
            predicted = daily.get(episode["code"], fill["time"])
            if observed and predicted and float(observed) > 0:
                rows.append({"time": fill["time"], "code": episode["code"],
                             "snapshot_atr": observed, "prior_day_atr": predicted,
                             "relative_difference_pct": (predicted / float(observed) - 1) * 100})
    errors = sorted(abs(row["relative_difference_pct"]) for row in rows)
    return {"n": len(rows), "median_absolute_difference_pct": statistics.median(errors) if errors else None,
            "max_absolute_difference_pct": max(errors) if errors else None,
            "rows": rows,
            "interpretation": "Differences include today's partial range. This is a causal proxy, not exact live ATR replay."}


def analyze(dataset: dict, daily_histories: dict | None = None, *, regular_only: bool = True) -> dict:
    daily = PreviousDayATR(daily_histories) if daily_histories is not None else None
    arms = list(ARM_DESCRIPTIONS) if daily else ["actual", "take_profit_5", "bep_after_5"]
    result = {"method": {
        "fixed_entries": True, "fill_rule": "next eligible observed quote, never ideal threshold",
        "session": "09:00-15:30" if regular_only else "all observed sessions",
        "costs": {"buy": BUY_FEE_RATE, "sell": SELL_FEE_RATE, "rounding": "integer won floor"},
        "baseline": "actual cashflow net; can differ a few won from reported PnL after additions",
        "open_valuation": "last eligible observed quote less hypothetical liquidation cost",
        "atr_input": "previous completed day's official Wilder ATR14; no today's final OHLC" if daily else None,
        "limitations": ["Not a portfolio backtest: freed capital, future entries and risk sizing are not recomputed",
                        "Sampled holdings prices miss between-observation peaks and execution liquidity",
                        "ATR proxy control must be compared with actual exits before interpreting alternative arms",
                        "Results are diagnostics on a small losing sample, not an optimized strategy or forward prediction"],
    }, "arms": {}, "extra_exit_20bps": {}, "overlay_execution_stress": {"20bps": {}, "40bps": {}}}
    for arm in arms:
        rows = [simulate_episode(ep, make_trigger(arm, daily), regular_only=regular_only)
                for ep in dataset["episodes"]]
        stressed = [simulate_episode(ep, make_trigger(arm, daily), extra_exit_bps=20, regular_only=regular_only)
                    for ep in dataset["episodes"]]
        result["arms"][arm] = {"description": ARM_DESCRIPTIONS[arm], "summary": _groups(rows), "episodes": rows}
        result["extra_exit_20bps"][arm] = _groups(stressed)
        for bps in (20, 40):
            overlay_stressed = [
                simulate_episode(ep, make_trigger(arm, daily), overlay_execution_bps=bps,
                                 regular_only=regular_only)
                for ep in dataset["episodes"]
            ]
            result["overlay_execution_stress"][f"{bps}bps"][arm] = _groups(overlay_stressed)
    result["method"]["overlay_execution_stress"] = (
        "20/40bps adverse price adjustment ONLY for hypothetical early exits; "
        "actual fill prices and actual baseline remain untouched. "
        "This controls for the live order-price buffer absent from quote-only fills."
    )
    if daily:
        result["atr_snapshot_validation"] = atr_snapshot_validation(dataset, daily)
        coverage = {}
        for arm in (name for name in arms if name.startswith("atr_")):
            entries = []
            for episode in dataset["episodes"]:
                if not episode["end"] or not episode.get("exit_is_auto"):
                    continue
                rule = make_trigger(arm, daily)
                counts = {"armed_observations": 0, "triggered_observations": 0, "observations": 0}

                def observe(state, tick):
                    triggered = rule(state, tick)
                    info = state.get("atr_rule_last")
                    counts["observations"] += 1
                    counts["armed_observations"] += int(bool(info and info["armed"]))
                    counts["triggered_observations"] += int(triggered)
                    return False  # Measure the same complete actual holding path for every arm.

                simulate_episode(episode, observe, regular_only=regular_only)
                entries.append({"id": episode["id"], "name": episode["name"], **counts})
            coverage[arm] = {
                "episodes_ever_armed": sum(row["armed_observations"] > 0 for row in entries),
                "episodes_ever_triggered": sum(row["triggered_observations"] > 0 for row in entries),
                "episodes": entries,
            }
        result["atr_observed_path_coverage_closed_auto"] = coverage
    return result


def build_report(dataset: dict, store: dict | None = None, *, regular_only: bool = True) -> dict:
    """Aggregate-tool API; store accepts daily records with either atr or atr14."""
    return analyze(dataset, store, regular_only=regular_only)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--logs", type=Path, default=DEFAULT_LOGS)
    parser.add_argument("--dataset", type=Path, help="Previously generated evidence JSON")
    parser.add_argument("--daily-db", type=Path, default=DEFAULT_DAILY_DB)
    parser.add_argument("--no-atr", action="store_true", help="Only exact percentage diagnostics")
    parser.add_argument("--all-sessions", action="store_true", help="Sensitivity: include all logged observation times")
    parser.add_argument("--output", type=Path, help="Optional output path; default stdout")
    args = parser.parse_args(argv)
    dataset = json.loads(args.dataset.read_text()) if args.dataset else build_dataset(args.db, args.logs)
    histories = None if args.no_atr else load_daily_atr(args.daily_db, [ep["code"] for ep in dataset["episodes"]])
    result = analyze(dataset, histories, regular_only=not args.all_sessions)
    output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
