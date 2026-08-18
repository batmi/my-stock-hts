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

[축 4 — 모멘텀 크래시 감지기] 3차(국면 조건부)까지 와서도 구간2만 8-0-28로 진다. 결정적
 단서는 대조군이다 — 같은 구간2에서 '건전일 전 종목 +10%'는 21-0-15로 **이긴다.** 즉 손해의
 원인은 증액이 아니라 **추세품질 상위를 골라 증액한 것**이다. 이것이 모멘텀 크래시다:
 급락 뒤 반등장에서 직전 승자가 가장 크게 무너진다(Daniel-Moskowitz). PendDown/Bear 라벨은
 지수가 '내려갈 때' 켜지므로 반등 구간을 못 짚는다 — 그래서 3차가 실패했다.
   감지기 후보(전부 시점 기준, 미래 참조 없음): ① 지수 252일 고점 대비 낙폭이 문턱 아래로
   내려간 뒤 N거래일 — 낙폭 국면과 그 직후 반등을 함께 덮는다. ② 지수 20일 실현변동성이
   과거 분포 상위 — 크래시는 고변동 국면에서 일어난다.
 [반드시 먼저] 조건이 며칠에 걸리는지 세고(3차의 88.5% 사고), 크래시 창 안에서 TQ 강함
 진입이 실제로 더 나쁜지 실현 손익으로 확인한 뒤에 팔을 돌린다. 감지기가 사후에 구간2만
 골라내는 것이라면 그건 발견이 아니라 곡선 맞추기다.

[실행] python3 tools/audit_score_weighted_sizing.py --trials 12 --sample 25
       python3 tools/audit_score_weighted_sizing.py --only 4 --trials 12 --sample 25
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import indicators  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)

TQ_LOOKBACK = None   # None이면 config의 TREND_QUALITY_LOOKBACK(기본 90)


