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

[측정 함정 2 · 후보 압력] 순위는 후보가 슬롯보다 많을 때만 의미가 있다. 남는 슬롯에
 후보가 늘 1개뿐이면 어떤 잣대를 써도 결과가 같다. 그래서 '빈 슬롯이 있던 날의 평균
 후보 수'와 '경쟁이 실제로 있었던 날의 비율'을 함께 찍는다 — 이 값이 낮으면 A·B의
 무승부는 '잣대가 무용'이 아니라 '표본에 경쟁이 없었다'는 뜻이다.

[판정 잣대] 기존 결정과 같다. 총수익·MDD뿐 아니라 상위10%·최대·>30%로 fat-tail을
 보고, 하위 구간 다수에서 이기지 못하면 채택하지 않는다.

[실행] python tools/audit_scoring_weights.py [--trials 15] [--sample 25] [--only A]
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
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


class Counter:
    """빈 슬롯이 있던 날의 후보 수를 센다.

    run_portfolio는 후보 정렬 시 key를 원소마다 정확히 한 번 부른다. 그 호출 수가
    곧 그날 경쟁한 후보 수다(슬롯이 꽉 찬 날은 후보 블록 자체를 건너뛰므로 안 잡힌다).
    """

    def __init__(self, inner):
        self.inner = inner
        self.per_day = {}

    def __call__(self, score, code, row, day):
        self.per_day[day] = self.per_day.get(day, 0) + 1
        return score if self.inner is None else self.inner(score, code, row, day)

    def stats(self, slots_free_hint=1):
        n = list(self.per_day.values())
        if not n:
            return 0.0, 0.0
        return float(np.mean(n)), sum(1 for x in n if x > slots_free_hint) / len(n) * 100


def metrics(r, cand_mean=0.0, cand_comp=0.0):
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
        "cand": cand_mean, "comp": cand_comp,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--only", default=None, help="A(가중치 형태) 또는 B(순위 잣대)")
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
    scale_fn = make_scale_fn(mkt, dd)

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]
    print(f"[창] 검증 {len(head)}일 (~{head[-1] if head else '-'}) · 제외 {len(tail)}일")

    # (그룹, 라벨, 가중치 라벨, rank_fn)
    sets = []
    for label, _w in wsets:
        sets.append(("A. 가중치 형태 (합 10.0 정규화)", label, label, None))
    for label, fn in rank_sets(args.seed):
        sets.append(("B. 순위 잣대 자체 (가중치 현행 고정)", label, wsets[0][0], fn))
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]

    codes = list(dfs.keys())
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    all_results = {}
    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _wl, _f in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for g, label, wlabel, fn in sets:
                st = {c: status_by_w[wlabel][c] for c in pick}
                ctr = Counter(fn)
                r = pb.run_portfolio(sd, st, wdates, initial_capital=INITIAL_CAPITAL,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=scale_fn, rank_fn=ctr)
                cm, cc = ctr.stats()
                results[(g, label)].append(metrics(r, cm, cc))
            print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 108
    print(f"\n{'=' * W}")
    print(f"스코어링 가중치·순위 잣대 ({args.trials}회 × {args.sample}종목 짝비교)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        last_group = None
        for g, label, _wl, _f in sets:
            if g != last_group:
                base_label = next(l for gg, l, _x, _y in sets if gg == g)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<20}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
                      f"{'청산':>6}{'보유일':>7}{'상위10%':>9}{'최대':>9}{'>30%':>6}"
                      f"{'후보':>6}{'경쟁%':>7}{'승-무-패':>10}{'MAR승':>7}{'꼬리승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda key: float(np.median([x[key] for x in rs]))  # noqa: E731
            is_base = label == base_label
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
            print(f"{label:<20}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('win'):>7.1f}{m('n'):>6.0f}{m('days'):>7.0f}{m('top10'):>9.1f}"
                  f"{m('best'):>9.1f}{m('big'):>6.0f}{m('cand'):>6.1f}{m('comp'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>8}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}"
                  f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("후보 = 빈 슬롯이 있던 날의 평균 후보 수 · 경쟁% = 후보가 2개 이상이던 날의 비율.")
    print("[읽는 법] 경쟁%가 낮으면 순위 잣대의 무승부는 '잣대 무용'이 아니라 '경쟁 부재'다.")
    print("[읽는 법] B에서 역순이 현행만큼 좋다면, 점수는 게이트로만 값을 하고 순위로는 못 한다.")


if __name__ == "__main__":
    main()
