"""매매 '기계'가 온전히 도는가 — DB 기록만으로 사후 점검한다.

[왜 이 도구인가] 2026-08-10 현재 시장필터가 지수 SMA80 이탈로 신규 매수를 막고 있어
  (해제까지 지수 +16~19% 필요) 실매매가 발생하지 않는다. 전략은 6.7년 백테스트로 이미
  검증했지만, **주문 → 체결 → 기록 → 청산**이 실제로 도는지는 매매가 있어야 알 수 있다.
  그래서 가상계좌에서 시장필터를 끄고 매매를 강제 발생시켜 기계를 시험한다.

  [중요] 그 운용의 **손익은 아무 의미가 없다**. 하락장에 필터를 끄고 사는 것은 백테스트가
  명확히 기각한 설정이다. 이 도구가 보는 것은 성과가 아니라 '경로가 온전한가' 뿐이다.
  검증이 끝나면 시장필터를 반드시 다시 켤 것.

[읽기 전용] DB를 수정하지 않는다. 아무 때나 돌려도 운용에 영향이 없다.

실행:
  python3 tools/audit_trade_mechanics.py                       # 관찰모드 DB
  python3 tools/audit_trade_mechanics.py --db db/trade_history.db --days 30
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import trading_cost  # noqa: E402

# INFO 는 판정이 아니라 맥락(표본 규모 등)이다. SKIP(판정 보류)과 같은 기호를 쓰되
#  집계에서는 갈라야 한다 — 안 그러면 안내문 한 줄이 "검증 보류 1건"으로 둔갑한다.
OK, WARN, BAD, SKIP, INFO = "✅", "⚠️ ", "❌", "· ", "·  "


class Report:
    def __init__(self):
        self.rows = []

    def add(self, mark, title, detail=""):
        self.rows.append((mark, title, detail))

    def print(self):
        bad = sum(1 for m, _, _ in self.rows if m == BAD)
        warn = sum(1 for m, _, _ in self.rows if m == WARN)
        ok = sum(1 for m, _, _ in self.rows if m == OK)
        skip = sum(1 for m, _, _ in self.rows if m == SKIP)
        # 무엇이 아직 판정되지 않았는지 이름으로 남긴다 — 개수만으로는 다음에 무엇을
        #  기다려야 하는지 알 수 없다(대개 '매도 체결'이다).
        pending = [t.split(" — ")[0] for m, t, _ in self.rows if m == SKIP]
        print("\n" + "=" * 96)
        for mark, title, detail in self.rows:
            print(f"{mark} {title}")
            for line in (detail or "").splitlines():
                if line.strip():
                    print(f"      {line}")
        print("=" * 96)
        if bad:
            print(f"실패 {bad}건 · 주의 {warn}건 — 위 항목을 확인하기 전에는 실계좌로 넘어가지 말 것.")
        elif warn:
            print(f"실패 0건 · 주의 {warn}건 — 대부분 '표본 부족'이다. 더 돌린 뒤 다시 볼 것.")
        elif skip:
            # [중요] 표본이 없는 항목을 '통과'로 묶어 버리면, 아무것도 검증하지 못한 운용이
            #  검증된 운용처럼 읽힌다 — 이 도구가 가장 경계해야 할 실패다. 보류는 보류로 센다.
            print(f"판정 {ok}건 통과 · {skip}건 보류(표본 없음) — 아직 검증이 끝나지 않았다.")
            print("  보류 항목: " + " / ".join(pending))
        else:
            print(f"판정 {ok}건 모두 통과.")
        return bad


def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params)]


def _is_system(t):
    """시스템 트레이딩이 낸 주문인가. 수동·외부 주문은 판정 대상이 아니다."""
    return "(AUTO)" in (t.get("type") or "")


# [주의] 매매 구분은 기록 주체마다 표기가 다르다. 시스템은 영문("buy(AUTO)"/"sell(AUTO)"),
#  수동·외부 감지는 한글("매수(수동)"·"현금매수(외부)")로 남긴다. 한쪽만 보면 정작
#  판정 대상인 시스템 체결이 통째로 빠져 도구가 '표본 없음'으로 조용히 통과한다.
def _is_buy(t):
    ty = (t.get("type") or "").lower()
    return "buy" in ty or "매수" in (t.get("type") or "")


def _is_sell(t):
    ty = (t.get("type") or "").lower()
    return "sell" in ty or "매도" in (t.get("type") or "")


# ─────────────────────────────────────────────
# 0. 빌드 확인 — 무엇을 검증하고 있는지부터 확정한다
# ─────────────────────────────────────────────

def check_build(conn, rep):
    """[가장 먼저] 기록을 만든 코드가 현재 코드인가.

    구버전으로 돌린 운용을 아무리 오래 검증해도 지금 코드에 대한 증거가 되지 않는다.
    trades.buy_price 는 2026-08-10 에 추가된 컬럼이라 빌드 표지로 쓸 수 있다.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
    if "buy_price" in cols:
        rep.add(OK, "빌드 확인 — trades.buy_price 존재 (2026-08-10 이후 코드)")
        return True
    rep.add(BAD, "빌드가 오래됐다 — trades.buy_price 컬럼이 없다",
            "이 DB 는 2026-08-10 이전 코드로 만들어졌다. 그 이후의 변경(실현손익 재계산·\n"
            "거래비용 요율 확정·주문 무응답 대사·텔레그램 fail-closed)이 하나도 반영되지\n"
            "않은 운용이므로, 여기서 무엇을 확인해도 현재 코드의 증거가 되지 않는다.\n"
            "→ 최신 코드로 받아 재기동한 뒤 처음부터 다시 돌릴 것.")
    return False


