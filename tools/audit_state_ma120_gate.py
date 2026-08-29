"""매수 상태에서 '120일선 아래'를 잘라야 하는가 — 색상 규칙과의 충돌에서 나온 축.

[출발점] 화면에서 현재가가 **노란색**(역배열 = EMA20<EMA60, 그런데 20일선은 되찾음)인
 종목이 동시에 **'매수'**로 찍힌다. 색상 규칙의 도움말은 노랑을 "전 색 중 최약"이라 하고
 상태는 진입 후보라 하니, 운용자 눈에는 두 판정이 부딪힌다.

[왜 부딪히나 — 코드] analysis.price_trend_color 는 **구조**(EMA20 vs EMA60)를 보고,
 analysis.classify_stock_state 는 그 비교를 **한 번도 하지 않는다**(현재가 vs EMA60/EMA120
 만 본다). 게다가 매수 분기는 `is_caution`("120일선 이탈")이 켜진 뒤에도 그 앞에서
 return 하므로, 점수만 넘으면 120일선 아래여도 '매수'가 된다("얼리 스테이지 반등 포함" —
 의도된 설계). 실매매 진입 경로(auto_trade/engine)에도 EMA120 참조는 없다.

[교차 실측 2026-08-29 · 44종목 5년, 51,850관측] 60일 전방수익
      노랑 전체        +2.75% (승률 48.9%)
      노랑 & 매수      +4.14% (51.6%)   ← 상태 필터가 노랑 안에서 실제로 고른다
      비노랑 & 매수   +10.48% (57.7%)   ← 그래도 여전히 절반 이하, 꼬리도 얇다
      노랑 & 매수 & 120선 아래  -0.47% (46.8%)  ← 유일한 음수 구간
 즉 색과 상태는 모순이 아니지만, **'노랑 × 매수 × 120일선 아래'** 한 구간만은 신호 단위
 통계가 음수다. 이미 점수 항목(시너지)에는 `macd>0 AND price>ema120` 게이트가 있는데
 상태·진입 경로에는 없다 — 같은 방어가 한쪽에만 있다.

[그런데 신호 통계는 채택 근거가 아니다] 연도별로 쪼개면 6창 중 2창이 뒤집힌다. 그래서
 이 도구는 채택 규칙이 요구하는 세 가지를 전부 건다.
  A) 신호 성질 — 위 교차표를 재현 가능하게. 창을 나눠 하위기간 견고성까지 본다.
  B) 무작위 대조 — **같은 차단율**로 아무 근거 없이 후보를 버리는 팔. 진입 필터의 이득은
     대개 '순위를 흔든 것'이지 '옳게 걸렀다'가 아니다([[entry-filter-random-control]]).
     창마다 실측 차단율을 재서 그 비율로 맞춘다.
  C) 포트폴리오 짝비교 — 같은 표본·같은 창에서 게이트 유무만 바꾼다. 여러 씨드·여러 창의
     승패로만 판정한다([[audit-seed-robustness]]).
  D) **색 규칙 자체의 분해** — 위 셋은 '매수 신호'만 다루지만, 색은 매 봉 화면에 뜨는
     1차 판독 수단이다. 노랑이 갈리는 근본 이유는 규칙이 **데드캣 되돌림**과 **바닥 탈출
     초기**를 구분하지 못한다는 데 있다(둘 다 EMA20<EMA60 & 현재가>EMA20 으로 똑같이
     보인다). 색마다 후보 보조축(120일선·5일선 정렬·52주 위치·ADX·DMI)을 하나씩 걸어
     **얼마나 크게, 얼마나 일관되게 가르는지**를 재서 규칙 개선안을 고른다.

[게이트 두 갈래]
  G1  현재가 < EMA120 이면 후보에서 뺀다        — 색과 무관한 일반 다이얼(차단율 큼)
  G2  노랑 & 현재가 < EMA120 이면 뺀다          — 실측이 가리킨 좁은 표적
 G1 이 G2 만큼 좋으면 좁은 표적은 불필요하다(단순한 쪽을 택한다). G2 만 좋으면 '색이
 정보를 더한다'가 되고, 둘 다 무작위 대조를 못 이기면 축을 닫는다.

[실행] python3 tools/audit_state_ma120_gate.py --only A,B,C,D --trials 8 --seeds 20260829,7,101

[결과 2026-08-29 · 44종목 10년 · 표본25 · 8회 × 씨드 3]
  결론 1  모순 아니다 — 노랑 전체 60일 +2.71% → '노랑 & 매수' +4.43%. 상태 필터가 노랑
          안에서 실제로 고른다. 다만 매수 신호 전체 +7.81% 에는 못 미치고 꼬리도 얇다.
  결론 2  **G1·G2 둘 다 기각.** 2년 창 5개 3승2패, MDD 5창 중 2창 악화(방어 예외 미달),
          무작위 대조 순위가 창별로 6/6(꼴찌)·5/6 까지 섞인다. G1≈G2 라 색 정보가 더하는
          것도 없다. 차단율 4.1%/3.7% 로 애초에 작은 레버였다. **축 종결 — 재제안 불필요.**
  결론 3  색 규칙 쪽에 진짜 구멍이 있다 — `120일선 위`는 네 색 모두 부호가 같고 red 에서
          유일하게 5/5 창 일치(+4.90%p). 그런데 red 는 "완벽한 정배열"이라 불리면서
          EMA120 을 보지 않는다. 개선 후보는 진입 게이트가 아니라 **색 승격 조건**이다.
  결론 4  노랑은 데드캣과 바닥 탈출 초기를 섞는다. 가르는 축은 52주 위치(+4.91%p·4/5).
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_common import seed_notice  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, entry_days, metrics, new_scale_fn_factory,
)

# 색상 판정은 화면과 같은 규칙을 써야 한다. analysis.price_trend_color 는 dict(ind)를
#  받으므로 백테스트 프레임의 행을 그 모양으로 넘긴다 — 색 규칙을 여기서 다시 쓰면
#  화면과 감사가 서로 다른 '노랑'을 재게 된다.
from core.indicators import EMA20_SLOPE_LOOKBACK  # noqa: E402
from modules import analysis  # noqa: E402


def color_maps(dfs):
    """{code: {date: 색이름}} 과 {code: {date: 120일선 아래 여부}}."""
    colors, below = {}, {}
    for code, df in dfs.items():
        rec = df.to_dict("records")
        e20 = [r.get("EMA20") for r in rec]
        cmap, bmap = {}, {}
        for i, r in enumerate(rec):
            d = str(r["date"])
            ref = e20[max(0, i - EMA20_SLOPE_LOOKBACK)]
            tag = analysis.price_trend_color(
                r.get("close"), r.get("EMA20"), r.get("EMA60"),
                ind={"ema_5": r.get("EMA5"), "ema_20_slope_ref": ref})
            cmap[d] = tag.strip("[]")
            e120 = r.get("EMA120")
            bmap[d] = bool(e120 is not None and e120 == e120 and r["close"] < e120)
        colors[code], below[code] = cmap, bmap
    return colors, below


def fwd(rec, i, h):
    return (rec[i + h]["close"] / rec[i]["close"] - 1) * 100 if i + h < len(rec) else None


def summarize(vals):
    a = np.array([v for v in vals if v is not None], dtype=float)
    if a.size == 0:
        return None
    return dict(n=a.size, mean=a.mean(), med=float(np.median(a)),
                win=float((a > 0).mean() * 100), p90=float(np.percentile(a, 90)))


def line(label, s):
    if not s:
        return f"  {label:<26}   표본 없음"
    return (f"  {label:<26}{s['n']:>7,d}{s['mean']:>10.2f}{s['med']:>9.2f}"
            f"{s['win']:>8.1f}{s['p90']:>10.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="A,B,C,D")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seeds", default="20260829,7,101")
    ap.add_argument("--draws", type=int, default=5, help="무작위 대조 장수(창마다)")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260829,7,101")
    only = args.only.split(",")
    seeds = [int(x) for x in args.seeds.split(",")]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    base = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(base)}종목 · {args.days}일 · 슬롯 {slots}", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(base, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 · 거래일 {len(dates)}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    colors, below = color_maps(dfs)
    new_scale = new_scale_fn_factory(dates, args.days)

    # 창: 전체 + 2년 구간. 한 창의 결과는 복리 경로의 추첨이라 창을 나눠야 판정이 선다.
    bounds = ["20160101", "20180101", "20200101", "20220101", "20240101", "20270101"]
    W = [("전체", list(dates))]
    W += [(f"{bounds[k][2:6]}~{bounds[k + 1][2:6]}",
           [d for d in dates if bounds[k] <= d < bounds[k + 1]]) for k in range(len(bounds) - 1)]
    W = [(n, d) for n, d in W if len(d) > 120]

    # 게이트 — entry_gate(day, code, held) -> True 면 그날 그 종목을 후보에서 뺀다.
    def g1(day, code, _held):
        return below.get(code, {}).get(day, False)

    def g2(day, code, _held):
        return below.get(code, {}).get(day, False) and colors.get(code, {}).get(day) == "yellow"

    # ---------------- A) 신호 성질 ----------------
    if "A" in only:
        sig = entry_days(dfs, status, mf)
        print("\n[A] 진입 신호일의 전방수익 — 색상 × 120일선 교차")
        for h in (20, 60):
            buckets = {}
            for code, (rec, hits) in sig.items():
                cm, bm = colors[code], below[code]
                for i in hits:
                    d = str(rec[i]["date"])
                    c = cm.get(d, "dim")
                    v = fwd(rec, i, h)
                    buckets.setdefault(("색", c), []).append(v)
                    key = ("교차", f"{'노랑' if c == 'yellow' else '비노랑'}"
                                   f" · 120선 {'아래' if bm.get(d) else '위'}")
                    buckets.setdefault(key, []).append(v)
                    buckets.setdefault(("전체", "매수 신호 전체"), []).append(v)
            print(f"\n  ── {h}일 전방 {'':<10}{'건수':>9}{'평균%':>10}{'중앙%':>9}"
                  f"{'승률%':>8}{'상위10%':>10}")
            for grp in ("전체", "색", "교차"):
                for (g, lab), vals in sorted(buckets.items()):
                    if g == grp:
                        print(line(lab, summarize(vals)))

        # 하위기간 — 신호 통계도 창을 나눠 본다. 평균 하나로는 판정하지 않는다.
        print("\n[A-창] 60일 전방 평균 — 창별 (건수)")
        print(f"  {'창':<12}{'매수 전체':>16}{'노랑&매수':>16}{'노랑&매수·120아래':>20}")
        for wn, wd in W:
            ws = set(wd)
            acc = {"all": [], "y": [], "yb": []}
            for code, (rec, hits) in sig.items():
                cm, bm = colors[code], below[code]
                for i in hits:
                    d = str(rec[i]["date"])
                    if d not in ws:
                        continue
                    v = fwd(rec, i, 60)
                    if v is None:
                        continue
                    acc["all"].append(v)
                    if cm.get(d) == "yellow":
                        acc["y"].append(v)
                        if bm.get(d):
                            acc["yb"].append(v)
            f = lambda k: (f"{np.mean(acc[k]):+7.2f}% ({len(acc[k]):4d})"  # noqa: E731
                           if acc[k] else "        -     ")
            print(f"  {wn:<12}{f('all'):>16}{f('y'):>16}{f('yb'):>20}")

    # ---------------- D) 색 규칙 분해 ----------------
    if "D" in only:
        # 전 봉 기준(매수 신호 제한 없음) — 색은 화면의 1차 판독 수단이므로 기저율을 본다.
        allrows = []   # (color, date, fwd60, 보조축들…)
        for code, df in dfs.items():
            rec = df.to_dict("records")
            cm = colors[code]
            for i, r in enumerate(rec):
                d = str(r["date"])
                v = fwd(rec, i, 60)
                if v is None:
                    continue
                e120, e20, e5 = r.get("EMA120"), r.get("EMA20"), r.get("EMA5")
                allrows.append((cm.get(d, "dim"), d, v, {
                    "120일선 위": bool(e120 == e120 and r["close"] > e120),
                    "5일선>20일선": bool(e5 == e5 and e20 == e20 and e5 > e20),
                    "52주 60%↑": (r.get("w52_pos") or 0) >= 60,
                    "ADX 20↑": bool((r.get("ADX") or 0) >= 20),
                    "+DI 우위": bool((r.get("PLUS_DI") or 0) > (r.get("MINUS_DI") or 0)),
                }))
        print("\n[D] 색 규칙 분해 — 전 봉 기준 60일 전방수익")
        print(f"  {'색':<10}{'건수':>8}{'평균%':>9}{'중앙%':>9}{'승률%':>8}{'상위10%':>10}")
        order = ["magenta", "red", "orange3", "yellow", "blue", "white"]
        for c in order:
            sel = [r[2] for r in allrows if r[0] == c]
            s2 = summarize(sel)
            if s2:
                print(f"  {c:<10}{s2['n']:>8,d}{s2['mean']:>9.2f}{s2['med']:>9.2f}"
                      f"{s2['win']:>8.1f}{s2['p90']:>10.2f}")

        axes = ["120일선 위", "5일선>20일선", "52주 60%↑", "ADX 20↑", "+DI 우위"]
        print("\n[D-분리] 색마다 보조축을 걸었을 때 60일 평균이 얼마나 갈리는가")
        print("  (참=축 성립 / 거짓=미성립 · 격차가 크고 창마다 부호가 같아야 규칙에 넣을 값이 있다)")
        print(f"  {'색':<10}{'보조축':<14}{'참 n':>7}{'참%':>8}{'거짓 n':>8}{'거짓%':>8}"
              f"{'격차':>8}{'창 부호일치':>12}")
        for c in order:
            sel = [r for r in allrows if r[0] == c]
            if len(sel) < 500:
                continue
            for ax in axes:
                t = [r[2] for r in sel if r[3][ax]]
                f_ = [r[2] for r in sel if not r[3][ax]]
                if len(t) < 200 or len(f_) < 200:
                    continue
                gap = float(np.mean(t) - np.mean(f_))
                same = 0
                tot = 0
                for wn, wd in W[1:]:
                    ws = set(wd)
                    tt = [r[2] for r in sel if r[3][ax] and r[1] in ws]
                    ff = [r[2] for r in sel if not r[3][ax] and r[1] in ws]
                    if len(tt) < 50 or len(ff) < 50:
                        continue
                    tot += 1
                    if (np.mean(tt) - np.mean(ff)) * gap > 0:
                        same += 1
                print(f"  {c:<10}{ax:<14}{len(t):>7,d}{np.mean(t):>8.2f}{len(f_):>8,d}"
                      f"{np.mean(f_):>8.2f}{gap:>8.2f}{f'{same}/{tot}':>12}")

    if "B" not in only and "C" not in only:
        return

    # ---------------- 표본 ----------------
    codes_all = list(dfs)
    picks = {sd: [random.Random(sd * 7 + i).sample(codes_all, min(args.sample, len(codes_all)))
                  for i in range(args.trials)] for sd in seeds}

    def run(cs, wd, gate):
        return metrics(pb.run_portfolio(
            {c: dfs[c] for c in cs}, {c: status[c] for c in cs}, wd,
            initial_capital=INITIAL_CAPITAL, slots=slots,
            market_filter_dates={c: mf.get(c, set()) for c in cs},
            risk_scale_by_date=new_scale(), entry_gate=gate))

    sig_all = entry_days(dfs, status, mf)   # 창마다 다시 세면 같은 계산을 수십 번 한다

    def block_rate(wd, gate):
        """이 창에서 실제로 차단된 후보-일의 비율. 무작위 대조를 여기에 맞춘다."""
        ws = set(wd)
        sig = sig_all
        tot = hit = 0
        for code, (rec, hits) in sig.items():
            for i in hits:
                d = str(rec[i]["date"])
                if d not in ws:
                    continue
                tot += 1
                if gate(d, code, ()):
                    hit += 1
        return (hit / tot) if tot else 0.0, tot

    # ---------------- C) 포트폴리오 짝비교 ----------------
    if "C" in only:
        print(f"\n[C] 게이트 짝비교 — 표본 {args.sample} · {args.trials}회 × 씨드 {len(seeds)}개")
        print(f"  {'창':<12}{'게이트':<6}{'차단%':>7}{'승':>4}{'무':>4}{'패':>4}"
              f"{'수익 기준':>11}{'수익 게이트':>12}{'차이':>8}"
              f"{'MDD기준':>9}{'MDD게이트':>10}{'꼬리기준':>9}{'꼬리게이트':>10}")
        for wn, wd in W:
            b = [run(p, wd, None) for sd in seeds for p in picks[sd]]
            for gname, gate in (("G1", g1), ("G2", g2)):
                br, _ = block_rate(wd, gate)
                e = [run(p, wd, gate) for sd in seeds for p in picks[sd]]
                win = sum(1 for x, y in zip(e, b) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(e, b) if abs(x["ret"] - y["ret"]) <= 1e-9)
                m = lambda ms, k: float(np.mean([q[k] for q in ms]))  # noqa: E731
                print(f"  {wn:<12}{gname:<6}{br * 100:>7.1f}{win:>4}{tie:>4}{len(b) - win - tie:>4}"
                      f"{m(b, 'ret'):>11.2f}{m(e, 'ret'):>12.2f}{m(e, 'ret') - m(b, 'ret'):>8.2f}"
                      f"{m(b, 'mdd'):>9.2f}{m(e, 'mdd'):>10.2f}"
                      f"{m(b, 'top10'):>9.2f}{m(e, 'top10'):>10.2f}", flush=True)

    # ---------------- B) 무작위 대조 ----------------
    if "B" in only:
        print(f"\n[B] 같은 차단율 무작위 대조 — 장 {args.draws}개 (창마다 차단율 실측)")
        print("  게이트가 무작위 분포 안에 앉으면 '옳게 걸렀다'가 아니라 '순위를 흔들었다'다.")
        print(f"  {'창':<12}{'게이트':<6}{'차단%':>7}{'게이트%':>9}"
              f"{'무작위 평균':>12}{'최소':>9}{'최대':>9}{'순위':>7}")
        for wn, wd in W:
            for gname, gate in (("G1", g1), ("G2", g2)):
                br, _ = block_rate(wd, gate)
                if br <= 0:
                    continue
                gret = float(np.mean([run(p, wd, gate)["ret"]
                                      for sd in seeds for p in picks[sd]]))
                per = []
                for j in range(args.draws):
                    salt = f"ma120|{gname}|{wn}|{j}"

                    def rg(day, code, _held, _s=salt, _r=br):
                        return random.Random(f"{_s}|{day}|{code}").random() < _r
                    per.append(float(np.mean([run(p, wd, rg)["ret"]
                                              for sd in seeds for p in picks[sd]])))
                a = np.array(per)
                rank = int((a > gret).sum()) + 1      # 1 = 게이트가 최고
                print(f"  {wn:<12}{gname:<6}{br * 100:>7.1f}{gret:>9.2f}"
                      f"{a.mean():>12.2f}{a.min():>9.2f}{a.max():>9.2f}"
                      f"{rank:>4}/{len(a) + 1}", flush=True)


if __name__ == "__main__":
    main()
