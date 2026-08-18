"""신호 원장으로 실매매 전용 게이트를 판정한다 — 로그 파싱 없이.

[왜 새 도구인가] 같은 질문을 `audit_pi_operation.py`가 **로그 문자열**로 답해 왔다. 두
 가지가 그 답을 막았다.
   ① 창이 짧다 — autotrade 로그가 30일에 삭제돼 감사 창이 영원히 18거래일이었다. 전방
      20일 수익을 볼 수가 없어 "게이트가 이익인지 손해인지"는 **판정 불가**로 남았다.
   ② 파싱이 위험하다 — `[매도비:3.92]`(정보 표기)와 `매도비:3.92<1.0`(차단)이 한 글자
      차이라 차단율을 1.3% → 75%로 뒤집어 읽은 적이 있다(2026-08-16).

 2026-08-19에 판정 지점이 결과를 **직접** DB(signal_ledger)에 남기도록 바꿨다. 이 도구는
 그 원장을 읽는다. 부등호를 세는 일이 없고, 원장은 3년 보존이라 창이 자란다.

[읽는 법 — 과대계상 주의] 하루에 주기가 수십 번 돈다. '한 번이라도 막혔으면 차단'으로
 세면 오후에 통과해 실제로 산 종목까지 차단으로 잡힌다. 여기서는 그날 **한 번도 통과하지
 못한 것**만 완전 차단으로 세고 부분 차단과 나눠 본다.

[표본이 없으면 없다고 말한다] 전방 수익 비교는 표본 수를 반드시 함께 찍는다. 한 자릿수
 표본으로 나온 차이는 결론이 아니다 — 그렇게 읽어서 한 번 틀린 적이 있다.

[실행]
  python3 tools/audit_signal_ledger.py                       # 실매매 DB
  python3 tools/audit_signal_ledger.py --db db/paper_trading.db --forward 20
"""
import argparse
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

BLOCK_COLS = {
    "blocked_vol": "체결강도",
    "blocked_abr": "매도잔량비",
    "blocked_hold": "체결강도 미확인(보류)",
    "blocked_corr": "상관관계",
    "blocked_rs": "상대강도",
    "blocked_tq": "추세품질 상한",
    "blocked_reentry": "당일 재진입 차단",
    "blocked_other": "기타",
}
GATE_COLS = ("blocked_vol", "blocked_abr", "blocked_hold")   # 실매매 전용 수급 게이트


def load(db_path, start, end):
    if db_path:
        config.DB_FILE_PATH = db_path
    from modules.db_manager import DBManager
    return DBManager().get_signal_ledger(start_date=start, end_date=end)


