"""Compare KOSPI entry gates using fixed-horizon, independently funded sleeves.

Input is a chronological JSON array of date (YYYYMMDD), open, high, low, close,
or an object with that array under "rows" and optional source metadata.
Signals are known at the previous close and executed at the following open.
Changing a filter never liquidates an existing sleeve: its scheduled exit is H
sessions after entry. This index proxy is NOT a backtest of stock selection,
ATR exits, position sizing or pyramiding. It reuses the audited cash accounting
from audit_kospi_entry_gate without importing application settings.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

if __package__:
    from .audit_kospi_entry_gate import (
        COSTS_BPS, HORIZONS, filter_allowed, forward_returns, run_entry_gate,
        summarize_window, validate_rows,
    )
else:
    from audit_kospi_entry_gate import (
        COSTS_BPS, HORIZONS, filter_allowed, forward_returns, run_entry_gate,
        summarize_window, validate_rows,
    )


RULES = {
    "OFF": "Always permit entry after the common warmup",
    "SMA80_BAND1": "Block below SMA80 * 0.99; allow above SMA80 * 1.01; otherwise retain state",
    "DUAL9_50_80": "SMA9 > SMA50 AND close > SMA80; strict comparisons, no band or hysteresis",
    "SMA80_BAND0": "close > SMA80; strict comparison, equality blocked, no hysteresis",
    "SMA9_GT50": "SMA9 > SMA50; strict comparison, equality blocked, no hysteresis",
    "SMA100_BAND1": "Block below SMA100 * 0.99; allow above SMA100 * 1.01; otherwise retain state",
}
BASELINE = "SMA80_BAND1"


def validate_date(value):
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise ValueError("Dates must be strings in YYYYMMDD format")
    datetime.strptime(value, "%Y%m%d")
    return value


def normalize_rows(rows):
    """Validate actual sessions; historical Saturday trading is permitted."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("Input must be a nonempty JSON array")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Every price row must be an object")
        day = validate_date(row.get("date"))
        prices = {}
        for field in ("open", "high", "low", "close"):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field} must be numeric on {day}")
            prices[field] = float(value)
        normalized.append({"date": day, **prices})
    validate_rows(normalized)
    return normalized


