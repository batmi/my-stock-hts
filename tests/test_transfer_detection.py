"""외부 입출금 자동 감지가 가짜로 발동하지 않는가 — 일일 손실 한도의 기준선 검증.

[왜 중요한가] 감지되면 initial_asset(당일 시작 자산)이 그만큼 이동한다. 이 값은
일일 손실 한도(SYSTEM_DAILY_LOSS_LIMIT)의 분모이자 방어 모드 발동 기준이다.
  · 가짜 '입금'이 잡히면 → 기준이 부풀어 실제 손실을 과소평가한다(비상정지가 늦는다).
  · 가짜 '출금'이 잡히면 → 기준이 줄어 멀쩡한 계좌에 방어 모드가 걸린다.
과거 실제로 두 방향 모두 겪었다(trader._monitor_account_status 주석의 Fix 이력).

[불변식] 감지 기준은 총자산이 아니라 **원금 = 현금 + 매입원가 - 실현손익** 이다.
원금은 외부 입출금이 없는 한 시세 변동·매매로 변하지 않아야 한다. 이 파일은 그
불변식이 실제로 성립하는지, 그리고 확정 규칙(5만원 이상 · 3회 연속)이 동작하는지 본다.
"""
from unittest.mock import MagicMock, patch

import pytest

import config
from modules.auto_trade import AutoTrader

CODE, NAME = "005930", "삼성전자"


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.initial_asset = 5_000_000
    t.baseline_principal = 0
    t.buy_halted = False
    for attr in ("_pending_transfer_amt", "_pending_transfer_count"):
        if hasattr(t, attr):
            delattr(t, attr)
    yield t
    t.is_running = False


def _holdings(qty=10, buy=100_000, price=100_000):
    """보유 1종목. buy=매입단가, price=현재가."""
    pchs, evlu = buy * qty, price * qty
    return [{'pdno': CODE, 'prdt_name': NAME, 'hldg_qty': str(qty),
             'ord_psbl_qty': str(qty), 'pchs_avg_pric': str(buy), 'prpr': str(price),
             'pchs_amt': str(pchs), 'evlu_amt': str(evlu),
             'evlu_pfls_amt': str(evlu - pchs),
             'evlu_pfls_rt': f"{(evlu - pchs) / pchs * 100:.2f}"}]


def _cycle(trader, cash, holdings, realized=0):
    """한 주기 실행 → 텔레그램 mock. 자산은 현금 + 평가금으로 구성한다."""
    evlu = sum(int(h['evlu_amt']) for h in holdings)
    asset = {'tot_asset': cash + evlu, 'sec_eval': evlu, 'order_possible': cash}
    deposit = {'deposit': cash, 'd2_deposit': cash, 'd2_real': cash,
               'foreign_deposit': 0, 'order_possible': cash}
    summary = [{'dnca_tot_amt': str(cash), 'prvs_rcdl_excc_amt': str(cash),
                'scts_evlu_amt': str(evlu), 'tot_evlu_amt': str(cash + evlu)}]

    # 실현손익은 당일 매도 기록에서 나온다 — 필요한 만큼만 흉내낸다.
    trades = ([{'type': '매도', 'code': CODE, 'name': NAME, 'qty': 1,
                'price': 0, 'profit_amt': realized, 'profit_rate': 0.0,
                'order_status': '체결', 'odno': 'X', 'time': '2026-08-04 10:00:00'}]
              if realized else [])

    with patch('modules.auto_trade.account.get_asset_status_data', return_value=asset), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=trades), \
         patch('modules.auto_trade.db_manager.db.save_daily_asset'), \
         patch('modules.auto_trade.save_daily_initial_asset'), \
         patch('modules.auto_trade.load_daily_initial_asset', return_value=0), \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch.object(trader, '_refine_trade_records', side_effect=lambda x: x):
        trader._monitor_account_status(holdings, summary, deposit)
    return tg


def _transfer_alerts(tg):
    return [c for c in tg.call_args_list if "자동 감지" in str(c)]


# ─────────────────────────── 오탐 방지 (가장 중요) ───────────────────────────

