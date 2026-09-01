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


def _cycle(trader, cash, holdings, realized=0, external_sell=False):
    """한 주기 실행 → 텔레그램 mock. 자산은 현금 + 평가금으로 구성한다.

    external_sell: 운용자가 HTS/MTS로 직접 판 체결(우리 주문 기록이 없어 실현손익이 0으로
      남는 행)을 섞는다. 그 0을 실현손익으로 세면 가짜 입출금이 된다.
    """
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
    if external_sell:
        trades.append({'type': '매도(외부)', 'code': CODE, 'name': NAME, 'qty': 1,
                       'price': 0, 'profit_amt': 0, 'profit_rate': 0.0,
                       'order_status': '체결', 'odno': 'EXT', 'time': '2026-08-04 11:00:00'})

    with patch('modules.auto_trade.account.get_asset_status_data', return_value=asset), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=trades), \
         patch('modules.auto_trade.db_manager.db.save_daily_asset') as save_asset, \
         patch('modules.auto_trade.db_manager.db.shift_daily_assets') as shift, \
         patch('modules.auto_trade.save_daily_initial_asset'), \
         patch('modules.auto_trade.load_daily_initial_asset', return_value=0), \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch.object(trader, '_refine_trade_records', side_effect=lambda x: x):
        trader._monitor_account_status(holdings, summary, deposit)
    # 기준선 이동은 DB를 만지므로 반드시 막는다(막지 않으면 테스트가 실계좌 이력을 옮긴다).
    tg.shift_daily_assets = shift
    tg.save_daily_asset = save_asset
    return tg


def _transfer_alerts(tg):
    return [c for c in tg.call_args_list if "자동 감지" in str(c)]


def _net_transfer_writes(*mocks):
    """여러 주기에 걸쳐 기록된 '그날 순입출금' 쓰기를 모은다.

    기록은 값이 **바뀐 주기에만** 일어난다(매 주기 쓰면 파이3에 부담이고 의미도 없다).
    그래서 마지막 주기의 mock만 보면 아무것도 없다.
    """
    out = []
    for m in mocks:
        out += [c for c in m.save_daily_asset.call_args_list
                if c.kwargs.get('net_transfer') is not None]
    return out


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
    """5만원 이상 원금 증가가 3주기 연속 유지되면 입금으로 확정해 **알린다**.

    [설계] 확정돼도 기준 자산(initial_asset)은 옮기지 않는다. 일일 손실 한도와 사이징은
    net_transfer_today 를 빼고 계산하고(effective_baseline), 드로다운은 그날 행의
    net_transfer 로 환산하므로(get_max_daily_asset) 옮길 상태가 없다.
    옮기는 쪽은 되돌릴 수 없고, 추정이 틀리면 잘못된 기준이 그대로 굳는다.
    """
    _cycle(trader, 1_000_000, _holdings())
    before = trader.initial_asset

    alerts = []
    for _ in range(3):
        alerts.append(_transfer_alerts(_cycle(trader, 1_500_000, _holdings())))

    assert not alerts[0] and not alerts[1], "1~2회 만에 확정하면 API 지연에 오탐한다"
    assert alerts[2], "3회 연속 동일 변동인데 입금이 확정되지 않았다"
    assert trader.initial_asset == before, "기준 자산을 옮겼다 — 파생 보정이면 옮길 필요가 없다"
    assert trader.net_transfer_today == 500_000
    assert trader.effective_baseline() == before + 500_000, "유효 기준선이 입금을 반영하지 않았다"


def test_withdrawal_is_detected(trader):
    """출금도 같은 규칙으로 잡아 유효 기준선을 낮춘다."""
    _cycle(trader, 1_500_000, _holdings())
    before = trader.initial_asset

    for _ in range(3):
        tg = _transfer_alerts(_cycle(trader, 1_000_000, _holdings()))
    assert tg, "출금이 감지되지 않았다"
    assert trader.net_transfer_today == -500_000
    assert trader.effective_baseline() == before - 500_000


def test_the_transfer_is_recorded_on_todays_asset_row(trader):
    """[핵심] 그날 순입출금을 자산 이력에 남긴다 — 내일부터의 드로다운 기준이 여기서 나온다.

    [왜 이력을 옮기지 않는가] 종전에는 daily_asset_history 를 통째로 평행이동했다. 그 방식은
     되돌릴 수 없고, 추정이 틀리면 고점이 낮아져 드로다운을 **과소**평가한다 = 리스크 한도가
     조용히 열린다. 원본을 두고 환산만 하면 잘못된 값이 굳지 않고, 그날 항목만 다시 쓰면
     저절로 복구된다.
     (2026-08-23 가상계좌: 자산 이력 한 줄이 드로다운 50%를 만들어 히트 캡을 석 달간 묶었다.)
    """
    _cycle(trader, 1_000_000, _holdings())
    mocks = [_cycle(trader, 1_500_000, _holdings()) for _ in range(3)]

    assert _transfer_alerts(mocks[-1]), "이 표본은 입금이 확정된 상태여야 한다"
    calls = _net_transfer_writes(*mocks)
    assert calls, "그날 순입출금이 자산 이력에 기록되지 않았다"
    assert calls[-1].kwargs['net_transfer'] == 500_000
    for m in mocks:
        m.shift_daily_assets.assert_not_called()   # 이력을 옮기면 되돌릴 수 없다


