"""일일 손실 한도(계좌 차단기)가 실제로 돌고 있는가.

[종전 구조] check_loss_limit 호출은 _monitor_account_status 의 **맨 끝**에 있었고,
그 함수 전체가 265줄짜리 `try: ... except Exception: pass` 로 묶여 있었다. 그 사이에는
손익 표시·입출금 감지·문자열 포맷 같은 차단기와 무관한 코드가 가득했다.

그래서 그 중 **어느 하나라도 던지면 차단기가 조용히 건너뛰어졌다.** 로그도 안 남는다.
게다가 그런 예외는 손실이 큰 날에 더 잘 난다(다룰 값과 분기가 많아지므로) — 정확히
차단기가 필요한 날에 꺼지는 구조였다.

[고친 방향] 기준자산이 정해진 직후, 표시 코드보다 **앞에서** 돌린다. 여기서도 예외는
잡되 삼키지 않는다 — 세고, 로그에 남기고, 반복되면 알린다.
"""
import time

import pytest
from unittest.mock import MagicMock, patch

import config
from modules.auto_trade import AutoTrader
from modules.auto_trade.trader import CIRCUIT_BREAKER_ALERT_FAILS


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.initial_asset = 10_000_000
    t.circuit_breaker_fails = 0
    t.circuit_breaker_ran_at = 0.0
    t.buy_halted = False
    t.buy_halt_reason = ""
    t.buy_halt_date = ""
    yield t


# ───────────────────────── 발동 자체 ─────────────────────────

def test_breach_halts_new_buys(trader):
    """[핵심] 한도를 넘으면 신규 매수가 멈춰야 한다."""
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.engine.get_mystock_log_tail', return_value=""):
        trader._run_account_circuit_breaker(8_900_000)      # -11%
    assert trader.buy_halted is True, "한도를 넘었는데 매수가 계속된다"


def test_within_limit_does_not_halt(trader):
    """대조군 — 한도 안이면 멈추면 안 된다(상시 차단이면 매매가 죽는다)."""
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._run_account_circuit_breaker(9_200_000)      # -8%
    assert trader.buy_halted is False


def test_halt_stops_buys_but_not_sells(trader):
    """방어 모드는 신규 진입만 막는다 — 청산 감시까지 멈추면 무방비가 된다."""
    trader.buy_halted = True
    trader.buy_halt_reason = "일일 손실 한도 초과"
    with patch.object(trader, '_analyze_candidates') as analyze, \
         patch.object(config.session, 'stock_data',
                      {'stocks_kr': [{'code': '005930', 'name': '삼성전자'}]}), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[]):
        trader._check_buy_conditions([], {'d2_deposit': 10_000_000, 'deposit': 10_000_000})
    assert not analyze.called, "방어 모드인데 신규 매수를 검토했다"

    # 매도 경로는 buy_halted 를 보지 않아야 한다.
    import inspect
    src = inspect.getsource(trader._check_sell_conditions)
    assert "buy_halted" not in src, "매도 검사가 방어 모드에 걸린다 — 손절이 멈춘다"


# ─────────────── 표시 코드가 차단기를 덮지 않는가 ───────────────

def test_display_failure_cannot_suppress_the_breaker(trader):
    """[핵심 회귀] 표시·로깅이 던져도 차단기는 이미 돌아 있어야 한다.

    종전에는 차단기가 표시 코드 **뒤에** 있어서, 표시가 던지면 통째로 건너뛰어졌다.
    호출 순서를 못박는다 — 순서가 뒤집히면 이 테스트가 깨진다.
    """
    # 손실 한도 블록은 '보유 종목 있음' 분기 안에 있으므로 보유분을 채워야 도달한다.
    holdings = [{'pdno': '005930', 'prdt_name': '삼성전자', 'hldg_qty': '10',
                 'pchs_avg_pric': '100000', 'prpr': '89000',
                 'evlu_pfls_amt': '-110000', 'evlu_pfls_rt': '-11.0',
                 'evlu_amt': '890000', 'pchs_amt': '1000000'}]
    summary = [{'dnca_tot_amt': '8010000', 'prvs_rcdl_excc_amt': '8010000',
                'tot_evlu_amt': '8900000', 'scts_evlu_amt': '890000',
                'evlu_pfls_smtl_amt': '-110000'}]
    deposit = {'d2_deposit': 8_010_000, 'deposit': 8_010_000}
    blew_up = []

    def _log(msg, *a, **k):
        # 차단기 **뒤에** 오는 표시 코드에서 터뜨린다.
        if "증권 자산 현황" in str(msg):
            blew_up.append(True)
            raise RuntimeError("표시 단계 실패")

    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.account.get_asset_status_data',
               return_value={'tot_asset': 8_900_000}), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.engine.get_mystock_log_tail', return_value=""), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=[]), \
         patch.object(trader, 'log', side_effect=_log):
        trader._monitor_account_status(holdings, summary, deposit)     # -11%

    assert blew_up, "전제가 깨졌다 — 표시 코드가 실행되지 않아 이 테스트는 아무것도 검증하지 못한다"
    assert trader.buy_halted is True, \
        "표시 코드가 던지자 차단기가 통째로 건너뛰어졌다 (차단기가 표시 뒤에 있다)"


