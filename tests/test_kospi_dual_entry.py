"""Strict-AND, chronology and integration checks for the entry-only comparison."""
from datetime import date, timedelta
import json
import math
from pathlib import Path

import pytest

from tools.audit_kospi_dual_entry import (
    BASELINE, build_report, build_signals, load_input, normalize_rows, resolve_start,
    run_entry_gate, trailing_sma,
)
from tools.audit_kospi_entry_gate import filter_allowed


def prices(closes, opens=None, first=date(1997, 1, 1)):
    opens = closes if opens is None else opens
    return [{"date": (first + timedelta(days=i)).strftime("%Y%m%d"),
             "open": float(o), "close": float(c), "high": float(max(o, c)), "low": float(min(o, c))}
            for i, (o, c) in enumerate(zip(opens, closes))]


@pytest.mark.parametrize("wrapped", [False, True])
def test_loader_accepts_array_or_source_metadata_wrapper(monkeypatch, wrapped):
    rows = prices([100] * 102)
    payload = {"metadata": {"source": "audit fixture"}, "rows": rows} if wrapped else rows
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: json.dumps(payload))
    assert load_input("unused.json") == normalize_rows(rows)


@pytest.mark.parametrize("closes,cross,above,dual", [
    ([100] * 71 + [120] * 9, True, True, True),
    ([100] * 71 + [80] * 9, False, False, False),
    ([100] * 71 + [120] * 8 + [90], True, False, False),
    ([50] * 30 + [120] * 41 + [80] * 8 + [100], False, True, False),
    ([100] * 80, False, False, False),
    ([50] * 30 + [100] * 50, False, True, False),
    ([100] * 71 + [179] * 8 + [108], True, False, False),
])
def test_dual_requires_both_strict_conditions(closes, cross, above, dual):
    signals = build_signals(prices(closes))
    assert signals["SMA9_GT50"][-1] is cross
    assert signals["SMA80_BAND0"][-1] is above
    assert signals["DUAL9_50_80"][-1] is dual
    assert signals["DUAL9_50_80"][:79] == [None] * 79


def test_sma80_no_band_control_blocks_equality_without_latching():
    signals = build_signals(prices([100] * 102))
    assert signals["SMA80_BAND0"][-1] is False
    assert signals[BASELINE][-1] is True


def test_band_baselines_keep_existing_audited_semantics():
    rows = prices([100 + 20 * math.sin(i / 9) + i / 10 for i in range(180)])
    signals = build_signals(rows)
    assert signals[BASELINE] == filter_allowed(rows, 80, 1)
    assert signals["SMA100_BAND1"] == filter_allowed(rows, 100, 1)
    for period in (9, 50, 80):
        actual = trailing_sma(rows, period)
        expected = [None if i < period - 1 else
                    math.fsum(row["close"] for row in rows[i - period + 1:i + 1]) / period
                    for i in range(len(rows))]
        assert actual == expected


def test_future_data_cannot_change_past_signals_or_nav():
    rows = prices([100 + 20 * math.sin(i / 11) + i / 5 for i in range(180)])
    full_signals = build_signals(rows)
    for cut in (80, 101, 150):
        prefix_signals = build_signals(rows[:cut])
        for name in full_signals:
            assert prefix_signals[name] == full_signals[name][:cut]
    start = resolve_start(rows)
    full = run_entry_gate(rows, full_signals["DUAL9_50_80"], 20, start=start)
    prefix = run_entry_gate(rows[:150], full_signals["DUAL9_50_80"][:150], 20, start=start)
    assert full["curve"][:50] == prefix["curve"]


