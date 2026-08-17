"""점수·추세품질이 높은 종목에 더 넣는다 — 연속 가중 대신 '계단식 증액'은 어떤가.

[출발점] tools/audit_allocation.py 축 A에서 연속 점수 가중(0.6~1.5배)이 전체창 370.1% vs
 균등 287.6%로 크게 이겼지만 구간1·2에서 지고 MDD가 -32.3 → -35.2로 악화됐다. 그래서
 채택하지 않았다. 남은 질문은 둘이다.
   ① 그 이득은 어디서 왔나 — 점수가 실제로 '더 큰 수익'을 예고하는가, 아니면 단순히
      포지션이 커져서 좋을 때 더 벌고 나쁠 때 더 잃은 것인가.
   ② 연속 가중 대신 **문턱을 넘은 종목만 조금 더** 넣으면(계단식 증액) 상방은 남기고
      MDD 악화는 줄일 수 있는가.

[진단(축 0)] 진입 시점의 점수·추세품질 구간별 **실현 손익 분포**를 본다. 가중의 전제는
 '그 지표가 결과를 가른다'이다. 안 갈리면 가중은 그냥 베팅 크기를 키우는 것일 뿐이다.
 추세품질(indicators.get_trend_quality: 연환산 회귀 기울기 × R²)은 실매매에서 동점 가름에만
 쓰이고 크기에는 반영되지 않는다 — 이 지표로 크기를 정해도 되는지 여기서 갈린다.

[팔(축 1)] 증액은 노출을 늘린다. 그래서 **'선별해서 더 넣기'와 '그냥 더 넣기'를 반드시
 가른다** — 전 종목 일괄 증액 대조군이 없으면 무엇이 이겼는지 알 수 없다.
   · 균등(현행) · TQ 강함(≥60) +20% · TQ 계단(≥60 +30%, ≥30 +15%)
   · 점수 계단(≥8.5 +20%, ≥8.0 +10%) · 연속 가중 상한 1.2 · **[대조] 전 종목 +10%**

[실행] python3 tools/audit_score_weighted_sizing.py --trials 12 --sample 25
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)

TQ_LOOKBACK = None   # None이면 config의 TREND_QUALITY_LOOKBACK(기본 90)


def rolling_trend_quality(close, lookback):
    """indicators.get_trend_quality를 롤링으로 — 닫힌 형태 회귀로 한 번에 계산한다.

    종목·날짜마다 polyfit을 돌리면 44종목 × 2,450일에서 수십만 번이 된다. 기울기와 R²는
    Σy·Σy²·Σxy만 있으면 닫힌 형태로 나오므로 고정 가중 합성곱으로 한 번에 구한다.
    (같은 정의: 연환산 기울기 % × R², 데이터 부족 구간은 NaN)
    """
    y = np.log(np.asarray(close, dtype=float))
    n = lookback
    x = np.arange(n, dtype=float)
    Sx, Sxx_raw = x.sum(), (x ** 2).sum()
    Sxx = Sxx_raw - Sx ** 2 / n

    ones = np.ones(n)
    sum_y = np.convolve(y, ones, mode="valid")
    sum_y2 = np.convolve(y ** 2, ones, mode="valid")
    # x 가중 합 — convolve는 커널을 뒤집으므로 x를 뒤집어 넣는다.
    sum_xy = np.convolve(y, x[::-1], mode="valid")

    Sxy = sum_xy - Sx * sum_y / n
    ss_tot = sum_y2 - sum_y ** 2 / n
    slope = Sxy / Sxx
    with np.errstate(divide="ignore", invalid="ignore"):
        r2 = np.where(ss_tot > 0, np.clip(slope ** 2 * Sxx / ss_tot, 0.0, 1.0), 0.0)
    ann = (np.exp(slope * 252) - 1) * 100
    tq = ann * r2
    out = np.full(len(y), np.nan)
    out[n - 1:] = tq
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--only", default="0,1")
    args = ap.parse_args()
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]
    only = args.only.split(",")

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    dfs, mf, dates, failed = pb.prepare_universe(live, args.days)
    print(f"[준비] {len(dfs)}종목 · 거래일 {len(dates)} · 슬롯 {slots}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)
    base_ratio = 1.0 / slots

    lookback = TQ_LOOKBACK or config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    tq = {}
    for c, df in dfs.items():
        arr = rolling_trend_quality(df["close"].to_numpy(dtype=float), lookback)
        tq[c] = dict(zip((str(d) for d in df["date"]), arr))
    print(f"[준비] 추세품질 롤링 계산 완료 (lookback {lookback})", flush=True)

    def score_of(code, day):
        st = status.get(code, {}).get(day)
        return float(st[0]) if st else 0.0

    def tq_of(code, day):
        v = tq.get(code, {}).get(day)
        return None if v is None or not np.isfinite(v) else float(v)

    # ---------------- 축 0) 진단 ----------------
    if "0" in only:
        print("\n[0] 진입 시점 지표 → 실현 손익. 가중의 전제는 '이 지표가 결과를 가른다'다.")
        recs = []
        for sd in seeds:
            for i in range(args.trials):
                pick = random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs)))
                r = pb.run_portfolio(
                    {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, dates,
                    initial_capital=INITIAL_CAPITAL, slots=slots,
                    market_filter_dates={c: mf.get(c, set()) for c in pick},
                    risk_scale_by_date=new_scale())
                # 청산 기록에는 진입일이 없으므로 종목별로 '첫 매수 → 다음 청산'을 짝짓는다.
                #  [주의] 증액 기록의 reason은 '피라미딩1차/2차/3차'다. 이것을 청산으로 세면
                #  승률이 20% → 12%로 내려앉는다(profit 0인 매수 기록이 패로 잡힌다).
                def _is_exit(reason):
                    return reason != "매수" and not str(reason).startswith("피라미딩")

                seq = {}
                for t in r["trades"]:
                    seq.setdefault(t["code"], []).append(t)
                for code, ts in seq.items():
                    open_day = None
                    for t in ts:
                        if not _is_exit(t["reason"]):
                            if open_day is None:
                                open_day = t["date"]      # 증액은 진입일을 바꾸지 않는다
                        elif open_day is not None:
                            recs.append((score_of(code, open_day), tq_of(code, open_day),
                                         float(t.get("profit", 0) or 0)))
                            open_day = None
        print(f"   표본 {len(recs)}건 (진입→청산 짝)")

        def show(title, keyfn, bands):
            print(f"\n   ── {title}")
            print(f"   {'구간':<16}{'건수':>7}{'평균%':>9}{'중앙%':>9}{'상위10%':>9}"
                  f"{'최대%':>9}{'승률%':>8}")
            for lo, hi, lab in bands:
                seg = [p for s, q, p in recs
                       if (v := keyfn(s, q)) is not None and lo <= v < hi]
                if len(seg) < 20:
                    print(f"   {lab:<16}{len(seg):>7}   (표본 부족)")
                    continue
                a = np.array(seg)
                top = np.sort(a)[::-1][:max(1, len(a) // 10)]
                print(f"   {lab:<16}{len(a):>7}{a.mean():>9.2f}{np.median(a):>9.2f}"
                      f"{top.mean():>9.1f}{a.max():>9.1f}{(a > 0).mean() * 100:>8.1f}")

        show("진입 점수별", lambda s, q: s,
             [(0, 7.5, "7.0~7.5"), (7.5, 8.0, "7.5~8.0"), (8.0, 8.5, "8.0~8.5"),
              (8.5, 99, "8.5 이상")])
        show("추세품질(TQ)별", lambda s, q: q,
             [(-1e9, 0, "하락(<0)"), (0, 10, "미검증(0~10)"), (10, 30, "약함(10~30)"),
              (30, 60, "양호(30~60)"), (60, 1e9, "강함(60+)")])

    # ---------------- 축 1) 계단식 증액 ----------------
    if "1" in only:
        def f_tq_strong(day, code):
            q = tq_of(code, day)
            return base_ratio * (1.2 if q is not None and q >= 60 else 1.0)

        def f_tq_steps(day, code):
            q = tq_of(code, day)
            if q is None:
                return base_ratio
            return base_ratio * (1.3 if q >= 60 else 1.15 if q >= 30 else 1.0)

        def f_score_steps(day, code):
            s = score_of(code, day)
            return base_ratio * (1.2 if s >= 8.5 else 1.1 if s >= 8.0 else 1.0)

        def f_cont12(day, code):
            s = score_of(code, day)
            return base_ratio * min(1.2, max(0.8, 0.8 + (s - 7.0) / 5.0))

        def f_all10(day, code):
            return base_ratio * 1.1

        def f_tq10(day, code):
            q = tq_of(code, day)
            return base_ratio * (1.1 if q is not None and q >= 60 else 1.0)

        def f_tq_neutral(day, code):
            """노출 중립 재분배 — 강함은 +20%, 하락(<0)은 -20%. 총 노출을 늘리지 않고
            같은 돈을 옮기기만 한다. 증액 팔의 MDD 악화가 '노출 확대' 탓인지
            '배분' 탓인지 여기서 갈린다."""
            q = tq_of(code, day)
            if q is None:
                return base_ratio
            return base_ratio * (1.2 if q >= 60 else 0.8 if q < 0 else 1.0)

        def f_tq_and_score(day, code):
            q = tq_of(code, day)
            s = score_of(code, day)
            return base_ratio * (1.2 if (q is not None and q >= 60 and s >= 8.0) else 1.0)

        # ── 국면 조건부 증액 ─────────────────────────────────────────
        # [왜] 1·2차에서 모든 증액 변형이 구간2(코로나 급락 + 2022 약세)에서만 6~7/36으로
        #  졌다. 노출 중립 재분배도 똑같이 져서 '노출'이 아니라 '국면'이 원인이다.
        #  추세품질이 높은 종목은 이미 많이 오른 종목이라 급락장에서 더 크게 다친다.
        #  → 국면이 나쁜 날에는 증액을 접고 평시에만 얹는다.
        # [판정] **국면 라벨**로 가른다 — PendDown(하락 미확정) 또는 Bear(하락 확정)인 날.
        #  [실패한 첫 시도] 리스크 배수 <1.0을 조건으로 썼더니 거래일의 88.5%가 걸렸다.
        #   휩소율 배수가 연속값(1.0~0.85)이라 거의 매일 1.0을 살짝 밑돈다 — '나쁜 국면'이
        #   아니라 '거의 항상'을 뜻하게 되어 증액 자체가 사라졌다(314.1% → 279.6%).
        #   조건을 만들 때는 그 조건이 며칠에 걸리는지부터 세야 한다.
        from datetime import datetime, timedelta
        from tools.audit_market_axes import load_index, regime_series
        _start = (datetime.now() - timedelta(days=args.days + 400)).strftime("%Y-%m-%d")
        _reg = {}
        for _tk in ("KS11", "KQ11"):
            _idx, _cl = load_index(_tk, _start)
            _rg, _w = regime_series(_idx, _cl)
            for _d, _r in zip(_idx.strftime("%Y%m%d"), _rg):
                # 두 시장 중 나쁜 쪽을 취한다(trader.risk_scale과 같은 보수적 결합).
                if str(_r) in ("PendDown", "Bear") or _reg.get(_d) in ("PendDown", "Bear"):
                    _reg[_d] = "PendDown" if str(_r) == "PendDown" else str(_r)
                else:
                    _reg.setdefault(_d, str(_r))
        bad_days, _last = set(), "Sideways"
        for d in dates:
            _last = _reg.get(d, _last)
            if _last in ("PendDown", "Bear"):
                bad_days.add(d)
        print(f"[준비] 국면 PendDown/Bear 인 날 {len(bad_days)}일 "
              f"({len(bad_days) / len(dates) * 100:.1f}%)", flush=True)

        def f_tq_regime(day, code):
            if day in bad_days:
                return base_ratio
            q = tq_of(code, day)
            return base_ratio * (1.2 if q is not None and q >= 60 else 1.0)

        def f_tq_score_regime(day, code):
            if day in bad_days:
                return base_ratio
            q = tq_of(code, day)
            s = score_of(code, day)
            return base_ratio * (1.2 if (q is not None and q >= 60 and s >= 8.0) else 1.0)

        def f_all10_regime(day, code):
            return base_ratio * (1.0 if day in bad_days else 1.1)

        arms3 = [
            ("균등 (현행)", None),
            ("TQ 강함 +20% (무조건)", f_tq_strong),
            ("TQ 강함 +20% · 국면조건", f_tq_regime),
            ("TQ+점수8 +20% · 국면조건", f_tq_score_regime),
            ("[대조] 건전일 전종목 +10%", f_all10_regime),
        ]

        arms2 = [
            ("균등 (현행)", None),
            ("TQ 강함 +20% (1차 우승)", f_tq_strong),
            ("TQ 강함 +10% (완만)", f_tq10),
            ("TQ 재분배 +20/-20% (노출중립)", f_tq_neutral),
            ("TQ 강함 & 점수 8+ +20%", f_tq_and_score),
        ]

        arms = [
            ("균등 (현행)", None),
            ("TQ 강함(60+) +20%", f_tq_strong),
            ("TQ 계단 +30/+15%", f_tq_steps),
            ("점수 계단 +20/+10%", f_score_steps),
            ("연속 가중 (상한 1.2)", f_cont12),
            ("[대조] 전 종목 +10%", f_all10),
        ]

        k = max(1, args.subperiods)
        size = max(1, len(dates) // k)
        W = [("전체", list(dates))]
        W += [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
              for i in range(k)]
        picks = {sd: [random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs)))
                      for i in range(args.trials)] for sd in seeds}

        if "2" in only:      # 2차 정제 팔로 교체
            arms = arms2
        if "3" in only:      # 3차 국면 조건부 팔로 교체
            arms = arms3
        print(f"\n\n[1] 계단식 증액 — 표본 {args.sample} · {args.trials}회 × 씨드 {len(seeds)}개")
        for wn, wd in W:
            print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
            print(f"{'팔':<22}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
            base_res = None
            for label, fn in arms:
                res = []
                for sd in seeds:
                    for pick in picks[sd]:
                        kw = {} if fn is None else {"invest_ratio_fn": fn}
                        r = pb.run_portfolio(
                            {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                            initial_capital=INITIAL_CAPITAL, slots=slots,
                            market_filter_dates={c: mf.get(c, set()) for c in pick},
                            risk_scale_by_date=new_scale(), **kw)
                        res.append(metrics(r))
                g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if base_res is None:
                    base_res = res
                    wl = "—"
                else:
                    win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base_res)
                              if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<22}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                      f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                      f"{wl:>10}", flush=True)

        print("\n[읽는 법] 선별 증액 팔이 [대조] 전 종목 +10%를 이기지 못하면, 이긴 것은 "
              "'잘 골라 더 넣었다'가 아니라 '그냥 더 넣었다'다.")


if __name__ == "__main__":
    main()
