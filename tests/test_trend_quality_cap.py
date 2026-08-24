"""추세품질 상한(TREND_QUALITY_MAX) 진입 게이트 — 실매매 경로 검증.

[왜 고정하나] 이 게이트는 '강할수록 좋다'는 직관과 반대 방향이라, 나중에 코드를 읽는
 사람이 버그로 오인해 지우기 쉽다. 추세품질은 단조가 아니다 — 100~140에서 정점을 찍고
 300 위에서 전방수익이 음수로 꺾이며 꼬리가 잘린다(상위10% 56.6 → 14.2). 근거는 config
 ANALYSIS_THRESHOLDS['TREND_QUALITY_MAX'] 주석. 게이트가 조용히 무동작이 되면 백테스트
 수치와 실매매가 갈리므로, '막는다·안 막는다·해제된다' 셋을 못 박는다.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import indicators  # noqa: E402
from modules.auto_trade import AutoTrader  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_trader():
    """게이트 하나만 남기고 앞단 분기(시장 필터·상관·RS)를 모두 통과시킨다."""
    AutoTrader._instance = None
    prev = (config.USE_MARKET_FILTER, config.USE_CORRELATION_FILTER,
            getattr(config, "USE_RS_FILTER", False))
    config.USE_MARKET_FILTER = False
    config.USE_CORRELATION_FILTER = False
    config.USE_RS_FILTER = False
    patchers = [
        patch("modules.auto_trade.api.get_current_price", return_value=0),
        patch("modules.auto_trade.api.get_order_book",
              return_value={"rt_cd": "0", "output1": {"total_askp_rsqn": "100",
                                                      "total_bidp_rsqn": "100"}}),
        patch("modules.auto_trade.api.is_nxt_tradeable", return_value=True),
    ]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()
    (config.USE_MARKET_FILTER, config.USE_CORRELATION_FILTER,
     config.USE_RS_FILTER) = prev
    AutoTrader._instance = None


def _df(daily_drift, length=200):
    """일정 기울기로 로그선형 상승하는 일봉 — 추세품질을 원하는 크기로 만든다.

    잡음이 없으면 R²가 1에 가까워 추세품질 ≈ 연환산 수익률(%)이 된다. 그래서 기울기만
    바꿔 '가파른 추세'와 '보통 추세'를 통제된 크기로 만들 수 있다.
    """
    dates = pd.date_range("2024-01-01", periods=length).strftime("%Y%m%d")
    prices = 1000 * np.exp(np.arange(length) * daily_drift)
    return pd.DataFrame({"date": dates, "close": prices, "open": prices,
                         "high": prices * 1.005, "low": prices * 0.995, "volume": 1000})


def _run(trader, df):
    item = {"code": "005930", "name": "삼성전자", "group": "stocks_kr"}
    with patch.object(trader.strategy, "analyze_buy") as mock_buy:
        mock_buy.return_value = {
            "action": "buy", "state": "매수", "score": 8.0, "rsi": 55, "adx": 30,
            "cci": 50, "atr": 1000.0, "psar": 0, "macd": 1.0, "macd_signal": 0.5,
            "w52_pos": 80.0,
            "trend_quality": indicators.get_trend_quality(df),
        }
        with patch("modules.auto_trade.api.get_chart_data", return_value=df):
            return trader._analyze_candidate_worker(
                item, holding_codes=set(), rules_map={}, restricted_stocks={},
                market_regime_adj={"KOSPI": 0.0}, safe_delay=0, reentry_hurdles={},
                holdings_dfs={}, holding_groups_map={})


@patch("modules.auto_trade.AutoTrader._get_stock_market_type", return_value="KOSPI")
@patch("modules.auto_trade.api.get_realtime_vol_strength", return_value=120.0)
def test_상한을_넘는_과열_추세는_매수_후보에서_빠진다(_vol, _mkt):
    trader = AutoTrader()
    trader.is_running = True
    steep = _df(0.006)          # 연환산 ≈ +360% → 추세품질이 300을 훌쩍 넘는다
    assert indicators.get_trend_quality(steep) > 300

    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": 300.0}):
        res = _run(trader, steep)

    assert res is not None
    assert res["type"] == "tq_cap_skip", f"추세품질 상한이 걸리지 않았다: {res}"
    assert "추세품질 상한" in res["log"]


@patch("modules.auto_trade.AutoTrader._get_stock_market_type", return_value="KOSPI")
@patch("modules.auto_trade.api.get_realtime_vol_strength", return_value=120.0)
def test_상한_아래_추세는_그대로_후보가_된다(_vol, _mkt):
    """게이트가 정상 추세까지 자르면 진입이 고갈된다 — 반대쪽도 함께 고정한다."""
    trader = AutoTrader()
    trader.is_running = True
    normal = _df(0.0008)        # 연환산 ≈ +22% → 상한과 무관한 보통 추세
    assert 0 < indicators.get_trend_quality(normal) < 300

    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": 300.0}):
        res = _run(trader, normal)

    assert res is not None
    assert res["type"] == "candidate", f"정상 추세가 막혔다: {res}"


@patch("modules.auto_trade.AutoTrader._get_stock_market_type", return_value="KOSPI")
@patch("modules.auto_trade.api.get_realtime_vol_strength", return_value=120.0)
def test_상한_0은_해제다(_vol, _mkt):
    """0 = 해제. 채택 이전 동작으로 되돌리는 통로가 살아 있어야 한다."""
    trader = AutoTrader()
    trader.is_running = True
    steep = _df(0.006)

    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": 0}):
        res = _run(trader, steep)

    assert res is not None
    assert res["type"] == "candidate", f"해제 상태인데 막혔다: {res}"


@patch("modules.auto_trade.AutoTrader._get_stock_market_type", return_value="KOSPI")
@patch("modules.auto_trade.api.get_realtime_vol_strength", return_value=120.0)
def test_이력부족은_통과시킨다(_vol, _mkt):
    """fail-open — 데이터가 없다고 막으면 신규 상장·데이터 장애가 매수 중단으로 번진다."""
    trader = AutoTrader()
    trader.is_running = True
    short = _df(0.006, length=30)       # 룩백 90봉 미만 → 추세품질 None
    assert indicators.get_trend_quality(short) is None

    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": 300.0}):
        res = _run(trader, short)

    assert res is not None
    assert res["type"] != "tq_cap_skip", f"이력부족이 차단됐다: {res}"
