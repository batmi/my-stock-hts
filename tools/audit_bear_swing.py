"""약세장(시장 필터 차단) 구간에 '짧은 스윙' 프로필로 진입하면 얻는 것이 있는가.

[제안 · 2026-09-15] 추세추종은 시장 필터가 차단 상태면 신규 매수를 전혀 하지 않아 침체기를
관망만 한다. 그 구간에도 **작은 익절을 반복해** 수익을 조금이라도 만들자는 안이다.
  · 차단(보류) 상태: 고정 익절 15% + 반익절(7.5%에서 50%) · 고정 손절 -7% · TS 발동선 고정 7%
    (ATR·손익분기 연동이 아니라) · 시간청산 정산. 이 조합으로 진입·청산 허용.
  · 정상 상태: 위 장치를 전부 OFF 하고 현행 추세추종(ATR 손절·샹들리에 TS·시간청산) 그대로.
  · 두 상태 사이의 전환은 시장 필터가 자동으로 한다(설정 화면에 표시).

[무엇을 재나] 세 질문으로 나눈다.
  Q1 약세장 스윙 거래 **그 자체**가 비용을 빼고 돈이 되는가 — 스윙 태그 청산만 모아
     순손익(원)·승률·평균 수익률·보유일·사유 비중을 본다. 이것이 제안의 직접 목적이다.
  Q2 그 거래를 얹은 **포트폴리오**가 현행보다 나은가 — 같은 표본·같은 창에서 수익·MDD·
     MAR·꼬리를 짝비교한다. 슬롯 경쟁(약세장에 잡은 스윙이 반등 초입의 추세 진입 자리를
     차지한다)·국면 전환 직후의 손절이 여기서 드러난다.
  Q3 '문자 그대로'의 구현(daily: 그날 국면으로 프로필을 매일 켜고 끈다)과 '진입 국면 태그'
     (entry: 산 날의 국면이 청산까지 따라간다)가 다른가 — daily 는 강세장에 산 추세 포지션도
     약세 전환 시 15% 익절 천장에 걸린다.

[대조군] A1 '단순 해제'(약세장 진입 + 청산은 현행) — MARKET_FILTER_RELEASE_ON_BEAR 와 같은
 세계다(2026-08-03 기각). 스윙 프로필이 그 기각 사유(MDD 악화)를 실제로 걷어내는지 본다.

[판정 규약] audit-seed-robustness · adoption-rule-drawdown-veto · 창별 승패로만 읽는다.
 단일 창 절대 수치는 인용 금지. 장중 청산 모사는 이 훅이 지원하지 않으므로 종가 모델이다
 (손절·TS 는 실매매보다 낙관적 — exit-timing-intraday-ts).

[실행] python3 tools/audit_bear_swing.py --trials 8 --seeds 20260915,7,101 [--arms A0,A1,A2,...]

[결과 2026-09-15 · 관심종목 41 · 10년 · 표본25 · 8회 × 씨드3 · 창 6개(전체+2년×5) · 차단일 비율 58.5%]
  Q1 **스윙 거래 자체가 돈이 안 된다.** 제안 프로필(A2) 10년 406건 · 순손익 시드 대비 -31.6% ·
     승률 50.6% · 평균 +1.39%/건 · 절반(48%)이 시간청산. 2년 창 5개 중 4개 음수(+0.4/-1.2/-0.3/
     -7.0/-9.4). 손잡이를 어느 쪽으로 돌려도 부호가 안 바뀐다 — 시간청산 제거 -5.1(A4) ·
     슬롯1 -10.2(A5) · 배분½ -17.1(A6) · 익절8% -35.8(A7, 승률 57%인데 가장 나쁨) · ATR손절 -28.8(A8).
     같은 진입을 추세추종 청산에 맡긴 A1은 +71.7%(승률 21.5%) — 약세장 진입에서 나오는 돈은
     소수의 큰 추세이고, 익절 천장은 정확히 그것을 자른다. '작은 익절을 반복'하는 구조가 문제다.
  Q2 포트폴리오도 전 팔 열위. A2 수익 46-98/144 · MAR 49/144 · 꼬리(상위10%) 37.2→25.2 ·
     MDD 3/6 창에서 10% 이상 악화. 가장 나은 A5(슬롯1)도 61-83. 2022~24(가장 긴 침체기)가
     정확히 제안이 노린 창인데 A2 4-20 · MDD -13.8→-18.4 — **침체기에 들어가서 침체기에 진다.**
  Q3 '문자 그대로'(A3 daily)가 최악 — 38-106 · 2024~26 창 1-23(216%→110%). 강세장에 산 추세
     포지션이 약세 전환 시 15% 익절 천장에 걸려 잘린다(추세 손익 401→211%).
  → **기각 · 축 종결.** 관망 구간의 기회비용을 '작은 익절'로 메우려는 안은 추세추종 자산곡선의
    원천(소수의 큰 추세)과 정면으로 부딪힌다. 훅(run_portfolio bear_swing)은 재현용으로 남긴다.
"""
import argparse
import os
import random
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_common import base_metrics, exits, seed_notice  # noqa: E402
from tools.audit_defensive_sector import INITIAL_CAPITAL, new_scale_fn_factory  # noqa: E402

