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

from modules.auto_trade.common import (OrderStatus, _norm_odno, get_mystock_log_tail, register_system_odno)

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
UNMANAGED_STALE_PRICE = "현재가 조회 실패(판정 불가)"
UNMANAGED_OVERSEAS = "해외 미지원"
#  매도를 결정했는데 증권사가 매도가능수량 0을 준 상태. 시스템 자신의 미체결 주문은 그 앞의
#  is_pending 검사에서 이미 걸러지므로, 여기까지 오면 거래정지·상장폐지·HTS에서 직접 낸
#  매도 주문에 물량이 묶인 경우다 — 어느 쪽이든 시스템이 스스로 빠져나올 수 없다.
UNMANAGED_NO_SELLABLE = "매도가능수량 0 (거래정지·외부주문 의심)"
#  주문 상태기계에 대기 주문이 남아 그 종목의 매도 판정이 통째로 건너뛰어지는 상태.
#  정상 주문은 몇 주기 안에 체결·취소로 종결되지만, 상태기계에 유령 항목이 남으면
#  **손절·트레일링이 조용히 영구 정지**한다(2026-08-05 관측: 손절 기준을 넘겼는데도
#  매도 판정 로그 자체가 나오지 않음). 종전에는 이 스킵이 DEBUG 로그라 보이지 않았다.
UNMANAGED_STUCK_PENDING = "대기 주문에 묶임 (주문 상태기계 확인 필요)"
#  매도 판정 자체가 예외로 죽은 상태. 종전에는 _sell_worker의 예외를 아무도 회수하지 않아
#  (concurrent.futures.wait은 예외를 되살리지 않는다) 그 종목만 [보유분석] 줄 없이 사라지고
#  손절·트레일링이 조용히 정지했다. 실제로 개별 룰의 NULL 컬럼 하나가 analyze_sell을
#  TypeError로 죽였고, 원인을 찾는 데 로그가 전혀 도움이 되지 않았다(2026-08-05 NAVER).
UNMANAGED_ANALYSIS_ERROR = "매도 판정 오류 (분석 실패)"
#  경보 전 연속 관측 횟수. 미체결 취소 직후 한 주기 정도는 일시적으로 0이 될 수 있어,
#  즉시 알리면 정상 운영 중에도 오경보가 난다(입출금 감지의 '3회 연속'과 같은 방식).
NO_SELLABLE_ALERT_CYCLES = 3
#  대기 주문 스킵은 정상 흐름에서도 몇 주기 이어질 수 있으므로 더 여유를 둔다.
#  (미체결 자동 취소 타임아웃보다 길게 잡아야 정상 취소 흐름을 오경보로 만들지 않는다)
STUCK_PENDING_ALERT_CYCLES = 10


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


#  평단·매입금액의 '변하지 않았다'를 판정하는 허용 오차. 분할 시 단주는 현금 정산되어
#  매입금액이 아주 조금 줄고, 평단도 소수점에서 반올림되므로 정확히 같지는 않다.
CORP_ACTION_TOLERANCE = 0.01   # 1%


def detect_corporate_action(ref_avg, ref_pchs_amt, cur_avg, cur_pchs_amt):
    """액면분할·무상증자로 평단이 재조정됐는지 판정하고, 최고가에 곱할 배율을 돌려준다.

    [왜 필요한가] trailing_stops.highest_price는 원시 가격이고 갱신은 단조 증가라
    (db_manager.update_highest_price의 `WHERE excluded.highest_price > ...`) 내려갈 수 없다.
    5:1 분할이 나면 증권사는 매입평균단가를 1/5로 조정하지만 우리 고점만 분할 전 값으로
    남아, compute_trailing_stop이 drop_rate 80%를 보고 즉시 청산을 때린다. 백테스트는
    수정주가를 쓰므로 이 사고를 영원히 재현하지 못한다 — 실계좌에서만 터진다.

    [판정 근거] **매입금액 = 수량 × 평단** 이 보존되는가로 매수·매도와 구분한다.
      · 분할·무상증자 : 수량↑ 평단↓ , 매입금액 그대로   → 배율 = 새 평단 / 옛 평단
      · 매수(피라미딩·HTS 수동) : 매입금액 증가          → 보정하지 않는다
      · 부분 매도     : 매입금액 감소, 평단 불변          → 보정하지 않는다
    평단만 보고 판정하면 'HTS에서 비싸게 추가 매수'가 분할로 오인되어 고점이 위로
    조정되고, 오히려 없던 청산이 발생한다. 그래서 매입금액 불변이 필수 조건이다.

    반환: (배율, 사유). 보정이 필요 없으면 (1.0, "").
    """
    if ref_avg <= 0 or cur_avg <= 0 or ref_pchs_amt <= 0 or cur_pchs_amt <= 0:
        return 1.0, ""      # 기준이 없다(최초 관측·이관 직후) — 이번 주기는 기록만 한다

    if abs(cur_avg - ref_avg) / ref_avg <= CORP_ACTION_TOLERANCE:
        return 1.0, ""      # 평단이 그대로면 아무 일도 없었다

    if abs(cur_pchs_amt - ref_pchs_amt) / ref_pchs_amt > CORP_ACTION_TOLERANCE:
        return 1.0, ""      # 매입금액이 움직였다 = 매수·매도 → 정상 변경

    ratio = cur_avg / ref_avg
    kind = "액면병합" if ratio > 1 else "액면분할·무상증자"
    return ratio, (f"{kind} 추정 (평단 {ref_avg:,.0f} → {cur_avg:,.0f}원, "
                   f"매입금액 {ref_pchs_amt:,.0f}원 유지)")


def detect_retro_price_adjustment(ref_close, now_close_for_ref_date):
    """같은 **과거 날짜**의 종가가 달라졌는가 — 미보유 종목의 권리 조정 판정.

    [왜 별도인가] detect_corporate_action은 잔고의 평단·매입금액을 본다. 예약 주문만
    걸어 둔 종목은 보유분이 없어 그 근거가 통째로 없다. 대신 거래소가 권리 조정 시
    **과거 시세를 소급 수정**하는 성질을 쓴다. 어제 종가로 500,000원을 적어 뒀는데 오늘
    같은 날짜를 조회하니 100,000원이면, 그 사이에 5:1 조정이 있었다는 뜻이다.

    [왜 가격 점프가 아닌가] '전일 대비 ±30% 초과'로 잡는 방법도 있으나 두 가지가 걸린다.
      · 거래소는 권리락일에 기준가를 미리 조정하므로 전일대비는 정상 범위로 보인다.
      · 30% 무상증자는 -23% — 가격제한폭 안이라 정상 등락과 구분되지 않는다.
    소급 수정 비교는 조정 폭과 무관하게 정확하다.

    반환: (배율, 사유). 조정이 없으면 (1.0, ""). 배율은 옛 가격에 곱하면 새 가격이 된다.
    """
    if ref_close <= 0 or now_close_for_ref_date <= 0:
        return 1.0, ""      # 기준이 없다(최초 관측) — 이번엔 기록만 한다

    ratio = now_close_for_ref_date / ref_close
    if abs(ratio - 1.0) <= CORP_ACTION_TOLERANCE:
        return 1.0, ""

    kind = "액면병합" if ratio > 1 else "액면분할·무상증자"
    return ratio, (f"{kind} 추정 (과거 종가가 {ref_close:,.0f} → "
                   f"{now_close_for_ref_date:,.0f}원으로 소급 수정, 배율 {ratio:.4g})")