# ─────────────────────────────────────────────
# 1. 매수 기록에 손절 근거가 남는가
# ─────────────────────────────────────────────

def check_stop_recorded(buys, rep):
    """손절률은 **매수 시점 ATR**로 굳어 기록돼야 한다.

    기록이 없으면 매도 판정이 전역 고정 손절률로 떨어져, 변동성이 큰 종목이 좁은
    고정폭에서 잘려 나간다. 추세추종에서 가장 비싼 실패다.
    """
    if not buys:
        rep.add(SKIP, "매수 손절률 기록 — 표본 없음")
        return
    missing = [b for b in buys if not (b.get("stop_loss_rate") or 0)]
    pos = [b for b in buys if (b.get("stop_loss_rate") or 0) > 0]
    if missing:
        rep.add(BAD, f"매수 {len(missing)}/{len(buys)}건에 손절률이 없다",
                "예: " + ", ".join(f"{b['name']}({b['code']})" for b in missing[:5]))
    elif pos:
        rep.add(BAD, f"손절률이 양수인 기록 {len(pos)}건 — 부호가 뒤집혔다",
                "손절률은 음수여야 한다. 양수면 판정이 즉시 발동하거나 아예 안 걸린다.")
    else:
        rates = sorted(abs(b["stop_loss_rate"]) for b in buys)
        rep.add(OK, f"매수 {len(buys)}건 모두 손절률 기록됨",
                f"손절폭 중앙 {rates[len(rates)//2]:.2f}% · 최소 {rates[0]:.2f}% · 최대 {rates[-1]:.2f}%")


# ─────────────────────────────────────────────
# 2. 손절이 기록된 폭에서 실제로 발동하는가
# ─────────────────────────────────────────────

def check_stop_fires_at_recorded_rate(sells, buy_by_code, rep, tol=3.0):
    """'손절' 사유 매도의 실제 손실률이 매수 때 기록한 손절률 부근인가.

    크게 어긋나면 기록과 판정이 다른 값을 보고 있다는 뜻이다. 슬리피지·갭하락으로
    더 나쁘게 체결되는 것은 정상이므로, **기록보다 얕은 손실에서 잘린 경우**만 문제 삼는다.
    """
    stops = [s for s in sells if "손절" in (s.get("reason") or "")]
    if not stops:
        rep.add(SKIP, "손절 발동 검증 — 손절 청산 표본 없음")
        return
    early = []
    for s in stops:
        rate = s.get("profit_rate")
        planned = (buy_by_code.get(s["code"]) or {}).get("stop_loss_rate")
        if rate is None or not planned:
            continue
        # planned 는 음수. 실제 손실이 계획보다 '얕은데' 잘렸다면 조기 발동이다.
        if rate > planned + tol:
            early.append((s["name"], rate, planned))
    if early:
        rep.add(BAD, f"기록된 손절폭보다 얕은 손실에서 잘린 건 {len(early)}건",
                "\n".join(f"{n}: 실제 {r:+.2f}% (계획 {p:+.2f}%)" for n, r, p in early[:5]))
    else:
        rep.add(OK, f"손절 {len(stops)}건 — 모두 기록된 손절폭 이내에서 발동")


