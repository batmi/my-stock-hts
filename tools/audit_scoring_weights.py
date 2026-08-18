"""스코어링 가중치 검증 — 4개 슬롯에 무엇이 들어갈지 정하는 잣대에 근거가 있는가.

[왜 지금] tools/에 감사 도구가 27개 있는데 스코어링을 통째로 잰 것은 하나도 없다.
 청산 다이얼은 열 개 넘게 반복 측정해 대부분 '현행 유지'로 확정했는데, 정작 진입
 종목을 고르는 4개 숫자(TREND 4.0 / MOMENTUM 2.5 / STRENGTH 1.5 / SYNERGY 2.0)에는
 개별 수정 기록(데드캣 시너지 게이트·가격모멘텀 신설)만 있고 형태 자체의 실증이 없다.
 "강추세 종목 선별 최우선"이 이 시스템의 1번 원칙인데 그 선별기가 미검증이다.

[두 개의 다른 질문] 섞으면 안 된다.
  A) 가중치 **형태** — 추세를 더 키우면 나아지는가. 모멘텀(RSI·CCI)은 역추세 성격의
     오실레이터인데 추세추종 점수에 2.5점을 쓰는 것이 맞는가.
  B) 점수라는 **잣대 자체** — 후보가 슬롯보다 많을 때 점수순으로 고르는 것이
     무작위보다 나은가. 이 질문에 지면 A는 의미가 없다(무엇을 넣어도 같으므로).
     대조군을 무작위·역순(최저점 우선) 둘 다 세운다. 역순이 확실히 나쁘지 않다면
     점수는 순위 정보를 담고 있지 않다는 뜻이다.

[측정 함정 1 · 총점 스케일] BUY_SCORE(7.0)·RISE_SCORE(6.0)·SELL_SCORE(4.0)·
 SUPER_MOMENTUM_SCORE(8.0)가 모두 '10점 만점' 위의 절대값이다. TREND만 4.0 → 6.0으로
 올리면 만점이 12로 커져 같은 BUY_SCORE가 **더 헐거운 문턱**이 된다. 그러면 형태의
 효과와 문턱 완화의 효과가 섞인다. 그래서 모든 후보를 **합 10.0으로 정규화**한다.

  C) **실매매가 쓰는 순위**(2026-08-12 추가) — A·B는 '점수가 슬롯 주인을 정한다'는 전제
     위에서 쟀는데, 실매매는 그렇게 사지 않는다. trader.candidate_priority_key는
     추세품질(연환산 기울기 × R²)로 먼저 줄을 세우고 점수는 동점을 가르는 2순위다.
     B의 기준선이 실매매가 쓰지 않는 잣대이므로, 실매매의 1순위를 같은 잣대로 재
     맞대본다. 여기서 갈리면 A·B 결론의 사정범위를 그만큼 좁혀 적어야 한다.

[측정 함정 2 · 후보 압력] 순위는 후보가 **남은 슬롯보다** 많을 때만 의미가 있다. 빈 슬롯이
 3칸인데 후보가 2개면 순서를 어떻게 매겨도 셋 다 산다. 그래서 run_portfolio(probe_fn=...)
 으로 그날의 후보 수와 남은 슬롯 수를 함께 받아 '진짜 경쟁일' 비율을 센다 — 이 값이
 낮으면 A·B·C의 무승부는 '잣대가 무용'이 아니라 '표본에 경쟁이 없었다'는 뜻이다.

[측정 함정 3 · 동점] 점수는 이진 신호 합산에 0.1 반올림이라 동점이 흔하다. 백테스트는
 동점을 관심종목 등록 순서로 가르므로, 마지막 슬롯이 동점으로 갈린 날의 비율만큼은
 '순위의 값'이 아니라 임의 상수의 몫이다. 경계 동점률을 함께 찍어 그 몫을 분리한다.

[판정 잣대] 기존 결정과 같다. 총수익·MDD뿐 아니라 상위10%·최대·>30%로 fat-tail을
 보고, 하위 구간 다수에서 이기지 못하면 채택하지 않는다.

[실행] python tools/audit_scoring_weights.py [--trials 15] [--sample 25] [--only A|B|C]
"""
import argparse
import os
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import indicators  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
SELL_REASONS = ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "이익보호")
BASE_W = {"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}


def norm(trend, mom, strength, syn):
    """합을 10.0으로 맞춘다 — 형태만 바꾸고 문턱(BUY_SCORE 등)의 상대 위치는 고정한다."""
    raw = {"TREND": trend, "MOMENTUM": mom, "STRENGTH": strength, "SYNERGY": syn}
    total = sum(raw.values())
    return {k: round(v * 10.0 / total, 4) for k, v in raw.items()}


def weight_sets():
    """(라벨, 가중치). 첫 항목이 기준선."""
    return [
        ("현행 4/2.5/1.5/2", dict(BASE_W)),
        # 추세 비중을 단계적으로 올린다. 원칙("강추세 선별 최우선")이 맞다면 여기서 나와야 한다.
        ("추세↑ 5.5/2/1.5/1", norm(5.5, 2.0, 1.5, 1.0)),
        ("추세↑↑ 7/1.5/1/0.5", norm(7.0, 1.5, 1.0, 0.5)),
        ("추세 단독 10/0/0/0", norm(10.0, 0.0, 0.0, 0.0)),
        # 모멘텀(RSI·CCI)은 오실레이터라 추세추종과 방향이 어긋날 수 있다. 뺐을 때를 본다.
        ("모멘텀 제거 5/0/2/3", norm(5.0, 0.0, 2.0, 3.0)),
        # 강도(ADX·OBV)는 '추세의 힘'이라 원칙상 추세와 같은 편이다. 키워 본다.
        ("강도↑ 3.5/2/3/1.5", norm(3.5, 2.0, 3.0, 1.5)),
        # 시너지는 지표 동조화 가산점 — 2026-07-14에 게이트를 붙인 뒤 형태를 잰 적이 없다.
        ("시너지↑ 3.5/2/1/3.5", norm(3.5, 2.0, 1.0, 3.5)),
        # 형태가 무의미한지 보는 대조군.
        ("균등 2.5×4", norm(2.5, 2.5, 2.5, 2.5)),
    ]


def rank_sets(seed):
    """(라벨, rank_fn). 점수라는 잣대 자체를 묻는다 — 게이트는 그대로, 순서만 바꾼다."""
    rng = random.Random(seed)
    return [
        ("점수순 (현행)", None),
        # 무작위: 순위 정보를 0으로 만든다. 현행과 비기면 점수는 순위로서 값을 못 한다.
        ("무작위", lambda s, c, r, d: rng.random()),
        # 역순: 정보가 있다면 확실히 나빠야 한다. 안 나빠지면 점수는 순위 정보가 없다.
        ("역순(최저점 우선)", lambda s, c, r, d: -s),
        # [참고] 저변동 우선 — 점수와 무관한 다른 잣대도 하나 세워, 무승부가
        #  '어떤 잣대든 같다'인지 '점수만 무용'인지 가른다.
        ("저ATR 우선", lambda s, c, r, d: -(r.get("ATR", 0) / max(r["close"], 1))),
    ]


def rank_sets_d(tq_by_lb):
    """(라벨, rank_fn) — 동점을 가르는 추세품질의 **룩백**을 스윕한다.

    [왜] 2026-08-12 변경으로 추세품질은 1순위에서 '점수 동점 가름'으로 내려왔다. 그런데
     룩백 90일은 도입 당시 임의로 정한 값이고 한 번도 실측된 적이 없다. 1순위에서 진 것이
     '추세품질이 무용해서'인지 '90일이 틀려서'인지도 아직 갈리지 않았다. 동점 가름 자리에서
     룩백만 바꿔 재면 두 질문이 함께 풀린다 — 어떤 룩백에서도 개선이 없으면 90일은 유지고,
     특정 룩백이 뚜렷하면 그 값이 답이다.
    """
    NEG = float("-inf")

    def make(lb):
        tq_by_code = tq_by_lb[lb]

        def key(s, c, r, d):
            v = tq_by_code.get(c, {}).get(d)
            return (s, NEG if v is None else v)
        return key

    out = [("점수순 (동점=등록순서)", None)]
    for lb in sorted(tq_by_lb):
        out.append((f"점수→추세품질 {lb}일", make(lb)))
    return out


def rank_sets_c(tq_by_code):
    """(라벨, rank_fn) — 백테스트의 순위(점수)와 **실매매의 순위(추세품질)** 를 맞대본다.

    [왜 이 그룹이 필요한가] B그룹은 '점수순 vs 무작위 vs 역순'을 재서 순위에 정보가
     있다는 것까지 보였다. 그런데 실매매가 실제로 쓰는 1순위 키는 점수가 아니다 —
     trader.candidate_priority_key는 추세품질(연환산 기울기 × R²)로 먼저 줄을 세우고
     점수는 그 동점을 가르는 2순위다. 즉 B의 기준선('점수순')은 실매매가 쓰지 않는
     잣대이고, 실매매의 1순위는 한 번도 측정된 적이 없다. 여기서 그 공백을 메운다.

    [점수→추세품질] 점수는 이진 신호 합산에 0.1 반올림이라 동점이 흔하고, 백테스트는
     그 동점을 관심종목 등록 순서로 가른다. 이 후보는 '순서를 임의 상수 대신 추세품질로
     가르면 나아지는가'만 따로 묻는다 — 1순위를 바꾸는 것과는 다른 질문이다.
    """
    NEG = float("-inf")

    def tq(code, day):
        v = tq_by_code.get(code, {}).get(day)
        return NEG if v is None else v   # 이력부족은 실매매와 같이 최하순위

    return [
        ("점수순 (백테스트 전제)", None),
        ("추세품질순", lambda s, c, r, d: tq(c, d)),
        # 실매매 복제. 체결강도는 실시간 값이라 백테스트에 없어 3순위(52주 위치)까지만 맞춘다.
        ("추세품질→점수 (실매매)",
         lambda s, c, r, d: (tq(c, d), s, float(r.get("w52_pos", 0.0) or 0.0))),
        ("점수→추세품질(동점만)", lambda s, c, r, d: (s, tq(c, d))),
    ]


class Probe:
    """매수 직전의 후보 목록과 남은 슬롯 수를 받아 순위 실험의 **타당성**을 계측한다.

    [왜 필요한가] 순위는 후보가 남은 슬롯보다 많을 때만 의미가 있다. 종전에는 rank_fn의
     호출 횟수로 후보 수만 셌고, 남은 슬롯 수를 알 길이 없어 '후보 2개 이상'을 경쟁으로
     어림했다 — 빈 슬롯이 3칸인데 후보가 2개인 날도 경쟁으로 세는 과대 계측이다.
     run_portfolio(probe_fn=...)이 남은 슬롯 수를 함께 주므로 이제 정확히 센다.

    [경계 동점] 후보가 슬롯보다 많은 날, 마지막 자리를 두고 겨루는 두 후보(정렬 후
     free-1번째와 free번째)의 **정렬키가 같으면** 슬롯 주인은 순위가 아니라 동점 처리,
     곧 관심종목 등록 순서라는 임의 상수가 정한다. 점수는 이진 신호 합산에 0.1 반올림이라
     동점이 흔하다 — 이 비율을 모르면 '순위의 값'에 임의 상수의 몫이 섞인 채로 읽게 된다.
    """

    def __init__(self, key=None):
        # key(score, code, row, day) -> 정렬키. None이면 점수(현행 정렬과 동일).
        self.key = key or (lambda s, c, r, d: s)
        self.n = []          # 빈 슬롯이 있던 날의 후보 수
        self.compete = 0     # 후보 > 빈 슬롯이던 날
        self.tie = 0         # 그중 경계 동점으로 갈린 날

    def __call__(self, day, candidates, free):
        self.n.append(len(candidates))
        if len(candidates) > free >= 1:
            self.compete += 1
            a = self.key(*candidates[free - 1], day)
            b = self.key(*candidates[free], day)
            if a == b:
                self.tie += 1

    def stats(self):
        if not self.n:
            return 0.0, 0.0, 0.0
        comp = self.compete / len(self.n) * 100
        tie = self.tie / self.compete * 100 if self.compete else 0.0
        return float(np.mean(self.n)), comp, tie


def rolling_trend_quality(df, lookback):
    """indicators.trend_quality_map 재수출 — {일자: 추세품질}. 산식은 지표 계층 한 곳.

    [2026-08-18] 백테스트 엔진이 실매매 동점 가름을 기본값으로 재현하게 되면서 원본을
     indicators로 올렸다. 이 이름으로 import하는 도구가 여럿이라 자리는 남긴다.
    """
    return indicators.trend_quality_map(df, lookback)


def verify_tq_parity(dfs, tq_by_code, lookback, sample=8):
    """마지막 시점 값을 indicators.get_trend_quality와 대조한다(불일치 건수를 돌려준다)."""
    return indicators.verify_trend_quality_parity(dfs, lookback, sample=sample)


def metrics(r, cand_mean=0.0, cand_comp=0.0, tie_pct=0.0):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    wins = [p for p in profits if p > 0]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"],
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "win": len(wins) / len(profits) * 100 if profits else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "n": len(sells),
        "cand": cand_mean, "comp": cand_comp, "tie": tie_pct,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--only", default=None,
                    help="A(가중치 형태)·B(순위 잣대)·C(실매매 순위 대조)·D(동점 가름 룩백)")
    ap.add_argument("--tq-lookbacks", default="60,90,120,180", help="D그룹 룩백 목록(쉼표)")
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

    def thr_for(weights):
        return {
            "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
            "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
            "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
            "WEIGHTS": weights,
        }

    # 가중치를 바꾸면 점수·상태가 통째로 달라지므로 후보마다 상태를 다시 깐다(시행별이 아니라
    # 후보별로 한 번씩만 — 상태는 하루 단위로 결정되고 표본 추출과 무관하다).
    wsets = weight_sets()
    status_by_w = {}
    for label, w in wsets:
        print(f"  상태 사전계산: {label}          ", end="\r", flush=True)
        status_by_w[label] = pb.precompute_status(dfs, thr_for(w))
    print(" " * 60, end="\r")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    # [실행마다 새로 만든다 — 2026-08-13] make_scale_fn 이 돌려주는 콜러블은 내부에 자산곡선
    #  이력(hist)을 들고 있다. 하나를 만들어 모든 팔·시행에 돌려쓰면 앞선 실행의 자산곡선이
    #  남아 드로다운 판정이 오염되고, 팔의 실행 순서에 따라 결과가 달라진다(같은 설정을 두 번
    #  돌리면 392.66% → 284.79%). 짝비교의 전제가 깨지므로 팩토리로 두고 매번 새로 만든다.
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]
    print(f"[창] 검증 {len(head)}일 (~{head[-1] if head else '-'}) · 제외 {len(tail)}일")

    lookback = int(config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90))
    want_c = (not args.only) or args.only.upper().startswith("C")
    want_d = (not args.only) or args.only.upper().startswith("D")
    lookbacks = [int(x) for x in str(args.tq_lookbacks).split(",") if x.strip()]
    tq_by_lb = {}
    if want_d:
        tq_by_lb = {lb: {c: rolling_trend_quality(df, lb) for c, df in dfs.items()}
                    for lb in lookbacks}
    tq_by_code = {}
    if want_c:
        tq_by_code = {c: rolling_trend_quality(df, lookback) for c, df in dfs.items()}
        bad = verify_tq_parity(dfs, tq_by_code, lookback)
        print(f"[검증] 추세품질 산식 대조(마지막 시점 8종목): 불일치 {bad}건"
              + ("  ← 산식이 실매매와 어긋난다. C그룹 결과를 쓰면 안 된다." if bad else " (실매매와 동일)"))

    # (그룹, 라벨, 가중치 라벨, rank_fn)
    sets = []
    for label, _w in wsets:
        sets.append(("A. 가중치 형태 (합 10.0 정규화)", label, label, None))
    for label, fn in rank_sets(args.seed):
        sets.append(("B. 순위 잣대 자체 (가중치 현행 고정)", label, wsets[0][0], fn))
    if want_c:
        for label, fn in rank_sets_c(tq_by_code):
            sets.append(("C. 실매매 순위(추세품질) 대조", label, wsets[0][0], fn))
    if want_d:
        for label, fn in rank_sets_d(tq_by_lb):
            sets.append(("D. 동점 가름 추세품질 룩백", label, wsets[0][0], fn))
    if args.only:
        sets = [x for x in sets if x[0].upper().startswith(args.only.upper())]

    codes = list(dfs.keys())
    k = max(1, args.subperiods)

    def build_windows(hd):
        size = max(1, len(hd) // k)
        out = [("제외 전 전체", hd)]
        if k > 1:
            out += [(f"구간{i + 1}", hd[i * size:(i + 1) * size if i < k - 1 else len(hd)])
                    for i in range(k)]
        if tail:
            out.append(("[대조] 제외구간(고변동)", tail))
        return out

    groups = []
    for g, label, wlabel, fn in sets:
        if not groups or groups[-1][0] != g:
            groups.append((g, []))
        groups[-1][1].append((label, wlabel, fn))

    # [C그룹만 창을 자른다] 추세품질은 90일 이력이 있어야 값이 나온다. 앞 89일은 모든
    #  후보가 '이력부족'이라 순위가 통째로 등록 순서로 정해진다 — 그 구간을 넣으면
    #  실매매 순위를 재는 것이 아니라 등록 순서를 재게 된다. 같은 그룹의 기준선(점수순)도
    #  같은 창에서 돌리므로 짝비교는 그대로 유효하다.
    warm = {"C": lookback - 1, "D": (max(lookbacks) - 1 if lookbacks else 0)}
    head_of = {g: head[warm.get(g[0], 0):] for g, _m in groups}

    all_results = {}
    for g, members in groups:
        for wname, wdates in build_windows(head_of[g]):
            res = {label: [] for label, _wl, _f in members}
            rng = random.Random(args.seed)
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for label, wlabel, fn in members:
                    st = {c: status_by_w[wlabel][c] for c in pick}
                    probe = Probe(fn)
                    r = pb.run_portfolio(sd, st, wdates, initial_capital=INITIAL_CAPITAL,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=new_scale_fn(), rank_fn=fn,
                                         probe_fn=probe)
                    cm, cc, tie = probe.stats()
                    res[label].append(metrics(r, cm, cc, tie))
                print(f"  {g[:2]} {wname} 시행 {t + 1}/{args.trials}   ", end="\r", flush=True)
            all_results[(g, wname)] = res
    print(" " * 60, end="\r")

    W = 116
    print(f"\n{'=' * W}")
    print(f"스코어링 가중치·순위 잣대 ({args.trials}회 × {args.sample}종목 짝비교)")
    print(f"{'=' * W}")
    for g, members in groups:
        base_label = members[0][0]
        print(f"\n\n{'#' * W}\n{g}  (기준선: {base_label})\n{'#' * W}")
        for wname, wdates in build_windows(head_of[g]):
            results = all_results[(g, wname)]
            base = results[base_label]
            print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
            print(f"{'설정':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
                  f"{'청산':>6}{'보유일':>7}{'상위10%':>9}{'최대':>9}{'>30%':>6}"
                  f"{'후보':>6}{'경쟁%':>7}{'동점%':>7}{'승-무-패':>10}{'MAR승':>7}{'꼬리승':>7}")
            print("-" * W)
            for label, _wl, _f in members:
                rs = results[label]
                m = lambda key: float(np.median([x[key] for x in rs]))  # noqa: E731
                is_base = label == base_label
                tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
                los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
                rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
                mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
                tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
                print(f"{label:<24}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                      f"{m('win'):>7.1f}{m('n'):>6.0f}{m('days'):>7.0f}{m('top10'):>9.1f}"
                      f"{m('best'):>9.1f}{m('big'):>6.0f}{m('cand'):>6.1f}{m('comp'):>7.1f}"
                      f"{m('tie'):>7.1f}"
                      f"{'—' if is_base else f'{rw}-{tie}-{los}':>8}"
                      f"{'—' if is_base else f'{mw}/{len(rs)}':>7}"
                      f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("후보 = 빈 슬롯이 있던 날의 평균 후보 수 · 경쟁% = 후보가 **남은 슬롯보다 많던** 날의 비율.")
    print("동점% = 경쟁일 중, 마지막 자리를 두고 겨룬 두 후보의 정렬키가 같아 등록 순서가 주인을 정한 비율.")
    print("[읽는 법] 경쟁%가 낮으면 순위 잣대의 무승부는 '잣대 무용'이 아니라 '경쟁 부재'다.")
    print("[읽는 법] 동점%가 높으면 그 잣대의 성과 중 그만큼은 순위가 아니라 임의 상수의 몫이다.")
    print("[읽는 법] B에서 역순이 현행만큼 좋다면, 점수는 게이트로만 값을 하고 순위로는 못 한다.")
    print("[읽는 법] C의 기준선은 백테스트 전제(점수순)다. '추세품질→점수'가 실매매의 실제 순위다.")


if __name__ == "__main__":
    main()
