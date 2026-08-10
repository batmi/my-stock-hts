"""정지할 때 '무엇이 함께 꺼지는지' 알리는가.

[왜 이 테스트인가] 정지는 매도 감시 루프까지 함께 끈다. 이 코드베이스는 같은 이유로
일일 손실 한도 초과와 Kill Switch를 '정지'에서 '방어 모드'로 바꿨다 —
engine.check_loss_limit 주석: "정지는 포지션을 청산하지 않고 매도 감시 루프까지 함께 끄기
때문에 무방비 상태가 된다."

그런데 명시적 정지(메뉴·텔레그램 /stop)는 그대로 전부 끄면서 종료 알림에 자산만 담고
그 사실을 말하지 않았다. 텔레그램은 한 단어로 실행된다. 정지 자체를 막을 일은 아니지만,
무엇을 껐는지는 알려야 한다.
"""
import inspect

from modules.auto_trade import AutoTrader
from modules.telegram_bot import TelegramCommander


def test_stop_message_names_the_unprotected_positions():
    src = inspect.getsource(AutoTrader.stop)
    assert "손절·트레일링 감시가 함께 멈춥니다" in src, "종료 알림이 무방비 사실을 알리지 않는다"
    assert "청산은 되지 않았습니다" in src, "청산된 것으로 오해할 수 있다"


def test_stop_message_lists_codes_but_caps_the_list():
    """종목을 알려야 조치가 되고, 많을 때 알림이 잘리지 않아야 한다."""
    src = inspect.getsource(AutoTrader.stop)
    assert "unmanaged[:10]" in src
    assert "외 " in src


def test_unmanaged_list_only_counts_real_positions():
    """수량 0인 줄까지 세면 없는 위험을 알리게 된다."""
    src = inspect.getsource(AutoTrader.stop)
    head = src.split("unmanaged = [")[1][:200]
    assert "valid_holdings" in head, "hldg_qty > 0 로 거른 목록을 써야 한다"


def test_telegram_stop_reply_warns_too():
    """/stop 응답 자체에서 바로 보여야 한다 — 별도 알림을 못 볼 수도 있다."""
    src = inspect.getsource(TelegramCommander._cmd_stop)
    assert "손절·트레일링 감시도 함께 멈춥니다" in src


def test_telegram_stop_still_stops():
    """경고를 붙이느라 정지 자체가 막히면 안 된다."""
    src = inspect.getsource(TelegramCommander._cmd_stop)
    assert "self.trader.stop(use_status=False)" in src
    assert src.index("self.trader.stop(") < src.index("reply +=") if "reply +=" in src else True
