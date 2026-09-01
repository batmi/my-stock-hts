"""오픈 리스크가 **직전 주기의 실측 손절선**을 쓰는가.

히트(총 오픈 리스크)는 주기 앞머리, 잔고를 받자마자 계산된다. 그 자리엔 차트가 없어
종전에는 진입 시점 손절률에서 ATR을 역산했다 — '진입 때의 변동성이 지금도 그대로'라는
가정이다. 추세가 길어지면 실제 ATR이 커지고 샹들리에 콜백도 넓어지므로, 가정한 청산선이
실제보다 위에 놓인다 = **리스크를 작게 본다**. 히트 캡은 리스크 스케일링의 실효 방어
경로라(config SYSTEM_MAX_PORTFOLIO_RISK), 과소 계상은 방어를 그만큼 무르게 만든다.

같은 주기의 매도 판정은 이미 그 종목의 지표(ATR)와 실효 손절률을 손에 쥔다. 그것을
남겨 다음 주기의 히트가 쓰게 하는 것이 이 배선이다(60초 전의 일봉 ATR이라 실질 차이가
없다). 산식 쪽 검증은 test_risk_manager.py 의 live_map 테스트들에 있고, 여기서는
**배선**을 고정한다 — 채우는 곳, 거르는 곳, 넘기는 곳.
"""
import inspect
import threading

import pytest

from modules.auto_trade import AutoTrader


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    yield t
    AutoTrader._instance = None


def test_the_cache_starts_empty_and_is_a_dict(trader):
    assert trader.holding_risk_cache == {}


def test_only_currently_held_codes_survive(trader):
    """판 종목의 값이 남으면, 같은 종목을 다시 담았을 때 **예전 포지션의 손절선**이
    새 포지션의 리스크로 계상된다."""
    trader.holding_risk_cache = {
        '005930': {'sl_rate': -7.0, 'atr': 900.0},
        '000660': {'sl_rate': -12.0, 'atr': 4000.0},
    }
    live = trader._live_risk_map(['005930'])
    assert live == {'005930': {'sl_rate': -7.0, 'atr': 900.0}}
    assert list(trader.holding_risk_cache) == ['005930'], "캐시 자체도 함께 정리돼야 한다"


def test_an_empty_account_clears_everything(trader):
    trader.holding_risk_cache = {'005930': {'sl_rate': -7.0, 'atr': 900.0}}
    assert trader._live_risk_map([]) == {}
    assert trader.holding_risk_cache == {}


def test_the_snapshot_is_a_copy(trader):
    """호출부가 들고 있는 동안 매도 워커가 캐시를 고쳐도 스냅샷은 흔들리지 않아야 한다."""
    trader.holding_risk_cache = {'005930': {'sl_rate': -7.0, 'atr': 900.0}}
    live = trader._live_risk_map(['005930'])
    trader.holding_risk_cache['005930'] = {'sl_rate': -1.0, 'atr': 1.0}
    assert live['005930']['sl_rate'] == -7.0


def test_the_sell_scan_records_the_stop_it_actually_used():
    """[배선] 매도 판정이 쓴 손절률·ATR을 그 자리에서 남기지 않으면 히트는 영영
    역산으로 돌아간다. 이 블록은 잔고·시세·차트를 모두 갖춘 주기 안에 있어 행위
    재현 비용이 크고, 회귀는 '한 블록이 빠진다'는 형태로만 온다."""
    src = inspect.getsource(AutoTrader._check_sell_conditions)
    assert "self.holding_risk_cache[code]" in src, "매도 판정이 실측 손절선을 남기지 않는다"
    block = src.split("self.holding_risk_cache[code]")[1][:400]
    assert "thresholds.get(" in block and "STOP_LOSS_RATE" in block, \
        "손절률을 SSOT(build_sell_thresholds 결과)에서 가져오지 않는다"
    assert "ind.get('atr')" in block, "그 시점 ATR을 남기지 않는다"


def test_the_heat_snapshot_is_fed_the_live_map():
    """[배선] 남겨 둔 값을 히트에 넘기지 않으면 아무 일도 일어나지 않는다."""
    src = inspect.getsource(AutoTrader._check_sell_conditions)
    assert "compute_portfolio_heat(" in src
    call = src.split("compute_portfolio_heat(")[1][:200]
    assert "live_map=self._live_risk_map(" in call, "히트가 실측 손절선을 받지 않는다"
