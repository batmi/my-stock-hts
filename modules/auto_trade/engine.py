# modules/auto_trade/engine.py
"""매매 엔진: DefaultStrategy(매수/매도 판단) · OrderManager(주문 집행) · RiskManager(리스크 관리)

기존 modules/auto_trade.py 에서 분해. 외부 인터페이스는 패키지(__init__)가 재수출한다.
"""
import threading
import concurrent.futures
import logging
import time
import requests
import json
import jsonio
import os
import sqlite3 # [추가] DB 직접 접근용
from datetime import datetime, timedelta
from collections import Counter
from rich.prompt import Prompt
from rich.markup import escape
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
import config
import context # [추가]
import api
import utils
import indicators
from modules import analysis, account # [수정] account 모듈 재사용
import math # [추가] math 모듈
from modules import db_manager # [추가] DB 매니저
from modules import chart # [추가] 차트 모듈
import re # [추가] 정규식 모듈
import pandas as pd

from modules.auto_trade.common import (OrderStatus, get_mystock_log_tail, register_system_odno)

console = config.console

logger = logging.getLogger(__name__)


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자.

    분해 전에는 모듈 전역 조회였던 상호 호출을 패키지 속성 조회로 유지해,
    테스트의 patch('modules.auto_trade.X') 가 분해 전과 동일하게 내부 호출에도
    적용되도록 한다. (지연 import라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


# 시스템 자동 매도 대상에서 빠지는 사유. 매도 루프의 스킵 로그·경보와 잔고 화면 표기가
# 같은 문구를 쓰도록 상수로 둔다.
UNMANAGED_RESTRICTED = "트레이딩 제한"
UNMANAGED_ETF = "ETF 제외 설정"
UNMANAGED_BAD_PRICE = "현재가 이상(판정 불가)"
UNMANAGED_OVERSEAS = "해외 미지원"


def get_unmanaged_reason(code, name="", is_overseas=False, restricted_codes=None):
    """시스템 자동 매도 대상에서 제외되는 사유를 반환한다. 대상이면 None.

    _check_sell_conditions의 스킵 분기와 같은 기준이며, 장중 특정 시간대에만 걸리는
    일시적 스킵(진행 중 주문·NXT 비거래 시간)은 포함하지 않는다.

    해외 종목이 항상 제외인 이유: 매도 루프는 국내 잔고(get_domestic_balance)만 순회한다.
    해외 포지션은 손절을 포함해 전량 수동 관리 대상이다.
    """
    if restricted_codes and code in restricted_codes:
        return UNMANAGED_RESTRICTED
    if is_overseas:
        return UNMANAGED_OVERSEAS
    if api.is_domestic_etf_etn(code, name) and not getattr(config, 'SYSTEM_INCLUDE_ETF', False):
        return UNMANAGED_ETF
    return None


def giveback_callback_cap(max_profit_rate, giveback_ratio):
    """'최고 수익의 giveback_ratio 이상은 반납하지 않는다'를 만족하는 콜백 상한(%)을 구한다.

    [Fix] 기존 식은 `max_profit_rate × ratio`를 그대로 콜백 상한으로 썼는데, 콜백은 '최고가'
    대비 비율이고 max_profit_rate는 '매수가' 대비 비율이라 기준이 달랐다. 수익이 커질수록
    상한이 과대평가되어(예: MFE +108%, ratio 0.30 → 상한 32.5%, 실제 반납 60%p 이상 허용)
    캡이 사실상 무력화됐다. 두 기준을 정확히 환산한다.

        청산가 = 매수가 × (1 + MFE(1-R)/100),  최고가 = 매수가 × (1 + MFE/100)
        상한   = 1 - 청산가/최고가 = MFE·R / (100 + MFE)

    (같은 MFE +108%, R 0.30이면 32.5%가 아니라 15.6%가 정답)
    """
    if max_profit_rate <= 0 or giveback_ratio <= 0:
        return 0.0
    return (max_profit_rate * giveback_ratio) / (100.0 + max_profit_rate) * 100.0


def compute_trailing_stop(highest_price, buy_price, current_price, ind=None, thresholds=None,
                          ts_activation=None, ts_callback=None, ts_atr_mult=None, use_atr_stop=None):
    """샹들리에 트레일링 스탑 발동선을 계산한다. (순수 함수 · 부수효과 없음)

    주청산 로직인 TS 콜백 산식의 단일 소스. analyze_sell의 청산 판정과 잔고 화면의
    'TS 청산가' 표시가 같은 식을 쓰도록 분리했다(표시선 ≠ 실제 청산선 방지).

    반환: None(계산 불가) 또는
      {'armed'(발동 조건 도달), 'triggered'(청산 조건 충족), 'stop_price'(청산선),
       'callback'(적용 콜백%), 'drop_rate'(최고가 대비 하락%), 'max_profit_rate', 'activation'}
    """
    if not (highest_price and buy_price) or highest_price <= 0 or buy_price <= 0:
        return None

    t = thresholds or {}
    ss = config.SELL_STRATEGY
    if use_atr_stop is None:
        use_atr_stop = t.get("USE_ATR_STOP", ss.get("USE_ATR_STOP", True))
    if ts_activation is None:
        ts_activation = t.get("ts_activation", ss.get("TRAILING_STOP_ACTIVATION_RATE", 10.0))
    if ts_callback is None:
        ts_callback = t.get("ts_callback", ss.get("TRAILING_STOP_CALLBACK_RATE", 5.0))
    if ts_atr_mult is None:
        ts_atr_mult = t.get("TRAILING_ATR_MULTIPLIER", ss.get("TRAILING_ATR_MULTIPLIER", 3.0))

    max_profit_rate = ((highest_price - buy_price) / buy_price) * 100
    drop_rate = ((highest_price - current_price) / highest_price) * 100

    actual_callback = ts_callback
    atr_val = (ind.get('atr') if ind else None) or 0
    if use_atr_stop and atr_val > 0:
        dynamic_callback = (atr_val * ts_atr_mult / highest_price) * 100

        # [리스크 관리 방어 로직]
        # 1. 하한선: 너무 작은 변동성으로 인한 조기 털림 방지 (기본 ts_callback 보장)
        # 2. 상한선: ATR이 너무 커서 도달한 최대 수익의 일정 비율 이상을 반납하는 것 방지.
        #    TS_MAX_GIVEBACK_RATIO ≤ 0 이면 상한 캡 해제(순수 샹들리에).
        giveback_ratio = ss.get("TS_MAX_GIVEBACK_RATIO", 0.0)
        if giveback_ratio > 0:
            actual_callback = min(max(ts_callback, dynamic_callback),
                                  max(ts_callback, giveback_callback_cap(max_profit_rate, giveback_ratio)))
        else:
            actual_callback = max(ts_callback, dynamic_callback)

    armed = max_profit_rate >= ts_activation
    return {
        'armed': armed,
        'triggered': bool(armed and drop_rate >= actual_callback),
        'stop_price': highest_price * (1 - actual_callback / 100),
        'callback': actual_callback,
        'drop_rate': drop_rate,
        'max_profit_rate': max_profit_rate,
        'activation': ts_activation,
    }


def atr_stop_rate(atr, price, atr_mult=None, max_cap=None):
    """ATR 손절률(%, 음수)을 구한다. 산출 불가하면 None. (부수효과 없음)

    매수 체결 시 trades.stop_loss_rate에 굳는 값과 같은 식이다. 신규 매수·피라미딩·
    보유 분석이 각자 같은 식을 복제하고 있어 캡(MAX_ATR_STOP_LOSS_RATE) 적용 여부가
    갈릴 위험이 있어 SSOT로 모은다.
    """
    try:
        atr = float(atr or 0)
        price = float(price or 0)
    except (TypeError, ValueError):
        return None
    if atr <= 0 or price <= 0:
        return None

    if atr_mult is None:
        atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    if not atr_mult:
        return None

    rate = -((atr * atr_mult / price) * 100)
    if max_cap is None:
        max_cap = config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)
    if max_cap and rate < max_cap:
        rate = max_cap
    return rate


def entry_atr_stop_rate(df, entry_date=None, atr_mult=None):
    """진입 시점 봉의 ATR로 손절률을 복원한다. 근거가 없으면 None. (부수효과 없음)

    HTS·MTS로 직접 매수한 포지션은 trades에 매수 기록이 없어 매수 시점 ATR 손절률이
    남아 있지 않다. 그러면 USE_ATR_STOP이 켜져 있어도 판정이 전역 고정 손절률로
    떨어져, 변동성이 큰 종목이 좁은 고정폭에서 잘려 나간다. 진입일 봉의 ATR로
    '매수 당시 기록됐을 값'을 복원해 시스템 매수분과 같은 기준으로 맞춘다.

    진입일을 모르면 최신 봉의 ATR을 쓴다(현재 변동성 기준의 근사).
    """
    if df is None or df.empty or 'close' not in df.columns:
        return None
    try:
        atr_series = indicators.get_atr_full_series(df)
        if atr_series is None or atr_series.empty:
            return None

        pos = len(df) - 1
        if entry_date is not None:
            dates = pd.to_datetime(df['date']) if 'date' in df.columns else pd.to_datetime(df.index)
            if getattr(dates.dt, 'tz', None) is not None:
                dates = dates.dt.tz_localize(None)
            dates = dates.dt.normalize()

            since = pd.Timestamp(entry_date)
            if since.tz is not None:
                since = since.tz_localize(None)
            since = since.normalize()

            # 진입일 당일 봉(없으면 직후 첫 봉). 차트가 진입일까지 거슬러 가지 못하면
            # 최신 봉으로 두어 조용히 폴백한다.
            mask = (dates >= since).values
            if mask.any():
                pos = int(mask.argmax())

        atr_val = float(atr_series.iloc[pos])
        price_val = float(df['close'].iloc[pos])
        return atr_stop_rate(atr_val, price_val, atr_mult=atr_mult)
    except Exception as e:
        logger.debug(f"진입 시점 ATR 손절률 복원 실패: {e}")
        return None


