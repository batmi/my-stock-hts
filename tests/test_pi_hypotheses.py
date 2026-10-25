"""Audit-only checks; unittest avoids importing the application's pytest fixtures.

python3 -B -m unittest discover -s tests -p test_pi_hypotheses.py -v
"""
import ast
from datetime import date, timedelta
from pathlib import Path
import unittest

from tools.audit_pi_exit_hypotheses import PreviousDayATR, make_trigger, simulate_episode
from tools.audit_pi_hypotheses import ConfirmedPrices, prepare_price_history, structural_trigger
from tools.audit_pi_market_hypotheses import filter_series
from tools.audit_pi_filter_on import cash_path, intraday_bounds


def episode(prices, *, later_add=False, is_open=False):
    buy = {"time": "2026-08-21 09:00:00", "qty": 10, "price": 100.0,
           "amount": 1000.0, "fee": 0.0}
    buys = [buy]
    if later_add:
        buys.append({"time": "2026-08-21 09:10:00", "qty": 5, "price": 120.0,
                     "amount": 600.0, "fee": 0.0})
    quantity = sum(row["qty"] for row in buys)
    sell = {"time": "2026-08-21 15:00:00", "qty": quantity, "price": 90.0,
            "amount": quantity * 90.0, "fee": int(quantity * 90.0 * .002177)}
    return {"id": "fixture", "code": "000000", "name": "fixture", "start": buy["time"],
            "end": None if is_open else sell["time"], "buys": buys,
            "sells": [] if is_open else [sell], "exit_is_auto": True,
            "ticks": [{"time": f"2026-08-21 09:{i+1:02}:00.000", "price": price,
                       "regular_session": True, "source": {"line": i+1}}
                      for i, price in enumerate(prices)]}


class EconomicReplayTests(unittest.TestCase):
    def test_trigger_executes_next_observation_and_skips_later_add(self):
        row = simulate_episode(episode([105, 102, 80], later_add=True), make_trigger("take_profit_5"))
        self.assertEqual(row["execution_price"], 102)
        self.assertEqual(row["net_profit"], 18)  # 1020 - 1000 - floor(1020*.002177)
        self.assertEqual(row["skipped_adds"], 1)
        self.assertEqual(row["buy_amount"], 1000)

    def test_breakeven_is_not_a_guaranteed_break_even_fill(self):
        row = simulate_episode(episode([110, 99, 95]), make_trigger("bep_after_5"))
        self.assertEqual(row["execution_price"], 95)
        self.assertEqual(row["net_profit"], -52)

    def test_future_profit_does_not_arm_previous_observations(self):
        row = simulate_episode(episode([99, 98, 110]), make_trigger("bep_after_5"))
        self.assertFalse(row["early_exit"])
        self.assertEqual(row["execution_price"], 90)

    def test_signal_without_an_execution_quote_keeps_actual_exit(self):
        row = simulate_episode(episode([105]), make_trigger("take_profit_5"))
        self.assertFalse(row["early_exit"])
        self.assertEqual(row["net_profit"], -101)

    def test_open_position_is_marked_with_liquidation_cost(self):
        row = simulate_episode(episode([105], is_open=True))
        self.assertEqual(row["valuation"], "endpoint_net_liquidation")
        self.assertEqual(row["net_profit"], 48)

    def test_extra_exit_cost_cannot_improve_fixed_schedule_profit(self):
        original = episode([105, 102])
        plain = simulate_episode(original, make_trigger("take_profit_5"))
        stressed = simulate_episode(original, make_trigger("take_profit_5"), extra_exit_bps=20)
        self.assertLess(stressed["net_profit"], plain["net_profit"])

    def test_overlay_slippage_keeps_actual_fill_control_unchanged(self):
        original = episode([105, 102])
        control = simulate_episode(original, overlay_execution_bps=40)
        self.assertEqual(control["net_profit"], simulate_episode(original)["net_profit"])
        plain = simulate_episode(original, make_trigger("take_profit_5"))
        stressed = simulate_episode(original, make_trigger("take_profit_5"), overlay_execution_bps=40)
        self.assertLess(stressed["net_profit"], plain["net_profit"])


