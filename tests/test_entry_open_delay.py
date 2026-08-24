"""개장 직후 진입 보류 게이트 — 신규 매수만 막고 청산은 건드리지 않는가.

거래 시작 시간(SYSTEM_TRADING_START_TIME)을 늦춰 같은 효과를 내려 하면
is_system_market_open()이 False가 되어 _run_loop이 분석 사이클을 통째로 건너뛴다
= 손절·트레일링까지 멈춘다. 이 게이트는 그 함정을 피하려고 만든 것이므로,
'청산 경로가 이 게이트를 보지 않는다'는 것 자체가 회귀 대상이다.
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules.auto_trade import common  # noqa: E402


@pytest.fixture
def dials():
    saved = (config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE,
             config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES,
             config.settings.SYSTEM_TRADING_START_TIME)
    yield
    (config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE,
     config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES,
     config.settings.SYSTEM_TRADING_START_TIME) = saved


def at(hh, mm, ss=0):
    return datetime(2026, 8, 21, hh, mm, ss)


def test_보류_구간과_해제_시점(dials):
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE = True
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES = 30
    config.settings.SYSTEM_TRADING_START_TIME = "0900"

    # 오늘 사고가 난 시각(09:00:50)은 보류 대상이어야 한다.
    assert common.entry_open_delay_remaining(at(9, 0, 50)) == 29 * 60 + 10
    assert common.entry_open_delay_remaining(at(9, 29, 59)) > 0
    # 경계: 09:30 정각부터는 통과한다.
    assert common.entry_open_delay_remaining(at(9, 30)) == 0
    assert common.entry_open_delay_remaining(at(10, 27)) == 0


def test_끄거나_0분이면_무동작(dials):
    config.settings.SYSTEM_TRADING_START_TIME = "0900"
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES = 30
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE = False
    assert common.entry_open_delay_remaining(at(9, 10)) == 0

    config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE = True
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES = 0
    assert common.entry_open_delay_remaining(at(9, 10)) == 0


def test_프리마켓_설정이어도_KRX_개장_기준(dials):
    """START_TIME을 0800으로 넓혀도 막는 구간은 09:00~09:30이다.

    감사(tools/audit_time_of_day.py)가 잰 것이 KRX 정규장 첫 30분이라,
    08:00~08:30을 막으면 근거 없는 차단이 된다.
    """
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE = True
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES = 30
    config.settings.SYSTEM_TRADING_START_TIME = "0800"

    assert common.entry_open_delay_remaining(at(8, 15)) == 0     # 프리마켓은 대상 아님
    assert common.entry_open_delay_remaining(at(9, 10)) > 0      # KRX 개장 직후는 보류
    assert common.entry_open_delay_remaining(at(9, 30)) == 0


def test_시작시간이_더_늦으면_그_시각_기준(dials):
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE = True
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES = 30
    config.settings.SYSTEM_TRADING_START_TIME = "1000"

    assert common.entry_open_delay_remaining(at(9, 10)) == 0
    assert common.entry_open_delay_remaining(at(10, 10)) > 0
    assert common.entry_open_delay_remaining(at(10, 30)) == 0


def test_청산_경로는_이_게이트를_보지_않는다():
    """게이트가 매수 경로에만 걸려 있는지 소스로 고정한다."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "modules", "auto_trade", "trader.py"), encoding="utf-8").read()
    buy_at = src.index("def _check_buy_conditions")
    sell_at = src.index("def _check_sell_conditions")

    calls = [i for i in range(len(src))
             if src.startswith("entry_open_delay_remaining(", i)]
    assert calls, "매수 경로에 게이트 호출이 없다"

    # 매도 함수 본문(다음 def 전까지) 안에서는 호출되면 안 된다.
    sell_end = src.index("\n    def ", sell_at + 10)
    assert not any(sell_at < i < sell_end for i in calls), \
        "청산 경로가 진입 보류 게이트를 보고 있다 — 손절이 함께 멈춘다"


def test_시장시간_설정은_건드리지_않았다():
    """START_TIME으로 지연을 구현하지 않았음을 고정한다."""
    assert config.settings.SYSTEM_TRADING_START_TIME == "0900"
    assert config.settings.SYSTEM_TRADING_END_TIME == "1530"


def test_설정_저장에_포함된다(monkeypatch, dials):
    """메뉴 0에서 바꾼 값이 dynamic_config.json에 남아 재기동 후에도 유지되는가."""
    import jsonio
    from modules import settings as settings_mod

    captured = {}
    monkeypatch.setattr(jsonio, "save_json",
                        lambda path, data: captured.update(data) or True)
    monkeypatch.setattr(settings_mod, "check_and_update_active_preset",
                        lambda *a, **k: None, raising=False)

    config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE = False
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES = 45
    settings_mod._save_dynamic_config()

    assert captured["SYSTEM_ENTRY_OPEN_DELAY_USE"] is False
    assert captured["SYSTEM_ENTRY_OPEN_DELAY_MINUTES"] == 45


