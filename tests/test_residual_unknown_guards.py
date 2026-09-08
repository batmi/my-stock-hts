"""2026-09-08 감사의 잔여 두 건 — 못 읽은 값이 숫자가 되고, 못 쓴 자리가 잠겨 있던 곳.

① 서킷브레이커 알림의 등락률
   `market_halt._index_rate` 는 전일 종가만 검사하고 현재가는 안 봤다. 현재가 필드가
   비면 `curr = 0` 이 되어 등락률이 **-100.00%** 로 찍힌다 — 하필 CB 알림에 붙는
   숫자다. CB 중은 모두가 시세를 두드려 조회가 가장 잘 실패하는 순간이라 실제로 닿는다
   (같은 클래스의 `checked < 2` 주석이 같은 얘기를 한다).

② 체결통보 등록 슬롯
   호출부가 구독 **전에** `set_reserved(1)` 로 41건 중 한 자리를 떼어 뒀는데, send 가
   실패해도 그 예약이 남았다. 구독은 안 됐는데 자리만 비워 둔 꼴이라 시세 커버리지가
   한 종목 줄어든 채 그 연결이 끊길 때까지 회복되지 않았다(재시도도 연결 직후 한 번뿐).
   예약은 이제 `_subscribe_exec` 가 성공 여부에 맞춰 잡고 푼다.
"""
import asyncio

import pytest

from brokers import realtime as rt
from modules import market_halt


# ── ① 못 읽은 지수를 -100% 로 만들지 않는다 ────────────────────────
class _Monitor(market_halt.MarketHaltMonitor):
    def __init__(self):        # 실제 __init__ 은 스레드·알림을 끌고 온다
        pass


@pytest.fixture
def monitor():
    return _Monitor()


def _patch_index(monkeypatch, output):
    monkeypatch.setattr(market_halt.api, "get_domestic_index_price",
                        lambda code: {"rt_cd": "0", "output": output})


def test_index_rate_normal(monkeypatch, monitor):
    _patch_index(monkeypatch, {"bstp_nmix_prpr": "2450.0", "bstp_nmix_prdy_clpr": "2500.0"})
    assert monitor._index_rate("KOSPI") == pytest.approx(-2.0)


def test_missing_current_price_yields_no_rate_not_minus_100(monkeypatch, monitor):
    _patch_index(monkeypatch, {"bstp_nmix_prdy_clpr": "2500.0"})
    assert monitor._index_rate("KOSPI") is None, \
        "현재가를 못 읽었는데 '지수 -100.00%' 를 만들어 냈다"


def test_missing_prev_close_yields_no_rate(monkeypatch, monitor):
    _patch_index(monkeypatch, {"bstp_nmix_prpr": "2450.0"})
    assert monitor._index_rate("KOSPI") is None


def test_query_failure_yields_no_rate(monkeypatch, monitor):
    monkeypatch.setattr(market_halt.api, "get_domestic_index_price",
                        lambda code: {"rt_cd": "1"})
    assert monitor._index_rate("KOSPI") is None


# ── ② 못 쓴 등록 슬롯은 시세에 돌려준다 ────────────────────────────
class _WS:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, msg):
        if self.fail:
            raise ConnectionError("소켓 끊김")
        self.sent.append(msg)


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setattr(rt.KisRealtimeFeed, "_exec_enabled", lambda self: True)
    monkeypatch.setattr(rt.KisRealtimeFeed, "_hts_id", lambda self: "testid")
    f = rt.KisRealtimeFeed()
    f.manager.set_reserved(1)      # 종전 호출부가 미리 걸어 두던 상태를 흉내 낸다
    return f


def test_failed_exec_subscription_releases_the_slot(feed):
    cap_before = feed.manager.max_regs
    asyncio.run(feed._subscribe_exec(_WS(fail=True), "approval"))
    assert feed._exec_subscribed is False
    assert feed.manager.capacity_symbols() == cap_before, \
        "구독은 실패했는데 등록 슬롯이 잠긴 채 남았다"


def test_successful_exec_subscription_holds_the_slot(feed):
    ws = _WS()
    asyncio.run(feed._subscribe_exec(ws, "approval"))
    assert feed._exec_subscribed is True
    assert ws.sent, "구독 메시지가 나가지 않았다"
    assert feed.manager.capacity_symbols() == feed.manager.max_regs - 1


def test_retry_after_failure_succeeds(feed):
    asyncio.run(feed._subscribe_exec(_WS(fail=True), "approval"))
    ws = _WS()
    asyncio.run(feed._subscribe_exec(ws, "approval"))   # 다음 재조정 주기
    assert feed._exec_subscribed is True and ws.sent
    assert feed.manager.capacity_symbols() == feed.manager.max_regs - 1


def test_already_subscribed_is_a_noop(feed):
    ws = _WS()
    asyncio.run(feed._subscribe_exec(ws, "approval"))
    asyncio.run(feed._subscribe_exec(ws, "approval"))
    assert len(ws.sent) == 1, "매 주기 재호출이 중복 구독을 보내면 안 된다"


def test_disabled_exec_reserves_nothing(monkeypatch):
    monkeypatch.setattr(rt.KisRealtimeFeed, "_exec_enabled", lambda self: False)
    f = rt.KisRealtimeFeed()
    f.manager.set_reserved(1)
    asyncio.run(f._subscribe_exec(_WS(), "approval"))
    assert f.manager.capacity_symbols() == f.manager.max_regs
