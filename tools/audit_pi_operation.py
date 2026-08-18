"""라즈베리파이 운영 로그로 백테스트가 못 재는 것을 잰다 — 실기(實機) 감사.

[왜 로그인가] 두 축은 원리상 백테스트로 답할 수 없다.
  ① **감시 주기 부하** — 관심종목 상한을 정하는 것은 수익률이 아니라 파이3의 처리 속도다
     (`trader._record_cycle_duration`: 실제 청산 감시 간격 = 주기 소요 + 대기 간격).
     기계마다 다르므로 그 기계의 기록이 있어야 한다.
  ② **실매매 전용 게이트** — 체결강도·매도잔량비는 실시간 호가 데이터라 일봉 백테스트에
     아예 없다. 지금까지의 모든 다이얼은 이 게이트가 없는 세계에서 정해졌다.

[이 도구가 하지 않는 것] 차단된 신호의 사후 수익 판정. 로그 창이 18거래일이라
 전방 20일을 볼 수 없고, 짧은 지평으로 줄여도 표본이 2~21건이라 판정이 불가능하다.
 **표본이 없으면 없다고 말하는 것**이 이 도구의 규약이다(tools/audit_live_gate_impact.py
 가 --forward 로 시도하되 표본 수를 반드시 함께 볼 것).

[차단 판정] 부등호가 있어야 차단이다. `[체결:78.0%<100.0%]`·`[매도비:0.51<1.0]` 은 차단,
 `[매도비:2.55]` 는 정보 표기다. 2026-08-16에 이걸 혼동해 차단율을 1.3% → 75%로 잘못
 보고한 적이 있다.

[과대계상 주의] 하루에 주기가 수십 번 돈다. '한 번이라도 막혔으면 차단'으로 세면
 오후에 통과해 실제로 산 종목까지 차단으로 잡힌다. 여기서는 **그날 전 주기에서 한 번도
 통과하지 못한 것**만 완전 차단으로 세고, 부분 차단과 나눠 보고한다.

[2026-08-19 이후] 게이트 축은 로그가 아니라 **DB 원장**으로 세는 것이 정확하다 —
 판정 지점이 결과를 직접 남기므로 부등호를 세는 일이 없고, 원장은 3년 보존이라 창이 자란다.
 `tools/audit_signal_ledger.py` 를 쓸 것. 이 도구는 (a) 원장 이전 기간과 (b) 주기 부하
 축(로그에만 있다)에 남는다.

[실행] python3 tools/audit_pi_operation.py --logs 'logs/autotrade_*.log'
"""
import argparse
import glob
import io
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

TS = r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\.(\d{3})\]"
RE_CYCLE = re.compile(TS + r" 모니터링 완료 \(소요 ([0-9.]+)초")
RE_START = re.compile(TS + r" 모니터링 주기 시작")
RE_ANALYZE = re.compile(TS + r" \[분석\] ([^(]+)\(([A-Z0-9]{6})\).*?점수=([0-9.]+)")
RE_HOLD = re.compile(TS + r" \[보유분석\]")
RE_BUY = re.compile(TS + r".*\[주문 실행\] BUY")
RE_BUY_TGT = re.compile(r"대상: ([^(]+)\(([A-Z0-9]{6})\)")
RE_SLOTFULL = re.compile(r"보유 슬롯 가득 참")
REJ_VOL = re.compile(r"체결:([0-9.]+)%<([0-9.]+)%")
REJ_AB = re.compile(r"매도비:([0-9.]+)<([0-9.]+)")
REJ_HOLDCHK = re.compile(r"체결강도 미확인")


