"""스코어링 항목 검증 — 30개 세부 항목 하나하나가 정보를 담고 있는가.

[왜] 이 시스템의 심장은 스코어링이다. 그런데 지금까지 잰 것은 **팩터 4개의 가중치 형태**
 (2026-08-12 A그룹)와 **순위로서의 값어치**(B·C그룹)뿐이고, 그 팩터를 이루는 **세부 항목**은
 한 번도 개별로 측정된 적이 없다. 가중치를 아무리 잘 배분해도 항목 자체가 무정보이면
 배분할 것이 없다. 여기서는 항목별로 세 가지를 묻는다.

   ① 발화율 — 이 항목이 켜지는 날이 얼마나 되는가. 0%에 가까우면 죽은 조항이고,
      100%에 가까우면 변별력이 없다(모두에게 주는 점수는 순위를 못 가른다).
   ② 정보량 — 켜진 날과 꺼진 날의 **전방 수익률** 차이. 음수면 그 항목은 점수를 올리면서
      수익은 깎는다 — 있는 것이 없는 것만 못하다.
   ③ 중복 — 항목끼리 같은 날 함께 켜지는 비율. 높으면 같은 정보를 두 번 세고 있다.

[읽을 때 주의] 전방 수익률은 **신호 자체의 값어치**이지 시스템 성과가 아니다. 이 시스템은
 손절·트레일링·슬롯 경쟁을 거치므로, 여기서 좋은 항목이 반드시 포트폴리오 수익을 올리는
 것은 아니다(그 판정은 audit_scoring_weights.py의 짝비교가 한다). 이 도구의 쓰임은
 **선별**이다 — 죽은 항목·해로운 항목·중복 항목을 찾아 다음 실험의 후보를 좁힌다.
 창이 겹치는 표본이라 자기상관이 있으므로 t값은 내지 않고 차이와 표본 수만 본다.

[실행] python tools/audit_score_factors.py [--days 3650] [--fwd 20]
"""
import argparse
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import analysis, backtest  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

# 상세 문구 → 짧은 항목 키. 문구가 바뀌면 여기도 바뀌어야 한다(미분류는 '기타'로 뜬다).
LABELS = [
    ("EMA: 현재가 > 20일선", "추세 EMA20위"),
    ("EMA: 20/60/120 정배열", "추세 정배열"),
    ("EMA: 5일선 > 20일선", "추세 EMA5>20"),
    ("EMA: 60일선 돌파", "추세 60돌파[초기]"),
    ("EMA: 단기 급등", "추세 단기급등"),
    ("EMA: 장기 지지", "추세 EMA120위"),
    ("[상한] EMA 군집", "추세 MA상한적용"),
    ("추세 지속:", "추세 지속이력"),
    ("MACD: 0선 위", "추세 MACD0선위"),
    ("MACD: 신규 골든크로스", "추세 MACD신규GC"),
    ("MACD: 상승 추세 확산", "추세 MACD확산"),
    ("SAR: 상승 추세", "추세 SAR"),
    ("RSI: 강세 구간", "모멘텀 RSI강세"),
    ("RSI: 모멘텀 확장", "모멘텀 RSI확장"),
    ("RSI: 상승 여력", "모멘텀 RSI눌림"),
    ("CCI: 상승 추세", "모멘텀 CCI상승"),
    ("CCI: 과매도권 탈출", "모멘텀 CCI탈출"),
    ("DMI: +DI", "모멘텀 DMI"),
    ("가격 모멘텀 보류", "모멘텀 가격보류"),
    ("가격 모멘텀:", "모멘텀 가격"),
    ("ADX: 추세 형성", "강도 ADX"),
    ("VOL: 거래량 폭증", "강도 VOL급증"),
    ("VOL: 거래량 추세", "강도 VOL추세"),
    ("수급: OBV/SM", "강도 수급"),
    ("추세 시작:", "시너지 추세시작"),
    ("모멘텀 폭발:", "시너지 모멘텀폭발"),
    ("감점: MACD 데드크로스", "감점 MACD_DC"),
    ("감점: MACD 하락 가속", "감점 MACD가속"),
    ("감점: -DI 우위", "감점 -DI우위"),
]


