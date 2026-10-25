"""Safety and causal signal checks for the requested market-gate comparison."""
import json
from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
from audit_kospi_dual_filter import (signals, load_input, choose_start,
                                     bootstrap_difference, complete_rolling_windows)


def rows_for(closes, first=date(2011,1,3), saturday=False):
    rows = []
    day = first
    for value in closes:
        while day.weekday() == 6 or (day.weekday()==5 and not saturday):
            day += timedelta(days=1)
        rows.append({'date':day.strftime('%Y%m%d'),'open':float(value),'high':float(value),
                     'low':float(value),'close':float(value)})
        day += timedelta(days=1)
    return rows


class DualSignalTests(unittest.TestCase):
    def test_both_components_required(self):
        for closes in ([100]*80+[200]*20+[80], [100]*80+[50]*40+[100]):
            with self.subTest(tail=closes[-2:]):
                s = signals(rows_for(closes))
                self.assertNotEqual(bool(s['SMA80_BAND0'][-1]),bool(s['SMA9_GT50'][-1]))
                self.assertFalse(s['DUAL9_50_80'][-1])
        self.assertTrue(signals(rows_for(range(100,230)))['DUAL9_50_80'][-1])

    def test_equality_blocks_without_hysteresis(self):
        s = signals(rows_for([100]*150))
        for rule in ('DUAL9_50_80','SMA80_BAND0','SMA9_GT50'):
            self.assertFalse(s[rule].any())
        self.assertTrue(s['SMA80_BAND1'][-1])

    def test_future_prices_cannot_change_past_signals(self):
        rng = np.random.default_rng(51)
        c = 100*np.exp(np.cumsum(rng.normal(0,.02,250)))
        a = signals(rows_for(c))
        c[180:] *= 4
        b = signals(rows_for(c))
        for k in a:
            np.testing.assert_array_equal(a[k][:180],b[k][:180])

    def test_common_start_has_prior_warmup(self):
        rows = rows_for([100]*150)
        self.assertEqual(choose_start(rows),100)
        with self.assertRaises(ValueError):
            choose_start(rows,rows[99]['date'])
        self.assertEqual(choose_start(rows,rows[100]['date']),100)

    def test_old_saturday_retained_modern_saturday_rejected(self):
        temp_root = ROOT/'tmp'
        temp_root.mkdir(exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=temp_root) as d:
                path = Path(d)/'history.json'
                old = rows_for([100]*110,date(1997,1,3),True)
                path.write_text(json.dumps(old))
                self.assertEqual(len(load_input(path)),110)
                modern = rows_for([100]*110,date(2011,1,3),True)
                path.write_text(json.dumps(modern))
                with self.assertRaises(ValueError):
                    load_input(path)
        finally:
            try:
                temp_root.rmdir()
            except OSError:
                pass

    def test_zero_paired_difference_has_zero_interval(self):
        rows = rows_for([100]*300)
        r = bootstrap_difference([x['date'] for x in rows],np.zeros(300),np.zeros(300),20)
        for w in r['windows'].values():
            self.assertEqual(w['mean_difference_pp_per_year'],0)
            for b in w['blocks'].values():
                self.assertEqual(b['paired_percentile_95'],[0,0])

    def test_rolling_window_does_not_include_missing_initial_quarter(self):
        rows = rows_for([100]*1300, date(1995,9,1))
        dates = [r['date'] for r in rows]
        sim = {k:np.zeros(len(rows)) for k in ('returns','position','turnover')}
        windows = complete_rolling_windows(dates,sim,'19950831')
        self.assertEqual(windows[0]['requested_start'],'19951001')
        self.assertTrue(all(w['start'] >= w['requested_start'] for w in windows))


if __name__ == '__main__':
    unittest.main()
