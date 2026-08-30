"""당일 재진입 게이트 — 판 종목을 같은 날 다시 살 때의 두 관문.

시스템은 같은 날 판 종목의 재매수를 두 가지로 통제한다.

  ① **손절가 게이트**: 당일 손절로 잘린 종목을 그 손절가 **이상**에서는 되사지 않는다.
  ② **체결강도 허들**: 직전 매수 때의 체결강도를 넘어야 다시 산다(진입 강도 경신 요구).

둘 다 시드를 쓰는 진입 판정인데, 전체 스위트에서 ②의 분기는 한 번도 실행되지
않았다(2026-08-30 커버리지 실측). 게이트가 조용히 무동작이 되어도 붉어지지 않는
상태였고, 이 종류의 무동작은 실매매에서만 손실로 나타난다(2026-08-05: 손절 →
10초 뒤 더 비싸게 재매수를 반복해 왕복 스프레드만 쌓였다).

[알려진 비대칭 — 토스 모드] 토스는 체결강도를 주지 않아 ②의 자리에 매도잔량비 검사가
들어가 있는데, 그 조건은 analyze_buy 의 일반 매수 게이트와 **같은 임계값**이라 이미
통과한 후보를 다시 막지 못한다. 실제로 걸리는 경우는 '호가를 못 구했다(None)' 하나뿐이다.
즉 토스 모드에는 '직전 진입 강도를 넘어야 한다'는 자기 갱신 허들이 없고, 당일 재진입
방어는 ①만 남는다. 아래 두 테스트가 그 사실을 명시적으로 못 박는다 — 새 게이트를
넣는 것은 진입 필터 변경이라 같은 차단율 무작위 대조가 먼저다.
"""
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

import config
from modules.auto_trade import AutoTrader

CODE, NAME = "005930", "삼성전자"


@pytest.fixture(autouse=True)
def isolated_trader():
    """앞단 게이트(시장 필터·상관·RS)를 열어 재진입 게이트만 남긴다."""
    AutoTrader._instance = None
    prev = (config.USE_MARKET_FILTER, config.USE_CORRELATION_FILTER,
            getattr(config, "USE_RS_FILTER", False))
    config.USE_MARKET_FILTER = False
    config.USE_CORRELATION_FILTER = False
    config.USE_RS_FILTER = False
    patchers = [
        patch("modules.auto_trade.api.get_current_price", return_value=0),
        patch("modules.auto_trade.api.is_nxt_tradeable", return_value=True),
        patch("modules.auto_trade.api.get_ask_bid_ratio", return_value=2.0),
    ]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()
    (config.USE_MARKET_FILTER, config.USE_CORRELATION_FILTER,
     config.USE_RS_FILTER) = prev
    AutoTrader._instance = None


def _df(length=200):
    dates = pd.date_range("2024-01-01", periods=length).strftime("%Y%m%d")
    prices = 1000 * np.exp(np.arange(length) * 0.002)
    return pd.DataFrame({"date": dates, "close": prices, "open": prices,
                         "high": prices * 1.005, "low": prices * 0.995, "volume": 1000})


def _run(*, vol_strength=130.0, hurdles=None, stop_prices=None, abr=2.0, min_abr=1.0):
    """매수 신호가 선 종목 하나를 후보 분석에 태우고 결과를 돌려준다."""
    trader = AutoTrader()
    trader.is_running = True
    df = _df()
    item = {"code": CODE, "name": NAME, "group": "stocks_kr"}
    result = {
        "action": "buy", "state": "매수", "score": 8.0, "rsi": 55, "adx": 30,
        "cci": 50, "atr": 1000.0, "psar": 0, "macd": 1.0, "macd_signal": 0.5,
        "w52_pos": 80.0, "trend_quality": 50.0,
        "vol_strength": vol_strength, "ask_bid_ratio": abr,
        "min_ask_bid_ratio": min_abr,
    }
    with patch.object(trader.strategy, "analyze_buy", return_value=result), \
         patch("modules.auto_trade.api.get_chart_data", return_value=df), \
         patch("modules.auto_trade.api.get_realtime_vol_strength", return_value=vol_strength), \
         patch.object(AutoTrader, "_get_stock_market_type", return_value="KOSPI"):
        return trader._analyze_candidate_worker(
            item, holding_codes=set(), rules_map={}, restricted_stocks={},
            market_regime_adj={"KOSPI": 0.0}, safe_delay=0,
            reentry_hurdles=(hurdles or {}), holdings_dfs={}, holding_groups_map={},
            stop_exit_prices=(stop_prices or {}))


