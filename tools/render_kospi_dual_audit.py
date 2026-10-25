"""Render saved dual-gate research only, without production imports or network.

Units: returns/CAGR/MDD/exposure in percent; differences in percentage points;
Sharpe dimensionless; NAV is an initial-capital multiple. 2026 uses total return.
Matplotlib caches are private workspace tmp children and removed on exit.
"""
from __future__ import annotations

import argparse
import atexit
from datetime import datetime
import hashlib
from html import escape
import json
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tmp"
TMP.mkdir(exist_ok=True)
CACHE = Path(tempfile.mkdtemp(prefix="kospi-dual-render-", dir=TMP))
os.environ["MPLCONFIGDIR"] = str(CACHE / "matplotlib")
os.environ["XDG_CACHE_HOME"] = str(CACHE / "xdg")


def cleanup():
    shutil.rmtree(CACHE, ignore_errors=True)
    try:
        TMP.rmdir()  # Remove the parent only when no other task uses it.
    except OSError:
        pass


atexit.register(cleanup)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np

DEFAULT = ROOT / "data/audit_runs/kospi_dual_filter_20261025"
BASE, NEW = "SMA80_BAND1", "DUAL9_50_80"
LABEL = {BASE: "기존 SMA80 ±1%", NEW: "새 9/50 + 종가/80 AND", "OFF": "필터 OFF",
         "SMA80_BAND0": "종가 > SMA80", "SMA9_GT50": "SMA9 > SMA50", "SMA100_BAND1": "SMA100 ±1%"}
COLOR = {BASE: "#087f8c", NEW: "#dc763b", "OFF": "#64748b"}
SOURCE_1997 = "https://traders.com/Documentation/FEEDbk_docs/1997/05/Abstracts0597/0597Kaeppel.html"
SOURCE_CODE = "https://traders.com/documentation/FEEDbk_docs/1997/06/0697tradetips.html"


def n(x, digits=2):
    return "—" if x is None else f"{x:,.{digits}f}"


def date(d):
    return datetime.strptime(str(d).replace("-", ""), "%Y%m%d").strftime("%Y-%m-%d")


def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{escape(str(x))}</th>' for x in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(x))}</td>' for x in r) + '</tr>' for r in rows) + '</tbody></table></div>'


def row(label, m, total=False):
    if not m:
        return [label] + ["자료 없음"] * 5
    return [label, n(m['return_pct'] if total else m['cagr_pct']), n(m['mdd_pct']),
            n(m['sharpe_zero_rf'], 3), n(m['exposure_pct'], 1), m['one_way_orders']]


def proxy_map(data):
    return {(r['holding_days'], r['filter']): r for r in data['runs'] if r['cost_bps'] == 10}


