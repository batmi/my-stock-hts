"""시드를 지출하는 두 경로(신규 매수·피라미딩)가 같은 안전장치를 갖는가.

[왜 한 파일인가] 증액은 '이미 검증된 포지션을 키우는 것'이라 신규 매수보다 안전해
보이지만, 나가는 것은 똑같은 현금이고 늘어나는 것은 똑같은 노출이다. 그런데 가드는
_execute_buy_orders 에만 붙고 _try_pyramid_buy 에는 빠져 있었다 — 같은 자리에서
두 경로를 나란히 재는 테스트가 없어 비대칭이 오래 살아남았다.

여기서 고정하는 두 가지:

1. **탈출 기준 없음** — ATR 손절 OFF + 고정 손절 0(둘 다 사용자 설정으로 가능)이면
   청산 기준도, 손실액 상한도 없는 포지션이 된다("탈출 전략이 없다면 포지션을 잡지 마라").
   신규 매수는 종전부터 막았지만 증액은 그대로 나갔고, 더 나쁘게는 히트 캡 판정이
   `sl_rate < 0` 일 때만 도는 구조라 리스크 회계에서도 통째로 빠졌다.
2. **미체결 현황 미확인** — 재기동 직후 복구 조회가 실패하면 is_pending 맵이 비어
   '미체결 없음'과 '모름'이 구분되지 않는다. 그 상태의 주문은 거래소에 이미 걸린 주문을
   못 본 채 내는 두 번째 주문이다(주문 유실은 재전송하지 않는다는 규약과 같은 자리).

각 게이트마다 대조군(정상 조건에서는 주문이 나간다)을 함께 둔다 — 상시 보류로
기능이 죽은 상태도 '통과'가 되기 때문이다.
"""
import pytest
from unittest.mock import patch

import config
from modules import db_manager
from modules.auto_trade import AutoTrader

CODE, NAME = "005930", "삼성전자"
NO_STOP = {"USE_ATR_STOP": False, "STOP_LOSS_RATE": 0}
ATR_STOP = {"USE_ATR_STOP": True, "STOP_LOSS_RATE": -7.0, "ATR_STOP_MULTIPLIER": 2.0}


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.buy_halted = False
    t.pending_restore_ok = True
    t.initial_asset = 10_000_000
    t.current_total_asset = 10_000_000
    t.portfolio_heat_amt = 0.0
    t.portfolio_heat_unknown = False
    yield t


# ───────────────────────── 경로 2개를 태우는 헬퍼 ─────────────────────────

def _pyramid(trader, *, sell_strategy=ATR_STOP, atr=1000, rule=None):
    """증액 1회를 태우고 주문 Mock을 돌려준다."""
    result = {'state': '보유', 'score': 5.0, 'ind': {'atr': atr}}
    with patch.object(db_manager.db, 'get_pyramid_count', return_value=0), \
         patch.object(db_manager.db, 'bump_pyramid_count', return_value=True), \
         patch.object(trader.strategy, 'analyze_pyramid', return_value=(True, "피라미딩 1차")), \
         patch.object(config, 'USE_MARKET_FILTER', False), \
         patch.dict(config.SELL_STRATEGY, sell_strategy), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=1000), \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value="ODNO1") as order:
        trader._try_pyramid_buy(CODE, NAME, 100, 70_000, 12.0, result, None,
                                is_market_open=True, rule=rule)
    return order


def _buy(trader, *, sell_strategy=ATR_STOP, atr=1000):
    """신규 매수 1회를 태우고 주문 Mock을 돌려준다."""
    cand = [{'code': CODE, 'name': NAME, 'price': 70_000, 'score': 9.0,
             'rsi': 50, 'adx': 30, 'cci': 100, 'is_custom_rule': False, 'atr': atr}]
    with patch.dict(config.SELL_STRATEGY, sell_strategy), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=1000), \
         patch('modules.auto_trade.db_manager.db.delete_trailing_stop'), \
         patch('modules.auto_trade.db_manager.db.delete_half_tp'), \
         patch('modules.auto_trade.db_manager.db.cancel_reserved_buy_orders', return_value=0), \
         patch.object(trader.order_manager, 'send_order', return_value="ODNO1") as order:
        trader._execute_buy_orders(cand, 10_000_000, 0.25, 0, 4)
    return order


# ───────────────────── ① 탈출 기준이 없으면 사지 않는다 ─────────────────────

def test_new_buy_needs_a_stop(trader):
    assert not _buy(trader, sell_strategy=NO_STOP).called, \
        "손절 기준이 없는데 신규 매수가 나갔다"


def test_pyramid_needs_a_stop(trader):
    """[핵심] 증액도 같다 — 검증된 포지션이라도 탈출 기준 없이 키우지 않는다."""
    assert not _pyramid(trader, sell_strategy=NO_STOP).called, \
        "손절 기준이 없는데 증액 주문이 나갔다"


def test_pyramid_without_a_stop_does_not_slip_past_the_heat_cap(trader):
    """[핵심] 손절률 0은 히트 캡 우회로이기도 했다 — 예산을 잡지도, 쓰지도 않았다."""
    trader.portfolio_heat_amt = 0.0
    assert not _pyramid(trader, sell_strategy=NO_STOP).called
    assert trader.portfolio_heat_amt == 0.0, "보류인데 예산이 선점된 채 남았다"


def test_pyramid_stop_from_a_null_rule_column_is_a_clean_hold(trader, caplog):
    """개별 룰의 stop_loss 가 NULL 이고 ATR 도 없으면 — 종전에는 TypeError 였다.

    결과는 우연히 fail-closed 였지만 로그에 '피라미딩 오류'만 남아 원인을 알 수 없었다.
    """
    order = _pyramid(trader, sell_strategy=NO_STOP, atr=0, rule={'stop_loss': None})
    assert not order.called
    assert "오류" not in caplog.text, "가드가 아니라 예외로 멈췄다"


def test_both_paths_buy_when_a_stop_exists(trader):
    """[대조군] 정상 설정에서는 둘 다 나간다 — 보류가 상시면 기능이 죽는다."""
    assert _buy(trader).called, "정상 설정인데 신규 매수가 막혔다"
    assert _pyramid(trader).called, "정상 설정인데 증액이 막혔다"


# ─────────────── ② 미체결 현황을 모르면 신규도 증액도 멈춘다 ───────────────

def test_pyramid_blocked_while_the_open_order_state_is_unknown(trader):
    """[핵심] 복구 실패 상태의 is_pending 은 '없음'이 아니라 '모름'이다."""
    trader.pending_restore_ok = False
    assert not _pyramid(trader).called, \
        "미체결 현황을 모르는 상태에서 증액 주문이 나갔다 (중복 주문 위험)"


def test_pyramid_resumes_once_the_state_is_known(trader):
    """[대조군] 복구되면 저절로 풀린다 — 영구 차단이 아니다."""
    trader.pending_restore_ok = False
    assert not _pyramid(trader).called
    trader.pending_restore_ok = True
    assert _pyramid(trader).called, "복구됐는데 증액이 계속 막혀 있다"