def test_dual_uses_next_open_and_does_not_force_exit_when_signal_fails():
    rows = prices([100] * 71 + [120] * 9 + [80] * 5)
    signals = build_signals(rows)["DUAL9_50_80"]
    assert signals[79] is True and signals[80] is False
    run = run_entry_gate(rows, signals, 3, start=rows[80]["date"], cost_bps=0)
    assert len(run["cohorts"]) == 1
    cohort = run["cohorts"][0]
    assert cohort["signal_date"] == rows[79]["date"]
    assert cohort["entry_price"] == 80  # The signal close was 120, before the gap.
    assert cohort["exit_index"] == 83
    assert run["curve"][0]["nav"] == pytest.approx(1)


def test_common_start_requires_prior_100_sessions_and_accepts_saturdays():
    rows = normalize_rows(prices([100] * 104))
    assert any(date.fromisoformat(row["date"]).weekday() == 5 for row in rows)
    assert resolve_start(rows) == rows[100]["date"]
    assert resolve_start(rows, rows[101]["date"]) == rows[101]["date"]
    with pytest.raises(ValueError, match="100 preceding"):
        resolve_start(rows, rows[99]["date"])
    with pytest.raises(ValueError, match="101 observations"):
        resolve_start(rows[:100])
    with pytest.raises(ValueError, match="No observations"):
        resolve_start(rows, "20990101")


def test_report_has_common_windows_all_54_paths_and_baseline_differences():
    rows = prices([100 + i / 10 for i in range(130)], first=date(2025, 9, 15))
    report = build_report(rows, include_curves=True)
    assert len(report["runs"]) == 54
    assert set(report["windows"]) == {"full", "pre2026", "2011_2025", "2021_2025", "2026"}
    assert report["data"]["evaluation_start"] == rows[100]["date"]
    assert len({run["curve"][0]["date"] for run in report["runs"]}) == 1
    assert len({run["curve"][-1]["date"] for run in report["runs"]}) == 1
    for item in report["differences_vs_baseline"]:
        if item["filter"] == BASELINE:
            assert all(value == 0 for metrics in item["metrics"].values() for value in metrics.values())
    for window in report["signals"].values():
        assert window[BASELINE]["different_sessions"] == 0
        for metrics in window.values():
            assert metrics["sessions"] == sum(metrics[name] for name in (
                "both_allowed", "both_blocked", "baseline_only_allowed", "candidate_only_allowed"))
    pre = next(run for run in report["runs"] if run["filter"] == "OFF")["metrics"]["pre2026"]
    assert pre["end"] == "20251231"


def test_2011_restart_uses_fresh_cash_not_inherited_sleeve_weights():
    rows = prices([100 + i / 5 for i in range(600)], first=date(2010, 1, 1))
    report = build_report(rows, horizons=(20,), costs_bps=(10,), include_curves=True)
    restarted = report["restart_2011"]
    assert restarted["status"] == "available"
    assert restarted["data"]["evaluation_start"] == "20110103"
    assert len(restarted["runs"]) == 6
    old = next(run for run in report["runs"] if run["filter"] == "OFF")
    new = next(run for run in restarted["runs"] if run["filter"] == "OFF")
    inherited = next(point for point in old["curve"] if point["date"] == "20110103")
    fresh = new["curve"][0]
    assert inherited["exposure"] == pytest.approx(1)
    assert fresh["cash"] == pytest.approx(0.95)
    assert fresh["nav"] == pytest.approx(0.95 + 0.05 / 1.001)
    assert new["metrics"]["full"]["start_nav"] == 1


def test_invalid_dates_duplicate_dates_and_ohlc_are_rejected():
    rows = prices([100] * 102)
    for bad in ("19970230", "1997-01-01", "123", 19970101):
        changed = [dict(row) for row in rows]
        changed[0]["date"] = bad
        with pytest.raises(ValueError):
            normalize_rows(changed)
    for field, bad in (("close", float("nan")), ("open", -1), ("close", True), ("high", 50)):
        changed = [dict(row) for row in rows]
        changed[0][field] = bad
        with pytest.raises(ValueError):
            normalize_rows(changed)
    with pytest.raises(ValueError, match="unique"):
        normalize_rows([rows[0], rows[0]])
