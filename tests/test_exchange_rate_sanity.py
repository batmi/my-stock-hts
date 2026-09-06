"""환율은 화면 숫자가 아니라 **판정의 계수**다 (감사 2026-09-06, 배치 54).

get_exchange_rate 의 값은 해외 평가액을 원화로 환산하는 데 쓰이고, 그 총자산은
  · 일일 손실 차단기의 분모
  · 포지션 사이징의 기준 자산
  · 드로다운 고점(HWM)의 재료
가 된다. 그런데 종전에는 ① 조회가 **어떤 값이든** 돌려주기만 하면 그대로 썼고
② 두 경로가 다 실패해도 두 except 가 조용히 pass 해 기본값이 흔적 없이 나갔다.

0 이 통과하면 해외 자산이 통째로 사라진 것처럼 보이고, 그 자산 급감은 가짜 드로다운과
가짜 출금 감지로 연쇄한다([[daily-asset-baseline-transfers]]).
"""
import logging

import pytest

import config
from core import utils


class _FastInfo:
    def __init__(self, last_price):
        self.last_price = last_price


class _Ticker:
    def __init__(self, last_price):
        self.fast_info = _FastInfo(last_price)


@pytest.fixture
def no_tv(monkeypatch):
    """TradingView 경로를 막아 yfinance 경로만 남긴다."""
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == 'tradingview_screener':
            raise ImportError("blocked in test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', _blocked)
    monkeypatch.setattr(utils, '_fx_fallback_last_log', 0.0, raising=False)
    yield


@pytest.mark.parametrize("bogus", [0, 0.0, -1450.0, float('nan'), 1.0, 999999.0, "", None])
def test_환율로_쓸_수_없는_값은_기본값으로_되돌린다(no_tv, monkeypatch, bogus):
    """0·음수·NaN·자릿수가 어긋난 값이 계수로 들어가면 총자산이 조용히 틀어진다."""
    monkeypatch.setattr(utils.yf, 'Ticker', lambda sym: _Ticker(bogus))
    assert utils.get_exchange_rate() == config.DEFAULT_EXCHANGE_RATE


@pytest.mark.parametrize("good", [1300.0, 1450.5, 900.0, 2000.0])
def test_정상_범위의_값은_그대로_쓴다(no_tv, monkeypatch, good):
    """좁게 잡아 정상 환율을 거르면 그것이 더 나쁘다."""
    monkeypatch.setattr(utils.yf, 'Ticker', lambda sym: _Ticker(good))
    assert utils.get_exchange_rate() == good


def test_기본값_폴백은_흔적을_남긴다(no_tv, monkeypatch, caplog):
    """고정 환율로 자산을 재고 있다는 사실은 운영자가 알아야 한다.
    종전에는 DEBUG/TRACE 를 켜지 않으면 아무 흔적도 없었다."""
    def _boom(sym):
        raise RuntimeError("network down")

    monkeypatch.setattr(utils.yf, 'Ticker', _boom)
    with caplog.at_level(logging.WARNING, logger=utils.logger.name):
        assert utils.get_exchange_rate() == config.DEFAULT_EXCHANGE_RATE
    assert any("환율" in r.message for r in caplog.records), \
        "환율 조회 실패가 아무 흔적도 남기지 않았다"


def test_폴백_로그는_반복해서_도배하지_않는다(no_tv, monkeypatch, caplog):
    """이 함수는 화면·자산집계에서 자주 불린다 — 파이 로그를 채우면 안 된다."""
    def _boom(sym):
        raise RuntimeError("network down")

    monkeypatch.setattr(utils.yf, 'Ticker', _boom)
    with caplog.at_level(logging.WARNING, logger=utils.logger.name):
        for _ in range(5):
            utils.get_exchange_rate()
    fx_logs = [r for r in caplog.records if "환율" in r.message and "기본값" in r.message]
    assert len(fx_logs) == 1, f"폴백 로그가 {len(fx_logs)}번 찍혔다 — 묶이지 않았다"
