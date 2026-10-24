"""실매매 동적 손절 캡의 지수 변동성 배율 — 백테스트와 같은 식을 긴 확정 종가 이력에 적용하는가.

[왜 · 2026-10-24] 배율(indicators.vol_regime_ratio)은 60일 변동성 ÷ 250표본 이상의 확장 중앙값이라 311봉
이상에서만 1.0 이 아닌 값을 낸다. 실매매는 이 배율을 판단용 지수 창(KRX 264봉·KIS 300봉·tvDatafeed 260봉)으로
쟀으므로 값이 늘 1.0 이었고, 손절 캡은 도입(2026-08-09) 이후 실매매에서 한 번도 움직이지 않았다(같은 기간
백테스트 배율 3.0·캡 -26%). 게다가 그 갱신은 시장 필터를 켰을 때만 돌았다 — 필터를 끈 파이 가상투자는
계산조차 하지 않았다. 기존 테스트(test_dynamic_atr_cap)는 식과 '같은 배율이면 실매매=백테스트'만 봐서
실매매가 받는 입력의 길이는 아무도 확인하지 않았다.
"""
import ast
import inspect
import logging
import textwrap
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import api
import config
from core import indicators
from modules import analysis, krx_openapi
from modules.auto_trade import AutoTrader, engine
from modules.auto_trade import trader as trader_mod

SETTLED = "20261023"


def _long_close(n=3700, tail_sd=0.03, seed=11):
    """끝 60일만 변동성이 큰 긴 지수 종가 — 배율이 상한(3.0) 가까이 간다."""
    rs = np.random.RandomState(seed)
    sd = np.r_[np.full(n - 60, 0.010), np.full(60, tail_sd)]
    return 1000.0 * np.cumprod(1 + rs.normal(0.0003, sd))


def _yf_frame(close, last_dd=SETTLED, extra_today=None):
    """yfinance 일봉 모양(DatetimeIndex·'Close' 열). extra_today 면 확정일 뒤 '장중 봉'을 하나 더 붙인다."""
    idx = pd.bdate_range(end=pd.Timestamp(last_dd), periods=len(close))
    df = pd.DataFrame({"Close": close}, index=idx)
    if extra_today is not None:
        df.loc[idx[-1] + pd.tseries.offsets.BDay(1)] = extra_today
    return df


@pytest.fixture
def settled(monkeypatch):
    monkeypatch.setattr(api, "krx_last_settled_day", lambda: SETTLED)
    analysis.reset_live_vol_regime()
    yield
    analysis.reset_live_vol_regime()


# ── 결함 자체 ────────────────────────────────────────────────────────────────────────────────
def test_the_live_index_window_never_leaves_the_warmup():
    """[재현] 판단용 지수 창(264봉)으로 재면 배율은 늘 1.0 이다 — 같은 종가를 길게 주면 그렇지 않다."""
    close = _long_close()
    assert indicators.vol_regime_ratio(close[-264:]).iloc[-1] == 1.0
    assert indicators.vol_regime_ratio(close).iloc[-1] > 1.5
    window = int(config.SELL_STRATEGY.get("ATR_CAP_VOL_WINDOW", 60))
    ref_min = int(config.SELL_STRATEGY.get("ATR_CAP_VOL_REF_MIN", 250))
    #  실매매 입력의 최소 길이가 워밍업을 넉넉히 넘는다(약 10년 ≥ 311봉).
    assert analysis._LIVE_VOL_MIN_STORED_BARS > window + ref_min + 1


# ── 실매매 배율의 출처·식 ────────────────────────────────────────────────────────────────────────
def test_live_ratio_is_the_backtest_formula_on_the_long_history(settled, monkeypatch):
    close = _long_close()
    monkeypatch.setattr(api, "fetch_yfinance_data", lambda *a, **k: _yf_frame(close))
    info = analysis.get_live_vol_regime()
    assert info["source"] == "YF" and info["bars"] == len(close) and info["asof"] == SETTLED
    assert info["ratio"] == pytest.approx(float(indicators.vol_regime_ratio(close).iloc[-1]))


def test_todays_intraday_bar_is_not_used(settled, monkeypatch):
    """yfinance 일봉은 장중에 오늘 봉을 현재가로 준다 — 확정일 뒤 봉은 버린다(배율이 분마다 흔들리지 않게)."""
    close = _long_close()
    monkeypatch.setattr(api, "fetch_yfinance_data", lambda *a, **k: _yf_frame(close, extra_today=close[-1] * 0.8))
    info = analysis.get_live_vol_regime()
    assert info["asof"] == SETTLED and info["bars"] == len(close)
    assert info["ratio"] == pytest.approx(float(indicators.vol_regime_ratio(close).iloc[-1]))


def test_falls_back_to_the_local_store_but_not_to_a_short_one(settled, monkeypatch):
    close = _long_close()
    monkeypatch.setattr(api, "fetch_yfinance_data", lambda *a, **k: pd.DataFrame())
    stored = pd.DataFrame({"date": pd.bdate_range(end=SETTLED, periods=len(close)).strftime("%Y%m%d"),
                           "close": close})
    monkeypatch.setattr(krx_openapi, "index_history_stored", lambda *a, **k: stored)
    info = analysis.get_live_vol_regime()
    assert info["source"] == "OPENAPI" and info["bars"] == len(close)

    #  저장분이 짧으면(파이처럼 400일만 받아 둔 경우) 쓰지 않는다 — 짧은 이력의 확장 중앙값은 검증된 배율이 아니다.
    analysis.reset_live_vol_regime()
    monkeypatch.setattr(krx_openapi, "index_history_stored", lambda *a, **k: stored.tail(300))
    assert analysis.get_live_vol_regime() is None