# ───────────────────── ① 손절가 게이트 ─────────────────────

def test_it_will_not_buy_back_at_or_above_todays_stop_price():
    """[핵심] 당일 손절가 위에서는 되사지 않는다 — 더 비싸게 되사기만 정확히 막는다."""
    px = float(_df()['close'].iloc[-1])
    res = _run(stop_prices={CODE: px - 1})           # 현재가 > 손절가
    assert res['type'] == 'log_only'
    assert "손절가 재진입 불가" in res['log']
    assert res['ledger']['outcome'] == 'reentry'


def test_below_the_stop_price_it_may_buy_again():
    """[대조군] 눌림에서 다시 잡히는 정상 재진입까지 막으면 추세추종에 역행한다."""
    px = float(_df()['close'].iloc[-1])
    assert _run(stop_prices={CODE: px + 1})['type'] == 'candidate'


def test_the_stop_price_gate_can_be_turned_off():
    px = float(_df()['close'].iloc[-1])
    with patch.object(config, 'REENTRY_BLOCK_ABOVE_STOP_PRICE', False):
        assert _run(stop_prices={CODE: px - 1})['type'] == 'candidate'


# ───────────────────── ② 체결강도 허들 ─────────────────────

def test_reentry_needs_to_beat_the_previous_entry_strength():
    """[핵심] 직전 매수의 체결강도 이하면 다시 사지 않는다."""
    res = _run(vol_strength=120.0, hurdles={CODE: 130.0})
    assert res['type'] == 'log_only'
    assert "당일 재진입 불가" in res['log']


def test_equal_strength_is_not_good_enough():
    """경계: '경신'이어야 한다 — 같은 값은 통과가 아니다(허들이 무동작이 된다)."""
    assert _run(vol_strength=130.0, hurdles={CODE: 130.0})['type'] == 'log_only'


def test_beating_the_hurdle_lets_it_back_in():
    res = _run(vol_strength=131.0, hurdles={CODE: 130.0})
    assert res['type'] == 'candidate'
    assert "당일 재진입" in res['data']['reentry_msg']


def test_unknown_strength_is_not_a_pass():
    """체결강도를 모르면 통과가 아니다 — 모르는 채로 되사지 않는다."""
    assert _run(vol_strength=None, hurdles={CODE: 130.0})['type'] == 'log_only'


def test_a_stock_not_sold_today_has_no_hurdle():
    """[대조군] 당일 매도가 없으면 허들 자체가 없다."""
    assert _run(vol_strength=1.0, hurdles={})['type'] == 'candidate'


# ───────────────── 토스 모드: 허들이 없다는 사실을 못 박는다 ─────────────────

def test_toss_has_no_self_raising_hurdle(monkeypatch):
    """[알려진 비대칭] 토스는 매도잔량비가 임계만 넘으면 같은 날 곧바로 되산다.

    체결강도가 없어 '직전 진입 강도 경신'을 요구할 수 없다. 이 테스트는 결함을
    승인하는 것이 아니라, 방어가 손절가 게이트 하나뿐이라는 사실을 드러내 둔다.
    """
    monkeypatch.setattr(config.session, 'is_toss', True, raising=False)
    res = _run(vol_strength=1.0, hurdles={CODE: 999.0}, abr=2.0, min_abr=1.0)
    assert res['type'] == 'candidate', "토스 동작이 바뀌었다 — 문서와 테스트를 함께 갱신할 것"


def test_toss_blocks_when_the_order_book_is_unknown(monkeypatch):
    """토스에서 실제로 걸리는 유일한 경우 — 호가를 못 구한 재진입은 보류한다."""
    monkeypatch.setattr(config.session, 'is_toss', True, raising=False)
    with patch("modules.auto_trade.api.get_ask_bid_ratio", return_value=None):
        res = _run(vol_strength=1.0, hurdles={CODE: 999.0}, abr=None, min_abr=1.0)
    assert res['type'] == 'log_only'
    assert "재진입 불가" in res['log']