def profit_lock_stop_rate(max_profit_rate, min_mfe=None, giveback=None):
    """무장 전 구간의 이익 보호선(매수가 대비 %). 조건 미달이면 None. (부수효과 없음)

    [왜 필요한가] TS가 무장하기 전까지 포지션을 지키는 건 ATR 손절선뿐인데, 이 선은 매수가
    기준으로 고정돼 있다(캡 -15%). 이미 +40% 오른 포지션에게 '매수가 -15%'는 방어가 아니다.
    무장을 앞당기는 해법은 2026-08-09 실측에서 기각됐으므로(위 '정확식' 참조), 무장 전
    구간에만 걸리는 별도의 선을 둔다.

    선 = 매수가 + (고점 - 매수가) × (1 - giveback)
       = 매수가 × (1 + MFE(1-giveback)/100)     → 반환값은 MFE×(1-giveback)

    [BEP와 다른 점] 본전 청산은 낮은 MFE에서 손절선을 본전으로 끌어올려 눌림에 털린다
    (2026-08-04 실측으로 OFF). 이 선은 min_mfe 위에서만 켜지고, 켜진 뒤에도 이익의
    giveback 비율만큼은 계속 내줘 추세에 여유를 남긴다.
    """
    ss = config.SELL_STRATEGY
    if min_mfe is None:
        min_mfe = ss.get("PROFIT_LOCK_MIN_MFE", 25.0)
    if giveback is None:
        giveback = ss.get("PROFIT_LOCK_GIVEBACK", 0.5)
    try:
        mfe = float(max_profit_rate or 0)
    except (TypeError, ValueError):
        return None
    if mfe <= 0 or mfe < float(min_mfe) or not (0 < float(giveback) < 1):
        return None
    return mfe * (1 - float(giveback))


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

    # [발동] breakeven 모드 = '한 번의 정상 되돌림(3.5 ATR)을 맞아도 본전 이상'인 지점부터 무장.
    #  손실 구간에서 트레일링으로 털리는 것을 막고, 무장 시점을 종목 변동성이 자동으로 정한다
    #  (고변동주는 늦게, 저변동주는 일찍). 고정 %는 41종목 중 40개에서 청산선이 아직 매수가
    #  아래인 상태로 무장했다(config.TRAILING_STOP_ACTIVATION_RATE 주석의 10년 실측 참조).
    #
    #  [중요] 되돌림 폭은 '고점'이 아니라 '매수가' 기준으로 환산한다. 고점 기준으로 잡으면
    #   고점이 오를수록 문턱이 따라 낮아져(자기참조) 사실상 현행과 같아진다 — 실측에서
    #   구간5 수익승이 23/30 → 14/30으로 무너졌다. 매수가 기준이라야 문턱이 진입 시점에
    #   고정되고, 검증된 결과가 재현된다.
    stop_price = highest_price * (1 - actual_callback / 100)
    if str(ss.get("TS_ACTIVATION_MODE", "fixed")).lower() == "breakeven":
        # [분리] 발동선은 콜백 배수(ts_atr_mult)가 아니라 발동 전용 배수로 계산한다.
        ts_activation = breakeven_activation_rate(atr_val, buy_price, ts_callback,
                                                  ts_activation_atr_mult(), use_atr_stop)
        armed = max_profit_rate >= ts_activation
    else:
        armed = max_profit_rate >= ts_activation
    return {
        'armed': armed,
        'triggered': bool(armed and drop_rate >= actual_callback),
        'stop_price': stop_price,
        'callback': actual_callback,
        'drop_rate': drop_rate,
        'max_profit_rate': max_profit_rate,
        'activation': ts_activation,
    }


def ts_activation_atr_mult():
    """발동선 계산에 쓰는 ATR 배수. 콜백 배수(TRAILING_ATR_MULTIPLIER)와 **분리**돼 있다.

    [왜 분리했나 · 2026-08-11] 한 키가 두 일을 겸하고 있었다 — 발동선(언제 무장하나)과
    콜백(얼마나 넓게 따라가나). 그래서 '발동을 앞당기자'고 배수를 낮추면 청산선까지 좁아져
    추세를 조기에 끊었고, 실측에서 수익승 0/15로 완패했다(config.TRAILING_ATR_MULTIPLIER 주석).
    콜백을 3.5로 고정한 채 발동선만 낮추는 반사실을 재보니 **fat-tail이 무손상**이었다
    (상위10% 40.0 → 36.1, 최대 163.4 → 165.2, >30% 9건 동일). 두 축은 별개였던 것이다.
    미설정(0 이하)이면 종전처럼 콜백 배수를 그대로 쓴다.
    """
    ss = config.SELL_STRATEGY
    try:
        m = float(ss.get("TS_ACTIVATION_ATR_MULTIPLIER", 0) or 0)
    except (TypeError, ValueError):
        m = 0.0
    if m > 0:
        return m
    return ss.get("TRAILING_ATR_MULTIPLIER", 3.5)


def breakeven_activation_rate(atr, buy_price, ts_callback=None, ts_atr_mult=None, use_atr=True):
    """손익분기 연동 TS 발동선(%)을 구한다. 매수가 기준의 되돌림 폭으로 환산한다.

    되돌림 폭 cb(매수가 대비 %)를 한 번 맞고도 본전이 되려면 MFE가 cb/(1-cb) 이상이어야 한다
    (고점×(1-cb) ≥ 매수가). cb는 실효 콜백과 같은 재료(max(하한, ATR×배수))를 쓰되 **매수가**로
    정규화한다 — 고점으로 정규화하면 고점이 오를수록 문턱이 낮아져 무장이 사실상 즉시 이뤄진다.

    ATR을 못 구하면 콜백 하한만으로 계산한다(그래도 고정 %보다 일관된다).
    산출 불가하면 config의 고정 발동률로 되돌린다.
    """
    ss = config.SELL_STRATEGY
    if ts_callback is None:
        ts_callback = ss.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
    if ts_atr_mult is None:
        ts_atr_mult = ts_activation_atr_mult()
    try:
        buy_price = float(buy_price or 0)
        atr = float(atr or 0)
    except (TypeError, ValueError):
        return ss.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    if buy_price <= 0:
        return ss.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)

    cb = float(ts_callback)
    if use_atr and atr > 0:
        cb = max(cb, atr * ts_atr_mult / buy_price * 100)
    cb = min(cb, 60.0)          # 초고변동 종목에서 문턱이 발산하지 않게 상한을 둔다
    act = cb / (100 - cb) * 100
    # [발동선 상한] 산식은 그대로 두고 결과에만 뚜껑을 씌운다. 고ATR 종목에서만 구속하므로
    #  평시 종목의 발동선은 손대지 않는다(0 이하 = 캡 해제).
    cap = ss.get("TS_ACTIVATION_MAX_RATE", 0) or 0
    return min(act, float(cap)) if cap > 0 else act


