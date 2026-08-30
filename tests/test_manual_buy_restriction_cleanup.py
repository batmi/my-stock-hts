"""수동 매수의 제한 종목 자동 정리 — 제한이 잘못 남으면 손절이 멈춘다.

[구조] 자동 계좌에서 사람이 직접 매수하면 **발주 즉시** 그 종목을 제한 종목(수동매매)에
넣는다. 체결 감지는 감시 주기에 의존해 지연이 있고, 그 사이 시스템이 그 종목을 팔아
버리는 창을 막기 위해서다. 대신 그 매수가 끝내 체결되지 못하면(취소·거부) 제한을 도로
풀어야 한다 — 안 풀면 잔여물이 남아 시스템이 그 종목을 영영 관리하지 않는다.

[종전 판정의 구멍] 체결 여부를 '잔고 > 0'으로 봤다. **이미 들고 있던 종목**을 추가로
수동 매수했다가 취소하면, 기존 보유분 때문에 잔고가 0이 아니라 '체결됨'으로 읽혀
제한이 영원히 남는다. 그 포지션은 시스템 매도 대상에서 빠지므로 손절·트레일링이
정지한다(경보는 나가지만 청산은 사람이 손대기 전까지 멈춘다).

지금은 ① 그 주문번호의 체결 기록 ② 발주 전 대비 **늘어난** 잔고 두 가지로 판정한다.
이 파일은 그 판정을, 특히 '기존 보유 + 취소' 조합을 못 박는다(종전 전체 스위트에서
이 함수는 한 줄도 실행되지 않았다 — 2026-08-30 커버리지 실측).
"""
import pytest
from unittest.mock import patch, MagicMock

from modules.auto_trade import common as at_common

CODE, CANO, ACNT = "005930", "44048158", "01"

#  conftest 의 autouse fixture(block_buy_restriction_cleanup_thread)가 이 함수를 통째로
#  무력화한다 — 다른 테스트에서 데몬 스레드가 살아남아 실제 서버로 잔고를 폴링하기 때문이다.
#  여기서는 그 함수 자체가 검증 대상이므로, 임포트 시점의 진짜 구현을 붙잡아 쓴다.
_REAL_CLEANUP = at_common.schedule_buy_restriction_cleanup


class _SyncThread:
    """스레드를 만들지 않고 즉시 실행한다(테스트에서 워커를 동기적으로 태우기 위해)."""
    def __init__(self, target=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


def _run(*, pre_qty=None, odno=None, balance_qty=0, filled=False, pending=False):
    """정리 워커를 한 번 태우고 (해제 Mock, 잔고조회 Mock)을 돌려준다."""
    om = MagicMock()
    om.is_pending.return_value = pending
    trader = MagicMock()
    trader.order_manager = om

    with patch.object(at_common, 'remove_restricted_stock') as removed, \
         patch('modules.auto_trade.current_holding_qty',
               return_value=balance_qty) as qty, \
         patch('modules.auto_trade.AutoTrader', return_value=trader), \
         patch.object(at_common.db_manager.db, 'check_trade_exists', return_value=filled), \
         patch.object(at_common.time, 'sleep'), \
         patch.object(at_common.threading, 'Thread', _SyncThread):
        _REAL_CLEANUP(CODE, CANO, ACNT, pre_qty=pre_qty, odno=odno)
    return removed, qty


# ───────────────────── 제한을 풀어야 하는 경우 ─────────────────────

def test_a_cancelled_add_on_buy_releases_the_restriction():
    """[핵심] 이미 10주를 들고 있는데 추가 매수가 취소됐다 — 제한이 남으면 손절이 멈춘다."""
    removed, _ = _run(pre_qty=10, balance_qty=10, pending=False)
    removed.assert_called_once_with(CODE, cano=CANO, acnt=ACNT)


def test_a_cancelled_first_buy_releases_the_restriction():
    """[하위 호환] 보유가 없던 종목의 취소 — 종전 판정과 같은 결과여야 한다."""
    removed, _ = _run(pre_qty=0, balance_qty=0, pending=False)
    removed.assert_called_once()


def test_it_still_works_without_a_pre_quantity():
    """발주 전 수량을 못 구한 호출(구 시그니처·조회 실패)도 종전대로 동작한다."""
    removed, _ = _run(pre_qty=None, balance_qty=0, pending=False)
    removed.assert_called_once()


# ───────────────────── 제한을 유지해야 하는 경우 ─────────────────────

def test_an_increased_balance_keeps_the_restriction():
    """잔고가 늘었다 = 체결됐다 — 사람이 산 물량이므로 시스템이 건드리면 안 된다."""
    removed, _ = _run(pre_qty=10, balance_qty=13)
    removed.assert_not_called()


def test_a_fill_record_settles_it_without_asking_the_balance():
    """체결 기록이 있으면 확정이다 — 잔고를 물어볼 필요도 없다(호출도 아끼는 편이 좋다)."""
    removed, qty = _run(pre_qty=10, odno="0000123456", balance_qty=10, filled=True)
    removed.assert_not_called()
    qty.assert_not_called()


def test_a_still_pending_order_is_not_treated_as_cancelled():
    """지정가가 아직 걸려 있으면 미체결로 단정하지 않는다(추적 시간까지 기다린다)."""
    removed, _ = _run(pre_qty=10, balance_qty=10, pending=True)
    removed.assert_not_called()


def test_an_unreadable_balance_keeps_the_restriction():
    """잔고 조회 실패(None)는 '보유 없음'이 아니다 — 모르면 유지한다."""
    removed, _ = _run(pre_qty=10, balance_qty=None, pending=False)
    removed.assert_not_called()


# ───────────────────── 보유수량 조회 헬퍼 ─────────────────────

def test_holding_quantity_distinguishes_failure_from_zero():
    """'모름'(None)과 '없음'(0)이 갈려야 위 판정이 성립한다."""
    with patch('modules.auto_trade.common.api.get_domestic_balance', return_value=(None, None)):
        assert at_common.current_holding_qty(CODE, CANO, ACNT) is None
    with patch('modules.auto_trade.common.api.get_domestic_balance', return_value=([], None)):
        assert at_common.current_holding_qty(CODE, CANO, ACNT) == 0
    with patch('modules.auto_trade.common.api.get_domestic_balance',
               return_value=([{'pdno': CODE, 'hldg_qty': '7'}], None)):
        assert at_common.current_holding_qty(CODE, CANO, ACNT) == 7


def test_holding_quantity_reads_the_overseas_balance():
    with patch('modules.auto_trade.common.api.get_overseas_balance',
               return_value=[{'ovrs_pdno': 'AAPL', 'ovrs_cblc_qty': '3'}]):
        assert at_common.current_holding_qty('AAPL', CANO, ACNT, is_overseas=True) == 3
    with patch('modules.auto_trade.common.api.get_overseas_balance', return_value=None):
        assert at_common.current_holding_qty('AAPL', CANO, ACNT, is_overseas=True) is None
