"""Causality, inventory and fixed-horizon semantics for the index proxy."""
from datetime import date, timedelta

import pytest

from tools.audit_kospi_entry_gate import (
    filter_allowed, forward_returns, run_entry_gate, summarize_window, validate_rows,
)


def prices(opens, closes=None):
    closes = closes or opens
    return [{"date": (date(2020, 1, 1) + timedelta(days=i)).strftime("%Y%m%d"),
             "open": float(o), "close": float(c), "high": float(max(o, c)), "low": float(min(o, c))}
            for i, (o, c) in enumerate(zip(opens, closes))]


def test_blocked_sleeves_stay_cash_without_costs():
    rows = prices([100, 120, 70, 200, 80])
    run = run_entry_gate(rows, [False] * len(rows), 2, start="20200102", cost_bps=25)
    assert all(point["nav"] == 1 and point["exposure"] == 0 for point in run["curve"])
    assert not run["events"]


def test_signal_uses_previous_close_and_fills_next_open_after_gap():
    rows = prices([100, 200, 200], [100, 200, 200])
    run = run_entry_gate(rows, [True, False, False], 2, start="20200102", cost_bps=0)
    assert run["cohorts"][0]["entry_price"] == 200
    assert run["curve"][0]["nav"] == 1
    assert run["cohorts"][0]["signal_date"] == "20200101"
    changed = run_entry_gate(rows, [False, True, True], 2, start="20200102", cost_bps=0)
    assert changed["curve"][0]["exposure"] == 0


def test_filter_turning_off_does_not_exit_open_cohort():
    rows = prices([100, 100, 110, 120, 130])
    run = run_entry_gate(rows, [True, False, False, False, False], 3, start="20200102", cost_bps=0)
    cohort = run["cohorts"][0]
    assert cohort["exit_index"] - cohort["entry_index"] == 3
    assert cohort["exit_date"] == "20200105"
    assert [event["date"] for event in run["events"] if event["side"] == "sell"] == ["20200105"]


def test_blocked_sleeve_waits_its_entire_cycle():
    rows = prices([100] * 6)
    run = run_entry_gate(rows, [False, True, True, True, True, True], 3, start="20200102", cost_bps=0)
    # Sleeve zero skipped Jan 2. It cannot enter on Jan 3 or 4 despite new signals.
    zero = [cohort for cohort in run["cohorts"] if cohort["sleeve"] == 0]
    assert zero[0]["entry_date"] == "20200105"


def test_round_trip_cost_uses_cash_inclusive_buy_and_preserves_cash():
    rows = prices([100, 100, 100])
    run = run_entry_gate(rows, [True, False, False], 1, start="20200102", cost_bps=10)
    assert run["curve"][0]["nav"] == pytest.approx(1 / 1.001)
    assert run["curve"][1]["nav"] == pytest.approx(0.999 / 1.001)
    assert run["curve"][1]["exposure"] == 0
    assert all(row["cash"] >= 0 and row["exposure"] <= 1 + 1e-12 for row in run["curve"])


def test_off_has_same_scheduled_rollover_costs_and_initial_ramp():
    rows = prices([100] * 5)
    run = run_entry_gate(rows, [True] * 5, 2, start="20200102", cost_bps=0)
    assert [row["exposure"] for row in run["curve"]] == pytest.approx([0.5, 1, 1, 1])
    assert len([event for event in run["events"] if event["side"] == "sell"]) == 2


def test_hysteresis_is_strict_and_prefix_invariant():
    rows = prices([100, 90, 90, 110, 110, 80])
    signal = filter_allowed(rows, 2, band_pct=0)
    assert signal[:4] == [None, False, False, True]
    for n in range(1, len(rows)):
        assert filter_allowed(rows[:n], 2, 0) == signal[:n]


def test_future_prices_cannot_change_earlier_nav():
    rows = prices([100, 110, 120, 130, 140, 150])
    signal = filter_allowed(rows, 2, band_pct=1)
    full = run_entry_gate(rows, signal, 2, start="20200103", cost_bps=10)
    prefix = run_entry_gate(rows[:4], signal[:4], 2, start="20200103", cost_bps=10)
    assert full["curve"][:2] == prefix["curve"]


def test_forward_returns_use_next_open_and_full_within_window_horizon():
    rows = prices([100, 200, 300, 400, 500])
    signal = [True, False, False, False, False]
    result = forward_returns(rows, signal, 2, "20200102", "20200104")
    assert result["allowed"]["n_overlapping"] == 1
    assert result["allowed"]["mean_pct"] == 100  # Jan 2 open 200 -> Jan 4 open 400.
    assert result["blocked"]["n_overlapping"] == 0


def test_subwindow_is_continuous_nav_slice_not_restart():
    rows = prices([100, 100, 110, 121, 133.1])
    run = run_entry_gate(rows, [True] * 5, 1, start="20200102", cost_bps=0)
    window = summarize_window(run, "20200104", "20200105")
    assert window["baseline_date"] == "20200103"
    assert window["total_return_pct"] == pytest.approx(21)


def test_invalid_inputs_fail_instead_of_silent_investment():
    with pytest.raises(ValueError):
        validate_rows(prices([100, 0]))
    with pytest.raises(ValueError):
        run_entry_gate(prices([100, 100]), [True], 1)
    with pytest.raises(ValueError):
        run_entry_gate(prices([100, 100]), [True, True], 0)