def secs(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", nargs="*", default=["logs/autotrade_*.log"])
    ap.add_argument("--buy-score", type=float, default=None)
    args = ap.parse_args()
    buy_score = args.buy_score or config.ANALYSIS_THRESHOLDS["BUY_SCORE"]

    files = sorted(f for pat in args.logs for f in glob.glob(pat))
    cycles = []                                   # (날짜, 시각초, 소요초, 분석종목수)
    sig = defaultdict(lambda: {"n": 0, "blocked": 0, "hold": 0, "score": 0.0, "name": ""})
    buys = set()
    slotfull_cycles = 0
    per_stock_gaps = []                           # 연속 [분석] 줄 간 간격 = 종목당 비용
    days = set()

    parts = []                                    # (고정비, 종목당, n) — 분해 측정
    for path in files:
        prev_an = None                            # 직전 [분석] 줄 시각
        cur_n = 0
        t_start = t_first = t_last = None
        for line in io.open(path, encoding="utf-8", errors="ignore"):
            m = RE_START.search(line)
            if m:
                prev_an, cur_n = None, 0
                t_start = secs(*m.group(2).split(":"), m.group(3))
                t_first = t_last = None
                continue
            m = RE_ANALYZE.search(line)
            if m:
                d, hh, mm, ss = m.group(1).replace("-", ""), *m.group(2).split(":"),
                t = secs(hh, mm, ss, m.group(3))
                days.add(d)
                cur_n += 1
                if prev_an is not None and 0 < t - prev_an < 5:   # 5초 넘는 간격은 다른 작업
                    per_stock_gaps.append(t - prev_an)
                prev_an = t
                if t_first is None:
                    t_first = t
                t_last = t
                score = float(m.group(6))
                if score >= buy_score:
                    k = (d, m.group(5))
                    e = sig[k]
                    e["n"] += 1
                    e["name"] = m.group(4).strip()
                    e["score"] = max(e["score"], score)
                    if REJ_VOL.search(line) or REJ_AB.search(line):
                        e["blocked"] += 1
                    elif REJ_HOLDCHK.search(line):
                        e["hold"] += 1
                continue
            m = RE_CYCLE.search(line)
            if m:
                d = m.group(1).replace("-", "")
                t_end = secs(*m.group(2).split(":"), m.group(3))
                cycles.append((d, t_end, float(m.group(4)), cur_n))
                # [분해] 주기 = 고정비(분석 전·후 작업) + 분석 구간. 종목 수가 거의 안 변해
                #  회귀로는 못 가르므로(느린 주기가 오히려 종목을 적게 분석해 기울기가
                #  음수로 나온다) 시각 자체로 나눈다.
                if t_start is not None and t_first is not None and cur_n >= 10 \
                        and t_end >= t_last >= t_first >= t_start:
                    span = t_last - t_first
                    parts.append(((t_first - t_start) + (t_end - t_last),
                                  span / max(1, cur_n - 1), cur_n))
                cur_n = 0
                t_start = t_first = t_last = None
                continue
            if RE_SLOTFULL.search(line):
                slotfull_cycles += 1
            m = RE_BUY_TGT.search(line)
            if m:
                buys.add(m.group(2))

    dl = sorted(days)
    print(f"[로그] 파일 {len(files)}개 · 거래일 {len(dl)}일 ({dl[0]}~{dl[-1]}) · "
          f"주기 {len(cycles):,}회 · 매수 문턱 {buy_score}점", flush=True)

    # ── ① 감시 주기 부하
    dur = np.array([c[2] for c in cycles])
    n_an = np.array([c[3] for c in cycles])
    live = dur[n_an > 0]                      # 종목을 실제로 분석한 주기만
    interval = int(getattr(config, "SYSTEM_TRADING_INTERVAL", 60))
    print(f"\n[1] 감시 주기 부하 (라즈베리파이3 실측)")
    print(f"   전체 주기 {len(dur):,}회 · 그중 종목 분석이 있었던 주기 {len(live):,}회")
    print(f"   소요(초): p50 {np.median(live):.1f} · p90 {np.percentile(live, 90):.1f} · "
          f"p99 {np.percentile(live, 99):.1f} · 최대 {live.max():.1f}")
    print(f"   실제 청산 감시 간격 = 소요 + {interval}초 → "
          f"p50 {np.median(live) + interval:.0f}초 · p90 {np.percentile(live, 90) + interval:.0f}초 "
          f"· 최대 {live.max() + interval:.0f}초")
    if len(per_stock_gaps) > 100:
        g = np.array(per_stock_gaps)
        print(f"   종목당 분석 간격: 표본 {len(g):,} · p50 {np.median(g) * 1000:.0f}ms · "
              f"p90 {np.percentile(g, 90) * 1000:.0f}ms · 평균 {g.mean() * 1000:.0f}ms")
    # 고정비 vs 종목당 비용 — 시각 분해(회귀는 교란된다, 위 주석 참조)
    if len(parts) > 100:
        fx = np.array([p[0] for p in parts])
        ps = np.array([p[1] for p in parts])
        nn = np.array([p[2] for p in parts])
        print(f"\n   [분해] 분석 10종목 이상인 주기 {len(parts):,}회 · 분석 종목수 "
              f"p50 {np.median(nn):.0f}")
        print(f"     고정비(분석 전·후 작업): p50 {np.median(fx):.1f}초 · "
              f"p90 {np.percentile(fx, 90):.1f}초")
        print(f"     종목당 비용:            p50 {np.median(ps) * 1000:.0f}ms · "
              f"p90 {np.percentile(ps, 90) * 1000:.0f}ms")
        for q, tag in ((50, "평시(p50)"), (90, "혼잡(p90)")):
            f0, p0 = np.percentile(fx, q), np.percentile(ps, q)
            print(f"     {tag} 기준 추정 — " + " · ".join(
                f"{n}종목 {f0 + p0 * n:.0f}초" for n in (44, 60, 80, 100, 120)))
        f0, p0 = np.median(fx), np.median(ps)
        cap = int((interval - f0) / p0) if p0 > 0 else 0
        print(f"     → 평시 기준 '주기 ≤ 대기간격({interval}초)'을 지키는 상한: "
              f"약 {cap}종목 (그 이상이면 감시 간격이 2배를 넘기 시작한다)")
        print("     ※ 이 추정은 종목 수에 선형이라고 본 것이다. 실제로는 API TPS 대기가"
              " 겹쳐 더 나빠질 수 있다 — 상한의 낙관적 경계로 읽을 것.")

    # ── ② 실매매 전용 게이트
    tot = len(sig)
    full = sum(1 for v in sig.values() if v["blocked"] == v["n"])
    part = sum(1 for v in sig.values() if 0 < v["blocked"] < v["n"])
    none = tot - full - part
    print(f"\n[2] 실매매 전용 게이트 (체결강도·매도잔량비) — {buy_score}점 이상 신호 {tot}건")
    print(f"   완전 차단(그날 전 주기에서 한 번도 통과 못함) {full}건 ({full / tot * 100:.1f}%)")
    print(f"   부분 차단(일부 주기만 막힘 — 통과 기회는 있었다) {part}건 ({part / tot * 100:.1f}%)")
    print(f"   무차단 {none}건 ({none / tot * 100:.1f}%)")
    print(f"   ※ '한 번이라도 막힘'으로 세면 {full + part}건({(full + part) / tot * 100:.1f}%)이지만 "
          f"그중 {part}건은 통과 기회가 있었다 — 과대계상이다.")
    codes_full = {c for (d, c), v in sig.items() if v["blocked"] == v["n"]}
    print(f"   완전 차단 종목 중 그 기간에 결국 매수된 적이 있는 종목: "
          f"{len(codes_full & buys)}/{len(codes_full)}")

    # ── ③ 무엇이 진짜 진입을 막았나
    print(f"\n[3] 진입을 막은 것의 순위 (같은 기간)")
    print(f"   '보유 슬롯 가득 참'으로 주문 미전송된 주기: {slotfull_cycles:,}회 "
          f"({slotfull_cycles / len(cycles) * 100:.1f}% of 전체 주기)")
    print(f"   실제 매수 실행: {len(buys)}종목")
    print("   [읽는 법] 슬롯 포화가 압도적이면 게이트 차단율은 실질 손해로 이어지지 않는다 "
          "— 어차피 살 자리가 없었다.")


if __name__ == "__main__":
    main()
