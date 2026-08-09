"""자동매매가 낸 주문이 자동매매 계좌로 기록되는지 검증한다.

[배경] 모든 DB 호출은 db_queue.DBProxy를 거쳐 단일 DBWorker 스레드에서 실행된다
(SQLite 'database is locked' 방지). 그런데 DBManager.insert_trade는 기록 대상 계좌를
context.trade_context.use_auto_account(threading.local)에서 읽는다.

thread-local은 스레드 간 상속되지 않으므로, 컨텍스트를 함께 실어 보내지 않으면
워커 스레드에서는 항상 기본값(False)이 되어 **자동매매 주문이 전부 수동 계좌 기록으로
남는다**. 주문은 자동 계좌로 나가는데 기록만 수동 계좌에 쌓이는 상태다.

그러면 계좌로 필터하는 조회(get_trades(account=...))가 빈 결과를 돌려주고, 평단·
트레일링 최고가·손절 기준이 붙을 자리를 잃는다. 재기동 복구도 같은 이유로 깨진다.
"""
import os
import queue
import sqlite3
import threading
import time

import pytest

import config
import context

MAIN = ("MAIN0001", "01")
AUTO = ("AUTO9999", "01")


@pytest.fixture
def proxied_db(tmp_path, monkeypatch):
    """임시 DB + 프록시(워커 스레드) 설치."""
    monkeypatch.setattr(config, 'DB_FILE_PATH', str(tmp_path / "t.db"), raising=False)

    from modules import db_manager, db_queue

    s = config.session
    for k, v in (('is_simulation', False), ('is_toss', False), ('is_paper', False),
                 ('cano', MAIN[0]), ('acnt_prdt_cd', MAIN[1]),
                 ('auto_cano', AUTO[0]), ('auto_acnt_prdt_cd', AUTO[1])):
        monkeypatch.setattr(s, k, v, raising=False)

    real = db_manager.DBManager()
    proxy = db_queue.DBProxy(real)
    monkeypatch.setattr(context.trade_context, 'use_auto_account', False, raising=False)
    try:
        yield proxy, str(tmp_path / "t.db")
    finally:
        proxy.stop()
        context.trade_context.use_auto_account = False


def _accounts(db_path):
    c = sqlite3.connect(db_path)
    try:
        return [r[0] for r in c.execute("SELECT account FROM trades ORDER BY id")]
    finally:
        c.close()


def test_auto_context_is_carried_into_the_db_worker(proxied_db):
    """자동 계좌 컨텍스트에서 낸 주문은 자동 계좌로 기록된다."""
    proxy, db_path = proxied_db
    context.trade_context.use_auto_account = True
    proxy.insert_trade("buy(AUTO)", "005930", "삼성전자", 1, "70000", "ODNO_A")

    assert _accounts(db_path) == [f"{AUTO[0]}-{AUTO[1]}"], (
        "자동매매 주문이 수동 계좌로 기록됐다 — 계좌 필터 조회가 전부 빈 결과가 된다")


def test_manual_context_still_records_the_manual_account(proxied_db):
    """수동 경로는 종전대로 수동 계좌다(전파가 한쪽으로 쏠리지 않았는지)."""
    proxy, db_path = proxied_db
    context.trade_context.use_auto_account = False
    proxy.insert_trade("buy", "005930", "삼성전자", 1, "70000", "ODNO_M")

    assert _accounts(db_path) == [f"{MAIN[0]}-{MAIN[1]}"]


def test_worker_does_not_leak_context_between_tasks(proxied_db):
    """워커 스레드는 재사용된다 — 앞 작업의 계좌가 뒤 작업에 묻으면 안 된다."""
    proxy, db_path = proxied_db

    context.trade_context.use_auto_account = True
    proxy.insert_trade("buy(AUTO)", "005930", "삼성전자", 1, "70000", "ODNO_1")
    context.trade_context.use_auto_account = False
    proxy.insert_trade("buy", "000660", "SK하이닉스", 1, "50000", "ODNO_2")
    context.trade_context.use_auto_account = True
    proxy.insert_trade("sell(AUTO)", "005930", "삼성전자", 1, "71000", "ODNO_3")

    assert _accounts(db_path) == [f"{AUTO[0]}-{AUTO[1]}",
                                  f"{MAIN[0]}-{MAIN[1]}",
                                  f"{AUTO[0]}-{AUTO[1]}"], "작업 간에 계좌 컨텍스트가 샜다"


def test_context_is_read_on_the_calling_thread_not_the_worker(proxied_db):
    """캡처 시점은 '제출한 스레드'다 — 제출 후 호출자가 컨텍스트를 바꿔도 무관하다."""
    proxy, db_path = proxied_db

    def submit():
        context.trade_context.use_auto_account = True
        proxy.insert_trade("buy(AUTO)", "005930", "삼성전자", 1, "70000", "ODNO_T")

    t = threading.Thread(target=submit, name="at_sell-fake")
    t.start()
    t.join()

    # 메인 스레드는 줄곧 수동이었지만, 제출 스레드가 자동이었으므로 자동 계좌여야 한다
    assert getattr(context.trade_context, 'use_auto_account', False) is False
    assert _accounts(db_path) == [f"{AUTO[0]}-{AUTO[1]}"]


def test_execute_custom_also_carries_the_context(proxied_db):
    """트랜잭션 단위 커스텀 작업도 같은 규약을 따른다."""
    proxy, _ = proxied_db
    seen = []

    def probe():
        seen.append(getattr(context.trade_context, 'use_auto_account', False))

    context.trade_context.use_auto_account = True
    proxy.execute_custom(probe)
    assert seen == [True], "execute_custom이 계좌 컨텍스트를 잃었다"
