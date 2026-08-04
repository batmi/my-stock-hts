"""킬스위치가 매도 감시까지 멈추지 않는가.

[종전 구조] 연속 에러가 한도(기본 5)에 닿으면 _wait_for_server_recovery()가 루프 전체를
1분 주기로 붙잡았다. 그 동안 매도 검사가 돌지 않아 손절·트레일링이 무감시가 된다.

서버가 정말 죽었다면 그건 옳다 — 주문 자체가 나갈 수 없으니 붙잡고 있어도 잃을 게 없다.
문제는 consecutive_errors 가 **API 오류만이 아니라 루프의 모든 예외**에 오른다는 점이다.
지표 계산 버그 하나로 5회가 쌓이면, 증권사 서버가 멀쩡한데도 대기에 들어가 손절만 꺼진다.

일일 손실 한도에서 이미 같은 결함을 고쳤다(engine.check_loss_limit 주석:
"정지는 매도 감시까지 꺼서 무방비 상태를 만든다"). 킬스위치에는 그 수정이 안 들어갔다.

[고친 방향] 대기에 들어가기 전에 서버 상태를 먼저 확인한다. 서버가 정상이면 대기하지
않고 다음 주기로 넘어간다 — 버그가 계속되면 다시 돌아오지만 그 사이 매도 검사는 돈다.
"""
import time

import pytest
from unittest.mock import patch

import config
from modules.auto_trade import AutoTrader
from modules.auto_trade.trader import CODE_ERROR_ALERT_COOLDOWN


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.code_error_streaks = 0
    t._code_error_alerted_at = 0.0
    yield t


# ─────────────── 서버 정상 = 대기하지 않는다 ───────────────

def test_healthy_server_means_no_blocking_wait(trader):
    """[핵심] 서버가 정상이면 대기하지 않는다 — 매도 감시를 멈출 이유가 없다."""
    with patch('modules.auto_trade.api.check_server_health', return_value=True), \
         patch('modules.auto_trade.api.send_telegram_message'):
        assert trader._errors_are_not_the_server("ZeroDivisionError") is True
    assert trader.code_error_streaks == 1


def test_dead_server_still_waits(trader):
    """대조군 — 서버가 죽었으면 대기가 옳다. 주문이 나갈 수 없으니 붙잡아도 잃을 게 없다."""
    with patch('modules.auto_trade.api.check_server_health', return_value=False), \
         patch('modules.auto_trade.api.send_telegram_message'):
        assert trader._errors_are_not_the_server("timeout") is False
    assert trader.code_error_streaks == 0, "서버 장애를 코드 오류로 셌다"


def test_health_check_failure_is_treated_as_a_server_problem(trader):
    """상태 확인조차 안 되면 서버 문제로 본다(보수적) — 모르면 대기한다."""
    with patch('modules.auto_trade.api.check_server_health',
               side_effect=OSError("network")), \
         patch('modules.auto_trade.api.send_telegram_message'):
        assert trader._errors_are_not_the_server("boom") is False


def test_code_error_is_logged_to_the_file(trader, caplog):
    """대기로 숨기지 않는 대신 반드시 드러나야 한다."""
    with patch('modules.auto_trade.api.check_server_health', return_value=True), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         caplog.at_level("ERROR", logger="modules.auto_trade.trader"):
        trader._errors_are_not_the_server("IndexError: list index out of range")
    assert any("킬스위치" in r.message for r in caplog.records), "로그 파일에 안 남는다"


def test_code_error_alert_is_rate_limited(trader):
    """이 상태는 대기로 숨겨지지 않아 매 주기 반복된다 — 억제 없으면 도배된다."""
    with patch('modules.auto_trade.api.check_server_health', return_value=True), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        for _ in range(6):
            trader._errors_are_not_the_server("boom")
    assert tg.call_count == 1, f"같은 원인으로 {tg.call_count}건 알렸다"
    assert "매도" in str(tg.call_args), "매도 감시가 유지된다는 사실을 알리지 않는다"


def test_alert_returns_after_the_cooldown(trader):
    """쿨다운이 지나면 다시 알려야 한다 — 영구 침묵이면 안 된다."""
    with patch('modules.auto_trade.api.check_server_health', return_value=True), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        trader._errors_are_not_the_server("boom")
        trader._code_error_alerted_at = time.time() - CODE_ERROR_ALERT_COOLDOWN - 1
        trader._errors_are_not_the_server("boom")
    assert tg.call_count == 2


# ─────────────── 루프 배선 ───────────────

def test_loop_skips_the_wait_when_the_server_is_healthy(trader):
    """[배선] 판정 함수만 있고 루프가 안 쓰면 아무 소용이 없다.

    소스에서 호출 순서를 확인한다 — _wait_for_server_recovery 앞에 판정이 있어야 한다.
    """
    import inspect
    src = inspect.getsource(trader._run_loop)
    assert "_errors_are_not_the_server" in src, "루프가 서버 판정을 하지 않는다"
    assert src.index("_errors_are_not_the_server") < src.index("self._wait_for_server_recovery()"), \
        "판정이 대기 뒤에 있다 — 이미 붙잡힌 다음이라 의미가 없다"


def test_health_panel_exposes_code_errors(trader):
    """[배선] 서버가 멀쩡한데 루프가 터지는 상태는 운영자가 알아야 고칠 수 있다."""
    base = trader.get_health_message()
    assert "루프 오류" not in base
    trader.code_error_streaks = 3
    assert "루프 오류 3회" in trader.get_health_message()


def test_sell_path_does_not_depend_on_the_error_counter(trader):
    """매도 검사가 연속 에러 수를 보고 스스로 멈추면 안 된다."""
    import inspect
    src = inspect.getsource(trader._check_sell_conditions)
    assert "consecutive_errors" not in src, "매도 검사가 에러 카운터에 걸린다"