def render(folder):
    files = ['dual_index.json', 'dual_daily.json', 'dual_entry.json']
    raw = {name: (folder / name).read_bytes() for name in files}
    r, daily, entry = [json.loads(raw[name]) for name in files]
    source_path = folder / 'extended_source.json'
    source = json.loads(source_path.read_bytes()) if source_path.exists() else {}
    if source:
        raw[source_path.name] = source_path.read_bytes()
    fixed = r['fixed']
    ds = np.asarray(daily['dates'])
    pre = ds < '20260101'
    # Refuse a report assembled while upstream artifacts are being replaced.
    for key, path in daily['rules'].items():
        expected = 1 + fixed[key]['pre2026']['return_pct'] / 100
        actual = np.prod(1 + np.asarray(path['returns'])[pre])
        if not np.isclose(actual, expected, rtol=1e-10, atol=1e-10):
            raise ValueError(f'Inconsistent daily/index artifacts: {key}')
    if entry['data']['evaluation_start'].replace('-', '') != str(ds[0]).replace('-', ''):
        raise ValueError('Entry and switching models do not share the common start')
    if entry['data']['canonical_sha256'] != r['provenance']['normalized_rows_sha256']:
        raise ValueError('Entry and switching models used different normalized OHLC inputs')
    long_proxy = proxy_map(entry)
    restarted = entry.get('restart_2011', {})
    if restarted.get('status') != 'available':
        raise ValueError('2011 equal-cash restart required for this report')
    fresh_proxy = proxy_map(restarted)
    horizons = sorted({h for h, key in long_proxy})
    fonts = {f.name for f in font_manager.fontManager.ttflist}
    for font in ('NanumGothic', 'Apple SD Gothic Neo', 'AppleGothic', 'Noto Sans CJK KR'):
        if font in fonts:
            plt.rcParams['font.family'] = font
            break
    plt.rcParams.update({'axes.unicode_minus': False, 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False})
    fig, axes = plt.subplots(3, 1, figsize=(13.5, 13), gridspec_kw={'height_ratios': [1.1, 1.1, .85]})
    fig.patch.set_facecolor('#f7f9fc')
    times = [datetime.strptime(d, '%Y%m%d') for d in ds[pre]]
    for key in (BASE, NEW, 'OFF'):
        nav = np.cumprod(1 + np.asarray(daily['rules'][key]['returns'])[pre])
        axes[0].plot(times, nav, lw=1.8, color=COLOR[key], label=LABEL[key])
    axes[0].set_yscale('log')
    axes[0].set(title=f'① 최장 연속 운용: {times[0]:%Y-%m-%d}–2025년 말', ylabel='초기 자산 = 1 · 로그 눈금')
    axes[0].set_xlim(times[0], times[-1]); axes[0].legend(ncol=3, frameon=False, loc='upper left')
    fresh_daily = daily['fresh_start']['2011']
    fmask = np.asarray(fresh_daily['dates']) < '20260101'
    ftimes = [datetime.strptime(d, '%Y%m%d') for d in np.asarray(fresh_daily['dates'])[fmask]]
    for key in (BASE, NEW, 'OFF'):
        nav = np.cumprod(1 + np.asarray(fresh_daily['rules'][key]['returns'])[fmask])
        expected = 1 + r['fresh_start']['2011'][key]['2011_2025']['return_pct']/100
        if not np.isclose(nav[-1], expected, rtol=1e-10, atol=1e-10):
            raise ValueError(f'Fresh-start artifact mismatch: {key}')
        axes[1].plot(ftimes, nav, lw=2, color=COLOR[key], label=LABEL[key])
    axes[1].set(title='② 2011년 같은 현금으로 새로 시작: 지수 ↔ 현금 전환', ylabel='초기 자산 = 1')
    axes[1].set_xlim(ftimes[0], ftimes[-1]); axes[1].legend(ncol=3, frameon=False, loc='upper left')
    x = np.arange(len(horizons))
    for shift, data, window, label, color in [(-.18, long_proxy, 'pre2026', '최장 연속 운용 → 2025년 말', '#8d83ae'),
                                             (.18, fresh_proxy, '2011_2025', '2011년 동일 현금 새 시작', '#dc763b')]:
        diff = [data[h,NEW]['metrics'][window]['cagr_pct'] - data[h,BASE]['metrics'][window]['cagr_pct'] for h in horizons]
        bars = axes[2].bar(x+shift, diff, .34, color=color, label=label)
        axes[2].bar_label(bars, fmt='%+.2f', padding=4, fontsize=10)
    axes[2].set_xticks(x, [f'{h}거래일 보유' for h in horizons])
    axes[2].set(title='③ 신규진입만 차단하는 모형: 새 조건 - 기존 조건', ylabel='CAGR 차이 (%p)')
    axes[2].axhline(0, color='#334155', lw=.9)
    axes[2].legend(frameon=False, loc='upper left', ncol=2)
    axes[2].margins(y=.35)
    for ax in axes[:2]: ax.grid(alpha=.16)
    fig.suptitle('KOSPI 시장필터: SMA80 ±1% vs 9/50·80 AND', x=.08, ha='left', fontsize=20, weight='bold')
    fig.text(.08,.944,'전일 종가 판단 → 다음 시가 · 매수/매도 각 10bp · 배당·현금이자 제외 · 2026년은 별도 확인',color='#475569')
    fig.text(.08,.018,'①②는 보유분도 청산하는 지수 모형, ③은 예정 신규진입만 차단하는 모형입니다. 실제 종목 시스템 성과가 아닙니다.',fontsize=10,color='#475569')
    fig.subplots_adjust(top=.9,bottom=.07,left=.08,right=.96,hspace=.48)
    fig.savefig(folder/'overview.png',dpi=170,facecolor=fig.get_facecolor()); plt.close(fig)

    old, new = fixed[BASE]['pre2026'], fixed[NEW]['pre2026']
    fresh_old = r['fresh_start']['2011'][BASE]['2011_2025']
    fresh_new = r['fresh_start']['2011'][NEW]['2011_2025']
    delta = new['cagr_pct']-old['cagr_pct']
    bs = r['paired_bootstrap']['windows']['pre2026']
    ci = bs['blocks']['60']['paired_percentile_95']
    uncertain = ci[0] <= 0 <= ci[1]
    conclusion = '장기 평균 수익 차이의 불확실성이 남습니다.' if uncertain else '장기 평균 수익 차이가 60일 블록의 95% 구간에서 0과 구분됩니다.'
    heads=['조건','CAGR (%)','MDD (%)','Sharpe','평균 노출 (%)','편도 주문 수']
    parts=[f'''<header><p class="eyebrow">읽기 전용 비교 연구 · 2026-10-25</p><h1>기존 80일 필터와<br>9/50·80일 AND 조건 비교</h1>
    <p class="lead">최장 연속 표본에서 새 조건의 CAGR은 기존 대비 {delta:+.2f}%p입니다. {conclusion} 적용 방식과 시작 시점을 나누어 판단해야 합니다.</p></header>
    <div class="cards"><div><small>기존 SMA80 ±1% · 최장~2025</small><strong>{n(old['cagr_pct'])}%</strong><span>CAGR · MDD {n(old['mdd_pct'])}%</span></div>
    <div><small>새 AND 조건 · 최장~2025</small><strong>{n(new['cagr_pct'])}%</strong><span>CAGR · MDD {n(new['mdd_pct'])}%</span></div>
    <div><small>새 − 기존 · 평균 수익 차이</small><strong>{n(ci[0])} ~ {n(ci[1])}</strong><span>연환산 %p · paired 95% 구간 · 60일 블록</span></div></div>
    <p class="note">신뢰구간은 연환산 산술평균 수익 차이에 대한 값으로 CAGR 차이가 아닙니다. 0 포함은 우위를 확인하지 못했다는 뜻입니다. 0 미포함도 모든 연구 선택에 대한 보정이나 미래 수익의 보장은 아닙니다.</p>
    <p>이전 검증과 같은 2011년 현금 시작에서는 기존 CAGR {n(fresh_old['cagr_pct'])}%, 새 조건 {n(fresh_new['cagr_pct'])}%입니다. 장기 경로에서 2011년 이후만 잘라낸 수치와는 연초 보유분의 시가 갭·초기 비용 때문에 다릅니다. 신규진입 모형에서도 보유기간별 결과를 따로 보아야 합니다.</p>
    <img src="overview.png" alt="최장기간 누적자산, 2011년 새 시작 비교, 신규진입 모형의 CAGR 차이" class="overview">
    <h2>1. 비교 조건과 적용 범위</h2>''', table(['항목','기존','새 조건'],[
        ['허용·차단','SMA80 아래 1% 이탈 시 차단, 위 1% 돌파 시 해제','SMA9 > SMA50 AND 종가 > SMA80일 때만 허용'],
        ['밴드 안·등호','이전 상태 유지','밴드 없음; 등호는 차단'],
        ['판정 시점','전일 확정 종가','전일 확정 종가'],['실행 시점','다음 거래일 시가','다음 거래일 시가']]),
        '<p>주 비교는 두 조건만이며 기간 최적화를 수행하지 않았습니다. 단일 조건과 SMA100은 차이의 원인을 살펴보는 보조 비교입니다. 모든 조건에 공통 준비기간 100개 관측치를 적용했습니다. MA의 일수는 실제 관측 거래일 수입니다.</p>',
        '<p><b>지수 전환 모형</b>은 차단 시 기존 보유 지수도 청산합니다. <b>신규진입 모형</b>은 H개 분할 계정 중 그날 진입 예정인 계정만 제한하며 보유분은 H거래일 후 예정 청산합니다. 실제 프로젝트의 종목 선정·ATR 청산·피라미딩은 두 모형에 포함하지 않았습니다.</p>',
        '<h2>2. 최장 연속 운용과 2011년 새 시작</h2>',
        f'<p>확보한 최장 평가: <b>{date(r["provenance"]["evaluation_start"])}~{date(r["provenance"]["last"])}</b>. 2026년 포함 전체 결과와 2025년까지의 주 비교를 구분합니다. 연속 운용과 2011년 새 시작은 시작 자본의 이력이 다릅니다.</p>',
        '<h3>최장 전체기간 · 2026년 확인 표본 포함</h3>',table(
            ['조건','누적 수익률 (%)','전체기간 CAGR (%)','MDD (%)','평균 노출 (%)'],
            [[LABEL[k],n(fixed[k]['all']['return_pct']),n(fixed[k]['all']['cagr_pct']),n(fixed[k]['all']['mdd_pct']),n(fixed[k]['all']['exposure_pct'],1)] for k in (BASE,NEW,'OFF')]),
        '<h3>최장 연속 운용 → 2025년 말</h3>',table(heads,[row(LABEL[k],fixed[k]['pre2026']) for k in (BASE,NEW,'OFF')]),
        '<h3>2011년 동일 현금으로 새로 시작 → 2025년 말</h3>',table(heads,[row(LABEL[k],r['fresh_start']['2011'][k]['2011_2025']) for k in (BASE,NEW,'OFF')])]
    parts += ['<h2>3. 신규진입만 차단: 자본 이력의 영향을 분리</h2><p>장기간 독립 복리로 운용한 분할 계정은 크기가 서로 달라집니다. 따라서 과거부터 운용하던 계정의 2011년 이후 구간을 자르는 것과, 2011년에 같은 현금으로 시작하는 것은 다릅니다. 아래는 각각의 초기 조건 안에서 비교한 결과입니다. OFF도 같은 예정 회전과 비용을 부담합니다.</p>']
    for title,data,window in [('최장 연속 운용 → 2025년 말',long_proxy,'pre2026'),('2011년 동일 현금 새 시작 → 2025년 말',fresh_proxy,'2011_2025')]:
        values=[]
        for h in horizons:
            for key in (BASE,NEW,'OFF'):
                m=data[h,key]['metrics'][window]
                values.append([h,LABEL[key],n(m['cagr_pct']),n(m['mdd_pct']),n(m['mean_exposure_pct'],1)])
        parts += [f'<h3>{title}</h3>',table(['보유 거래일','조건','CAGR (%)','MDD (%)','평균 노출 (%)'],values)]
    drift=[]
    for h in horizons:
        for key in (BASE,NEW):
            a=long_proxy[h,key]['metrics']['2011_2025']; b=fresh_proxy[h,key]['metrics']['2011_2025']
            drift.append([h,LABEL[key],n(a['cagr_pct']),n(b['cagr_pct']),n(a['mean_exposure_pct'],1),n(b['mean_exposure_pct'],1)])
    parts.append('<details><summary>같은 2011–2025 기간: 물려받은 계정과 새 계정의 차이</summary>'+table(
        ['보유 거래일','조건','연속 운용 CAGR (%)','새 시작 CAGR (%)','연속 노출 (%)','새 시작 노출 (%)'],drift)+'</details>')
    whole_proxy=[]
    for h in horizons:
        for key in (BASE,NEW,'OFF'):
            m=long_proxy[h,key]['metrics']['full']
            whole_proxy.append([h,LABEL[key],n(m['total_return_pct']),n(m['cagr_pct']),n(m['mdd_pct']),n(m['mean_exposure_pct'],1)])
    parts.append('<details><summary>신규진입 모형의 최장 전체기간 · 2026년 포함</summary>'+table(
        ['보유 거래일','조건','누적 수익률 (%)','전체기간 CAGR (%)','MDD (%)','평균 노출 (%)'],whole_proxy)+'</details>')
    parts += ['<h2>4. 시기별 지수 전환 결과</h2><p>아래는 최장 연속 경로의 부분구간이며 새로 시작한 결과가 아닙니다. 2026년은 이미 관찰한 확인 표본이고, 1년 미만 결과는 기간 수익률로 표시합니다.</p>']
    window_names={'pre2010':'2010년 이전','2011_2025':'2011–2025','2021_2025':'2021–2025','asian_crisis_1997_1998':'1997–1998','gfc_2007_2009':'2007–2009','covid_2020':'2020','bear_2022':'2022'}
    subrows=[]
    for key,label in window_names.items():
        for rule in (BASE,NEW,'OFF'):
            m=fixed[rule].get(key)
            if m: subrows.append([label,LABEL[rule],n(m['return_pct']),n(m['mdd_pct']),n(m['exposure_pct'],1)])
    parts += [table(['구간','조건','기간 수익률 (%)','MDD (%)','평균 노출 (%)'],subrows),'<h3>2026년 첫 거래일~마지막 관측일</h3>',
              table(['조건','기간 수익률 (%)','MDD (%)','Sharpe','평균 노출 (%)','편도 주문 수'],[row(LABEL[k],fixed[k]['observed_2026'],True) for k in (BASE,NEW,'OFF')])]
    controls=[row(LABEL[k],fixed[k]['pre2026']) for k in fixed]
    parts.append('<details><summary>보조 조건: 밴드 제거·9/50 단독·SMA100</summary>'+table(heads,controls)+'</details>')
    sensitivity=[]
    for name,records in r['execution_sensitivity'].items():
        for key in (BASE,NEW):
            m=records[key]['pre2026']; sensitivity.append([name,LABEL[key],n(m['cagr_pct']),n(m['mdd_pct']),n(m['sharpe_zero_rf'],3)])
    parts.append('<details><summary>비용·추가 체결 지연·현금이자 민감도</summary><p>cost는 편도 bp, lag는 판단 후 거래일까지의 거래일 수, cash는 연 현금금리입니다. 모두 최장 연속 표본의 2025년 말까지 비교입니다.</p>'+table(['가정','조건','CAGR (%)','MDD (%)','Sharpe'],sensitivity)+'</details>')
    intervals=[]
    for window,obj in r['paired_bootstrap']['windows'].items():
        for block,values in obj['blocks'].items():
            intervals.append([window,block,n(obj['mean_difference_pp_per_year']), ' ~ '.join(n(v) for v in values['paired_percentile_95'])])
    parts.append('<details><summary>Paired block bootstrap 전체 구간</summary><p>새 조건 − 기존 조건의 연환산 산술평균 수익 차이(%p)입니다. 같은 날짜 블록을 재표집하며, 당시 실제 연평균 관측 수로 연환산합니다. CAGR·MDD 차이의 신뢰구간이 아닙니다.</p>'+table(['표본','블록 거래일','평균 차이 (%p)','95% 구간 (%p)'],intervals)+'</details>')
    cross=r['signal_cross_tab']
    pi=cross.get('pi_observed_period')
    if pi:
        parts.append(f'<p class="note">라즈베리파이 운영 구간의 전일 종가 판정: {pi["sessions"]}거래일 중 두 조건 모두 차단한 날은 {pi["both_block"]}일, 기존만 허용한 날은 {pi["current_only_allows"]}일, 새 조건만 허용한 날은 {pi["dual_only_allows"]}일입니다. 이는 신규진입 허용 판정 비교입니다.</p>')
    parts.append('<details><summary>두 필터의 실제 허용일 겹침</summary><p>±1% 밴드를 없애므로 새 조건이 기존 조건의 단순한 부분집합이라고 가정할 수 없습니다.</p>'+table(
        ['구간','둘 다 허용','기존만 허용','새 조건만 허용','둘 다 차단'],[[k,v['both_allow'],v['current_only_allows'],v['dual_only_allows'],v['both_block']] for k,v in cross.items()])+'</details>')
    parts += [f'<h2>5. 1997년 문헌과 이번 실험의 관계</h2><p><a href="{SOURCE_1997}">Jay Kaeppel의 1997년 원인터뷰</a>는 9·50일 SMA와 80일 SMA 확인 조건을 제시하며 선물시장 분산, 매수·매도 포지션을 포함합니다. <a href="{SOURCE_CODE}">같은 출판사의 후속 구현</a>에는 교차 발생일에만 신호를 내고 당일 종가에 거래하는 예시도 있습니다. 이번 실험은 사용자가 지정한 매일 AND 판정의 매수 전용 시장필터 개작이며 원래 선물 시스템의 완전 재현이 아닙니다.</p>',
              '<h2>6. 원자료와 검증 범위</h2>']
    if source:
        period=source.get('period',{}); quality=source.get('quality',{}); overlap=source.get('overlap',{})
        parts.append(table(['항목','확인 내용'],[
            ['원자료 범위',f"{period.get('first',r['provenance']['first'])} ~ {period.get('last',r['provenance']['last'])}"],
            ['관측 수',period.get('bars',r['provenance']['bars'])],['과거 확장 관측 수',period.get('extension_rows','미기록')],
            ['공개 캐시와 공식 데이터 겹침',overlap.get('count','미기록')],['겹침 중 OHLC 불일치 일수',overlap.get('mismatch_days','미기록')],
            ['과거 토요일 관측 수',quality.get('saturday_count','미기록')],['OHLC 결측 수',quality.get('missing_ohlc_count','미기록')]]))
        if period.get('extension_rows')==0:
            parts.append('<p class="note">현재 원자료에는 2010년 이전 확장 관측이 없습니다. 1997·2008년 위기를 포함한 검증으로 해석하지 마십시오.</p>')
        parts.append('<p>여기서 최장은 이번에 확보한 연속적이고 완전한 OHLC 자료의 범위입니다. 과거 확장 구간은 <a href="https://github.com/FinanceData/fdr_krx_data_cache">공개 FinanceData 캐시</a>, 이후 구간은 로컬 KRX OpenAPI가 출처입니다. 겹치는 날짜에는 공식 로컬 자료를 우선합니다. 같은 KRX 원천의 서로 다른 배포 경로 비교는 독립적인 가격 측정 검증과 같지 않습니다. 2010년 이전 구간은 별도 직접 원천과 완전히 교차 검증한 자료가 아닙니다.</p>')
        parts.append('<details><summary>외부 원자료 대조·품질 기록 전체</summary><pre>'+escape(json.dumps(source,ensure_ascii=False,indent=2))+'</pre></details>')
    parts.append('<p>가격지수라 배당 재투자·ETF 추적비용을 포함하지 않았습니다. 역사적 토요일 거래는 관측치로 유지하며 CAGR은 달력 시간, 표의 Sharpe는 비교를 위해 연 252거래일을 사용합니다. 고정 보유기간 모형의 겹치는 계정을 독립 표본으로 보지 않았습니다. 이 결과만으로 실제 종목 포트폴리오의 필터 교체 효과를 확정할 수 없습니다.</p>')
    manifest={'inputs':{name:hashlib.sha256(value).hexdigest() for name,value in raw.items()},
              'renderer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'validation':'All continuous pre2026 NAVs and 2011 fresh primary NAVs reconcile at 1e-10; common model start and normalized OHLC SHA256 match'}
    parts.append('<details><summary>재현 정보와 원결과</summary><p>'+ ' · '.join(f'<a href="{name}">{name}</a>' for name in files)+
                 '</p><pre>'+escape(json.dumps(manifest,ensure_ascii=False,indent=2))+'</pre></details>')
    css='''body{margin:0;background:#f1f5f9;color:#172b3a;font:16px/1.75 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}main{max-width:1120px;margin:38px auto;padding:42px;background:white;border-radius:18px}h1{font-size:35px;line-height:1.3;margin:8px 0 22px}h2{font-size:23px;margin:38px 0 10px}h3{font-size:18px;margin-top:27px}.eyebrow{font-size:13px;color:#087f8c;font-weight:700}.lead{font-size:20px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:28px 0}.cards>div{padding:22px;background:#edf6f7;border-radius:12px}.cards small,.cards span{display:block;font-size:12px}.cards strong{display:block;font-size:27px;margin:8px 0;color:#087f8c}.note{padding:14px 18px;background:#fff7e7;border-left:4px solid #e9a43b;font-size:14px}.overview{width:100%;height:auto}.scroll{overflow:auto;margin:18px 0}table{border-collapse:collapse;width:100%;font-size:14px;white-space:nowrap}td,th{padding:10px 12px;border-bottom:1px solid #e4eaf0;text-align:right}th{background:#eaf0f6}td:first-child,th:first-child{text-align:left}tbody tr:hover{background:#f5f9fc}details{border:1px solid #dbe3eb;border-radius:9px;margin:18px 0;padding:15px 18px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#087f8c}@media(max-width:700px){main{margin:0;padding:23px;border-radius:0}h1{font-size:27px}.cards{grid-template-columns:1fr}}'''
    html='<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>KOSPI 이중 조건 시장필터 비교</title><style>'+css+'</style><main>'+''.join(parts)+'</main></html>'
    (folder/'report.html').write_text(html,encoding='utf-8')
    (folder/'render_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'html':str(folder/'report.html'),'png':str(folder/'overview.png'),'validation':manifest['validation']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=DEFAULT)
    render(parser.parse_args().input)