def ts_activation_label(ts_activation=None):
    """TS 발동 기준을 화면·로그용 문구로. 표시부가 설정값을 각자 해석하지 않게 모은다.

    breakeven 모드에서는 발동 시점이 종목 변동성에 따라 달라져 하나의 %로 적을 수 없다
    (콜백이 넓을수록 늦게 무장). 고정 수치를 그대로 찍으면 실제 동작과 어긋나므로
    '손익분기'로 표기한다. compute_trailing_stop이 돌려주는 activation은 그 종목의
    환산값이므로, 개별 포지션 화면은 그 값을 넘겨 구체적인 %를 보여줄 수 있다.
    """
    ss = config.SELL_STRATEGY
    if str(ss.get("TS_ACTIVATION_MODE", "fixed")).lower() == "breakeven":
        if ts_activation is None:
            return "손익분기"
        return f"손익분기(≈+{ts_activation:.1f}%)"
    rate = ts_activation if ts_activation is not None else ss.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    return f"+{rate}%"


def ts_activation_dynamic():
    """발동선이 종목마다 달라지는 체제인가. (화면이 개별 %를 병기할지 판단)

    고정 모드에서는 전 종목 같은 상수라 행마다 찍으면 잡음일 뿐이지만, breakeven
    모드에서는 종목 변동성에 따라 20%~90%까지 벌어져 그 값이 없으면 화면만 보고
    무장 여부를 설명할 수 없다. 표시부가 모드 문자열을 각자 해석하지 않게 모은다.
    """
    return str(config.SELL_STRATEGY.get("TS_ACTIVATION_MODE", "fixed")).lower() == "breakeven"


# [변동성 국면] 지수 실현변동성의 장기 대비 배율. 손절 캡을 국면에 맞춰 넓히는 데 쓴다.
#  실매매는 trader가 주기마다 갱신하고, 백테스트는 날짜별 값을 vol_ratio 인자로 직접 준다.
#  기본 1.0 = 평시 = 캡이 MAX_ATR_STOP_LOSS_RATE 그대로.
_VOL_REGIME_RATIO = 1.0


def set_vol_regime_ratio(ratio):
    """실매매용 — 주기마다 지수 변동성 배율을 갱신한다. 이상값은 무시하고 1.0을 유지한다."""
    global _VOL_REGIME_RATIO
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return
    if r > 0 and math.isfinite(r):
        _VOL_REGIME_RATIO = r


def get_vol_regime_ratio():
    return _VOL_REGIME_RATIO


def effective_atr_stop_cap(vol_ratio=None):
    """[SSOT] 지금 적용할 ATR 손절 캡(%, 음수)을 돌려준다.

    캡 = MAX_ATR_STOP_LOSS_RATE × 배율^power, 상·하한으로 클립.

    [왜 동적인가] 고정 -15%는 평시엔 이상치만 잘라내지만 고변동 국면에서는 상시 구속해
    ATR 적응 손절을 사실상 고정 손절로 만든다(실측 2026-08-09: 2026-07 봉의 66.4%가
    구속, 손절폭 중앙 17.4%). 그러면 변동성 상위 = 대개 모멘텀 상위 종목의 청산선이
    노이즈 안으로 들어온다 — 추세추종에서 가장 비싼 쪽이다.

    [왜 제곱근인가] 배율을 그대로 반영하면 고변동 국면에서 캡이 하한까지 가 사실상
    해제된다(2026년 중앙 -35%). 제곱근은 같은 방향이되 완만하다 — 배율 3배에서 1.73배만
    넓어진다(2026년 중앙 -26%, 구속률 26.2%→2.3%).

    [실측 2026-08-09 / 41종목·10년·5구간·15회 짝비교] 구간2·4에서는 수치가 소수점까지
    동일하다 — 그 국면에서는 캡이 애초에 걸리지 않아 아무 일도 하지 않는다. 유일하게
    움직인 구간5(최근 2년)에서 수익 140.0 vs 140.5·MDD 동일인데 상위10% 74.3 vs 72.0,
    최대 185.1 vs 170.2로 fat-tail만 개선됐다. 즉 '평시 무해 + 고변동 국면에서만 작동'.
    총수익 우위는 없다 — 채택 근거는 성과가 아니라 **비용이 0으로 측정된 보험**이다.
    """
    ss = config.SELL_STRATEGY
    base = ss.get("MAX_ATR_STOP_LOSS_RATE", -15.0)
    if not base:
        return base                                    # 0 = 캡 미사용
    if not ss.get("ATR_CAP_DYNAMIC", True):
        return base

    r = _VOL_REGIME_RATIO if vol_ratio is None else vol_ratio
    try:
        r = float(r)
    except (TypeError, ValueError):
        return base
    if not (r > 0 and math.isfinite(r)):
        return base

    power = float(ss.get("ATR_CAP_VOL_POWER", 0.5))
    cap = base * (r ** power)
    floor = float(ss.get("ATR_CAP_FLOOR", -35.0))      # 가장 넓게 허용
    ceil = float(ss.get("ATR_CAP_CEIL", -6.0))         # 가장 좁게 허용
    return max(floor, min(ceil, cap))


