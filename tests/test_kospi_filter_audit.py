"""Economic/information-boundary checks for the standalone KOSPI audit.

.venv/bin/python -B -m unittest discover -s tests -p test_kospi_filter_audit.py -v
"""
import unittest
from datetime import date, timedelta

import numpy as np

from tools.audit_kospi_filter import (drawdown_events, metrics, positions, production_parity,
                                      simulate, sma_gate, validate_rows, walk_forward)


def row(day, op, close):
    return {"date":day,"open":op,"close":close,"high":max(op,close),"low":min(op,close)}


class ExecutionTests(unittest.TestCase):
    def test_first_entry_cannot_capture_pre_entry_gap(self):
        rows=[row("20200102",100,100),row("20200103",200,220)]
        s=simulate(rows,1,[1],cost_bps=0)
        self.assertAlmostEqual(s["returns"][0],.10)

    def test_sell_cannot_avoid_overnight_gap(self):
        rows=[row("20200102",100,100),row("20200103",100,100),row("20200106",80,60)]
        s=simulate(rows,1,[1,0],cost_bps=0)
        self.assertAlmostEqual(s["returns"][1],-.20)

    def test_new_entry_only_receives_open_to_close(self):
        rows=[row("20200102",100,100),row("20200103",100,110),row("20200106",120,132)]
        s=simulate(rows,1,[0,1],cost_bps=0)
        np.testing.assert_allclose(s["returns"],[0,.1])

    def test_round_trip_pays_two_fees(self):
        rows=[row(f"2020010{i}",100,100) for i in (2,3,6,7)]
        s=simulate(rows,1,[1,1,0],cost_bps=10)
        self.assertAlmostEqual(np.prod(1+s["returns"]),.999/1.001)
        self.assertEqual(s["turnover"].sum(),2)

    def test_buy_hold_matches_first_open_last_close(self):
        rows=[row("20200102",100,101),row("20200103",105,110),row("20200106",90,99)]
        s=simulate(rows,1,[1,1],cost_bps=0)
        self.assertAlmostEqual(np.prod(1+s["returns"]),99/105)

    def test_cash_interest_uses_calendar_gap(self):
        rows=[row("20200102",100,100),row("20200103",100,100),row("20200106",100,100)]
        s=simulate(rows,1,[0,0],cash_yield=.03)
        self.assertAlmostEqual(s["returns"][1],1.03**(3/365.25)-1)

    def test_unknown_or_current_close_never_enters_same_day_position(self):
        sig=np.array([False,True,False,True])
        np.testing.assert_array_equal(positions(sig,1),[0,1,0])
        with self.assertRaises(ValueError): positions(sig,1,lag=0)

    def test_future_prices_cannot_change_past_signal_or_returns(self):
        cl=[100,98,95,105,111,115]
        a,_=sma_gate(cl,3,1)
        b,_=sma_gate(cl[:4]+[1,10000],3,1)
        np.testing.assert_array_equal(a[:4],b[:4])


class ValidationTests(unittest.TestCase):
    def test_constant_price_zero_band_never_flips_prior_state(self):
        for p in (20,80,120,130,190,230,250):
            allowed,_=sma_gate(np.full(300,100.0),p,0)
            blocked,_=sma_gate(np.full(300,100.0),p,0,True)
            self.assertTrue(allowed[p-1:].all())
            self.assertFalse(blocked[p-1:].any())

    def test_already_allowed_at_trough_has_no_reopening_delay(self):
        rows=[row("20200102",100,100),row("20200103",80,80),row("20200106",90,90)]
        e=drawdown_events(rows,0,{80:np.array([True,True,True])})[0]["filters"]["80"]
        self.assertEqual(e["sessions_to_allowed_from_trough"],0)
        self.assertEqual(e["rebound_before_first_allowed_pct"],0)
        self.assertFalse(e["was_ever_blocked_before_trough"])

    def test_production_function_matches_all_actual_parameter_shapes(self):
        prices=100*np.exp(np.cumsum(np.random.default_rng(7).normal(0,.02,500)))
        self.assertEqual(production_parity(prices)["valid_bar_mismatches"],0)

    def test_drawdown_includes_initial_capital(self):
        m=metrics(["20200102","20200103"],[-.1,0])
        self.assertAlmostEqual(m["mdd_pct"],-10)

    def test_bad_ohlc_and_duplicate_dates_rejected(self):
        a=row("20200102",100,100)
        with self.assertRaises(ValueError):validate_rows([a,a])
        with self.assertRaises(ValueError):validate_rows([{**a,"low":101}])

    def test_walk_forward_selection_unaffected_by_future_returns(self):
        ds=[]
        d=date(2010,1,1)
        while d<=date(2017,12,29):
            if d.weekday()<5:ds.append(d.strftime("%Y%m%d"))
            d+=timedelta(days=1)
        rows=[row(d,100,100) for d in ds]
        start=next(i for i,d in enumerate(ds) if d>="20110101")
        signal={20:np.ones(len(ds),dtype=bool),80:np.zeros(len(ds),dtype=bool)}
        ev=ds[start:]
        sims={}
        for p in signal:
            returns=np.array([.001+(.0005 if i%2 else -.0005) for i in range(len(ev))])
            if p==80:returns=-returns
            sims[p]={"returns":returns,"position":np.ones(len(ev)),"turnover":np.zeros(len(ev))}
        a,_,_=walk_forward(rows,start,signal,sims)
        for p in sims:
            for i,d in enumerate(ev):
                if d>="20160101":sims[p]["returns"][i]=10 if p==80 else -.99
        b,_,_=walk_forward(rows,start,signal,sims)
        self.assertEqual(a["choices"][0]["chosen_ma"],b["choices"][0]["chosen_ma"])
        self.assertLess(a["choices"][0]["training_end"],"20160101")


if __name__=="__main__":unittest.main()
