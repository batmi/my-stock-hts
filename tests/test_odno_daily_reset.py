"""증권사 주문번호(odno)는 당일 채번이라 날짜와 짝지어야 유일하다.

2026-09-04 감사 · 사용자 실거래 기록 실측:
  날짜순으로 정렬한 KIS 10자리 주문번호가 19번 중 7번 '이전보다 작아진다'.
  하루 안에서는 단조 증가한다(04-10: 9,048,600 → 13,587,000 → 14,186,400).
  → 번호는 누적이 아니라 **매일 리셋**된다.

종전에는 체결 추적 캐시가 `f"{cano}-{odno}"` 로, DB 중복 판정이 odno 하나로만 이뤄졌다.
어제 100주가 체결된 번호가 오늘 10주짜리 새 주문으로 재등장하면
  - 캐시: `10 > 100` 이 거짓 → 체결 감지 블록 전체를 건너뛴다
    (DB 기록·텔레그램·수동매매 제한 등록·전량매도 시 제한 해제까지 모두)
  - DB : `check_trade_exists(odno, "체결")` 가 참 → 오늘 체결이 영영 안 남는다
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from modules import db_manager
from modules.auto_trade import conclusion


# --------------------------------------------------------------------------
# DB 중복 판정
# --------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    import config as _cfg
    from modules.db_manager import DBManager
    orig = _cfg.DB_FILE_PATH
    _cfg.DB_FILE_PATH = str(tmp_path / "t.sqlite")
    m = DBManager()
    yield m
    if getattr(getattr(m, 'local', None), 'conn', None):
        m.local.conn.close()
    _cfg.DB_FILE_PATH = orig


def test_check_trade_exists_can_be_scoped_to_a_day(db):
    old = (datetime.now() - timedelta(days=40)).strftime('%Y-%m-%d %H:%M:%S')
    db.insert_trade("매수", "005930", "삼성전자", 100, 10000, "0013747800",
                    order_status="체결", custom_time=old)

    assert db.check_trade_exists("0013747800", "체결") is True          # 종전 동작(전체 이력)
    assert db.check_trade_exists("0013747800", "체결", on_date=old[:10]) is True
    today = datetime.now().strftime('%Y-%m-%d')
    assert db.check_trade_exists("0013747800", "체결", on_date=today) is False, (
        "몇 달 전 같은 번호 때문에 오늘 체결이 '이미 있음'으로 판정되면 안 된다")


def test_scope_defaults_to_the_whole_history():
    """인자를 안 주면 종전 동작(전체 이력) 그대로여야 한다."""
    import inspect

    sig = inspect.signature(db_manager.DBManager.check_trade_exists)
    assert sig.parameters['on_date'].default is None


def test_backfill_scopes_to_the_records_own_date():
    """복원은 '오늘'이 아니라 **그 체결의 날짜**로 좁힌다.

    복원 구간이 최대 12개월이라 그 안에서 같은 주문번호가 여러 번 나온다. 전체 이력에서
    찾으면 다른 날의 같은 번호 때문에 진짜 체결이 '이미 있음'으로 건너뛰어지고, 그 종목의
    평단·진입일·손절률이 복원되지 않는다.
    """
    import inspect

    from modules import holdings_backfill
    src = inspect.getsource(holdings_backfill.apply)
    assert "on_date=" in src, "복원이 날짜 없이 중복 판정한다"
    assert "r.get('time')" in src, "그 체결이 저장될 날짜로 좁혀야 한다"

    sig = inspect.signature(holdings_backfill._exists)
    assert 'on_date' in sig.parameters


# --------------------------------------------------------------------------
# 체결 추적 캐시
# --------------------------------------------------------------------------
@pytest.fixture
def monitor():
    conclusion.ConclusionMonitor._instance = None
    m = conclusion.ConclusionMonitor()
    m.order_status = {}
    m.cancel_status = {}
    yield m
    conclusion.ConclusionMonitor._instance = None


def test_order_key_is_a_tuple_with_the_date():
    """토스 주문번호에는 '-' 가 들어 있다 — 문자열로 이으면 날짜 자리를 되찾을 수 없다."""
    import inspect

    src = inspect.getsource(conclusion.ConclusionMonitor._check_conclusions)
    assert "order_key = (cano, key_date, odno)" in src
    assert 'order_key = f"{cano}-{odno}"' not in src


def test_yesterdays_key_is_purged(monitor):
    today = datetime.now().strftime('%Y%m%d')
    old = (datetime.now() - timedelta(days=5)).strftime('%Y%m%d')
    yday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')

    monitor.order_status[("123", old, "0013747800")] = 100
    monitor.order_status[("123", yday, "0013747800")] = 50
    monitor.order_status[("123", today, "0013747800")] = 10
    monitor.cancel_status[("123", old, "0013747800")] = 7

    monitor._purge_stale_order_keys(today)

    assert ("123", old, "0013747800") not in monitor.order_status, "옛 일자가 램에 계속 쌓인다"
    assert ("123", old, "0013747800") not in monitor.cancel_status
    assert monitor.order_status[("123", yday, "0013747800")] == 50, "어제까지는 남긴다(자정 걸침)"
    assert monitor.order_status[("123", today, "0013747800")] == 10


def test_reused_odno_does_not_hide_todays_fill(monitor):
    """어제 100주 체결된 번호가 오늘 10주로 재등장해도 오늘 체결이 보여야 한다."""
    today = datetime.now().strftime('%Y%m%d')
    yday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    monitor.order_status[("123", yday, "0013747800")] = 100

    prev_today = monitor.order_status.get(("123", today, "0013747800"), 0)
    assert prev_today == 0, "날짜가 다르면 어제 값이 오늘 판정에 끼어들면 안 된다"
    assert 10 > prev_today, "체결 감지 조건(tot_ccld_qty > prev_qty)이 성립해야 한다"


def test_purge_survives_a_bad_date(monitor):
    monitor.order_status[("123", "20260101", "1")] = 1
    monitor._purge_stale_order_keys("이상한값")      # 예외 없이 아무것도 하지 않는다
    assert monitor.order_status


# --------------------------------------------------------------------------
# 일자 스코프 헬퍼
# --------------------------------------------------------------------------
def test_scope_date_follows_the_stored_time_not_ord_dt():
    """저장 일자는 ord_dt 가 아니라 실제로 쓰는 시각을 따른다.

    체결 시각이 원 주문 접수 시각보다 과거로 오면(거래소 서버 시간 역전) 접수 시각으로
    당겨서 저장한다. 판정 일자를 ord_dt 로 잡으면 저장 일자와 어긋나 **같은 체결이 두 번**
    적재된다(2026-09-04 이 테스트로 실제로 잡혔다).
    """
    item = {'ord_dt': '20260804'}
    assert conclusion._odno_scope_date(item) == "2026-08-04"
    assert conclusion._odno_scope_date(item, "2026-09-04 10:00:00") == "2026-09-04"
    assert conclusion._odno_scope_date(item, "") == "2026-08-04"


@pytest.mark.parametrize("ord_dt,expect_today", [
    ("20260410", False), ("", True), (None, True), ("2026041", True), ("abcdefgh", True),
])
def test_scope_date_from_order_row(ord_dt, expect_today):
    got = conclusion._odno_scope_date({'ord_dt': ord_dt})
    if expect_today:
        assert got == datetime.now().strftime('%Y-%m-%d')
    else:
        assert got == "2026-04-10"


def test_todays_history_paths_are_scoped():
    """오늘 주문을 다루는 경로는 전부 그날로 좁혀야 한다 — 소스로 고정한다."""
    import inspect
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("modules/auto_trade/conclusion.py", "modules/account.py",
                "modules/trading.py", "modules/auto_trade/common.py", "api/orders.py"):
        lines = (root / rel).read_text(encoding='utf-8').splitlines()
        for i, line in enumerate(lines):
            if "check_trade_exists(" not in line or line.strip().startswith("#"):
                continue
            #  호출이 여러 줄에 걸치므로 뒤 두 줄까지 함께 본다.
            stmt = " ".join(lines[i:i + 3])
            assert "on_date" in stmt, f"{rel}:{i + 1} 날짜 없이 중복 판정한다 → {line.strip()}"