def test_breaker_failure_is_counted_and_logged(trader, caplog):
    """차단기 자신이 실패하면 조용히 넘기지 않는다."""
    with patch.object(trader.risk_manager, 'check_loss_limit',
                      side_effect=RuntimeError("boom")), \
         caplog.at_level("ERROR", logger="modules.auto_trade.trader"):
        trader._run_account_circuit_breaker(9_000_000)
    assert trader.circuit_breaker_fails == 1
    assert any("차단기" in r.message for r in caplog.records), "로그 파일에 안 남는다"


def test_repeated_breaker_failure_alerts_once(trader):
    """연속 실패는 '차단기가 꺼져 있다'는 뜻이라 알려야 한다(도배는 하지 않는다)."""
    with patch.object(trader.risk_manager, 'check_loss_limit',
                      side_effect=RuntimeError("boom")), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        for _ in range(CIRCUIT_BREAKER_ALERT_FAILS + 4):
            trader._run_account_circuit_breaker(9_000_000)
    assert tg.call_count == 1, f"연속 실패로 {tg.call_count}건 알렸다"
    assert "차단기" in str(tg.call_args)


def test_success_clears_the_failure_streak(trader):
    """복구되면 연속 실패는 0으로 — 안 그러면 경보가 상시가 된다."""
    with patch.object(trader.risk_manager, 'check_loss_limit', side_effect=RuntimeError("x")), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._run_account_circuit_breaker(9_000_000)
    assert trader.circuit_breaker_fails == 1
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0):
        trader._run_account_circuit_breaker(9_800_000)
    assert trader.circuit_breaker_fails == 0
    assert trader.circuit_breaker_ran_at > 0, "정상 수행 시각이 안 남는다"


# ───────────────────────── 기준자산 ─────────────────────────

def test_absurd_asset_drop_does_not_move_the_baseline(trader):
    """반토막 이하는 API 결손 의심이라 기준 평가자산에 반영하지 않는다.

    반영하면 히트 캡·드로다운 배수가 허깨비 값으로 조여져 매매가 통째로 막힌다.
    """
    trader.current_total_asset = 10_000_000
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._run_account_circuit_breaker(1_000_000)      # -90% = 결손 의심
    assert trader.current_total_asset == 10_000_000, "결손 의심 값이 기준자산에 들어갔다"
    assert trader.buy_halted is False, "결손 의심 값으로 방어 모드를 켰다"


def test_normal_drop_does_update_the_baseline(trader):
    """대조군 — 정상 범위 하락은 반영해야 한다(안 하면 캡이 낡은 값으로 논다)."""
    trader.current_total_asset = 10_000_000
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.engine.get_mystock_log_tail', return_value=""):
        trader._run_account_circuit_breaker(8_800_000)      # -12%
    assert trader.current_total_asset == 8_800_000


def test_limit_disabled_is_respected(trader):
    """한도를 0으로 끄면 발동하지 않는다."""
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._run_account_circuit_breaker(1_000)
    assert trader.buy_halted is False


# ───────────────────────── 상태 표시 ─────────────────────────

def test_health_panel_exposes_a_dead_breaker(trader):
    """[배선] 차단기가 안 도는 것을 아무도 모르는 상태가 가장 나쁘다."""
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0):
        assert "연속 실패" not in trader._health_circuit_breaker_text()
        trader.circuit_breaker_fails = 4
        text = trader._health_circuit_breaker_text()
    assert "연속 실패 4회" in text and "감시되지 않는다" in text, f"상태창이 침묵한다: {text}"


def test_health_panel_shows_active_defense_mode(trader):
    trader.buy_halted = True
    trader.buy_halt_reason = "일일 손실 한도 초과 (-11.2%)"
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0):
        text = trader._health_circuit_breaker_text()
    assert "방어 모드 작동 중" in text
