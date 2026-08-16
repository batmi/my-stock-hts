"""아직 한 번도 훑지 않은 다이얼 네 개 — 있는 줄도 몰랐던 축들.

[어떻게 골랐나] config의 전략 키를 감사 도구 48개와 대조해 '한 번도 등장하지 않은 키'를
 추린 뒤, UI·인프라 키를 빼고 **실제로 매매 결정을 바꾸는 것**만 남겼다. 네 축 모두
 감사 도구 등장 0회다.

  A) 피라미딩 시장 게이트 `PYRAMIDING_REQUIRE_HEALTHY_MARKET`
     시장 필터가 보류인 시장의 종목은 증액도 멈춘다. portfolio_backtest는 이 규칙을
     구현해 두었는데(실매매와 같게), 정작 **켜고 끄고 비교한 적이 없다.**
  B) 변동성 스케일링 상한 `VOLATILITY_SCALING_MAX`(2.0)
     사이징 3층 중 변동성층이 88~96% 구속한다는 실측은 있는데, 그 실측은 전부 **하한**
     (0.4) 이야기였다. 상한은 저변동 종목에서만 걸려 한 번도 안 쟀다.
  C) 동적 ATR 캡의 지수 `ATR_CAP_VOL_POWER`(0.5)
     동적 캡 자체는 '평시 무해 + 고변동 국면에서만 작동'으로 채택됐지만, 캡이 변동성에
     얼마나 민감하게 반응할지 정하는 이 지수는 근거 없이 0.5다.
  E) 갭 리스크 버퍼 `GAP_RISK_BUFFER`(1.2)
     리스크 기반 사이징에서 손절폭에 곱하는 보수 계수. 리스크층이 '구속률 0.0%'라는
     실측이 있으니 이 값도 무동작일 것으로 보이는데, **그 예측을 확인한 적이 없다.**
  D) 상관관계 문턱 `CORRELATION_THRESHOLD`(0.7)
     '10년에 22건뿐'이라는 기록은 있으나 문턱을 흔들어 본 적은 없다. 22건이 문턱 탓인지
     시스템이 애초에 비슷한 종목을 동시에 사지 않아서인지 갈린다.

[실행] python3 tools/audit_unswept_dials.py --axis A,B,C,E,D --trials 12 --sample 25
"""
import argparse
import os
import random
import sys

import numpy as np
import pandas as pd  # noqa: F401  (CorrGate가 쓴다)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)
from tools.audit_entry_gate_parity import CorrGate  # noqa: E402


def apply_cfg(pairs):
    """(대상, 키, 값) 목록을 적용하고 되돌릴 목록을 돌려준다."""
    prev = []
    for tgt, key, val in pairs:
        d = {"sell": config.SELL_STRATEGY, "at": config.ANALYSIS_THRESHOLDS,
             "risk": config.RISK_SCALING_PARAMS}.get(tgt)   # 'settings'는 최상위 값
        if d is not None:
            prev.append((tgt, key, d.get(key)))
            d[key] = val
        else:                                   # settings 최상위 값
            prev.append((tgt, key, getattr(config, key, None)))
            setattr(config, key, val)
    return prev


