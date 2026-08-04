"""확정 Bear 국면에서 시장 필터 차단을 해제해야 하는가 — 종목 단위 검증.

[왜 다시] MARKET_FILTER_RELEASE_ON_BEAR 의 기존 근거는 전부 **지수 기준**이다.
  · tools/audit_market_axes.py  : 차단일의 향후 20일 지수 수익률 (Bear 구간 +2.4%)
  · tools/audit_axis_combination.py : 지수에 배수만큼 노출한 가상 자산곡선
둘 다 '지수를 산다'는 가정이라, 슬롯 경쟁·사이징·손절·피라미딩이 만드는 실제 손익과
다르다. config 주석도 "위 수치는 모두 지수 기준이다. 종목 단위 손익은 포트폴리오
백테스트로 확인해야 한다"로 판단을 유보해 두었다. 그 유보를 푸는 도구다.

[무엇을 보는가] 추세추종에서 진입 게이트의 가치는 평균 수익률이 아니라 **fat-tail을
살리는가**로 판정한다. 게이트가 막은 날 중에 큰 추세의 시작점이 섞여 있으면, 평균이
좋아져도 손익을 만드는 상위 거래를 잃어 전략이 망가진다. 그래서 수익·MDD·MAR과 함께
상위10% 청산수익률 · 최대 단일거래 · >30% 거래 수를 같이 본다.

[해석 주의] 이 게이트는 **신규 진입만** 막는다. 보유분 청산은 ATR 손절·샹들리에 TS가
담당하므로, 해제의 비용은 '더 깊은 낙폭'이 아니라 '약세 구간에 진입한 포지션의 손절'로
나타난다. MDD 악화가 곧 기각 사유는 아니고 MAR로 봐야 한다.

[실행] python tools/audit_market_bear_release.py [--days 1095] [--trials 30] [--sample 30]
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import backtest  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 5_000_000   # 실거래 시드와 같게 둔다(seed-slot-sizing)


def _ticker_of(code):
    """prepare_market_filter 와 같은 규칙으로 종목 → 지수 티커."""
    for key in ("stocks_kr", "etfs_kr"):
        for item in config.session.stock_data.get(key, []):
            if item.get("code") == code and item.get("exchange"):
                return "^KQ11" if str(item["exchange"]).upper() == "KOSDAQ" else "^KS11"
    return "^KS11"


def blocked_by_code(codes, days, release_on_bear):
    """{code: 차단일 집합}. 지수는 2개뿐이라 티커별로 한 번씩만 계산한다.

    **production 함수(backtest.prepare_market_filter)를 그대로 호출한다** — 판정식을
    도구에 복제하면 config 를 바꿨을 때 조용히 갈라진다.
    """
    prev = getattr(config, "MARKET_FILTER_RELEASE_ON_BEAR", False)
    config.settings.MARKET_FILTER_RELEASE_ON_BEAR = bool(release_on_bear)
    try:
        per_ticker, out = {}, {}
        for code in codes:
            tk = _ticker_of(code)
            if tk not in per_ticker:
                backtest.prepare_market_filter(code, False, days)
                per_ticker[tk] = set(backtest._MARKET_FILTER_STATE.get("dates") or set())
            out[code] = per_ticker[tk]
        return out, per_ticker
    finally:
        config.settings.MARKET_FILTER_RELEASE_ON_BEAR = prev
        backtest._MARKET_FILTER_STATE["key"] = None   # 다음 호출이 캐시를 재사용하지 않게


def metrics(r):
    """추세추종 관점의 요약. fat-tail 지표를 반드시 포함한다."""
    profits = sorted((t["profit"] for t in r["sells"]), reverse=True)
    n = len(profits)
    top10 = float(np.mean(profits[:max(1, n // 10)])) if n else 0.0
    mdd = r["mdd"]
    return {
        "ret": r["total_return"],
        "mdd": mdd,
        "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
        "pf": r["pf"],
        "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
        "n": n,
        "top10": top10,
        "best": profits[0] if n else 0.0,
        "big30": sum(1 for p in profits if p >= 30),
        "big50": sum(1 for p in profits if p >= 50),
    }


def run_set(dfs, status, dates, mf, slots, codes):
    sub_dfs = {c: dfs[c] for c in codes}
    sub_status = {c: status[c] for c in codes}
    sub_mf = {c: mf.get(c, set()) for c in codes}
    return pb.run_portfolio(sub_dfs, sub_status, dates, initial_capital=INITIAL_CAPITAL,
                            slots=slots, market_filter_dates=sub_mf)


def report(title, off, on):
    """짝비교 결과 출력. off/on 은 같은 표본·같은 기간의 metrics 리스트."""
    med = lambda rs, k: float(np.median([x[k] for x in rs]))  # noqa: E731
    print(f"\n{title}  (n={len(off)}쌍)")
    print(f"  {'':<10}{'수익%':>9}{'MDD%':>8}{'최악MDD':>9}{'MAR':>7}{'PF':>6}{'승률%':>7}"
          f"{'거래':>6}{'상위10%':>9}{'최대':>8}{'>30%':>6}{'>50%':>6}")
    for name, rs in (("현행(차단)", off), ("Bear해제", on)):
        # 최악MDD = 전 시행 중 가장 깊은 낙폭(꼬리). 중앙값만 보면 파산 경로를 놓친다.
        worst = float(np.min([x["mdd"] for x in rs]))
        print(f"  {name:<10}{med(rs, 'ret'):>9.1f}{med(rs, 'mdd'):>8.1f}{worst:>9.1f}"
              f"{med(rs, 'mar'):>7.2f}"
              f"{med(rs, 'pf'):>6.2f}{med(rs, 'wr'):>7.1f}{med(rs, 'n'):>6.0f}"
              f"{med(rs, 'top10'):>9.2f}{med(rs, 'best'):>8.1f}"
              f"{med(rs, 'big30'):>6.0f}{med(rs, 'big50'):>6.0f}")
    w_ret = sum(1 for a, b in zip(on, off) if a["ret"] > b["ret"])
    w_mar = sum(1 for a, b in zip(on, off) if a["mar"] > b["mar"])
    w_mdd = sum(1 for a, b in zip(on, off) if a["mdd"] > b["mdd"])   # mdd 는 음수
    w_t10 = sum(1 for a, b in zip(on, off) if a["top10"] > b["top10"])
    print(f"  해제 승 →  수익 {w_ret}/{len(off)} · MAR {w_mar}/{len(off)} · "
          f"MDD {w_mdd}/{len(off)} · 상위10% {w_t10}/{len(off)}")
    return {"ret": w_ret, "mar": w_mar, "mdd": w_mdd, "top10": w_t10, "n": len(off)}


def walk(dfs, status, dates, mf_off, mf_on, per_off, per_on, slots, codes, win, step):
    """전 종목 고정 · 창을 굴리며 비교. 관측이 '기간'이라 유니버스를 좁히지 않아도 된다.

    창끼리는 시간이 겹치지만 종목 표본 추출과 달리 **실거래와 같은 후보 풀**을 쓴다.
    창별로 해제일 수를 함께 보고, 해제일이 적은 창은 판정에서 제외한다(강세장 = 무차별).
    """
    starts = list(range(0, max(1, len(dates) - win + 1), step))
    print(f"\n{'=' * 104}")
    print(f"확정 Bear 해제 — 워크포워드 (전 {len(codes)}종목 고정 · 창 {win}거래일 · 이동 {step}일 · {len(starts)}창)")
    print(f"{'=' * 104}")
    print(f"{'창 시작':<10}{'해제일':>7}{'수익 차단':>10}{'해제':>9}{'MDD 차단':>10}{'해제':>9}"
          f"{'MAR 차단':>10}{'해제':>9}{'상위10 차단':>12}{'해제':>9}")
    print("-" * 104)

    rows = []
    for s in starts:
        wd = dates[s:s + win]
        seg = set(wd)
        rel = max(len(per_off[tk] & seg) - len(per_on[tk] & seg) for tk in per_off)
        a = metrics(run_set(dfs, status, wd, mf_off, slots, codes))
        b = metrics(run_set(dfs, status, wd, mf_on, slots, codes))
        rows.append((wd[0], rel, a, b))
        print(f"{wd[0]:<10}{rel:>7}{a['ret']:>10.1f}{b['ret']:>9.1f}"
              f"{a['mdd']:>10.1f}{b['mdd']:>9.1f}{a['mar']:>10.2f}{b['mar']:>9.2f}"
              f"{a['top10']:>12.2f}{b['top10']:>9.2f}")

    # 해제일이 30일 미만인 창은 두 설정이 사실상 같은 전략이라 판정에서 뺀다.
    live = [r for r in rows if r[1] >= 30]
    print("-" * 104)
    print(f"판정 대상 창: {len(live)}/{len(rows)} (해제일 30일 이상)")
    if not live:
        return
    for label, key, higher in (("수익", "ret", True), ("MAR", "mar", True),
                               ("MDD", "mdd", True), ("상위10%", "top10", True)):
        w = sum(1 for _d, _r, a, b in live if (b[key] > a[key]) == higher)
        med = float(np.median([b[key] - a[key] for _d, _r, a, b in live]))
        print(f"  {label:<8} 해제 승 {w}/{len(live)}  ·  중앙 차이 {med:+.2f}")
    worst_a = min(a["mdd"] for _d, _r, a, _b in live)
    worst_b = min(b["mdd"] for _d, _r, _a, b in live)
    print(f"  최악MDD  차단 {worst_a:.1f}% → 해제 {worst_b:.1f}%")

    # [핵심] 시장 필터의 존재 이유는 평균이 아니라 **꼬리**다. 상승 창이 표본을 지배하면
    #  중앙값은 좋아 보이면서 정작 막아야 할 하락장에서 무너질 수 있다. 그래서 기준선이
    #  손실인 창(= 게이트가 일해야 하는 창)만 따로 본다.
    bad = [r for r in live if r[2]["ret"] < 0]
    good = [r for r in live if r[2]["ret"] >= 0]
    print(f"\n  [꼬리 분해] 게이트가 일해야 하는 창 = 기준선이 손실인 창")
    for label, grp in (("하락 창", bad), ("상승 창", good)):
        if not grp:
            continue
        d_ret = float(np.median([b["ret"] - a["ret"] for _d, _r, a, b in grp]))
        d_mdd = float(np.median([b["mdd"] - a["mdd"] for _d, _r, a, b in grp]))
        w_ret = sum(1 for _d, _r, a, b in grp if b["ret"] > a["ret"])
        wa = min(a["mdd"] for _d, _r, a, _b in grp)
        wb = min(b["mdd"] for _d, _r, _a, b in grp)
        print(f"   {label}({len(grp)}창)  Δ수익 {d_ret:+7.1f}%p · ΔMDD {d_mdd:+6.1f}%p · "
              f"수익승 {w_ret}/{len(grp)} · 최악MDD {wa:.1f}→{wb:.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--include-etf", action="store_true")
    # 표본이 유니버스에 가까우면 시행끼리 겹쳐 짝비교 승수가 과장된다. 작은 표본 + 다른
    #  시드로 재확인해야 '30/30 승'이 독립 근거인지 겹침의 산물인지 갈린다.
    ap.add_argument("--seed", type=int, default=20260804)
    # [권장] 종목 표본 추출은 유니버스가 41개뿐이라 표본을 키우면 시행이 겹치고(짝비교 승수 과장),
    #  줄이면 실거래와 다른 좁은 유니버스를 재게 된다(게이트 해제의 가치는 후보 수에 비례한다).
    #  --walk 는 **실거래와 같은 전 종목**을 쓰고 시간축으로 창을 굴려 독립 관측을 만든다.
    ap.add_argument("--walk", type=int, default=0, metavar="WIN",
                    help="전 종목 고정 · WIN 거래일 창을 굴리며 비교(권장 250)")
    ap.add_argument("--step", type=int, default=30, help="--walk 의 창 이동 간격(거래일)")
    args = ap.parse_args()

    # [필수] 거래소(KOSPI/KOSDAQ) 조회는 config.session.stock_data 를 본다. 로드하지 않으면
    #  prepare_market_filter 가 전 종목을 KOSPI 로 취급한다(기존 감사 도구들의 누락).
    config.session.load_stock_config()
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    if args.include_etf:
        targets += [(s["code"], s["name"]) for s in config.session.stock_data.get("etfs_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 시드 {INITIAL_CAPITAL:,}원")

    dfs, _mf_ignored, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 가능 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}개" if failed else ""))
    codes = list(dfs.keys())
    if len(codes) < args.sample:
        args.sample = max(5, len(codes) // 2)
        print(f"[준비] 표본 수를 {args.sample}로 조정")

    n_kq = sum(1 for c in codes if _ticker_of(c) == "^KQ11")
    print(f"[준비] 지수 구성: KOSPI {len(codes) - n_kq} · KOSDAQ {n_kq}")

    mf_off, per_off = blocked_by_code(codes, args.days, False)
    mf_on, per_on = blocked_by_code(codes, args.days, True)
    span = set(dates)
    for tk in per_off:
        b_off = len(per_off[tk] & span)
        b_on = len(per_on[tk] & span)
        print(f"[준비] {tk} 차단일 {b_off} → {b_on}일 "
              f"(해제 {b_off - b_on}일, 기간의 {(b_off - b_on) / max(1, len(dates)) * 100:.1f}%)")
    if all(len(per_off[t] & span) == len(per_on[t] & span) for t in per_off):
        print("[경고] 두 설정의 차단일이 같다 — 비교가 무의미하다. 판정 경로를 확인할 것.")
        return

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)

    # 하위기간: 과최적화 방지. 전체 창에서만 이기는 설정은 채택하지 않는다.
    third = len(dates) // 3
    periods = [("전체", dates)]
    if third > 120:
        periods += [(f"1/3 {dates[0][:6]}~", dates[:third]),
                    (f"2/3 {dates[third][:6]}~", dates[third:third * 2]),
                    (f"3/3 {dates[third * 2][:6]}~", dates[third * 2:])]

    if args.walk:
        walk(dfs, status, dates, mf_off, mf_on, per_off, per_on, slots, codes,
             args.walk, args.step)
        return

    rng = random.Random(args.seed)
    samples = [rng.sample(codes, min(args.sample, len(codes))) for _ in range(args.trials)]

    print(f"\n{'=' * 96}")
    print(f"확정 Bear 해제 감사 — {args.trials}회 × {args.sample}종목 짝비교 (같은 표본·같은 기간)")
    print(f"{'=' * 96}")

    summary = []
    for pname, pdates in periods:
        # [해석 필수] 그 구간에 '해제된 차단일'이 몇 날이나 있었는가. 이 값이 작으면
        #  승패는 게이트의 우열이 아니라 잡음이다(강세장 구간은 애초에 차단이 없다).
        seg = set(pdates)
        rel = {tk: len(per_off[tk] & seg) - len(per_on[tk] & seg) for tk in per_off}
        rel_txt = " · ".join(f"{tk} {len(per_off[tk] & seg)}→{len(per_on[tk] & seg)}일(해제 {v})"
                             for tk, v in rel.items())
        off, on = [], []
        for i, pick in enumerate(samples):
            off.append(metrics(run_set(dfs, status, pdates, mf_off, slots, pick)))
            on.append(metrics(run_set(dfs, status, pdates, mf_on, slots, pick)))
            print(f"  [{pname}] 시행 {i + 1}/{len(samples)}", end="\r", flush=True)
        print(" " * 40, end="\r")
        w = report(pname, off, on)
        print(f"  해제된 차단일: {rel_txt}")
        w["rel"] = max(rel.values()) if rel else 0
        summary.append((pname, w))

    print(f"\n{'-' * 96}")
    print("판정 기준: 하위기간 3개 중 2개 이상에서 MAR·상위10%가 함께 개선돼야 채택한다")
    print("           (전체 창만 이기는 설정은 과최적화 — TRAILING_ATR_MULTIPLIER 4.0 이 같은 이유로 기각됐다).")
    for pname, w in summary[1:]:
        weak = "  ← 해제일이 적어 판정 불가" if w["rel"] < 30 else ""
        print(f"  {pname:<14} MAR {w['mar']}/{w['n']} · 상위10% {w['top10']}/{w['n']} · "
              f"수익 {w['ret']}/{w['n']} · 해제일 {w['rel']}{weak}")
    print(f"\n[참고] 현재 config = MARKET_FILTER_RELEASE_ON_BEAR "
          f"{getattr(config, 'MARKET_FILTER_RELEASE_ON_BEAR', False)}")


if __name__ == "__main__":
    main()
