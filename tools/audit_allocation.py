"""포트폴리오 구성의 세 전제 — 균등 배분 · 업종 무제한 · 재진입 무제한.

[왜] 지금까지 잰 것은 전부 '얼마나 살 것인가(사이징 층)'와 '무엇을 살 것인가(스코어링)'
 였다. 그런데 그 위에 아무도 묻지 않은 전제가 셋 깔려 있다.
  A) **슬롯마다 똑같이 나눈다** — invest_ratio는 1/slots 고정이다. 점수가 9점인 후보와
     7점인 후보에 같은 돈을 넣는다. 스코어링을 그렇게 공들여 만들어 놓고, 정작 크기에는
     반영하지 않는다. 점수 가중·변동성 역가중을 한 번도 재본 적이 없다.
  B) **업종은 안 본다** — 상관관계 필터(0.7)가 대리 역할을 하지만 그건 가격 상관이고,
     같은 업종 3종목이 상관 0.6으로 동시에 담기는 것은 막지 못한다. 4슬롯짜리
     포트폴리오에서 업종 집중은 실질 슬롯 수를 줄인다.
  C) **판 종목을 바로 다시 산다** — 당일 재진입 허들은 있지만 그 다음 날은 자유다.
     추세가 꺾여 청산한 종목이 며칠 뒤 다시 후보로 올라오면 같은 실패를 반복할 수 있다.

[무엇을 재는가]
  축 A: 균등(현행) / 점수 가중 / 변동성 역가중 / 점수×변동성
  축 B: 업종 무제한(현행) / 같은 업종 최대 2 / 최대 1
  축 C: 쿨다운 없음(현행) / 청산 후 3일 / 5일 / 10일 재진입 금지
        (손실 청산에만 거는 팔도 함께 — 승자 회수 후 재진입까지 막으면 추세추종에 해롭다)

[한계] 업종은 FDR의 현재 분류를 과거에 그대로 적용한다(업종은 잘 바뀌지 않지만 편입·
 재분류는 있다). 쿨다운은 '청산 사유'를 백테스트 기록에서 읽으므로 실매매와 같은 기준이다.

[실행] python3 tools/audit_allocation.py --axis A,B,C --trials 12 --sample 25
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
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)

# 청산 사유 문자열은 버전에 따라 늘어난다('이평선 완전 이탈(60&120)' 등).
# 손실 여부는 사유 목록이 아니라 실현 손익 부호로 가른다.


def industry_map(codes):
    """종목 → 업종. 탐색 메뉴와 같은 소스(KRX-DESC)를 쓴다."""
    import FinanceDataReader as fdr
    desc = fdr.StockListing("KRX-DESC").set_index("Code")
    out = {}
    for c in codes:
        v = desc.loc[c, "Industry"] if c in desc.index else None
        if hasattr(v, "iloc"):
            v = v.iloc[0]
        out[c] = str(v or "미분류")
    return out


class SectorGate:
    """같은 업종 보유가 상한에 닿으면 그 업종 후보를 막는다."""

    def __init__(self, ind, cap):
        self.ind, self.cap = ind, cap

    def __call__(self, day, code, held):
        if not held:
            return False
        mine = self.ind.get(code, "미분류")
        n = sum(1 for h in held if self.ind.get(h, "미분류") == mine)
        return n >= self.cap


class CooldownGate:
    """청산일 다음 거래일부터 N거래일 동안 같은 종목을 다시 사지 않는다.

    [주의] '마지막 청산일 + N' 같은 단일 문턱으로 만들면 안 된다 — 그러면 그 날짜
     **이전 전체**가 막혀 유니버스가 통째로 사라진다(실측: 5,556칸 중 4,608칸 차단,
     거래 58 → 3건). 차단은 청산마다 열리는 **구간의 합집합**이다.

    run_portfolio 실행마다 새로 만들어야 한다(앞 실행의 청산 이력이 남으면 짝비교가 깨진다).
    """

    def __init__(self, dates, days, losses_only=False):
        self.idx = {d: i for i, d in enumerate(dates)}
        self.n = len(dates)
        self.days, self.losses_only = days, losses_only
        self.blocked = {}

    def note_exit(self, day, code, reason, profit):
        if self.losses_only and (profit or 0) > 0:
            return
        i = self.idx.get(day)
        if i is None:
            return
        self.blocked.setdefault(code, set()).update(
            range(i + 1, min(self.n, i + 1 + self.days)))

    def __call__(self, day, code, held):
        i = self.idx.get(day)
        return i is not None and i in self.blocked.get(code, ())


def run_with_cooldown(kwargs, dates, days, losses_only):
    """청산 이력을 게이트에 먹여야 하므로 probe 대신 2패스로 돌린다.

    1패스: 쿨다운 없이 돌려 청산 시점을 얻는다.
    2패스: 그 청산 이력으로 게이트를 만들어 다시 돌린다.
    [한계] 2패스의 청산 시점은 1패스와 달라질 수 있다(경로 의존). 완전한 재현은
     시뮬레이터 안에서 게이트가 실시간으로 갱신돼야 하지만, 그 정도의 차이는
     '쿨다운이 성과를 바꾸는가'라는 질문의 답을 뒤집지 못한다. 방향만 본다.
    """
    r1 = pb.run_portfolio(**kwargs)
    gate = CooldownGate(dates, days, losses_only)
    for t in r1["trades"]:
        if t["reason"] != "매수":
            gate.note_exit(t["date"], t["code"], t["reason"], t.get("profit", 0))
    return pb.run_portfolio(**dict(kwargs, entry_gate=gate))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="A,B,C")
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
    base_ratio = 1.0 / slots

    rows = {c: {str(r["date"]): r for r in df.to_dict("records")} for c, df in dfs.items()}

    def score_of(code, day):
        st = status.get(code, {}).get(day)
        return float(st[0]) if st else 0.0

    def atr_pct(code, day):
        r = rows.get(code, {}).get(day)
        if not r or not r.get("close"):
            return None
        a = float(r.get("ATR") or 0)
        c = float(r["close"])
        return (a / c * 100) if c > 0 and a > 0 else None

    def ratio_score(day, code):
        """점수 가중 — 7.0점 기준선 대비 초과분에 비례해 최대 1.5배까지."""
        s = score_of(code, day)
        return base_ratio * min(1.5, max(0.6, 0.6 + (s - 6.0) / 4.0))

    def ratio_vol(day, code):
        """변동성 역가중 — ATR% 4%를 기준으로 낮으면 키우고 높으면 줄인다(0.6~1.5배)."""
        a = atr_pct(code, day)
        if not a:
            return base_ratio
        return base_ratio * min(1.5, max(0.6, 4.0 / a))

    def ratio_both(day, code):
        s = ratio_score(day, code) / base_ratio
        v = ratio_vol(day, code) / base_ratio
        return base_ratio * min(1.5, max(0.6, (s * v) ** 0.5))

    ind = industry_map(list(dfs)) if "B" in args.axis else {}
    if ind:
        from collections import Counter
        top = Counter(ind.values()).most_common(4)
        print("[준비] 업종 분포 상위: " + " · ".join(f"{k}({v})" for k, v in top))

    AXES = {
        "A": ("자본 배분 방식", [
            ("균등 (현행)", {}), ("점수 가중", {"invest_ratio_fn": ratio_score}),
            ("변동성 역가중", {"invest_ratio_fn": ratio_vol}),
            ("점수×변동성", {"invest_ratio_fn": ratio_both}),
        ]),
        "B": ("업종 집중 제한", [
            ("무제한 (현행)", {}), ("같은 업종 최대 2", {"sector_cap": 2}),
            ("같은 업종 최대 1", {"sector_cap": 1}),
        ]),
        "C": ("청산 후 재진입 쿨다운", [
            ("없음 (현행)", {}), ("3일", {"cool": (3, False)}),
            ("5일", {"cool": (5, False)}), ("10일", {"cool": (10, False)}),
            ("5일 (손실 청산만)", {"cool": (5, True)}),
        ]),
    }

    codes = list(dfs)
    picks = {sd: [random.Random(sd * 31 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    k = max(1, args.subperiods)
    size = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
          for i in range(k)]

    for ax in args.axis.split(","):
        ax = ax.strip()
        if ax not in AXES:
            continue
        title, arms = AXES[ax]
        print(f"\n\n=========== 축 {ax} · {title} ===========")
        for wn, wd in W:
            print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
            print(f"{'팔':<20}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
            base_res = None
            for label, opt in arms:
                res = []
                for sd in seeds:
                    for pick in picks[sd]:
                        kwargs = dict(
                            dfs={c: dfs[c] for c in pick}, status={c: status[c] for c in pick},
                            dates=wd, initial_capital=INITIAL_CAPITAL, slots=slots,
                            market_filter_dates={c: mf.get(c, set()) for c in pick},
                            risk_scale_by_date=new_scale())
                        if "invest_ratio_fn" in opt:
                            kwargs["invest_ratio_fn"] = opt["invest_ratio_fn"]
                        if "sector_cap" in opt:
                            kwargs["entry_gate"] = SectorGate(ind, opt["sector_cap"])
                        if "cool" in opt:
                            d, lo = opt["cool"]
                            res.append(metrics(run_with_cooldown(kwargs, wd, d, lo)))
                            continue
                        res.append(metrics(pb.run_portfolio(**kwargs)))
                g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if base_res is None:
                    base_res = res
                    wl = "—"
                else:
                    win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base_res)
                              if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<20}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                      f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                      f"{wl:>10}", flush=True)

    print("\n[읽는 법] 세 축 모두 '기회를 줄이는' 방향이다. 수익이 함께 줄면 그냥 덜 사는 "
          "것이고, 수익이 유지되면서 MDD·꼬리가 좋아져야 값을 하는 것이다.")


if __name__ == "__main__":
    main()
