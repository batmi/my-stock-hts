"""화면의 시간청산 기한은 판정이 쓰는 값과 같아야 한다.

청산 판정(engine.analyze_sell)은 `thresholds["TIME_STOP_DAYS"] = _rv('time_stop_days', 전역)`
으로 **개별 룰**을 존중한다(stock_strategies.time_stop_days 는 실제 컬럼이다). 그런데
화면들은 전역 config 만 읽었다 — 룰로 기한을 바꾼 종목에서 잔고의 보유일 경고와
가상투자의 'D-n' 이 실제 청산 시점과 어긋났다. 텔레그램 룰 안내는 폴백 리터럴이 10 이라
정본(현재 15)과도 달랐다.
"""
import pytest

import config
from modules.auto_trade import common


@pytest.fixture
def global_15(monkeypatch):
    #  기능 스위치까지 고정한다 — 다른 테스트가 전역을 바꾼 채 끝나면 실행 순서에 따라
    #  결과가 갈린다(test_time_stop.py 가 실제로 그랬다).
    monkeypatch.setitem(config.SELL_STRATEGY, "TIME_STOP_DAYS", 15)
    monkeypatch.setitem(config.SELL_STRATEGY, "TIME_STOP_USE", True)


def test_rule_wins_over_global(global_15):
    assert common.effective_time_stop_days(rule={"time_stop_days": 7}) == 7


@pytest.mark.parametrize("rule", [None, {}, {"time_stop_days": None}, {"time_stop_days": ""}])
def test_falls_back_to_global(global_15, rule):
    """룰이 없거나 값이 비면 전역 — 판정도 그렇게 폴백한다."""
    assert common.effective_time_stop_days(rule=rule) == 15


def test_lookup_by_code_uses_the_rule(global_15, monkeypatch):
    from modules import db_manager

    monkeypatch.setattr(db_manager.db, "get_stock_strategy",
                        lambda code: {"time_stop_days": 9} if code == "005930" else None)
    assert common.effective_time_stop_days("005930") == 9
    assert common.effective_time_stop_days("000660") == 15


def test_lookup_failure_falls_back_quietly(global_15, monkeypatch):
    """DB 조회가 깨져도 화면이 죽으면 안 된다 — 판정과 같은 폴백(전역)으로 간다."""
    from modules import db_manager

    def _boom(code):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(db_manager.db, "get_stock_strategy", _boom)
    assert common.effective_time_stop_days("005930") == 15


def test_balance_screen_warns_on_the_rule_threshold(global_15, monkeypatch):
    """잔고 보유일 경고가 개별 룰 기한을 따른다."""
    from modules import account
    from modules import db_manager

    monkeypatch.setattr(db_manager.db, "get_stock_strategy",
                        lambda code: {"time_stop_days": 5})

    warned = account._fmt_holding_days_cell({"holding_days": 6}, code="005930")
    calm = account._fmt_holding_days_cell({"holding_days": 4}, code="005930")
    assert "yellow" in warned, "룰 기한(5일)을 넘겼는데 경고가 없다"
    assert "yellow" not in calm


def test_display_paths_do_not_read_the_global_directly():
    """화면이 다시 전역을 직접 읽으면 같은 어긋남이 재발한다."""
    import inspect

    from modules import paper_report

    src = inspect.getsource(paper_report._print_verification_detail)
    assert 'SELL_STRATEGY.get("TIME_STOP_DAYS"' not in src
    assert "effective_time_stop_days" in src
