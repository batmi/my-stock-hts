"""대기 주문에 묶인 포지션이 조용히 손절 대상에서 빠지지 않게 한다.

[관측 2026-08-05] 손절 기준(-0.1%)을 넘긴 가상투자 포지션이 청산되지 않았는데,
로그에는 그 종목의 [보유분석] 줄 자체가 없었다. 매도 루프에서 통째로 빠졌다는 뜻인데
어느 경로인지 알 수 없었다 — 대기 주문 스킵과 주문가능수량 0 스킵이 둘 다
FILE_DEBUG_LEVEL == "DEBUG" 일 때만 로그를 남겼기 때문이다.

**손절·트레일링을 끄는 경로는 조용해선 안 된다.** 여기서 두 가지를 고정한다.
  1) is_pending 은 '빈 dict = 대기 없음'으로 판정한다(키만 남으면 영구 스킵).
  2) 스킵은 항상 로그를 남기고, 오래 갇히면 경보로 올라간다.
"""
import threading

import pytest

from modules.auto_trade.engine import (NO_SELLABLE_ALERT_CYCLES, OrderManager,
                                       STUCK_PENDING_ALERT_CYCLES,
                                       UNMANAGED_STUCK_PENDING)


class _FakeTrader:
    def __init__(self):
        self.logs = []
        self.half_tp_cache = set()
        self.trailing_stop_cache = {}
        self._lock = threading.RLock()

    def log(self, msg):
        self.logs.append(msg)


@pytest.fixture
def om():
    m = OrderManager.__new__(OrderManager)
    m._lock = threading.RLock()
    m.pending_orders = {}
    m.trader = _FakeTrader()
    return m


def test_empty_dict_is_not_pending(om):
    """[회귀 방지] 주문이 모두 종결돼 빈 dict만 남으면 '대기 없음'이다.

    키 존재만 보면 그 종목이 매도 판정에서 영구히 빠져 손절이 조용히 꺼진다.
    """
    om.pending_orders["035420"] = {}
    assert om.is_pending("035420") is False
    assert om.pending_odnos("035420") == []


def test_real_pending_is_detected(om):
    om.pending_orders["035420"] = {"P123": "ORDER_SENT"}
    assert om.is_pending("035420") is True
    assert om.pending_odnos("035420") == ["P123"]


def test_unknown_code_is_not_pending(om):
    assert om.is_pending("000000") is False
    assert om.pending_odnos("000000") == []


def test_alert_threshold_is_looser_than_no_sellable():
    """대기 주문 스킵은 정상 흐름에서도 몇 주기 이어지므로 매도불가보다 여유를 둔다.

    미체결 자동 취소 타임아웃보다 짧게 잡으면 정상 취소 흐름이 오경보가 된다.
    """
    assert STUCK_PENDING_ALERT_CYCLES > NO_SELLABLE_ALERT_CYCLES
    assert UNMANAGED_STUCK_PENDING, "경보 사유 문구가 비어 있다"


def test_sell_skip_paths_are_not_debug_only():
    """[회귀 방지] 매도 판정을 건너뛰는 스킵이 DEBUG 로그 뒤에 숨지 않는다.

    소스에서 확인한다 — 이 두 줄이 다시 DEBUG 조건 뒤로 들어가면, 손절이 왜 안 나가는지
    운영자가 알 수 없는 상태로 돌아간다.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "modules/auto_trade/trader.py"), encoding="utf-8").read()

    for marker in ("진행 중인 주문 존재", "주문 가능 수량 0"):
        idx = src.find(marker)
        assert idx > 0, f"스킵 로그 문구를 찾지 못했다: {marker}"
        line_start = src.rfind("\n", 0, idx) + 1
        line = src[line_start:idx]
        assert 'FILE_DEBUG_LEVEL == "DEBUG"' not in line, \
            f"'{marker}' 스킵이 DEBUG 로그로 되돌아갔다 — 손절이 꺼진 이유가 보이지 않는다"
