"""ATR 진폭 감쇠와 동적 손절 캡 — 10년 창 검증.

[문제] 2026년 국면에서 두 적응 장치가 한계값에 붙어 상수가 됐다(실측 2026-08-09).
  · ATR 손절 캡(-15%)   : 2026-07 봉의 66.4% 구속 → 손절폭이 사실상 고정 -15%
  · 변동성 배수 하한(0.4): 2026-07 봉의 98.3% 구속 → 포지션 크기가 사실상 고정 0.4배
ATR 산식·데이터는 이미 검증됐다(참조 구현과 오차 1e-11, KRX 공식과 원 단위 일치).
즉 '측정이 틀린' 것이 아니라 '반응이 포화된' 것이다.

[레버 G] ATR 진폭 감쇠 — 기간을 늘리지 않고 튐을 줄인다.
  ATR_PERIOD 확대는 이미 검증했고 구간마다 방향이 뒤집혔다(구간5 MAR 5.68→8.00 /
  구간4 3.93→0.34). 기간 확대는 **지연**을 늘려 반응을 무디게 하는 방식이라 양날이다.
  여기서는 지연을 늘리지 않고 **단일 이상봉의 기여만** 깎는 방식을 잰다.
    · 윈저화 : TR을 직전 60봉 분위수에서 클립한 뒤 Wilder 평활 (반응성 유지, 꼬리만 절단)
    · 중앙값 : Wilder 평활 대신 rolling median(TR) — 이상치에 구조적으로 둔감
  주의: ATR은 손절폭·TS 콜백·발동선·포지션 사이징에 **전부** 쓰인다. ATR을 줄이면
  손절이 좁아지고(조기 이탈↑) 동시에 포지션이 커진다(변동성 배수↑). 양방향이다.

[레버 H] 동적 손절 캡 — -15% 고정 대신 종목 자신의 최근 이력에 연동.
  cap_t = -(그 종목 직전 250봉 ATR 손절폭의 분위수). 실매매에서도 그 종목 일봉만으로
  계산되므로 그대로 옮길 수 있다(유니버스 횡단면 정보 불필요).
  상·하한을 둬 발산을 막는다 — 캡이 손절폭을 못 줄이면 리스크층 분모가 무한정 커진다.

[판정 잣대] 기존 청산·리스크 다이얼 결정과 같다. 총수익만 보지 않는다.
  상위10%·최대·>30%(fat-tail) · MDD·MAR · 손절% · 보유일 · 구간 분할 일관성.

[실행]
  python3 tools/audit_atr_damping.py --days 3650 --trials 15 --sample 25 --subperiods 5
  python3 tools/audit_atr_damping.py --only G      # 진폭 감쇠만
"""
import argparse
import os
import random
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import indicators  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from modules.auto_trade import engine  # noqa: E402

from tools.audit_atr_cap_and_ts import metrics  # noqa: E402
from tools.audit_common import seed_notice  # noqa: E402

INITIAL_CAPITAL = 10_000_000
WINSOR_LOOKBACK = 60          # TR 분위수를 재는 창
CAP_LONG_PERIOD = 120         # 동적 캡의 기준이 되는 '느린 ATR' 기간
CAP_FLOOR, CAP_CEIL = -35.0, -6.0   # 동적 캡의 허용 범위(발산 방지)


# ---------------------------------------------------------------------------
# 레버 G — ATR 진폭 감쇠
# ---------------------------------------------------------------------------
def true_range(df):
    pc = df['close'].shift()
    return pd.concat([df['high'] - df['low'],
                      (df['high'] - pc).abs(),
                      (df['low'] - pc).abs()], axis=1).max(axis=1)


def atr_wilder(df, period):
    return true_range(df).ewm(alpha=1.0 / period, adjust=False).mean()


