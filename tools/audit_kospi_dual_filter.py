"""Read-only comparison of the current hysteresis gate and the requested AND gate.

No application imports, trading APIs, DB writes, or parameter fitting. This is
an index/cash switching experiment, distinct from the production entry-only gate.
The requested rule is evaluated every close, not only on crossover event days.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from audit_kospi_filter import (sma_gate, simulate, positions, metrics, subset,
                               drawdown_events)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/audit_runs/kospi_dual_filter_20261025'
RULES = ('SMA80_BAND1', 'DUAL9_50_80', 'SMA80_BAND0', 'SMA9_GT50', 'SMA100_BAND1', 'OFF')
WINDOWS = {
    'all': ('00000000', '99999999'), 'pre2026': ('00000000', '20251231'),
    'pre2010': ('00000000', '20091231'), '2011_2025': ('20110101', '20251231'),
    '2021_2025': ('20210101', '20251231'), 'observed_2026': ('20260101', '20261231'),
    'asian_crisis_1997_1998': ('19970101', '19981231'),
    'gfc_2007_2009': ('20070101', '20091231'),
    'covid_2020': ('20200101', '20201231'), 'bear_2022': ('20220101', '20221231'),
    'pi_observed_period': ('20260821', '20261023'),
}


def load_input(path):
    data = json.loads(Path(path).read_text())
    rows = data if isinstance(data, list) else data['rows']
    result, previous = [], ''
    for r in rows:
        row = {k: r[k] for k in ('date', 'open', 'high', 'low', 'close')}
        row['date'] = str(row['date']).replace('-', '')
        day = datetime.strptime(row['date'], '%Y%m%d')
        if row['date'] <= previous or day.weekday() == 6:
            raise ValueError(f'Unsorted, duplicate, or Sunday date: {row["date"]}')
        if day.weekday() == 5 and row['date'] >= '19981207':
            raise ValueError(f'Saturday after exchange Saturday closure: {row["date"]}')
        for k in ('open', 'high', 'low', 'close'):
            row[k] = float(row[k])
            if not math.isfinite(row[k]) or row[k] <= 0:
                raise ValueError(f'Invalid {k} on {row["date"]}')
        if row['low'] > min(row['open'], row['close']) or row['high'] < max(row['open'], row['close']):
            raise ValueError(f'Invalid OHLC bounds: {row["date"]}')
        result.append(row)
        previous = row['date']
    if len(result) <= 100:
        raise ValueError('Need more than 100 observations for common warmup')
    return result


def signals(rows):
    c = pd.Series([r['close'] for r in rows], dtype=float)
    ma = {p: c.rolling(p).mean().to_numpy() for p in (9, 50, 80, 100)}
    a = c.to_numpy()
    fast = ma[9] > ma[50]
    above = a > ma[80]
    return {
        'SMA80_BAND1': sma_gate(a, 80, 1.0)[0],
        'DUAL9_50_80': fast & above,
        'SMA80_BAND0': above,
        'SMA9_GT50': fast,
        'SMA100_BAND1': sma_gate(a, 100, 1.0)[0],
        'OFF': np.ones(len(rows), dtype=bool),
    }


def choose_start(rows, requested=None):
    if requested:
        requested = requested.replace('-', '')
        found = next((i for i, r in enumerate(rows) if r['date'] >= requested), None)
        if found is None or found < 100:
            raise ValueError('Requested start lacks 100 prior observations or is after the data')
        return found
    return 100


def window_reports(dates, sim):
    out = {k: subset(dates, sim, a, b) for k, (a, b) in WINDOWS.items()}
    out['annual'] = {y: subset(dates, sim, y+'0101', y+'1231') for y in sorted({d[:4] for d in dates})}
    out['five_year_blocks'] = {str(y): subset(dates, sim, f'{y}0101', f'{y+4}1231')
                               for y in range(1980, 2026, 5) if any(f'{y}0101' <= d <= f'{y+4}1231' for d in dates)}
    return out


def complete_rolling_windows(dates, sim, prior_date, years=3):
    """Quarterly windows with all their requested history actually available."""
    out = []
    for year in range(int(dates[0][:4]), int(dates[-1][:4])-years+2):
        for month in (1,4,7,10):
            lo, hi = f'{year}{month:02}01', f'{year+years}{month:02}01'
            # An evaluation starting mid-quarter must not masquerade as a full
            # three-year window. A holiday before its first session is valid.
            if lo <= prior_date or hi > dates[-1]:
                continue
            end = (datetime.strptime(hi, '%Y%m%d')-timedelta(days=1)).strftime('%Y%m%d')
            m = subset(dates, sim, lo, end)
            if m is not None and m['days'] >= years*200:
                m['requested_start'] = lo
                m['requested_end_exclusive'] = hi
                out.append(m)
    return out


def bootstrap_difference(dates, baseline, candidate, repetitions=2000):
    """Paired primary comparison; annualized arithmetic mean, NOT CAGR CI.

    Annualization uses the actual average sessions/calendar year in each sample
    so historical Saturday sessions are included. No independence of overlapping
    returns, and no multiple-testing correction across arbitrary research choices.
    """
    out = {}
    ds = np.asarray(dates)
    for window in ('pre2026', 'pre2010', '2011_2025', '2021_2025'):
        a, b = WINDOWS[window]
        mask = (ds >= a) & (ds <= b)
        selected = ds[mask]
        if len(selected) < 240:
            continue
        diff = np.asarray(candidate)[mask] - np.asarray(baseline)[mask]
        years = ((datetime.strptime(selected[-1], '%Y%m%d') - datetime.strptime(selected[0], '%Y%m%d')).days+1)/365.25
        annualizer = len(diff)/years
        result = {'sessions': len(diff), 'sessions_per_year': annualizer,
                  'mean_difference_pp_per_year': float(diff.mean()*annualizer*100), 'blocks': {}}
        for block in (20, 60, 120):
            rng = np.random.default_rng(20261025+block)
            samples = np.empty(repetitions)
            for i in range(repetitions):
                starts = rng.integers(0, len(diff), size=math.ceil(len(diff)/block))
                ids = ((starts[:, None]+np.arange(block)) % len(diff)).ravel()[:len(diff)]
                samples[i] = diff[ids].mean()*annualizer*100
            result['blocks'][str(block)] = {'paired_percentile_95': np.quantile(samples, [.025, .975]).tolist()}
        out[window] = result
    return {'comparison': 'DUAL9_50_80 minus SMA80_BAND1', 'repetitions': repetitions,
            'statistic': 'annualized arithmetic mean daily net return difference, not CAGR', 'windows': out}


def production_check(rows, current):
    source = ROOT/'core/indicators.py'
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'get_market_filter_blocked')
    env = {'np': np, 'pd': pd}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), env)
    closes = [r['close'] for r in rows]
    actual = ~env['get_market_filter_blocked'](closes, 80, 1.0, False).to_numpy()
    errors = int(np.sum(actual[79:] != current[79:]))
    if errors:
        raise ValueError(f'Current production gate mismatch: {errors}')
    return {'different_valid_signal_days': errors,
            'function_sha256': hashlib.sha256(ast.unparse(node).encode()).hexdigest()}


def signal_comparison(rows, gate, start):
    ds = np.asarray([r['date'] for r in rows[start:]])
    current = gate['SMA80_BAND1'][start-1:-1]
    new = gate['DUAL9_50_80'][start-1:-1]
    result = {}
    for name, (lo, hi) in WINDOWS.items():
        mask = (ds >= lo) & (ds <= hi)
        if not mask.any():
            continue
        a, b = current[mask], new[mask]
        result[name] = {'sessions': int(mask.sum()), 'both_allow': int(np.sum(a & b)),
                        'current_only_allows': int(np.sum(a & ~b)),
                        'dual_only_allows': int(np.sum(~a & b)), 'both_block': int(np.sum(~a & ~b))}
    return result


def run(path, output=OUT, start_date=None, repetitions=2000):
    rows = load_input(path)
    gate = signals(rows)
    start = choose_start(rows, start_date)
    dates = [r['date'] for r in rows[start:]]
    print(f'Loaded {len(rows)} rows; comparing from {dates[0]}', flush=True)
    sims = {k: simulate(rows, start, positions(gate[k], start)) for k in RULES}
    checks = {'production_current_gate': production_check(rows, gate['SMA80_BAND1'])}
    c = [r['close'] for r in rows]
    checks['current_initial_state_differences'] = int(np.sum(sma_gate(c,80,1,True)[0][start-1:] != gate['SMA80_BAND1'][start-1:]))
    fixed = {k: window_reports(dates, sim) for k, sim in sims.items()}
    sensitivity = {}
    for fee, lag, cash in ((0,1,0), (25,1,0), (10,2,0), (10,1,.03)):
        sensitivity[f'cost{fee}_lag{lag}_cash{cash}'] = {
            k: window_reports(dates, simulate(rows, start, positions(gate[k], start, lag), fee, cash))
            for k in ('SMA80_BAND1','DUAL9_50_80')}
    fresh = {}
    fresh_daily = {}
    for label, threshold in (('2011', '20110101'), ('2021', '20210101')):
        ix = next((i for i,r in enumerate(rows) if r['date'] >= threshold and i>=100), None)
        if ix is None:
            continue
        ds = [r['date'] for r in rows[ix:]]
        ss = {k: simulate(rows, ix, positions(gate[k], ix)) for k in RULES}
        fresh[label] = {k: window_reports(ds,s) for k,s in ss.items()}
        fresh_daily[label] = {'dates': ds, 'rules': {k: {field: v[field].tolist() for field in ('returns','position','turnover')} for k,v in ss.items()}}
    rolling = {k: complete_rolling_windows(dates, sims[k], rows[start-1]['date'], 3)
               for k in ('SMA80_BAND1','DUAL9_50_80')}
    paired = bootstrap_difference(dates, sims['SMA80_BAND1']['returns'], sims['DUAL9_50_80']['returns'], repetitions)
    primary = ('SMA80_BAND1','DUAL9_50_80')
    events = drawdown_events(rows, start, {k:gate[k] for k in primary})
    definition = {
        'SMA80_BAND1': 'close < SMA80*0.99 blocks, close > SMA80*1.01 allows; otherwise retain state',
        'DUAL9_50_80': 'SMA9 > SMA50 AND close > SMA80; strict daily state, no band or hysteresis',
        'SMA80_BAND0': 'close > SMA80; strict, equality blocks, no hysteresis',
        'SMA9_GT50': 'SMA9 > SMA50; strict, equality blocks',
        'SMA100_BAND1': '100-session version of the current +/-1% hysteresis gate',
        'OFF': 'always allow, buy once at evaluation first open and hold',
    }
    report = {
        'design': {'definitions': definition, 'primary_comparison': list(primary), 'cost_bps': 10,
                   'execution': 'prior close signal, next open switch, old holding bears opening gap',
                   'common_warmup': '100 observations before the first execution; all available history used for signals',
                   'unit': 'observed trading sessions; valid historical Saturdays retained',
                   'cash_yield': 0, 'dividends': False, 'terminal_forced_sale': False,
                   'sharpe_volatility_convention': '252 sessions/year for comparability; historical Saturdays change actual annual session counts; CAGR uses calendar days',
                   'bootstrap_annualization': 'actual average sessions per calendar year within each window',
                   '2026_status': 'already observed confirmation, not a pristine holdout',
                   'limits': 'index/cash switching exits holdings on a blocked gate; separate entry-only proxy required; not the historical Kaeppel full futures strategy'},
        'provenance': {'input': str(Path(path).resolve()), 'input_file_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                       'normalized_rows_sha256': hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                       'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       'simulation_engine_sha256': hashlib.sha256((ROOT/'tools/audit_kospi_filter.py').read_bytes()).hexdigest(),
                       'first': rows[0]['date'], 'last': rows[-1]['date'], 'bars': len(rows),
                       'evaluation_start': dates[0], 'evaluation_bars': len(dates),
                       'year_counts': dict(Counter(r['date'][:4] for r in rows))},
        'validation': checks, 'fixed': fixed, 'execution_sensitivity': sensitivity,
        'fresh_start': fresh, 'signal_cross_tab': signal_comparison(rows,gate,start),
        'paired_bootstrap': paired, 'rolling_3year': rolling, 'disjoint_drawdown_events': events,
    }
    daily = {'dates': dates, 'rules': {k: {f:s[f].tolist() for f in ('returns','position','turnover')} for k,s in sims.items()}, 'fresh_start': fresh_daily}
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    (output/'dual_index.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    (output/'dual_daily.json').write_text(json.dumps(daily,ensure_ascii=False,allow_nan=False))
    print(json.dumps({'data':report['provenance'],'pre2026':{k:v['pre2026'] for k,v in fixed.items()}},ensure_ascii=False,indent=2))
    return report


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,default=OUT/'kospi_extended.json')
    ap.add_argument('--output',type=Path,default=OUT)
    ap.add_argument('--start')
    ap.add_argument('--bootstrap',type=int,default=2000)
    args = ap.parse_args()
    run(args.input,args.output,args.start,args.bootstrap)
