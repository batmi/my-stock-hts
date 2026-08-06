"""잔고 조회가 실패했을 때 매매가 멈추는가, 그리고 '빈 잔고'를 실패와 구분하는가.

계좌 상태를 모르는 채로 매매하는 것이 이 시스템에서 가장 위험한 상태다. 두 갈래로 나뉜다.

1. **조회 실패(None)** — 예외를 올려 Kill Switch(SYSTEM_MAX_CONSECUTIVE_ERRORS)에 연동돼야 한다.
   실패했는데 조용히 넘어가면 보유 종목이 없는 것처럼 보여 손절이 통째로 건너뛰어진다.

2. **빈 잔고([])** — rt_cd='0' + output1=[] 는 정상적인 '보유 없음'이지만, 토스가 items를
   비워 응답하거나 페이징이 어긋나도 똑같은 모양이 된다. 이때 _check_buy_conditions 의
   holding_codes 가 0개가 되어 매수 슬롯이 **전부** 열린다. 실제로는 보유 중인데
   SYSTEM_MAX_HOLDINGS 만큼 추가 매수가 나가면 자본 대비 리스크 한도가 그대로 깨진다.
   → 직전 주기에 보유가 있었다면 1주기 재확인 후에만 수용한다.
"""
import threading
from unittest.mock import patch, MagicMock

import pytest

import config
from modules import auto_trade


def _holding(code="005930", name="삼성전자", qty=10):
    return {'pdno': code, 'prdt_name': name, 'hldg_qty': str(qty),
            'pchs_avg_pric': "70000", 'prpr': "71000", 'evlu_amt': "710000",
            'evlu_pfls_amt': "10000", 'evlu_pfls_rt': "1.43"}


_SUMMARY = [{'dnca_tot_amt': "5000000", 'prvs_rcdl_excc_amt': "5000000",
             'scts_evlu_amt': "0", 'tot_evlu_amt': "5000000"}]


@pytest.fixture
def trader():
    """루프를 한 주기씩 돌릴 수 있는 상태의 AutoTrader."""
    t = auto_trade.AutoTrader()
    t.is_running = True
    t.thread = threading.current_thread()
    t.consecutive_errors = 0
    t.last_wait_alert_time = 0
    t._wait_alert_sent = False
    t.initial_holdings = None
    t.initial_summary = None
    t.last_holdings_count = 0

    monitor = auto_trade.ConclusionMonitor()
    saved_monitor_errors = monitor.consecutive_errors
    monitor.consecutive_errors = 0

    yield t

    t.is_running = False
    t.consecutive_errors = 0
    t.last_holdings_count = 0
    monitor.consecutive_errors = saved_monitor_errors


def _drive(trader, balance_returns, max_cycles=None):
    """balance_returns 를 한 주기에 하나씩 흘려보내며 루프를 돌린다.

    반환: (sell_mock, buy_mock, recovery_mock)
    """
    seq = list(balance_returns)
    calls = {'n': 0}

    def fake_balance(*args, **kwargs):
        i = calls['n']
        calls['n'] += 1
        if i >= len(seq) - 1:
            trader.is_running = False  # 마지막 주기 후 루프 종료
        if i >= len(seq):
            return [], _SUMMARY
        val = seq[i]
        if isinstance(val, Exception):
            raise val
        return val

    def fake_recovery():
        trader.is_running = False

    # 주기 말미 대기 루프는 time.sleep이 아니라 실제 시각(time.time)으로 간격을 재므로,
    # 간격을 0으로 두지 않으면 테스트가 SYSTEM_TRADING_INTERVAL(기본 60초) 동안 스핀한다.
    saved_interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
    config.SYSTEM_TRADING_INTERVAL = 0
    try:
        return _drive_inner(trader, fake_balance, fake_recovery)
    finally:
        config.SYSTEM_TRADING_INTERVAL = saved_interval


def _drive_inner(trader, fake_balance, fake_recovery):
    with patch.object(trader, 'is_market_open', return_value=True), \
         patch.object(trader, '_check_sell_conditions') as sell_mock, \
         patch.object(trader, '_check_buy_conditions') as buy_mock, \
         patch.object(trader, '_wait_for_server_recovery', side_effect=fake_recovery) as rec_mock, \
         patch.object(trader.order_manager, 'manage_unfilled_orders'), \
         patch.object(trader, '_monitor_account_status'), \
         patch.object(trader, '_update_risk_scale'), \
         patch('modules.auto_trade.api.get_domestic_balance', side_effect=fake_balance), \
         patch('modules.auto_trade.api.get_deposit_balance',
               return_value={'deposit': 5000000, 'foreign_deposit': 0, 'd2_deposit': 5000000}), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('time.sleep'):
        trader._run_loop()

    return sell_mock, buy_mock, rec_mock


