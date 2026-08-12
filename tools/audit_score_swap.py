"""스코어링 항목 '교체' 실험 — 원칙과 어긋나는 무동작 항목의 자리를 추세 지표로 바꾼다.

[출발점] 2026-08-12 전수 검증에서 두 항목이 추세추종 원칙과 어긋난다는 것이 확인됐다.
    CCI 과매도권 탈출  — 평균회귀 신호. 발화 1.0%, 정보량 -0.25
    60일선 돌파[초기]  — 역배열(ema20<=ema60)에서의 전환 포착 = 예측. 발화 7.1%, 정보량 -0.67
 그런데 **빼도 조여도 결과가 움직이지 않았다**(제거 시 15회 중 10~11회 완전 동점, 또는
 오히려 열위). 원칙 위배가 실제 매매에 닿지 않는다는 뜻이라 '그대로 둔다'가 답이었다.

[이번 질문] 그렇다면 그 자리를 **원칙에 맞는 다른 조건으로 바꾸면** 어떤가. 빼는 것과
 바꾸는 것은 다른 실험이다 — 빼면 배점 예산이 줄지만, 바꾸면 예산은 그대로고 조건만 바뀐다.

[무엇으로 바꾸는가] 추세 품질(연환산 회귀 기울기 × R², indicators.get_trend_quality).
 고른 이유 넷.
   ① 원칙 정합 — 추세의 강도와 매끄러움을 함께 재는 정통 추세추종 지표(Clenow 모멘텀)다.
   ② 이미 검증됐다 — 진입 순위의 동점 가름으로 값을 한다(2026-08-12 채택, 룩백 90일 확정).
   ③ 연속값이다 — 현행 항목은 전부 0/1이라 점수가 0.5 단위로 양자화되고, 그것이 슬롯
      경쟁일의 25~32%를 동점으로 만든다. 연속 지표를 문턱으로 쓰면 그 쏠림이 줄어든다.
   ④ 직교성 — 현행 상위 항목(추세 지속이력)과 성격이 겹칠 수 있어, 동시 발화율을 함께 찍는다.

[측정 규약] 만점 10.0을 건드리지 않는다. 같은 슬롯의 조건만 바꾸므로 정규화가 필요 없다 —
 이전 실험에서 배점을 키운 팔들이 전부 진 이유의 절반이 '문턱 완화/강화 혼입'이었다.
 대조군으로 '교체 없이 추가'(만점 10.5 → 정규화) 팔을 하나 세워 둘을 가른다.

[실행] python tools/audit_score_swap.py [--trials 15] [--sample 25] [--seed ...]
"""
import argparse
import os
import random
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import analysis, backtest  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_scoring_weights import metrics, rolling_trend_quality  # noqa: E402

INITIAL_CAPITAL = 10_000_000
_PTS = re.compile(r"\(([+-]?\d+\.\d+)\)")
CCI_ESCAPE = "CCI: 과매도권 탈출"
MA_BREAK = "EMA: 60일선 돌파"
MA_SURGE = "EMA: 단기 급등 추세"
CCI_UP = "CCI: 상승 추세"
MA_PREFIX = "EMA: "
CAP_PREFIX = "[상한] EMA 군집"

# 추세품질 밴드(indicators.TREND_QUALITY_BANDS): 30 미만 약함 · 30~60 양호 · 60+ 강함
TQ_STRONG = 60.0
TQ_GOOD = 30.0

def build_arms(cci_thr, ma_thr, with_addonly):
    """(라벨, CCI자리 교체 문턱, MA자리 교체 문턱, 추가(정규화) 여부)

    [빈도 일치가 핵심이다] 교체 문턱은 **대체되는 조건과 같은 빈도로 켜지도록** 잡아야 한다.
     추세품질 분포는 극단적 우편향이라(p50 0.2 · p90 133 · p99 1136) 밴드 기준 '강함(60+)'이
     이미 상위 18.8%다. 반면 대체 대상은 60돌파 7.17% · CCI탈출 1.18%뿐이다. 빈도를 맞추지
     않으면 '조건이 좋아서'가 아니라 '점수를 더 자주 줘서' 이기는 팔이 나온다 — 만점은
     10.0으로 유지돼도 평균 점수가 올라가 더 많은 종목이 BUY_SCORE를 넘기 때문이다.
     같은 빈도가 되는 문턱: 60돌파 자리 189.0 · CCI탈출 자리 966.7 (10년 90,528일 기준).
    """
    arms = [
        ("현행 (기준선)", None, None, False),
        (f"CCI자리 → 추세품질 {cci_thr:g}+", cci_thr, None, False),
        (f"60돌파자리 → 추세품질 {ma_thr:g}+", None, ma_thr, False),
        ("둘 다 교체", cci_thr, ma_thr, False),
    ]
    if with_addonly:
        # [비채택 대조군] 자리를 비우지 않고 항목을 더한다(만점 10.5 → 정규화).
        #  만점 10.0 불변식(config.py 참조)에 어긋나므로 채택 대상이 아니다. 개선이 나왔을 때
        #  그것이 '새 신호' 때문인지 '낡은 조건 제거' 때문인지 가르는 용도로만 쓴다.
        arms.append(("[대조] 교체 없이 추가", None, None, True))
    return arms

