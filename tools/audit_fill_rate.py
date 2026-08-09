"""주문 체결률 — 백테스트와 실거래의 마지막 미지수.

[왜 이것이 남았나] 계산·데이터·사이징은 전부 검증했다(지표 12종 참조구현 일치, 일봉
KIS↔pykrx 100% 일치, 청산 패리티 0건, 배분액 0건). 그러나 백테스트는 **모든 신호가
포지션이 된다**고 가정한다. 실거래는 다르다.

  · 지정가 `현재가 × (1 ± SLIPPAGE_RATE)` 로 접수
  · UNFILLED_ORDER_CANCEL_SECONDS(120초) 내 미체결이면 자동 취소
  · 부분체결이면 그 상태로 남는다(운영 정책: 운영자가 취소하기 전까지 대기)

미체결로 날아간 신호는 백테스트에는 없는 손실이다. 반대로 미체결률이 0에 가까우면
백테스트의 진입 가정이 그대로 성립한다. **추정하지 말고 재야 한다.**

[무엇을 재는가] DB(trades)에 이미 다 남는다. 별도 계측 코드가 필요 없다.
  · 시스템 주문(AUTO) 건수 · 자동취소 건수 → 미체결률
  · 접수→체결 소요 시간 분포
  · 부분체결 비율(주문 수량 vs 체결 수량)
  · 종목별 미체결률 (유동성이 낮은 종목이 있는가)

[실행] python3 tools/audit_fill_rate.py [--db db/trade_history.db] [--days 30]
       실거래를 며칠 돌린 뒤에 의미가 생긴다.
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 시스템이 낸 주문만 본다. 수동·외부·예약은 체결률 판단 대상이 아니다.
SYSTEM_MARK = "(AUTO)"
CANCEL_MARKS = ("미체결 시간 초과",)


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def load(db_path, days):
    if not os.path.exists(db_path):
        print(f"DB 없음: {db_path}")
        return []
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM trades WHERE time >= ? ORDER BY time", (since,))
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="db/trade_history.db")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    rows = load(args.db, args.days)
    if not rows:
        print(f"[체결률] 최근 {args.days}일 거래 기록 없음.")
        return

    # 주문번호(odno) 단위로 접수 → 결말을 잇는다.
    placed, outcome = {}, {}
    for r in rows:
        t = str(r.get("type") or "")
        odno = str(r.get("odno") or "")
        org = str(r.get("org_odno") or "")
        status = str(r.get("order_status") or "")
        reason = str(r.get("reason") or "")

        if SYSTEM_MARK in t and status not in ("취소", "체결", "취소(추정)"):
            placed[odno] = r                                  # 접수
        elif SYSTEM_MARK in t and status == "체결":
            outcome.setdefault(odno, ("체결", r))
        if org and status in ("취소", "취소(추정)"):
            kind = "미체결취소" if any(m in reason for m in CANCEL_MARKS) else "기타취소"
            outcome.setdefault(org, (kind, r))
        elif status == "체결" and odno in placed:
            outcome.setdefault(odno, ("체결", r))

    if not placed:
        print(f"[체결률] 최근 {args.days}일 시스템 주문(AUTO) 없음 — "
              f"실거래를 돌린 뒤 다시 실행하세요.")
        print(f"         (전체 기록 {len(rows)}건은 수동·외부 주문입니다)")
        return

    n = len(placed)
    cnt = defaultdict(int)
    fill_secs, part = [], 0
    by_code = defaultdict(lambda: [0, 0])
    for odno, p in placed.items():
        kind, o = outcome.get(odno, ("미결(진행중/미확인)", None))
        cnt[kind] += 1
        code = p.get("code")
        by_code[code][0] += 1
        if kind == "미체결취소":
            by_code[code][1] += 1
        if kind == "체결" and o is not None:
            try:
                t0 = datetime.strptime(p["time"][:19], "%Y-%m-%d %H:%M:%S")
                t1 = datetime.strptime(o["time"][:19], "%Y-%m-%d %H:%M:%S")
                fill_secs.append((t1 - t0).total_seconds())
            except Exception:
                pass
            if _num(o.get("qty")) < _num(p.get("qty")):
                part += 1

    print(f"[체결률] {args.db} · 최근 {args.days}일 · 시스템 주문 {n}건\n")
    for k in ("체결", "미체결취소", "기타취소", "미결(진행중/미확인)"):
        if cnt[k]:
            print(f"  {k:<22}{cnt[k]:>5}건  {cnt[k]/n*100:>5.1f}%")
    print()
    filled = cnt["체결"]
    if filled:
        print(f"  부분체결 {part}건 ({part/filled*100:.1f}% of 체결)")
    if fill_secs:
        s = sorted(fill_secs)
        print(f"  접수→체결 소요  중앙 {s[len(s)//2]:.0f}초  "
              f"p90 {s[int(len(s)*0.9)]:.0f}초  최대 {s[-1]:.0f}초")
    cancel_sec = 120
    try:
        import config
        cancel_sec = getattr(config, "UNFILLED_ORDER_CANCEL_SECONDS", 120)
    except Exception:
        pass
    print(f"  (자동취소 기준 {cancel_sec}초)")

    bad = sorted(((c, a, b) for c, (a, b) in by_code.items() if b), key=lambda x: -x[2] / x[1])
    if bad:
        print("\n  종목별 미체결률 (높은 순)")
        for code, tot, miss in bad[:10]:
            nm = next((p.get("name") for p in placed.values() if p.get("code") == code), code)
            print(f"    {nm}({code}) {miss}/{tot}  {miss/tot*100:.0f}%")

    print("\n" + "-" * 66)
    rate = cnt["미체결취소"] / n * 100
    if rate < 2:
        print("  → 미체결률이 낮다. 백테스트의 '모든 신호가 포지션이 된다' 가정이 성립한다.")
    elif rate < 10:
        print("  → 미체결이 일부 있다. 백테스트가 그만큼 낙관적이다. 슬리피지 상향을 검토할 것.")
    else:
        print("  → 미체결이 많다. 지정가 폭(SLIPPAGE_RATE)이나 취소 대기(120초)를 재검토할 것.")
        print("     유동성이 낮은 종목이 원인이면 관심목록에서 빼는 편이 낫다.")


if __name__ == "__main__":
    main()
