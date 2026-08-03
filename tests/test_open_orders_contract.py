"""미체결 조회의 반환 계약 — 모든 모드에서 '주문 dict의 리스트'여야 한다.

관찰 모드(mode 4)가 응답 봉투 dict({"rt_cd":..., "output": []})를 돌려주는 바람에
호출부가 그대로 순회하다가 키 문자열을 원소로 받아 터졌다.

  [2026-08-04] 미체결 관리 중 오류: 'str' object has no attribute 'get'

봉투 dict는 비어 있지 않아 `if unfilled_list:` 를 통과하고, 순회하면 'rt_cd' 같은
str이 나온다. 증상이 두 곳에서 났다.
  · engine.manage_unfilled_orders — 매 주기 로그 에러 (사용자 관측)
  · trader._get_toss_open_buy_reserved — 예외로 입금 자동 감지가 조용히 스킵됨
    (mode 4는 is_toss·is_paper가 동시에 True라 토스 전용 경로도 함께 탄다)

계약을 테스트로 고정해 어떤 모드가 추가돼도 형태가 갈라지지 않게 한다.
"""
import pytest
from unittest.mock import patch

import api
import config


@pytest.fixture
def paper_session(monkeypatch):
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', True, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', False, raising=False)


def test_paper_mode_returns_list(paper_session):
    """관찰 모드는 빈 '리스트'를 돌려줘야 한다 — 응답 봉투 dict가 아니다."""
    res = api.get_domestic_open_orders()
    assert isinstance(res, list), f"list 계약 위반: {type(res).__name__} 반환"
    assert res == []


def test_alias_shares_the_contract(paper_session):
    """get_unfilled_orders는 alias이므로 같은 형태여야 한다(엔진이 쓰는 진입점)."""
    assert isinstance(api.get_unfilled_orders(), list)


def test_iterating_result_yields_dicts_not_strings(paper_session):
    """호출부는 원소에 .get()을 호출한다 — str이 나오면 그 자리에서 터진다."""
    for item in api.get_unfilled_orders():
        assert hasattr(item, 'get'), \
            f"원소가 {type(item).__name__}이다 — 'str' object has no attribute 'get' 재발"


def test_toss_mode_returns_list(monkeypatch):
    """대조군 — 토스(실전) 경로도 리스트다. 관찰 모드만 달랐던 것이 원인이었다."""
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', True, raising=False)
    with patch.object(api, '_toss_open_orders', return_value=[]) as mock_toss:
        res = api.get_domestic_open_orders()
    mock_toss.assert_called_once_with('domestic')
    assert isinstance(res, list)


def test_reserved_cash_calculation_survives_paper_mode(paper_session):
    """trader._get_toss_open_buy_reserved가 예외 없이 0을 돌려줘야 한다.

    mode 4는 is_toss도 True라 이 토스 전용 보정 경로를 함께 탄다. 예외가 나면
    toss_cash_reliable=False가 되어 입금 자동 감지가 매 주기 조용히 건너뛰어진다.
    """
    from modules.auto_trade import AutoTrader

    AutoTrader._instance = None
    trader = AutoTrader()
    assert trader._get_toss_open_buy_reserved("PAPER", "") == 0


def test_unfilled_manager_runs_clean_in_paper_mode(paper_session):
    """엔진의 미체결 관리가 로그 에러 없이 한 주기를 돈다(사용자가 관측한 증상)."""
    from modules.auto_trade import AutoTrader

    AutoTrader._instance = None
    trader = AutoTrader()
    trader.is_running = True

    logged = []
    with patch.object(trader, 'log', side_effect=lambda m, *a, **k: logged.append(str(m))), \
         patch.object(trader, 'is_market_open', return_value=True):
        trader.order_manager.manage_unfilled_orders()

    bad = [m for m in logged if '미체결 관리 중 오류' in m or 'has no attribute' in m]
    assert not bad, f"미체결 관리가 여전히 실패한다: {bad}"