def rolling_trend_quality(close, lookback):
    """indicators.rolling_trend_quality 재수출 — 산식은 지표 계층 한 곳에만 둔다.

    [2026-08-18] 같은 산식이 tools 안에 두 벌 있었고 백테스트 엔진은 아예 없었다.
     엔진이 실매매 순위를 기본값으로 재현하게 되면서 원본을 indicators로 올렸다.
     이 이름으로 import하는 도구가 여럿이라 자리는 남긴다.
    """
    return indicators.rolling_trend_quality(close, lookback)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--only", default="0,1")
    ap.add_argument("--crash-detector", default="급락 -12%/20일 +60일")
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

    # 청산 기록에는 진입일이 없으므로 종목별로 '첫 매수 → 다음 청산'을 짝짓는다.
    #  [주의] 증액 기록의 reason은 '피라미딩1차/2차/3차'다. 이것을 청산으로 세면
    #  승률이 20% → 12%로 내려앉는다(profit 0인 매수 기록이 패로 잡힌다).
    def _is_exit(reason):
        return reason != "매수" and not str(reason).startswith("피라미딩")

    def collect_records():
        """기준선(균등) 운용을 돌려 (진입점수, 진입TQ, 진입일, 실현손익) 목록을 만든다."""
        out = []
        for sd in seeds:
            for i in range(args.trials):
                pick = random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs)))
                r = pb.run_portfolio(
                    {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, dates,
                    initial_capital=INITIAL_CAPITAL, slots=slots,
                    market_filter_dates={c: mf.get(c, set()) for c in pick},
                    risk_scale_by_date=new_scale())
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
                            out.append((score_of(code, open_day), tq_of(code, open_day),
                                        open_day, float(t.get("profit", 0) or 0)))
                            open_day = None
        return out

    # ---------------- 축 0) 진단 ----------------
    if "0" in only:
        print("\n[0] 진입 시점 지표 → 실현 손익. 가중의 전제는 '이 지표가 결과를 가른다'다.")
        recs = [(s_, q_, p_) for s_, q_, _d, p_ in collect_records()]
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

    # ---------------- 축 4) 모멘텀 크래시 감지기 ----------------
    if "4" in only:
        from datetime import datetime, timedelta
        from tools.audit_market_axes import load_index
        _start = (datetime.now() - timedelta(days=args.days + 400)).strftime("%Y-%m-%d")

        def _sharp(drop_pct, win, hold, skip=0):
            """급락 방아쇠 — win 거래일 수익률이 drop_pct 아래로 떨어지면 hold일 켠다.

            [왜 '낙폭'이 아니라 '급락'인가] 첫 시도는 252일 고점 대비 낙폭을 썼는데
             커버리지가 62~77%였다. 코스닥은 10년 중 대부분을 고점 -15% 아래에서 보내
             '낙폭'은 크래시가 아니라 평상시를 뜻한다. 모멘텀 크래시는 **빠른 붕괴**와
             그 직후 반등에서 터지므로 속도(20일 수익률)로 잡아야 한다.
            [skip] 방아쇠 직후 skip일을 빼면 '급락 자체'는 제외하고 **반등 구간만** 남는다.
            """
            def f(cl):
                n = len(cl)
                on = np.zeros(n, dtype=bool)
                left = cnt = 0
                for i in range(n):
                    if (cl[i] / cl[max(0, i - win)] - 1) * 100 <= drop_pct:
                        left, cnt = hold, 0    # 다시 밟으면 창이 갱신된다
                    if left > 0:
                        cnt += 1
                        if cnt > skip:
                            on[i] = True
                        left -= 1
                return on
            return f

        def _dd_and_vol(dd_thr, pct, vol_win=20):
            """깊은 낙폭 **그리고** 고변동 — 둘을 곱해야 크래시 구간만 남는다.
            분위수는 '그날까지의 과거'로만 잡는다(확장 분위수 — 미래를 보지 않는다).
            """
            def f(cl):
                n = len(cl)
                rmax = np.array([cl[max(0, i - 251):i + 1].max() for i in range(n)])
                dd = (cl / rmax - 1.0) * 100
                r = np.diff(np.log(cl), prepend=np.log(cl[0]))
                vol = np.array([r[max(0, i - vol_win + 1):i + 1].std() * np.sqrt(252) * 100
                                for i in range(n)])
                on = np.zeros(n, dtype=bool)
                for i in range(252, n):        # 1년치가 쌓인 뒤부터만 판정
                    if dd[i] <= dd_thr and vol[i] >= np.percentile(vol[:i + 1], pct):
                        on[i] = True
                return on
            return f

        _idx_cache = {}

        def crash_flags(fn, combine="union"):
            """두 시장(KOSPI·KOSDAQ)에 감지기를 걸어 합집합/교집합을 돌려준다."""
            per = {}
            for tk in ("KS11", "KQ11"):
                if tk not in _idx_cache:
                    _idx_cache[tk] = load_index(tk, _start)
                idx, cl = _idx_cache[tk]
                per[tk] = (idx.strftime("%Y%m%d").values, fn(cl))
            flag = {}
            for ds, on in per.values():
                for d, v in zip(ds, on):
                    flag[d] = (flag.get(d, False) or bool(v)) if combine == "union" \
                        else (flag.get(d, True) and bool(v))
            out, last = set(), False
            for d in dates:                    # 지수 휴장일은 직전 상태를 잇는다
                last = flag.get(d, last)
                if last:
                    out.add(d)
            return out

        k = max(1, args.subperiods)
        size = max(1, len(dates) // k)
        W = [("전체", list(dates))]
        W += [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
              for i in range(k)]

        # 커버리지 8~30%대로 잡힌 정의만 남겼다(첫 시도의 62~77%짜리는 전부 버렸다).
        DETECTORS = [
            ("급락 -12%/20일 +60일", _sharp(-12, 20, 60), "union"),
            ("급락 -12% 반등만(10~70일)", _sharp(-12, 20, 70, skip=10), "union"),
            ("급락 -12% [양시장 동시]", _sharp(-12, 20, 60), "inter"),
            ("낙폭-15% & 변동성상위20%", _dd_and_vol(-15.0, 80.0), "union"),
        ]
        flags = {}
        print("\n\n[4] 모멘텀 크래시 감지기 — 먼저 '며칠에 걸리는가'부터 센다.")
        print(f"   {'감지기':<30}{'전체%':>8}" + "".join(f"{w[0]:>9}" for w in W[1:]))
        for name, fn, comb in DETECTORS:
            flags[name] = crash_flags(fn, comb)
            cov = [len([d for d in wd if d in flags[name]]) / len(wd) * 100 for _, wd in W]
            print(f"   {name:<30}{cov[0]:>8.1f}" + "".join(f"{c:>9.1f}" for c in cov[1:]),
                  flush=True)
        print("   [읽는 법] 구간2에만 몰려 있고 나머지가 0에 가까우면 감지기가 아니라 "
              "구간2를 손으로 지운 것이다 — 그런 팔의 승리는 곡선 맞추기다.")

        # ── 감지기가 실제로 무엇을 가르는지: 크래시 창 안팎의 실현 손익
        print("\n[4-진단] 크래시 창 안에서 TQ 강함 진입이 정말 더 나쁜가 (기준선 운용의 실현 손익)")
        recs4 = collect_records()
        print(f"   표본 {len(recs4)}건")
        for name in flags:
            fl = flags[name]
            print(f"\n   ── {name}")
            print(f"   {'구간':<22}{'건수':>7}{'평균%':>9}{'상위10%':>9}{'승률%':>8}")
            for lab, sel in (("크래시 창 안", True), ("창 밖(평시)", False)):
                for tqlab, lo in (("TQ 강함(60+)", 60.0), ("TQ 그 외", None)):
                    seg = [p for _s, q, d, p in recs4
                           if (d in fl) == sel
                           and (q is not None and q >= 60 if lo else (q is None or q < 60))]
                    if len(seg) < 20:
                        print(f"   {lab + ' · ' + tqlab:<22}{len(seg):>7}   (표본 부족)")
                        continue
                    a = np.array(seg)
                    top = np.sort(a)[::-1][:max(1, len(a) // 10)]
                    print(f"   {lab + ' · ' + tqlab:<22}{len(a):>7}{a.mean():>9.2f}"
                          f"{top.mean():>9.1f}{(a > 0).mean() * 100:>8.1f}")
        print("\n   [읽는 법] '창 안 TQ강함'만 유독 나쁘고 '창 안 그 외'는 멀쩡해야 감지기가 "
              "값을 한다. 창 안이 통째로 나쁘면 그건 크래시 감지기가 아니라 그냥 하락장 탐지기다.")

        # ── 팔 (기준 감지기 하나 + 3차 최고 팔 대조)
        best = args.crash_detector if args.crash_detector in flags else DETECTORS[0][0]
        cflag = flags[best]
        print(f"\n[4-팔] 감지기 '{best}' 로 증액 해제")

        def f_tq_score_crash(day, code):
            if day in cflag:
                return base_ratio
            q, sc = tq_of(code, day), score_of(code, day)
            return base_ratio * (1.2 if (q is not None and q >= 60 and sc >= 8.0) else 1.0)

        def f_tq_crash(day, code):
            if day in cflag:
                return base_ratio
            q = tq_of(code, day)
            return base_ratio * (1.2 if q is not None and q >= 60 else 1.0)

        def f_tq_score_crash_flip(day, code):
            """해제 대신 **뒤집기** — 크래시 창에서는 TQ 강함을 오히려 줄인다(-20%).
            창 안에서 승자가 무너지는 것이 사실이라면 줄이는 편이 더 벌어야 한다."""
            q, sc = tq_of(code, day), score_of(code, day)
            hot = q is not None and q >= 60 and sc >= 8.0
            if day in cflag:
                return base_ratio * (0.8 if hot else 1.0)
            return base_ratio * (1.2 if hot else 1.0)

        def f_all10_crash(day, code):
            return base_ratio * (1.0 if day in cflag else 1.1)

        arms4 = [
            ("균등 (현행)", None),
            ("TQ+점수8 +20% (무조건)", lambda day, code: base_ratio * (
                1.2 if (tq_of(code, day) is not None and tq_of(code, day) >= 60
                        and score_of(code, day) >= 8.0) else 1.0)),
            ("TQ+점수8 · 크래시해제", f_tq_score_crash),
            ("TQ 강함 · 크래시해제", f_tq_crash),
            ("TQ+점수8 · 크래시뒤집기", f_tq_score_crash_flip),
            ("[대조] 평시 전종목 +10%", f_all10_crash),
        ]
        picks4 = {sd: [random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs)))
                       for i in range(args.trials)] for sd in seeds}
        for wn, wd in W:
            print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
            print(f"{'팔':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
            base_res = None
            for label, fn in arms4:
                res = []
                for sd in seeds:
                    for pick in picks4[sd]:
                        kw = {} if fn is None else {"invest_ratio_fn": fn}
                        r = pb.run_portfolio(
                            {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                            initial_capital=INITIAL_CAPITAL, slots=slots,
                            market_filter_dates={c: mf.get(c, set()) for c in pick},
                            risk_scale_by_date=new_scale(), **kw)
                        res.append(metrics(r))
                g4 = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if base_res is None:
                    base_res, wl = res, "—"
                else:
                    win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base_res)
                              if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<24}{g4('ret'):>9.1f}{g4('mdd'):>8.1f}{g4('mar'):>7.2f}"
                      f"{g4('pf'):>6.2f}{g4('n'):>6.0f}{g4('top10'):>9.1f}{g4('win'):>7.1f}"
                      f"{wl:>10}", flush=True)


if __name__ == "__main__":
    main()