def load_input(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    return normalize_rows(rows)


def trailing_sma(rows, period):
    """Close-inclusive SMA; fsum avoids accumulated rolling-sum error."""
    if not isinstance(period, int) or period < 1:
        raise ValueError("period must be a positive integer")
    result, window = [], deque()
    for row in rows:
        window.append(float(row["close"]))
        if len(window) > period:
            window.popleft()
        result.append(math.fsum(window) / period if len(window) == period else None)
    return result


def build_signals(rows):
    means = {period: trailing_sma(rows, period) for period in (9, 50, 80)}
    signals = {
        "OFF": [True] * len(rows),
        "SMA80_BAND1": filter_allowed(rows, 80, 1.0),
        "DUAL9_50_80": [], "SMA80_BAND0": [], "SMA9_GT50": [],
        "SMA100_BAND1": filter_allowed(rows, 100, 1.0),
    }
    for i, row in enumerate(rows):
        cross = None if means[50][i] is None else means[9][i] > means[50][i]
        above = None if means[80][i] is None else float(row["close"]) > means[80][i]
        signals["SMA9_GT50"].append(cross)
        signals["SMA80_BAND0"].append(above)
        signals["DUAL9_50_80"].append(None if cross is None or above is None else cross and above)
    return signals


def resolve_start(rows, start=None):
    """All six rules share 100 completed observations before the first open."""
    if len(rows) < 101:
        raise ValueError("At least 101 observations required for common 100-day warmup")
    if start is None:
        return rows[100]["date"]
    validate_date(start)
    first = next((i for i, row in enumerate(rows) if row["date"] >= start), None)
    if first is None:
        raise ValueError("No observations at or after --start")
    if first < 100:
        raise ValueError("--start requires at least 100 preceding observations")
    return rows[first]["date"]


def signal_comparison(rows, signals, start, end):
    """Compare yesterday's signals at entry opens, including band decomposition."""
    positions = [i for i, row in enumerate(rows) if start <= row["date"] <= end and i > 0]
    baseline = signals[BASELINE]
    result = {}
    for name, values in signals.items():
        pairs = [(baseline[i - 1], values[i - 1]) for i in positions
                 if baseline[i - 1] is not None and values[i - 1] is not None]
        result[name] = {
            "sessions": len(pairs),
            "allowed": sum(candidate for _, candidate in pairs),
            "allowed_pct": 100 * sum(candidate for _, candidate in pairs) / len(pairs) if pairs else None,
            "both_allowed": sum(old and new for old, new in pairs),
            "both_blocked": sum(not old and not new for old, new in pairs),
            "baseline_only_allowed": sum(old and not new for old, new in pairs),
            "candidate_only_allowed": sum(not old and new for old, new in pairs),
            "different_sessions": sum(old != new for old, new in pairs),
        }
    return result


def build_report(rows, start=None, horizons=HORIZONS, costs_bps=COSTS_BPS,
                 include_curves=False, *, include_restart=True):
    rows = normalize_rows(rows)
    requested_start, start = start, resolve_start(rows, start)
    end = rows[-1]["date"]
    signals = build_signals(rows)
    windows = {
        "full": (start, end), "pre2026": (start, min(end, "20251231")),
        "2011_2025": (max(start, "20110101"), min(end, "20251231")),
        "2021_2025": (max(start, "20210101"), min(end, "20251231")),
        "2026": (max(start, "20260101"), end),
    }
    canonical = json.dumps(rows, separators=(",", ":"), sort_keys=True, allow_nan=False)
    result = {
        "method": {
            "model": "entry-only fixed-horizon index proxy; NOT the actual stock-picking strategy",
            "rules": RULES, "baseline": BASELINE,
            "execution": "Prior session close signal -> next session open; scheduled exit H sessions later at open",
            "holding_exit": "Filter changes do not liquidate existing cohorts",
            "allocation": "H initially equal cash sleeves compound independently; one sleeve rotates daily; no leverage",
            "blocked_entry": "The rotating sleeve stays cash until its next H-session cycle",
            "cost": "Symmetric one-way bps; buys include fee in spent cash; OFF pays scheduled rollover costs too",
            "warmup": "All rules use the same start after at least 100 completed observations; warmup prices are not traded",
            "band_initial_state": "Band rules begin allowed once warmed; neutral or exactly equal band boundaries retain state",
            "strict_equality": "DUAL9_50_80, SMA80_BAND0 and SMA9_GT50 block on equality",
            "windows": "Slices of one continuous run, using previous close NAV; not fresh restarts",
            "initial_and_terminal": "Initial H-session ramp from cash; headline NAV marks open positions without terminal sale fee",
            "terminal_fee": "final_net_liquidation_nav separately deducts one-way sale costs on final holdings",
            "cash_interest": 0.0, "price_return_only": True,
            "forward_returns": "Overlapping open-to-open outcomes; descriptive, not independent observations",
            "limitations": [
                "Fixed horizons and sleeve allocation do not reproduce ATR exits, stock selection, position sizing or pyramiding",
                "Daily signals do not reproduce live intraday market-filter updates",
                "No dividend income, product tracking error, changing spread or risk-free cash yield",
                "Lower exposure can lower drawdown without improving entry timing",
                "2026 was previously observed; it is not an untouched holdout",
                "Subwindow results depend on the portfolio inherited from earlier sessions",
            ],
        },
        "data": {"first": rows[0]["date"], "last": end, "rows": len(rows),
                 "requested_start": requested_start, "evaluation_start": start,
                 "canonical_sha256": hashlib.sha256(canonical.encode()).hexdigest()},
        "windows": windows,
        "signals": {name: signal_comparison(rows, signals, lo, hi) for name, (lo, hi) in windows.items()},
        "runs": [], "forward_returns": {}, "differences_vs_baseline": [],
    }
    for label, signal in signals.items():
        result["forward_returns"][label] = {
            str(h): {name: forward_returns(rows, signal, h, lo, hi) for name, (lo, hi) in windows.items()}
            for h in (20, 60, 120)
        }
        for horizon in horizons:
            for cost in costs_bps:
                run = run_entry_gate(rows, signal, horizon, start, cost)
                item = {"filter": label, "holding_days": horizon, "cost_bps": cost,
                        "metrics": {name: summarize_window(run, lo, hi) for name, (lo, hi) in windows.items()},
                        "final_net_liquidation_nav": run["final_net_liquidation_nav"],
                        "open_cohorts": run["open_cohorts"]}
                if include_curves:
                    item["curve"] = run["curve"]
                result["runs"].append(item)
    controls = {(item["holding_days"], item["cost_bps"]): item
                for item in result["runs"] if item["filter"] == BASELINE}
    for item in result["runs"]:
        control = controls[(item["holding_days"], item["cost_bps"])]
        changes = {}
        for name, metrics in item["metrics"].items():
            if not metrics["sessions"]:
                continue
            changes[name] = {
                field + "_difference_pp": metrics[field] - control["metrics"][name][field]
                for field in ("cagr_pct", "mdd_pct", "mean_exposure_pct")
                if metrics[field] is not None and control["metrics"][name][field] is not None
            }
        result["differences_vs_baseline"].append({
            "filter": item["filter"], "holding_days": item["holding_days"],
            "cost_bps": item["cost_bps"], "metrics": changes,
        })
    if include_restart:
        try:
            restart_start = resolve_start(rows, "20110103")
        except ValueError:
            restart_start = None
        if restart_start is None or restart_start[:4] != "2011":
            result["restart_2011"] = {
                "status": "unavailable",
                "reason": "Input does not provide a warmed 2011 starting session",
            }
        else:
            restarted = build_report(rows, "20110103", horizons, (10.0,),
                                     include_curves, include_restart=False)
            result["restart_2011"] = {
                "status": "available",
                "purpose": "Fresh cash start for comparison with the earlier 2011 audit; not the inherited long-history portfolio",
                **{key: restarted[key] for key in (
                    "data", "windows", "runs", "differences_vs_baseline")},
            }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--start", help="YYYYMMDD; defaults to first open after 100 observed sessions")
    parser.add_argument("--horizons", type=int, nargs="+", default=HORIZONS)
    parser.add_argument("--costs-bps", type=float, nargs="+", default=COSTS_BPS)
    parser.add_argument("--include-curves", action="store_true")
    parser.add_argument("--output", type=Path, help="Optional JSON destination; default stdout")
    args = parser.parse_args(argv)
    result = build_report(load_input(args.input), args.start, args.horizons, args.costs_bps, args.include_curves)
    result["data"]["input_path"] = str(args.input.resolve())
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
