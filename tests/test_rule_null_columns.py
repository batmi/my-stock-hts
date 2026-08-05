"""개별 룰의 NULL 컬럼이 매도 판정을 죽이지 않게 한다.

[관측 2026-08-05] 가상투자에서 NAVER에 개별 룰(손절 -0.1%)을 걸자 그 종목만
[보유분석]에서 사라졌다. 손절 기준을 넘겨도 청산되지 않았고, 로그에는 스킵 사유조차
없었다. 룰이 없는 삼성 SDS는 멀쩡했다.

원인은 두 겹이다.
  1) build_sell_thresholds가 `rule.get('time_stop_days', 기본값)`을 썼다. dict.get은
     **키가 있고 값이 None이면 기본값이 아니라 None을 돌려준다.** stock_strategies는
     SELECT * 로 모든 컬럼을 싣고 오므로, 사용자가 지정하지 않은 항목이 정확히 그
     상태다. 그 None이 analyze_sell의 `if time_stop_days <= 0`에서 TypeError를 냈다.
  2) 매도 루프가 그 예외를 회수하지 않았다(concurrent.futures.wait은 예외를 되살리지
     않는다). 그래서 종목이 손절 대상에서 조용히 빠졌다.

여기서 고정하는 계약: **개별 룰이 있다고 해서 종목이 판정에서 사라지지 않는다.**
"""
import json

import numpy as np
import pandas as pd
import pytest

import config
from modules.auto_trade.engine import (DefaultStrategy, build_buy_thresholds,
                                       build_sell_thresholds)

# stock_strategies 스키마 전체 컬럼 — 사용자가 손절만 지정하고 나머지는 NULL인 상태.
# (메뉴에서 항목을 건너뛰면 실제로 이렇게 저장된다)
_RULE_ONLY_STOP_LOSS = {
    'code': '035420', 'name': 'NAVER',
    'buy_score': None, 'buy_rsi': None, 'buy_vol_strength': None,
    'sell_score': None, 'stop_loss': -0.1, 'take_profit': None,
    'take_profit_rsi': None, 'ts_activation': None, 'ts_callback': None,
    'updated_at': '2026-08-05', 'memo': None, 'weights': None,
    'invest_ratio': None, 'time_stop_days': None, 'use_atr_stop': None,
    'atr_stop_multiplier': None, 'half_take_profit_use': None,
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """스마트머니 판정은 실제 API를 호출한다.

    이 테스트의 관심사가 아닐 뿐 아니라, 실패하더라도 전역 API 스로틀의 토큰을 소모해
    같은 워커에 배정된 다른 테스트가 대기(time.sleep)에 걸린다. time.sleep을 전역
    패치하는 UI 테스트와 만나면 그쪽이 엉뚱하게 실패한다 — 병렬 실행 불안정의 원인이다.
    """
    from modules import analysis
    monkeypatch.setattr(analysis, 'check_smart_money_turnaround',
                        lambda code, is_overseas=False: (False, ""))


@pytest.fixture
def df():
    n = 300
    return pd.DataFrame({
        'open': np.linspace(200000, 230000, n),
        'high': np.linspace(201000, 231000, n),
        'low': np.linspace(199000, 229000, n),
        'close': np.linspace(200000, 230500, n),
        'volume': np.full(n, 1_000_000),
    })


def test_null_columns_fall_back_to_global_defaults():
    """[회귀 방지] NULL 컬럼은 None이 아니라 전역 기본값이 된다."""
    th = build_sell_thresholds(rule=dict(_RULE_ONLY_STOP_LOSS))

    nones = [k for k, v in th.items() if v is None]
    assert not nones, f"NULL 컬럼이 None으로 새어 나왔다: {nones} — 비교식에서 TypeError를 낸다"

    assert th["TIME_STOP_DAYS"] == config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20)
    assert th["BUY_RSI_MAX"] == config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    assert th["WEIGHTS"] == config.SCORING_WEIGHTS
    # 사용자가 실제로 지정한 값은 그대로 살아남아야 한다
    assert th["STOP_LOSS_RATE"] == -0.1


def test_buy_score_null_restores_market_regime_adjustment():
    """룰에 매수 기준이 없으면 룰 없는 종목과 같은 기준(전역 + 국면 보정)으로 돌아간다."""
    th = build_sell_thresholds(rule=dict(_RULE_ONLY_STOP_LOSS), score_adj=0.5)
    assert th["BUY_SCORE"] == config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + 0.5


def test_specified_values_still_override():
    """NULL 폴백이 사용자가 지정한 값을 덮지 않는다."""
    rule = dict(_RULE_ONLY_STOP_LOSS, time_stop_days=5, buy_rsi=60.0, sell_score=3.0)
    th = build_sell_thresholds(rule=rule)
    assert th["TIME_STOP_DAYS"] == 5
    assert th["BUY_RSI_MAX"] == 60.0
    assert th["SELL_SCORE"] == 3.0


def test_analyze_sell_survives_rule_with_null_columns(df):
    """[핵심] 개별 룰이 걸린 보유 종목도 판정 결과가 나온다 — 사라지지 않는다."""
    th = build_sell_thresholds(rule=dict(_RULE_ONLY_STOP_LOSS))
    res = DefaultStrategy().analyze_sell(
        '035420', 'NAVER', df, current_price=231000.0, buy_price=230500.0,
        profit_rate=0.22, thresholds=th, holding_days=0, highest_price=231000.0)

    assert res is not None, "개별 룰이 있는 종목이 판정 결과 없이 사라졌다"
    assert 'action' in res and 'score' in res