_CTX = {"tq": None}


def make_patches(orig_status, orig_score, cci_to, ma_to, add_only):
    """상태 계산 직전에 그날의 추세품질을 문맥에 넣고, 점수 래퍼가 그것을 읽어 조건을 갈아끼운다.

    precompute_status는 단일 스레드로 종목·일자를 순회하므로 이 문맥 전달은 안전하다.
    calculate_score의 인자에는 종목 코드가 없어(가격·지표 스칼라만 온다) 이 방법 외에는
    '그 시점 그 종목의 추세품질'을 점수 계산에 넣을 길이 없다.
    """

    def patched_status(row, prev, thresholds=None):
        _CTX["tq"] = row.get("TQ")
        return orig_status(row, prev, thresholds=thresholds)

    def patched_score(*a, **kw):
        score, details = orig_score(*a, **kw)
        tq = _CTX.get("tq")
        weights = kw.get("weights") or config.SCORING_WEIGHTS
        if not isinstance(weights, dict):
            weights = config.SCORING_WEIGHTS
        r_trend = weights.get("TREND", 4.0) / 4.0
        r_mom = weights.get("MOMENTUM", 2.5) / 2.5
        cap = round(2.0 * r_trend, 2)
        unit_t = round(0.5 * r_trend, 2)
        unit_m = round(0.5 * r_mom, 2)

        # ── 1) 원본에서 각 '슬롯'의 점유 상태를 읽는다.
        #  [핵심] 두 대상 항목은 단독 항목이 아니라 **다른 조건과 슬롯을 나눠 쓴다.**
        #    · CCI 슬롯(0.5): 'CCI 상승 추세' 또는 'CCI 과매도권 탈출' 중 하나만 인정
        #    · MA 돌파 슬롯(0.5): 'EMA 60일선 돌파[초기]' 또는 'EMA 단기 급등 추세' 중 하나만
        #  따라서 교체는 '그 조건이 차지하던 자리'만 바꿔야 한다. 무조건 더하면 슬롯이 두 배가
        #  되어 만점이 커지고, 측정하려던 것(조건 교체)이 아니라 문턱 완화를 재게 된다.
        ma_raw = 0.0
        cci_up = cci_escape = ma_break = ma_surge = False
        for d in details:
            if d.startswith(CAP_PREFIX):
                continue
            m = _PTS.search(d)
            pts = float(m.group(1)) if m else 0.0
            if d.startswith(MA_PREFIX):
                ma_raw += pts
                if d.startswith(MA_BREAK):
                    ma_break = True
                elif d.startswith(MA_SURGE):
                    ma_surge = True
            elif d.startswith(CCI_ESCAPE):
                cci_escape = True
            elif d.startswith(CCI_UP):
                cci_up = True

        # ── 2) 슬롯 단위로 교체한다. 배점·만점은 그대로다.
        delta = 0.0
        if cci_to is not None:
            old_slot = unit_m if (cci_up or cci_escape) else 0.0
            new_slot = unit_m if (cci_up or (tq is not None and tq >= cci_to)) else 0.0
            delta += new_slot - old_slot

        ma_new = ma_raw
        if ma_to is not None:
            old_slot = unit_t if (ma_break or ma_surge) else 0.0
            new_slot = unit_t if (ma_surge or (tq is not None and tq >= ma_to)) else 0.0
            ma_new = ma_raw - old_slot + new_slot
        delta += min(ma_new, cap) - min(ma_raw, cap)

        if add_only and tq is not None and tq >= TQ_STRONG:
            delta += unit_t   # 자리를 비우지 않고 항목을 하나 더하는 대조군

        adj = score + delta
        if adj < 0:
            adj = 0.0
        if add_only:
            adj *= 10.0 / 10.5                            # 만점이 커진 만큼 되돌린다(문턱 완화 차단)
        return round(adj, 2), details

    return patched_status, patched_score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    ap.add_argument("--cci-thr", type=float, default=TQ_STRONG,
                    help="CCI 과매도탈출 자리를 대체할 추세품질 문턱 (빈도 일치값 966.7)")
    ap.add_argument("--ma-thr", type=float, default=TQ_GOOD,
                    help="60일선 돌파 자리를 대체할 추세품질 문턱 (빈도 일치값 189.0)")
    ap.add_argument("--with-addonly", action="store_true",
                    help="비채택 대조군(교체 없이 추가) 팔을 포함한다")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 씨드 {args.seed}", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""), flush=True)

    # 추세품질을 컬럼으로 깐다(룩백은 실매매 랭킹과 같은 값).
    lookback = int(config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90))
    for code, df in dfs.items():
        tq = rolling_trend_quality(df, lookback)
        df["TQ"] = [tq.get(str(d)) for d in df["date"]]
    for thr, who in ((args.cci_thr, "CCI자리"), (args.ma_thr, "60돌파자리")):
        fired = float(np.mean([np.mean([(v is not None and v >= thr) for v in df["TQ"]])
                               for df in dfs.values()]) * 100)
        print(f"[진단] {who} 대체 문턱 {thr:g}+ 발화율 {fired:.2f}%"
              f"  (대체 대상: CCI탈출 1.18% · 60돌파 7.17%)", flush=True)

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    rp = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if rp.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(rp.get("DD_LOOKBACK_DAYS", 90)), float(rp.get("DD_LEVEL_1", 5.0)),
              float(rp.get("DD_SCALE_1", 0.9)), float(rp.get("DD_LEVEL_2", 10.0)),
              float(rp.get("DD_SCALE_2", 0.8)))
    scale_fn = make_scale_fn(mkt, dd)

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and "".join(filter(str.isdigit, d)) >= cut]
    head = head[lookback - 1:]   # 추세품질 워밍업 구간 제외(다른 팔과 창을 맞춘다)
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
    orig_status = backtest.calculate_daily_status
    orig_score = analysis.calculate_score

    def run_arm(cci_to, ma_to, add_only):
        if cci_to is None and ma_to is None and not add_only:
            ps, pc = orig_status, orig_score
        else:
            ps, pc = make_patches(orig_status, orig_score, cci_to, ma_to, add_only)
        backtest.calculate_daily_status, analysis.calculate_score = ps, pc
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
                                         risk_scale_by_date=scale_fn)
                    res.append(metrics(r))
                out[wname] = res
            return out
        finally:
            backtest.calculate_daily_status = orig_status
            analysis.calculate_score = orig_score

    W = 116
    print(f"\n{'=' * W}")
    print(f"스코어링 항목 교체 ({args.trials}회 × {args.sample}종목 짝비교 · 씨드 {args.seed})")
    print(f"{'=' * W}")
    print(f"{'설정':<26}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'상위10%':>9}{'최대':>8}{'>30%':>7}"
          f"{'전체 승-무-패':>14}{'하위4구간':>11}{'MAR승':>8}{'꼬리승':>8}{'고변동':>11}")
    print("-" * W, flush=True)

    base = None
    for label, cci_to, ma_to, add_only in build_arms(args.cci_thr, args.ma_thr, args.with_addonly):
        arm = run_arm(cci_to, ma_to, add_only)
        m = arm["전체"]
        row = (f"{label:<26}{np.median([x['ret'] for x in m]):>9.1f}"
               f"{np.median([x['mdd'] for x in m]):>8.1f}{np.median([x['mar'] for x in m]):>7.2f}"
               f"{np.median([x['top10'] for x in m]):>9.1f}{np.median([x['best'] for x in m]):>8.1f}"
               f"{np.median([x['big'] for x in m]):>7.0f}")
        if base is None:
            base = arm
            print(row + f"{'—':>14}{'—':>11}{'—':>8}{'—':>8}{'—':>11}", flush=True)
            continue

        def tally(wname):
            a, b = arm[wname], base[wname]
            w = sum(1 for x, y in zip(a, b) if x["ret"] > y["ret"])
            t = sum(1 for x, y in zip(a, b) if abs(x["ret"] - y["ret"]) < 1e-9)
            return w, t, len(a) - w - t

        sub_w = sum(tally(f"구간{i + 1}")[0] for i in range(k))
        aw, at, al = tally("전체")
        hv = tally("고변동") if tail else (0, 0, 0)
        b = base["전체"]
        mw = sum(1 for x, y in zip(m, b) if x["mar"] > y["mar"])
        tw = sum(1 for x, y in zip(m, b) if x["top10"] > y["top10"])
        print(row + f"{f'{aw}-{at}-{al}':>14}{f'{sub_w}/{k * args.trials}':>11}"
              f"{f'{mw}/{len(m)}':>8}{f'{tw}/{len(m)}':>8}"
              f"{f'{hv[0]}-{hv[1]}-{hv[2]}':>11}", flush=True)

    print("\n" + "-" * W)
    print("하위4구간 = 하위 4개 구간 짝비교 승수 합(60 중). 31/60 이상이라야 채택 검토 대상이고,")
    print("기준선 부근(28~34)이면 씨드를 바꿔 3회 이상 재확인할 것(2026-08-12 규약).")


if __name__ == "__main__":
    main()
