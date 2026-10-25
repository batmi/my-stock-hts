"""KOSPI entry-only filter proxy: independent fixed-horizon cash sleeves.

This is NOT the stock-picking system's backtest. H initially equal-funded sleeves
rotate one per trading session. A sleeve uses yesterday's closing filter state
to buy at today's open, holds for H sessions regardless of subsequent filter
changes, and exits at the scheduled open. If blocked, it stays cash for that
entire cycle. Each sleeve compounds its own proceeds; weights can subsequently
drift. There is no leverage, interest, dividend income or futures roll assumption.

Signals are SMA +/- a symmetric hysteresis band. Warmup is unavailable, not an
entry signal. Only public index OHLC is read from SQLite in query-only mode.
No application/config imports or database writes are performed.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import date
import json
import math
from pathlib import Path
import sqlite3
import statistics


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data/krx_openapi.db"
PERIODS = (20, 40, 50, 60, 80, 100, 120, 150, 200, 250)
HORIZONS = (20, 60, 120)
COSTS_BPS = (0.0, 10.0, 25.0)


def _day(value):
    return str(value)[:10].replace("-", "")


def _date(value):
    value = _day(value)
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def load_rows(db_path=DEFAULT_DB):
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    try:
        source = con.execute(
            "SELECT bas_dd, open, high, low, close FROM index_daily "
            "WHERE api_id='kospi_dd_trd' AND name='코스피' ORDER BY bas_dd"
        ).fetchall()
    finally:
        con.close()
    rows = [dict(zip(("date", "open", "high", "low", "close"), row)) for row in source]
    validate_rows(rows)
    return rows


def validate_rows(rows):
    previous = None
    for row in rows:
        day = _day(row["date"])
        if previous is not None and day <= previous:
            raise ValueError("Dates must be unique and strictly increasing")
        previous = day
        for field in ("open", "high", "low", "close"):
            value = row.get(field)
            if value is None or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"Invalid {field} on {day}: {value}")
        if float(row["low"]) > min(float(row["open"]), float(row["close"])):
            raise ValueError(f"OHLC low inconsistent on {day}")
        if float(row["high"]) < max(float(row["open"]), float(row["close"])):
            raise ValueError(f"OHLC high inconsistent on {day}")


def filter_allowed(rows, period=None, band_pct=1.0):
    """Close-known signal; None before SMA warmup. OFF has no filter delay."""
    if period is None:
        return [True] * len(rows)
    if int(period) != period or period < 1 or not 0 <= band_pct < 100:
        raise ValueError("Positive integer period and band in [0, 100) required")
    window, total, blocked, signals = deque(), 0.0, False, []
    band = float(band_pct) / 100
    for row in rows:
        close = float(row["close"])
        window.append(close)
        total += close
        if len(window) > period:
            total -= window.popleft()
        if len(window) < period:
            signals.append(None)
            continue
        sma = total / period
        if close < sma * (1 - band):
            blocked = True
        elif close > sma * (1 + band):
            blocked = False
        signals.append(not blocked)
    return signals


def run_entry_gate(rows, allowed, holding_days, start="20110101", cost_bps=10.0):
    """Return continuous NAV and completed/open cohorts, initial capital 1.

    Each trading-day index rotates one of H sleeves; entry i exits at i+H open.
    The first H sessions ramp from cash. Exit and subsequent entry costs both
    apply at rollover, including OFF. Buys spend cash including their fee.
    """
    if len(rows) != len(allowed):
        raise ValueError("Signal and price lengths differ")
    if int(holding_days) != holding_days or holding_days < 1:
        raise ValueError("holding_days must be a positive integer")
    if not 0 <= cost_bps < 10_000:
        raise ValueError("cost_bps must be in [0, 10000)")
    h = int(holding_days)
    fee = cost_bps / 10_000
    sleeves = [{"cash": 1.0 / h, "units": 0.0, "cohort": None} for _ in range(h)]
    total_cash, total_units = 1.0, 0.0
    curve, cohorts, events = [], [], []
    session = 0
    for i, row in enumerate(rows):
        day = _day(row["date"])
        if day < _day(start):
            continue
        slot = sleeves[session % h]
        opening, close = float(row["open"]), float(row["close"])
        if slot["units"]:
            sale = slot["units"] * opening
            exit_fee = sale * fee
            proceeds = sale - exit_fee
            total_units -= slot["units"]
            total_cash += proceeds
            slot["cash"], slot["units"] = proceeds, 0.0
            cohort = slot["cohort"]
            cohort.update({"exit_date": day, "exit_index": i, "exit_price": opening,
                           "exit_fee": exit_fee, "proceeds": proceeds,
                           "net_return_pct": (proceeds / cohort["initial_cash"] - 1) * 100})
            events.append({"date": day, "side": "sell", "amount": sale, "fee": exit_fee})
            slot["cohort"] = None
        # The observation at i close is never used to decide an i-open order.
        signal = allowed[i - 1] if i > 0 else None
        if signal is True:
            initial_cash = slot["cash"]
            buy_amount = initial_cash / (1 + fee)
            entry_fee = initial_cash - buy_amount
            units = buy_amount / opening
            total_cash -= initial_cash
            total_units += units
            slot["cash"], slot["units"] = 0.0, units
            cohort = {"entry_date": day, "entry_index": i, "signal_date": _day(rows[i - 1]["date"]),
                      "entry_price": opening, "initial_cash": initial_cash, "entry_fee": entry_fee,
                      "units": units, "exit_date": None, "sleeve": session % h}
            slot["cohort"] = cohort
            cohorts.append(cohort)
            events.append({"date": day, "side": "buy", "amount": buy_amount, "fee": entry_fee})
        stock_value = total_units * close
        nav = total_cash + stock_value
        # Small floating-point residue is harmless; a material negative cash balance is not.
        if total_cash < -1e-10 or total_units < -1e-10 or nav <= 0:
            raise ValueError("Inventory/cash invariant failed")
        curve.append({"date": day, "nav": nav, "cash": max(0.0, total_cash),
                      "stock_value": stock_value, "exposure": stock_value / nav,
                      "signal_for_entry": signal, "entry_slot": session % h})
        session += 1
    if not curve:
        raise ValueError("No evaluation rows")
    end = curve[-1]
    return {"holding_days": h, "cost_bps": cost_bps, "curve": curve, "cohorts": cohorts,
            "events": events, "final_net_liquidation_nav": end["nav"] - end["stock_value"] * fee,
            "open_cohorts": sum(slot["units"] > 0 for slot in sleeves)}


def summarize_window(run, start, end):
    curve = run["curve"]
    selected = [row for row in curve if _day(start) <= row["date"] <= _day(end)]
    if not selected:
        return {"sessions": 0}
    previous = [row for row in curve if row["date"] < selected[0]["date"]]
    baseline = previous[-1]["nav"] if previous else 1.0
    baseline_date = previous[-1]["date"] if previous else selected[0]["date"]
    years = (_date(selected[-1]["date"]) - _date(baseline_date)).days / 365.2425
    growth = selected[-1]["nav"] / baseline
    high, mdd = baseline, 0.0
    for row in selected:
        high = max(high, row["nav"])
        mdd = min(mdd, row["nav"] / high - 1)
    entries = [e for e in run["events"] if e["side"] == "buy" and selected[0]["date"] <= e["date"] <= selected[-1]["date"]]
    sales = [e for e in run["events"] if e["side"] == "sell" and selected[0]["date"] <= e["date"] <= selected[-1]["date"]]
    allowed = [row["signal_for_entry"] for row in selected if row["signal_for_entry"] is not None]
    return {
        "start": selected[0]["date"], "end": selected[-1]["date"], "baseline_date": baseline_date,
        "sessions": len(selected), "years": years, "start_nav": baseline, "end_nav": selected[-1]["nav"],
        "total_return_pct": (growth - 1) * 100,
        "cagr_pct": (growth ** (1 / years) - 1) * 100 if years > 0 else None,
        "mdd_pct": mdd * 100,
        "mean_exposure_pct": statistics.mean(row["exposure"] for row in selected) * 100,
        "fully_cash_sessions": sum(row["exposure"] < 1e-10 for row in selected),
        "allowed_entry_sessions": sum(allowed), "known_signal_sessions": len(allowed),
        "entries": len(entries), "scheduled_exits": len(sales),
        "fees_in_initial_capital_units": sum(e["fee"] for e in entries + sales),
    }


def forward_returns(rows, allowed, horizon, start, end):
    """Open-to-open outcomes grouped by lagged signal; overlapping, not IID."""
    groups = {"allowed": [], "blocked": [], "all": []}
    for i in range(1, len(rows) - horizon):
        entry, exit_row = rows[i], rows[i + horizon]
        if _day(entry["date"]) < _day(start) or _day(exit_row["date"]) > _day(end):
            continue
        signal = allowed[i - 1]
        if signal is None:
            continue
        ret = (float(exit_row["open"]) / float(entry["open"]) - 1) * 100
        groups["allowed" if signal else "blocked"].append(ret)
        groups["all"].append(ret)
    return {name: {"n_overlapping": len(values),
                   "mean_pct": statistics.mean(values) if values else None,
                   "median_pct": statistics.median(values) if values else None,
                   "positive_pct": sum(value > 0 for value in values) / len(values) * 100 if values else None}
            for name, values in groups.items()}


def build_report(rows, periods=PERIODS, horizons=HORIZONS, costs_bps=COSTS_BPS,
                 band_pct=1.0, start="20110101", include_curves=False):
    validate_rows(rows)
    end = _day(rows[-1]["date"])
    windows = {"full_2011_2026": (start, end), "recent_2021_2026": ("20210101", end),
               "recent_2021_2025": ("20210101", "20251231"),
               "pre_2026": (start, "20251231"), "year_2026": ("20260101", end)}
    result = {"method": {
        "model": "entry-only index cash-sleeve proxy; NOT the actual stock-picking system",
        "initial_capital": 1.0, "allocation": "H initially equal sleeves independently compound; no leverage",
        "execution": "previous close signal -> next session open; scheduled exit H sessions later at open",
        "blocked_entry": "that sleeve stays cash until its next scheduled cycle H sessions later",
        "holding_exit": "filter changes do not liquidate existing cohorts",
        "price_return_only": True, "cash_interest": 0.0,
        "cost": "symmetric one-way bps; buys spend cash including fee; OFF also pays scheduled rollover costs",
        "windows": "slices of one continuous run, with previous close NAV as subwindow baseline",
        "exposure": "average close-marked invested value / NAV; first H sessions ramp from cash",
        "sleeve_phase": "one sleeve rotates every session; renumbering equal-initial-capital sleeves "
                        "only permutes identities and cannot change economic results",
        "forward_returns": "overlapping open-to-open outcomes; descriptive, not independent observations",
        "limitations": ["Horizon and sleeve allocation are proxy assumptions, not live ATR exits or live sizing",
                        "No dividend income, index-product tracking error, spread variability or risk-free cash yield",
                        "No standard errors from overlapping cohorts; no significance claim from daily sample count",
                        "Filters reduce exposure, so lower drawdown alone is not proof of better entry timing"],
    }, "data": {"first": _day(rows[0]["date"]), "last": end, "rows": len(rows)},
        "band_pct": band_pct, "windows": windows, "runs": [], "forward_returns": {}}
    for period in (None, *periods):
        label = "OFF" if period is None else f"SMA{period}"
        signal = filter_allowed(rows, period, band_pct)
        evaluation_indices = [i for i, row in enumerate(rows) if _day(row["date"]) >= _day(start)]
        if evaluation_indices and evaluation_indices[0] > 0 and signal[evaluation_indices[0] - 1] is None:
            raise ValueError(f"Insufficient common warmup for {label}")
        result["forward_returns"][label] = {
            str(h): {name: forward_returns(rows, signal, h, lo, hi) for name, (lo, hi) in windows.items()}
            for h in (20, 60)
        }
        for horizon in horizons:
            for cost in costs_bps:
                run = run_entry_gate(rows, signal, horizon, start, cost)
                item = {"filter": label, "period": period, "holding_days": horizon, "cost_bps": cost,
                        "metrics": {name: summarize_window(run, lo, hi) for name, (lo, hi) in windows.items()},
                        "final_net_liquidation_nav": run["final_net_liquidation_nav"],
                        "open_cohorts": run["open_cohorts"]}
                if include_curves:
                    item["curve"] = run["curve"]
                result["runs"].append(item)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--periods", type=int, nargs="+", default=PERIODS)
    parser.add_argument("--horizons", type=int, nargs="+", default=HORIZONS)
    parser.add_argument("--costs-bps", type=float, nargs="+", default=COSTS_BPS)
    parser.add_argument("--band-pct", type=float, default=1.0)
    parser.add_argument("--start", default="20110101")
    parser.add_argument("--include-curves", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = build_report(load_rows(args.db), args.periods, args.horizons, args.costs_bps,
                          args.band_pct, args.start, args.include_curves)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