# ─────────────────────────────────────────────
# 3. 실현손익이 왕복 비용을 뺀 값인가
# ─────────────────────────────────────────────

def check_realized_is_net(sells, rep):
    """[2026-08-10 변경분] 매도 실현손익은 매수·매도 양쪽 비용을 뺀 값이어야 한다.

    총이익이 왕복 비용(약 0.235%)보다 작은 거래는 실제로 손실인데, 비용을 빼지 않으면
    '승'으로 집계된다. 승률·손익비가 파라미터 판단의 근거이므로 그 왜곡이 그대로
    설정 결정으로 넘어간다.
    """
    usable = [s for s in sells
              if (s.get("buy_price") or 0) > 0 and (s.get("qty") or 0) > 0
              and s.get("profit_amt") is not None]
    if not usable:
        rep.add(SKIP, "실현손익 검증 — buy_price 가 기록된 매도 표본 없음",
                "매도 체결이 쌓이면 다시 볼 것.")
        return
    off = []
    for s in usable:
        try:
            price = float(str(s["price"]).replace(",", ""))
        except (TypeError, ValueError):
            continue
        want, _ = trading_cost.net_realized_profit(s["buy_price"], price, int(s["qty"]))
        got = float(s["profit_amt"])
        basis = float(s["buy_price"]) * int(s["qty"])
        if basis > 0 and abs(got - want) / basis > 0.001:      # 기준가 0.1% 초과 오차
            off.append((s["name"], got, want))
    if off:
        rep.add(BAD, f"실현손익이 비용 반영값과 다른 건 {len(off)}건",
                "\n".join(f"{n}: 기록 {g:+,.0f}원 / 비용반영 {w:+,.0f}원" for n, g, w in off[:5]))
    else:
        gross_win = sum(1 for s in usable if s["profit_amt"] > 0)
        rep.add(OK, f"매도 {len(usable)}건 실현손익이 왕복 비용 반영값과 일치",
                f"승 {gross_win} / 패 {len(usable) - gross_win}")


# ─────────────────────────────────────────────
# 4. 트레일링 최고가가 갱신되는가
# ─────────────────────────────────────────────

def check_trailing(conn, sells, rep):
    ts = _rows(conn, "SELECT code, highest_price, update_time FROM trailing_stops")
    trail_sells = [s for s in sells if "트레일링" in (s.get("reason") or "")]
    if not ts and not trail_sells:
        rep.add(SKIP, "트레일링 검증 — 추적 기록·트레일링 청산 모두 없음")
        return
    zero = [t for t in ts if not (t.get("highest_price") or 0)]
    if zero:
        rep.add(WARN, f"최고가가 0인 추적 기록 {len(zero)}건",
                "감시 시작가가 설정되지 않았다면 트레일링이 발동하지 않는다: "
                + ", ".join(t["code"] for t in zero[:8]))
    else:
        rep.add(OK, f"트레일링 추적 {len(ts)}종목 모두 최고가 보유"
                    + (f" · 트레일링 청산 {len(trail_sells)}건" if trail_sells else ""))


# ─────────────────────────────────────────────
# 5. 슬롯 상한을 넘지 않는가
# ─────────────────────────────────────────────

def check_slot_cap(buys, sells, rep):
    """보유 종목 수가 SYSTEM_MAX_HOLDINGS 를 넘은 시점이 있는가(체결 기록 재생)."""
    max_slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4) or 4
    events = [(t["time"], t["code"], +1) for t in buys] + \
             [(t["time"], t["code"], -1) for t in sells]
    if not events:
        rep.add(SKIP, "슬롯 상한 검증 — 체결 표본 없음")
        return
    events.sort(key=lambda e: e[0])
    held, peak, peak_at = set(), 0, None
    for when, code, delta in events:
        if delta > 0:
            held.add(code)
        else:
            held.discard(code)
        if len(held) > peak:
            peak, peak_at = len(held), when
    if peak > max_slots:
        rep.add(BAD, f"동시 보유가 슬롯 상한({max_slots})을 넘었다 — 최대 {peak}종목",
                f"발생 시각: {peak_at}\n분할 매도로 기록이 어긋났을 수도 있으니 잔고와 대조할 것.")
    else:
        rep.add(OK, f"동시 보유 최대 {peak}종목 (상한 {max_slots})")