# 운용자가 제시한 약세장 프로필(2026-09-15).
PROFILE = {"tp_rate": 15.0, "half_tp": True, "stop_rate": -7.0, "ts_act": 7.0,
           "time_stop_days": 15, "time_stop_any": True, "pyramid": False}

ARMS = {
    "A0": ("현행(차단)", None),
    "A1": ("단순해제(청산 현행)", {"mode": "entry", "tp_rate": 0.0, "half_tp": False,
                                 "stop_rate": None, "ts_act": None, "time_stop_days": 0,
                                 "pyramid": True}),
    "A2": ("스윙·entry", dict(PROFILE, mode="entry")),
    "A3": ("스윙·daily(문자그대로)", dict(PROFILE, mode="daily")),
    "A4": ("스윙·entry·시간청산없음", dict(PROFILE, mode="entry", time_stop_days=0)),
    "A5": ("스윙·entry·스윙슬롯1", dict(PROFILE, mode="entry", slots=1)),
    "A6": ("스윙·entry·배분½", dict(PROFILE, mode="entry", size_mult=0.5)),
    "A7": ("스윙·entry·익절8", dict(PROFILE, mode="entry", tp_rate=8.0)),
    "A8": ("스윙·entry·손절ATR", dict(PROFILE, mode="entry", stop_rate=None)),
}


