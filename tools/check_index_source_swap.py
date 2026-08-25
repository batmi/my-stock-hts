#!/usr/bin/env python3
"""지수 원천을 바꾸면 **매수 게이트 판정이 바뀌는가**를 재는 도구.

[왜 필요한가 · 2026-08-25]
 2026-08-25 부터 국내 지수 일봉의 뼈대가 KRX 확정 봉으로 바뀌었다(analysis._merge_index_history).
 종가는 FDR 과 399/399 일치를 확인했지만, 시장 필터는 SMA80 의 ±1% **밴드**라 소수점 차이가
 경계에서 판정을 뒤집을 수 있다. 그리고 이 원천은 KRX_ID/KRX_PW 유무로 켜지고 꺼진다 —
 즉 **자격증명이 없는 프로세스와 있는 프로세스가 서로 다른 게이트를 볼 수 있다.**

 한 가지가 더 있다. 실매매 게이트(trader._update_market_indices_status)는
 analysis.get_domestic_index_data 를 쓰고, 백테스트 게이트(backtest.prepare_market_filter)는
 yfinance 의 ^KS11/^KQ11 을 쓴다. 원래부터 다른 소스였고 KRX 도입이 그 간격을 넓혔다.
 백테스트로 정한 다이얼이 실매매에 옮겨가려면 두 게이트가 같은 날을 차단해야 한다.

 그래서 세 팔을 같은 자로 잰다:
   ① KRX 켜짐   — 자격증명이 있는 프로세스가 보는 값(현재 운영 목표 상태)
   ② KRX 꺼짐   — 자격증명 없는 프로세스가 보는 값(종전 경로)
   ③ yfinance   — 백테스트가 보는 값

[실행]  python3 tools/check_index_source_swap.py [--days 800]
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config                                  # noqa: E402
from core import indicators                    # noqa: E402
from modules import analysis, krx_data          # noqa: E402
from rich.console import Console               # noqa: E402
from rich.table import Table                   # noqa: E402

console = Console()

INDICES = [("KOSPI", "^KS11"), ("KOSDAQ", "^KQ11")]


def _keys(df):
    """날짜를 'YYYYMMDD' 로 통일한다.

    지수 소스마다 date 타입이 다르다 — tvDatafeed·토스는 datetime, KIS 는 문자열,
    KRX 병합본은 'YYYYMMDD' 다(api.charts._to_chart_schema 가 같은 이유로 같은 변환을 한다).
    이걸 맞추지 않으면 두 팔이 겹치는 날이 0 이 되어 비교 자체가 성립하지 않는다.
    """
    return df["date"].map(analysis._date_key)


def _gate(df, ma, band):
    """(현재 차단 여부, 전 구간 차단일 집합). 실매매·백테스트가 공유하는 판정 함수를 쓴다."""
    if df is None or getattr(df, "empty", True) or len(df) < ma:
        return None, set()
    blocked = indicators.get_market_filter_blocked(df["close"], ma, band)
    return bool(blocked.iloc[-1]), set(_keys(df)[blocked.values])


def _closes(df):
    if df is None or getattr(df, "empty", True):
        return {}
    return {d: float(c) for d, c in zip(_keys(df), df["close"])}


def _fetch_live(name, krx_on):
    """실매매가 보는 지수 일봉. krx_on=False 면 자격증명이 없는 프로세스를 흉내 낸다."""
    krx_data.clear_cache()      # 팔마다 원천을 새로 타야 한다(force_refresh 가 지수 캐시를 우회한다)
    if krx_on:
        return analysis.get_domestic_index_data(name, force_refresh=True)
    with patch.object(krx_data, "is_available", return_value=False):
        return analysis.get_domestic_index_data(name, force_refresh=True)


def _fetch_yf(ticker, days, ma):
    """백테스트(prepare_market_filter)와 같은 경로 — yfinance 지수."""
    import api
    import pandas as pd
    start = (datetime.now() - timedelta(days=days + 400 + ma * 2)).strftime("%Y-%m-%d")
    df = api.fetch_yfinance_data(ticker, start=start)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        try:
            df = df.xs(ticker, axis=1, level=1)
        except Exception:       # noqa: BLE001
            pass
    df.columns = [c.lower() for c in df.columns]
    df = df.reset_index()
    df.rename(columns={"Date": "date", "index": "date"}, inplace=True)
    df["date"] = df["date"].apply(
        lambda x: x.strftime("%Y%m%d") if hasattr(x, "strftime") else str(x).replace("-", "")[:8])
    return df[["date", "close"]]


def _compare(label_a, a, label_b, b, ma, band):
    """두 시계열의 값 차이와 게이트 판정 차이를 한 줄로 요약한다."""
    ca, cb = _closes(a), _closes(b)
    common = sorted(set(ca) & set(cb))
    diffs = [(d, ca[d], cb[d]) for d in common if abs(ca[d] - cb[d]) > 1e-9]
    worst = max((abs(x - y) / y * 100 for _d, x, y in diffs), default=0.0)
    worst_pt = max((abs(x - y) for _d, x, y in diffs), default=0.0)
    ga, sa = _gate(a, ma, band)
    gb, sb = _gate(b, ma, band)
    # **겹치는 구간에서만** 센다. 한쪽이 더 긴 이력을 가진 것은 원천 차이가 아니라
    #  조회 기간 차이라, 그대로 세면 '수백 일이 다르다'는 거짓 경보가 난다.
    span = set(common)
    only_a, only_b = sorted((sa - sb) & span), sorted((sb - sa) & span)
    return {
        "pair": f"{label_a} vs {label_b}",
        "common": len(common),
        "value_diff": len(diffs),
        "worst_pct": worst,
        "worst_pt": worst_pt,
        "gate_now": f"{'차단' if ga else '허용' if ga is not None else '판정불가'}"
                    f" / {'차단' if gb else '허용' if gb is not None else '판정불가'}",
        "gate_flip": ga is not None and gb is not None and ga != gb,
        "only_a": only_a,
        "only_b": only_b,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=800, help="yfinance 팔의 조회 기간(일)")
    args = ap.parse_args()

    ma = int(getattr(config, "MARKET_FILTER_MA", 80))
    band = float(getattr(config, "MARKET_FILTER_BAND", 1.0))
    krx_ok, krx_msg = krx_data.status_text()

    console.print(f"\n[bold cyan]지수 원천 교체 영향 측정[/bold cyan]  (SMA{ma} · 밴드 {band:g}%)")
    console.print(f"[dim]{krx_msg}[/dim]")
    if not krx_ok:
        console.print("[yellow]※ KRX 자격증명이 이 프로세스에 없어 ①(KRX 켜짐) 팔을 만들 수 없습니다.[/yellow]")
        console.print("[dim]  ~/.htsrc 에 KRX_ID·KRX_PW 를 넣은 뒤 **새 셸에서** 다시 실행하세요.[/dim]")
        console.print("[dim]  (②·③ 대조만 진행합니다 — 실매매 게이트와 백테스트 게이트의 차이는 이것만으로도 드러납니다.)[/dim]")

    rows = []
    for name, yf_ticker in INDICES:
        arms = {}
        if krx_ok:
            arms["①KRX"] = _fetch_live(name, krx_on=True)
        arms["②종전"] = _fetch_live(name, krx_on=False)
        arms["③yf"] = _fetch_yf(yf_ticker, args.days, ma)

        for label, df in arms.items():
            g, s = _gate(df, ma, band)
            console.print(f"  {name} {label}: 봉 {0 if df is None else len(df)}개 · "
                          f"현재 {'차단' if g else '허용' if g is not None else '판정불가'} · "
                          f"차단일 {len(s)}개")

        pairs = []
        if krx_ok:
            pairs.append(("①KRX", "②종전"))
            pairs.append(("①KRX", "③yf"))
        pairs.append(("②종전", "③yf"))
        for la, lb in pairs:
            rows.append((name, _compare(la, arms[la], lb, arms[lb], ma, band)))

    table = Table(title="원천 쌍별 차이", box=None, header_style="dim")
    for col in ("지수", "비교", "겹친 봉", "값 불일치", "최대 편차%", "최대 편차(pt)",
                "현재 판정", "차단일 차이(겹친 구간)"):
        table.add_column(col, justify="right" if col not in ("지수", "비교", "현재 판정") else "left")
    for name, r in rows:
        flip = "[red]뒤집힘[/red]" if r["gate_flip"] else r["gate_now"]
        table.add_row(name, r["pair"], str(r["common"]), str(r["value_diff"]),
                      f"{r['worst_pct']:.6f}", f"{r['worst_pt']:.4f}", flip,
                      f"+{len(r['only_a'])} / -{len(r['only_b'])}")
    console.print()
    console.print(table)

    console.print("\n[dim]읽는 법[/dim]")
    console.print("[dim] · '값 불일치'가 0 이 아니어도 편차가 소수점 아래면 반올림 차이다 — 중요한 건 오른쪽 두 칸이다.[/dim]")
    console.print("[dim] · '차단일 차이'가 크면 백테스트로 정한 다이얼이 실매매에 그대로 옮겨가지 않는다.[/dim]")
    console.print("[dim] · '뒤집힘'이면 지금 이 순간 두 원천이 서로 다른 신규 진입 판정을 내리고 있다.[/dim]")

    for name, r in rows:
        if r["only_a"] or r["only_b"]:
            console.print(f"\n[dim]{name} {r['pair']} — 한쪽에만 있는 차단일(최근 10개)[/dim]")
            console.print(f"[dim]  왼쪽만: {r['only_a'][-10:]}[/dim]")
            console.print(f"[dim]  오른쪽만: {r['only_b'][-10:]}[/dim]")


if __name__ == "__main__":
    main()