def test_fetched_once_per_settled_day_and_retried_after_a_failure(settled, monkeypatch):
    close = _long_close()
    calls = []

    def _yf(*a, **k):
        calls.append(1)
        return _yf_frame(close) if len(calls) != 2 else pd.DataFrame()

    monkeypatch.setattr(api, "fetch_yfinance_data", _yf)
    monkeypatch.setattr(krx_openapi, "index_history_stored", lambda *a, **k: None)
    first = analysis.get_live_vol_regime()
    assert analysis.get_live_vol_regime() == first and len(calls) == 1, "같은 확정일에 다시 받았다"

    #  확정일이 바뀌면 다시 받는다. 이번엔 실패 → 직전 값을 쓰고(하루 낡음), 시한 안에는 다시 묻지 않는다.
    monkeypatch.setattr(api, "krx_last_settled_day", lambda: "20261026")
    assert analysis.get_live_vol_regime() == first and len(calls) == 2
    assert analysis.get_live_vol_regime() == first and len(calls) == 2

    real_time = analysis.time.time
    monkeypatch.setattr(analysis.time, "time", lambda: real_time() + analysis._LIVE_VOL_RETRY_SEC + 1)
    assert analysis.get_live_vol_regime() is not None and len(calls) == 3


def test_a_value_older_than_a_week_is_dropped(settled, monkeypatch):
    close = _long_close()
    monkeypatch.setattr(api, "fetch_yfinance_data", lambda *a, **k: _yf_frame(close))
    assert analysis.get_live_vol_regime() is not None
    monkeypatch.setattr(api, "fetch_yfinance_data", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(krx_openapi, "index_history_stored", lambda *a, **k: None)
    monkeypatch.setattr(api, "krx_last_settled_day", lambda: "20261106")       # 14일 뒤
    assert analysis.get_live_vol_regime() is None, "2주 묵은 배율로 손절 캡을 넓혔다"


# ── 트레이더 배선 ────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    yield t
    AutoTrader._instance = None


def test_ratio_is_updated_even_with_the_market_filter_off(trader, monkeypatch):
    """[핵심 회귀] 시장 필터를 끈 운용(파이 가상)에서도 배율이 갱신되고 캡이 넓어진다."""
    monkeypatch.setattr(config, "USE_MARKET_FILTER", False)
    monkeypatch.setattr(analysis, "get_live_vol_regime",
                        lambda: {"ratio": 3.0, "asof": SETTLED, "source": "YF", "bars": 3700})
    trader._update_vol_regime()
    assert engine.get_vol_regime_ratio() == 3.0
    assert engine.effective_atr_stop_cap() == pytest.approx(-15.0 * 3.0 ** 0.5)       # -25.98%


def test_missing_history_returns_to_the_fixed_cap_and_warns_once_a_day(trader, monkeypatch, caplog):
    engine.set_vol_regime_ratio(3.0)
    monkeypatch.setattr(analysis, "get_live_vol_regime", lambda: None)
    with caplog.at_level(logging.WARNING, logger="modules.auto_trade.trader"):
        trader._update_vol_regime()
        trader._update_vol_regime()
    assert engine.get_vol_regime_ratio() == 1.0, "이력을 못 받았는데 넓힌 캡을 그대로 뒀다"
    assert sum("지수 장기 이력을 받지 못해" in r.message for r in caplog.records) == 1


def test_the_loop_updates_the_ratio_outside_the_market_filter_branch():
    """종전 결함의 형태 — 갱신이 `if USE_MARKET_FILTER:` 안에 있으면 필터를 끄는 순간 캡이 고정된다."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(trader_mod.AutoTrader._run_loop)))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "_update_vol_regime"]
    assert calls, "주 루프가 변동성 배율을 갱신하지 않는다"
    node = calls[0]
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If):
            assert "USE_MARKET_FILTER" not in ast.unparse(node.test), "배율 갱신이 시장 필터 분기 안에 있다"
    #  판단용 지수 창으로 배율을 재던 옛 경로가 되살아나면 그 값(늘 1.0)이 긴 이력 값을 덮는다.
    assert "set_vol_regime_ratio" not in inspect.getsource(trader_mod.AutoTrader._update_market_indices_status)


def test_health_panel_shows_the_effective_cap(trader, monkeypatch):
    monkeypatch.setattr(trader, "is_market_open", lambda: True)
    monkeypatch.setattr(config, "USE_WEBSOCKET", False, raising=False)
    trader.is_running = True
    trader.last_cycle_at = datetime.now()
    engine.set_vol_regime_ratio(3.0)
    trader._vol_regime_tried = True
    trader.vol_regime_info = {"ratio": 3.0, "asof": SETTLED, "source": "YF", "bars": 3700}
    assert "ATR 손절 캡 -26.0% (지수 변동성 x3.00, 10/23 확정 종가 기준)" in trader.get_health_message()

    trader.vol_regime_info = None
    engine.set_vol_regime_ratio(1.0)
    assert "ATR 손절 캡 -15.0% 고정 (지수 장기 이력 미확보)" in trader.get_health_message()