def atr_stop_rate(atr, price, atr_mult=None, max_cap=None, vol_ratio=None):
    """ATR 손절률(%, 음수)을 구한다. 산출 불가하면 None. (부수효과 없음)

    매수 체결 시 trades.stop_loss_rate에 굳는 값과 같은 식이다. 신규 매수·피라미딩·
    보유 분석이 각자 같은 식을 복제하고 있어 캡(MAX_ATR_STOP_LOSS_RATE) 적용 여부가
    갈릴 위험이 있어 SSOT로 모은다.

    max_cap을 명시하지 않으면 effective_atr_stop_cap()이 정한다 — 즉 동적 캡이 켜져 있으면
    여기를 지나는 **모든 경로**(신규 매수·피라미딩·보유 분석·백테스트 2종)가 자동으로 따른다.
    vol_ratio는 백테스트가 '그 날짜의' 배율을 주입하는 통로다(실매매는 모듈 상태를 쓴다).
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
        max_cap = effective_atr_stop_cap(vol_ratio)
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


def rule_value(rule, key, default):
    """개별 룰의 값을 읽되, NULL이면 전역 기본값으로 되돌린다.

    [중요] rule.get(key, default)를 쓰면 안 된다. dict.get은 **키가 존재하되 값이 None이면
    default가 아니라 None을 돌려준다.** stock_strategies는 SELECT * 로 모든 컬럼을 싣고
    오므로, 사용자가 지정하지 않은 항목이 정확히 그 상태다. 그 None이 판정의 비교식으로
    들어가면 TypeError가 나고, 예외를 회수하지 않는 루프에서는 그 종목만 조용히 사라진다.
    """
    v = (rule or {}).get(key)
    return default if v is None else v


def normalize_weights(w):
    """스코어링 가중치를 dict로 확정한다.

    DB에는 JSON 문자열로 저장되고 _enrich_rules_with_weights가 dict로 바꿔 준다. 그 보강이
    실패하면(가상투자에서 실계좌 DB를 열던 문제 등) 문자열이 그대로 흘러 calculate_score의
    weights.get()에서 AttributeError가 난다 — 점수 계산은 매수·매도 판정 양쪽의 심장이라
    여기서 죽으면 그 종목이 판정에서 통째로 빠진다(2026-08-05 NAVER).
    """
    if isinstance(w, str):
        try:
            w = json.loads(w)
        except Exception as e:
            logger.warning(f"개별 룰 가중치 파싱 실패 — 전역 가중치로 판정한다: {e}")
            return config.SCORING_WEIGHTS
    return w if isinstance(w, dict) else config.SCORING_WEIGHTS


def build_buy_thresholds(rule=None, score_adj=0.0):
    """매수 판단(analyze_buy)에 넘길 임계값을 조립한다. (부수효과 없음)

    매도 경로의 build_sell_thresholds와 같은 규약을 쓴다 — 룰의 NULL 컬럼은 전역 기본값으로
    되돌리고, 가중치는 dict로 확정한다. 개별 룰이 걸렸다는 이유로 종목이 분석 결과 없이
    사라지는 일이 없어야 한다.
    """
    at = config.ANALYSIS_THRESHOLDS
    if not rule:
        return {"BUY_SCORE": at["BUY_SCORE"] + score_adj, "WEIGHTS": config.SCORING_WEIGHTS}

    return {
        # 개별 룰의 매수 기준은 시장 국면 보정을 대체한다(사용자가 못 박은 절대값).
        # 다만 룰에 값이 없으면 룰 없는 종목과 같은 기준으로 돌아가야 한다.
        "BUY_SCORE": rule_value(rule, 'buy_score', at["BUY_SCORE"] + score_adj),
        "BUY_RSI_MAX": rule_value(rule, 'buy_rsi', at["BUY_RSI_MAX"]),
        "BUY_VOL_STRENGTH": rule_value(rule, 'buy_vol_strength', at.get("BUY_VOL_STRENGTH", 100.0)),
        "BUY_ASK_BID_RATIO": rule_value(rule, 'buy_ask_bid_ratio', at.get("BUY_ASK_BID_RATIO", 1.0)),
        "AUTO_ADJUST_ASK_BID_RATIO": bool(rule_value(rule, 'auto_adjust_ask_bid_ratio',
                                                     at.get("AUTO_ADJUST_ASK_BID_RATIO", True))),
        "WEIGHTS": normalize_weights(rule_value(rule, 'weights', config.SCORING_WEIGHTS)),
    }


def build_sell_thresholds(rule=None, score_adj=0.0, buy_trades=None, fallback_atr_rate=None):
    """보유 종목의 매도 판단(analyze_sell)에 넘길 임계값을 조립한다. (부수효과 없음)

    시스템 트레이딩 루프(_check_sell_conditions)와 잔고 화면의 보유 분석이 같은
    임계값을 쓰도록 SSOT로 둔다.

    [손절률 우선순위] 진입 시 기록된 ATR 손절률(수량가중) > 전역 설정. 개별 룰의
    stop_loss는 **기록값보다 타이트할 때만** 이를 덮는다.

      · 조이는 방향은 받는다 — 운용자가 그 종목만 빨리 자르겠다는 명시적 지시이고,
        손실 상한이 줄어들 뿐이라 리스크 한도를 깨지 않는다.
      · 넓히는 방향은 거부한다 — 포지션 크기는 진입 시점의 손절폭을 전제로 계산됐다.
        사후에 손절을 넓히면 그 포지션의 실제 손실이 사이징이 가정한 상한을 넘는다
        (자본대비 리스크 한도가 명목만 남는다).

    종전에는 ATR 기록값이 룰을 무조건 덮어써서, 운용자가 룰로 손절을 조여도 **조용히
    무시**됐다(룰에서 use_atr_stop=False까지 함께 꺼야만 반영 — 발견하기 어렵다).
    docstring은 '개별 룰 최우선'이라 적혀 있어 코드와 반대였다.

    fallback_atr_rate: 매수 기록이 없어 ATR 손절률을 못 구할 때 쓸 복원값
                       (entry_atr_stop_rate). 기록에서 구한 값이 항상 우선한다.
    """
    def _rv(key, default):
        return rule_value(rule, key, default)

    if rule:
        thresholds = {
            "TAKE_PROFIT_RATE": _rv('take_profit', config.SELL_STRATEGY["TAKE_PROFIT_RATE"]),
            "STOP_LOSS_RATE": _rv('stop_loss', config.SELL_STRATEGY["STOP_LOSS_RATE"]),
            "TAKE_PROFIT_RSI": _rv('take_profit_rsi', config.SELL_STRATEGY["TAKE_PROFIT_RSI"]),
            "SELL_SCORE": _rv('sell_score', config.SELL_STRATEGY["SELL_SCORE"]),
            "WEIGHTS": normalize_weights(_rv('weights', config.SCORING_WEIGHTS)),
            # 룰의 매수 기준은 시장 국면 보정을 대체한다(사용자가 못 박은 값). 다만 룰에
            # 값이 없으면 룰 없는 종목과 같은 기준(전역 + 국면 보정)으로 돌아가야 한다.
            "BUY_SCORE": _rv('buy_score', config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj),
            # [Fix] 개별 룰의 RSI 상한을 매도 경로에도 전달한다.
            #  analyze_sell도 classify_stock_state로 상태를 재판정하는데,
            #  이 키가 없으면 전역 BUY_RSI_MAX로 폴백해, 같은 종목·같은 시각인데도
            #  매수 경로/메뉴 2 화면과 상태가 갈렸다(룰 RSI ≠ 전역 RSI인 보유 종목).
            "BUY_RSI_MAX": _rv('buy_rsi', config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]),
            "TIME_STOP_DAYS": _rv('time_stop_days', config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20)),
            "HALF_TAKE_PROFIT_USE": bool(_rv('half_take_profit_use',
                                             config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False))),
            # [Fix] 개별 룰의 TS 발동/콜백을 analyze_sell에 실제로 전달
            "ts_activation": _rv('ts_activation', config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)),
            "ts_callback": _rv('ts_callback', config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)),
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
        # 룰이 더 타이트하면 룰을 살린다(위 우선순위 주석 참조). 손절률은 음수이므로
        #  '더 타이트' = 0에 더 가까움 = 더 큼.
        rule_sl = None
        if rule:
            try:
                rv = float(rule_value(rule, 'stop_loss', 0) or 0)
                if rv < 0:
                    rule_sl = rv
            except (TypeError, ValueError):
                rule_sl = None

        if rule_sl is not None and rule_sl > applied_sl_rate:
            thresholds["STOP_LOSS_RATE"] = rule_sl
            thresholds["ATR_APPLIED_SL_RATE"] = rule_sl
            return thresholds

        if rule_sl is not None and rule_sl < applied_sl_rate:
            logger.info(
                f"[손절 우선순위] 개별 룰의 손절({rule_sl:.2f}%)이 진입 시 기록된 "
                f"ATR 손절({applied_sl_rate:.2f}%)보다 넓어 적용하지 않습니다 — "
                f"포지션 크기가 기록값을 전제로 계산되어 있어, 넓히면 실제 손실이 "
                f"사이징이 가정한 상한을 넘습니다.")

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
    수량 흐름으로 잰다.

    우선순위:
      1) 수량 재생으로 구한 진입일 — 시스템 DB와 증권사 체결 내역 중 더 이른 쪽
         (analyze_holdings가 두 소스를 합쳐 넘긴다)
      2) 증권사 체결 재생 결과 — 1)이 비어 있을 때 (형식 변환 포함)
      3) 최근 매수 기록 — 위가 모두 없을 때의 마지막 근사치
    3)을 마지막에 두는 이유: 분할 매수 때마다 보유일수를 리셋시켜 시간청산이 걸리지 않는다.
    """
    if entry_date:
        return str(entry_date)[:10]

    if fallback_buy_date:
        try:
            s = (fallback_buy_date if isinstance(fallback_buy_date, str)
                 else fallback_buy_date.strftime("%Y%m%d")).replace('-', '').strip()
            if len(s) == 8 and s.isdigit():
                return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        except Exception:
            pass

    if last_buy and last_buy.get('time'):
        return str(last_buy['time'])[:10]

    return None


