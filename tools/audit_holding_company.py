"""지주회사·기타 금융업을 관심종목에서 배제해야 하는가.

[왜 지금] 2026-08-16에 만든 탐색 메뉴([7-4])가 배제 규칙을 화면에 표로 띄운다. 그중
 지주회사만 근거 칸이 "개별 사업의 추세가 희석돼 신호가 흐려짐"이라는 **주장**이다.
 방어주는 같은 의심에서 출발해 tools/audit_defensive_sector.py 로 신호 전방수익까지
 재고 나서 규칙이 됐는데(+0.02% vs +3.04%), 지주회사는 그 절차를 건너뛰었다.
 화면에 근거를 적어 놓고 실제로는 측정이 없으면 그 화면 전체를 믿을 수 없다.

[무엇을 재는가] 방어주 감사와 같은 3축. 도구를 새로 만드는 대신 그 축을 그대로 빌려온다.
  A) 신호 성질 — 진입 조건이 성립한 날의 전방 20/60일 수익. 슬롯 경쟁과 무관해 표본이
     크고(수천 건) 여기서 갈리면 그것이 근본 원인이다.
  B) 변동성으로 환원되는가 — 지주회사는 ATR%가 낮다. 라벨이 '저변동성'의 대리 변수라면
     업종 라벨이 아니라 일반화된 다이얼로 다뤄야 한다(방어주에서 이미 기각된 길).
  C) 포트폴리오 짝비교 — 같은 표본·같은 창에서 배제 유무만 바꾼다.

[라벨을 어떻게 정하는가] 탐색 메뉴와 **같은 규칙**을 쓴다(discover.HOLDING_KEYWORDS —
 업종 문자열에 '지주회사' 또는 '기타 금융업'). 감사가 다른 정의를 쓰면 그 결과로
 메뉴의 규칙을 채택하거나 지울 수 없다.

[대조군] 방어주 감사의 대조군(경기민감 13종목)은 결과를 알고 고른 종목이라 선택편향이
 있었다. 여기서는 **시총 순위가 가장 가까운 비(非)지주 종목**을 짝으로 붙인다. 크기를
 통제하면 '지주회사라서'와 '그 크기의 종목이라서'가 갈린다.

[실행] python3 tools/audit_holding_company.py --only A,B,C --trials 12 --sample 25
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, entry_days, metrics, new_scale_fn_factory,
)
from modules.manage.discover import DEFENSIVE_KEYWORDS, HOLDING_KEYWORDS  # noqa: E402
from tools.audit_common import seed_notice  # noqa: E402


def _is_defensive(industry):
    t = str(industry or "")
    return any(k in t for k, _lab in DEFENSIVE_KEYWORDS)


def _is_holding(industry):
    t = str(industry or "")
    return any(k in t for k in HOLDING_KEYWORDS)


def pick_groups(n, pool, exclude):
    """지주회사 n종목과 시총 순위가 인접한 비지주 대조군 n종목을 고른다.

    [왜 인접인가] 지주회사는 시총 분포가 관심종목과 다르다. 무작위 대조군을 쓰면
     '지주회사라서 진 것'과 '그 크기 구간이라서 진 것'이 섞인다. 순위 이웃을 짝지으면
     크기가 통제되고 라벨만 남는다.
    """
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])].dropna(subset=["Marcap"])
    bad = (df["Name"].str.contains("스팩|리츠", na=False)
           | df["Code"].str.endswith(("5", "7", "9")))
    df = df[~bad].sort_values("Marcap", ascending=False).head(pool).reset_index(drop=True)
    # 업종은 KRX 목록에 없다 — 탐색 메뉴와 같은 소스(KRX-DESC)에서 가져온다.
    desc = fdr.StockListing("KRX-DESC").set_index("Code")

    def _ind(code):
        if code not in desc.index:
            return ""
        v = desc.loc[code, "Industry"]
        return v.iloc[0] if hasattr(v, "iloc") else v

    rows = [(i, r["Code"], r["Name"], _ind(r["Code"]), r["Marcap"]) for i, r in df.iterrows()]
    hold = [r for r in rows if _is_holding(r[3]) and r[1] not in exclude]
    hold = hold[:n]
    used = {r[1] for r in hold} | set(exclude)
    ctrl = []
    for i, code, _name, _hind, _mc in hold:   # _ind 함수와 이름이 겹치지 않게 둔다
        # 순위가 가까운 순으로 훑어 비지주·비방어 종목을 하나 붙인다.
        for off in range(1, pool):
            for j in (i - off, i + off):
                if not 0 <= j < len(rows):
                    continue
                cand = rows[j]
                if cand[1] in used or _is_holding(cand[3]) or _is_defensive(cand[3]):
                    continue
                ctrl.append(cand)
                used.add(cand[1])
                break
            else:
                continue
            break
    return hold, ctrl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="A,B,C")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--n-hold", type=int, default=14)
    ap.add_argument("--pool", type=int, default=500, help="탐색 메뉴와 같은 시총 상위 범위")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seeds", default="20260816,7,101")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    only = args.only.split(",")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    base = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    have = {c for c, _ in base}

    hold, ctrl = pick_groups(args.n_hold, args.pool, have)
    HOLD = {r[1] for r in hold}
    CTRL = {r[1] for r in ctrl}
    print(f"[준비] 지주 {len(hold)}종목: " + ", ".join(f"{r[2]}" for r in hold))
    print(f"[준비] 대조(시총 이웃) {len(ctrl)}종목: " + ", ".join(f"{r[2]}" for r in ctrl))

    extra = [(r[1], r[2]) for r in hold + ctrl]
    names = dict(base + extra)
    dfs, mf, dates, failed = pb.prepare_universe(base + extra, args.days)
    HOLD &= set(dfs)
    CTRL &= set(dfs)
    print(f"[준비] 사용 {len(dfs)}종목 (지주 {len(HOLD)} · 대조 {len(CTRL)}) · 거래일 {len(dates)}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    BASE = [c for c, _ in base if c in dfs]
    new_scale = new_scale_fn_factory(dates, args.days)

    def group_of(code):
        return "지주" if code in HOLD else ("대조" if code in CTRL else "관심")

    bounds = ["20160816", "20180101", "20200101", "20220101", "20240101", "20270101"]
    W = [("전체", list(dates))]
    W += [(f"{bounds[k][2:6]}~{bounds[k + 1][2:6]}",
           [d for d in dates if bounds[k] <= d < bounds[k + 1]]) for k in range(5)]
    W = [(n, d) for n, d in W if d]

    # ---------------- A) 신호 성질 ----------------
    if "A" in only:
        sig = entry_days(dfs, status, mf)
        pool_g, rows = {}, []
        for code, (rec, hits) in sig.items():
            cl = np.array([r["close"] for r in rec], dtype=float)
            n = len(rec)
            g = group_of(code)
            f20 = [(cl[i + 20] - cl[i]) / cl[i] * 100 for i in hits if i + 20 < n]
            f60 = [(cl[i + 60] - cl[i]) / cl[i] * 100 for i in hits if i + 60 < n]
            at = [rec[i]["ATR"] / cl[i] * 100 for i in hits if rec[i].get("ATR")]
            p = pool_g.setdefault(g, {"f20": [], "f60": [], "atr": []})
            p["f20"] += f20; p["f60"] += f60; p["atr"] += at
            if f20 and g != "관심":
                rows.append((g, names.get(code, code), len(hits),
                             np.median(at) if at else float("nan"),
                             np.mean(f20), np.median(f20), np.percentile(f20, 90),
                             np.mean(f60) if f60 else float("nan"),
                             float(np.mean(np.array(f20) > 0) * 100)))
        print("\n[A] 진입 신호일의 전방수익 — 지주·대조 종목별")
        print(f"{'그룹':<5}{'종목':<14}{'신호':>6}{'ATR%':>7}{'20일평균':>9}{'20일중앙':>9}"
              f"{'20일P90':>9}{'60일평균':>9}{'승률%':>7}")
        for r in sorted(rows, key=lambda x: (x[0], -x[4])):
            print(f"{r[0]:<5}{r[1][:13]:<14}{r[2]:>6}{r[3]:>7.2f}{r[4]:>9.2f}{r[5]:>9.2f}"
                  f"{r[6]:>9.1f}{r[7]:>9.2f}{r[8]:>7.1f}")
        print("\n[A-집계] 그룹별 (신호일 가중)")
        print(f"{'그룹':<6}{'신호수':>7}{'ATR%중앙':>9}{'20일평균':>9}{'20일중앙':>9}"
              f"{'P90':>8}{'P99':>8}{'60일평균':>9}{'승률%':>7}")
        for g in ("지주", "대조", "관심"):
            if g not in pool_g:
                continue
            a = np.array(pool_g[g]["f20"]); b = np.array(pool_g[g]["f60"])
            at = np.array(pool_g[g]["atr"])
            print(f"{g:<6}{len(a):>7}{np.median(at):>9.2f}{a.mean():>9.2f}{np.median(a):>9.2f}"
                  f"{np.percentile(a, 90):>8.2f}{np.percentile(a, 99):>8.2f}{b.mean():>9.2f}"
                  f"{(a > 0).mean() * 100:>7.1f}")

    # ---------------- B) 변동성으로 환원되는가 ----------------
    if "B" in only:
        sig = entry_days(dfs, status, mf)
        recs = []
        for code, (rec, hits) in sig.items():
            cl = np.array([r["close"] for r in rec], dtype=float)
            n = len(rec)
            for i in hits:
                if i + 20 >= n or not rec[i].get("ATR"):
                    continue
                recs.append((group_of(code), rec[i]["ATR"] / cl[i] * 100,
                             (cl[i + 20] - cl[i]) / cl[i] * 100,
                             (cl[i + 60] - cl[i]) / cl[i] * 100 if i + 60 < n else np.nan))
        G = np.array([x[0] for x in recs]); at = np.array([x[1] for x in recs])
        f20 = np.array([x[2] for x in recs]); f60 = np.array([x[3] for x in recs])
        print("\n[B] 같은 ATR% 구간 안에서 지주 vs 대조 vs 관심")
        print(f"{'ATR% 구간':<12}{'그룹':<6}{'건수':>7}{'20일평균':>9}{'20일중앙':>9}"
              f"{'P90':>8}{'P99':>8}{'60일평균':>9}{'승률%':>7}")
        for lo, hi in [(0, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 4.0), (4.0, 99)]:
            m0 = (at >= lo) & (at < hi)
            for lab in ("지주", "대조", "관심"):
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
        hold_in = sorted(HOLD)
        rest = [c for c in BASE if c not in HOLD]
        # 지주회사는 표본에 항상 넣는다 — 없으면 배제 팔과 기준선이 같아져 정보가 0이다.
        picks = {sd: [hold_in + random.Random(sd * 7 + i).sample(
            rest, max(0, args.sample - len(hold_in))) for i in range(args.trials)]
            for sd in seeds}

        def run(codes, wd, gate):
            return pb.run_portfolio({c: dfs[c] for c in codes}, {c: status[c] for c in codes}, wd,
                                    initial_capital=INITIAL_CAPITAL, slots=slots,
                                    market_filter_dates={c: mf.get(c, set()) for c in codes},
                                    risk_scale_by_date=new_scale(), entry_gate=gate)

        # [필수 대조군] 이 설계는 '관심종목이 아닌 종목 14개를 표본에 억지로 넣었다가 뺀다'는
        #  뜻이기도 하다. 관심종목은 운영자가 추세를 보고 고른 목록이라 선택편향이 크고,
        #  그 목록에 무엇을 섞든 빼면 좋아질 수 있다. 그래서 **시총 이웃 대조군**으로 같은
        #  실험을 한 번 더 돌린다. 대조군 배제도 비슷하게 이기면 이 축이 잰 것은
        #  '지주회사라서'가 아니라 '관심종목이 아니라서'다.
        ctrl_in = sorted(CTRL)[:len(hold_in)]
        picks_c = {sd: [ctrl_in + random.Random(sd * 7 + i).sample(
            [c for c in BASE if c not in CTRL], max(0, args.sample - len(ctrl_in)))
            for i in range(args.trials)] for sd in seeds}

        def paired(label, picks_map, gate_fn):
            print(f"\n[{label}] 표본 {args.sample}(강제 포함 {len(hold_in)}) · "
                  f"{args.trials}회 × 씨드 {len(seeds)}개")
            print(f"{'창':<12}{'쌍':>5}{'배제승':>7}{'무':>4}{'배제패':>7}{'수익 기준':>11}"
                  f"{'수익 배제':>11}{'차이':>9}{'MDD기준':>9}{'MDD배제':>9}{'꼬리기준':>9}"
                  f"{'꼬리배제':>9}")
            for wn, wd in W:
                b = [metrics(run(p, wd, None)) for sd in seeds for p in picks_map[sd]]
                e = [metrics(run(p, wd, gate_fn)) for sd in seeds for p in picks_map[sd]]
                win = sum(1 for x, y in zip(e, b) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(e, b) if abs(x["ret"] - y["ret"]) <= 1e-9)
                g = lambda ms, k: np.mean([m[k] for m in ms])  # noqa: E731
                print(f"{wn:<12}{len(b):>5}{win:>7}{tie:>4}{len(b) - win - tie:>7}"
                      f"{g(b, 'ret'):>11.2f}{g(e, 'ret'):>11.2f}"
                      f"{g(e, 'ret') - g(b, 'ret'):>9.2f}"
                      f"{g(b, 'mdd'):>9.2f}{g(e, 'mdd'):>9.2f}{g(b, 'top10'):>9.2f}"
                      f"{g(e, 'top10'):>9.2f}", flush=True)

        paired("C) 지주회사 배제 짝비교", picks, lambda day, code, held: code in HOLD)
        paired("C-대조) 시총 이웃 배제 짝비교", picks_c, lambda day, code, held: code in CTRL)


if __name__ == "__main__":
    main()
