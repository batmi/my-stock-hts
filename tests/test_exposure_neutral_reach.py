"""노출 중립 대조가 **실제로 바꿀 수 있는 것만** 받는가.

[무엇이 문제였나 · 2026-09-09] `audit_exposure_neutral` 의 오버라이드는 run_portfolio 를
감쌀 뿐인데, 지표 컬럼(dfs)과 일자별 판정(status)은 그보다 **먼저 한 번만** 만들어진다.
그래서 INDICATOR_PARAMS 를 --set 으로 주면 옛 컬럼을 그대로 읽어 조용히 무동작이 되고,
표에는 `0-60-0 완전 동률`이 찍힌다.

**그 표는 '차이가 없다'로 읽히지만 실제로는 '재지 못했다'이다** — [[unknown-vs-empty]] 의
감사 도구 판이고, 채택 규칙의 근거로 쓰이는 표라 더 나쁘다. 실측: OBV_MA_PERIOD 5 vs 10
을 걸었더니 다섯 창 전부 소수점까지 같았다(같은 함정이 audit_indicator_construction.py
머리말에 이미 적혀 있었는데 이 도구엔 가드가 없었다).

고친 방향은 둘이다.
 · 컬럼에 굳는 것(INDICATOR_PARAMS) → **멈춘다.** 다른 도구를 가리킨다.
 · 판정에 굳는 것(SCORING_WEIGHTS·BUY_SCORE 등) → 팔마다 status 를 다시 계산해 지원한다.
"""
import pytest

from tools import audit_exposure_neutral as en


def test_indicator_params_are_refused_not_silently_ignored():
    with pytest.raises(SystemExit) as e:
        en._reject_unmeasurable([("INDICATOR_PARAMS", "OBV_MA_PERIOD", 5)])
    msg = str(e.value)
    assert "잴 수 없다" in msg
    assert "audit_indicator_construction" in msg, "어디서 재야 하는지 말해야 한다"


def test_runtime_groups_are_allowed():
    """청산·리스크 다이얼은 run_portfolio 가 매번 읽으므로 이 도구로 잴 수 있다."""
    en._reject_unmeasurable([("SELL_STRATEGY", "TIME_STOP_DAYS", 15),
                             ("RISK_SCALING_PARAMS", "DD_SCALE_1", 0.9)])


def test_status_baked_keys_trigger_a_recompute():
    """점수 판정에 굳는 값은 status 를 다시 만들어야 실제로 반영된다."""
    assert en._touches_status([("SCORING_WEIGHTS", "TREND", 4.0)])
    assert en._touches_status([("ANALYSIS_THRESHOLDS", "BUY_SCORE", 7.5)])


def test_runtime_keys_do_not_pay_for_a_recompute():
    """매번 읽히는 값까지 재계산하면 느려지기만 한다 — 구분이 있어야 한다."""
    assert not en._touches_status([("SELL_STRATEGY", "TIME_STOP_DAYS", 15)])
    assert not en._touches_status([("ANALYSIS_THRESHOLDS", "PYRAMIDING_PROFIT_TRIGGER", 7.0)])
    assert not en._touches_status([])


def test_the_recompute_is_actually_wired_into_the_run():
    """분류만 있고 호출부가 없으면 무동작이 그대로 남는다(물기 검사)."""
    import inspect
    src = inspect.getsource(en.main)
    assert "_touches_status" in src, "분류 함수가 run() 에서 쓰이지 않는다"
    assert "precompute_status" in src, "status 를 다시 만드는 곳이 없다"
    assert "_reject_unmeasurable" in src, "거부 가드가 호출되지 않는다"