def atr_winsor(df, period, q):
    """TR을 직전 WINSOR_LOOKBACK봉의 q분위수에서 클립한 뒤 Wilder 평활.

    지연을 늘리지 않고 단일 이상봉(갭·서킷)의 기여만 깎는다. 분위수는 **직전** 봉들로만
    구해 미래를 보지 않는다(shift(1)).
    """
    tr = true_range(df)
    thr = tr.rolling(WINSOR_LOOKBACK, min_periods=20).quantile(q).shift(1)
    clipped = np.minimum(tr, thr.fillna(tr))
    return pd.Series(clipped, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean()


def atr_median(df, period):
    """rolling median(TR). 이상치에 구조적으로 둔감하지만 Wilder보다 반응이 느리다."""
    return true_range(df).rolling(period, min_periods=2).median()


def atr_variants():
    p = config.INDICATOR_PARAMS.get("ATR_PERIOD", 14)
    return {
        "현행 Wilder":       lambda d: atr_wilder(d, p),
        "윈저 p90":          lambda d: atr_winsor(d, p, 0.90),
        "윈저 p80":          lambda d: atr_winsor(d, p, 0.80),
        "중앙값(rolling)":   lambda d: atr_median(d, p),
    }


# ---------------------------------------------------------------------------
# 레버 H — 동적 손절 캡
# ---------------------------------------------------------------------------
def add_dynamic_cap(df, k):
    """캡 = -(k × 장기ATR/가격 × 100). 봉마다 미리 계산해 컬럼에 넣는다.

    [왜 장기 ATR인가] 캡을 손절폭과 **같은 척도**(ATR14)에 비례시키면 캡은 무의미해진다 —
    손절폭이 ATR14×2이므로 캡이 ATR14×k면 k>2에서 절대 안 걸리고 k<2면 항상 걸린다.
    캡이 일을 하려면 손절폭보다 **느린** 척도에 걸려야 한다. 그래야
      · 시장 전체가 고변동이면 장기 ATR도 올라 캡이 함께 넓어지고(적응성 보존)
      · 그 종목이 자기 평소보다 튄 날만 잘라낸다(이상치 절단)
    는 두 성질을 동시에 얻는다. k=2.5면 ATR14가 ATR120의 1.25배를 넘을 때부터 구속한다.

    첫 설계(자기이력 분위수)는 기각했다 — 250봉 분위수는 현행보다 더 조여(2026년 캡
    중앙 -9~-13%, 구속률 26%→74%) 원하는 방향의 정반대였다.
    """
    long_atr = true_range(df).ewm(alpha=1.0 / CAP_LONG_PERIOD, adjust=False).mean()
    cap = -(k * long_atr / df['close'] * 100)
    return cap.clip(lower=CAP_FLOOR, upper=CAP_CEIL)


def make_sl_fn(cap_col):
    """run_portfolio의 sl_rate_fn 훅. cap_col이 None이면 config 고정 캡."""
    def fn(row, price, atr_mult):
        cap = row.get(cap_col) if cap_col else None
        if cap is None or not np.isfinite(cap):
            cap = None                                       # 이력 부족 → 고정 캡으로
        return engine.atr_stop_rate(row.get("ATR", 0), price,
                                    atr_mult=atr_mult, max_cap=cap) or 0.0
    return fn


# 지수 실현변동성 기준 캡 — 날짜별 배율 {YYYYMMDD: ratio}
_INDEX_RATIO = {}


def _set_index_ratio(d):
    global _INDEX_RATIO
    _INDEX_RATIO = d


def build_index_ratio(days, window=60):
    """KOSPI 실현변동성의 '장기 대비 배율' 시계열.

    [왜 지수인가] 캡을 그 종목 ATR에 비례시키면 손절폭도 같은 ATR에 비례하므로 캡이
    무의미해진다(k>2면 절대 안 걸리고 k<2면 항상 걸린다). 장기 ATR로 척도를 늦춰봐도
    120봉 EMA는 국면 전환을 절반밖에 못 따라온다(실측: 2026년 캡 중앙 -15.1%로
    고정값과 같아짐). 지수 변동성은 종목 ATR과 **독립된 시계열**이라 이 순환을 끊는다.
    시장 전체가 고변동이면 캡이 넓어지고, 평시로 돌아오면 좁아진다.

    장기 기준은 확장(expanding) 중앙값 + shift(1)로 잡아 미래를 보지 않는다.
    """
    from tools.audit_market_axes import load_index
    from datetime import datetime, timedelta
    start = (datetime.now() - timedelta(days=days + 400)).strftime("%Y-%m-%d")
    idx, close = load_index("KS11", start)
    ret = pd.Series(close).pct_change()
    vol = ret.rolling(window).std()
    ref = vol.expanding(min_periods=250).median().shift(1)
    ratio = (vol / ref).clip(0.4, 3.0)
    return {d.strftime("%Y%m%d"): (r if np.isfinite(r) else 1.0)
            for d, r in zip(idx, ratio.to_numpy())}


def add_dynamic_cap_index(df, base, power=1.0):
    """캡 = base(-15%) × 그 날의 지수 변동성 배율^power.

    power=1.0은 배율을 그대로 반영해 고변동 국면에서 캡이 사실상 해제된다(2026년 하한 -35%).
    power=0.5(제곱근)는 같은 방향이되 완만하다 — 배율 3배에서 캡이 1.73배만 넓어진다.
    """
    r = df['date'].astype(str).map(lambda d: _INDEX_RATIO.get(d, 1.0)).astype(float)
    return (base * np.power(r, power)).clip(lower=CAP_FLOOR, upper=CAP_CEIL)


def cap_variants():
    """(라벨, 종류, 파라미터). 종류: None=고정 / 'atr'=장기ATR배수 / 'idx'=지수연동."""
    return {
        "고정 -15% (현행)": (None, None),
        "장기ATR×4.0":      ("atr", 4.0),
        "지수연동√ -15%":    ("idxs", -15.0),
        "지수연동 -15%":     ("idx", -15.0),
    }


# ---------------------------------------------------------------------------
def fmt_table(group, rows, base_label, extra=None):
    print(f"\n{group}  (기준선: {base_label})")
    hdr = (f"{'설정':<18}{'수익%':>8}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
           f"{'상위10%':>9}{'최대':>9}{'>30%':>6}{'TS이익%':>9}{'손절%':>7}"
           f"{'보유일':>7}{'청산':>6}{'수익승':>8}{'MAR승':>7}{'꼬리승':>7}")
    print(hdr); print("-" * len(hdr))
    base = rows[base_label]
    for label, ms in rows.items():
        a = lambda k: float(np.mean([m[k] for m in ms]))
        if label == base_label:
            w = mw = tw = "—"
        else:
            w = f"{sum(1 for x, y in zip(ms, base) if x['ret'] > y['ret'])}/{len(ms)}"
            mw = f"{sum(1 for x, y in zip(ms, base) if x['mar'] > y['mar'])}/{len(ms)}"
            tw = f"{sum(1 for x, y in zip(ms, base) if x['top10'] > y['top10'])}/{len(ms)}"
        print(f"{label:<18}{a('ret'):>8.1f}{a('mdd'):>8.1f}{a('mar'):>7.2f}{a('pf'):>6.2f}"
              f"{a('wr'):>7.1f}{a('top10'):>9.1f}{a('best'):>9.1f}{a('big'):>6.0f}"
              f"{a('ts_profit_share'):>9.1f}{a('stop_share'):>7.1f}{a('days'):>7.0f}"
              f"{a('n'):>6.0f}{w:>8}{mw:>7}{tw:>7}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None, help="G(진폭 감쇠) 또는 H(동적 캡)")
    ap.add_argument("--seed", type=int, default=20260809)
    ap.add_argument("--subperiods", type=int, default=5)
    ap.add_argument("--diag-only", action="store_true", help="진단표만(시뮬 생략)")
    args = ap.parse_args()
    seed_notice(1)

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일(≈{args.days/365:.1f}년) · 슬롯 {slots}")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

    mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    fixed_cap = abs(config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0))

    # ── 진단 ①: 감쇠 방식별 손절폭·캡 구속률 ────────────────────────
    print(f"\n{'='*100}\n진단 ① ATR 진폭 감쇠 — 손절폭(ATR×{mult}/가격)과 캡(-{fixed_cap:.0f}%) 구속률\n{'='*100}")
    print(f"{'산출 방식':<18}{'전체 중앙':>10}{'전체 p95':>10}{'구속%':>8}"
          f"{'2026 중앙':>11}{'2026 p95':>10}{'2026 구속%':>11}")
    print("-" * 100)
    variants = atr_variants()
    for label, fn in variants.items():
        allw, w26 = [], []
        for c, df in dfs.items():
            d = df.dropna(subset=['high', 'low', 'close']).reset_index(drop=True)
            w = (fn(d) * mult / d['close'] * 100).to_numpy()
            yr = d['date'].astype(str).str[:4].to_numpy()
            ok = np.isfinite(w)
            allw.append(w[ok]); w26.append(w[ok & (yr == '2026')])
        a = np.concatenate(allw); b = np.concatenate(w26)
        print(f"{label:<18}{np.median(a):>9.2f}%{np.percentile(a,95):>9.2f}%"
              f"{(a>fixed_cap).mean()*100:>7.1f}%{np.median(b):>10.2f}%"
              f"{np.percentile(b,95):>9.2f}%{(b>fixed_cap).mean()*100:>10.1f}%")

    # ── 진단 ②: 동적 캡이 실제로 어떤 값이 되는가 ──────────────────
    print(f"\n{'='*100}\n진단 ② 동적 캡 — 장기ATR({CAP_LONG_PERIOD}) 배수 (허용 {CAP_FLOOR}~{CAP_CEIL}%)\n{'='*100}")
    print(f"{'배수':<14}{'전체 캡 중앙':>14}{'2026 캡 중앙':>14}{'전체 구속%':>12}{'2026 구속%':>12}")
    print("-" * 100)
    _set_index_ratio(build_index_ratio(args.days))
    for label, (kind, q) in cap_variants().items():
        if kind is None:
            allc = np.array([-fixed_cap]); c26 = np.array([-fixed_cap])
            bind_a = bind_b = None
        else:
            ac, bc, ba, bb = [], [], [], []
            for c, df in dfs.items():
                d = df.dropna(subset=['ATR', 'close']).reset_index(drop=True)
                cap = (add_dynamic_cap(d, q) if kind == "atr"
                       else add_dynamic_cap_index(d, q, 0.5 if kind == "idxs" else 1.0)
                       ).to_numpy()
                w = (d['ATR'] * mult / d['close'] * 100).to_numpy()
                yr = d['date'].astype(str).str[:4].to_numpy()
                ok = np.isfinite(cap)
                ac.append(cap[ok]); ba.append(w[ok] > -cap[ok])
                m26 = ok & (yr == '2026')
                bc.append(cap[m26]); bb.append(w[m26] > -cap[m26])
            allc = np.concatenate(ac); c26 = np.concatenate(bc)
            bind_a = np.concatenate(ba).mean()*100; bind_b = np.concatenate(bb).mean()*100
        s_a = f"{bind_a:>11.1f}%" if bind_a is not None else f"{'—':>12}"
        s_b = f"{bind_b:>11.1f}%" if bind_b is not None else f"{'—':>12}"
        print(f"{label:<14}{np.median(allc):>13.1f}%{np.median(c26):>13.1f}%{s_a}{s_b}")

    if args.diag_only:
        return

    # ── 시뮬레이션 ────────────────────────────────────────────────
    if not _INDEX_RATIO:
        _set_index_ratio(build_index_ratio(args.days))
    th = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
          "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
          "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
          "WEIGHTS": config.SCORING_WEIGHTS}

    sets = []   # (그룹, 라벨, dfs, status, sl_fn)
    want = (args.only or "").upper()

    if not want or want == "G":
        for label, fn in variants.items():
            d2 = {}
            for c, df in dfs.items():
                x = df.copy(); x['ATR'] = fn(x); d2[c] = x
            print(f"  [준비] G/{label} 사전계산...", end="\r", flush=True)
            sets.append(("G. ATR 진폭 감쇠", label, d2, pb.precompute_status(d2, th), None))

    if not want or want == "H":
        st0 = None
        for label, (kind, q) in cap_variants().items():
            d2 = {}
            for c, df in dfs.items():
                x = df.copy()
                x['DYNCAP'] = (np.nan if kind is None else
                               (add_dynamic_cap(x, q) if kind == "atr"
                                else add_dynamic_cap_index(
                                    x, q, 0.5 if kind == "idxs" else 1.0)))
                d2[c] = x
            if st0 is None:
                print("  [준비] H/상태 사전계산...", end="\r", flush=True)
                st0 = pb.precompute_status(d2, th)   # ATR 불변이므로 상태는 공유
            sets.append(("H. 동적 손절 캡", label, d2, st0,
                         make_sl_fn('DYNCAP' if kind is not None else None)))
    print(" " * 70, end="\r")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = ((int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
           float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
           float(p.get("DD_SCALE_2", 0.8))) if p.get("USE_DRAWDOWN_RISK_SCALING", True) else None)

    codes = list(dfs.keys())
    k = max(1, args.subperiods); size = len(dates) // k
    windows = ([(f"구간{i+1}", dates[i*size:(i+1)*size if i < k-1 else len(dates)])
                for i in range(k)] if k > 1 else [("전체", dates)])

    print(f"\n{'='*126}")
    print(f"진폭 감쇠 · 동적 캡 검증 — {args.trials}회 × {args.sample}종목 짝비교 "
          f"({args.days}일 ≈ {args.days/365:.1f}년)")
    print("=" * 126)

    for wname, wdates in windows:
        res = {(g, l): [] for g, l, _d, _s, _f in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sm = {c: mf.get(c, set()) for c in pick}
            for g, label, d2, st, slfn in sets:
                r = pb.run_portfolio({c: d2[c] for c in pick}, {c: st[c] for c in pick},
                                     wdates, initial_capital=args.seed_capital, slots=slots,
                                     market_filter_dates=sm,
                                     risk_scale_by_date=make_scale_fn(mkt, dd),
                                     sl_rate_fn=slfn)
                res[(g, label)].append(metrics(r))
            print(f"  {wname} 시행 {t+1}/{args.trials}", end="\r", flush=True)

        print(f"\n{'#'*10} {wname} ({len(wdates)} 거래일) {'#'*10}")
        by = defaultdict(dict)
        for (g, l), ms in res.items():
            by[g][l] = ms
        for g in dict.fromkeys(x[0] for x in sets):
            rows = by[g]
            base = next(l for l in rows if "현행" in l)
            fmt_table(g, rows, base)

    print("\n" + "-" * 126)
    print("G) ATR을 줄이면 손절이 좁아지고(조기 이탈↑) 동시에 포지션이 커진다(변동성 배수↑).")
    print("   총수익만 보면 오판한다 — MAR과 상위10%를 함께 볼 것.")
    print("H) 캡만 바꾸므로 포지션 크기는 3층(변동성 타겟팅)이 그대로 정한다.")
    print("   즉 H는 '청산선을 노이즈 밖으로 내보내는' 한 방향 효과다.")


if __name__ == "__main__":
    main()