def test_the_alert_says_no_action_is_needed(trader):
    """운용자가 할 일이 없다는 것을 알려야 한다 — 모르면 멀쩡한 시스템을 세운다."""
    _cycle(trader, 1_000_000, _holdings())
    for _ in range(3):
        tg = _cycle(trader, 1_500_000, _holdings())
    body = str(_transfer_alerts(tg)[0])
    assert "자동" in body and "조치할 것은 없" in body


def test_the_detection_alert_does_not_repeat_all_day(trader):
    """같은 입출금으로 알림이 되풀이되면 진짜 경보가 묻힌다."""
    _cycle(trader, 1_000_000, _holdings())
    total = 0
    for _ in range(12):
        total += len(_transfer_alerts(_cycle(trader, 1_500_000, _holdings())))
    assert total == 1, f"감지 알림이 {total}번 나갔다(도배)"


def test_a_large_transfer_is_handled_like_any_other(trader):
    """[상한 폐지] 큰 입출금도 사람 손을 기다리지 않는다.

    종전에는 기준자산의 30%를 넘으면 반영하지 않고 사람에게 넘겼다 — 기준선을 **옮기는**
    방식이라 오탐 한 번이 차단기와 사이징을 동시에 틀어 놓았기 때문이다. 그 미반영분이
    90일짜리 가짜 드로다운으로 남는 것이 실제 문제였다. 이제 옮기지 않으므로 상한이 없다.
    """
    _cycle(trader, 4_000_000, _holdings())
    before = trader.initial_asset
    mocks = [_cycle(trader, 100_000, _holdings()) for _ in range(3)]  # 390만 출금(기준의 78%)
    assert _transfer_alerts(mocks[-1]), "큰 출금이 감지되지 않았다"
    assert trader.net_transfer_today == -3_900_000
    assert trader.effective_baseline() == before - 3_900_000
    calls = _net_transfer_writes(*mocks)
    assert calls and calls[-1].kwargs['net_transfer'] == -3_900_000


def test_transient_blip_does_not_confirm(trader):
    """한 주기만 튀었다가 돌아오면 확정하지 않는다(체결 중 API 지연)."""
    _cycle(trader, 1_000_000, _holdings())
    before = trader.initial_asset

    _cycle(trader, 1_500_000, _holdings())             # 1회 튐
    _cycle(trader, 1_000_000, _holdings())             # 복귀
    tg = _cycle(trader, 1_000_000, _holdings())

    assert not _transfer_alerts(tg)
    assert trader.initial_asset == before




# ───────────────── 수동(외부) 매매가 입출금으로 둔갑하지 않는가 ─────────────────

def test_an_external_sell_is_not_a_deposit(trader):
    """[핵심] 운용자가 HTS/MTS로 직접 판 이익이 '입금'으로 잡히면 안 된다.

    외부 체결은 우리 주문 기록(odno)이 없어 실현손익이 0으로 저장된다
    (conclusion._recalc_realized 는 origin_trade 가 있어야 손익을 채운다).
    그 0을 그대로 실현손익으로 세면 원금이 이익만큼 늘어 보이고, 3주기 뒤
    **이익 전액이 입금으로 확정된다** — 기준 자산이 부풀어 차단기가 늦어지고,
    자산 이력에도 그 금액이 순입출금으로 남는다.
    """
    _cycle(trader, 1_000_000, _holdings(qty=10))            # 기준 확립
    # 10주 중 5주를 외부에서 팔아 60만원이 들어왔다(원가 50만 + 이익 10만)
    for _ in range(4):
        tg = _cycle(trader, 1_600_000, _holdings(qty=5), external_sell=True)
        assert not _transfer_alerts(tg), "외부 매도 이익이 입금으로 잡혔다"
    assert trader.net_transfer_today == 0, "모르는 실현손익이 순입출금으로 남았다"


def test_a_known_sell_still_measures_transfers(trader):
    """[반대 방향] 실현손익을 아는 매도는 감지를 막지 않는다 — 게이트가 과하면 안 된다."""
    _cycle(trader, 1_000_000, _holdings(qty=10))
    alerts = []
    for _ in range(4):     # 확정에 3주기가 필요하고, 알림은 하루 1회라 전 주기를 모은다
        alerts += _transfer_alerts(_cycle(trader, 2_600_000, _holdings(qty=5),
                                          realized=100_000))
    assert alerts, "정상 매도까지 감지를 막았다"
    assert trader.net_transfer_today == 1_000_000
