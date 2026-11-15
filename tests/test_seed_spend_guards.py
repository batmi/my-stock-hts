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


# ─────────── ③ 옛 앵커를 못 지웠으면 사지 않는다 (2026-09-06) ───────────
#
#  신규 매수 직전의 delete_trailing_stop 은 **새 포지션이 이전 포지션의 고점을
#  물려받지 않게** 하는 장치다. 그런데 이 함수는 실패를 통째로 삼켰고 반환값도 없었다
#  (같은 행을 쓰는 update_highest_price 는 처음부터 bool 을 돌려주고 그 사유까지 적어
#  뒀는데도). 실패해도 그 세션은 멀쩡해 보인다 — 메모리 캐시는 pop 되므로.
#  **재기동해야** 드러난다: DB 의 옛 고점이 실려 와 샹들리에 TS 가 '고점 대비 폭락'으로
#  읽고 즉시 시장가 청산을 때린다. 진입을 한 주기 미루는 것은 되돌릴 수 있고,
#  그 청산은 되돌릴 수 없다.

def _buy_with_anchor_delete(trader, *, ok, half_ok=True):
    cand = [{'code': CODE, 'name': NAME, 'price': 70_000, 'score': 9.0,
             'rsi': 50, 'adx': 30, 'cci': 100, 'is_custom_rule': False, 'atr': 1000}]
    with patch.dict(config.SELL_STRATEGY, ATR_STOP), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=1000), \
         patch('modules.auto_trade.db_manager.db.delete_trailing_stop', return_value=ok), \
         patch('modules.auto_trade.db_manager.db.delete_half_tp', return_value=half_ok), \
         patch('modules.auto_trade.db_manager.db.cancel_reserved_buy_orders', return_value=0), \
         patch.object(trader.order_manager, 'send_order', return_value="ODNO1") as order:
        trader._execute_buy_orders(cand, 10_000_000, 0.25, 0, 4)
    return order


def test_new_buy_holds_when_the_old_anchor_cannot_be_cleared(trader):
    assert not _buy_with_anchor_delete(trader, ok=False).called, \
        "옛 트레일링 고점을 못 지웠는데 매수가 나갔다 — 재기동하면 즉시 청산된다"


def test_new_buy_proceeds_when_the_anchor_is_cleared(trader):
    """대조군 — 상시 보류로 기능이 죽은 상태도 '통과'가 되므로 반드시 함께 잰다."""
    assert _buy_with_anchor_delete(trader, ok=True).called


def test_a_held_buy_returns_its_heat_reservation(trader):
    """보류하면서 선점한 히트 예산을 반납하지 않으면 그 예산이 영영 묶인다."""
    trader.portfolio_heat_amt = 0.0
    _buy_with_anchor_delete(trader, ok=False)
    assert trader.portfolio_heat_amt == pytest.approx(0.0), \
        f"선점분이 반납되지 않았다: {trader.portfolio_heat_amt}"


def test_a_stale_half_tp_record_does_not_block_the_buy(trader):
    """반익절 기록은 방향이 다르다 — 남아 있어도 '없는 매도'를 만들지는 않는다."""
    assert _buy_with_anchor_delete(trader, ok=True, half_ok=False).called


# ─────────── ④ 개장 직후에는 신규도 증액도 사지 않는다 (2026-11-15) ───────────
#
#  개장 직후 보류 게이트는 신규 매수 판정(_check_buy_conditions) 맨 앞에만 있었다. 증액은 매도 판정
#  루프가 '보유'로 본 종목마다 부르는 경로라 그 게이트를 지나지 않았고, 개장 1분 30초에 증액이 나갔다
#  (2026-08-26 09:01:30 한국콜마 · 가상 원장 증액 5건 중 4건이 09:00~09:29).
#  게이트의 이유(개장 스파이크를 고점에서 추격)는 증액에 더 직접 걸린다 — 증액의 발동 조건이
#  '수익률 문턱 도달'이고 스파이크가 바로 그 문턱을 넘겨 주는 사건이기 때문이다.
#  신규 매수 쪽의 런타임 확인은 tests/test_entry_open_delay.py 에 있다(같은 함수 이름을 같은 모듈에서 읽는다).
#  [주의] conftest 가 스위트 전역에서 이 게이트를 0 으로 꺼 둔다 — 여기서 명시적으로 되살린다.

def test_pyramid_holds_during_the_open_delay(trader, monkeypatch):
    """[핵심] 보류 중에는 증액 주문이 나가지 않고, 횟수·리스크 예산도 건드리지 않는다."""
    from modules.auto_trade import buying as buying_mod
    monkeypatch.setattr(buying_mod, "entry_open_delay_remaining", lambda *a, **k: 28 * 60 + 30)
    trader.portfolio_heat_amt = 0.0
    with patch.object(db_manager.db, 'bump_pyramid_count', return_value=True) as bump, \
         patch.object(trader, 'log') as log:
        order = _pyramid(trader)
    assert not order.called, "개장 직후 보류 중인데 증액 주문이 나갔다"
    assert trader.portfolio_heat_amt == 0.0, "보류인데 리스크 예산이 선점된 채 남았다"
    logged = " ".join(str(c) for c in log.call_args_list)
    assert "개장 직후 증액 보류" in logged, f"보류 사유를 남기지 않는다: {logged}"
    del bump    # _pyramid 가 같은 이름을 다시 감싸므로 바깥 patch 는 호출 수를 볼 수 없다


def test_pyramid_goes_out_once_the_open_delay_is_over(trader, monkeypatch):
    """[대조군] 보류가 끝나면 그대로 나간다 — 상시 보류로 기능이 죽은 상태도 '통과'가 되기 때문이다."""
    from modules.auto_trade import buying as buying_mod
    monkeypatch.setattr(buying_mod, "entry_open_delay_remaining", lambda *a, **k: 0)
    assert _pyramid(trader).called, "보류가 없는데 증액이 막혔다"


def test_pyramid_open_delay_is_logged_once_per_stock_per_day(trader, monkeypatch):
    """보류는 30분 동안 주기마다 걸린다 — 같은 종목 줄이 주기 수만큼 쌓이면 로그가 그것으로 덮인다."""
    from modules.auto_trade import buying as buying_mod
    monkeypatch.setattr(buying_mod, "entry_open_delay_remaining", lambda *a, **k: 600)
    with patch.object(trader, 'log') as log:
        for _ in range(5):
            assert not _pyramid(trader).called
    lines = [str(c) for c in log.call_args_list if "개장 직후 증액 보류" in str(c)]
    assert len(lines) == 1, f"같은 종목 보류 줄이 {len(lines)}번 적혔다"


def test_the_open_delay_comes_after_the_pyramid_signal(trader, monkeypatch):
    """증액 조건이 서지 않은 보유 종목은 보류 줄도 남기지 않는다(보유 종목마다 매 주기 불리는 경로다)."""
    from modules.auto_trade import buying as buying_mod
    monkeypatch.setattr(buying_mod, "entry_open_delay_remaining", lambda *a, **k: 600)
    result = {'state': '보유', 'score': 5.0, 'ind': {'atr': 1000}}
    with patch.object(db_manager.db, 'get_pyramid_count', return_value=0), \
         patch.object(trader.strategy, 'analyze_pyramid', return_value=(False, "")), \
         patch.object(trader, 'log') as log:
        trader._try_pyramid_buy(CODE, NAME, 100, 70_000, 3.0, result, None, is_market_open=True)
    assert not any("개장 직후" in str(c) for c in log.call_args_list)