def forward_returns(rows, horizon):
    """(일자, 종목)마다 그날 종가 → horizon 거래일 뒤 종가 수익률.

    일봉은 KRX 확정 봉을 쓴다 — 판단에 쓰는 것과 같은 소스여야 비교가 성립한다.
    """
    from modules import krx_daily
    need = defaultdict(list)
    for r in rows:
        need[r["code"]].append(r["date"])
    out = {}
    for code, dates in need.items():
        try:
            df = krx_daily.get_daily(code, 400)
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        d = [str(x) for x in df["date"].tolist()]
        close = df["close"].tolist()
        idx = {day: i for i, day in enumerate(d)}
        for day in dates:
            i = idx.get(day)
            if i is None or i + horizon >= len(close) or close[i] <= 0:
                continue
            out[(day, code)] = (close[i + horizon] - close[i]) / close[i] * 100.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="읽을 DB (기본: config.DB_FILE_PATH)")
    ap.add_argument("--start", default=None, help="YYYYMMDD")
    ap.add_argument("--end", default=None, help="YYYYMMDD")
    ap.add_argument("--forward", type=int, default=20, help="전방 수익 지평(거래일)")
    ap.add_argument("--min-sample", type=int, default=15,
                    help="이 미만이면 전방 수익 비교를 '판정 불가'로 보고한다")
    args = ap.parse_args()

    rows = load(args.db, args.start, args.end)
    if not rows:
        print("[없음] 신호 원장이 비어 있다. 자동매매가 돌아야 쌓인다 "
              "(2026-08-19 이전 기간은 원장이 없다 — 그때는 로그를 봐야 한다).")
        return

    days = sorted({r["date"] for r in rows})
    print(f"[원장] {len(rows):,}행 · {len(days)}거래일 ({days[0]} ~ {days[-1]}) · "
          f"종목 {len({r['code'] for r in rows})}개")
    print(f"       평가 주기 합계 {sum(r['cycles'] for r in rows):,}회")

    # ── ① 무엇이 신호를 막았나 (주기 기준)
    print("\n[1] 사유별 차단 (주기 기준 — 하루에 여러 번 돈다)")
    total_cycles = sum(r["cycles"] for r in rows)
    passed_cycles = sum(r["passed"] for r in rows)
    print(f"   통과 {passed_cycles:,}회 ({passed_cycles / total_cycles * 100:.1f}%)")
    for col, label in BLOCK_COLS.items():
        n = sum(r[col] for r in rows)
        if n:
            print(f"   {label:<22} {n:>8,}회 ({n / total_cycles * 100:5.1f}%)")

    # ── ② 신호 기준 (일자·종목) — 완전 차단 / 부분 차단
    gate_full, gate_part, clean = [], [], []
    for r in rows:
        blocked = sum(r[c] for c in GATE_COLS)
        if blocked == 0:
            clean.append(r)
        elif r["passed"] == 0:
            gate_full.append(r)
        else:
            gate_part.append(r)
    tot = len(rows)
    print(f"\n[2] 실매매 전용 수급 게이트 — 신호 {tot}건 (일자×종목)")
    print(f"   완전 차단(그날 한 번도 통과 못함)  {len(gate_full):>4}건 "
          f"({len(gate_full) / tot * 100:.1f}%)")
    print(f"   부분 차단(통과 기회는 있었다)      {len(gate_part):>4}건 "
          f"({len(gate_part) / tot * 100:.1f}%)")
    print(f"   무차단                             {len(clean):>4}건 "
          f"({len(clean) / tot * 100:.1f}%)")
    print(f"   ※ '한 번이라도 막힘'으로 세면 {len(gate_full) + len(gate_part)}건이지만 "
          f"그중 {len(gate_part)}건은 통과 기회가 있었다 — 과대계상이다.")

    # ── ③ 게이트가 이익인가 손해인가 (오래 막혀 있던 질문)
    print(f"\n[3] 차단된 신호는 그 뒤 어떻게 됐나 (전방 {args.forward}거래일)")
    fr = forward_returns(rows, args.forward)
    # [대조군 정의] '수급 게이트에 안 걸림'이 아니라 **실제로 한 번은 통과한** 신호다.
    #  추세품질 상한처럼 다른 사유로 완전히 막힌 것을 통과군에 넣으면, 사지도 못한 신호가
    #  '게이트를 통과한 신호'인 척 대조군을 오염시킨다.
    blocked_r = [fr[(r["date"], r["code"])] for r in gate_full
                 if (r["date"], r["code"]) in fr]
    passed_r = [fr[(r["date"], r["code"])] for r in rows
                if r["passed"] > 0 and (r["date"], r["code"]) in fr]
    print(f"   완전 차단군 표본 {len(blocked_r)}건 · 무차단군 표본 {len(passed_r)}건")
    if len(blocked_r) < args.min_sample or len(passed_r) < args.min_sample:
        print(f"   → **판정 불가.** 한쪽이 {args.min_sample}건 미만이다. 표본이 없으면 "
              f"없다고 말한다 — 한 자릿수로 낸 차이를 결론으로 읽어 한 번 틀린 적이 있다.")
        print(f"   [언제 답이 나오나] 전방 {args.forward}일을 보려면 원장의 마지막 날이 "
              f"오늘보다 {args.forward}거래일 이상 앞서야 한다. 지금 원장은 "
              f"{len(days)}거래일치다.")
    else:
        bm, pm = float(np.mean(blocked_r)), float(np.mean(passed_r))
        print(f"   완전 차단군 평균 {bm:+.2f}% (중앙 {np.median(blocked_r):+.2f}%)")
        print(f"   무차단군   평균 {pm:+.2f}% (중앙 {np.median(passed_r):+.2f}%)")
        verdict = ("게이트가 나쁜 신호를 잘랐다" if bm < pm
                   else "게이트가 좋은 신호를 잘랐다 — 게이트를 다시 봐야 한다")
        print(f"   → {verdict} (차이 {bm - pm:+.2f}%p)")
        print("   [읽을 때] 이것은 '차단된 신호가 그 뒤 올랐나'이지 '샀으면 벌었나'가 "
              "아니다. 슬롯이 차 있었다면 어차피 못 샀다 — [4]와 함께 볼 것.")

    # ── ④ 게이트 말고 무엇이 막았나
    print("\n[4] 게이트 밖의 차단")
    for col in ("blocked_corr", "blocked_rs", "blocked_tq", "blocked_reentry"):
        sig_n = sum(1 for r in rows if r[col] > 0 and r["passed"] == 0)
        if sig_n:
            print(f"   {BLOCK_COLS[col]:<22} 완전 차단 신호 {sig_n}건")
    print("   [읽는 법] 슬롯 포화는 원장에 남지 않는다(후보까지는 됐으므로 '통과'다). "
          "진입이 없는데 통과가 많으면 원인은 게이트가 아니라 슬롯이다 — "
          "tools/audit_pi_operation.py 의 [3]을 함께 볼 것.")


if __name__ == "__main__":
    main()