def build_sell_thresholds(rule=None, score_adj=0.0, buy_trades=None, fallback_atr_rate=None):
    """보유 종목의 매도 판단(analyze_sell)에 넘길 임계값을 조립한다. (부수효과 없음)

    시스템 트레이딩 루프(_check_sell_conditions)와 잔고 화면의 보유 분석이 같은
    임계값을 쓰도록 SSOT로 둔다. 개별 룰 > ATR 수량가중 손절 > 전역 설정 순으로 덮어쓴다.

    fallback_atr_rate: 매수 기록이 없어 ATR 손절률을 못 구할 때 쓸 복원값
                       (entry_atr_stop_rate). 기록에서 구한 값이 항상 우선한다.
    """
    if rule:
        thresholds = {
            "TAKE_PROFIT_RATE": rule['take_profit'],
            "STOP_LOSS_RATE": rule['stop_loss'],
            "TAKE_PROFIT_RSI": rule['take_profit_rsi'],
            "SELL_SCORE": rule['sell_score'],
            "WEIGHTS": rule.get('weights'),
            "BUY_SCORE": rule['buy_score'],
            # [Fix] 개별 룰의 RSI 상한을 매도 경로에도 전달한다.
            #  analyze_sell도 classify_stock_state로 상태를 재판정하는데,
            #  이 키가 없으면 전역 BUY_RSI_MAX로 폴백해, 같은 종목·같은 시각인데도
            #  매수 경로/메뉴 2 화면과 상태가 갈렸다(룰 RSI ≠ 전역 RSI인 보유 종목).
            "BUY_RSI_MAX": rule['buy_rsi'],
            "TIME_STOP_DAYS": rule.get('time_stop_days', config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20)),
            "HALF_TAKE_PROFIT_USE": bool(rule.get('half_take_profit_use', config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False))),
            # [Fix] 개별 룰의 TS 발동/콜백을 analyze_sell에 실제로 전달
            "ts_activation": rule['ts_activation'] if rule.get('ts_activation') is not None else config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0),
            "ts_callback": rule['ts_callback'] if rule.get('ts_callback') is not None else config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0),
        }
        # [Fix] 룰의 ATR 손절 사용 여부를 TS 동적 콜백(샹들리에) 판정에도 일관 적용
        if rule.get('use_atr_stop') is not None:
            thresholds["USE_ATR_STOP"] = bool(rule['use_atr_stop'])
    else:
        thresholds = {
            "WEIGHTS": config.SCORING_WEIGHTS,
            "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj,
            "TIME_STOP_DAYS": config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20),
        }

    use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
    if rule and rule.get('use_atr_stop') is not None:
        use_atr_stop = bool(rule['use_atr_stop'])

    applied_sl_rate = None
    if use_atr_stop and buy_trades:
        # [Fix: Point 4] 분할 매수를 고려하여, 현재 보유량에 해당하는 모든 매수 기록의
        # ATR 손절률을 수량 가중 평균하여 적용합니다.
        total_qty_trade = 0
        weighted_sl_sum = 0
        for trade in buy_trades:
            qty_trade = api.safe_int(trade.get('qty', 0))
            sl_rate_trade = float(trade.get('stop_loss_rate') or 0.0)
            if qty_trade > 0 and sl_rate_trade != 0.0:
                total_qty_trade += qty_trade
                weighted_sl_sum += qty_trade * sl_rate_trade

        if total_qty_trade > 0:
            avg_sl_rate = weighted_sl_sum / total_qty_trade
            if avg_sl_rate != 0.0:
                applied_sl_rate = avg_sl_rate

    # [Fix] 매수 기록이 없는 포지션(HTS·MTS 직접 매수)은 여기서 전역 고정 손절률로
    #  떨어졌다. USE_ATR_STOP이 켜져 있는데 실제 판정만 고정폭으로 도는 셈이라,
    #  진입 시점 봉에서 복원한 ATR 손절률로 시스템 매수분과 기준을 맞춘다.
    if applied_sl_rate is None and use_atr_stop and fallback_atr_rate is not None:
        try:
            fb = float(fallback_atr_rate)
        except (TypeError, ValueError):
            fb = 0.0
        if fb < 0:
            applied_sl_rate = fb

    if applied_sl_rate is not None:
        thresholds["STOP_LOSS_RATE"] = applied_sl_rate
        # 손절 사유 표기('ATR손절')·화면 표시용 마커. analyze_sell은 이 키를 읽지 않는다.
        thresholds["ATR_APPLIED_SL_RATE"] = applied_sl_rate

        # [Fix] ATR 동적 손절 사용 시, 본전 청산(BEP) 발동 기준을 손절폭(절대값)과 1:1로 동기화
        #  (기본 +5%에 조기 발동하면 ATR 손절폭이 넓은 변동성 종목이 정상 눌림에서 조기 청산된다)
        if applied_sl_rate < 0:
            thresholds["BREAK_EVEN_PROFIT_RATE"] = abs(applied_sl_rate)

    return thresholds


def highest_since(df, since_date):
    """일봉에서 since_date(포함) 이후의 최고가를 구한다. 근거가 없으면 None.

    시스템이 감시하지 않은 수동 포지션에는 trailing_stops 기록이 없어, 트레일링 스탑
    앵커(최고가)를 매수일 이후 실제 고가에서 유도한다.
    """
    if df is None or df.empty or since_date is None or 'high' not in df.columns:
        return None
    try:
        dates = pd.to_datetime(df['date']) if 'date' in df.columns else pd.to_datetime(df.index)

        # [Fix] tvDatafeed 경로 차트는 date가 tz-aware(Asia/Seoul)라 naive 기준일과 비교 시
        #  TypeError가 난다. 양쪽을 tz 없는 '날짜'로 정규화해 비교한다.
        #  (예외를 삼키면 앵커가 조용히 사라져 해외 종목의 TS가 통째로 빠졌다)
        if getattr(dates.dt, 'tz', None) is not None:
            dates = dates.dt.tz_localize(None)
        dates = dates.dt.normalize()

        since = pd.Timestamp(since_date)
        if since.tz is not None:
            since = since.tz_localize(None)
        since = since.normalize()

        highs = df.loc[(dates >= since).values, 'high']
        if highs.empty:
            return None
        val = float(highs.max())
        return val if val > 0 else None
    except Exception as e:
        logger.debug(f"highest_since 계산 실패: {e}")
        return None


def resolve_entry_date(entry_date=None, last_buy=None, fallback_buy_date=None):
    """현재 보유 포지션의 진입일을 확정한다. 'YYYY-MM-DD' 또는 None.

    [중요] 진입일은 '최근 매수일'이 아니라 '보유수량이 0에서 1 이상으로 바뀐 시점'이다.
    분할 매수·피라미딩으로 1주만 더 담아도 최근 매수 기준이면 보유일수가 0으로 리셋되어
    시간청산 시계가 무한히 미뤄진다. 시간청산의 취지는 '자본이 얼마나 오래 묶였나'이므로
    수량 흐름으로 잰다(db_manager.get_position_entry_dates).

    우선순위: 수량 재생으로 구한 진입일 → 최근 매수 기록 → 증권사 체결 내역(HTS 직접 매수분).
    """
    if entry_date:
        return str(entry_date)[:10]

    if last_buy and last_buy.get('time'):
        return str(last_buy['time'])[:10]

    if fallback_buy_date:
        try:
            s = (fallback_buy_date if isinstance(fallback_buy_date, str)
                 else fallback_buy_date.strftime("%Y%m%d")).replace('-', '').strip()
            if len(s) == 8 and s.isdigit():
                return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        except Exception:
            pass

    return None


def resolve_holding_context(last_buy, fallback_buy_date=None, entry_date=None):
    """(보유일수, 역추세 보유 여부)를 유도한다. (부수효과 없음)

    보유일수는 진입일(resolve_entry_date) 기준이며, 어디서도 매수일을 찾지 못하면
    0일(오늘 매수)로 본다. 역추세 보유 여부는 진입 성격이므로 최근 매수 사유로 판정한다.
    """
    is_mr_holding = False
    if last_buy:
        reason_str = str(last_buy.get('reason', ''))
        if '역매수' in reason_str or '역추세' in reason_str:
            is_mr_holding = True

    holding_days = 0
    resolved = resolve_entry_date(entry_date, last_buy, fallback_buy_date)
    if resolved:
        try:
            buy_d = datetime.strptime(resolved, "%Y-%m-%d").date()
            holding_days = max(0, (datetime.now().date() - buy_d).days)
        except Exception:
            pass

    return holding_days, is_mr_holding


