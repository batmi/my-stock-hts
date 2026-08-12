"""스코어링 세부 항목 제거 실험 — 정보량이 없거나 음수인 항목을 빼면 나아지는가.

[출발점] tools/audit_score_factors.py 실측(93,426 종목·일, 전방 20일)에서 네 항목이 걸렸다.
    추세 60돌파[초기]  발화 7.1% · 차이 -0.67  (가점인데 전방수익이 낮다)
    모멘텀 CCI탈출     발화 1.0% · 차이 -0.25  (거의 죽은 조항이고 방향도 반대)
    강도 VOL급증       발화 3.4% · 차이 -0.24  (중앙차 -0.92)
    강도 VOL추세       발화 39.7% · 차이 -0.02 (절반 가까이 켜지는데 정보가 없다)
 정보량은 '신호의 값어치'일 뿐이므로 채택·기각은 언제나 짝비교 백테스트로 한다. 여기가 그 자리다.

[구현] 프로덕션 코드는 건드리지 않는다. analysis.calculate_score를 감싸(monkeypatch) 항목의
 점수만 빼는데, 두 가지를 정확히 처리해야 결과가 참이 된다.
   ① MA 군집 상한 — EMA 계열 항목을 빼도 나머지가 여전히 상한(2.0)을 넘으면 총점은 그대로다.
      단순 뺄셈을 하면 있지도 않은 감소를 만든다. 그래서 상한을 다시 적용해 계산한다.
   ② 만점 스케일 — 같은 슬롯을 나눠 쓰는 항목(VOL급증/VOL추세, CCI상승/CCI탈출)은 하나만 빼면
      만점이 그대로지만, 둘 다 빼면 만점이 9.5로 줄어 BUY_SCORE 7.0이 더 빡빡한 문턱이 된다.
      그 경우 '항목 효과'와 '문턱 강화'가 섞이므로 정규화 팔을 따로 세운다(A그룹과 같은 규약).

 monkeypatch는 classify_stock_state가 부르는 경로에도 그대로 걸린다(모듈 전역 조회) —
 점수만이 아니라 상태 판정까지 같은 잣대로 바뀌어야 실험이 성립한다.

[실행] python tools/audit_score_items.py [--trials 15] [--sample 25]
"""
import argparse
import os
import random
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import analysis  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_scoring_weights import metrics  # noqa: E402

INITIAL_CAPITAL = 10_000_000
_PTS = re.compile(r"\(([+-]?\d+\.\d+)\)")

# 라벨 → 상세 문구 접두사
ITEMS = {
    "60돌파초기": "EMA: 60일선 돌파",
    "CCI탈출": "CCI: 과매도권 탈출",
    "VOL급증": "VOL: 거래량 폭증",
    "VOL추세": "VOL: 거래량 추세",
}
MA_PREFIX = "EMA: "
CAP_PREFIX = "[상한] EMA 군집"

# 강화·신설 대상 (정보량 상위 항목). 접두사로 상세 문구를 집는다.
BOOST = {
    "지속이력": "추세 지속:",
    "가격모멘텀": "가격 모멘텀:",
    "모멘텀보류": "가격 모멘텀 보류",
}

