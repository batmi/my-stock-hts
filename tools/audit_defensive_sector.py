"""방어주(통신·전기가스·음식료·필수소비 유통)를 진입에서 제한해야 하는가.

[출발점] "방어주는 추세를 타는 일이 거의 없고 슬롯만 소진하는 것 아닌가"라는 의심.
 추세추종 시스템에는 이미 '추세 없음'을 거르는 장치가 있다(점수 7.0 게이트·상태 판정·
 RSI 캡·시장 필터·점수 순위 경쟁). 그 문턱을 넘은 신호까지 업종 라벨로 다시 자르는 것은
 측정 위에 선입견을 얹는 일이라, 라벨이 **측정 너머의 정보**를 갖는지부터 물어야 한다.

[무엇을 재는가]
  A) 신호 성질 — 슬롯 경쟁과 무관하게, 진입 조건이 성립한 날의 전방 20/60일 수익 분포.
     포트폴리오 성과는 경로의존성이 커서 한 번의 실행으로는 판단할 수 없다. 신호 단위
     통계는 표본이 1만 건 규모라 훨씬 단단하고, 여기서 갈리면 그것이 근본 원인이다.
  B) 저변동성으로 환원되는가 — 방어주는 ATR%가 낮다. 라벨이 단지 '저변동성'의 대리
     변수라면 일반화된 다이얼(ATR% 하한)로 대신해야 한다. 같은 ATR% 구간 안에서
     방어 vs 비방어를 비교해 가른다.
  C) 포트폴리오 짝비교 — 같은 표본·같은 창에서 배제 유무만 바꾼다. 전수 1회 실행은
     복리 경로의 추첨이라(방어주를 빼서 자리가 하루 당겨지면 이후 전 경로가 갈린다)
     반드시 여러 표본·여러 씨드의 승패로 판정한다.

[대조군] 유니버스를 키운 효과와 '방어주라서'를 가르기 위해 경기민감·성장주 13종목을
 같은 수만큼 붙여 함께 잰다. 다만 대조군은 결과를 알고 고른 종목이라 선택편향이 있으므로,
 채택 판정은 언제나 **현행 관심종목**과의 비교로 한다.

[실행] python tools/audit_defensive_sector.py [--only A,B,C] [--trials 12] [--sample 25]
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000  # 실거래 시드와 같게 둔다(seed-slot-sizing)
SELL_REASONS = ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "교체")

# 방어주 — KRX 업종 기준(통신 / 전기가스 / 음식료 / 필수소비 유통).
#  경기와 무관하게 수요가 유지된다고 통상 분류되는 업종만 넣는다. 제약·바이오는 한국에서
#  성장주로 거래되므로 제외했다(라벨의 뜻이 흐려지면 실험이 무엇을 쟀는지 알 수 없다).
DEFENSIVE = [
    ("017670", "SK텔레콤"), ("030200", "KT"), ("032640", "LG유플러스"),
    ("015760", "한국전력"), ("036460", "한국가스공사"), ("004690", "삼천리"),
    ("033780", "KT&G"), ("097950", "CJ제일제당"), ("271560", "오리온"),
    ("004370", "농심"), ("005300", "롯데칠성"),
    ("007070", "GS리테일"), ("139480", "이마트"),
]
# 대조군(경기민감·성장) — '유니버스가 커졌다'와 '방어주다'를 가르기 위한 짝.
CYCLICAL = [
    ("000270", "기아"), ("051910", "LG화학"), ("003670", "포스코퓨처엠"),
    ("247540", "에코프로비엠"), ("086520", "에코프로"), ("042700", "한미반도체"),
    ("240810", "원익IPS"), ("058470", "리노공업"), ("000990", "DB하이텍"),
    ("011200", "HMM"), ("011790", "SKC"), ("034220", "LG디스플레이"),
    ("009830", "한화솔루션"),
]
DEF_CODES = {c for c, _ in DEFENSIVE}
CYC_CODES = {c for c, _ in CYCLICAL}


def new_scale_fn_factory(dates, days):
    """[실행마다 새로 만든다] make_scale_fn 의 콜러블은 자산곡선 이력을 들고 있어
    돌려쓰면 드로다운 판정이 오염된다(같은 설정 재실행이 392.66% → 284.79%)."""
    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    rp = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(list(dates), days)
    dd = None
    if rp.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(rp.get("DD_LOOKBACK_DAYS", 90)), float(rp.get("DD_LEVEL_1", 5.0)),
              float(rp.get("DD_SCALE_1", 0.9)), float(rp.get("DD_LEVEL_2", 10.0)),
              float(rp.get("DD_SCALE_2", 0.8)))
    return lambda: make_scale_fn(mkt, dd)


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    wins = [p for p in profits if p > 0]
    return {"ret": r["total_return"], "mdd": r["mdd"],
            "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
            "pf": r["pf"], "top10": float(np.mean(top10)) if top10 else 0.0,
            "best": profits[0] if profits else 0.0,
            "big": sum(1 for p in profits if p >= 30),
            "win": len(wins) / len(profits) * 100 if profits else 0.0,
            "n": len(sells), "slots": r["avg_slots"]}


def entry_days(dfs, status, mf):
    """진입 조건(can_buy·점수·RSI 캡·시장필터)이 성립한 날 → (code, i, row)."""
    T = config.ANALYSIS_THRESHOLDS
    bs, br = T["BUY_SCORE"], T["BUY_RSI_MAX"]
    s_use, s_sc = T.get("SUPER_MOMENTUM_USE", True), T.get("SUPER_MOMENTUM_SCORE", 8.0)
    s_w52, s_rsi = T.get("SUPER_MOMENTUM_W52_POS", 90.0), T.get("SUPER_BUY_RSI_MAX", 80.0)
    out = {}
    for code, df in dfs.items():
        rec = df.to_dict("records")
        hits = []
        for i, r in enumerate(rec):
            d = str(r["date"])
            raw, _chk, can_buy, state, _ = status[code][d]
            if not can_buy or not (raw >= bs or state == "역매수"):
                continue
            is_super = s_use and raw >= s_sc and (r.get("w52_pos") or 0) >= s_w52
            if r["RSI"] >= (s_rsi if is_super else br):
                continue
            if d in mf.get(code, ()):        # 시장필터 차단일은 애초에 살 수 없다
                continue
            hits.append(i)
        out[code] = (rec, hits)
    return out


def group_of(code):
    return "방어" if code in DEF_CODES else ("대조" if code in CYC_CODES else "관심")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="A,B,C")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seeds", default="20260813,7,101,4242,31337")
    args = ap.parse_args()
    only = args.only.split(",")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    base = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    have = {c for c, _ in base}
    extra = [t for t in DEFENSIVE + CYCLICAL if t[0] not in have]
    names = dict(base + extra)
    print(f"[준비] 관심종목 {len(base)} + 추가 {len(extra)} · {args.days}일 · 슬롯 {slots}", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(base + extra, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 · 거래일 {len(dates)}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    BASE = [c for c, _ in base]
    new_scale = new_scale_fn_factory(dates, args.days)

    # 창: 전체 + 2년 구간 5개. 한 창의 결과는 추첨이므로 창을 나눠 안정성을 본다.
    bounds = ["20160816", "20180101", "20200101", "20220101", "20240101", "20270101"]
    W = [("전체", list(dates))]
    W += [(f"{bounds[k][2:6]}~{bounds[k + 1][2:6]}",
           [d for d in dates if bounds[k] <= d < bounds[k + 1]]) for k in range(5)]
    W = [(n, d) for n, d in W if d]

    # ---------------- A) 신호 성질 ----------------
    if "A" in only:
        sig = entry_days(dfs, status, mf)
        pool = {}
        print("\n[A] 진입 신호일의 전방수익 — 종목별")
        print(f"{'그룹':<5}{'종목':<14}{'신호':>6}{'ATR%':>7}{'20일평균':>9}{'20일중앙':>9}"
              f"{'20일P90':>9}{'60일평균':>9}{'승률%':>7}")
        rows = []
        for code, (rec, hits) in sig.items():
            cl = np.array([r["close"] for r in rec], dtype=float)
            n = len(rec)
            g = group_of(code)
            f20 = [(cl[i + 20] - cl[i]) / cl[i] * 100 for i in hits if i + 20 < n]
            f60 = [(cl[i + 60] - cl[i]) / cl[i] * 100 for i in hits if i + 60 < n]
            at = [rec[i]["ATR"] / cl[i] * 100 for i in hits if rec[i].get("ATR")]
            pool.setdefault(g, {"f20": [], "f60": [], "atr": []})
            pool[g]["f20"] += f20; pool[g]["f60"] += f60; pool[g]["atr"] += at
            if f20:
                rows.append((g, names.get(code, code), len(hits), np.median(at) if at else float("nan"),
                             np.mean(f20), np.median(f20), np.percentile(f20, 90),
                             np.mean(f60) if f60 else float("nan"),
                             float(np.mean(np.array(f20) > 0) * 100)))
        for r in sorted(rows, key=lambda x: (x[0], -x[4])):
            print(f"{r[0]:<5}{r[1][:13]:<14}{r[2]:>6}{r[3]:>7.2f}{r[4]:>9.2f}{r[5]:>9.2f}"
                  f"{r[6]:>9.1f}{r[7]:>9.2f}{r[8]:>7.1f}")
        print(f"\n[A-집계] 그룹별 (신호일 가중)")
        print(f"{'그룹':<6}{'신호수':>7}{'ATR%중앙':>9}{'20일평균':>9}{'20일중앙':>9}"
              f"{'P90':>8}{'P99':>8}{'60일평균':>9}{'승률%':>7}")
        for g in ("방어", "대조", "관심"):
            if g not in pool: continue
            a = np.array(pool[g]["f20"]); b = np.array(pool[g]["f60"]); at = np.array(pool[g]["atr"])
            print(f"{g:<6}{len(a):>7}{np.median(at):>9.2f}{a.mean():>9.2f}{np.median(a):>9.2f}"
                  f"{np.percentile(a, 90):>8.2f}{np.percentile(a, 99):>8.2f}{b.mean():>9.2f}"
                  f"{(a > 0).mean() * 100:>7.1f}")

    # ---------------- B) 저변동성으로 환원되는가 ----------------
    if "B" in only:
        sig = entry_days(dfs, status, mf)
        recs = []
        for code, (rec, hits) in sig.items():
            cl = np.array([r["close"] for r in rec], dtype=float); n = len(rec)
            for i in hits:
                if i + 20 >= n or not rec[i].get("ATR"): continue
                recs.append((group_of(code), rec[i]["ATR"] / cl[i] * 100,
                             (cl[i + 20] - cl[i]) / cl[i] * 100,
                             (cl[i + 60] - cl[i]) / cl[i] * 100 if i + 60 < n else np.nan))
        G = np.array([x[0] for x in recs]); at = np.array([x[1] for x in recs])
        f20 = np.array([x[2] for x in recs]); f60 = np.array([x[3] for x in recs])
        print("\n[B] 같은 ATR% 구간 안에서 방어 vs 관심 — 라벨이 변동성 너머의 정보를 갖는가")
        print(f"{'ATR% 구간':<12}{'그룹':<6}{'건수':>7}{'20일평균':>9}{'20일중앙':>9}"
              f"{'P90':>8}{'P99':>8}{'60일평균':>9}{'승률%':>7}")
        for lo, hi in [(0, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 4.0), (4.0, 99)]:
            m0 = (at >= lo) & (at < hi)
            for lab in ("방어", "관심"):
                m = m0 & (G == lab)
                if m.sum() < 30:
                    print(f"{f'{lo:.1f}~{hi:.1f}':<12}{lab:<6}{m.sum():>7}   (표본 부족)")
                    continue
                a = f20[m]; b = f60[m][~np.isnan(f60[m])]
                print(f"{f'{lo:.1f}~{hi:.1f}':<12}{lab:<6}{m.sum():>7}{a.mean():>9.2f}"
                      f"{np.median(a):>9.2f}{np.percentile(a, 90):>8.1f}"
                      f"{np.percentile(a, 99):>8.1f}{b.mean():>9.2f}{(a > 0).mean() * 100:>7.1f}")

    # ---------------- C) 포트폴리오 짝비교 ----------------
    if "C" in only:
        seeds = [int(s) for s in args.seeds.split(",")]
        defs_in = [c for c in BASE if c in DEF_CODES]
        rest = [c for c in BASE if c not in DEF_CODES]
        # 방어주는 표본에 항상 넣는다 — 표본에 없으면 배제 팔과 기준선이 같아져 정보가 0이다.
        picks = {sd: [defs_in + random.Random(sd * 7 + i).sample(
            rest, max(0, args.sample - len(defs_in))) for i in range(args.trials)] for sd in seeds}

        def run(codes, wd, gate):
            return pb.run_portfolio({c: dfs[c] for c in codes}, {c: status[c] for c in codes}, wd,
                                    initial_capital=INITIAL_CAPITAL, slots=slots,
                                    market_filter_dates={c: mf.get(c, set()) for c in codes},
                                    risk_scale_by_date=new_scale(), entry_gate=gate)

        gate = lambda day, code, held: code in DEF_CODES  # noqa: E731
        print(f"\n[C] 방어주 배제 짝비교 — 표본 {args.sample}(방어 {len(defs_in)} 강제 포함) · "
              f"{args.trials}회 × 씨드 {len(seeds)}개")
        print(f"{'창':<12}{'쌍':>5}{'배제승':>7}{'무':>4}{'배제패':>7}{'수익 기준':>11}{'수익 배제':>11}"
              f"{'차이':>9}{'MDD기준':>9}{'MDD배제':>9}{'꼬리기준':>9}{'꼬리배제':>9}")
        for wn, wd in W:
            b = [metrics(run(p, wd, None)) for sd in seeds for p in picks[sd]]
            e = [metrics(run(p, wd, gate)) for sd in seeds for p in picks[sd]]
            win = sum(1 for x, y in zip(e, b) if x["ret"] > y["ret"] + 1e-9)
            tie = sum(1 for x, y in zip(e, b) if abs(x["ret"] - y["ret"]) <= 1e-9)
            g = lambda ms, k: np.mean([m[k] for m in ms])  # noqa: E731
            print(f"{wn:<12}{len(b):>5}{win:>7}{tie:>4}{len(b) - win - tie:>7}"
                  f"{g(b, 'ret'):>11.2f}{g(e, 'ret'):>11.2f}{g(e, 'ret') - g(b, 'ret'):>9.2f}"
                  f"{g(b, 'mdd'):>9.2f}{g(e, 'mdd'):>9.2f}{g(b, 'top10'):>9.2f}{g(e, 'top10'):>9.2f}")


if __name__ == "__main__":
    main()