# ─────────────────────────────── 1. 조회 실패 → Kill Switch ───────────────────────────────

def test_balance_none_stops_trading_this_cycle(trader):
    """(None, None) 이면 매도·매수 판정이 아예 돌지 않아야 한다."""
    sell_mock, buy_mock, _ = _drive(trader, [(None, None)])

    assert not sell_mock.called, "잔고를 모르는데 매도 판정이 돌았다"
    assert not buy_mock.called, "잔고를 모르는데 매수 판정이 돌았다 — 중복 매수 위험"
    assert trader.consecutive_errors >= 1, "잔고 조회 실패가 에러로 계수되지 않았다"


def test_repeated_balance_failure_enters_wait_mode(trader):
    """연속 실패가 임계에 닿으면 대기 모드(_wait_for_server_recovery)로 전환된다."""
    saved = config.SYSTEM_MAX_CONSECUTIVE_ERRORS
    config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 3
    try:
        _, _, rec_mock = _drive(trader, [(None, None)] * 3)
        assert rec_mock.called, (
            "잔고 조회가 연속 실패했는데 대기 모드로 넘어가지 않았다 — "
            "계좌 상태를 모르는 채 루프가 계속 돈다")
    finally:
        config.SYSTEM_MAX_CONSECUTIVE_ERRORS = saved


def test_success_resets_error_count(trader):
    """조회가 회복되면 누적 에러가 0으로 돌아가야 한다(오탐 누적 방지)."""
    trader.consecutive_errors = 2
    _drive(trader, [([_holding()], _SUMMARY)])
    assert trader.consecutive_errors == 0


# ─────────────────────────────── 2. 빈 잔고 = 실패와 구분 ───────────────────────────────

def test_sudden_empty_balance_defers_buy(trader):
    """보유가 있다가 갑자기 0건이 되면 그 주기의 매수를 보류한다."""
    sell_mock, buy_mock, _ = _drive(trader, [
        ([_holding()], _SUMMARY),   # 1주기: 1종목 보유
        ([], _SUMMARY),             # 2주기: 갑자기 0건
    ])

    assert buy_mock.call_count == 1, (
        "보유 1건 → 0건 직후에도 매수 판정이 돌았다. 조회 이상이라면 슬롯이 전부 열려 "
        "이미 보유 중인 계좌에 최대 종목 수만큼 추가 매수가 나간다")
    assert sell_mock.call_count == 2, "매도(청산) 경로까지 막으면 안 된다"
    assert trader.consecutive_errors == 0, "빈 잔고는 에러가 아니다 — Kill Switch를 건드리면 안 된다"


def test_empty_balance_accepted_on_second_read(trader):
    """두 번 연속 0건이면 진짜 청산으로 보고 매수를 재개한다(무기한 차단 방지)."""
    _, buy_mock, _ = _drive(trader, [
        ([_holding()], _SUMMARY),
        ([], _SUMMARY),   # 보류
        ([], _SUMMARY),   # 재확인 → 수용
    ])
    assert buy_mock.call_count == 2, (
        "재확인에서도 0건이면 실제 전량 청산이다. 계속 막으면 매매가 영구 정지된다")


def test_steady_empty_balance_never_blocks(trader):
    """처음부터 보유가 없는 계좌(신규·전량 청산 후)는 한 번도 막히지 않아야 한다."""
    _, buy_mock, _ = _drive(trader, [([], _SUMMARY)] * 3)
    assert buy_mock.call_count == 3, "보유 이력이 없는데 매수가 보류됐다 — 오탐"


def test_defers_again_after_holdings_return(trader):
    """보유가 다시 잡힌 뒤의 0건도 매번 새로 재확인돼야 한다(1회성 보호가 아니다)."""
    _, buy_mock, _ = _drive(trader, [
        ([_holding()], _SUMMARY),
        ([], _SUMMARY),             # 보류 (1회차)
        ([_holding()], _SUMMARY),   # 보유 복귀
        ([], _SUMMARY),             # 다시 0건 → 또 보류돼야 한다
    ])
    assert buy_mock.call_count == 2, (
        "첫 조회 이상에서만 보호가 걸리고 이후에는 그대로 통과했다")