# (라벨, 제거 항목, 가점 조정 {항목: 증분}, 만점 스케일)
#  스케일은 만점이 10.0에서 벗어나는 팔에만 건다 — 만점이 커지면 BUY_SCORE 7.0이 상대적으로
#  헐거워져 '항목 효과'와 '문턱 완화'가 섞인다(A그룹에서 확인된 측정 함정).
ARMS = [
    ("현행 (기준선)", (), {}, 1.0),
    # --- 제거: 정보량이 없거나 음수인 항목 ---
    ("60돌파[초기] 제거", ("60돌파초기",), {}, 1.0),
    ("CCI 과매도탈출 제거", ("CCI탈출",), {}, 1.0),
    ("VOL 급증 제거", ("VOL급증",), {}, 1.0),
    ("VOL 추세 제거", ("VOL추세",), {}, 1.0),
    ("4개 모두 제거", ("60돌파초기", "CCI탈출", "VOL급증", "VOL추세"), {}, 1.0),
    # 거래량 항목을 둘 다 빼면 만점이 9.5로 줄어 문턱이 상대적으로 높아진다 → 10.0으로 되돌린 대조군.
    ("4개 제거 + 만점 정규화", ("60돌파초기", "CCI탈출", "VOL급증", "VOL추세"), {}, 10.0 / 9.5),
    # --- 강화: 정보량이 가장 큰 두 항목(전방 20일 차이 +1.92 / +1.81) ---
    ("추세 지속이력 0.5→1.0", (), {"지속이력": 0.5}, 10.0 / 10.5),
    ("가격 모멘텀 0.5→1.0", (), {"가격모멘텀": 0.5}, 10.0 / 10.5),
    ("둘 다 1.0", (), {"지속이력": 0.5, "가격모멘텀": 0.5}, 10.0 / 11.0),
    # --- 변경: 다중기간 정합 게이트를 0/1에서 부분 가점으로 ---
    #  보류 케이스도 전방 20일 +2.68로 전체 평균(1.78)을 크게 웃돈다 — 통째로 0을 주는 것이
    #  과한 처벌일 수 있다. 만점은 그대로다(보류와 가점은 동시에 나지 않는다).
    ("모멘텀 보류 시 절반 가점", (), {"모멘텀보류": 0.25}, 1.0),
]