def key_of(detail):
    for needle, label in LABELS:
        if detail.startswith(needle):
            return label
    return "기타: " + detail[:20]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--fwd", type=int, default=20, help="전방 수익률 창(거래일)")
    ap.add_argument("--fwd2", type=int, default=60)
    args = ap.parse_args()

    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일")
    dfs, _mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

    weights = config.SCORING_WEIGHTS
    fired = defaultdict(list)     # 항목 -> 전방수익 리스트(켜진 날)
    unfired = defaultdict(list)   # 항목 -> 전방수익 리스트(꺼진 날)
    fired2 = defaultdict(list)
    n_days = 0
    scores = []
    score_fwd = []
    cooc = defaultdict(int)       # 항목 -> 동시 발화 횟수 누적용
    seen_keys = set()
    pair = defaultdict(int)
    single = defaultdict(int)

    for code, df in dfs.items():
        rows = df.to_dict("records")
        closes = np.array([float(r["close"]) for r in rows])
        prev = None
        for i, row in enumerate(rows):
            if i + args.fwd >= len(rows):
                break
            # [SSOT] 입력 조립은 backtest.calculate_daily_status와 같은 순서·같은 필드여야 한다.
            #  여기서 하나라도 다르면 '실제로 매매를 결정하는 점수'가 아닌 다른 점수를 재게 된다.
            try:
                _score, details = analysis.calculate_score(
                    price=row["close"], ema20=row["EMA20"], ema60=row["EMA60"], ema120=row["EMA120"],
                    sar=row["SAR"], rsi=row["RSI"], adx=row["ADX"], cci=row["CCI"],
                    obv_trend=(row["OBV"] > row["OBV_MA"]), macd=row.get("MACD"),
                    macd_signal=row.get("MACD_Signal"), weights=weights,
                    smart_money=row.get("smart_money", False),
                    plus_di=row.get("PLUS_DI"), minus_di=row.get("MINUS_DI"),
                    ema_5=row.get("EMA5"), macd_hist=row.get("MACD_Hist"),
                    prev_macd_hist=(prev.get("MACD_Hist") if prev is not None else None),
                    prev_cci=(prev.get("CCI") if prev is not None else None),
                    vol_spike=row.get("VOL_SPIKE", False), vol_trend=row.get("VOL_TREND", False),
                    w52_pos=row.get("w52_pos", 0.0), mom_ret=row.get("MOM_RET"),
                    mom_ret_1m=row.get("MOM_RET_1M"), mom_ret_3m=row.get("MOM_RET_3M"),
                    trend_persist=row.get("TREND_PERSIST"),
                )
            except Exception:
                prev = row
                continue
            prev = row
            base = closes[i]
            if base <= 0:
                continue
            fwd = (closes[i + args.fwd] / base - 1) * 100
            fwd2 = ((closes[i + args.fwd2] / base - 1) * 100
                    if i + args.fwd2 < len(rows) else None)
            keys = {key_of(d) for d in details}
            seen_keys |= keys
            n_days += 1
            scores.append(round(_score, 1))
            score_fwd.append(fwd)
            for k in keys:
                fired[k].append(fwd)
                if fwd2 is not None:
                    fired2[k].append(fwd2)
                single[k] += 1
            for k in seen_keys:
                if k not in keys:
                    unfired[k].append(fwd)
            ks = sorted(keys)
            for a_i in range(len(ks)):
                for b_i in range(a_i + 1, len(ks)):
                    pair[(ks[a_i], ks[b_i])] += 1
        print(f"  스코어링 재현: {code}      ", end="\r", flush=True)
    print(" " * 40, end="\r")

    W = 104
    print(f"\n{'=' * W}")
    print(f"스코어링 세부 항목 정보량 ({n_days:,}종목·일 · 전방 {args.fwd}/{args.fwd2}거래일)")
    print(f"{'=' * W}")
    print(f"{'항목':<22}{'발화율%':>9}{'발화시':>9}{'미발화':>9}{'차이':>9}"
          f"{'중앙차':>9}{f'{args.fwd2}일차':>9}{'표본':>9}")
    print("-" * W)
    rows_out = []
    for k in sorted(single, key=lambda x: -single[x]):
        f = np.array(fired[k]) if fired[k] else np.array([0.0])
        u = np.array(unfired[k]) if unfired[k] else np.array([0.0])
        f2 = np.array(fired2[k]) if fired2[k] else np.array([0.0])
        u2 = np.array([x for kk in [k] for x in []]) if False else None
        rate = single[k] / n_days * 100
        diff = float(f.mean() - u.mean())
        mdiff = float(np.median(f) - np.median(u))
        rows_out.append((k, rate, float(f.mean()), float(u.mean()), diff, mdiff,
                         float(f2.mean()), len(f)))
    for k, rate, fm, um, diff, mdiff, f2m, n in sorted(rows_out, key=lambda x: x[4]):
        print(f"{k:<22}{rate:>9.1f}{fm:>9.2f}{um:>9.2f}{diff:>9.2f}{mdiff:>9.2f}{f2m:>9.2f}{n:>9,}")

    print("\n" + "-" * W)
    print("점수 구간별 전방 수익률 — 평균·중앙만 보면 안 된다.")
    print("  이 시스템은 추세추종이라 소수의 큰 수익이 성과를 만든다(슬롯 교체 실험에서 승률이")
    print("  올라도 수익이 6분의 1이 된 것이 그 실증이다). 그래서 꼬리를 함께 본다 —")
    print("  상위10% = 그 구간 상위 10% 전방수익 평균 · 20%+ = 20% 이상 오른 날의 비율.")
    print(f"{'점수':<12}{'표본':>10}{'평균%':>9}{'중앙%':>9}{'승률%':>9}"
          f"{'상위10%':>10}{'20%+':>8}{'하위10%':>10}")
    print("-" * W)
    sc = np.array(scores)
    fw = np.array(score_fwd)
    for lo, hi in [(0, 3), (3, 5), (5, 6), (6, 7), (7, 8), (8, 12)]:
        m = (sc >= lo) & (sc < hi)
        if m.sum() == 0:
            continue
        v = np.sort(fw[m])[::-1]
        top = v[:max(1, len(v) // 10)]
        bot = v[-max(1, len(v) // 10):]
        print(f"{f'{lo}~{hi}':<12}{int(m.sum()):>10,}{fw[m].mean():>9.2f}"
              f"{np.median(fw[m]):>9.2f}{(fw[m] > 0).mean() * 100:>9.1f}"
              f"{top.mean():>10.2f}{(fw[m] >= 20).mean() * 100:>8.2f}{bot.mean():>10.2f}")

    print("\n" + "-" * W)
    print("동시 발화율 상위 — 같은 정보를 두 번 세고 있는 후보(중복 계상 진단).")
    print(f"{'항목 A':<22}{'항목 B':<22}{'동시%':>9}{'A발화중 B비율%':>16}")
    print("-" * W)
    tops = sorted(pair.items(), key=lambda x: -x[1])[:15]
    for (a, b), c in tops:
        print(f"{a:<22}{b:<22}{c / n_days * 100:>9.1f}{c / max(single[a], 1) * 100:>16.1f}")

    print("\n[한계] 전방 수익률은 신호의 값어치이지 시스템 성과가 아니다. 창이 겹쳐 자기상관이")
    print("       있으므로 차이의 크기와 표본 수만 보고, 채택 판정은 짝비교 백테스트로 할 것.")


if __name__ == "__main__":
    main()