def metrics(r):
    m = base_metrics(r)
    sw = [t for t in exits(r) if t.get("swing")]
    tr = [t for t in exits(r) if not t.get("swing")]
    m["n_sw"] = len(sw)
    m["sw_pnl"] = sum(t["profit_amt"] for t in sw) / INITIAL_CAPITAL * 100   # 시드 대비 %
    m["sw_win"] = (sum(1 for t in sw if t["profit"] > 0) / len(sw) * 100) if sw else float("nan")
    m["sw_avg"] = float(np.mean([t["profit"] for t in sw])) if sw else float("nan")
    m["sw_days"] = float(np.median([t["days"] for t in sw])) if sw else float("nan")
    m["tr_pnl"] = sum(t["profit_amt"] for t in tr) / INITIAL_CAPITAL * 100
    m["n_tr"] = len(tr)
    m["sw_reasons"] = Counter(t["reason"] for t in sw)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seeds", default="20260915,7,101")
    ap.add_argument("--arms", default=",".join(ARMS))
    args = ap.parse_args()
    #  경고는 데이터 준비(수 분) 전에 나와야 한다 — parse_args 바로 다음(가드 테스트가 자리를 본다).
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260915,7,101")
    seeds = [int(x) for x in args.seeds.split(",")]
    arms = [a for a in args.arms.split(",") if a in ARMS]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    base = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(base)}종목 · {args.days}일 · 슬롯 {slots}", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(base, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 · 거래일 {len(dates)}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)
    blocked_days = np.mean([len(mf.get(c, ())) for c in dfs]) / max(1, len(dates)) * 100
    print(f"[준비] 시장 필터 차단일 비율(종목 평균) {blocked_days:.1f}%")

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    bounds = ["20160101", "20180101", "20200101", "20220101", "20240101", "20270101"]
    W = [("전체", list(dates))]
    W += [(f"{bounds[k][2:6]}~{bounds[k + 1][2:6]}",
           [d for d in dates if bounds[k] <= d < bounds[k + 1]]) for k in range(len(bounds) - 1)]
    W = [(n, d) for n, d in W if len(d) > 120]

    codes_all = list(dfs)
    picks = {sd: [random.Random(sd * 7 + i).sample(codes_all, min(args.sample, len(codes_all)))
                  for i in range(args.trials)] for sd in seeds}

    def run(cs, wd, swing):
        return metrics(pb.run_portfolio(
            {c: dfs[c] for c in cs}, {c: status[c] for c in cs}, wd,
            initial_capital=INITIAL_CAPITAL, slots=slots,
            market_filter_dates={c: mf.get(c, set()) for c in cs},
            risk_scale_by_date=new_scale(), bear_swing=swing))

    m = lambda ms, k: float(np.nanmean([q[k] for q in ms]))  # noqa: E731
    results = {}   # (arm, window) -> list[metrics]
    print(f"\n[Q1] 약세장 스윙 거래 자체 — 표본 {args.sample} · {args.trials}회 × 씨드 {len(seeds)}개"
          f" (평균; 순손익은 시드 대비 %, 비용 차감)")
    print(f"  {'창':<12}{'팔':<4}{'스윙n':>6}{'순손익%':>8}{'승률%':>7}{'평균%':>7}{'중앙일':>7}"
          f"{'추세n':>6}{'추세손익%':>10}  사유 비중")
    for wn, wd in W:
        for a in arms:
            name, swing = ARMS[a]
            ms = [run(p, wd, swing) for sd in seeds for p in picks[sd]]
            results[(a, wn)] = ms
            if swing is None:
                continue
            reasons = Counter()
            for q in ms:
                reasons.update(q["sw_reasons"])
            tot = sum(reasons.values()) or 1
            mix = " ".join(f"{k}{v / tot * 100:.0f}%" for k, v in reasons.most_common(5))
            print(f"  {wn:<12}{a:<4}{m(ms, 'n_sw'):>6.1f}{m(ms, 'sw_pnl'):>8.2f}{m(ms, 'sw_win'):>7.1f}"
                  f"{m(ms, 'sw_avg'):>7.2f}{m(ms, 'sw_days'):>7.0f}"
                  f"{m(ms, 'n_tr'):>6.1f}{m(ms, 'tr_pnl'):>10.2f}  {mix}", flush=True)

    print(f"\n[Q2] 포트폴리오 짝비교 — 각 팔 vs A0 현행 (승/무/패는 수익률, 같은 표본·창)")
    print(f"  {'창':<12}{'팔':<4}{'승':>4}{'무':>4}{'패':>4}{'수익A0':>9}{'수익팔':>9}{'차이':>8}"
          f"{'MDD A0':>8}{'MDD팔':>8}{'MDD악화':>7}{'MAR승':>6}{'꼬리A0':>8}{'꼬리팔':>8}{'>30%A0':>7}{'>30%팔':>7}")
    tally = {}
    for wn, wd in W:
        b = results[("A0", wn)]
        for a in arms:
            if a == "A0":
                continue
            e = results[(a, wn)]
            win = sum(1 for x, y in zip(e, b) if x["ret"] > y["ret"] + 1e-9)
            tie = sum(1 for x, y in zip(e, b) if abs(x["ret"] - y["ret"]) <= 1e-9)
            lose = len(b) - win - tie
            mar_w = sum(1 for x, y in zip(e, b) if (x["mar"] or 0) > (y["mar"] or 0))
            # 낙폭 거부: MDD 가 상대 10% 넘게 나빠진 쌍의 수
            worse = sum(1 for x, y in zip(e, b) if x["mdd"] < y["mdd"] * 1.10 - 1e-9)
            t = tally.setdefault(a, {"w": 0, "l": 0, "mdd_worse_win": 0, "mar": 0, "n": 0})
            t["w"] += win; t["l"] += lose; t["n"] += len(b); t["mar"] += mar_w
            t["mdd_worse_win"] += 1 if worse > len(b) / 2 else 0
            print(f"  {wn:<12}{a:<4}{win:>4}{tie:>4}{lose:>4}{m(b, 'ret'):>9.1f}{m(e, 'ret'):>9.1f}"
                  f"{m(e, 'ret') - m(b, 'ret'):>8.1f}{m(b, 'mdd'):>8.1f}{m(e, 'mdd'):>8.1f}"
                  f"{worse:>4}/{len(b):<3}{mar_w:>4}/{len(b):<2}"
                  f"{m(b, 'top10'):>8.1f}{m(e, 'top10'):>8.1f}{m(b, 'big'):>7.1f}{m(e, 'big'):>7.1f}",
                  flush=True)

    print("\n[요약] 창 합산 (수익 승-패 · MAR 승 · MDD 가 절반 넘게 10% 이상 악화한 창 수)")
    for a, t in tally.items():
        print(f"  {a} {ARMS[a][0]:<22} 수익 {t['w']}-{t['l']}/{t['n']} · MAR {t['mar']}/{t['n']}"
              f" · MDD 악화 창 {t['mdd_worse_win']}/{len(W)}")


if __name__ == "__main__":
    main()