def analyze_holdings(entries, max_workers=None, restricted_codes=None):
    """보유 종목에 시스템 매도 판단(analyze_sell)을 그대로 적용한다. (읽기 전용)

    시스템 트레이딩 루프와 달리 DB 최고가 갱신·주문·상태 캐시 변경 등 부수효과가 전혀 없어
    자동매매 미실행 상태나 수동 매수 계좌에서도 안전하게 호출할 수 있다.

    restricted_codes를 넘기면 각 결과에 'unmanaged'(자동 매도 제외 사유)를 채운다.
    청산 신호가 떠도 시스템이 팔지 않는 포지션을 화면에서 구분하기 위한 정보다.

    entries: [{'code', 'name', 'buy_price', 'current_price', 'profit_rate', 'is_overseas'}]
    반환: {code: analyze_sell 결과 + holding_days/highest_price/has_rule/unmanaged}
    """
    results = {}
    if not entries:
        return results

    codes = [e['code'] for e in entries]

    def _safe(fn, default):
        try:
            return fn()
        except Exception as e:
            logger.debug(f"보유분석 사전 로드 실패({fn}): {e}")
            return default

    rules_list = _safe(lambda: _pkg()._enrich_rules_with_weights(db_manager.db.get_all_stock_strategies()), [])
    rules_map = {r['code']: r for r in rules_list}
    latest_buy_map = _safe(lambda: db_manager.db.get_latest_buy_trades(codes), {})
    buy_trades_map = _safe(lambda: db_manager.db.get_buy_trades_for_current_holdings(codes), {})
    highest_map = _safe(lambda: db_manager.db.get_all_trailing_stops(), {})
    half_tp_set = _safe(lambda: db_manager.db.get_all_half_tp(), set())
    # 진입일(보유수량이 0 → 1 이상이 된 시점). 분할 매수·부분 매도가 섞여도 정확하다.
    entry_date_map = _safe(lambda: db_manager.db.get_position_entry_dates(codes), {})

    # [추가] HTS·MTS 직접 매수분은 시스템 DB에 매수 기록이 없다. 증권사 체결 내역에서
    #  실제 매수일을 복원해 보유일수·시간청산 판정이 '오늘 매수'로 굳는 것을 막는다.
    #  (기간 단위 조회라 보유 종목 수와 무관하게 호출 수가 고정된다)
    missing = [e['code'] for e in entries
               if not e.get('is_overseas') and e['code'] not in entry_date_map
               and e['code'] not in latest_buy_map and e.get('holding_days') is None]
    broker_buy_dates = _safe(lambda: api.get_period_buy_dates(missing), {}) if missing else {}

    # 시장 국면 보정 (매수 임계값 → 상태 분류에 반영). 매도 분석 경로와 동일하게 적용한다.
    market_regime_adj = {}
    if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
        for m_type in ("KOSPI", "KOSDAQ"):
            try:
                _regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj
            except Exception:
                market_regime_adj[m_type] = 0.0

    strategy = DefaultStrategy()
    market_cache = {}

    def _worker(entry):
        code = entry['code']
        try:
            is_overseas = bool(entry.get('is_overseas'))
            buy_price = float(entry.get('buy_price') or 0)
            current_price = float(entry.get('current_price') or 0)
            profit_rate = float(entry.get('profit_rate') or 0)
            if buy_price <= 0 or current_price <= 0:
                return code, None

            rule = rules_map.get(code)
            score_adj = 0.0
            if not is_overseas:
                m_type = _pkg().resolve_market_type(code, market_cache)
                score_adj = market_regime_adj.get(m_type, 0.0)

            last_buy = latest_buy_map.get(code)
            broker_date = broker_buy_dates.get(code)
            entry_date = resolve_entry_date(entry_date_map.get(code), last_buy, broker_date)
            holding_days, is_mr_holding = resolve_holding_context(
                last_buy, fallback_buy_date=broker_date, entry_date=entry_date_map.get(code))
            has_buy_record = entry_date is not None

            # [수동 분석] 계좌에 없는 가상 포지션은 DB에 매수 기록이 없으므로 입력값을 우선한다.
            if entry.get('holding_days') is not None:
                holding_days = int(entry['holding_days'])
                has_buy_record = True

            df = api.get_chart_data(code, is_overseas=is_overseas)
            # [일관성] 매도 분석 경로와 동일하게 당일 봉을 실시간가로 덮어 지표 불일치를 막는다.
            #  (장 종료 후에는 chart_overlay_price가 KRX 확정 종가를 유지한다)
            indicators.apply_realtime_price(df, api.chart_overlay_price(current_price, is_overseas))

            # 임계값 조립은 차트 확보 후. 매수 기록이 없는 포지션은 진입일 봉의 ATR에서
            # 손절률을 복원해야 하므로 df가 먼저 필요하다.
            thresholds = build_sell_thresholds(
                rule=rule, score_adj=score_adj, buy_trades=buy_trades_map.get(code),
                fallback_atr_rate=entry_atr_stop_rate(df, entry_date))

            # [읽기 전용] 트레이더와 달리 최고가를 DB에 기록하지 않는다. 다만 표시할 TS 라인이
            #  실제 청산선과 어긋나지 않도록, 현재가가 기록된 최고가를 넘었으면 현재가를 쓴다.
            highest_price = float(highest_map.get(code) or 0.0)
            if entry.get('highest_price') is not None:
                highest_price = float(entry['highest_price'])
            elif entry.get('highest_since') is not None:
                # [수동 분석] 입력한 매수일이 시스템 DB 기록보다 우선한다.
                derived = highest_since(df, entry['highest_since'])
                if derived:
                    highest_price = derived
            elif highest_price <= 0 and entry_date:
                # 시스템이 감시하지 않은 포지션(HTS 직접 매수 등)은 trailing_stops 기록이
                # 없다. 진입일 이후 실제 고가에서 TS 앵커를 유도한다.
                derived = highest_since(df, entry_date)
                if derived:
                    highest_price = derived
            if current_price > buy_price and current_price > highest_price:
                highest_price = current_price

            res = strategy.analyze_sell(
                code, entry.get('name', ''), df, current_price, buy_price, profit_rate,
                thresholds=thresholds, already_half_sold=(code in half_tp_set),
                holding_days=holding_days, is_mr_holding=is_mr_holding,
                highest_price=highest_price,
            )
            res['holding_days'] = holding_days
            res['highest_price'] = highest_price
            res['has_rule'] = bool(rule)
            res['is_mr_holding'] = is_mr_holding
            # 수동 매수 등 DB 매수 기록이 없으면 보유일수가 0으로 나오므로 표시부에서 구분한다
            res['has_buy_record'] = has_buy_record
            res['unmanaged'] = get_unmanaged_reason(code, entry.get('name', ''),
                                                    is_overseas=is_overseas,
                                                    restricted_codes=restricted_codes)
            return code, res
        except Exception as e:
            logger.debug(f"보유분석 실패 {code}: {e}")
            return code, None

    if max_workers is None:
        max_workers = 2 if config.session.is_simulation else 4
    max_workers = max(1, min(max_workers, len(entries)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for code, res in executor.map(_worker, entries):
            if res:
                results[code] = res

    return results


class DefaultStrategy:
    """기본 매매 전략 클래스 (매수/매도 판단 로직 분리)"""
    def __init__(self):
        self.trailing_stop_cache = {}

    def analyze_buy(self, code, name, df, current_price, vol_strength=None, thresholds=None, ask_bid_ratio=None):
        """매수 진입 여부 판단"""
        if df is None or df.empty:
            return None

        ind = indicators.calculate_indicators(df)
        # 전일 RSI (상태 분류용) — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
        prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None
        
        # [추가] 52주 위치 계산 (슈퍼 모멘텀 판정용)
        w52_pos = 0.0
        if df is not None and not df.empty:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100

        # [추세추종] 추세 품질(회귀 모멘텀 = 연환산 기울기 × R²) — 동시 매수 후보 간 우선순위 랭킹용.
        #   매수 게이트(점수/상태)에는 관여하지 않으며, 이력 부족 시 None(랭킹 최하순위).
        trend_quality = indicators.get_trend_quality(df)

        is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
        sm_flag, sm_reason = analysis.check_smart_money_turnaround(code, is_overseas)

        state, _, state_reason = analysis.classify_stock_state(
            df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
        )
        
        # [SSOT] w52_pos를 반드시 함께 넘긴다. 넘기지 않으면 calculate_score가 내부 폴백으로
        #  _w52_band(365 달력일)를 쓰는데, 바로 위 classify_stock_state에는 tail(250 거래일)로
        #  계산한 값을 넘기고 있어 같은 시점에 52주 위치가 두 개 존재하게 된다. 그러면 가격
        #  모멘텀 팩터(+0.5, 임계 80%)가 상태와 점수에서 다르게 매겨진다.
        score, _ = analysis.calculate_score(
            df=df, ind=ind, weights=thresholds.get('WEIGHTS') if thresholds else None,
            smart_money=sm_flag, w52_pos=w52_pos
        )
        score = round(score, 1)

        # [수정] 역매수 상태에 따른 체결강도 분기
        if state == "역매수":
            min_vol = thresholds.get("MR_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
        else:
            min_vol = thresholds.get("BUY_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        
        # [수정] 체결강도 미달 및 가짜 체결강도(호가창 비대칭성) 필터링
        is_vol_ok = True
        vol_reject_reason = ""
        min_ask_bid_ratio = thresholds.get("BUY_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)

        # [추가] 체결강도 100% 기준으로 매도잔량비 자동 비례 계산
        auto_adjust = thresholds.get("AUTO_ADJUST_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True)) if thresholds else config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True)
        
        if auto_adjust and min_ask_bid_ratio > 0 and min_vol > 0:
            ratio_multiplier = min_vol / 100.0
            min_ask_bid_ratio = round(min_ask_bid_ratio * ratio_multiplier, 2)
        
        if vol_strength is not None:
            if vol_strength < min_vol:
                is_vol_ok = False
                vol_reject_reason = f"체결:{vol_strength:.1f}%<{min_vol}%"
            elif ask_bid_ratio is not None and min_ask_bid_ratio > 0:
                # [핵심] 가짜 체결강도 방어 (호가창 매도잔량 비대칭성 확인)
                # 매도 잔량이 매수 잔량보다 최소 기준치 이상 많아야 진짜 상승 에너지로 판단
                if ask_bid_ratio < min_ask_bid_ratio:
                    is_vol_ok = False
                    vol_reject_reason = f"매도비:{ask_bid_ratio:.2f}<{min_ask_bid_ratio}"
        elif config.session.is_toss:
            # [추가] 토스: 체결강도 미제공 → 호가창 매도잔량비(ask_bid_ratio)만으로 수급 게이트 대체
            #   호가 조회가 실패해 ratio가 없으면 상태(state) 게이트만으로 진입(거래 중단 방지)
            if ask_bid_ratio is not None and min_ask_bid_ratio > 0 and ask_bid_ratio < min_ask_bid_ratio:
                is_vol_ok = False
                vol_reject_reason = f"매도비:{ask_bid_ratio:.2f}<{min_ask_bid_ratio}"
        elif min_vol > 0 and not is_overseas:
            # [Fix 2026-07-27 / fail-closed] KIS 국내 종목에서 체결강도를 못 구한 경우(정규장
            #  J 무효·EGW00201 스로틀 실패 등)는 수급 게이트를 '통과'시키던 종전 동작이 fail-open이었다.
            #  체결강도 기준(min_vol>0)을 켜 둔 이상 '확인 못 함'은 '충족'이 아니므로 보류한다.
            #  값은 캐시되지 않아 다음 분석 주기에 다시 조회된다.
            #  (min_vol==0은 사용자가 게이트를 끈 것, 해외 종목은 KIS가 체결강도를 제공하지 않아
            #   애초에 이 게이트의 대상이 아니므로 종전대로 통과시킨다)
            is_vol_ok = False
            vol_reject_reason = "체결강도 미확인(보류)"

        return {
            'action': 'buy' if (state in ["매수", "강매수", "역매수"] and is_vol_ok) else 'wait',
            'state': state,
            'state_reason': state_reason,
            'score': score,
            'rsi': ind['rsi'],
            'adx': ind['adx'],
            'cci': ind['cci'],
            'atr': ind.get('atr', 0), # [추가] ATR
            'psar': ind['psar'],
            'macd': ind.get('macd'),
            'macd_signal': ind.get('macd_signal'),
            'obv_trend': ind.get('obv_trend'),
            'vol_strength': vol_strength,
            'ask_bid_ratio': ask_bid_ratio,
            'min_ask_bid_ratio': min_ask_bid_ratio,  # [추가] 재진입 허들 등에서 재사용
            'vol_reject_reason': vol_reject_reason,
            'smart_money': sm_flag,
            'w52_pos': w52_pos,  # [추세추종] 매수 후보 우선순위(강한 종목 우선) 정렬용 52주 위치
            'trend_quality': trend_quality  # [추세추종] 추세 품질(회귀 모멘텀) — 후보 랭킹 1순위 키
        }

    def analyze_pyramid(self, profit_rate, state, score, pyramid_count, thresholds=None):
        """[추세추종] 수익 포지션 증액(피라미딩) 여부 판단

        물타기의 정반대: 수익으로 추세가 검증된 포지션에만, 추세가 유지되는 동안 증액한다.
        반환: (증액 여부, 사유 문자열)
        """
        at = config.ANALYSIS_THRESHOLDS
        use = thresholds.get("PYRAMIDING_USE", at.get("PYRAMIDING_USE", True)) if thresholds else at.get("PYRAMIDING_USE", True)
        if not use:
            return False, ""

        trigger = thresholds.get("PYRAMIDING_PROFIT_TRIGGER", at.get("PYRAMIDING_PROFIT_TRIGGER", 10.0)) if thresholds else at.get("PYRAMIDING_PROFIT_TRIGGER", 10.0)
        max_count = thresholds.get("PYRAMIDING_MAX_COUNT", at.get("PYRAMIDING_MAX_COUNT", 1)) if thresholds else at.get("PYRAMIDING_MAX_COUNT", 1)

        if pyramid_count >= max_count:
            return False, ""
        if profit_rate < trigger:
            return False, ""
        # 추세 유지 확인: 신규 진입과 동일한 '매수' 신호가 살아있어야 증액
        if state not in ("매수", "강매수"):
            return False, ""

        return True, f"피라미딩 {pyramid_count + 1}차 (수익률:+{profit_rate:.1f}%, 점수:{score}, 상태:{state})"

    def analyze_sell(self, code, name, df, current_price, buy_price, profit_rate, thresholds=None, already_half_sold=False, holding_days=0, is_mr_holding=False, highest_price=0.0):
        """매도 청산 여부 판단"""
        reason = ""
        ind = {}
        score = 0
        state = ""
        state_color = "[white]"
        sell_ratio = 1.0 # 기본값 전량 매도
        
        # 설정값 로드 (thresholds가 있으면 우선 사용)
        tp_rate = thresholds.get("TAKE_PROFIT_RATE", config.SELL_STRATEGY["TAKE_PROFIT_RATE"]) if thresholds else config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        sl_rate = thresholds.get("STOP_LOSS_RATE", config.SELL_STRATEGY["STOP_LOSS_RATE"]) if thresholds else config.SELL_STRATEGY["STOP_LOSS_RATE"]
        tp_rsi = thresholds.get("TAKE_PROFIT_RSI", config.SELL_STRATEGY["TAKE_PROFIT_RSI"]) if thresholds else config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        sell_score_limit = thresholds.get("SELL_SCORE", config.SELL_STRATEGY["SELL_SCORE"]) if thresholds else config.SELL_STRATEGY["SELL_SCORE"]
        
        # [추가] 반익절 설정 및 계산 (익절 설정의 절반)
        use_half_tp = thresholds.get("HALF_TAKE_PROFIT_USE", config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)) if thresholds else config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
        half_tp_rate = tp_rate / 2.0
        
        # [추가] 시간 청산 설정 로드
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = thresholds.get("TIME_STOP_DAYS", config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20)) if thresholds else config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20)
        time_stop_min_profit = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 0.0)
        
        if time_stop_days <= 0:
            use_time_stop = False
        
        mr_grace_loss_rate = thresholds.get("MR_GRACE_LOSS_RATE", config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -7.0)) if thresholds else config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -7.0)
        
        # [추가] 본전 청산(BEP) 및 ATR 기반 트레일링 설정 로드
        use_atr_stop = thresholds.get("USE_ATR_STOP", config.SELL_STRATEGY.get("USE_ATR_STOP", True)) if thresholds else config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        ts_activation = thresholds.get("ts_activation", config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_callback = thresholds.get("ts_callback", config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
        # [샹들리에 엑시트] TS 동적 콜백 전용 ATR 배수 (손절용 ATR_STOP_MULTIPLIER와 분리)
        ts_atr_mult = thresholds.get("TRAILING_ATR_MULTIPLIER", config.SELL_STRATEGY.get("TRAILING_ATR_MULTIPLIER", 3.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_ATR_MULTIPLIER", 3.0)
        
        bep_activation = thresholds.get("BREAK_EVEN_PROFIT_RATE", config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 5.0)) if thresholds else config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 5.0)
        bep_stop = thresholds.get("BREAK_EVEN_STOP_RATE", config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5)) if thresholds else config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5)
        
        defensive_half_tp = config.SELL_STRATEGY.get("DEFENSIVE_HALF_SELL_USE", False)

        # [추가] 52주 위치 계산 (슈퍼 모멘텀 판정용)
        w52_pos = 0.0

        # 1. 기술적 지표 분석 (시간 청산 시 매수 상태 확인을 위해 우선 수행)
        if df is not None and not df.empty:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100
                
            ind = indicators.calculate_indicators(df)
            # 전일 RSI (상태 분류용) — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
            prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None
            
            is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            sm_flag, sm_reason = analysis.check_smart_money_turnaround(code, is_overseas)

            state, state_color, state_reason = analysis.classify_stock_state(
                df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
            )
            
            # [SSOT] 위 classify_stock_state와 같은 w52_pos를 쓴다 (analyze_buy 주석 참조)
            score, _ = analysis.calculate_score(
                df=df, ind=ind, weights=thresholds.get('WEIGHTS') if thresholds else None,
                smart_money=sm_flag, w52_pos=w52_pos
            )
            score = round(score, 1)

        # [추가] 본전 청산(BEP) 임계값 재설정 로직
        is_bep_applied = False
        max_profit_rate = 0.0
        if highest_price > 0 and buy_price > 0:
            max_profit_rate = ((highest_price - buy_price) / buy_price) * 100
            if max_profit_rate >= bep_activation:
                if sl_rate < bep_stop:
                    sl_rate = bep_stop
                    is_bep_applied = True
                    
        # [추가] 트레일링 스탑 동적 콜백 계산 및 판별
        # [SSOT] 콜백 산식은 compute_trailing_stop()이 단독 보유한다. 잔고 화면(메뉴 9-2)의
        #  'TS 청산가' 표시도 같은 함수를 호출해, 표시된 선과 실제 청산선이 어긋나지 않게 한다.
        ts_msg = ""
        ts_info = compute_trailing_stop(highest_price, buy_price, current_price, ind=ind,
                                        thresholds=thresholds,
                                        ts_activation=ts_activation, ts_callback=ts_callback,
                                        ts_atr_mult=ts_atr_mult, use_atr_stop=use_atr_stop)
        if ts_info and ts_info['triggered']:
            ts_msg = (f"트레일링스탑 (최고가:{int(highest_price):,}원, "
                      f"하락률:-{ts_info['drop_rate']:.1f}%, 기준:-{ts_info['callback']:.1f}%)")

        # 2. 고정 익절/손절 및 시간 청산
        # [수정] 반익절 후 잔여 물량(천장 해제, Let profit run)은 이 elif 체인을 '소비'하면 안 된다.
        #  기존에는 목표 도달 시 pass로 체인이 끝나 아래 손절/시간청산/트레일링 스탑이 전부 차단되어,
        #  고수익 구간에서 TS가 영원히 발동하지 않는 버그가 있었다. 해당 케이스를 조건에서 제외해
        #  체인이 계속 흐르도록 한다.
        if tp_rate > 0 and use_half_tp and not already_half_sold and profit_rate >= half_tp_rate:
            reason = f"반익절({profit_rate:.1f}%)"
            sell_ratio = 0.5
        elif tp_rate > 0 and profit_rate >= tp_rate and not (use_half_tp and already_half_sold):
            reason = f"익절({profit_rate}%)"

        # [추가] 반익절 이후 Let profit run 시, 최소 수익 보존선 (Profit Lock-in)
        # 목표가를 한 번 뚫고 내려올 경우 TS(예: 4%) 발동 전이라도 목표가-3%에서 즉시 매도하여 수익 방어
        # (미발동 시 체인이 계속 흐르도록 판정 조건을 elif 조건식 안에 인라인)
        # [Fix] 보존선 하한 +0.5% — 익절 목표를 3% 이하로 설정한 경우 '수익보존' 명목의 손실 매도 방지
        elif (tp_rate > 0 and use_half_tp and already_half_sold and highest_price > 0
              and max_profit_rate >= tp_rate and profit_rate <= max(tp_rate - 3.0, 0.5)):
            reason = f"수익보존(목표돌파후 하락, {profit_rate:.1f}%)"
        elif sl_rate != 0 and profit_rate <= sl_rate:
            if is_bep_applied:
                reason = f"본전청산({profit_rate:.1f}%)"
            else:
                reason = f"손절({profit_rate:.1f}%)"
        elif use_time_stop and holding_days >= time_stop_days and profit_rate < time_stop_min_profit:
            time_stop_triggered = True
            # [수정] 매도 최적화 4번: 시간 청산 유예 조건을 가격 상방 모멘텀 유무로 엄격하게 변경
            if df is not None and not df.empty and len(df) >= 10:
                if state in ["매수", "강매수", "역매수", "상승", "대기"]:
                    recent_5d_high = df['high'].tail(5).max()
                    recent_10d_high = df['high'].tail(10).max()
                    # 최근 5일의 고점이 최근 10일 고점과 같거나 크면 상방 모멘텀이 살아있는 것으로 간주
                    if recent_5d_high >= recent_10d_high:
                        time_stop_triggered = False # 유예
            
            if time_stop_triggered:
                reason = f"시간청산({holding_days}일경과, 상방모멘텀 상실)"
        # 3. 트레일링 스탑 (외부에서 계산된 메시지 반영)
        elif ts_msg:
            reason = ts_msg
        
        if df is not None and not df.empty:
            # [추가] 슈퍼 모멘텀 동적 매도 평가 로직
            use_super = thresholds.get("SUPER_MOMENTUM_USE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)) if thresholds else config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)
            super_score = thresholds.get("SUPER_MOMENTUM_SCORE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.0)
            super_w52 = thresholds.get("SUPER_MOMENTUM_W52_POS", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)
            super_tp_rsi = thresholds.get("SUPER_TAKE_PROFIT_RSI", config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 90.0)) if thresholds else config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 90.0)
            
            actual_tp_rsi = tp_rsi
            is_super = False
            if use_super and score >= super_score and w52_pos >= super_w52:
                actual_tp_rsi = super_tp_rsi
                is_super = True
                
            # 4. RSI 과열 익절 (TAKE_PROFIT_RSI가 0이면 미사용 - 추세추종 기조)
            if not reason and tp_rsi > 0 and ind.get('rsi') is not None and ind['rsi'] > actual_tp_rsi:
                if is_super:
                    reason = f"RSI과열(슈퍼모멘텀, 기준:{actual_tp_rsi})"
                else:
                    reason = f"RSI과열(기준:{actual_tp_rsi})"
            
            # [추가] 매도 최적화 3번: 방어적 반매도 (하락 반전 신호 발생 시 절반 덜어내기)
            # [개선 #7] 이미 추세가 '매도'로 확정 붕괴된 경우에는 절반만 덜어내지 않고
            #          아래 추세이탈 로직에서 전량 청산하도록 방어적 반매도를 건너뜀
            #          (잔여 물량이 갭하락으로 손실 전환되는 것을 방지).
            if not reason and defensive_half_tp and not already_half_sold and state != "매도":
                if ind.get('psar') is not None and ind.get('ema_5') is not None:
                    # [엣지 케이스 방어] 손실 구간에서의 조기 손절(반손절)을 방지하고 '수익 보전' 목적에 맞게,
                    # 최소한의 의미 있는 수익(time_stop_min_profit, 기본 3.0%) 이상일 때만 발동하도록 안전장치 추가
                    if profit_rate >= time_stop_min_profit and current_price < ind['psar'] and current_price < ind['ema_5']:
                        reason = f"하락반전(방어적 반매도, 수익률:+{profit_rate:.1f}%)"
                        sell_ratio = 0.5

            # 5. 추세 이탈
            # [추세추종] 점수 하락 단독으로는 매도하지 않고 추세 구조 훼손(주가<60일선)을 동시 요구.
            #   스코어는 단기 신호(5>20 EMA, MACD 히스토그램, SAR 등) 비중이 커서 정배열 유지 중의
            #   통상적 눌림목에서도 기준 미만으로 떨어질 수 있음 → 주청산(샹들리에 TS)의 fat-tail
            #   추종을 점수 매도가 조기에 잘라내는 것을 방지. ('매도' 상태는 자체 조건이 이미 엄격하므로 즉시 발동)
            ema60_val = ind.get('ema_60') if ind else None
            structure_broken = ema60_val is None or current_price < ema60_val
            if not reason and (state == "매도" or (score < sell_score_limit and structure_broken)):
                rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
                adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
                cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
                
                # [수정] 역추세 매수 종목은 점수 하락뿐만 아니라 '매도' 상태일지라도 지정된 유예 기간 내에는 손절을 보류함
                if is_mr_holding and holding_days <= time_stop_days and profit_rate > mr_grace_loss_rate:
                    pass # 유예 기간 적용
                else:
                    if state == "매도":
                        reason = f"매도진입({state_reason}) [점수:{score}, RSI:{rsi_val}]"
                    else:
                        reason = f"추세이탈({state}/점수하락+60일선이탈) [점수:{score}, RSI:{rsi_val}, ADX:{adx_val}, CCI:{cci_val}]"
            
        return {
            'action': 'sell' if reason else 'hold',
            'reason': reason,
            'sell_ratio': sell_ratio,
            'ind': ind,
            'score': score,
            'state': state,
            # [추가] 표시 전용 부가 정보 (매매 판단에는 관여하지 않음)
            'state_color': state_color,
            'ts': ts_info,
            'applied_sl_rate': sl_rate,
            'is_atr_stop': bool(thresholds and thresholds.get("ATR_APPLIED_SL_RATE") is not None),
            'is_bep_applied': is_bep_applied,
            'max_profit_rate': max_profit_rate,
        }

