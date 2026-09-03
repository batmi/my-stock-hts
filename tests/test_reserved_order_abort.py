"""예약 주문 발주가 예외로 끊길 때, 예약이 어떤 상태로 남는가.

[배경] `_execute_order`는 발주 직전에 상태를 PROCESSING 으로 바꾼다. 그런데 감시 루프는
`get_pending_reserved_orders()`(= status='PENDING')만 조회하고, **PROCESSING 을 읽는
코드가 저장소 어디에도 없다**(2026-09-03 전수 확인: 쓰기 1곳·읽기 0곳). 그래서 발주 구간에서
예외가 나면 그 예약은 다시는 돌아오지 않고 화면에도 뜨지 않는다. 예약은 대개 손절·익절
조건이라, 조용히 사라지는 것이 그대로 보호 공백이 된다.

[가르는 기준] 발주 **전**에 터졌으면 거래소에 아무것도 안 갔으므로 PENDING 으로 되돌려
다시 본다. 발주 **중·후**에 터졌으면 되돌리지 않는다 — 이미 접수됐을 수 있어 재시도가
곧 이중 주문이다. 응답 유실은 '실패'가 아니라 '모름'이고, 그건 사람이 확인해야 한다.
"""
import pytest
from unittest.mock import patch

from modules.reserved_order_monitor import ReservedOrderMonitor


ORDER = {
    'id': 4242, 'cano': '12345678', 'acnt': '01', 'code': '005930',
    'name': '삼성전자', 'market': 'KR', 'order_type': 'sell', 'qty': 10,
    'order_price': 0, 'condition_type': 'STOP', 'target_price': 60000,
}


@pytest.fixture
def monitor():
    ReservedOrderMonitor._instance = None
    m = ReservedOrderMonitor()
    yield m
    ReservedOrderMonitor._instance = None


class _Recorder:
    """update_reserved_order_status 호출을 순서대로 받아 적는다."""

    def __init__(self):
        self.calls = []

    def __call__(self, order_id, status, odno=None, fail_reason=None):
        self.calls.append((order_id, status, fail_reason))

    @property
    def statuses(self):
        return [s for _, s, _ in self.calls]


def _run(monitor, rec, boom_at, msgs):
    """boom_at: 'price'(발주 전) 또는 'order'(발주 중) 에서 예외를 낸다."""
    def bad_price(*a, **k):
        if boom_at == 'price':
            raise RuntimeError("시세 조회 폭발")
        return 70000.0

    def bad_order(*a, **k):
        raise RuntimeError("주문 전송 폭발")

    with patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_status', rec), \
         patch.object(ReservedOrderMonitor, '_reconcile_sell_qty', lambda s, o: (10, "")), \
         patch('modules.reserved_order_monitor.api.get_current_price', bad_price), \
         patch('modules.reserved_order_monitor.api.place_order', bad_order), \
         patch('modules.reserved_order_monitor.api.send_telegram_message',
               lambda m, *a, **k: msgs.append(m)):
        monitor._execute_order(dict(ORDER), "테스트 발동")


def test_failure_before_sending_returns_the_order_to_pending(monitor):
    """발주 전 오류 — 거래소에 간 것이 없으므로 다시 볼 수 있어야 한다."""
    rec, msgs = _Recorder(), []
    _run(monitor, rec, boom_at='price', msgs=msgs)

    assert rec.statuses == ['PROCESSING', 'PENDING'], rec.statuses
    assert not msgs, "발주 전 일시 오류로 사람을 깨우지 않는다"


def test_failure_while_sending_is_not_retried_and_is_reported(monitor):
    """발주 중 오류 — 되돌리지 않고, 결과 불명임을 사람에게 알린다."""
    rec, msgs = _Recorder(), []
    _run(monitor, rec, boom_at='order', msgs=msgs)

    assert rec.statuses == ['PROCESSING', 'FAILED'], rec.statuses
    assert 'PENDING' not in rec.statuses, (
        "발주 뒤에 되돌리면 다음 주기에 같은 주문이 한 번 더 나간다")
    assert msgs, "결과 불명을 알리지 않으면 아무도 모른다"
    body = "\n".join(msgs)
    assert "결과 불명" in body and "삼성전자" in body, body
    assert "재시도하지 않습니다" in body, body

    _, _, fail_reason = rec.calls[-1]
    assert fail_reason and "결과 불명" in fail_reason, fail_reason


def test_processing_is_never_a_resting_state(monitor):
    """어떤 경로로 끝나든 PROCESSING 으로 멈추지 않는다."""
    for boom in ('price', 'order'):
        rec, msgs = _Recorder(), []
        _run(monitor, rec, boom_at=boom, msgs=msgs)
        assert rec.statuses[-1] != 'PROCESSING', f"{boom}: {rec.statuses}"


def test_nothing_else_reads_the_processing_state():
    """PROCESSING 을 읽는 코드가 생기면 이 검사가 알려준다.

    지금은 쓰기 1곳뿐이라 '중간 상태'로만 존재한다. 조회 대상에 넣는 변경이 들어오면
    발주 구간이 재진입 가능해지므로(= 이중 주문), 그때 이 검사가 걸려 재검토를 강제한다.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for base in ("modules", "core", "api"):
        for dirpath, _, files in os.walk(os.path.join(root, base)):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                for n, line in enumerate(open(p, encoding='utf-8'), 1):
                    if "PROCESSING" not in line or line.strip().startswith("#"):
                        continue
                    hits.append((os.path.relpath(p, root), n, line.strip()))

    # 쓰기 한 곳뿐이어야 한다 — 상태를 PROCESSING 으로 바꾸는 그 줄.
    assert len(hits) == 1, f"PROCESSING 을 다루는 자리가 늘었다: {hits}"
    rel, _, line = hits[0]
    assert rel == "modules/reserved_order_monitor.py", rel
    assert "update_reserved_order_status" in line, line
    # 조회 대상에 들어가면 발주 구간이 재진입 가능해진다(= 이중 주문).
    assert "SELECT" not in line.upper(), line