def make_patch(orig, disabled, boosts, scale):
    """항목을 빼거나 더한 점수를 돌려주는 래퍼. 아무 조정도 없으면 원본을 그대로 쓴다."""
    if not disabled and not boosts and scale == 1.0:
        return orig
    prefixes = [ITEMS[k] for k in disabled]
    boost_pairs = [(BOOST[k], v) for k, v in (boosts or {}).items()]
    ma_cap_base = 2.0

    def patched(*a, **kw):
        score, details = orig(*a, **kw)
        weights = kw.get("weights") or config.SCORING_WEIGHTS
        if not isinstance(weights, dict):
            weights = config.SCORING_WEIGHTS
        r_trend = weights.get("TREND", 4.0) / 4.0
        cap = round(ma_cap_base * r_trend, 2)

        drop = 0.0
        add = 0.0
        ma_raw = 0.0
        ma_removed = 0.0
        capped = False
        for d in details:
            m = _PTS.search(d)
            pts = float(m.group(1)) if m else 0.0
            if d.startswith(CAP_PREFIX):
                capped = True
                continue
            hit = any(d.startswith(p) for p in prefixes)
            if d.startswith(MA_PREFIX):
                ma_raw += pts
                if hit:
                    ma_removed += pts
            elif hit:
                drop += pts
            for bp, delta in boost_pairs:
                if d.startswith(bp):
                    add += delta
        # MA 군집은 상한을 다시 적용해 실제 감소분만 반영한다.
        old_ma = cap if capped else ma_raw
        new_ma = min(ma_raw - ma_removed, cap)
        adj = score - drop + add + (new_ma - old_ma)
        if adj < 0:
            adj = 0.0
        return round(adj * scale, 2), details

    return patched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    ap.add_argument("--arms", default=None,
                    help="라벨 일부(쉼표)만 실행 — 기준선은 항상 포함. 씨드 견고성 재확인용")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""), flush=True)

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    rp = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if rp.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(rp.get("DD_LOOKBACK_DAYS", 90)), float(rp.get("DD_LEVEL_1", 5.0)),
              float(rp.get("DD_SCALE_1", 0.9)), float(rp.get("DD_LEVEL_2", 10.0)),
              float(rp.get("DD_SCALE_2", 0.8)))
    # [실행마다 새로 만든다 — 2026-08-13] make_scale_fn 이 돌려주는 콜러블은 내부에 자산곡선
    #  이력(hist)을 들고 있다. 하나를 만들어 모든 팔·시행에 돌려쓰면 앞선 실행의 자산곡선이
    #  남아 드로다운 판정이 오염되고, 팔의 실행 순서에 따라 결과가 달라진다(같은 설정을 두 번
    #  돌리면 392.66% → 284.79%). 짝비교의 전제가 깨지므로 팩토리로 두고 매번 새로 만든다.
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and "".join(filter(str.isdigit, d)) >= cut]
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("전체", head)]
    windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                for i in range(k)]
    if tail:
        windows.append(("고변동", tail))
    print(f"[창] 검증 {len(head)}일 · 제외(고변동 대조) {len(tail)}일", flush=True)

    thr = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    codes = list(dfs.keys())
    orig_score = analysis.calculate_score

    def run_arm(disabled, boosts, scale):
        analysis.calculate_score = make_patch(orig_score, disabled, boosts, scale)
        try:
            status = pb.precompute_status(dfs, thr)
            out = {}
            for wname, wdates in windows:
                res = []
                rng = random.Random(args.seed)
                for _t in range(args.trials):
                    pick = rng.sample(codes, min(args.sample, len(codes)))
                    r = pb.run_portfolio({c: dfs[c] for c in pick},
                                         {c: status[c] for c in pick}, wdates,
                                         initial_capital=INITIAL_CAPITAL, slots=slots,
                                         market_filter_dates={c: mf.get(c, set()) for c in pick},
                                         risk_scale_by_date=new_scale_fn())
                    res.append(metrics(r))
                out[wname] = res
            return out
        finally:
            analysis.calculate_score = orig_score

    W = 116
    print(f"\n{'=' * W}")
    print(f"스코어링 항목 제거 ({args.trials}회 × {args.sample}종목 짝비교)")
    print(f"{'=' * W}")
    print(f"{'설정':<26}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'상위10%':>9}{'최대':>8}{'>30%':>7}"
          f"{'전체 승-무-패':>14}{'하위4구간':>11}{'MAR승':>8}{'꼬리승':>8}{'고변동':>11}")
    print("-" * W, flush=True)

    arms = ARMS
    if args.arms:
        want = [x.strip() for x in args.arms.split(",") if x.strip()]
        arms = [a for a in ARMS if a[1] == () and not a[2] or any(w in a[0] for w in want)]

    base = None
    for label, disabled, boosts, scale in arms:
        arm = run_arm(disabled, boosts, scale)
        if base is None:
            base = arm
            m = arm["전체"]
            print(f"{label:<26}{np.median([x['ret'] for x in m]):>9.1f}"
                  f"{np.median([x['mdd'] for x in m]):>8.1f}{np.median([x['mar'] for x in m]):>7.2f}"
                  f"{np.median([x['top10'] for x in m]):>9.1f}{np.median([x['best'] for x in m]):>8.1f}"
                  f"{np.median([x['big'] for x in m]):>7.0f}"
                  f"{'—':>14}{'—':>11}{'—':>8}{'—':>8}{'—':>11}", flush=True)
            continue

        def tally(wname):
            a, b = arm[wname], base[wname]
            w = sum(1 for x, y in zip(a, b) if x["ret"] > y["ret"])
            t = sum(1 for x, y in zip(a, b) if abs(x["ret"] - y["ret"]) < 1e-9)
            return w, t, len(a) - w - t

        sub_w = sum(tally(f"구간{i + 1}")[0] for i in range(k))
        aw, at, al = tally("전체")
        hv = tally("고변동") if tail else (0, 0, 0)
        m, b = arm["전체"], base["전체"]
        mw = sum(1 for x, y in zip(m, b) if x["mar"] > y["mar"])
        tw = sum(1 for x, y in zip(m, b) if x["top10"] > y["top10"])
        print(f"{label:<26}{np.median([x['ret'] for x in m]):>9.1f}"
              f"{np.median([x['mdd'] for x in m]):>8.1f}{np.median([x['mar'] for x in m]):>7.2f}"
              f"{np.median([x['top10'] for x in m]):>9.1f}{np.median([x['best'] for x in m]):>8.1f}"
              f"{np.median([x['big'] for x in m]):>7.0f}"
              f"{f'{aw}-{at}-{al}':>14}{f'{sub_w}/{k * args.trials}':>11}"
              f"{f'{mw}/{len(m)}':>8}{f'{tw}/{len(m)}':>8}"
              f"{f'{hv[0]}-{hv[1]}-{hv[2]}':>11}", flush=True)

    print("\n" + "-" * W)
    print("하위4구간 = 하위 4개 구간 짝비교 승수 합(60 중). 31/60 이상이라야 채택 검토 대상이다.")
    print("[읽는 법] 무승부가 대부분이면 그 항목은 이 표본에서 슬롯 주인을 바꾸지 못한다 —")
    print("          정보량이 음수여도 시스템 성과에는 닿지 않는다는 뜻이므로 건드릴 이유가 없다.")


if __name__ == "__main__":
    main()
