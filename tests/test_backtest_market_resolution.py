"""백테스트 시장 필터가 **남의 지수**로 차단일을 계산하지 못하게 한다.

[왜 · 2026-09-08 감사] `prepare_market_filter` 는 코스피/코스닥을
`config.session.stock_data` 에서만 찾고, 못 찾으면 조용히 KOSPI 로 단정했다.
관심종목만 돌리던 시절에는 맞았지만, 감사 도구가 씨드로 **관심종목 밖에서** 표본을
뽑기 시작하면서(rule_pool·dead_targets) 그 표본은 전부 '못 찾음' → 전부 KOSPI 가 됐다.

실측: 도구들이 뽑는 시총 상위 500 풀 중 456 종목이 관심종목 밖이고 그중 **153 종목이
코스닥**이다. 표본의 약 3분의 1이 남의 지수로 진입 차단일을 받아 온 셈이다. 코스피와
코스닥은 국면이 갈리므로 차단일 집합이 실제로 달라지고, 그러면 계측기가 조용히 다른
것을 잰다([[audit-scale-fn-contamination]] 과 같은 계열의 실패).

여기서 지키는 것은 셋이다.
  ① 관심종목은 stock.json 이 이긴다 — 이 순서가 깨지면 **기존 감사 수치가 재실행 대상**이 된다.
  ② 관심종목 밖은 analysis.get_market_type 에게 묻는다([[market-type-single-source]] 정본).
  ③ 둘 다 모르면 KOSPI 로 두되 **로그로 남긴다** — 추측을 조용히 하지 않는다.
"""
import logging

import pytest

import config
from modules import backtest


@pytest.fixture(autouse=True)
def _clear_warn_memo():
    backtest._MARKET_UNKNOWN_WARNED.clear()
    yield
    backtest._MARKET_UNKNOWN_WARNED.clear()


def test_watchlist_exchange_wins_over_ssot(monkeypatch):
    """① 관심종목에 적힌 exchange 가 최우선 — 기존 유니버스의 수치를 보존한다."""
    monkeypatch.setattr(config.session, "stock_data",
                        {"stocks_kr": [{"code": "123456", "exchange": "KOSDAQ"}]},
                        raising=False)
    monkeypatch.setattr("modules.analysis.get_market_type", lambda code: "KOSPI")
    assert backtest._resolve_backtest_market("123456") == "KOSDAQ"


def test_outside_watchlist_uses_market_type_ssot(monkeypatch):
    """② 관심종목 밖이면 정본에게 묻는다 — 종전에는 여기서 전부 KOSPI 였다."""
    monkeypatch.setattr(config.session, "stock_data", {"stocks_kr": []}, raising=False)
    monkeypatch.setattr("modules.analysis.get_market_type", lambda code: "KOSDAQ")
    assert backtest._resolve_backtest_market("247540") == "KOSDAQ"


def test_unknown_market_falls_back_to_kospi_but_says_so(monkeypatch, caplog):
    """③ 판정 불가는 KOSPI 로 진행하되 반드시 흔적을 남긴다."""
    monkeypatch.setattr(config.session, "stock_data", {"stocks_kr": []}, raising=False)
    monkeypatch.setattr("modules.analysis.get_market_type", lambda code: None)
    with caplog.at_level(logging.WARNING, logger=backtest.logger.name):
        assert backtest._resolve_backtest_market("999999") == "KOSPI"
    assert any("999999" in r.message for r in caplog.records), \
        "시장을 모른 채 KOSPI 로 진행했는데 아무 흔적도 남기지 않았습니다"


def test_unknown_market_warns_once_per_code(monkeypatch, caplog):
    """감사 한 번에 수백 종목이 지나므로 종목당 한 줄로 묶는다."""
    monkeypatch.setattr(config.session, "stock_data", {"stocks_kr": []}, raising=False)
    monkeypatch.setattr("modules.analysis.get_market_type", lambda code: None)
    with caplog.at_level(logging.WARNING, logger=backtest.logger.name):
        for _ in range(5):
            backtest._resolve_backtest_market("999999")
    assert sum(1 for r in caplog.records if "999999" in r.message) == 1


def test_market_type_failure_does_not_break_preparation(monkeypatch):
    """정본이 예외를 던져도 유니버스 준비가 멈추지는 않는다(KOSPI 로 흡수 + 로그)."""
    monkeypatch.setattr(config.session, "stock_data", {"stocks_kr": []}, raising=False)

    def _boom(code):
        raise RuntimeError("마스터 파일 없음")

    monkeypatch.setattr("modules.analysis.get_market_type", _boom)
    assert backtest._resolve_backtest_market("111111") == "KOSPI"