class InformationBoundaryTests(unittest.TestCase):
    def histories(self):
        start = date(2026, 1, 1)
        return [{"date": (start + timedelta(days=n)).strftime("%Y%m%d"),
                 "open": 100, "high": 101, "low": 99, "close": 100} for n in range(150)]

    def test_same_day_and_future_ohlc_do_not_enter_structure_signal(self):
        rows = self.histories()
        query_day = rows[-1]["date"]
        original = ConfirmedPrices({"000000": prepare_price_history(rows)})
        rows[-1].update(high=10000, close=10000)
        altered = ConfirmedPrices({"000000": prepare_price_history(rows)})
        timestamp = f"{query_day[:4]}-{query_day[4:6]}-{query_day[6:]} 10:00:00"
        self.assertEqual(original.previous("000000", timestamp), altered.previous("000000", timestamp))
        state = {"code": "000000", "max_observed_return": 6}
        tick = {"time": timestamp, "price": 99}
        self.assertTrue(structural_trigger("ema20_after_5", altered)(state, tick))
        state["max_observed_return"] = 4
        self.assertFalse(structural_trigger("ema20_after_5", altered)(state, tick))

    def test_atr_lookup_is_strictly_before_current_date(self):
        history = [{"date": r["date"], "atr": 2} for r in self.histories()]
        history[-1]["atr"] = 1000
        day = history[-1]["date"]
        self.assertEqual(PreviousDayATR({"000000": history}).get("000000", day), 2)

    def test_old_daily_breakdown_does_not_trigger_on_first_profit_day(self):
        class Prices:
            def previous(self, code, timestamp):
                return {"date": "20260820", "close": 90, "ema20": 100}
        trigger = structural_trigger("ema20_confirmed_after_5", Prices())
        state = {"code": "000000", "max_observed_return": 5}
        self.assertFalse(trigger(state, {"time": "2026-08-21 10:00:00", "price": 105}))

    def test_filter_preserves_hysteresis_inside_band(self):
        rows = [{"date": str(i), "close": p} for i, p in enumerate([100, 100, 90, 95, 110])]
        result = filter_series(rows, 2, 1)
        self.assertFalse(result[1]["blocked"])
        self.assertTrue(result[2]["blocked"])
        self.assertFalse(result[-1]["blocked"])

    def test_audit_scripts_do_not_import_operating_application(self):
        root = Path(__file__).resolve().parents[1]
        names = ("audit_pi_hypotheses.py", "audit_pi_trade_evidence.py",
                 "audit_pi_exit_hypotheses.py", "audit_pi_market_hypotheses.py", "audit_pi_filter_on.py")
        for filename in names:
            path = root / "tools" / filename
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], {"config", "api", "modules", "core"}, path.name)


class FilterOnProofTests(unittest.TestCase):
    def test_even_daily_high_below_boundary_proves_all_prices_blocked(self):
        rows = [{"date": "20260819", "close": 100, "high": 101},
                {"date": "20260820", "close": 100, "high": 101},
                {"date": "20260821", "close": 85, "high": 90}]
        result = intraday_bounds(rows, "20260821", "20260821", 3, 1)[0]
        self.assertTrue(result["blocked_for_entire_session"])
        for price in (85, 86, 88, 90):
            self.assertLess(price, .99 * (200 + price) / 3)

    def test_blocked_close_does_not_prove_intraday_blocking(self):
        rows = [{"date": "20260819", "close": 100, "high": 101},
                {"date": "20260820", "close": 100, "high": 101},
                {"date": "20260821", "close": 85, "high": 110}]
        self.assertFalse(intraday_bounds(rows, "20260821", "20260821", 3, 1)[0]["blocked_for_entire_session"])

    def test_no_trade_path_has_constant_equity_and_zero_cost(self):
        result = cash_path(10_000_000, ["20260821", "20260824"])
        self.assertEqual(result["final_equity"], 10_000_000)
        self.assertEqual(result["fees"], 0)
        self.assertEqual(result["return_pct"], 0)
        self.assertEqual([r["equity"] for r in result["daily"]], [10_000_000, 10_000_000])


if __name__ == "__main__":
    unittest.main()
