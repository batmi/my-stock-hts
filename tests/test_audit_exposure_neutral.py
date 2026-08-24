"""노출 중립 대조 도구의 **되돌리기와 인자 해석**이 정확한가.

[왜 이 둘인가] 이 도구는 config 를 실행 중에 갈아 끼운다. 되돌리기가 어긋나면 그
 오염이 같은 프로세스의 뒤 실행에 조용히 남아, 결과가 틀렸다는 사실조차 드러나지
 않는다(계측기 오염은 이 저장소가 이미 두 번 겪은 실패 유형이다). 그리고 --set 해석이
 어긋나면 '25' 대신 '"25"' 를 넣고도 표는 정상으로 보인다 — 아무것도 안 바뀐 팔을
 다른 팔로 착각하는 형태다.

 시뮬레이션 자체(수 분 걸린다)는 여기서 돌리지 않는다. 도구의 판정 로직은 감사 실행으로
 확인하고, 테스트는 **조용히 틀릴 수 있는 부분**만 못박는다.
"""
import argparse

import pytest

import config
from tools import audit_exposure_neutral as EN


def test_parse_set_reads_python_literals():
    assert EN.parse_set("SELL_STRATEGY.TIME_STOP_DAYS=25") == ("SELL_STRATEGY", "TIME_STOP_DAYS", 25)
    assert EN.parse_set("SELL_STRATEGY.TIME_STOP_USE=False")[2] is False
    assert EN.parse_set("SELL_STRATEGY.TRAILING_ATR_MULTIPLIER=3.5")[2] == 3.5
    assert EN.parse_set("SETTINGS.SYSTEM_MAX_HOLDINGS=3") == ("SETTINGS", "SYSTEM_MAX_HOLDINGS", 3)


def test_parse_set_keeps_bare_strings():
    """따옴표 없는 문자열도 받는다 — 셸에서 따옴표를 겹쳐 쓰지 않게."""
    assert EN.parse_set("SCORING_WEIGHTS.MODE=aggressive")[2] == "aggressive"


@pytest.mark.parametrize("bad", ["TIME_STOP_DAYS=25",          # 그룹 없음
                                 "SELL_STRATEGY.TIME_STOP_DAYS",  # 값 없음
                                 "NOPE.KEY=1"])                # 모르는 그룹
def test_parse_set_rejects_malformed(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        EN.parse_set(bad)


def test_overrides_restore_existing_values():
    key = "TIME_STOP_DAYS"
    before = config.SELL_STRATEGY[key]
    prev, missing = EN.apply_overrides([("SELL_STRATEGY", key, before + 10)])
    assert config.SELL_STRATEGY[key] == before + 10
    EN.undo_overrides(prev, missing)
    assert config.SELL_STRATEGY[key] == before


def test_overrides_remove_keys_that_did_not_exist():
    """없던 키는 **지워서** 되돌린다 — None 으로 남기면 다음 실행이 조용히 달라진다."""
    key = "__감사_임시_키__"
    assert key not in config.SELL_STRATEGY
    prev, missing = EN.apply_overrides([("SELL_STRATEGY", key, 7)])
    assert config.SELL_STRATEGY[key] == 7
    EN.undo_overrides(prev, missing)
    assert key not in config.SELL_STRATEGY


def test_overrides_restore_a_stored_none():
    """원래 값이 None 인 키와 '없던 키'는 구분돼야 한다."""
    key = "__감사_임시_None__"
    config.SELL_STRATEGY[key] = None
    try:
        prev, missing = EN.apply_overrides([("SELL_STRATEGY", key, 5)])
        EN.undo_overrides(prev, missing)
        assert key in config.SELL_STRATEGY and config.SELL_STRATEGY[key] is None
    finally:
        config.SELL_STRATEGY.pop(key, None)


def test_overrides_restore_top_level_settings():
    name = "SYSTEM_MAX_HOLDINGS"
    before = getattr(config, name)
    prev, missing = EN.apply_overrides([("SETTINGS", name, before + 1)])
    assert getattr(config, name) == before + 1
    EN.undo_overrides(prev, missing)
    assert getattr(config, name) == before


def test_deployed_is_the_complement_of_cash():
    """노출의 자는 '투입자본 = 100 - 평균현금비율'. avg_cash_ratio 는 이미 %다."""
    assert EN.deployed([{"cash": 60.0}, {"cash": 50.0}]) == pytest.approx(45.0)