def test_price_move_alone_is_not_a_transfer(trader):
    """시세만 움직였을 때 입출금으로 오인하면 안 된다.

    과거 initial_asset(총자산) 기준으로 비교하던 시절, 보유 종목이 하락하기만 해도
    '가짜 입금'이 잡혀 기준자산이 부풀고 비상정지가 오작동했다.
    """
    cash = 1_000_000
    _cycle(trader, cash, _holdings(price=100_000))     # 기준 확립
    before = trader.initial_asset

    # [중요] 값이 매 주기 바뀌면 '3회 연속 동일 변동' 규칙에 걸려 확정되지 않는다.
    #  그러면 기준이 원금이든 총자산이든 통과해버려 이 테스트가 아무것도 검증하지 못한다
    #  (변이 검증에서 실제로 드러났다). 현실적인 위험 시나리오는 '갭하락 후 그 가격 유지'다.
    for price in (90_000, 70_000, 120_000):            # -30% ~ +20%
        for _ in range(4):                             # 같은 시세가 여러 주기 유지된다
            tg = _cycle(trader, cash, _holdings(price=price))
            assert not _transfer_alerts(tg), f"시세 {price} 유지 중 가짜 입출금이 감지됐다"

    assert trader.initial_asset == before, "시세 변동만으로 기준 자산이 이동했다"


def test_buy_fill_is_not_a_transfer(trader):
    """매수 체결(현금 ↓ · 매입원가 ↑)은 원금을 바꾸지 않는다."""
    _cycle(trader, 2_000_000, _holdings(qty=10, buy=100_000, price=100_000))
    before = trader.initial_asset

    # 100만원어치 추가 매수 → 현금 -100만, 매입원가 +100만
    for _ in range(4):
        tg = _cycle(trader, 1_000_000, _holdings(qty=20, buy=100_000, price=100_000))
        assert not _transfer_alerts(tg), "매수 체결이 입출금으로 오인됐다"
    assert trader.initial_asset == before


def test_small_change_is_ignored(trader):
    """5만원 미만 변동은 무시한다(수수료·반올림 잡음)."""
    _cycle(trader, 1_000_000, _holdings())
    for _ in range(4):
        tg = _cycle(trader, 1_030_000, _holdings())    # +3만원
        assert not _transfer_alerts(tg)


# ─────────────────────────── 실제 입출금은 잡는가 ───────────────────────────

def test_real_deposit_is_detected_after_three_cycles(trader):
    """5만원 이상 원금 증가가 3주기 연속 유지되면 입금으로 확정한다."""
    _cycle(trader, 1_000_000, _holdings())
    before = trader.initial_asset

    alerts = []
    for _ in range(3):
        alerts.append(_transfer_alerts(_cycle(trader, 1_500_000, _holdings())))

    assert not alerts[0] and not alerts[1], "1~2회 만에 확정하면 API 지연에 오탐한다"
    assert alerts[2], "3회 연속 동일 변동인데 입금이 확정되지 않았다"
    assert trader.initial_asset == before + 500_000, "기준 자산이 입금액만큼 이동하지 않았다"


def test_withdrawal_is_detected(trader):
    """출금(원금 감소)도 같은 규칙으로 잡아 기준 자산을 낮춘다."""
    _cycle(trader, 1_500_000, _holdings())
    before = trader.initial_asset

    for _ in range(3):
        tg = _transfer_alerts(_cycle(trader, 1_000_000, _holdings()))
    assert tg, "출금이 감지되지 않았다 — 기준이 높은 채로 남아 방어 모드가 늦어진다"
    assert trader.initial_asset == before - 500_000


def test_transient_blip_does_not_confirm(trader):
    """한 주기만 튀었다가 돌아오면 확정하지 않는다(체결 중 API 지연)."""
    _cycle(trader, 1_000_000, _holdings())
    before = trader.initial_asset

    _cycle(trader, 1_500_000, _holdings())             # 1회 튐
    _cycle(trader, 1_000_000, _holdings())             # 복귀
    tg = _cycle(trader, 1_000_000, _holdings())

    assert not _transfer_alerts(tg)
    assert trader.initial_asset == before
