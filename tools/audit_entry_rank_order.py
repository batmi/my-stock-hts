"""진입 순위 1순위를 다시 묻는다 — 추세품질이 먼저인가, 점수가 먼저인가.

[이미 판정된 축을 왜 또 재나] 2026-08-12에 '점수 1순위 + 동점만 추세품질'로 바꿨고
 (config SYSTEM_MAX_HOLDINGS 블록의 기록 참조) 재도입 금지로 닫았다. 그 뒤 두 가지가
 바뀌었다.
   ① 거래비용 단일 소스화(2026-08-10)와 증액 시 시간청산 리셋 패리티 수정(2026-08-16)으로
      **이전 백테스트 수치와 직접 비교가 금지된 상태**다. 판정의 근거였던 숫자가 더는
      그 숫자가 아니다.
   ② 계단식 증액 감사(2026-08-17)에서 **크기 신호로는 점수보다 추세품질이 낫다**는
      진단이 나왔다(진입→청산 11,860건: TQ 강함 +0.81% vs 양호 -2.28%, 점수는 비단조).
      크기에서 TQ가 이겼다면 순위에서도 다시 물을 근거가 된다.
 그래서 같은 질문을 **지금 코드로, 씨드 3개로** 다시 던진다. 결론이 같으면 그 재도입
 금지가 최신 코드 위에서 재확인되는 것이고, 뒤집히면 그때 바꾸면 된다.

[함정 하나 — 2026-08-12에 실제로 밟았다] 추세품질은 연속값이라 동점이 사실상 0%다.
 그래서 '추세품질 → 점수'는 '추세품질 단독'과 **모든 창에서 수치가 완전히 같다**.
 2순위인 점수는 슬롯 주인을 한 번도 바꾸지 못한다. 이 도구는 그것을 가정하지 않고
 대조군('추세품질 단독')을 함께 돌려 **매번 확인한다** — 같지 않게 나오면 산식이나
 훅이 잘못된 것이다.

[정말 재는 것] 순위는 후보가 남은 슬롯보다 많을 때만 결과를 바꾼다. 그 경쟁이 몇 %의
 날에 일어나는지부터 세고(경쟁률), 그 다음 성과를 본다. 경쟁이 드물면 이 축은 애초에
 큰 레버가 아니다.

[축 2 — 추세품질 하한 게이트] "점수가 7.0을 넘어도 추세품질이 0 미만(회귀선 우하향)이면
 사지 않는다"는 제안. 순위(축 1)와 완전히 다른 질문이다 — 순위는 슬롯 주인을 바꿀 뿐
 후보 집합은 그대로지만, 게이트는 **후보 자체를 없앤다.** 슬롯이 4개뿐이라 후보를 지우면
 그 자리에 다음 후보가 들어오므로, 지워진 진입이 나빴는지뿐 아니라 **대체된 진입이 더
 좋았는지**까지 봐야 한다. 그래서 실현 손익 진단(그 진입들이 실제로 얼마를 벌었나)과
 팔 비교(포트폴리오 전체가 좋아지나)를 함께 돌린다.

[실행] python3 tools/audit_entry_rank_order.py --trials 12 --sample 25
       python3 tools/audit_entry_rank_order.py --axis gate --trials 12 --sample 25
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import seed_notice, windows as audit_windows  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)
from tools.audit_scoring_weights import (  # noqa: E402
    rolling_trend_quality, verify_tq_parity,
)

NEG = float("-inf")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--axis", default="rank")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    axes = [a.strip() for a in args.axis.split(",")]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]

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

    lookback = int(config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90))
    tq_by_code = {c: rolling_trend_quality(df, lookback) for c, df in dfs.items()}
    bad = verify_tq_parity(dfs, tq_by_code, lookback)
    print(f"[검증] 추세품질 산식 대조: 불일치 {bad}건"
          + ("  ← 결과를 쓰면 안 된다." if bad else ""), flush=True)

    def tq(code, day):
        v = tq_by_code.get(code, {}).get(day)
        return NEG if v is None else float(v)

    # 워밍업 구간은 잘라낸다 — 앞부분은 모든 후보가 이력부족이라 순위가 등록 순서로
    #  정해져 비교가 오염된다(2026-08-12 C그룹과 같은 규약).
    dates = dates[lookback - 1:]
    print(f"[창] 추세품질 워밍업 {lookback - 1}일 제외 후 {len(dates)}일", flush=True)

    # ── 이 축이 몇 번이나 결과를 바꾸는가: 후보 수 > 남은 슬롯인 날의 비율
    #  (실제 보유 수를 모르므로 '후보 2개 이상'을 하한으로, '슬롯 초과'를 상한으로 본다)
    n_day, n_two, n_over = 0, 0, 0
    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    for d in dates:
        cands = [c for c in dfs
                 if (st := status.get(c, {}).get(d)) and
                 (float(st[0]) >= buy_score or st[1] == "역매수")]
        n_day += 1
        n_two += len(cands) >= 2
        n_over += len(cands) > slots
    # [주의] 이 수치는 **44종목 전체 유니버스** 기준이고, 팔은 25종목 표본으로 돈다.
    #  또 config 기록의 '경쟁률 11.0%'는 정의가 다르다(운용 중 후보 > **남은** 슬롯).
    #  여기 값과 그 값을 나란히 놓고 비교하면 안 된다 — 순위 축이 개입할 여지의 상한이다.
    print(f"[경쟁·상한] 유니버스 {len(dfs)}종목 기준 후보 2개 이상 {n_two / n_day * 100:.1f}% · "
          f"후보 > 전체슬롯({slots}) {n_over / n_day * 100:.1f}% "
          f"(운용 중 '후보 > 남은 슬롯'과는 다른 정의다)", flush=True)

    # ── 점수 동점은 얼마나 흔한가 (2순위가 일할 기회가 있는지)
    ties, pairs = 0, 0
    for d in dates[::5]:
        sc = sorted(round(float(st[0]), 4) for c in dfs
                    if (st := status.get(c, {}).get(d)) and float(st[0]) >= buy_score)
        pairs += max(0, len(sc) - 1)
        ties += sum(1 for a, b in zip(sc, sc[1:]) if a == b)
    print(f"[동점] 후보 점수 인접쌍 {pairs}개 중 동점 {ties}개 "
          f"({ties / pairs * 100 if pairs else 0:.1f}%)", flush=True)

    if "rank" in axes:
        run_rank_axis(args, dfs, status, mf, dates, slots, seeds, new_scale, tq)
    if "gate" in axes:
        run_gate_axis(args, dfs, status, mf, dates, slots, seeds, new_scale, tq)


def run_rank_axis(args, dfs, status, mf, dates, slots, seeds, new_scale, tq):
    def rank_score_then_tq(s, c, r, d):
        return (s, tq(c, d))                       # 현행 — 점수 1순위

    def rank_tq_then_score(s, c, r, d):
        return (tq(c, d), s)                       # 제안 — 추세품질 1순위

    def rank_tq_only(s, c, r, d):
        return (tq(c, d),)                         # 대조 — 위와 같아야 정상

    def rank_score_only(s, c, r, d):
        return (s,)                                # 대조 — 동점은 등록 순서(종전 상태)

    def rank_tq_then_score_live(s, c, r, d):
        # 실매매 candidate_priority_key와 같은 3단(추세품질 → 점수 → 52주위치)
        return (tq(c, d), s, float(r.get("w52_pos", 0.0) or 0.0))

    arms = [
        ("점수 → 추세품질 (현행)", rank_score_then_tq),
        ("추세품질 → 점수 (제안)", rank_tq_then_score),
        ("추세품질 → 점수 → 52주 (실매매식)", rank_tq_then_score_live),
        ("[대조] 추세품질 단독", rank_tq_only),
        ("[대조] 점수 단독", rank_score_only),
    ]

    W = audit_windows(dates, args.subperiods, whole=True)
    picks = {sd: [random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs)))
                  for i in range(args.trials)] for sd in seeds}

    print(f"\n[순위 잣대] 표본 {args.sample} · {args.trials}회 × 씨드 {len(seeds)}개 "
          f"= {args.trials * len(seeds)}쌍")
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<28}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
        base_res = None
        for label, fn in arms:
            res = []
            for sd in seeds:
                for pick in picks[sd]:
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale(), rank_fn=fn)
                    res.append(metrics(r))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res, wl = res, "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res)
                          if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<28}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                  f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                  f"{wl:>10}", flush=True)

    print("\n[읽는 법] '추세품질 → 점수'와 '[대조] 추세품질 단독'의 수치가 같아야 정상이다"
          " — 다르면 산식이나 훅이 잘못된 것이다. 같다면 그 팔에서 점수는 순위에 아무 일도"
          " 하지 않는다는 뜻이고, 그것이 2026-08-12에 순위를 뒤집은 이유였다.")


def run_gate_axis(args, dfs, status, mf, dates, slots, seeds, new_scale, tq):
    """추세품질 하한 게이트 — '점수는 넘었지만 회귀선이 우하향인 종목'을 후보에서 뺀다."""
    import zlib

    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]

    def band(code, day):
        v = tq(code, day)
        return "이력부족" if np.isneginf(v) else ("하락" if v < 0 else
                                                 "미검증" if v < 10 else "그 외")

    # ── 게이트가 몇 %의 후보를 지우는가 (조건은 며칠에 걸리는지부터 센다)
    cnt = {"하락": 0, "이력부족": 0, "미검증": 0, "그 외": 0}
    for d in dates:
        for c in dfs:
            st = status.get(c, {}).get(d)
            if st and (float(st[0]) >= buy_score or st[1] == "역매수"):
                cnt[band(c, d)] += 1
    tot = sum(cnt.values()) or 1
    print(f"\n\n[2] 추세품질 하한 게이트 — 후보(종목·일) {tot}건 중 "
          + " · ".join(f"{k} {v / tot * 100:.1f}%" for k, v in cnt.items()), flush=True)
    p_drop = cnt["하락"] / tot          # 무작위 대조군의 차단 확률을 여기에 맞춘다

    # ── 진단: 그 진입들이 실제로 얼마를 벌었나
    print("\n[2-진단] 기준선 운용에서 밴드별 실현 손익 (진입→청산 짝)")

    def _is_exit(reason):
        return reason != "매수" and not str(reason).startswith("피라미딩")

    recs = []
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
                            open_day = t["date"]
                    elif open_day is not None:
                        recs.append((band(code, open_day), float(t.get("profit", 0) or 0)))
                        open_day = None
    print(f"   표본 {len(recs)}건")
    print(f"   {'밴드':<12}{'건수':>8}{'비중%':>8}{'평균%':>9}{'중앙%':>9}"
          f"{'상위10%':>9}{'승률%':>8}")
    for lab in ("하락", "이력부족", "미검증", "그 외"):
        seg = [p_ for b, p_ in recs if b == lab]
        if len(seg) < 20:
            print(f"   {lab:<12}{len(seg):>8}   (표본 부족)")
            continue
        a = np.array(seg)
        top = np.sort(a)[::-1][:max(1, len(a) // 10)]
        print(f"   {lab:<12}{len(a):>8}{len(a) / len(recs) * 100:>8.1f}{a.mean():>9.2f}"
              f"{np.median(a):>9.2f}{top.mean():>9.1f}{(a > 0).mean() * 100:>8.1f}")
    print("   [읽는 법] 지워질 밴드가 나쁘다는 것만으로는 부족하다. 슬롯은 4개뿐이라 "
          "지운 자리에 다음 후보가 들어온다 — 대체 진입이 더 나은지는 팔이 답한다.")

    def g_drop_neg(day, code, held):
        v = tq(code, day)
        return (not np.isneginf(v)) and v < 0          # 이력부족은 통과시킨다

    def g_drop_neg_and_none(day, code, held):
        v = tq(code, day)
        return np.isneginf(v) or v < 0

    def g_drop_lt10(day, code, held):
        v = tq(code, day)
        return (not np.isneginf(v)) and v < 10         # '미검증' 이하까지 배제

    def g_random(day, code, held):
        """[대조] 같은 비율을 무작위로 지운다 — 이긴 것이 '골라 지운 것'인지
        '덜 사는 것'인지 가른다. 게이트 팔이 이 팔을 못 이기면 선별에 값이 없다."""
        h = zlib.crc32(f"{day}|{code}".encode()) % 1000000 / 1000000.0
        return h < p_drop

    arms = [
        ("현행 (게이트 없음)", None),
        ("추세품질 <0 배제", g_drop_neg),
        ("추세품질 <0 · 이력부족 배제", g_drop_neg_and_none),
        ("추세품질 <10 배제 (미검증 이하)", g_drop_lt10),
        ("[대조] 같은 비율 무작위 배제", g_random),
    ]

    W = audit_windows(dates, args.subperiods, whole=True)
    picks = {sd: [random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs)))
                  for i in range(args.trials)] for sd in seeds}

    print(f"\n[2-팔] 표본 {args.sample} · {args.trials}회 × 씨드 {len(seeds)}개 "
          f"= {args.trials * len(seeds)}쌍")
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<30}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
        base_res = None
        for label, fn in arms:
            res = []
            for sd in seeds:
                for pick in picks[sd]:
                    kw = {} if fn is None else {"entry_gate": fn}
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale(), **kw)
                    res.append(metrics(r))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res, wl = res, "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res)
                          if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<30}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                  f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                  f"{wl:>10}", flush=True)


if __name__ == "__main__":
    main()