def test_보류_시간_입력_범위(dials):
    """음수·과도한 값이 메뉴에서 들어오지 못하게 범위가 등록돼 있는가."""
    from modules import settings as settings_mod
    lo, hi, _ = settings_mod._RANGE_RULES["SYSTEM_ENTRY_OPEN_DELAY_MINUTES"]
    assert (lo, hi) == (0, 120)


def test_보류_시간은_메뉴에서_잠긴다():
    """30분은 실측으로 정한 값이라 ON/OFF만 노출하고 시간은 숨긴다.

    정의(_RANGE_RULES·저장 대상)는 남겨 둔다 — dynamic_config.json 직접 편집 경로의
    타입·검증 근거이자, 잠금이 '기능 제거'가 아니라 '메뉴 노출 차단'임을 분명히 한다.
    """
    from modules import settings as settings_mod

    names = [it["name"] for it in settings_mod._trading_cycle_items()]
    assert "SYSTEM_ENTRY_OPEN_DELAY_USE" in names, "ON/OFF는 운영자가 바꿀 수 있어야 한다"
    assert "SYSTEM_ENTRY_OPEN_DELAY_MINUTES" not in names, "보류 시간이 메뉴에 노출됐다"
    assert "SYSTEM_ENTRY_OPEN_DELAY_MINUTES" in settings_mod.BACKTESTED_HIDDEN_KEYS


def test_ON_OFF_안내에_보류_시간이_적힌다():
    """시간을 숨긴 대신, 몇 분 보류되는지는 안내에서 읽을 수 있어야 한다."""
    from modules import settings as settings_mod

    item = next(it for it in settings_mod._trading_cycle_items()
                if it["name"] == "SYSTEM_ENTRY_OPEN_DELAY_USE")
    assert "30분" in item["help"]


def test_보류중이면_매수_경로가_실제로_멈춘다(monkeypatch):
    """게이트가 켜져 있으면 `_check_buy_conditions` 가 곧바로 돌아온다 — **런타임 확인**.

    [왜 필요한가] 배치는 위의 소스 검사 테스트가 지키지만, 그것은 '호출이 그 자리에 있다'
    까지다. 게이트가 실제로 매수를 멈추는지는 아무도 런타임으로 보고 있지 않았다.
    게다가 2026-08-24부터 tests/conftest.py 가 스위트 전역에서 이 게이트를 0으로 끈다
    (벽시계 09:00~09:30에 매수 경로 테스트가 실패하기 때문). 그 격리가 **동작 자체의
    유일한 런타임 근거까지 지워 버리지 않도록** 여기서 명시적으로 되살려 확인한다.
    """
    from unittest.mock import patch
    from modules.auto_trade import trader as trader_mod

    trader = trader_mod.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}

    monkeypatch.setattr(trader_mod, "entry_open_delay_remaining", lambda *a, **k: 12 * 60 + 5)
    monkeypatch.setattr(trader_mod, "is_system_market_open", lambda *a, **k: True)

    with patch.object(trader, 'log') as mock_log:
        # 예수금 부족 상황을 주고도 그 로그에 도달하지 못해야 한다 — 그 전에 돌아온다.
        trader._check_buy_conditions([], {'d2_deposit': 500})

    logged = " ".join(str(c) for c in mock_log.call_args_list)
    assert "예수금 부족" not in logged, "보류 중인데 매수 판정까지 내려갔다"
    assert "보류" in logged, f"보류 사유를 남기지 않는다: {logged}"


def test_보류가_풀리면_매수_판정으로_내려간다(monkeypatch):
    """대조군 — 위 테스트가 '무조건 아무것도 안 한다'로 통과하지 않게 한다."""
    from unittest.mock import patch
    from modules.auto_trade import trader as trader_mod

    trader = trader_mod.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}

    monkeypatch.setattr(trader_mod, "entry_open_delay_remaining", lambda *a, **k: 0)
    monkeypatch.setattr(trader_mod, "is_system_market_open", lambda *a, **k: True)

    with patch.object(trader, 'log') as mock_log:
        trader._check_buy_conditions([], {'d2_deposit': 500})

    logged = " ".join(str(c) for c in mock_log.call_args_list)
    assert "예수금 부족" in logged, f"보류가 없는데도 매수 판정에 못 갔다: {logged}"
