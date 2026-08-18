"""시장 필터의 기준선 기간(MARKET_FILTER_MA)과 이탈 밴드(MARKET_FILTER_BAND) 재측정.

[왜 다시] 현행 SMA80 ± 1%는 2026-08-03에 60에서 옮겨온 값이고, 근거의 한 축이
 '포트폴리오 백테스트 경로 234개'였다. 그런데 그 측정을 만든 도구는 남아 있지 않고
 (감사 도구 어디에도 MARKET_FILTER_MA를 흔드는 코드가 없다), 무엇보다 당시의
 portfolio_backtest **기본 정렬이 실매매와 달랐다** — 점수만 보고 동점을 관심종목
 등록 순서로 갈랐다([[backtest-tiebreak-parity]]). 시장 필터는 진입을 통째로 막는
 팔이라 순위에 정면으로 닿는다. 같은 결함이 있던 계측기에서 '무작위 차단만으로도
 기준선을 이기는' 가짜 신호가 나왔으므로, 이 축의 수치는 재현되기 전까지 믿을 수 없다.

[무엇이 달라졌나]
  · 엔진 기본 정렬이 실매매(점수 → 추세품질 → 52주위치)다. 도구는 워밍업만 자른다.
  · **같은 차단율 무작위 대조**를 세운다([[entry-filter-random-control]]). 필터는
    '지수를 보고' 막는 장치인데, 단지 '덜 사서' 좋아진 것이라면 아무 날이나 같은 수만큼
    막아도 같은 결과가 나와야 한다. 대조는 티커별 차단일 수를 그대로 맞춰 날짜만
    무작위로 고른다 — 차단 '양'은 같고 '고른 이유'만 없앤 팔이다.
  · 폐지 종목을 섞는다. 이 축의 값어치는 대부분 낙폭에 있는데, 생존 편향은 MDD를
    10%p 부풀린다([[survivorship-premium-2x]]) — 방어 장치를 생존자만으로 재면 안 된다.

[읽는 법] 채택 규칙은 종전대로 수익 승-무-패이되, 방어 장치이므로 MDD·MAR을 함께 본다.
 기간 팔끼리는 서로 대조군이 되어 주므로(모두 같은 종류의 차단) 무작위 대조는 '필터라는
 장치 자체'가 값을 하는지를 가르는 데 쓴다.

[실행] python3 tools/audit_market_filter_period.py --trials 12
       python3 tools/audit_market_filter_period.py --only band
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
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)
from tools.audit_universe import dead_targets, extend_targets  # noqa: E402


def _ticker_of(code):
    """backtest.prepare_market_filter 와 같은 규칙으로 종목 → 지수 티커."""
    for key in ("stocks_kr", "etfs_kr"):
        for item in config.session.stock_data.get(key, []):
            if item.get("code") == code and item.get("exchange"):
                return "^KQ11" if str(item["exchange"]).upper() == "KOSDAQ" else "^KS11"
    return "^KS11"


def blocked_by_code(codes, days, ma, band, use_filter=True):
    """{code: 차단일 집합}, {티커: 차단일 집합}.

    **production 함수(backtest.prepare_market_filter)를 그대로 호출한다.** 판정식을
    도구에 복제하면 config를 바꿨을 때 조용히 갈라진다 — 실매매와 다른 필터를 재게 된다.
    지수는 둘뿐이라 티커마다 한 번만 계산한다.
    """
    prev = (config.MARKET_FILTER_MA, config.MARKET_FILTER_BAND, config.USE_MARKET_FILTER)
    config.settings.MARKET_FILTER_MA = int(ma)
    config.settings.MARKET_FILTER_BAND = float(band)
    config.settings.USE_MARKET_FILTER = bool(use_filter)
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
        (config.settings.MARKET_FILTER_MA, config.settings.MARKET_FILTER_BAND,
         config.settings.USE_MARKET_FILTER) = prev
        backtest._MARKET_FILTER_STATE["key"] = None   # 다음 호출이 캐시를 재사용하지 않게


def random_like(per_ticker, dates, salt):
    """티커별 차단일 **수**만 맞추고 날짜는 무작위로 고른 대조 필터.

    '덜 샀다'와 '지수를 보고 막았다'를 가르는 유일한 방법이다. 차단일 수가 같으므로
    노출 감소량이 같고, 다른 것은 '언제 막았는가' 하나뿐이다.
    """
    pool = list(dates)
    out = {}
    for tk, blocked in per_ticker.items():
        n = len(blocked & set(pool))
        rng = random.Random(f"{salt}|{tk}|{n}")
        out[tk] = set(rng.sample(pool, min(n, len(pool))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--placebo-draws", type=int, default=5)
    ap.add_argument("--baseline", default="off", choices=("off", "current"),
                    help="off=필터 OFF 기준(장치의 값어치) · current=현행 기준(값끼리 짝비교)")
    ap.add_argument("--only", default="ma,band",
                    help="ma=기간 스윕 · band=이탈 밴드 스윕")
    ap.add_argument("--periods", default="40,60,80,100,120")
    ap.add_argument("--bands", default="0,1,2")
    args = ap.parse_args()
    seeds = [int(x) for x in args.seeds.split(",")]
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    only = [x.strip() for x in args.only.split(",")]

    cur_ma = int(getattr(config, "MARKET_FILTER_MA", 80))
    cur_band = float(getattr(config, "MARKET_FILTER_BAND", 1.0))

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    ext = extend_targets({c for c, _ in live}, 60, mode="random", pool=args.pool)
    n_dead = int(args.size * args.dead_frac)
    dfs, _mf, dates, _f = pb.prepare_universe(
        live + ext + dead_targets(n_dead + 10), args.days)
    # [계측기] 앞의 룩백-1일은 추세품질 이력이 없어 동점 가름이 등록 순서로 떨어진다.
    lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    dates = dates[lb - 1:]
    dead_set = {c for c, _ in dead_targets(n_dead + 10)}
    dead_c = [c for c in dfs if c in dead_set]
    live_c = [c for c in dfs if c not in dead_set]
    print(f"[준비] 생존 {len(live_c)} + 폐지 {len(dead_c)} → 표본 {args.size}종목 · "
          f"거래일 {len(dates)} · 슬롯 {slots} · 현행 SMA{cur_ma}±{cur_band:g}%", flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            picks[(sd, i)] = (rng.sample(dead_c, min(n_dead, len(dead_c)))
                              + rng.sample(live_c, args.size - n_dead))
    codes = sorted({c for p in picks.values() for c in p})

    # 설정별 차단일은 한 번만 계산해 재사용한다(지수 2개짜리 계산이라 캐시가 값을 한다).
    variants = {}          # 라벨 -> ({code: 차단일}, {티커: 차단일})
    # [기준선 선택] 기본은 '필터 OFF' — 장치 자체의 값어치를 묻는 자리다. 값을 서로
    #  견줄 때는(80 vs 100 같은) 현행을 기준선으로 놓아야 짝비교가 직접 성립한다.
    #  OFF를 끼고 재면 두 팔이 각각 OFF와의 승패로만 비교돼 '누가 더 나은가'를 못 가른다.
    if args.baseline == "off":
        variants["[기준선] 필터 OFF"] = ({c: set() for c in codes}, {})
    else:
        variants[f"[기준선] SMA{cur_ma} ±{cur_band:g}%  ← 현행"] = blocked_by_code(
            codes, args.days, cur_ma, cur_band)
    if "ma" in only:
        for ma in [int(x) for x in args.periods.split(",")]:
            lab = f"SMA{ma} ±{cur_band:g}%" + ("  ← 현행" if ma == cur_ma else "")
            if lab in variants or (args.baseline != "off" and ma == cur_ma):
                continue      # 기준선으로 이미 들어가 있다
            variants[lab] = blocked_by_code(codes, args.days, ma, cur_band)
    if "band" in only:
        for bd in [float(x) for x in args.bands.split(",")]:
            if "ma" in only and bd == cur_band:
                continue      # 기간 스윕의 현행 팔과 같다
            lab = f"SMA{cur_ma} ±{bd:g}%" + ("  ← 현행" if bd == cur_band else "")
            variants[lab] = blocked_by_code(codes, args.days, cur_ma, bd)

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
          for i in range(k)]

    def run(wd, mf_by_code):
        out = []
        for sd in seeds:
            for i in range(args.trials):
                pick = picks[(sd, i)]
                r = pb.run_portfolio(
                    {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                    initial_capital=INITIAL_CAPITAL, slots=slots,
                    market_filter_dates={c: mf_by_code.get(c, set()) for c in pick},
                    risk_scale_by_date=new_scale())
                out.append(dict(metrics(r), no_tq=r["rank_no_tq_pct"]))
        return out

    def show(label, res, base):
        g = lambda key: float(np.mean([m[key] for m in res]))    # noqa: E731
        if base is None:
            wl = "— (기준)"
        else:
            win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
            tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
            wl = f"{win}-{tie}-{len(res) - win - tie}"
        print(f"{label:<26}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
              f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}{wl:>10}", flush=True)
        return res

    for wn, wd in W:
        seg = set(wd)
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<26}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
        base = None
        cur_per_ticker = None
        for label, (mf_by_code, per_ticker) in variants.items():
            res = run(wd, mf_by_code)
            blocked = (max((len(v & seg) for v in per_ticker.values()), default=0)
                       / max(1, len(wd)) * 100)
            show(label + (f" [차단 {blocked:.1f}%]" if per_ticker else ""), res, base)
            if base is None:
                base = res
            if "← 현행" in label and per_ticker:
                cur_per_ticker = per_ticker
        no_tq = float(np.mean([m["no_tq"] for m in base]))
        if no_tq > 1.0:
            print(f"  [경고] 동점 가름에 추세품질을 못 쓴 비율 {no_tq:.1f}% — 이 창의 "
                  f"순위는 부분적으로 등록 순서다", flush=True)

        # 같은 차단'량' 무작위 대조 — 필터가 '지수를 보고' 막아서 값을 하는지 가른다.
        if cur_per_ticker:
            pooled, per_draw = [], []
            for j in range(max(1, args.placebo_draws)):
                rnd_tk = random_like(cur_per_ticker, wd, f"plc|{wn}|{j}")
                mf_rand = {c: rnd_tk.get(_ticker_of(c), set()) for c in codes}
                r = run(wd, mf_rand)
                pooled += r
                per_draw.append(float(np.mean([m["ret"] for m in r])))
            show(f"[대조] 무작위 차단 ({len(per_draw)}장)", pooled,
                 base * max(1, args.placebo_draws))
            print("      장별 수익%: " + " · ".join(f"{x:.1f}" for x in per_draw)
                  + f"  (표준편차 {np.std(per_draw):.1f})", flush=True)

    print("\n[읽는 법] 기간 팔은 서로가 대조군이다. 무작위 대조는 '필터라는 장치'가 "
          "값을 하는지를 가른다 — 현행 팔이 무작위와 비슷하면 공은 '덜 사는 것'에 있다.")
    print("[채택 규칙] 구간이 갈리면 채택하지 않는다. 방어 장치이므로 수익 승-무-패가 "
          "비슷하면 MDD·MAR로 가른다.")


if __name__ == "__main__":
    main()