# ─────────────────────────────────────────────
# 6. 손절가보다 비싸게 되사지 않는가
# ─────────────────────────────────────────────

def check_no_expensive_reentry(buys, sells, rep):
    """당일 손절로 잘린 종목을 그 손절가 **이상**에서 되사면 안 된다.

    체결강도 허들만으로는 못 막는다 — 재진입할 때마다 값이 갱신되어 스스로 세운 허들을
    스스로 넘는다(2026-08-05 실측: 손절 → 10초 뒤 더 비싸게 재매수를 반복, 왕복
    스프레드만큼 실현 손실만 쌓였다).
    """
    def _px(t):
        try:
            return float(str(t["price"]).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    stops = defaultdict(list)
    for s in sells:
        if "손절" in (s.get("reason") or ""):
            stops[s["code"]].append((s["time"][:10], s["time"], _px(s)))
    if not stops:
        rep.add(SKIP, "재진입 가격 검증 — 손절 표본 없음")
        return
    bad = []
    for b in buys:
        for day, when, px in stops.get(b["code"], []):
            if b["time"][:10] == day and b["time"] > when and px > 0 and _px(b) >= px:
                bad.append((b["name"], _px(b), px, b["time"]))
    if bad:
        rep.add(BAD, f"손절가 이상에서 되산 건 {len(bad)}건",
                "\n".join(f"{n}: 재매수 {p:,.0f} >= 손절 {sp:,.0f} ({w})" for n, p, sp, w in bad[:5]))
    else:
        rep.add(OK, f"당일 손절 {sum(len(v) for v in stops.values())}건 — 더 비싼 재진입 없음")


# ─────────────────────────────────────────────
# 7. 요약
# ─────────────────────────────────────────────

def summarize(buys, sells, rep):
    if not buys and not sells:
        rep.add(WARN, "시스템 체결이 한 건도 없다",
                "시장필터가 켜져 있으면 하락장에서는 매수가 나가지 않는다.\n"
                "기계를 시험하려면 **가상계좌에서만** 시장필터를 끄고 돌릴 것\n"
                "(그 운용의 손익은 의미가 없고, 끝나면 반드시 다시 켤 것).")
        return
    codes = {t["code"] for t in buys} | {t["code"] for t in sells}
    reasons = defaultdict(int)
    for s in sells:
        r = (s.get("reason") or "기타").split("(")[0].strip()
        reasons[r] += 1
    rep.add(INFO, f"표본: 매수 {len(buys)}건 · 매도 {len(sells)}건 · {len(codes)}종목",
            "청산 사유: " + (", ".join(f"{k} {v}" for k, v in
                                    sorted(reasons.items(), key=lambda x: -x[1])) or "-"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="검사할 DB 파일 (기본: 관찰모드 DB)")
    ap.add_argument("--days", type=int, default=30, help="최근 N일만 본다")
    args = ap.parse_args()

    db_path = args.db or getattr(config, "PAPER_DB_FILE_PATH", "db/paper_trading.db")
    if not os.path.exists(db_path):
        print(f"DB 파일이 없습니다: {db_path}")
        return 2

    since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    print(f"[대상] {db_path} · 최근 {args.days}일 (>= {since})")

    conn = sqlite3.connect(db_path)
    rep = Report()
    try:
        fresh = check_build(conn, rep)

        cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)")]
        sel = "time, type, code, name, qty, price, reason, profit_amt, profit_rate, stop_loss_rate"
        if "buy_price" in cols:
            sel += ", buy_price"
        trades = [t for t in _rows(conn, f"SELECT {sel} FROM trades "
                                         f"WHERE order_status='체결' AND time >= ? ORDER BY time",
                                   (since,)) if _is_system(t)]
        buys = [t for t in trades if _is_buy(t)]
        sells = [t for t in trades if _is_sell(t)]
        buy_by_code = {b["code"]: b for b in buys}

        summarize(buys, sells, rep)
        check_stop_recorded(buys, rep)
        check_stop_fires_at_recorded_rate(sells, buy_by_code, rep)
        if fresh:
            check_realized_is_net(sells, rep)
        check_trailing(conn, sells, rep)
        check_slot_cap(buys, sells, rep)
        check_no_expensive_reentry(buys, sells, rep)
    finally:
        conn.close()

    return 1 if rep.print() else 0


if __name__ == "__main__":
    sys.exit(main())
