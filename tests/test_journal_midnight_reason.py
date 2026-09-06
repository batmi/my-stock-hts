"""자정을 넘긴 체결도 '왜 샀는가'를 잃지 않는다.

[왜] `_lookup_entry_reason` 은 원 주문('접수' 행)의 사유를 **체결 행의 날짜 하나로**
 좁혀 찾았다. 주문번호는 당일 채번이라 날짜로 좁히는 것 자체는 옳다([[odno-daily-reset]]).
 그런데 미국 정규장은 한국 시각 22:30~06:00(서머타임 23:30~07:00)이라 접수와 체결이
 **한국 날짜 자정을 사이에 두고 갈린다**([[order-age-midnight]] 와 같은 이유).
 실측(접수 23:57 / 체결 00:02, 같은 odno·같은 계좌): 같은 날 체결은 근거를 찾고,
 자정을 넘긴 체결은 빈 문자열이었다. 그러면 웹 일지에 "체결 확인"만 남아 왜 샀는지가
 통째로 빠진다 — _compose_memo 주석이 "정작 판단 근거가 통째로 빠진다"고 적어 둔 그 상태다.

[어디까지 여는가] 새벽 체결에 한해 직전 6시간까지만 거슬러 본다. 창이 KRX 마감(15:30)
 뒤로 묶여 있어, 같은 번호를 쓴 **어제 국내 주문**은 끼어들 수 없다.
"""
import pytest

import config
from modules import journal_sync
from modules.db_manager import DBManager

ACCOUNT = '12345678-01'


@pytest.fixture
def cur(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_FILE_PATH', str(tmp_path / 'midnight.db'))
    m = DBManager()
    conn = m._get_conn()
    yield conn.cursor()
    m.close_all_connections()


def _place(cur, time, odno='0001234', account=ACCOUNT, reason='[추세매수] 조건 만족 [점수:8.5]',
           status='접수'):
    cur.execute(
        "INSERT INTO trades (time, type, code, name, qty, price, odno, account, "
        "order_status, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time, '매수(AUTO)', 'AAPL', '애플', '10', '200', odno, account, status, reason))


def _fill(time, odno='0001234', account=ACCOUNT, org=None):
    t = {'odno': odno, 'time': time, 'account': account}
    if org:
        t['org_odno'] = org
    return t


def test_같은_날_체결은_종전대로_근거를_찾는다(cur):
    _place(cur, '2026-09-06 23:57:10')
    assert journal_sync._lookup_entry_reason(
        cur, _fill('2026-09-06 23:58:40')).startswith('[추세매수]')


def test_자정을_넘긴_체결도_근거를_찾는다(cur):
    """미국 정규장 개장 직후 접수 → 한국 날짜가 바뀐 뒤 체결."""
    _place(cur, '2026-09-06 23:57:10')
    assert journal_sync._lookup_entry_reason(
        cur, _fill('2026-09-07 00:02:05')).startswith('[추세매수]')


def test_어제_국내_주문의_같은_번호는_끌어오지_않는다(cur):
    """창을 KRX 마감(15:30) 뒤로 묶어 둔 이유 그 자체."""
    _place(cur, '2026-09-06 09:30:00', reason='[국내매수] 다른 종목 근거')
    assert journal_sync._lookup_entry_reason(cur, _fill('2026-09-07 00:02:05')) == ''


def test_아침_체결은_전날을_보지_않는다(cur):
    """되돌아보는 폭(6시간)을 벗어나면 종전 규칙 그대로다 — 그 폭이 곧 새벽 경계다."""
    _place(cur, '2026-09-06 23:57:10')
    assert journal_sync._lookup_entry_reason(cur, _fill('2026-09-07 09:10:00')) == ''


def test_계좌가_다르면_끌어오지_않는다(cur):
    _place(cur, '2026-09-06 23:57:10', account='99999999-01')
    assert journal_sync._lookup_entry_reason(cur, _fill('2026-09-07 00:02:05')) == ''


def test_정정_주문도_자정을_넘어_원주문까지_거슬러_간다(cur):
    _place(cur, '2026-09-06 23:50:00', odno='0001000')
    _place(cur, '2026-09-06 23:55:00', odno='0001234', status='정정', reason='')
    got = journal_sync._lookup_entry_reason(
        cur, _fill('2026-09-07 00:02:05', odno='0001234', org='0001000'))
    assert got.startswith('[추세매수]')


def test_시각_형식이_깨져도_던지지_않는다(cur):
    _place(cur, '2026-09-06 23:57:10')
    assert journal_sync._lookup_entry_reason(cur, _fill('2026-09-07')) == ''
