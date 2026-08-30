"""매매 기계 점검 도구가 **실제로 이상을 잡아내는가**.

[왜 이 테스트인가] 점검 도구의 가장 흔한 실패는 '항상 통과'다. 표본이 없으면 조용히
넘어가고, 조건이 잘못돼도 아무 말이 없으면 운용자는 검증했다고 믿는다 — 검증하지 않은
것보다 나쁘다. 그래서 각 항목마다 **깨지는 데이터**를 만들어 실제로 잡히는지 고정한다.

도구 자체는 읽기 전용이므로 여기서도 임시 DB만 만들어 쓴다.
"""
import sqlite3

import pytest

from tools import audit_trade_mechanics as A


SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, type TEXT, code TEXT, name TEXT,
    qty INTEGER, price TEXT, odno TEXT, order_status TEXT,
    profit_amt INTEGER DEFAULT 0, profit_rate REAL DEFAULT 0.0, reason TEXT,
    stop_loss_rate REAL DEFAULT 0.0 {extra}
);
CREATE TABLE trailing_stops (code TEXT PRIMARY KEY, highest_price REAL, update_time TEXT);
"""


@pytest.fixture
def db(tmp_path):
    def _make(with_buy_price=True):
        path = tmp_path / f"t{'1' if with_buy_price else '0'}.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA.format(extra=", buy_price REAL DEFAULT 0.0"
                                               if with_buy_price else ""))
        conn.commit()
        return conn, str(path)
    return _make


def _buy(conn, code="005930", name="삼성전자", price=100000, sl=-7.0,
         when="2026-08-10 10:00:00", qty=10):
    conn.execute("INSERT INTO trades (time, type, code, name, qty, price, order_status, "
                 "reason, stop_loss_rate) VALUES (?,?,?,?,?,?,'체결','신규 매수',?)",
                 (when, "buy(AUTO)", code, name, qty, str(price), sl))
    conn.commit()


def _sell(conn, code="005930", name="삼성전자", price=93000, reason="ATR손절",
          when="2026-08-10 14:00:00", qty=10, profit_amt=None, buy_price=100000,
          profit_rate=-7.0, with_buy_price=True):
    from core import trading_cost
    if profit_amt is None:
        profit_amt = int(trading_cost.net_realized_profit(buy_price, price, qty)[0])
    cols = "time, type, code, name, qty, price, order_status, reason, profit_amt, profit_rate"
    vals = [when, "sell(AUTO)", code, name, qty, str(price), "체결", reason,
            profit_amt, profit_rate]
    if with_buy_price:
        cols += ", buy_price"
        vals.append(buy_price)
    conn.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    conn.commit()


def _run(path, days=30):
    import sys
    from unittest.mock import patch
    with patch.object(sys, "argv", ["x", "--db", path, "--days", str(days)]):
        return A.main()


# ─────────────────────────────────────────────
# 0. 빌드 확인 — 가장 중요한 항목
# ─────────────────────────────────────────────

def test_old_build_is_rejected(db, capsys):
    """구버전 DB 로 아무리 오래 돌려도 현재 코드의 증거가 되지 않는다."""
    conn, path = db(with_buy_price=False)
    _buy(conn)
    assert _run(path) == 1
    out = capsys.readouterr().out
    assert "빌드가 오래됐다" in out
    assert "재기동" in out


def test_current_build_passes_the_marker(db, capsys):
    conn, path = db()
    _buy(conn)
    _run(path)
    assert "빌드 확인" in capsys.readouterr().out


# ─────────────────────────────────────────────
# 1~2. 손절 근거와 발동
# ─────────────────────────────────────────────

def test_missing_stop_rate_is_caught(db, capsys):
    """손절률이 안 남으면 매도 판정이 전역 고정폭으로 떨어진다."""
    conn, path = db()
    _buy(conn, sl=0.0)
    assert _run(path) == 1
    assert "손절률이 없다" in capsys.readouterr().out


def test_positive_stop_rate_is_caught(db, capsys):
    """부호가 뒤집히면 판정이 즉시 발동하거나 아예 안 걸린다."""
    conn, path = db()
    _buy(conn, sl=7.0)
    assert _run(path) == 1
    assert "부호가 뒤집" in capsys.readouterr().out


def test_stop_firing_too_early_is_caught(db, capsys):
    """계획 -7% 인데 -1% 에서 잘렸다면 기록과 판정이 다른 값을 보고 있다."""
    conn, path = db()
    _buy(conn, sl=-7.0)
    _sell(conn, price=99000, profit_rate=-1.0, reason="ATR손절")
    assert _run(path) == 1
    assert "얕은 손실에서 잘린" in capsys.readouterr().out


def test_deeper_stop_than_planned_is_accepted(db, capsys):
    """갭하락·슬리피지로 계획보다 나쁘게 체결되는 것은 정상이다."""
    conn, path = db()
    _buy(conn, sl=-7.0)
    _sell(conn, price=88000, profit_rate=-12.0, reason="ATR손절")
    _run(path)
    assert "얕은 손실에서 잘린" not in capsys.readouterr().out


# ─────────────────────────────────────────────
# 3. 실현손익 = 왕복 비용 차감
# ─────────────────────────────────────────────

def test_gross_profit_recorded_as_realized_is_caught(db, capsys):
    """비용을 빼지 않은 총손익이 실현손익으로 들어가면 잡아야 한다.

    이게 2026-08-10 변경의 핵심이다 — 왕복 비용보다 작은 이익이 '승'으로 집계되면
    승률·손익비가 왜곡되고 그 왜곡이 그대로 설정 결정으로 넘어간다.
    """
    conn, path = db()
    _buy(conn)
    _sell(conn, price=110000, profit_amt=100_000, profit_rate=10.0,   # 총이익 그대로
          reason="트레일링스탑")
    assert _run(path) == 1
    assert "비용 반영값과 다른" in capsys.readouterr().out


def test_net_profit_passes(db, capsys):
    conn, path = db()
    _buy(conn)
    _sell(conn, price=110000, reason="트레일링스탑", profit_rate=9.7)  # 헬퍼가 비용 차감
    _run(path)
    assert "비용 반영값과 다른" not in capsys.readouterr().out


# ─────────────────────────────────────────────
# 4~6. 트레일링 · 슬롯 · 재진입
# ─────────────────────────────────────────────

def test_zero_highest_price_is_flagged(db, capsys):
    """감시 시작가가 0이면 트레일링이 발동하지 않는다."""
    conn, path = db()
    _buy(conn)
    conn.execute("INSERT INTO trailing_stops VALUES ('005930', 0, '2026-08-10')")
    conn.commit()
    _run(path)
    assert "최고가가 0" in capsys.readouterr().out


def _fix_slot_cap(monkeypatch, n):
    """슬롯 상한을 이 테스트 안에서 못박는다.

    도구는 config 모듈 속성(config.SYSTEM_MAX_HOLDINGS)을 읽는다. 그 이름이 모듈
    __dict__ 에 있으면 PEP 562 __getattr__ 가 불리지 않아 settings 만 고쳐서는 소용이
    없다 — load_dynamic_config() 를 돌린 다른 테스트가 모듈 속성을 심어 두면 여기 검사가
    조용히 무력화된다(실제로 상한 20 이 남아 6종목 위반을 놓쳤다). 양쪽 다 고정한다.
    """
    import config
    monkeypatch.setattr(config.settings, "SYSTEM_MAX_HOLDINGS", n, raising=False)
    if "SYSTEM_MAX_HOLDINGS" in config.__dict__:
        monkeypatch.setitem(config.__dict__, "SYSTEM_MAX_HOLDINGS", n)


def test_slot_cap_breach_is_caught(db, capsys, monkeypatch):
    """슬롯 상한을 넘으면 리스크 한도 산정 전제가 깨진다.

    [주의] 상한은 config 전역이라 다른 테스트가 바꿔 놓으면 이 검사가 조용히 무력화된다
    (xdist 병렬에서 실제로 겪었다). 주변 상태에 기대지 않도록 여기서 고정한다.
    """
    _fix_slot_cap(monkeypatch, 4)
    conn, path = db()
    for i in range(6):
        _buy(conn, code=f"00593{i}", name=f"종목{i}", when=f"2026-08-10 10:0{i}:00")
    assert _run(path) == 1
    assert "슬롯 상한" in capsys.readouterr().out


def test_slot_cap_counts_sells_back(db, capsys, monkeypatch):
    """팔면 슬롯이 비어야 한다 — 매도를 안 세면 멀쩡한 운용도 위반으로 잡힌다."""
    _fix_slot_cap(monkeypatch, 4)
    conn, path = db()
    for i in range(6):
        _buy(conn, code=f"00593{i}", name=f"종목{i}", when=f"2026-08-10 10:0{i}:00")
        _sell(conn, code=f"00593{i}", name=f"종목{i}", when=f"2026-08-10 10:0{i}:30",
              reason="트레일링스탑", profit_rate=1.0)
    _run(path)
    assert "슬롯 상한" not in capsys.readouterr().out


def test_expensive_reentry_is_caught(db, capsys):
    """당일 손절가 이상에서 되사면 왕복 스프레드만큼 손실이 쌓인다."""
    conn, path = db()
    _buy(conn, when="2026-08-10 09:30:00")
    _sell(conn, price=93000, when="2026-08-10 10:00:00", reason="ATR손절", profit_rate=-7.0)
    _buy(conn, price=94000, when="2026-08-10 10:10:00")       # 손절가보다 비싸게 재매수
    assert _run(path) == 1
    assert "손절가 이상에서 되산" in capsys.readouterr().out


def test_cheaper_reentry_is_allowed(db, capsys):
    """눌림에서 다시 잡는 정상 재진입까지 막으면 추세추종에 역행한다."""
    conn, path = db()
    _buy(conn, when="2026-08-10 09:30:00")
    _sell(conn, price=93000, when="2026-08-10 10:00:00", reason="ATR손절", profit_rate=-7.0)
    _buy(conn, price=91000, when="2026-08-10 10:10:00")
    _run(path)
    assert "손절가 이상에서 되산" not in capsys.readouterr().out


# ─────────────────────────────────────────────
# 7. 표본 없음을 '통과'로 위장하지 않는다
# ─────────────────────────────────────────────

def test_empty_db_says_there_is_nothing_to_judge(db, capsys):
    """표본이 없는데 '이상 없음'으로 보이면 검증했다고 착각하게 된다."""
    conn, path = db()
    _run(path)
    out = capsys.readouterr().out
    assert "체결이 한 건도 없다" in out
    assert "표본 없음" in out


def test_pending_items_are_not_summarized_as_all_pass(db, capsys):
    """매수만 있고 매도가 없으면 핵심 3항목이 판정 불가다 — '모두 통과'로 맺으면 안 된다.

    [왜 이 테스트인가] 실제 운용 로그에서 이 결함이 드러났다. 매수 4건·매도 0건인데
    맨 아래 줄이 '모든 항목 통과'였다. 항목별로는 정직하게 '표본 없음'이라 적어 놓고도
    마지막 한 줄이 그걸 통과로 덮어 버리면, 아무것도 검증하지 못한 운용을 검증된 것으로
    읽게 된다 — 이 도구가 가장 경계해야 할 실패다.
    """
    conn, path = db()
    _buy(conn)
    assert _run(path) == 0          # 실패는 아니다
    out = capsys.readouterr().out
    assert "모두 통과" not in out, "판정 보류가 있는데 전체 통과로 맺었다"
    assert "보류" in out
    assert "손절 발동 검증" in out and "실현손익 검증" in out


def test_all_pass_only_when_everything_was_actually_judged(db, capsys):
    """반대로, 표본이 다 있으면 '모두 통과'라고 말해야 한다 — 늘 보류면 신호가 죽는다."""
    conn, path = db()
    _buy(conn, when="2026-08-10 09:30:00")
    _sell(conn, price=93000, when="2026-08-10 10:00:00", reason="ATR손절", profit_rate=-7.0)
    _buy(conn, price=91000, when="2026-08-10 10:10:00")
    # 매입가 기준 검사는 '매수가 한 건인' 포지션만 본다(평단이 가중평균이면 비교 불가).
    #  위 종목은 재매수로 두 건이므로, 표본이 되도록 단일 매수 포지션을 하나 더 둔다.
    _buy(conn, code="000660", name="SK하이닉스", price=200000, when="2026-08-10 09:40:00")
    _sell(conn, code="000660", name="SK하이닉스", price=220000, buy_price=200000,
          reason="트레일링스탑", profit_rate=9.7, when="2026-08-10 13:00:00")
    conn.execute("INSERT INTO trailing_stops VALUES ('005930', 100000, '2026-08-10')")
    conn.commit()
    _run(path)
    out = capsys.readouterr().out
    assert "보류" not in out, out
    assert "모두 통과" in out


def test_manual_and_external_trades_are_not_judged(db, capsys):
    """수동·외부 주문은 시스템이 낸 것이 아니므로 판정 대상이 아니다."""
    conn, path = db()
    conn.execute("INSERT INTO trades (time, type, code, name, qty, price, order_status, "
                 "reason, stop_loss_rate) VALUES "
                 "('2026-08-10 10:00:00','매수(수동)','005930','삼성전자',10,'100000','체결','수동',0)")
    conn.commit()
    _run(path)
    assert "체결이 한 건도 없다" in capsys.readouterr().out


# ─────────────────────────────────────────────
# 8. 매입가 기준 일치 (수수료 이중 차감)
# ─────────────────────────────────────────────

def test_fee_inflated_basis_is_caught(db, capsys):
    """매도에 굳는 매입가에 매수 수수료가 얹혀 있으면 실현손익이 수수료를 두 번 뺀다.

    실거래의 buy_price 는 KIS 잔고의 pchs_avg_pric 에서 오는데, 여기에 수수료가 포함되는지는
    코드로 알 수 없다(KIS 가 pchs_amt 를 실전 잔고에서 0/누락으로 줘서 매입평균가가 유일한
    소스다). 우리가 낸 매수 체결가는 우리 DB 에 있으므로 대조하면 답이 나온다.
    """
    import config
    conn, path = db()
    fill = 100000
    inflated = fill * (1 + config.BUY_FEE_RATE)     # 매입평균가에 수수료가 포함된 모양
    _buy(conn, price=fill, when="2026-08-10 09:30:00")
    _sell(conn, price=110000, buy_price=inflated, reason="트레일링스탑", profit_rate=9.7,
          when="2026-08-10 14:00:00")
    assert _run(path) == 1
    assert "매수 수수료가 포함된 것으로 보인다" in capsys.readouterr().out


def test_matching_basis_passes(db, capsys):
    """평단이 체결가와 같으면(가상계좌가 늘 그렇다) 정상이다."""
    conn, path = db()
    _buy(conn, price=100000, when="2026-08-10 09:30:00")
    _sell(conn, price=110000, buy_price=100000, reason="트레일링스탑", profit_rate=9.7,
          when="2026-08-10 14:00:00")
    _run(path)
    assert "매수 수수료가 포함된 것으로 보인다" not in capsys.readouterr().out


def test_pyramided_position_is_excluded(db, capsys):
    """매수가 여러 건이면 평단이 가중평균이라 단순 비교가 성립하지 않는다."""
    import config
    conn, path = db()
    _buy(conn, price=100000, when="2026-08-10 09:30:00")
    _buy(conn, price=120000, when="2026-08-10 10:30:00")
    _sell(conn, price=130000, buy_price=100000 * (1 + config.BUY_FEE_RATE),
          reason="트레일링스탑", profit_rate=9.7, when="2026-08-10 14:00:00")
    _run(path)
    out = capsys.readouterr().out
    assert "매수 수수료가 포함된 것으로 보인다" not in out
    assert "표본 없음" in out


def test_tick_rounding_is_not_mistaken_for_fee(db, capsys):
    """호가 반올림 수준의 차이를 수수료 포함으로 오판하면 안 된다."""
    conn, path = db()
    _buy(conn, price=100000, when="2026-08-10 09:30:00")
    _sell(conn, price=110000, buy_price=100500, reason="트레일링스탑", profit_rate=9.2,
          when="2026-08-10 14:00:00")   # +0.5% — 수수료(0.0177%)와 자릿수가 다르다
    _run(path)
    assert "매수 수수료가 포함된 것으로 보인다" not in capsys.readouterr().out