def holding_profit_rate(item):
    """잔고 한 줄에서 평가손익률(%)을 구한다. 구할 수 없으면 None. (부수효과 없음)

    [왜 함수로 빼는가] `float(item.get('evlu_pfls_rt') or 0.0)` 은 '없음'을 0%로 바꾼다.
    0%는 손절선(음수)보다 위라, 손절 이탈 판정이 '아직 괜찮다'로 뒤집힌다. 증권사 어댑터는
    일부 필드를 0/누락으로 주므로(같은 이유로 pchs_amt 는 이미 수량×평단으로 복원한다)
    없으면 평단과 현재가로 직접 구하고, 그것도 안 되면 **모른다고 답한다**.
    """
    raw = (item or {}).get('evlu_pfls_rt')
    if raw not in (None, "", "-"):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    try:
        avg = float((item or {}).get('pchs_avg_pric') or 0)
        cur = float((item or {}).get('prpr') or 0)
    except (TypeError, ValueError):
        return None
    if avg <= 0 or cur <= 0:
        return None
    return (cur - avg) / avg * 100.0


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
    # ------------------------------------------------------------------ 진입일
    # 진입일 = 누적 보유수량이 0에서 1 이상으로 바뀐 시점. 분할 매수·부분 매도가 섞여도
    #  정확하다. 두 소스를 모두 재생하고 '더 이른 쪽'을 쓴다.
    #
    #  [왜 둘 다 보나] 시스템 DB는 증권사 이력의 '부분 사본'이다. HTS·MTS로 직접 매매한
    #   포지션은 시스템을 쓰기 시작한 뒤부터만 기록되므로 DB의 첫 기록이 '0 → 1 이상'처럼
    #   보여 진입일이 그 날짜로 굳는다(실측: 228주 보유인데 DB엔 2주만 기록 → 37일 vs 실제 107일).
    #   증권사 체결 내역은 계좌의 원본이라 DB보다 과거까지 닿는다.
    #  [왜 더 이른 쪽인가] 두 소스 모두 수량 흐름으로 판정하므로 서로 어긋나면 이력이 더
    #   많은 쪽이 이긴다. 보유일수는 '자본이 얼마나 오래 묶였나'이므로 과소평가(시간청산
    #   지연·TS 앵커 오차)가 과대평가보다 위험하다.
    entry_info_map = _safe(lambda: db_manager.db.get_position_entry_info(codes), {})

    # 진입이 조회 구간보다 과거인지 판별하려면 현재 보유수량이 필요하다.
    qty_map = {e['code']: e['qty'] for e in entries if e.get('qty') is not None}
    # 국내 보유분만 대상 (해외는 이 TR이 없다). 수동 분석은 입력한 보유일수를 그대로 쓴다.
    broker_targets = [e['code'] for e in entries
                      if not e.get('is_overseas') and e.get('holding_days') is None]
    broker_entry_dates = _safe(
        lambda: api.get_period_entry_dates(broker_targets, qty_map=qty_map), {}
    ) if broker_targets else {}

    def _norm_date(v):
        """'YYYYMMDD' / 'YYYY-MM-DD ...' → 'YYYY-MM-DD'. 판독 불가면 None."""
        if not v:
            return None
        d = str(v).replace('-', '').strip()[:8]
        return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else None

    entry_date_map = {}
    for e in entries:
        code = e['code']
        db_date = _norm_date((entry_info_map.get(code) or {}).get('date'))
        broker_date = _norm_date(broker_entry_dates.get(code))
        picked = min([d for d in (db_date, broker_date) if d], default=None)
        if picked:
            entry_date_map[code] = picked
        if db_date and broker_date and db_date != broker_date:
            logger.debug(f"[진입일] {code} DB {db_date} vs 증권사 {broker_date} → {picked} 채택")

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
            broker_date = broker_entry_dates.get(code)
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
            # [관측성] DEBUG로 두면 잔고 화면에서 그 종목만 '-'로 비고 이유가 어디에도
            #  남지 않는다. 보유 종목의 판정 실패는 보호 공백이므로 항상 남긴다.
            logger.warning(f"보유분석 실패 {code}: {type(e).__name__}: {e}", exc_info=True)
            return code, None

    if max_workers is None:
        max_workers = 2 if config.session.is_simulation else 4
    max_workers = max(1, min(max_workers, len(entries)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="at_engine") as executor:
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
        # [2026-08-04] BEP는 기본 OFF (config.SELL_STRATEGY 주석의 반사실 실측 참조).
        #  끄더라도 max_profit_rate는 트레일링·표시에 쓰이므로 계산은 유지한다.
        use_bep = (thresholds.get("USE_BREAK_EVEN_STOP", config.SELL_STRATEGY.get("USE_BREAK_EVEN_STOP", False))
                   if thresholds else config.SELL_STRATEGY.get("USE_BREAK_EVEN_STOP", False))
        if highest_price > 0 and buy_price > 0:
            max_profit_rate = ((highest_price - buy_price) / buy_price) * 100
            if use_bep and max_profit_rate >= bep_activation:
                if sl_rate < bep_stop:
                    sl_rate = bep_stop
                    is_bep_applied = True

        # [이익 보호선] 무장 전 구간의 공백을 메운다. TS가 켜지면 그쪽이 더 높아 자연히 무의미해진다.
        is_lock_applied = False
        use_lock = (thresholds.get("PROFIT_LOCK_USE", config.SELL_STRATEGY.get("PROFIT_LOCK_USE", False))
                    if thresholds else config.SELL_STRATEGY.get("PROFIT_LOCK_USE", False))
        if use_lock and buy_price > 0:
            lock_rate = profit_lock_stop_rate(
                max_profit_rate,
                (thresholds or {}).get("PROFIT_LOCK_MIN_MFE"),
                (thresholds or {}).get("PROFIT_LOCK_GIVEBACK"))
            if lock_rate is not None and lock_rate > sl_rate:
                sl_rate = lock_rate
                is_lock_applied = True
                is_bep_applied = False   # 표시·사유가 겹치지 않게 더 높은 쪽만 남긴다

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
            if is_lock_applied:
                reason = f"이익보호({profit_rate:.1f}%, 고점 {max_profit_rate:.1f}%)"
            elif is_bep_applied:
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
            'is_lock_applied': is_lock_applied,
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
        # [안전장치] '고아 주문' 경보를 이미 보낸 주문번호. API 미체결 목록에서 사라졌는데
        #  로컬 폴백(ORDER_SENT 전용)도 건드리지 않는 상태(ACCEPTED·PARTIAL_FILLED)라
        #  어느 경로로도 pending이 풀리지 않는 주문이다. 주문 종결 시 항목을 지운다.
        self.orphan_alerted = set()
        # [안전장치] 주문 실패 알림 억제 이력 {(code, 매매구분, 오류코드): 마지막 알림 시각}.
        #  거부는 상태를 정리하고 끝나므로 다음 주기에 같은 값으로 재시도한다. 그 자체는
        #  옳다(제한폭이 풀리면 체결돼야 한다). 다만 하한가에 하루 종일 락되면 3분마다
        #  같은 실패가 반복되어 알림이 100건 넘게 쌓이고, 정작 중요한 경보가 묻힌다.
        #  **같은 원인**의 반복만 억제한다 — 원인이 바뀌면 즉시 다시 알린다.
        self.order_fail_alerted = {}
        # [최적화] 누적 주문 접수 카운터 — 루프에서 '이번 주기에 주문이 나갔는가'를 판단해
        #  주문이 없으면 루프 말미 잔고/예수금 재조회를 생략하기 위한 단조 증가 값
        self.orders_sent_count = 0
        self._lock = threading.RLock()

    def is_pending(self, code):
        """특정 종목의 진행 중인 주문 존재 여부 확인.

        [주의] 빈 dict는 '대기 없음'이다. 키 존재만 보면 주문이 모두 종결됐는데도
        True가 되어 그 종목이 매도 판정에서 영구히 빠진다 — 손절이 조용히 꺼진다.
        """
        with self._lock:
            return bool(self.pending_orders.get(code))

    def pending_odnos(self, code):
        """해당 종목의 대기 주문번호 목록(진단용)."""
        with self._lock:
            return list((self.pending_orders.get(code) or {}).keys())

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
                        self.orphan_alerted.discard(str(odno))
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

    def _track_open_order(self, code, odno):
        """거래소에 살아 있는 주문을 메모리 추적에 올린다(이미 있으면 상태 유지).

        이미 추적 중인 주문의 상태(ACCEPTED·PARTIAL_FILLED)를 ORDER_SENT로 되돌리면
        안 된다 — 로컬 폴백 취소가 ORDER_SENT만 보므로 진행된 주문이 다시 취소 대상이 된다.
        """
        with self._lock:
            if self.pending_orders.get(code, {}).get(odno) is not None:
                return False
            self.pending_orders.setdefault(code, {})[odno] = OrderStatus.ORDER_SENT
            return True

    def restore_pending_orders(self, cano=None, acnt=None):
        """[재기동 복구] 거래소의 미체결 주문을 메모리 추적에 되살린다.

        pending_orders는 메모리에만 있어서 재기동하면 비고, is_pending(code)가 False가
        되는 순간 그 종목은 '주문 없음'으로 보인다. 그러면
          · 매수: 후보 필터를 그대로 통과해 **두 번째 매수 주문**이 나간다(중복 진입,
            의도한 리스크의 2배). 잔고에도 안 잡히므로 보유 종목 수 게이트도 못 막는다.
          · 매도: 이미 매도 주문이 걸려 매도가능수량이 0인데 거래정지로 오인해
            '매도 실패' 경보를 낸다.
        manage_unfilled_orders도 같은 복구를 하지만 매수·매도 검사 **뒤에** 돌기 때문에,
        첫 주기의 노출을 막으려면 시작 시점에 한 번 더 채워야 한다.

        반환: 조회 성공 여부. 실패는 '미체결이 없다'와 구분해야 한다 — 호출부는 실패 시
        신규 매수를 보류한다(모르는 상태로 주문을 더 내는 것이 가장 나쁘다).
        """
        try:
            open_orders = api.get_domestic_open_orders(cano, acnt)
        except Exception as e:
            self.trader.log(f"[재기동 복구] 미체결 주문 조회 실패: {e}")
            return False
        if open_orders is None:
            self.trader.log("[재기동 복구] 미체결 주문 조회 실패 (응답 없음)")
            return False

        restored = []
        for item in open_orders:
            odno, code = item.get('odno'), item.get('pdno')
            if not odno or not code:
                continue
            if api.safe_int(item.get('rmn_qty', 0)) <= 0:
                continue
            if self._track_open_order(code, odno):
                restored.append(f"{item.get('prdt_name') or code}({code}) No.{odno}")

        if restored:
            self.trader.log(f"[재기동 복구] 미체결 주문 {len(restored)}건을 추적에 복원: "
                            + ", ".join(restored))
        return True

    #  같은 원인의 주문 실패를 다시 알리기까지의 간격(초). 주기가 180초이므로 30분이면
    #  하루 종일 락된 종목의 알림이 6시간에 12건 수준으로 줄어든다(종전 120건).
    ORDER_FAIL_ALERT_COOLDOWN = 1800.0

    def _should_alert_order_fail(self, code, type_str, msg_cd):
        """이번 주문 실패를 텔레그램으로 알릴 것인가.

        억제하는 것은 **알림뿐이고 재시도가 아니다** — 제한폭이 풀리거나 예수금이 들어오면
        다음 주기에 체결돼야 하므로 주문 시도 자체는 계속한다. 로그에도 항상 남긴다.
        키에 오류코드를 넣어, 원인이 바뀌면(예: 제한폭 → 예수금 부족) 즉시 다시 알린다.
        """
        key = (str(code), str(type_str), str(msg_cd))
        now = time.time()
        with self._lock:
            last = self.order_fail_alerted.get(key, 0.0)
            if now - last < self.ORDER_FAIL_ALERT_COOLDOWN:
                return False
            self.order_fail_alerted[key] = now
        return True

    def send_order(self, code, qty, type_str, name=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, price=0, rule=None, stop_loss_rate=0.0, buy_price=0.0):
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
            # [계좌 라우팅 방어선] 시스템 트레이딩의 모든 주문은 이 함수 하나를 지난다
            #  (신규 매수·피라미딩·손절/트레일링 매도). 호출 스레드가 무엇이든 여기서
            #  자동매매 계좌를 명시 고정한다 — 워커 스레드는 계좌 컨텍스트를 상속하지
            #  않으므로(threading.local) 이 가드가 없으면 수동 계좌로 주문이 샌다.
            with utils.AccountContext(utils.system_trading_account()[0]):
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
                    # 한 번이라도 접수되면 그 종목의 억제 이력을 지운다 — 이후 다시 실패하면
                    #  '새로 생긴 문제'이므로 쿨다운을 기다리지 않고 알려야 한다.
                    for k in [k for k in self.order_fail_alerted if k[0] == str(code)]:
                        del self.order_fail_alerted[k]

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
                db_manager.db.insert_trade(f"{type_str}(AUTO)", code, name, qty, str(price), odno, snapshot=snapshot, profit_amt=profit_amt, profit_rate=profit_rate, reason=reason, score=score, stop_loss_rate=stop_loss_rate, buy_price=buy_price)
                
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
                # [안전장치] 같은 종목·같은 원인의 반복 실패는 알림을 억제한다. 로그는 항상 남긴다.
                if self._should_alert_order_fail(code, type_str, msg_cd):
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
            if unfilled_list is not None:
                # 조회가 됐다 = 미체결 현황을 안다. 시작 시 복구가 실패해 보류됐던
                # 신규 매수를 여기서 푼다(운영자 개입 없이 자동 복구되어야 한다).
                self.trader.pending_restore_ok = True

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
                        
                        self.trader.log(f"[외부 주문 감지] {name}({code}) {sll_buy_name} {qty}주 (No.{odno})")
                        msg = f"📡 [{sll_buy_name} 외부접수] {name}({code})\n수량: {qty}주\n단가: {int(price):,}원\n주문번호: {utils.format_order_no(odno)}\n사유: 앱(MTS)/HTS 등 외부 주문 감지"
                        api.send_telegram_message(msg)

                        trade = db_manager.db.get_trade_by_odno(odno)

                    # [안전장치] 거래소에 살아 있는 주문은 **DB에 기록이 있든 없든** 메모리
                    #  추적에 올린다. 종전에는 이 등록이 '외부 주문'(DB에 없는 주문) 분기
                    #  안에만 있어서, 재기동 후에는 자기가 낸 주문이 DB에 있다는 이유로
                    #  건너뛰어졌다. pending_orders는 메모리라 재기동하면 비는데, is_pending이
                    #  False가 되면 같은 종목에 **두 번째 주문**이 나간다(중복 진입).
                    self._track_open_order(code, odno)

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

            # 3. [안전장치] 어느 경로로도 풀리지 않는 '고아' 주문 경보
            self._alert_orphan_pending(api_checked_odnos, cancel_seconds, now)
        except Exception as e:
            self.trader.log(f"미체결 관리 중 오류: {e}")

    # 고아 판정 유예 배수 — 취소 타임아웃의 몇 배가 지나야 '비정상'으로 볼 것인가.
    #  ACCEPTED(정상 접수 대기)를 곧바로 경보하면 평범한 지정가 대기가 전부 알림이 된다.
    ORPHAN_ALERT_GRACE = 2.0

    def _resolve_paper_orphan(self, code, odno):
        """가상투자에서 체결이 확인된 고아 주문을 FILLED로 정리한다. (정리했으면 True)

        실계좌에서는 하지 않는다 — 살아 있는 주문을 임의로 풀면 매수를 열어 둔 채 매도가
        나가 서로 싸운다. 가상투자는 즉시 전량 체결 모델이라 그 모호성이 없고, 체결
        기록(paper_fills)으로 사실을 확인할 수 있다.
        """
        try:
            from modules import paper_broker
            if not paper_broker.is_active():
                return False
            fill = paper_broker.get_fill_by_odno(odno)
        except Exception as e:
            logger.debug(f"[PAPER] 고아 주문 체결 확인 실패({odno}): {e}")
            return False

        if not fill:
            return False   # 체결 기록이 없으면 추측하지 않는다 — 기존 경보 경로로 넘긴다

        self.trader.log(f"[가상체결 복구] {fill.get('name') or code}({code}) No.{odno} — "
                        f"체결 기록 확인({fill.get('type')} {fill.get('qty')}주 "
                        f"@{fill.get('price', 0):,.0f}) → 주문 상태를 체결로 정리합니다")
        self.update_order_status(code, odno, OrderStatus.FILLED)
        return True

    def _order_settled_at_exchange(self, odno):
        """이 주문이 거래소에서 **종결**됐는지 당일 주문내역으로 확인한다.

        반환: True=종결(잔량 0 — 전량 체결이든 취소든 더 이상 살아 있지 않다)
              False=아직 살아 있다(잔량 > 0)
              None=확인 불가(조회 실패·내역에 없음) — 단정하지 않는다

        [왜 이게 필요한가] 고아 pending을 자동으로 풀지 못한 이유는 하나였다.
        "주문이 실제로 살아 있는데 pending을 풀면 매수를 열어 둔 채 매도가 나가 서로
        싸운다." 맞는 걱정이지만, 그 전제는 '살아 있는지 알 수 없다'는 것이다.
        당일 주문내역을 보면 알 수 있다 — 주문 무응답 대사(api._reconcile_unknown_order)가
        이미 같은 방식을 쓴다. 확인된 것만 풀면 그 위험이 성립하지 않는다.
        """
        try:
            hist = api.get_today_history()
            rows = (hist or {}).get('output1') or []
        except Exception as e:
            logger.debug(f"[고아주문] 당일 주문내역 조회 실패({odno}): {e}")
            return None
        if not rows:
            return None
        target = _norm_odno(odno)
        for r in rows:
            if _norm_odno(r.get('odno')) != target:
                continue
            try:
                # 잔량이 곧 '살아 있는 수량'이다. 체결/취소 어느 쪽이든 0이면 종결이다.
                return int(float(r.get('rmn_qty') or 0)) <= 0
            except (TypeError, ValueError):
                return None
        return None      # 내역에 없다 — 조회 범위 밖일 수 있으므로 단정하지 않는다

    def _release_settled_orphan(self, code, odno):
        """거래소에서 종결이 확인된 고아 주문의 로컬 상태만 정리한다. (정리했으면 True)

        체결 기록 자체는 ConclusionMonitor가 당일 체결 내역에서 따로 남긴다 — 여기서
        체결을 '주장'하지 않는다. 하는 일은 pending 해제뿐이고, 그래야 그 종목이 다음
        주기부터 다시 손절·트레일링 판정을 받는다.
        """
        if self._order_settled_at_exchange(odno) is not True:
            return False
        with self._lock:
            orders = self.pending_orders.get(code) or {}
            if odno not in orders:
                return False
            del orders[odno]
            if not orders:
                self.pending_orders.pop(code, None)
            self.sell_pre_qty.pop(str(odno), None)
        self.trader.log(f"[고아주문 해제] {code} No.{utils.format_order_no(odno)} — "
                        f"당일 주문내역에서 잔량 0(종결) 확인 → 대기 해제. "
                        f"이 종목의 손절·트레일링 판정이 다시 돌아갑니다")
        return True

    def _alert_orphan_pending(self, api_checked_odnos, cancel_seconds, now):
        """API에서 사라졌는데 로컬 폴백도 건드리지 않는 주문을 운영자에게 알린다.

        pending에서 빠지는 경로는 두 갈래뿐이다.
          ① update_order_status 가 FILLED/CANCELED/REJECTED 를 받는다 (체결 이력 API가 알려줘야 한다)
          ② 로컬 폴백이 강제 취소한다 — 단 `status == ORDER_SENT` 인 주문만 본다

        따라서 API가 상태를 한 번 진행시킨 뒤(ACCEPTED·PARTIAL_FILLED) 목록에서
        사라지면 ①도 ②도 걸리지 않아 pending이 세션 내내 유지된다. is_pending(code)가
        True인 동안 그 종목은 매도 워커에서 통째로 빠지므로, 손절 판정이 함께 멈춘다.

        자동 정리는 하지 않는다. 주문이 실제로 살아 있는데 pending을 풀면 매수를 열어 둔
        채 같은 종목에 매도가 나가 서로 싸운다. 취소 실패 경보와 같은 취급으로,
        운영자가 HTS에서 확인·정리하도록 알리기만 한다(주문당 1회).
        """
        try:
            with self._lock:
                snapshot = {c: dict(o) for c, o in self.pending_orders.items()}

            for code, orders in snapshot.items():
                for odno, status in orders.items():
                    if odno in api_checked_odnos:
                        continue
                    # 로컬 폴백은 모의투자에서만, 그것도 ORDER_SENT만 강제 취소한다.
                    #  그 조합에 해당할 때만 경보를 미룬다 — 실계좌는 폴백 자체가 돌지
                    #  않으므로 ORDER_SENT 고아도 똑같이 갇힌다.
                    if config.session.is_simulation and status == OrderStatus.ORDER_SENT:
                        continue
                    # [관찰 모드] 가상투자는 '살아 있는 주문'이라는 상태가 존재하지 않는다.
                    #  paper_broker는 즉시 전량 체결로 모델링하고 get_domestic_open_orders는
                    #  계약상 항상 []다. 따라서 여기 남은 주문은 체결이 상태기계에 반영되지
                    #  못한 누수일 뿐이라, 실계좌와 달리 '풀면 주문끼리 싸운다'는 위험이 없다.
                    #  체결 기록으로 실제 체결을 확인한 뒤에만 정리한다(추측으로 풀지 않는다).
                    #  방치하면 그 종목이 매수·매도 판정에서 통째로 빠진다 — 2026-08-05 실측:
                    #  손절 체결 후에도 ORDER_SENT가 남아 NAVER가 분석 화면에서 사라졌다.
                    if self._resolve_paper_orphan(code, odno):
                        continue

                    # [실계좌] 거래소에 물어 '종결'이 확인된 것만 푼다. 확인되지 않으면
                    #  종전대로 경보만 하고 둔다 — 추측으로 풀지 않는다.
                    if self._release_settled_orphan(code, odno):
                        continue

                    if str(odno) in self.orphan_alerted:
                        continue

                    trade = db_manager.db.get_trade_by_odno(odno)
                    if not trade or not trade.get('time'):
                        continue
                    try:
                        ord_time = datetime.strptime(trade['time'], "%Y-%m-%d %H:%M:%S")
                    except (ValueError, TypeError):
                        continue
                    elapsed = (now - ord_time).total_seconds()
                    if elapsed < cancel_seconds * self.ORPHAN_ALERT_GRACE:
                        continue

                    self.orphan_alerted.add(str(odno))
                    name = trade.get('name', code)
                    self.trader.log(
                        f"[고아 주문] {name}({code}) No.{odno} 상태={status} · 경과 {int(elapsed)}초 — "
                        f"API 미체결 목록에 없어 자동으로 풀리지 않습니다. 손절 판정이 멈춥니다.")
                    api.send_telegram_message(
                        f"⚠️ [주문 상태 불일치] {name}({code})\n"
                        f"주문번호: {utils.format_order_no(odno)}\n"
                        f"상태: {status} · 경과 {int(elapsed)}초\n\n"
                        f"증권사 미체결 목록에서는 사라졌는데 시스템에는 진행 중으로 남아 있습니다. "
                        f"이 상태가 유지되는 동안 이 종목은 손절 판정에서 제외됩니다. "
                        f"HTS/MTS에서 주문 상태를 확인해 주세요.")
        except Exception as e:
            self.trader.log(f"고아 주문 점검 실패: {e}")

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
                    # 발동 기준은 설정 모드를 따른다(고정 % / 손익분기 연동).
                    #  여기엔 ATR 시계열이 없지만 sl_rate가 매수 시점 ATR 손절률이므로
                    #  ATR/매수가 = |sl_rate| / ATR_STOP_MULTIPLIER 로 역산할 수 있다.
                    #  하한(콜백)만으로 근사하면 실제보다 훨씬 일찍 무장한 것으로 보여
                    #  손절선을 과대 상향 → 오픈 리스크를 과소평가한다(반대 방향 위험).
                    act = ts_act
                    if str(sell_cfg.get("TS_ACTIVATION_MODE", "fixed")).lower() == "breakeven":
                        atr_mult = sell_cfg.get("ATR_STOP_MULTIPLIER", 2.0) or 2.0
                        est_atr = abs(sl_rate) / 100.0 * buy_price / atr_mult if use_atr_stop else 0
                        act = breakeven_activation_rate(est_atr, buy_price, ts_cb,
                                                        use_atr=bool(use_atr_stop))
                    if act > 0 and ts_cb > 0 and max_profit >= act:
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

    def portfolio_risk_budget_left(self, avail_cash=None):
        """히트 캡까지 남은 리스크 예산(원). 캡 미사용(0)·기준자산 미확보 시 None(제한 없음)

        [리스크 스케일링] 약세 국면·드로다운 시 캡이 배수만큼 축소된다.
        기준자산은 최근 조회된 현재 평가자산을 우선 사용해(폴백: 당일 시작자산),
        장중 손실이 나면 예산도 함께 줄어드는 보수적 방향으로 동작한다."""
        cap = getattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0)
        if cap <= 0:
            return None       # 사용자가 캡을 껐다 — 의도된 '제한 없음'

        # [fail-closed] 아래 두 경우는 '제한 없음'이 아니라 '계산 불가'다. None(=게이트 통째로
        #  스킵)을 돌려주면 한도가 조용히 사라진다 — 데이터가 없을수록 열리는 구조가 된다.
        #  0을 돌려 신규 진입을 막고, 다음 주기에 자산·히트가 잡히면 저절로 풀린다.
        if getattr(self.trader, 'portfolio_heat_unknown', False):
            return 0.0        # 오픈 리스크를 못 셌다 — 얼마가 남았는지 말할 수 없다

        equity = getattr(self.trader, 'current_total_asset', 0) or self.trader.initial_asset
        if equity <= 0:
            # 기준자산이 없으면 캡의 분모가 없다. 다만 게이트를 여는 대신, allocate_budget이
            #  같은 상황에서 쓰는 폴백(예수금)을 그대로 쓴다 — 두 한도가 서로 다른 기준을
            #  보면 종목당 한도는 걸리는데 합산 한도는 안 걸리는 어긋남이 생긴다.
            equity = float(avail_cash or 0)
        if equity <= 0:
            return 0.0        # 자산도 예수금도 모른다 — 한도를 계산할 수 없다

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

