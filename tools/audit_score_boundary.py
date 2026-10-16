"""채점 경계 원장(score_boundary_ledger)을 읽는다 — 시너지 경계가 실제 진입을 갈랐는가.

[무엇을 재나 · 지시서 5-4] 시너지 두 항목(각 1점)은 'MACD 확산 유지'
 (macd_hist >= prev_macd_hist × 0.8) 조건을 공유해, 그 경계에서 점수가 2점씩 뛴다. 실매매
 엔진(engine.analyze_buy)이 경계 근처·확산 뒤집힘·가격 모멘텀 fail-open 이 있던 종목만
 (일자, 종목)당 1행으로 남긴다(db_manager.record_score_boundary). 이 도구는 그것을 모아
 다음을 보여 준다.
   · 경계 근처 주기와 확산 뒤집힘 횟수 — 신호가 경계에서 얼마나 흔들리는가
   · admitted / excluded — 확산 조건 하나가 BUY_SCORE 를 넘기게 했나 / 미달로 만들었나
   · cf_gained_buy / cf_lost_buy — 확산을 뒤집은 반사실 점수로 상태 분류를 다시 돌렸을 때
     매수 상태가 바뀌었나(경계가 후보에서 뺀 경우와 들인 경우를 모두 센다)
   · fail-open — 1·3개월 수익률을 모른 채 가격 모멘텀이 가점된 주기

[판정하지 않는다] 이것은 관측이다. 경계를 부드럽게 바꿀지(완충 구간·단계 가점)는 진입
 지연과 큰 수익 기회 누락을 포함한 포트폴리오 짝비교로만 정한다
 (tools/audit_synergy_boundary.py). 원장 행 수가 적으면 그렇다고 찍는다.

[실행]
  python3 tools/audit_score_boundary.py                       # 실매매 DB
  python3 tools/audit_score_boundary.py --db db/paper_trading.db --start 20261001
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

COUNT_COLS = ("cycles", "near_cycles", "flips", "admitted_cycles", "excluded_cycles",
              "cf_gained_buy", "cf_lost_buy", "fail_open_cycles")
MIN_ROWS_FOR_READING = 30   # 이보다 적으면 '표본 부족'을 함께 찍는다


def load(db_path, start, end):
    """원장을 읽는다. 조회 실패는 None(빈 목록 = '사건 없음'과 구분)."""
    if db_path:
        config.DB_FILE_PATH = db_path
    from modules.db_manager import DBManager
    return DBManager().get_score_boundary(start_date=start, end_date=end)


def summarize(rows):
    tot = {c: 0 for c in COUNT_COLS}
    per_code = defaultdict(lambda: {c: 0 for c in COUNT_COLS})
    names, days = {}, set()
    versions = defaultdict(int)
    for r in rows:
        days.add(r["date"])
        names[r["code"]] = r.get("name") or r["code"]
        versions[r.get("rules_version") or "?"] += 1
        for c in COUNT_COLS:
            v = int(r.get(c) or 0)
            tot[c] += v
            per_code[r["code"]][c] += v
    return tot, per_code, names, days, versions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="DB 파일 (기본: 실매매 DB)")
    ap.add_argument("--start", default=None, help="YYYYMMDD")
    ap.add_argument("--end", default=None, help="YYYYMMDD")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    rows = load(args.db, args.start, args.end)
    if rows is None:
        print("[실패] 채점 경계 원장을 읽지 못했습니다 — '사건 없음'이 아닙니다. 로그를 확인하세요.")
        return 1
    if not rows:
        print("채점 경계 원장이 비어 있습니다(해당 기간에 경계 사건이 기록되지 않음).")
        return 0

    tot, per_code, names, days, versions = summarize(rows)
    print(f"채점 경계 원장 — {len(rows)}행 · {len(days)}일 · {len(per_code)}종목")
    print("  규칙판: " + ", ".join(f"{k} {v}행" for k, v in sorted(versions.items())))
    if len(rows) < MIN_ROWS_FOR_READING:
        print(f"  [표본 부족] {len(rows)}행 < {MIN_ROWS_FOR_READING} — 아래 수치는 결론이 아니다.")
    print()
    print(f"  경계 근처 주기     {tot['near_cycles']:>8,}   (기록된 주기 {tot['cycles']:,})")
    print(f"  확산 뒤집힘        {tot['flips']:>8,}")
    print(f"  admitted(경계 근처) {tot['admitted_cycles']:>7,}   → 반사실에서 매수 상태를 잃음 {tot['cf_lost_buy']:,}")
    print(f"  excluded(경계 근처) {tot['excluded_cycles']:>7,}   → 반사실에서 매수 상태가 됨 {tot['cf_gained_buy']:,}")
    print(f"  가격 모멘텀 fail-open {tot['fail_open_cycles']:>6,}")
    print("\n  [읽는 법] cf_gained_buy 는 '경계가 후보에서 뺀' 주기, cf_lost_buy 는 '경계가 들인' 주기다.")
    print("            둘 다 주기 단위라 같은 종목·같은 날이 여러 번 셀 수 있다. 채택 판정은 짝비교로.")

    print(f"\n  확산 뒤집힘 상위 {args.top}종목")
    print(f"  {'종목':<14}{'뒤집힘':>8}{'근처':>8}{'뺌':>6}{'들임':>6}")
    for code, c in sorted(per_code.items(), key=lambda kv: -kv[1]["flips"])[:args.top]:
        print(f"  {names[code][:12]:<14}{c['flips']:>8}{c['near_cycles']:>8}"
              f"{c['cf_gained_buy']:>6}{c['cf_lost_buy']:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
