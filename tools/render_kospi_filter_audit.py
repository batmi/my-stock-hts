"""Render saved audit JSON only; never import application or trading code.

Units: return/CAGR/MDD/exposure columns are percent; Sharpe is dimensionless;
bootstrap differences are percentage points per year; chart NAV starts at 1.
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
import tempfile

if "MPLCONFIGDIR" not in os.environ:
    _temp_root = Path(__file__).resolve().parents[1] / "tmp"
    _temp_root.mkdir(exist_ok=True)
    _mpl_cache = tempfile.TemporaryDirectory(prefix="kospi-audit-mpl-", dir=_temp_root)
    os.environ["MPLCONFIGDIR"] = _mpl_cache.name

    def _cleanup_mpl_cache():
        _mpl_cache.cleanup()
        try:
            _temp_root.rmdir()  # Keep any unrelated temporary files.
        except OSError:
            pass

    atexit.register(_cleanup_mpl_cache)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "data/audit_runs/kospi_filter_20261025"
COLORS = {"80": "#087f8c", "100": "#df773b", "200": "#8d83ae", "BH": "#475569"}


def number(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def table(headers, rows):
    return '<div class="table-wrap"><table><thead><tr>' + ''.join(
        f'<th>{escape(str(h))}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(c))}</td>' for c in r) + '</tr>' for r in rows
    ) + '</tbody></table></div>'


def metric_row(label, m, year=False):
    # 2026 deliberately uses total period return, never annualized CAGR.
    return [label, number(m["return_pct"] if year else m["cagr_pct"]),
            number(m["mdd_pct"]), number(m["sharpe_zero_rf"], 3),
            number(m.get("exposure_pct"), 1), m.get("one_way_orders", "—")]


def render(folder):
    names = ("index_filter.json", "index_daily_paths.json", "entry_gate.json")
    raw = {n: (folder / n).read_bytes() for n in names}
    report, daily, entry = [json.loads(raw[n]) for n in names]
    fixed = report["fixed"]
    primary = [str(p) for p in report["design"]["primary_periods"]]
    pre = np.array([d < "20260101" for d in daily["dates"]])
    dates = [datetime.strptime(d, "%Y%m%d") for d, keep in zip(daily["dates"], pre) if keep]
    for p in primary:
        nav = float(np.prod(1 + np.asarray(daily["fixed"][p]["returns"])[pre]))
        expected = 1 + fixed[p]["pre2026"]["return_pct"] / 100
        if not np.isclose(nav, expected, rtol=1e-10, atol=1e-10):
            raise ValueError(f"Input artifacts disagree for SMA{p}; rerun after audit completes")
    proxy = {(r["holding_days"], r["filter"]): r for r in entry["runs"] if r["cost_bps"] == 10}
    horizons = sorted({h for h, _ in proxy})
    fonts = {f.name for f in font_manager.fontManager.ttflist}
    for font in ("AppleGothic", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR"):
        if font in fonts:
            plt.rcParams["font.family"] = font
            break
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(3, 1, figsize=(13.5, 13), gridspec_kw={"height_ratios": [1.25, .85, 1]})
    fig.patch.set_facecolor("#f8fafc")
    for p in ("80", "100", "200"):
        nav = np.cumprod(1 + np.asarray(daily["fixed"][p]["returns"])[pre])
        axes[0].plot(dates, nav, label=f"SMA{p}", color=COLORS[p], lw=2)
    nav = np.cumprod(1 + np.asarray(daily["buy_hold"]["returns"])[pre])
    axes[0].plot(dates, nav, label="KOSPI 보유", color=COLORS["BH"], lw=1.8, alpha=.8)
    axes[0].set(title="① 지수 ↔ 현금 전환: 2011–2025 누적 자산", ylabel="초기자산 = 1")
    axes[0].set_xlim(dates[0], dates[-1])
    axes[0].legend(ncol=4, loc="upper left", frameon=False)
    axes[0].grid(alpha=.15)
    grid = report["sensitivity_grid"]
    periods = sorted({x["period"] for x in grid})
    bands = sorted({x["band"] for x in grid})
    lookup = {(x["period"], x["band"]): x["pre2026"]["sharpe_zero_rf"] for x in grid}
    z = np.array([[lookup[p, b] for p in periods] for b in bands])
    im = axes[1].imshow(z, aspect="auto", origin="lower", cmap="YlGnBu")
    axes[1].set_xticks(range(len(periods)), periods, fontsize=8)
    axes[1].set_yticks(range(len(bands)), [f"±{b:g}%" for b in bands])
    axes[1].set(title="② 기간 · 밴드 민감도: 2011–2025 Sharpe (현금금리 0)", xlabel="SMA 거래일 수")
    for p in (80, 100):
        axes[1].scatter(periods.index(p), bands.index(1.0), s=170, facecolors="none", edgecolors="#ef4444", lw=2)
    fig.colorbar(im, ax=axes[1], pad=.015, label="Sharpe")
    x = np.arange(len(horizons))
    for j, (p, window, label, hatch) in enumerate([
        ("80", "pre_2026", "SMA80 · 2011–2025", None),
        ("100", "pre_2026", "SMA100 · 2011–2025", None),
        ("80", "recent_2021_2025", "SMA80 · 2021–2025", "//"),
        ("100", "recent_2021_2025", "SMA100 · 2021–2025", "//"),
    ]):
        vals = [proxy[h, f"SMA{p}"]["metrics"][window]["cagr_pct"] for h in horizons]
        bars = axes[2].bar(x + (j-1.5)*.18, vals, .17, color=COLORS[p], label=label,
                           hatch=hatch, alpha=.65 if hatch else 1, edgecolor="white")
        axes[2].bar_label(bars, fmt="%.1f", padding=3, fontsize=9)
    axes[2].set_xticks(x, [f"{h}거래일 보유" for h in horizons])
    axes[2].set(title="③ 신규진입만 차단하는 별도 모형: 보유기간별 결과", ylabel="연복리 수익률 (%)")
    axes[2].axhline(0, color="#94a3b8", lw=.8)
    axes[2].legend(ncol=2, loc="upper left", frameon=False, fontsize=9)
    axes[2].margins(y=.3)
    fig.suptitle("KOSPI 시장필터 기간 검증", x=.08, ha="left", fontsize=21, weight="bold")
    fig.text(.08, .945, "주 비교 ±1% · 편도 비용 10bp · 전일 종가 판정 → 다음 시가 실행 · 배당 제외", color="#475569")
    fig.text(.08, .018, "①②는 지수 전액 전환, ③은 분할 자금의 예정 진입만 제한합니다. 실제 종목 시스템 수익률이 아닙니다.", fontsize=10, color="#475569")
    fig.subplots_adjust(top=.9, bottom=.07, left=.08, right=.96, hspace=.52)
    fig.savefig(folder / "overview.png", dpi=170, facecolor=fig.get_facecolor())
    plt.close(fig)

    headers = ["전략", "CAGR (%)", "MDD (%)", "Sharpe", "평균 노출 (%)", "편도 주문 수"]
    rows = [metric_row(f"SMA{p}", fixed[p]["pre2026"]) for p in primary]
    rows += [metric_row("KOSPI 보유", report["benchmark"]["buy_hold"]["pre2026"]),
             metric_row("현금", report["benchmark"]["cash"]["pre2026"])]
    ci = report["paired_bootstrap"]["blocks"]["60"]["100"]
    ci_low, ci_high = ci["simultaneous_95"]
    m80, m100 = fixed["80"]["pre2026"], fixed["100"]["pre2026"]
    rank = max(primary, key=lambda p: fixed[p]["pre2026"]["sharpe_zero_rf"])
    rolling80 = [r for r in report['rolling_3year_quarterly_starts']['80'] if r['end'] < '20260101']
    rolling100 = [r for r in report['rolling_3year_quarterly_starts']['100'] if r['end'] < '20260101']
    assert [(r['start'],r['end']) for r in rolling80] == [(r['start'],r['end']) for r in rolling100]
    rolling_wins = sum(b['cagr_pct'] > a['cagr_pct'] for a,b in zip(rolling80,rolling100))
    delayed = report['execution_sensitivity']['cost10_lag2_cash0']
    parts = [f'''<header><p class="eyebrow">읽기 전용 연구 · 2026-10-25</p><h1>KOSPI 시장필터 기간 검증</h1>
    <p class="lead">80일을 유일한 최적값으로 확정할 근거는 부족합니다. 주 비교에서 Sharpe가 가장 높은 기간은 {rank}일이지만, 기간별 차이는 표본과 적용 방식에 따라 달라집니다.</p></header>
    <div class="cards"><div><small>SMA80 · 2011–2025 CAGR</small><strong>{number(m80['cagr_pct'])}%</strong><span>MDD {number(m80['mdd_pct'])}%</span></div>
    <div><small>SMA100 · 2011–2025 CAGR</small><strong>{number(m100['cagr_pct'])}%</strong><span>MDD {number(m100['mdd_pct'])}%</span></div>
    <div><small>100일 − 80일 · 동시 95% 구간</small><strong>{number(ci_low)} ~ {number(ci_high)}</strong><span>연환산 산술평균 수익 차이 (%p), 60일 블록</span></div></div>
    <p class="note">이 구간은 CAGR 차이의 신뢰구간이 아닙니다. 0을 포함한다는 것은 우위가 확인되지 않았다는 뜻이며, 두 전략이 동등하다는 증명은 아닙니다.</p>
    <p>100일은 비교 후보로 남길 만하지만, 이번 결과만으로 80일을 바꿀 근거는 강하지 않습니다. 3년 구간 {len(rolling80)}개 중 100일의 CAGR이 더 높은 구간은 {rolling_wins}개입니다. 이 구간들은 겹치므로 독립 검증 횟수가 아닙니다. 실행을 하루 더 늦추면 CAGR은 80일 {number(delayed['80']['pre2026']['cagr_pct'])}%, 100일 {number(delayed['100']['pre2026']['cagr_pct'])}%로 순서가 바뀝니다. 이 표본은 200일로 교체하는 선택도 지지하지 않습니다.</p>
    <img class="overview" src="overview.png" alt="누적 자산, 기간·밴드 민감도, 신규진입 차단 모형 비교 그래프">
    <h2>1. 먼저 무엇을 검증했는가</h2><p>지수 종가가 SMA 아래 밴드를 이탈하면 차단, 위 밴드를 돌파하면 해제하고 밴드 안에서는 이전 상태를 유지합니다. 전일 종가로 판단해 다음 시가에 거래합니다. 기존 보유분에는 시가 갭을 반영하며, 신규 보유분에는 시가 이후 수익만 반영합니다. 기본 비용은 매수·매도 각각 10bp(0.1%), 현금 이자는 0입니다.</p>
    <p><b>지수 ↔ 현금 전환</b>은 차단 시 보유 지수도 매도합니다. <b>신규진입만 차단하는 모형</b>은 이미 보유한 물량을 예정 보유일까지 유지합니다. 두 실험 모두 개별 종목 선택·ATR 청산·피라미딩·실제 주문체결을 재현한 포트폴리오 백테스트가 아닙니다.</p>
    <p>2010년은 지표 준비에 사용하고 평가 시작은 {report['provenance']['evaluation_start']}입니다. 주 비교는 2011–2025년, ±1% 밴드와 10개 기간입니다. 2026년은 이미 관찰한 확인 표본으로 별도 표시합니다.</p>
    <h2>2. 지수 ↔ 현금 전환: 2011–2025</h2>''', table(headers, rows)]
    parts += ['<h2>3. 과거 5년으로 선택한 다음 해 성과</h2><p>매년 직전 5년의 비용 차감 Sharpe로 기간을 선택합니다. 아래 전략은 모두 2016년 첫 거래일 현금으로 시작하여 2025년 말까지 비교합니다.</p>']
    wfrows = [metric_row("매년 Sharpe로 선택", report["walk_forward"]["sharpe_zero_rf"]["metrics"]["pre2026"])]
    comp = report["walk_forward_same_start_fixed_comparisons"]
    wfrows += [metric_row(f"SMA{p} 고정", comp[p]["pre2026"]) for p in ("80", "100", "200")]
    for key in ("buy_hold", "BH"):
        if key in comp:
            wfrows.append(metric_row("KOSPI 보유", comp[key]["pre2026"]))
    parts.append(table(headers, wfrows))
    parts.append('<p>이 비교에서는 매년 과거 최적 기간을 다시 선택해도 80일·100일 고정 사용보다 성과가 높지 않습니다. 과거 최적값을 계속 갱신하는 것이 개선을 보장하지는 않습니다.</p>')
    choices = report["walk_forward"]["sharpe_zero_rf"]["choices"]
    parts.append('<details><summary>연도별 선택 · 보조 선택기준</summary>' + table(
        ["평가연도", "훈련 시작", "훈련 끝", "선택 SMA", "훈련 Sharpe"],
        [[c['test_year'], c['training_start'], c['training_end'], c['chosen_ma'], number(c['train_score'],3)] for c in choices]) + table(headers,
        [metric_row(obj, report['walk_forward'][obj]['metrics']['pre2026']) for obj in ('calmar','cagr_pct')]) + '</details>')
    parts += ['<h2>4. 신규진입만 차단하는 모형</h2><p>자금을 보유기간 H와 같은 수의 독립 계정으로 나누어 하루에 하나씩 진입 기회를 줍니다. 차단된 계정은 다음 예정 주기까지 현금을 유지합니다. 이미 보유한 계정은 H거래일 후 청산합니다. OFF도 같은 예정 회전비용을 부담합니다. 보유기간은 20·60·120거래일이며 실제 ATR 청산을 대체하는 가정입니다.</p>']
    prows=[]
    for h in horizons:
        for p in ('OFF','SMA80','SMA100'):
            a=proxy[h,p]['metrics']['pre_2026'];b=proxy[h,p]['metrics']['recent_2021_2025']
            prows.append([h,p,number(a['cagr_pct']),number(a['mdd_pct']),number(a['mean_exposure_pct'],1),number(b['cagr_pct']),number(b['mdd_pct'])])
    parts.append(table(['보유 거래일','필터','2011–25 CAGR (%)','MDD (%)','노출 (%)','2021–25 CAGR (%)','MDD (%)'],prows))
    parts.append('<p class="note">모형 간 CAGR을 직접 비교하여 우열을 정하지 마십시오. 노출과 회전율·비용 구조가 다릅니다. 겹치는 보유구간을 독립 표본으로 계산하지 않았습니다.</p>')
    parts += ['<h2>5. 2026년: 이미 본 확인 표본</h2><p>2026년 첫 거래일~10월 23일의 <b>기간 수익률</b>입니다. 1년 미만 수익을 연율로 확대하지 않습니다. 기존 경로의 연속 운용 구간입니다.</p>',
              table(['전략','기간 수익률 (%)','MDD (%)','Sharpe','노출 (%)','편도 주문 수'],
                    [metric_row(f'SMA{p}',fixed[p]['observed_2026'],True) for p in ('80','100','200')] +
                    [metric_row('KOSPI 보유',report['benchmark']['buy_hold']['observed_2026'],True)])]
    sensrows=[]
    for name,s in report['execution_sensitivity'].items():
        for p in ('80','100'):
            sensrows.append([name,f'SMA{p}',number(s[p]['pre2026']['cagr_pct']),number(s[p]['pre2026']['mdd_pct']),number(s[p]['pre2026']['sharpe_zero_rf'],3)])
    parts.append('<details><summary>비용 · 하루 추가 지연 · 현금금리 민감도</summary><p>비용은 편도 bp, lag는 판단 이후 거래일까지의 거래일 수, cash는 연 현금금리입니다. 비교 구간은 2011–2025년입니다.</p>'+table(['가정','기간','CAGR (%)','MDD (%)','Sharpe'],sensrows)+'</details>')
    matched=report['matched_exposure_reference']
    parts.append('<details><summary>평균 노출을 맞춘 참고값</summary><p>각 필터의 2011–2025 평균 노출만큼 매일 지수 수익률을 반영한 사후 비교입니다. 낮은 노출 자체의 영향을 살펴보는 참고값이며, 사전에 선택할 수 있었던 전략으로 해석하지 않습니다. 최초 지수 매수비용의 비례분이 포함되고 추가 리밸런싱 비용은 제외됩니다.</p>'+table(
        ['필터 노출 참고','지수 비중 (%)','CAGR (%)','MDD (%)'],
        [[f'SMA{p}와 동일 노출',number(matched[p]['fraction']*100,1),number(matched[p]['pre2026']['cagr_pct']),number(matched[p]['pre2026']['mdd_pct'])] for p in ('80','100','200')])+'</details>')
    cirows=[]
    for block, results in report['paired_bootstrap']['blocks'].items():
        for p in primary:
            if p=='80':continue
            v=results[p]
            cirows.append([block,f'{p}일 − 80일',number(v['mean_difference_pp_per_year']), ' ~ '.join(number(x) for x in v['simultaneous_95'])])
    parts.append('<details><summary>기간 비교의 불확실성 · 동시 신뢰구간</summary><p>동일 날짜 블록으로 모든 전략을 함께 재표집했습니다. 단위는 연환산 산술평균 수익 차이(%p)입니다. 주 비교 기간들에 대한 동시구간이며, 모든 과거 연구 선택이나 밴드 탐색까지 보정하지는 않습니다.</p>'+table(['블록 거래일','비교','평균 차이 (%p)','동시 95% 구간 (%p)'],cirows)+'</details>')
    parts.append('<details><summary>근접 기간 · 밴드별 전체 결과</summary>'+table(['SMA','밴드 (%)','2011–25 CAGR (%)','MDD (%)','Sharpe'],[[g['period'],g['band'],number(g['pre2026']['cagr_pct']),number(g['pre2026']['mdd_pct']),number(g['pre2026']['sharpe_zero_rf'],3)] for g in grid])+'</details>')
    parts.append('<h2>6. 데이터와 해석의 한계</h2><p>현재 저장된 KRX 지수 OHLC를 사용하며 배당·실제 현금금리·ETF 추적오차를 포함하지 않습니다. 2008년 금융위기는 이 표본에 없습니다. 반복 사용한 단일 시장의 역사이며, 워크포워드도 새로운 시장에서의 독립 검증과 같지 않습니다. 최근 260봉만 사용하는 운영 환경과 전체 이력의 상태 차이는 원자료에 별도로 기록했습니다.</p>')
    parts.append('<details><summary>운영 데이터 길이 · 초기 상태 민감도</summary><p>밴드 안에서는 이전 상태를 유지하므로, 짧은 이력으로 매번 재계산하면 전체 이력 결과와 다를 수 있습니다. 아래는 전체 평가구간의 신호 차이 수이며 수익 차이는 아닙니다.</p>'+table(
        ['SMA','최근 260봉과 전체 이력 차이(일)','초기 차단/허용 상태 차이(일)'],
        [[p,report['last_260_bars_vs_full_history'][p]['different_signal_days'],report['validation']['initial_state_differences'][p]] for p in primary])+'</details>')
    validation = folder / 'source_validation.json'
    if validation.exists():
        parts.append('<details><summary>외부 원자료 대조 결과</summary><pre>'+escape(validation.read_text())+'</pre></details>')
    else:
        parts.append('<p class="note" id="source-validation">외부 원자료 대조 결과는 별도 검토 항목입니다. 본 보고서는 저장된 데이터의 내용과 계산을 설명하며, 원자료의 외부 일치 여부를 보증하지 않습니다.</p>')
    provenance = report['provenance']
    manifest = {'inputs':{name:hashlib.sha256(content).hexdigest() for name,content in raw.items()},
                'renderer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'nav_reconciliation':'all primary pre2026 paths match index_filter.json within 1e-10'}
    if validation.exists():
        manifest['inputs'][validation.name] = hashlib.sha256(validation.read_bytes()).hexdigest()
    parts.append('<details><summary>재현 정보 · 원자료 파일</summary>'+table(['항목','값'],[
        ['데이터 범위',f"{provenance['first']} ~ {provenance['last']}"],['봉 수',provenance['bars']],
        ['입력 지수 SHA256',provenance['data_sha256']],['운영 함수 일치 검증',json.dumps(report['validation']['production_parity'],ensure_ascii=False)]])+
        '<p>'+ ' · '.join(f'<a href="{n}">{n}</a>' for n in names)+'</p><pre>'+escape(json.dumps(manifest,ensure_ascii=False,indent=2))+'</pre></details>')
    css='''body{margin:0;background:#f3f6fa;color:#172b3a;font:16px/1.75 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}main{max-width:1120px;margin:38px auto;padding:42px;background:white;border-radius:18px}h1{font-size:36px;line-height:1.25;margin:8px 0 22px}h2{font-size:23px;margin:40px 0 10px}.eyebrow{font-size:13px;color:#087f8c;font-weight:700;letter-spacing:.08em}.lead{font-size:20px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:28px 0}.cards>div{padding:22px;border-radius:12px;background:#edf6f7}.cards small,.cards span{display:block;font-size:12px}.cards strong{display:block;font-size:27px;margin:8px 0;color:#087f8c}.note{padding:14px 18px;background:#fff7e7;border-left:4px solid #e9a43b;font-size:14px}.overview{width:100%;height:auto;margin-top:20px}.table-wrap{overflow:auto;margin:18px 0}table{border-collapse:collapse;width:100%;font-size:14px;white-space:nowrap}th{background:#eaf0f6;text-align:right}th:first-child,td:first-child{text-align:left}td,th{padding:10px 12px;border-bottom:1px solid #e4eaf0;text-align:right}tbody tr:hover{background:#f5f9fc}details{border:1px solid #dbe3eb;border-radius:9px;margin:18px 0;padding:15px 18px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#087f8c}@media(max-width:700px){main{margin:0;padding:23px;border-radius:0}h1{font-size:28px}.cards{grid-template-columns:1fr}.cards strong{font-size:24px}}'''
    html='<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>KOSPI 시장필터 기간 검증</title><style>'+css+'</style><main>'+''.join(parts)+'</main></html>'
    (folder/'report.html').write_text(html,encoding='utf-8')
    (folder/'render_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'html':str(folder/'report.html'),'png':str(folder/'overview.png'),'checks':manifest['nav_reconciliation']},ensure_ascii=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=DEFAULT)
    render(parser.parse_args().input)