class OrderManager:
    """주문 관리 및 상태 추적 전담 클래스"""
    def __init__(self, trader):
        self.trader = trader
        self.pending_orders = {}
        # [추가] 매도 발주 직전 보유수량 {odno: qty} - 모의투자 부분매도 체결 감지용
        #  잔고가 0이 되어야만 체결로 보던 기존 방식은 부분매도를 감지하지 못해
        #  (실시간 감지 실패 → 미체결 타임아웃 우회 경로로 수 분 지연 발생) 보강한다.
        self.sell_pre_qty = {}
        # [Fix] 전량 매도 앵커 정리 유예 큐 {odno: code} — 접수 시점이 아닌 '체결 확정(FILLED)'
        #  시점에 트레일링 최고가·반익절 기록을 정리한다. 접수 시점에 지우면 주문이 미체결
        #  취소될 때 포지션은 남는데 앵커만 리셋되어 샹들리에 TS가 느슨해지는 문제가 있었다.
        self.sell_cleanup_odnos = {}
        # [안전장치] 미체결 취소 연속 실패 횟수 {odno: count}. 취소가 계속 실패하면 pending이
        #  풀리지 않아 그 종목의 매도·손절 판정이 무기한 건너뛰어진다 — 자동 복구가 안 되는
        #  상태라 한도를 넘기면 운영자에게 알린다. 취소 성공 시 항목을 지운다.
        self.cancel_failures = {}
        # [최적화] 누적 주문 접수 카운터 — 루프에서 '이번 주기에 주문이 나갔는가'를 판단해
        #  주문이 없으면 루프 말미 잔고/예수금 재조회를 생략하기 위한 단조 증가 값
        self.orders_sent_count = 0
        self._lock = threading.RLock()

    def is_pending(self, code):
        """특정 종목의 진행 중인 주문 존재 여부 확인"""
        with self._lock:
            return code in self.pending_orders

    def update_order_status(self, code, odno, status):
        """주문 상태 업데이트"""
        with self._lock:
            if code in self.pending_orders and odno in self.pending_orders[code]:
                current_status = self.pending_orders[code][odno]
                if current_status != status:
                    self.pending_orders[code][odno] = status
                    if status in [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED]:
                        del self.pending_orders[code][odno]
                        if not self.pending_orders[code]:
                            del self.pending_orders[code]
                        self.sell_pre_qty.pop(str(odno), None)
                        self.trader.log(f"[OrderState] 주문 종결({status}): {code} (No.{odno})")

                        # [Fix] 전량 매도 앵커 정리는 체결 확정 시에만 수행 (취소/거부 시 앵커 보존)
                        cleanup_code = self.sell_cleanup_odnos.pop(str(odno), None)
                        if cleanup_code and status == OrderStatus.FILLED:
                            try:
                                self.trader.half_tp_cache.discard(cleanup_code)
                                db_manager.db.delete_half_tp(cleanup_code)
                                db_manager.db.delete_trailing_stop(cleanup_code)
                                with self.trader._lock:
                                    self.trader.trailing_stop_cache.pop(cleanup_code, None)
                                self.trader.log(f"[TrailingStop] 전량 매도 체결 확정 → 감시 기록 정리: {cleanup_code}")
                            except Exception as e:
                                self.trader.log(f"[TrailingStop] 매도 체결 후 기록 정리 실패: {e}")
                        
                        # 체결 완료 시 지연 후 보유 종목 리스트 갱신 출력
                        if status == OrderStatus.FILLED:
                            def _delayed_log_holdings():
                                time.sleep(1.5) # KIS API 잔고 갱신 대기
                                self.trader.log_current_holdings()
                            threading.Thread(target=_delayed_log_holdings, daemon=True).start()
                            
                        # [추가] 사후 주문 거부(REJECTED) 시 텔레그램 알림 발송
                        elif status == OrderStatus.REJECTED:
                            try:
                                trade = db_manager.db.get_trade_by_odno(odno)
                                if trade:
                                    t_str = trade.get('type', '')
                                    t_type = "매수" if "buy" in t_str.lower() or "매수" in t_str else ("매도" if "sell" in t_str.lower() or "매도" in t_str else "주문")
                                    name = trade.get('name', code)
                                    qty = trade.get('qty', 0)
                                    price = float(trade.get('price', 0))
                                    
                                    is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
                                    price_str = f"${price:,.2f}" if is_overseas else f"{price:,.0f}원"
                                    if price <= 0: price_str = "시장가"
                                    
                                    msg = f"🚫 [{t_type} 사후 거부] {name}({code})\n수량: {qty}주 / 단가: {price_str}\n주문번호: {utils.format_order_no(odno)}\n사유: 사후 주문 거부 (상세 사유는 HTS/MTS 확인)"
                                    api.send_telegram_message(msg)
                            except Exception as e:
                                self.trader.log(f"REJECTED 알림 전송 실패: {e}")
                    else:
                        self.trader.log(f"[OrderState] 상태 변경: {code} (No.{odno}) {current_status} -> {status}")

    def register_manual_order(self, code, odno, pre_qty=None):
        """수동 주문 발생 시 상태 추적 등록 (외부 호출용)

        pre_qty: 매도 주문의 경우 발주 직전 보유수량. 모의투자에서 부분매도
                 체결을 잔고 감소분으로 감지하기 위해 사용한다.
        """
        with self._lock:
            if code not in self.pending_orders:
                self.pending_orders[code] = {}
            self.pending_orders[code][odno] = OrderStatus.ORDER_SENT
            if pre_qty is not None:
                self.sell_pre_qty[str(odno)] = int(pre_qty)

    def send_order(self, code, qty, type_str, name=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, price=0, rule=None, stop_loss_rate=0.0):
        """주문 전송 및 상태 등록"""
        ord_dvsn = "00" if price > 0 else "01"
        
        self.trader.log(f"━━━━━━━━ [주문 실행] {type_str.upper()} ━━━━━━━━")
        price_log = f"{price:,}원(지정가)" if price > 0 else "시장가(0)"
        
        target_display = f"{name}({code})" if name else code
        amount_log = f"{int(price * qty):,}원" if price > 0 else "-"
        log_detail = f"대상: {target_display}, 수량: {qty}, 단가: {price_log}, 금액: {amount_log}"
        
        if type_str.lower() == 'sell':
            log_detail += f", 손익: {int(profit_amt):+,}원 ({float(profit_rate):+.2f}%)"
            
        self.trader.log(log_detail)

        # [Fix: Point 3] API 지연 중 중복 주문 방지를 위한 임시 ID 선점 (Pre-registration)
        temp_id = f"PRE_{time.time()}"
        with self._lock:
            if code not in self.pending_orders:
                self.pending_orders[code] = {}
            self.pending_orders[code][temp_id] = OrderStatus.ORDER_SENT

        try:
            res_json = api.place_order("domestic", type_str, code, qty, price, ord_dvsn)
            
            if res_json['rt_cd'] == '0':
                odno = res_json['output']['ODNO']
                # [추가] 시스템 발주 주문번호를 즉시 기록(DB 큐 비동기 지연으로 인한 외부주문 오판 방지)
                register_system_odno(odno)
                success_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {type_str.upper()} 성공 | {code} | {qty}주 | No.{odno}"

                with self._lock:
                    # 임시 ID 삭제 및 실제 ODNO로 교체
                    if temp_id in self.pending_orders[code]:
                        del self.pending_orders[code][temp_id]
                    self.pending_orders[code][odno] = OrderStatus.ORDER_SENT
                    self.orders_sent_count += 1

                self.trader.trade_history.append(success_msg)
                self.trader.log(f"결과: 성공 (주문번호: {utils.format_order_no(odno)})")
                stock_display = f"{name}({code})" if name else code
                
                t_type = "매수" if type_str == 'buy' else "매도"
                title_tag = f"[{t_type} 접수]"
                if rule:
                    title_tag += " [개별]"
                
                msg = f"🚀 {title_tag} {stock_display}\n수량: {qty}주\n단가: {price_log}"
                if price > 0:
                    msg += f"\n금액: {int(price * qty):,}원"
                    
                if type_str.lower() == 'sell':
                    msg += f"\n손익: {int(profit_amt):+,}원 ({float(profit_rate):+.2f}%)"
                    
                msg += f"\n주문번호: {utils.format_order_no(odno)}"
                if reason:
                    msg += f"\n사유: {reason}"
                
                if rule:
                    msg += f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                    if rule.get('ts_activation'):
                        msg += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                
                api.send_telegram_message(msg)
                
                snapshot = analysis.get_snapshot(code, is_overseas=False)
                
                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    logger.debug(f"[AutoTrade] 주문 접수 DB 저장 시도: {odno}")
                db_manager.db.insert_trade(f"{type_str}(AUTO)", code, name, qty, str(price), odno, snapshot=snapshot, profit_amt=profit_amt, profit_rate=profit_rate, reason=reason, score=score, stop_loss_rate=stop_loss_rate)
                
                # [추가] DB 큐 처리 시간을 확보하여 체결 감시 모니터가 원주문을 정상 조회할 수 있도록 대기
                time.sleep(0.5)
                _pkg().ConclusionMonitor().check_now()
                
                if type_str == "buy":
                    init_price = float(price)
                    if init_price <= 0:
                        init_price = api.get_current_price(code, is_overseas=False)
                    
                    if init_price > 0:
                        db_manager.db.update_highest_price(code, init_price)
                        with self.trader._lock:
                            self.trader.trailing_stop_cache[code] = init_price
                        self.trader.log(f"[TrailingStop] 감시 시작가 설정: {name} {init_price:,.0f}원")
                
                return odno
            else:
                with self._lock:
                    if temp_id in self.pending_orders.get(code, {}):
                        del self.pending_orders[code][temp_id]
                        if not self.pending_orders[code]: del self.pending_orders[code]

                err_msg = res_json.get('msg1', 'Unknown Error')
                msg_cd = res_json.get('msg_cd')
                self.trader.log(f"결과: 실패 ({err_msg}) [Code: {msg_cd}]")
                
                stock_display = f"{name}({code})" if name else code
                t_type = "매수" if type_str == 'buy' else "매도"
                fail_msg = f"🚫 [{t_type} 실패] {stock_display}\n수량: {qty}주 / 단가: {price_log}\n원인: {err_msg} (Code: {msg_cd})"
                api.send_telegram_message(fail_msg)
                
                if res_json.get('rt_cd') == '9999' or msg_cd in ['OPSQ2000', 'EGW00201']:
                    raise Exception(f"주문 시스템 치명적 오류: {err_msg}")

        except Exception as e:
            with self._lock:
                if temp_id in self.pending_orders.get(code, {}):
                    del self.pending_orders[code][temp_id]
                    if not self.pending_orders[code]: del self.pending_orders[code]

            self.trader.log(f"결과: 에러 발생 ({str(e)})")
            stock_display = f"{name}({code})" if name else code
            t_type = "매수" if type_str == 'buy' else "매도"
            fail_msg = f"🚫 [{t_type} 에러] {stock_display}\n수량: {qty}주 / 단가: {price_log}\n에러: {str(e)}"
            api.send_telegram_message(fail_msg)
            raise e
        finally:
            self.trader.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return None

    # 취소가 연속 이 횟수만큼 실패하면 운영자에게 알린다. 이 상태는 자동 복구되지 않는다 —
    # pending이 유지되어 해당 종목의 매도·손절 판정이 계속 건너뛰어지기 때문이다.
    CANCEL_FAILURE_ALERT_THRESHOLD = 3

    def _note_cancel_failure(self, odno, code, name, res, elapsed):
        """미체결 취소 실패를 누적하고, 한도를 넘으면 1회 경보한다."""
        msg1 = (res or {}).get('msg1') if isinstance(res, dict) else str(res)
        self.trader.log(f"취소 실패: {msg1}")

        key = str(odno)
        cnt = self.cancel_failures.get(key, 0) + 1
        self.cancel_failures[key] = cnt
        if cnt != self.CANCEL_FAILURE_ALERT_THRESHOLD:
            return  # 한도 도달 시점에만 알린다(매 주기 스팸 방지)

        self.trader.log(
            f"⚠️ [미체결 취소 실패 누적] {name}({code}) 주문 {odno} — {cnt}회 연속 실패. "
            f"주문이 열려 있는 동안 이 종목은 매도·손절 판정에서 제외됩니다.")
        try:
            api.send_telegram_message(
                f"⚠️ [미체결 취소 실패] {name}({code})\n"
                f"주문번호: {utils.format_order_no(odno)}\n"
                f"{cnt}회 연속 취소에 실패했습니다 (경과 {int(elapsed)}초)\n"
                f"사유: {msg1}\n\n"
                f"주문이 열려 있는 동안 이 종목은 손절 판정에서 제외됩니다. "
                f"HTS/MTS에서 직접 취소해 주세요.")
        except Exception:
            pass

    def manage_unfilled_orders(self):
        """오래된 미체결 주문 확인 및 취소"""
        
        # [추가] 장 마감 상태이며 로컬에 진행 중인 주문이 없을 경우 API 호출 생략 (트래픽 낭비 원천 차단)
        if not self.trader.is_market_open():
            with self._lock:
                if not self.pending_orders:
                    return
                    
        try:
            # 1. API를 통한 미체결 내역 조회
            unfilled_list = api.get_unfilled_orders()
            
            api_checked_odnos = set()
            cancel_seconds = getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 120)
            now = datetime.now()
            
            # API 조회 결과 처리
            if unfilled_list:
                for item in unfilled_list:
                    odno = item.get('odno')
                    code = item.get('pdno')
                    name = item.get('prdt_name')
                    qty = int(item.get('rmn_qty', 0))
                    ord_time_str = item.get('ord_tmd')
                    
                    if not odno or qty <= 0 or not ord_time_str: continue
                    api_checked_odnos.add(odno)
                    
                    # [추가] 외부 앱(MTS/HTS)에서 들어온 신규 미체결 주문 감지 및 DB 등록
                    trade = db_manager.db.get_trade_by_odno(odno)
                    if not trade:
                        sll_buy_name = item.get('sll_buy_dvsn_cd_name')
                        if not sll_buy_name:
                            sll_buy_cd = item.get('sll_buy_dvsn_cd', '')
                            sll_buy_name = "매수" if sll_buy_cd == "02" else ("매도" if sll_buy_cd == "01" else "주문")
                            
                        price = float(item.get('ord_unpr', 0))
                        t_type = f"{sll_buy_name}(외부)"
                        
                        # DB에 접수 상태로 기록
                        db_manager.db.insert_trade(
                            t_type, code, name, qty, price, odno, 
                            order_status="접수", reason="앱(MTS)/HTS 외부 주문 감지"
                        )
                        
                        # 내부 트래킹(메모리)에 등록
                        with self._lock:
                            if code not in self.pending_orders:
                                self.pending_orders[code] = {}
                            self.pending_orders[code][odno] = OrderStatus.ORDER_SENT
                            
                        self.trader.log(f"[외부 주문 감지] {name}({code}) {sll_buy_name} {qty}주 (No.{odno})")
                        msg = f"📡 [{sll_buy_name} 외부접수] {name}({code})\n수량: {qty}주\n단가: {int(price):,}원\n주문번호: {utils.format_order_no(odno)}\n사유: 앱(MTS)/HTS 등 외부 주문 감지"
                        api.send_telegram_message(msg)
                        
                        trade = db_manager.db.get_trade_by_odno(odno)

                    try:
                        ord_dt = datetime.strptime(f"{now.strftime('%Y%m%d')}{ord_time_str}", "%Y%m%d%H%M%S")
                        elapsed = (now - ord_dt).total_seconds()
                        
                        if elapsed >= cancel_seconds:
                            # [추가] 외부에서 들어온 주문은 시스템이 자동 취소(타임아웃)하지 않도록 보호
                            if trade and "(외부)" in trade.get('type', ''):
                                continue
                                
                            self.trader.log(f"[미체결 관리] {name}({code}) 주문({odno})이 {int(elapsed)}초 동안 체결되지 않아 취소합니다.")
                            
                            res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
                            
                            if res.get('rt_cd') == '0':
                                trade = db_manager.db.get_trade_by_odno(odno)
                                t_type = ""
                                if trade:
                                    t_str = trade.get('type', '')
                                    t_type = "매수" if "buy" in t_str.lower() or "매수" in t_str else ("매도" if "sell" in t_str.lower() or "매도" in t_str else "")
                                type_label = f"{t_type}취소" if t_type else "주문 취소"
                                api.send_telegram_message(f"🗑 [{type_label}] {name} {qty}주\n사유: 미체결 시간 초과 ({int(elapsed)}초)")
                                
                                # [추가] DB에 취소 이력 남기기 (CANCELED 알림 중복 방지)
                                cancel_odno = res.get('output', {}).get('ODNO') or res.get('output', {}).get('KRX_FWDG_ORD_ORGNO') or f"CANCEL_{odno}"
                                db_manager.db.insert_trade(f"{t_type}취소(자동)", code, name, qty, 0, cancel_odno, org_odno=odno, reason=f"미체결 시간 초과 (자동 취소)", order_status="취소")
                                self.cancel_failures.pop(str(odno), None)
                            else:
                                # [Fix] 종전에는 실패를 로그 한 줄로 넘겨, 취소가 계속 실패하면
                                #  pending이 영원히 안 풀렸다. 그 종목은 is_pending 때문에 매도
                                #  판정에서 빠지므로 보호 공백이 무기한이 된다. 연속 실패를 세어
                                #  한도를 넘으면 경보한다(운영자 개입 없이는 복구 불가한 상태다).
                                self._note_cancel_failure(odno, code, name, res, elapsed)
                    except Exception: pass

            # 2. [추가] API에는 없지만 로컬에는 남아있는 주문 처리 (API 누락 대응)
            # 모의투자 등에서 API가 미체결 내역을 반환하지 않는 경우, 로컬 상태를 믿고 강제 확인
            if config.session.is_simulation:
                with self._lock:
                    pending_codes = list(self.pending_orders.keys())
                    
                    for code in pending_codes:
                        if code not in self.pending_orders: continue
                        orders = self.pending_orders[code]
                        odnos = list(orders.keys())
                        
                        for odno in odnos:
                            if odno in api_checked_odnos: continue
                            
                            status = orders[odno]
                            if status == OrderStatus.ORDER_SENT:
                                trade = db_manager.db.get_trade_by_odno(odno)
                                if trade and trade.get('time'):
                                    try:
                                        ord_time = datetime.strptime(trade['time'], "%Y-%m-%d %H:%M:%S")
                                        elapsed = (now - ord_time).total_seconds()
                                        
                                        if elapsed >= cancel_seconds:
                                            self.trader.log(f"[미체결 관리] 로컬 주문({odno}) 타임아웃({int(elapsed)}초). 강제 취소 시도 (API 누락 대응)")
                                            qty = int(trade['qty'])
                                            res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
                                            
                                            if res.get('rt_cd') == '0':
                                                self.trader.log(f"-> 강제 취소 성공. (미체결 상태였음)")
                                                
                                                t_str = trade.get('type', '')
                                                t_type = "매수" if "buy" in t_str.lower() or "매수" in t_str else ("매도" if "sell" in t_str.lower() or "매도" in t_str else "")
                                                type_label = f"{t_type}취소" if t_type else "주문 취소"
                                                api.send_telegram_message(f"🗑 [{type_label}] {trade['name']} {qty}주\n사유: 미체결 시간 초과 (API 누락 보정)")
                                                # 원본 접수 기록 보존을 위해 상태 덮어쓰기 로직 제거
                                                
                                                # 취소 주문 번호는 API 응답(res)에서 파싱해야 하나, revise_cancel_order는 현재 json을 반환함
                                                cancel_odno = res.get('output', {}).get('ODNO') or res.get('output', {}).get('KRX_FWDG_ORD_ORGNO') or f"CANCEL_{odno}"
                                                
                                                db_manager.db.insert_trade("취소(자동)", code, trade['name'], qty, 0, cancel_odno, org_odno=odno, reason="미체결 시간 초과 (자동 취소)", order_status="취소")
                                                
                                                # 로컬 상태 정리
                                                if code in self.pending_orders and odno in self.pending_orders[code]:
                                                    del self.pending_orders[code][odno]
                                                    if not self.pending_orders[code]: del self.pending_orders[code]
                                            else:
                                                # 취소 실패 시 (이미 체결되었거나 거부된 주문)
                                                msg_cd = res.get('msg_cd')
                                                # 40330000: 정정/취소할 수량이 없습니다 (이미 체결됨 or 취소됨)
                                                if msg_cd == '40330000':
                                                    self.trader.log(f"-> 이미 체결/취소된 주문입니다. 잔고 확인 후 상태를 동기화합니다.")
                                                    
                                                    # [추가] 잔고 확인을 통해 체결 여부 추정
                                                    is_filled = False
                                                    # 매수 주문이었던 경우 잔고에 해당 종목이 있는지 확인
                                                    if "buy" in trade.get('type', '').lower() or "매수" in trade.get('type', ''):
                                                        try:
                                                            holdings, _ = api.get_domestic_balance(config.session.cano, config.session.acnt_prdt_cd)
                                                            if holdings:
                                                                for h in holdings:
                                                                    if h['pdno'] == code and int(h['hldg_qty']) > 0:
                                                                        is_filled = True
                                                                        break
                                                        except Exception: pass
                                                    elif "sell" in trade.get('type', '').lower() or "매도" in trade.get('type', ''):
                                                        # [추가] 매도 주문인 경우 40330000 에러는 대부분 체결 완료를 의미함
                                                        is_filled = True
                                                    
                                                    if is_filled:
                                                        self.trader.log(f"-> 체결/잔고 확인됨. '체결(추정)'으로 기록합니다.")
                                                        
                                                        fill_price = float(trade['price'])
                                                        is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum()) if code else False
                                                        if fill_price <= 0:
                                                            try:
                                                                cp = api.get_current_price(code, is_overseas=is_overseas)
                                                                if cp > 0: fill_price = float(cp)
                                                            except Exception: pass

                                                        # 원본 접수 기록 보존을 위해 상태 덮어쓰기 로직 제거
                                                        # 체결 내역 강제 생성 (히스토리 보정)
                                                        db_manager.db.insert_trade(trade['type'], code, trade['name'], qty, fill_price, odno, order_status="체결(추정)", reason="체결 확인(잔고 확인)", custom_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                                                        
                                                        # [수정] 텔레그램 알림 발송 (실전 포맷 적용)
                                                        try:
                                                            type_str = trade.get('type', '')
                                                            type_name = "매수" if "buy" in type_str.lower() or "매수" in type_str else "매도"
                                                            
                                                            # 개별 룰 조회
                                                            custom_rules = db_manager.db.get_all_stock_strategies()
                                                            rules_map = {r['code']: r for r in custom_rules}
                                                            rule = rules_map.get(code)
                                                            
                                                            title_tag = "[체결 알림(추정)]"
                                                            rule_info = ""
                                                            if rule:
                                                                title_tag += " [개별]"
                                                                rule_info = f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                                                                if rule.get('ts_activation'):
                                                                    rule_info += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                                                            
                                                            # 현재가 정보
                                                            cur_info = ""
                                                            try:
                                                                cp_data = api.get_current_price_data(code, is_overseas=is_overseas)
                                                                if cp_data.get('rt_cd') == '0':
                                                                    if is_overseas:
                                                                        curr = float(cp_data['output'].get('last', 0))
                                                                        rate = float(cp_data['output'].get('rate', 0))
                                                                        icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                                                        cur_info = f"\n현재가: ${curr:,.2f} ({icon} {rate:+.2f}%)"
                                                                    else:
                                                                        curr = float(cp_data['output']['stck_prpr'])
                                                                        rate = float(cp_data['output']['prdy_ctrt'])
                                                                        icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                                                        cur_info = f"\n현재가: {int(curr):,}원 ({icon} {rate:+.2f}%)"
                                                            except Exception: pass

                                                            # 전략 지표 (스냅샷 활용)
                                                            strategy_info = ""
                                                            if trade.get('snapshot'):
                                                                try:
                                                                    snap = json.loads(trade['snapshot'])
                                                                    if 'indicators' in snap:
                                                                        ind = snap['indicators']
                                                                        score = trade.get('strategy_score', 0)
                                                                        rsi_str = f"{ind.get('rsi', 0):.1f}"
                                                                        adx_str = f"{ind.get('adx', 0):.1f}"
                                                                        cci_str = f"{ind.get('cci', 0):.1f}"
                                                                        strategy_info = f"\n\n📊 [전략 지표(진입시점)]\n• 점수: {score}점\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                                                except Exception: pass
                                                            
                                                            exec_amt = fill_price * qty
                                                            price_fmt = f"${fill_price:,.2f}" if is_overseas else f"{fill_price:,.0f}원"
                                                            amt_fmt = f"${exec_amt:,.2f}" if is_overseas else f"{int(exec_amt):,}원"
                                                            
                                                            profit_msg = ""
                                                            if type_name == "매도":
                                                                p_amt = trade.get('profit_amt')
                                                                p_rate = trade.get('profit_rate')
                                                                if p_amt is not None and p_rate is not None:
                                                                    profit_msg = f"\n손익: {int(p_amt):+,}원 ({float(p_rate):+.2f}%)"
                                                                    
                                                            original_reason = trade.get('reason', '잔고 확인')
                                                            msg = f"✅ {title_tag} {type_name} {trade['name']}({code})\n수량: {qty}주\n단가: {price_fmt}(추정체결가)\n금액: {amt_fmt}\n주문번호: {utils.format_order_no(odno)}{profit_msg}\n사유: {original_reason}{cur_info}{strategy_info}{rule_info}"
                                                            api.send_telegram_message(msg)
                                                            
                                                            # [추가] 매도 체결(추정) 시 AI 매매 복기 실행 (모의투자용)
                                                            if type_name == "매도":
                                                                threading.Thread(target=self._send_trading_autopsy, args=(code, trade['name'], trade), daemon=True).start()
                                                        except Exception as e:
                                                            self.trader.log(f"알림 전송 실패: {e}")
                                                    else:
                                                        self.trader.log(f"-> 잔고/체결 확인 안됨. '취소'로 상태를 변경합니다.")
                                                        # 원본 접수 기록 보존 및 취소 더미 이력 생성
                                                        db_manager.db.insert_trade(trade['type'], code, trade['name'], qty, float(trade['price']), odno, order_status="취소(추정)", reason="잔고/체결 확인 안됨 (취소 간주)", custom_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                                                    if code in self.pending_orders and odno in self.pending_orders[code]:
                                                        del self.pending_orders[code][odno]
                                                        if not self.pending_orders[code]: del self.pending_orders[code]
                                                else:
                                                    self.trader.log(f"-> 취소 실패: {res.get('msg1')}")
                                    except Exception as e:
                                        self.trader.log(f"로컬 미체결 처리 중 오류: {e}")
        except Exception as e:
            self.trader.log(f"미체결 관리 중 오류: {e}")

class RiskManager:
    """리스크 관리 및 자산 배분 전담 클래스"""
    def __init__(self, trader):
        self.trader = trader

    def compute_portfolio_heat(self, holdings, buy_trades_map=None):
        """포트폴리오 히트(총 오픈 리스크, 원) 계산

        보유 각 포지션이 '현재가 → 유효 손절선'까지 하락했을 때의 잠재 손실을 합산한다.
        SYSTEM_RISK_PER_TRADE가 종목당 손실을 통제한다면, 이 값은 '전 종목 동시 손절'
        시나리오의 합산 손실을 SYSTEM_MAX_PORTFOLIO_RISK 이하로 묶기 위한 기준값이다.

        유효 손절선 추정(매도 로직의 근사, 보수적 = 리스크 과대평가 방향):
          ① 매수가 × (1 + 손절률): 손절률은 보유분 매수 기록의 수량가중 평균(ATR 손절 저장값),
             없으면 전역 STOP_LOSS_RATE.
          ② 최고가 기준 max_profit이 BEP 발동선(ATR 손절 시 손절폭과 동일) 이상이면 본전(매수가)으로 상향.
          ③ max_profit이 트레일링 발동선 이상이면 최고가×(1-콜백%)으로 상향.
        손절선이 현재가 위(이미 이익 잠김)면 해당 포지션 리스크는 0으로 본다.
        """
        total_risk = 0.0
        if not holdings:
            return total_risk

        sell_cfg = config.SELL_STRATEGY
        default_sl = sell_cfg.get("STOP_LOSS_RATE", -5.0)
        use_atr_stop = sell_cfg.get("USE_ATR_STOP", True)
        bep_rate_cfg = sell_cfg.get("BREAK_EVEN_PROFIT_RATE", 5.0)
        ts_act = sell_cfg.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_cb = sell_cfg.get("TRAILING_STOP_CALLBACK_RATE", 5.0)

        for h in holdings:
            try:
                qty = api.safe_int(h.get('hldg_qty', 0))
                if qty <= 0:
                    continue
                buy_price = float(h.get('pchs_avg_pric') or 0)
                cur = float(h.get('prpr') or 0)
                if buy_price <= 0 or cur <= 0:
                    continue
                code = h.get('pdno')

                sl_rate = None
                trades = (buy_trades_map or {}).get(code) or []
                tq, ws = 0, 0.0
                for t in trades:
                    q = api.safe_int(t.get('qty', 0))
                    try:
                        s = float(t.get('stop_loss_rate') or 0.0)
                    except (TypeError, ValueError):
                        s = 0.0
                    if q > 0 and s != 0.0:
                        tq += q
                        ws += q * s
                if tq > 0:
                    sl_rate = ws / tq
                if sl_rate is None or sl_rate >= 0:
                    sl_rate = default_sl if default_sl < 0 else -5.0

                stop_price = buy_price * (1 + sl_rate / 100.0)

                highest = 0.0
                try:
                    with self.trader._lock:
                        highest = self.trader.trailing_stop_cache.get(code) or 0.0
                    if highest <= 0:
                        highest = db_manager.db.get_highest_price(code) or 0.0
                except Exception:
                    highest = 0.0

                if highest > 0:
                    max_profit = (highest - buy_price) / buy_price * 100.0
                    bep_threshold = abs(sl_rate) if use_atr_stop else bep_rate_cfg
                    if bep_threshold > 0 and max_profit >= bep_threshold:
                        stop_price = max(stop_price, buy_price)
                    if ts_act > 0 and ts_cb > 0 and max_profit >= ts_act:
                        stop_price = max(stop_price, highest * (1 - ts_cb / 100.0))

                total_risk += qty * max(0.0, cur - stop_price)
            except Exception:
                continue

        return total_risk

    def current_risk_scale(self, market_type=None):
        """[리스크 스케일링] 트레이더가 주기마다 갱신한 리스크 한도 배수 (0<scale≤1)

        약세 국면·휩소율·계좌 드로다운에 따라 트레이더(_update_risk_scale)가 산출한다.
        신규 진입 사이징(SYSTEM_RISK_PER_TRADE)과 히트 캡(SYSTEM_MAX_PORTFOLIO_RISK)에만
        적용되며 청산 로직에는 관여하지 않는다. 미산출 시 1.0(축소 없음).

        market_type("KOSPI"/"KOSDAQ")을 주면 **해당 시장의 배수**를 돌려준다 — 코스피와
        코스닥은 별개 시장이므로 종목 사이징에는 그 종목이 속한 시장의 국면·휩소율만 반영한다.
        생략하면 계좌 단위 배수(두 시장 중 열위 쪽)를 쓴다. 히트 캡처럼 계좌 전체의 총
        오픈 리스크를 묶는 용도는 시장별로 나눌 수 없으므로 이쪽(보수적)이 맞다."""
        scale = None
        if market_type:
            by_market = getattr(self.trader, 'risk_scale_by_market', None) or {}
            scale = by_market.get(market_type)
        if scale is None:
            scale = getattr(self.trader, 'risk_scale', 1.0)
        try:
            scale = float(scale or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        return min(1.0, scale) if scale > 0 else 1.0

    def effective_portfolio_cap(self):
        """리스크 스케일이 반영된 실효 히트 캡(%) — 로그 표시용"""
        cap = getattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0)
        return cap * self.current_risk_scale()

    def portfolio_risk_budget_left(self):
        """히트 캡까지 남은 리스크 예산(원). 캡 미사용(0)·기준자산 미확보 시 None(제한 없음)

        [리스크 스케일링] 약세 국면·드로다운 시 캡이 배수만큼 축소된다.
        기준자산은 최근 조회된 현재 평가자산을 우선 사용해(폴백: 당일 시작자산),
        장중 손실이 나면 예산도 함께 줄어드는 보수적 방향으로 동작한다."""
        cap = getattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0)
        if cap <= 0:
            return None
        equity = getattr(self.trader, 'current_total_asset', 0) or self.trader.initial_asset
        if equity <= 0:
            return None
        heat = getattr(self.trader, 'portfolio_heat_amt', 0.0)
        return equity * (cap * self.current_risk_scale() / 100.0) - heat

    def allocate_budget(self, avail_cash, invest_ratio, stop_loss_rate=None, atr=None,
                        current_price=None, market_type=None):
        """자산 배분 계산

        3개 레이어가 각자 '상한'을 내고, 그 중 가장 작은 값을 쓴다(min 결합).
          1) 기초 비중(invest_ratio): 종목당 명목 상한 (집중 방지). SYSTEM_MAX_HOLDINGS와 곱해 1.0 이하 권장.
             [리스크 스케일링] 여기에 risk_scale을 곱한다 — 약세 국면·톱니장·드로다운에서 노출을 실제로 줄이는 지점.
          2) 리스크 기반(SYSTEM_RISK_PER_TRADE): '손절 시 계좌 손실액'을 일정 이하로 고정 → 꼬리위험(tail loss) 상한.
             손절폭(ATR 손절이면 ATR 반영)이 넓을수록 상한을 줄인다.
             [갭 버퍼] 소프트 스탑의 갭하락 미끄러짐을 대비해 손절폭에 GAP_RISK_BUFFER 배수를 곱해 보수 계산.
             [리스크 스케일링] 약세 국면·드로다운 시 허용 손실액이 risk_scale 배수만큼 축소된다.
             market_type을 받으면 해당 시장(KOSPI/KOSDAQ)의 배수를 쓴다 — 코스닥이 톱니장이라고
             코스피 종목까지 줄이지 않는다.
             ※ [실측 2026-07-27] 이 층은 현재 파라미터에서 **최종액을 결정하지 않는다** — 관심종목
               50종목 전부에서 3)이 구속하고 2)의 상한은 항상 그보다 크다. 따라서
               SYSTEM_RISK_PER_TRADE를 4→3%로 낮춰도 배분액은 변하지 않는다.
               (그래서 risk_scale은 이 층이 아니라 1)에 적용한다 — 위 참조.)
          3) 변동성 타겟팅(TARGET_VOLATILITY): 종목의 연환산 변동성을 목표치로 정규화 → 변동성 균질화.
             상한 = 기초 비중 × scale (기초 대비 몇 %까지 허용하는가).

        [Fix] 종전에는 3)을 2)의 결과에 '곱셈'으로 얹었다. 그러나 ATR 손절을 쓰면 2)의 상한이
        이미 1/ATR에 비례하고 3)의 배수도 1/ATR에 비례해, 결합 결과가 1/ATR²로 과대 축소됐다
        (실측 2026-07-23 GS건설: 기초 2,487,790 → 리스크 1,326,821 → ×0.40 = 530,728 = 기초의 21%.
         같은 상황에서 기초 비중을 0.25→0.5로 올려도 최종액이 530,728원으로 동일했다 —
         기초를 올려도 곱셈 사슬 뒤쪽이 그대로 깎아내려 설정이 사문화됐다).
        2)와 3)은 '얼마를 살까'에 대한 서로 다른 답이지 누적 벌점이 아니므로 min()으로 결합한다.
        min 결합에서도 각 정책은 그대로 지켜진다 — 최종액 ≤ 2)이므로 손실액 캡은 여전히 불가침이고,
        최종액 ≤ 1)이므로 집중 캡도 유지된다. 다만 3)의 확대(scale>1)로 2)의 축소분을 되돌리던
        동작은 사라진다(그 복원은 손실액 캡을 넘길 수 있어 애초에 위험한 방향이었다).
        """
        risk_per_trade = getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0)
        risk_based_amt = 0
        risk_scale = self.current_risk_scale(market_type)
        risk_params = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}

        # [리스크 스케일링] 배수를 '기초 비중'에 적용한다.
        #  종전에는 2)리스크층에만 곱했는데 그 층이 최종액을 결정하는 일이 없어(3)이 상시 구속)
        #  배수가 0.45 아래로 떨어지기 전까지 배분액이 1원도 변하지 않았다 = 사실상 무력.
        #  기초 비중에 곱하면 3)의 상한(기초×변동성배수)도 함께 내려가 곧바로 방어가 걸린다.
        #  [실측 2026-07-27, 시드 500만/1,000만 · 30종목 무작위 50회 짝비교]
        #    MDD 개선 46/50·45/50 (중앙 +2.8%p·+3.2%p), PF 개선 41/50·44/50 (중앙 +0.27·+0.34),
        #    대가는 3년 수익 중앙 -16.9%p·-24.1%p, 유휴현금 +13%p.
        #    타이밍 가치 확인: 같은 평균 배수를 상수로 준 대조군은 수익이 절반(146.5%→71.0%)이고
        #    PF도 낮아(2.83→2.20) 국면 판단이 실제로 기여함을 확인했다(셔플 대조군도 동일).
        scaled_ratio = invest_ratio * risk_scale

        if self.trader.initial_asset > 0:
            target_invest_amt = int(self.trader.initial_asset * scaled_ratio)
        else:
            target_invest_amt = int(avail_cash * scaled_ratio)

        base_amt = target_invest_amt

        if risk_per_trade > 0 and stop_loss_rate and abs(stop_loss_rate) > 0:
            total_equity = self.trader.initial_asset if self.trader.initial_asset > 0 else avail_cash
            max_loss_amt = total_equity * (risk_per_trade * risk_scale / 100.0)
            try:
                gap_buffer = max(1.0, float(risk_params.get("GAP_RISK_BUFFER", 1.2)))
            except (TypeError, ValueError):
                gap_buffer = 1.2
            sl_ratio = (abs(stop_loss_rate) / 100.0) * gap_buffer
            risk_based_amt = int(max_loss_amt / sl_ratio)
            target_invest_amt = min(target_invest_amt, risk_based_amt)

        scale = 1.0
        vol_based_amt = 0
        if getattr(config, 'USE_VOLATILITY_TARGETING', True) and atr and current_price and current_price > 0:
            daily_vol = atr / current_price
            annual_vol = daily_vol * math.sqrt(252)

            target_vol = getattr(config, 'TARGET_VOLATILITY', 0.20)
            scale_max = getattr(config, 'VOLATILITY_SCALING_MAX', 2.0)
            scale_min = getattr(config, 'VOLATILITY_SCALING_MIN', 0.4)

            if annual_vol > 0:
                scale = target_vol / annual_vol
                scale = max(scale_min, min(scale_max, scale))
                # 변동성 상한은 '기초 비중 기준'으로 산출한다(리스크 상한에 곱하지 않는다 → 중복 축소 제거).
                # 확대(scale>1)는 기초 비중을 넘을 수 없으므로 집중 캡도 자동 충족된다.
                vol_based_amt = min(int(base_amt * scale), base_amt)
                target_invest_amt = min(target_invest_amt, vol_based_amt)

        invest_amt = min(target_invest_amt, avail_cash)

        log_msg = f"[자산배분] 기초:{base_amt:,}원"
        if risk_scale < 1.0:
            # 사유는 '그 종목이 속한 시장'의 것을 보여준다 (계좌 전역 사유를 찍으면 오인을 부른다).
            by_market = getattr(self.trader, 'risk_scale_reason_by_market', None) or {}
            reason = (by_market.get(market_type) if market_type else None) \
                or getattr(self.trader, 'risk_scale_reason', '') or '축소'
            label = f"{market_type} " if market_type else ""
            log_msg += f" | 리스크스케일 x{risk_scale:.2f}({label}{reason})"
        if risk_based_amt > 0:
            log_msg += f" | 리스크캡:{risk_based_amt:,}원(손절{abs(stop_loss_rate):.1f}%)"
        if vol_based_amt > 0:
            log_msg += f" | 변동성캡:{vol_based_amt:,}원(x{scale:.2f})"
        log_msg += f" -> 최종:{invest_amt:,}원"

        self.trader.log(log_msg)

        return invest_amt

    def check_loss_limit(self, current_total):
        """일일 손실 한도 체크"""
        loss_limit_pct = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
        
        if loss_limit_pct <= 0 or self.trader.initial_asset <= 0: return
        if current_total <= 0: return

        # [Fix] 비정상적인 데이터(갑작스런 반토막 이상 하락 등 API 데이터 누락 의심) 필터링
        # (주로 증권사 API 통신 오류로 인해 주식 평가액이 0으로 수신되어 예수금만 계산될 때 발생합니다.)
        if current_total < self.trader.initial_asset * 0.5:
            self.trader.log(f"⚠️ 비정상적인 자산 급감 감지(API 오류 의심). 손실 한도 체크를 스킵합니다. (현재자산: {current_total:,}원)")
            return

        loss_rate = (current_total - self.trader.initial_asset) / self.trader.initial_asset * 100
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[LossCheck] 시작자산:{self.trader.initial_asset:,} -> 현재자산:{current_total:,} | 변동률:{loss_rate:+.2f}% (한도:-{loss_limit_pct}%)")
        
        if loss_rate <= -loss_limit_pct:
            # [Fix] 기존에는 여기서 trader.stop()으로 시스템을 통째로 정지했다. 그러나 정지는
            #  포지션을 청산하지 않고 매도 감시 루프까지 함께 끄기 때문에, 일일 손실 한도에
            #  도달한(=여러 포지션이 이미 손절선 근처인) 바로 그 순간부터 손절·트레일링 스탑이
            #  작동하지 않는 무방비 상태가 됐다. 추세추종 원칙("손절을 하지 않으면 계좌가
            #  심각한 타격을 입는다")과 정면 충돌하므로, '신규 매수 중단(방어 모드)'으로 축소하고
            #  청산 감시는 계속 돌린다. 시스템 완전 정지는 사용자 판단(메뉴/텔레그램)에 맡긴다.
            reason = f"일일 손실 한도 초과 ({loss_rate:.2f}% / 제한 -{loss_limit_pct}%)"

            msg = (f"🛑 [방어 모드] 일일 손실 한도 초과\n\n"
                   f"수익률: {loss_rate:.2f}% (제한: -{loss_limit_pct}%)\n"
                   f"현재 자산: {current_total:,}원\n\n"
                   f"신규 매수를 중단합니다. 보유 종목의 손절·트레일링 스탑 감시는 계속됩니다.\n"
                   f"(완전 정지가 필요하면 직접 중지해 주세요)")

            # [추가] 에러 로그 꼬리 첨부 (1시간 쿨타임)
            now = time.time()
            if now - getattr(self.trader, 'last_emergency_alert_time', 0) > 3600:
                log_tail = get_mystock_log_tail(20)
                msg += f"\n\n📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```"
                self.trader.last_emergency_alert_time = now

            # 이미 같은 날 발동 중이면 halt_buys가 False를 돌려주어 알림·로그가 반복되지 않는다.
            if self.trader.halt_buys(reason, notify_msg=msg):
                self.trader.log(f"시작 자산: {self.trader.initial_asset:,}원 -> 현재 자산: {current_total:,}원")
                console.print(
                    f"\n[bold red]🛑 [방어 모드] 일일 손실 한도 초과 (수익률: {loss_rate:.2f}% / 제한: -{loss_limit_pct}%)[/bold red]\n"
                    f"[dim]신규 매수를 중단했습니다. 손절·트레일링 스탑 감시는 계속됩니다.[/dim]\n")

