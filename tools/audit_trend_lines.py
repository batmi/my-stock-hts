"""차트 추세선이 최근 흐름과 반대 방향으로 표기되는 문제를 실측한다.

[증상] 최근 1.5개월 내리 하락한 구간에서도 '상승 지지선/상승 저항선'이 그려진다.
실제 KOSPI 2026-01~08 구간(최근 30봉 -24.6%)에서 두 선 모두 '상승'으로 나왔다.

[원인 가설]
  ① 하락 구간에서는 스윙 포인트가 거의 생기지 않는다. get_swing_points(order=5)는
     좌우 5봉씩 총 11봉의 최극값을 요구하는데, 지속 하락에서는 새 저점이 곧바로 깨져
     저점이 성립하지 않고, 고점도 '앞 5봉보다 높아야' 해서 성립하지 않는다.
  ② 최근 order봉은 구조적으로 제외된다(range(order, n-order)).
  ③ 기간 제한이 없다. TREND_PERIOD는 '몇 개의 스윙점을 쓸지'(period//20)만 정하고
     탐색 범위를 자르지 않아, 몇 달 전 점으로 만든 선을 현재까지 외삽한다.

[판정 지표] 그림 비율이 아니라 **오도율** — 표기된 방향이 최근 실제 방향과 반대인 비율.
  틀린 선보다 없는 선이 낫다는 전제이므로, 미표시는 오도로 세지 않고 따로 집계한다.

[실행] python tools/audit_trend_lines.py [--days 400] [--stocks 20]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config      # noqa: E402
from core import indicators  # noqa: E402


def trend_lines(df, order, period, max_anchor_age=None):
    """현행 get_trend_lines에 두 가지 후보 수정을 얹은 실험 버전.

    period: 이 봉 수 안의 스윙점만 사용(현행은 제한 없음).
    max_anchor_age: 마지막 스윙점이 이보다 오래됐으면 그리지 않는다(현행은 무제한 외삽).
    """
    n_recent = max(2, period // 20)
    sh, sl = indicators.get_swing_points(df, order)
    n = len(df)
    cutoff = n - period
    out = {}
    for key, pts in (('support', sl), ('resistance', sh)):
        pts = [p for p in pts if p[0] >= cutoff]
        if len(pts) < 2:
            continue
        recent = pts[-n_recent:]
        if max_anchor_age is not None and (n - 1 - recent[-1][0]) > max_anchor_age:
            continue        # 앵커가 낡았다 = 현재 흐름을 설명하지 못한다
        xs = np.array([i for i, _ in recent], dtype=float)
        ys = np.array([p for _, p in recent], dtype=float)
        slope, _ic = np.polyfit(xs, ys, 1)
        out[key] = float(slope)
    return out


def channel(df, period, slope_src="mid"):
    """평행 채널 방식(현행 구현). 레그 시작점 고정 + 고·저 회귀 평균 기울기.

    slope_src: 'mid'(고·저 평균) / 'high'(고가만) / 'close'(종가만) — 기울기 산출 비교용.
    """
    n = len(df)
    if n < 10:
        return {}
    ws = max(0, n - period)
    hi = np.asarray(df['high'].values, float)
    lo = np.asarray(df['low'].values, float)
    i_hi = ws + int(np.argmax(hi[ws:]))
    i_lo = ws + int(np.argmin(lo[ws:]))
    a = min(i_hi, i_lo)
    if i_hi == i_lo or (n - a) < 10:
        return {}
    xs = np.arange(a, n, dtype=float)
    if slope_src == "high":
        s = float(np.polyfit(xs, hi[a:], 1)[0])
    elif slope_src == "close":
        s = float(np.polyfit(xs, np.asarray(df['close'].values, float)[a:], 1)[0])
    else:
        s = float((np.polyfit(xs, hi[a:], 1)[0] + np.polyfit(xs, lo[a:], 1)[0]) / 2)
    up = float(np.max(hi[a:] - s * xs))
    dn = float(np.min(lo[a:] - s * xs))
    if up - dn <= 0 or abs(s) * (n - 1 - a) / (up - dn) < 1.0:
        return {}
    return {'resistance': s, 'support': s}


def variants():
    """(라벨, order, period, 앵커 최대 나이). 현행은 period 제한·앵커 제한 모두 없음.

    'CH_' 로 시작하는 항목은 스윙 방식이 아니라 평행 채널 방식이다(order/age 무시).
    """
    return [
        ("현행 (o5/제한없음)",       5, 10_000, None),
        #  LIVE_ 는 실제 출하 구현(indicators.get_trend_lines)을 그대로 호출한다.
        #  감사 도구에 로직을 복제하면 구현을 바꿨을 때 조용히 갈라진다.
        ("LIVE_실제 구현",           0, 60, None),
        ("CH_채널 고저평균 P60",     0, 60, None),
        ("CH_채널 고저평균 P40",     0, 40, None),
        ("CH_채널 고가만 P60",       0, 60, "high"),
        ("CH_채널 종가만 P60",       0, 60, "close"),
        ("기간60만",                 5, 60, None),
        ("기간40만",                 5, 40, None),
        ("기간30만",                 5, 30, None),
        ("o3 + 기간60",              3, 60, None),
        ("o3 + 기간40",              3, 40, None),
        ("기간60 + 앵커≤20",         5, 60, 20),
        ("기간60 + 앵커≤10",         5, 60, 10),
        ("o3 + 기간60 + 앵커≤10",    3, 60, 10),
        ("o3 + 기간40 + 앵커≤10",    3, 40, 10),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--stocks", type=int, default=20)
    ap.add_argument("--horizon", type=int, default=20,
                    help="'최근 실제 방향'을 재는 봉 수")
    args = ap.parse_args()

    import FinanceDataReader as fdr
    config.session.load_stock_config()
    codes = [(s["code"], s["name"])
             for s in config.session.stock_data.get("stocks_kr", [])][:args.stocks]
    codes = [("KS11", "KOSPI"), ("KQ11", "KOSDAQ")] + codes

    start = (pd.Timestamp.today() - pd.Timedelta(days=args.days + 200)).strftime("%Y-%m-%d")
    frames = []
    for code, name in codes:
        try:
            d = fdr.DataReader(code, start)
            if d is None or len(d) < 150:
                continue
            d = d.rename(columns=str.lower).reset_index()
            frames.append((name, d[['high', 'low', 'close']].copy()))
        except Exception:
            continue
    print(f"[준비] {len(frames)}종목 · 관측 지평 {args.horizon}봉")

    vs = variants()
    stat = {v[0]: {"drawn": 0, "wrong": 0, "total": 0} for v in vs}

    for name, df in frames:
        n = len(df)
        for t in range(140, n, 5):          # 5봉 간격으로 as-of 시점 이동
            sub = df.iloc[:t].reset_index(drop=True)
            # 최근 실제 방향: horizon봉 종가 수익률 부호. 횡보(±2%)는 판정에서 제외.
            ret = sub['close'].iloc[-1] / sub['close'].iloc[-args.horizon] - 1
            if abs(ret) < 0.02:
                continue
            actual_up = ret > 0
            for label, order, period, age in vs:
                if label.startswith("LIVE_"):
                    res = {k: v[0] for k, v in
                           indicators.get_trend_lines(sub, period=period).items()}
                elif label.startswith("CH_"):
                    res = channel(sub, period, age or "mid")
                else:
                    res = trend_lines(sub, order, period, age)
                for slope in res.values():
                    stat[label]["total"] += 1
                    stat[label]["drawn"] += 1
                    if (slope > 0) != actual_up:
                        stat[label]["wrong"] += 1
                # 미표시는 '그리지 않음'이라 오도가 아니다 — total에 넣지 않는다.

    print(f"\n{'=' * 78}")
    print("추세선 표기 방향이 최근 실제 방향과 반대인 비율 (낮을수록 좋음)")
    print(f"{'=' * 78}")
    print(f"{'설정':<26}{'그린 선 수':>11}{'오도 수':>9}{'오도율':>9}")
    print("-" * 78)
    base = stat["현행 (o5/제한없음)"]
    for label, *_ in vs:
        s = stat[label]
        rate = s["wrong"] / s["total"] * 100 if s["total"] else 0.0
        mark = ""
        if s is not base and s["total"]:
            b = base["wrong"] / base["total"] * 100 if base["total"] else 0
            mark = f"  ({rate - b:+.1f}%p)"
        print(f"{label:<26}{s['drawn']:>11,}{s['wrong']:>9,}{rate:>8.1f}%{mark}")
    print("-" * 78)
    print("※ 미표시는 오도로 세지 않는다(틀린 선보다 없는 선이 낫다는 전제).")
    print("  따라서 '그린 선 수'가 줄면서 오도율이 함께 떨어져야 실제 개선이다.")


if __name__ == "__main__":
    main()