AXES = {
    # (라벨, config 오버라이드, run_portfolio 추가 인자)
    # ※ 이 키는 SELL_STRATEGY가 아니라 ANALYSIS_THRESHOLDS에 있다(pb도 thr에서 읽는다).
    #   'sell'로 잘못 쓰면 아무것도 안 바뀌어 0-N-0(무동작)으로 보인다 — 실제로 한 번 그랬다.
    "A": ("피라미딩 시장 게이트", [
        ("현행 ON", [("at", "PYRAMIDING_REQUIRE_HEALTHY_MARKET", True)], {}),
        ("OFF (보류장에서도 증액)", [("at", "PYRAMIDING_REQUIRE_HEALTHY_MARKET", False)], {}),
    ]),
    "B": ("변동성 스케일링 상한", [
        ("현행 2.0", [("cfg", "VOLATILITY_SCALING_MAX", 2.0)], {}),
        ("1.5", [("cfg", "VOLATILITY_SCALING_MAX", 1.5)], {}),
        ("3.0", [("cfg", "VOLATILITY_SCALING_MAX", 3.0)], {}),
        ("1.0 (확대 금지)", [("cfg", "VOLATILITY_SCALING_MAX", 1.0)], {}),
    ]),
    "C": ("동적 ATR 캡 지수", [
        ("현행 0.5", [("sell", "ATR_CAP_VOL_POWER", 0.5)], {}),
        ("0.3 (둔감)", [("sell", "ATR_CAP_VOL_POWER", 0.3)], {}),
        ("0.8 (민감)", [("sell", "ATR_CAP_VOL_POWER", 0.8)], {}),
        ("캡 고정 (동적 OFF)", [("sell", "ATR_CAP_DYNAMIC", False)], {}),
    ]),
    "E": ("갭 리스크 버퍼", [
        ("현행 1.2", [("risk", "GAP_RISK_BUFFER", 1.2)], {}),
        ("1.0 (미사용)", [("risk", "GAP_RISK_BUFFER", 1.0)], {}),
        ("2.0", [("risk", "GAP_RISK_BUFFER", 2.0)], {}),
    ]),
    "D": ("상관관계 문턱", [
        ("필터 OFF", None, {"corr": None}),
        ("현행 0.7", None, {"corr": 0.7}),
        ("0.5 (엄격)", None, {"corr": 0.5}),
        ("0.9 (느슨)", None, {"corr": 0.9}),
    ]),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="A,B,C,E,D")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
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

    k = max(1, args.subperiods)
    size = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
          for i in range(k)]

    codes = list(dfs)
    picks = {sd: [random.Random(sd * 29 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    # 상관 게이트는 종목쌍 롤링이라 비싸다 — 문턱별로 한 번만 만들어 재사용한다.
    corr_gates = {}
    if "D" in args.axis:
        for t in (0.5, 0.7, 0.9):
            corr_gates[t] = CorrGate(dfs, dates, t)
            print(f"[준비] 상관 게이트 문턱 {t}", flush=True)

    for ax in args.axis.split(","):
        ax = ax.strip()
        if ax not in AXES:
            continue
        title, arms = AXES[ax]
        print(f"\n\n=========== 축 {ax} · {title} ===========")
        for wn, wd in W:
            print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
            print(f"{'팔':<22}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
            base_res = None
            for label, ov, extra in arms:
                res = []
                prev = apply_cfg([("settings" if t == "cfg" else t, k2, v)
                                   for t, k2, v in ov]) if ov else None
                try:
                    for sd in seeds:
                        for pick in picks[sd]:
                            kw = {}
                            if "corr" in extra:
                                t = extra["corr"]
                                if t is not None:
                                    g = corr_gates[t]
                                    kw["entry_gate"] = (lambda day, code, held, _g=g:
                                                        _g(day, code, held))
                            r = pb.run_portfolio(
                                {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                                initial_capital=INITIAL_CAPITAL, slots=slots,
                                market_filter_dates={c: mf.get(c, set()) for c in pick},
                                risk_scale_by_date=new_scale(), **kw)
                            res.append(metrics(r))
                finally:
                    if prev:
                        apply_cfg(prev)
                g2 = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if base_res is None:
                    base_res = res
                    wl = "—"
                else:
                    win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base_res)
                              if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<22}{g2('ret'):>9.1f}{g2('mdd'):>8.1f}{g2('mar'):>7.2f}"
                      f"{g2('pf'):>6.2f}{g2('n'):>6.0f}{g2('top10'):>9.1f}{g2('win'):>7.1f}"
                      f"{wl:>10}", flush=True)

    print("\n[읽는 법] 완전 동률(0-N-0)은 '그 다이얼이 아무 일도 하지 않는다'는 뜻이다. "
          "값을 바꿀 이유도, 남겨 둘 위험도 그만큼 작다.")


if __name__ == "__main__":
    main()