def test_json_string_weights_never_reach_scoring(df):
    """[회귀 방지 · 실제 원인] 가중치가 JSON 문자열로 남아도 판정이 죽지 않는다.

    가상투자는 db_manager만 paper DB로 갈아끼우고 config.DB_FILE_PATH는 실계좌 경로로
    남는다. 가중치 보강이 엉뚱한 DB를 열면 weights가 dict로 바뀌지 않고 문자열로 남아
    calculate_score의 weights.get()에서 AttributeError가 났다(2026-08-05 NAVER).
    """
    rule = dict(_RULE_ONLY_STOP_LOSS,
                time_stop_days=20, buy_score=7.0, buy_rsi=70.0, sell_score=4.0,
                take_profit=0.0, take_profit_rsi=0.0, ts_activation=10.0, ts_callback=5.0,
                weights=json.dumps({'TREND': 4.0, 'MOMENTUM': 2.5,
                                    'STRENGTH': 1.5, 'SYNERGY': 2.0}))

    th = build_sell_thresholds(rule=rule)
    assert isinstance(th["WEIGHTS"], dict), "가중치가 문자열인 채로 판정에 넘어간다"
    assert th["WEIGHTS"]["TREND"] == 4.0

    res = DefaultStrategy().analyze_sell(
        '035420', 'NAVER', df, current_price=231000.0, buy_price=230500.0,
        profit_rate=0.22, thresholds=th, holding_days=0, highest_price=231000.0)
    assert res is not None, "가중치 문자열 때문에 종목이 판정에서 사라졌다"


def test_calculate_score_tolerates_string_weights():
    """점수 계산 자체도 문자열 가중치를 견딘다 (매수 경로 등 다른 호출부 방어)."""
    from modules import analysis
    n = 300
    d = pd.DataFrame({
        'open': np.linspace(100, 130, n), 'high': np.linspace(101, 131, n),
        'low': np.linspace(99, 129, n), 'close': np.linspace(100, 130, n),
        'volume': np.full(n, 1_000_000),
    })
    import indicators
    ind = indicators.calculate_indicators(d)
    w = json.dumps({'TREND': 4.0, 'MOMENTUM': 2.5, 'STRENGTH': 1.5, 'SYNERGY': 2.0})
    score, _ = analysis.calculate_score(df=d, ind=ind, weights=w)
    assert isinstance(score, (int, float))


def test_buy_thresholds_share_the_same_contract(df):
    """매수 경로도 같은 규약을 쓴다 — 개별 룰 종목이 종목분석에서 사라지면 안 된다."""
    rule = dict(_RULE_ONLY_STOP_LOSS,
                weights=json.dumps({'TREND': 4.0, 'MOMENTUM': 2.5,
                                    'STRENGTH': 1.5, 'SYNERGY': 2.0}))
    th = build_buy_thresholds(rule=rule, score_adj=0.5)

    nones = [k for k, v in th.items() if v is None]
    assert not nones, f"매수 임계값에 None이 새어 나왔다: {nones}"
    assert isinstance(th["WEIGHTS"], dict), "가중치가 문자열인 채로 매수 판정에 넘어간다"
    # 룰에 매수 기준이 없으면 전역 + 국면 보정으로 돌아간다
    assert th["BUY_SCORE"] == config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + 0.5

    res = DefaultStrategy().analyze_buy('035420', 'NAVER', df, 231000.0, thresholds=th)
    assert res is not None, "개별 룰이 있는 종목이 매수 분석에서 사라졌다"


def test_buy_thresholds_keep_explicit_rule_values():
    """지정한 매수 기준은 시장 국면 보정을 무시하는 절대값으로 유지된다."""
    rule = dict(_RULE_ONLY_STOP_LOSS, buy_score=7.0, buy_rsi=70.0)
    th = build_buy_thresholds(rule=rule, score_adj=0.5)
    assert th["BUY_SCORE"] == 7.0
    assert th["BUY_RSI_MAX"] == 70.0


def test_weights_enrichment_uses_active_db():
    """[회귀 방지] 가중치 보강·저장은 지금 열린 DB를 쓴다(가상투자 DB 분리 유지)."""
    import os
    from modules.auto_trade import common
    import modules.db_manager as db_manager

    active = common._active_db_path()
    assert active == getattr(db_manager.db, 'db_path', None) or active == config.DB_FILE_PATH

    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "modules/auto_trade/common.py"), encoding="utf-8").read()
    head = src[src.find("def _ensure_db_weights_column_logic"):src.find("def _enrich_rules_with_weights(")]
    assert "sqlite3.connect(config.DB_FILE_PATH)" not in head, (
        "가중치 헬퍼가 다시 config.DB_FILE_PATH를 직접 연다 — "
        "가상투자에서 실계좌 DB를 읽고 쓰게 된다")


def test_rule_stop_loss_actually_triggers(df):
    """룰의 손절률(-0.1%)이 실제로 청산 판정을 낸다. (강제 손절 리허설의 전제)"""
    th = build_sell_thresholds(rule=dict(_RULE_ONLY_STOP_LOSS))
    # ATR 손절이 룰을 덮지 않도록 룰에서 명시적으로 끈 경우를 함께 확인한다
    th["USE_ATR_STOP"] = False
    res = DefaultStrategy().analyze_sell(
        '035420', 'NAVER', df, current_price=230000.0, buy_price=230500.0,
        profit_rate=-0.22, thresholds=th, holding_days=0, highest_price=230500.0)

    assert res is not None
    assert res['action'] == 'sell', f"손절률 -0.1%를 넘겼는데 청산 판정이 아니다: {res.get('reason')}"
