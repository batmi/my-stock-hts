# modules/auto_trade/common.py
"""공용 헬퍼: 제한종목/일일자산/ODNO 추적/장시간 판정/OrderStatus/룰 가중치

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


def format_holdings_block(valid_holdings, title="보유 종목 현황", name_decorator=None, analysis_results=None):
    """보유 종목 목록 블록(헤더 + 종목별 4줄) 생성.

    /status·/holdings·시스템 시작/종료 알림·장 시작/마감 알림이 모두 이 함수를 쓰도록 해
    표기(종목 수, 불릿, 평단 포함 여부)가 메시지마다 달라지지 않게 한다.
    name_decorator(code, name) -> str 를 주면 종목명에 제한/개별룰 표식을 덧붙일 수 있다.
    보유 종목이 없을 때의 문구는 호출부마다 부가 설명이 달라 각자 처리한다.
    """
    msg = f"📋 [{title}] ({len(valid_holdings)}종목)"
    for item in valid_holdings:
        name = item['prdt_name']
        qty = int(item['hldg_qty'])
        cur_price = int(item['prpr'])
        buy_price = float(item.get('pchs_avg_pric') or 0)
        eval_amt = int(item['evlu_amt'])
        profit = int(item['evlu_pfls_amt'])
        rate = float(item['evlu_pfls_rt'])

        name_display = name_decorator(item.get('pdno'), name) if name_decorator else name

        state_str = ""
        atr_str = ""
        ts_str = ""
        if analysis_results and item.get('pdno') in analysis_results:
            res = analysis_results[item['pdno']]
            
            # 상태
            state_val = res.get('state') or "-"
            if res.get('action') == 'sell':
                state_val = "청산"
            score = res.get('score')
            score_str = f" {score:.1f}" if isinstance(score, (int, float)) else ""
            auto_str = " 수동" if res.get('unmanaged') else " 자동"
            state_str = f"\n   상태: {state_val}{score_str}{auto_str}"
            
            # 손절가
            sl_rate = res.get('applied_sl_rate')
            if sl_rate is not None and sl_rate != 0 and buy_price > 0:
                if res.get('is_bep_applied'):
                    label = "BEP"
                elif res.get('is_atr_stop'):
                    label = "ATR"
                else:
                    label = "고정"
                stop_price = buy_price * (1 + sl_rate / 100)
                atr_str = f"\n   {label}: {round(stop_price):,} (-{abs(sl_rate):.1f}%)" if sl_rate < 0 else f"\n   {label}: {round(stop_price):,} (+{abs(sl_rate):.1f}%)"
            
            # TS
            ts = res.get('ts')
            if ts:
                if not ts.get('armed'):
                    ts_str = f"\n   TS: +{ts['activation']:.0f}% 도달 시 "
                else:
                    ts_str = f"\n   TS: {round(ts['stop_price']):,} (-{ts['callback']:.1f}%)"

        msg += (
            f"\n\n• {name_display} ({qty}주)"
            f"\n   현재: {cur_price:,}원 | 평단: {buy_price:,.0f}원"
            f"\n   평가: {eval_amt:,}원 | 손익: {profit:+,}원 ({rate:+.2f}%)"
            f"{state_str}{atr_str}{ts_str}"
        )
    return msg


def get_mystock_log_tail(lines=20):
    """에러 발생 시 전송할 mystock.log의 꼬리 부분을 반환합니다."""
    log_path = os.path.join(getattr(config, 'LOG_DIR', 'logs'), 'mystock.log')
    if not os.path.exists(log_path):
        return "로그 파일이 존재하지 않습니다."
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.readlines()
            tail = "".join(content[-lines:])
            if len(tail) > 2000: # 텔레그램 메시지 길이 제한 방어
                tail = tail[-2000:]
            return tail
    except Exception as e:
        return f"로그 읽기 실패: {e}"

# [추가] 거래 제한 종목 파일 경로 및 관리 함수
RESTRICTED_FILE = os.path.join(config.JSON_DIR, "restricted_stocks.json")

# [추가] 제한 종목 파일 동시 접근 보호용 락 (여러 데몬 스레드의 read-modify-write 경합 방지)
#        add/remove는 load→수정→save 전체를 이 락 안에서 수행하므로 재진입 가능한 RLock 사용
_RESTRICTED_LOCK = threading.RLock()

# [추가] 시스템(자동매매)이 직접 발주한 주문번호(ODNO) 추적 세트.
# DB insert가 큐를 경유(비동기)하므로 체결 모니터가 원주문을 못 읽어 '외부 주문'으로 오판하는
# 레이스를 막기 위해, 발주 즉시 메모리에 ODNO를 기록하고 외부주문 판정 시 우선 확인한다.
_SYSTEM_ODNOS = set()
_SYSTEM_ODNOS_LOCK = threading.Lock()

def register_system_odno(odno):
    """자동매매가 발주한 주문번호를 시스템 주문으로 등록한다."""
    if not odno:
        return
    with _SYSTEM_ODNOS_LOCK:
        _SYSTEM_ODNOS.add(str(odno))

def is_system_odno(odno):
    """해당 주문번호가 시스템(자동매매)이 낸 주문인지 여부."""
    if not odno:
        return False
    with _SYSTEM_ODNOS_LOCK:
        return str(odno) in _SYSTEM_ODNOS

def is_system_trade(trade_type, odno=None):
    """이 체결이 **시스템(자동매매)이 낸 주문**인가. 거래 기록의 주문 종류로 판정한다.

    [왜 ODNO만으로는 안 되나] _SYSTEM_ODNOS는 프로세스 메모리라 재기동하면 비어 버린다.
    가상투자 체결 백필(ConclusionMonitor._check_paper_conclusions)처럼 재기동 뒤 당일
    원장을 다시 훑는 경로에서는 자동매매 주문이 전부 '수동 매수'로 오판돼, 시스템이
    자기가 산 종목을 트레이딩 제한에 등록하고 그 뒤로 매수를 스킵한다
    (2026-08-05 관측: 삼성SDS·NAVER).

    engine.send_order가 붙이는 '(AUTO)' 표기는 trades 테이블에 남으므로 재기동에도
    살아남는다. 수동('(수동)')·예약('(예약)') 주문은 사용자가 낸 것이므로 제한 대상이다.
    """
    if "(AUTO)" in str(trade_type or "").upper():
        return True
    return is_system_odno(odno) if odno else False

def _norm_odno(odno):
    """주문번호 정규화(매칭용). 발주 API의 ODNO와 WS 체결통보의 주문번호는 앞자리 0 패딩이
    다를 수 있으므로, 숫자형이면 선행 0을 제거해 동일 주문을 안정적으로 매칭한다.
    (숫자형이 아니면 원문 유지)"""
    s = str(odno or "").strip()
    return (s.lstrip('0') or '0') if s.isdigit() else s

def _current_account_type(cano=None, acnt=None):
    """제한 종목 계좌종류 라벨을 반환한다. (모의/토스/한투-자동/한투-수동)

    한투 실전은 실제 계좌(cano/acnt)를 자동매매 전용 계좌(auto_cano)와 비교해
    구분한다. 외부(앱/HTS) 수동 매수는 메인(수동) 계좌에서 감지되므로 '한투-수동',
    자동매매 전용 계좌면 '한투-자동'으로 라벨링한다. (cano 미지정 시 기존처럼 자동으로 간주)
    """
    if getattr(config.session, 'is_toss', False):
        return "토스"
    if config.session.is_simulation:
        return "모의"
    auto_cano = getattr(config.session, 'auto_cano', None)
    auto_acnt = getattr(config.session, 'auto_acnt_prdt_cd', None)
    # 자동매매 전용 계좌가 설정돼 있고 인자로 받은 계좌가 그와 일치하면 '한투-자동',
    # 그 외(메인/수동 계좌)는 '한투-수동'. 계좌 정보가 없으면 기존 동작(자동) 유지.
    if cano is None:
        return "한투-자동"
    if auto_cano and cano == auto_cano and (not auto_acnt or acnt == auto_acnt):
        return "한투-자동"
    return "한투-수동"

def _get_trade_account():
    """현재 시스템 트레이딩이 실제 매매하는 계좌(cano, acnt)를 반환한다.
    실전은 자동매매 전용 계좌(auto_cano), 모의/토스는 세션 계좌를 사용한다."""
    if config.session.is_simulation or getattr(config.session, 'is_toss', False):
        return config.session.cano, config.session.acnt_prdt_cd
    cano = getattr(config.session, 'auto_cano', None) or config.session.cano
    acnt = getattr(config.session, 'auto_acnt_prdt_cd', None) or config.session.acnt_prdt_cd
    return cano, acnt

_MARKET_TYPE_CACHE = {}

def resolve_market_type(code, cache=None):
    """종목 코드로 시장 구분(KOSPI/KOSDAQ)을 확인한다. (캐싱 적용)

    cache를 넘기면 호출자 전용 캐시를 쓰고(트레이더 인스턴스 등), 생략하면 모듈 캐시를 쓴다.
    """
    if cache is None:
        cache = _MARKET_TYPE_CACHE
    if code in cache:
        return cache[code]

    # 1. stock.json에 사전 정의된 exchange 정보 직접 탐색 (가장 빠르고 정확함)
    for key in ("stocks_kr", "etfs_kr"):
        for item in config.session.stock_data.get(key, []):
            if item['code'] == code and "exchange" in item:
                m_type = item['exchange'].upper()
                if m_type in ("KOSPI", "KOSDAQ"):
                    cache[code] = m_type
                    return m_type

    # 2. API 조회를 통한 Fallback (한글 '코스닥' 포함)
    try:
        res = api.get_current_price_data(code, is_overseas=False)
        if res and res.get('rt_cd') == '0':
            market_name = res['output'].get('rprs_mrkt_kor_name', '')
            if "KOSDAQ" in market_name or "코스닥" in market_name:
                cache[code] = "KOSDAQ"
                return "KOSDAQ"
    except Exception:
        pass

    # 3. API 조회 실패 또는 정보 누락 시 기본값 'KOSPI'로 설정
    cache[code] = "KOSPI"
    return "KOSPI"

def load_restricted_stocks():
    with _RESTRICTED_LOCK:
        return jsonio.load_json(RESTRICTED_FILE, default={}) or {}

def save_restricted_stocks(data):
    with _RESTRICTED_LOCK:
        if not jsonio.save_json(RESTRICTED_FILE, data):
            console.print("[red]제한 종목 저장 실패 (상세는 로그 참조)[/red]")

# [추가] 계좌별 제한 종목 필터링 헬퍼 함수
def get_restricted_stocks(cano=None, acnt=None):
    """지정된 계좌(cano, acnt)에 적용되는 제한 종목 목록만 반환합니다."""
    data = _pkg().load_restricted_stocks()
    if not cano:
        # 계좌 정보가 없으면 세션 정보 시도
        cano = getattr(config.session, 'cano', None)
        if not acnt:
            acnt = getattr(config.session, 'acnt_prdt_cd', "")
    
    account_key = None
    if cano:
        acnt_str = acnt if acnt is not None else ""
        account_key = f"{cano}-{acnt_str}"
        
    filtered_data = {}
    for code, info in data.items():
        global_memo = info.get('memo', '')
        accounts = info.get('accounts', {})
        
        is_restricted = False
        effective_memo = []
        
        if global_memo:
            is_restricted = True
            effective_memo.append(global_memo)
            
        if account_key and account_key in accounts:
            is_restricted = True
            acc_info = accounts[account_key]
            if isinstance(acc_info, str):
                effective_memo.append(acc_info)
            else:
                effective_memo.append(acc_info.get("memo", ""))
            
        if is_restricted:
            info_copy = info.copy()
            info_copy['effective_memo'] = ", ".join(effective_memo)
            filtered_data[code] = info_copy
            
    return filtered_data

# [추가] 제한 종목 등록 헬퍼 함수 (계좌 지정 시 계좌 전용으로 등록)
def add_restricted_stock(code, name, memo, is_overseas=False, cano=None, acnt=None, account_type=None):
    # [수정] load→수정→save 전체를 락으로 감싸 동시 등록/해제 시 lost update 방지
    with _RESTRICTED_LOCK:
        data = _pkg().load_restricted_stocks()

        if code not in data:
            data[code] = {
                "name": name,
                "memo": "",
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "is_overseas": is_overseas,
                "accounts": {}
            }
        else:
            if "accounts" not in data[code]:
                data[code]["accounts"] = {}

        if cano:
            acnt_str = acnt if acnt is not None else ""
            account_key = f"{cano}-{acnt_str}"
            existing = data[code]["accounts"].get(account_key, {})
            if isinstance(existing, str):
                existing = {"memo": existing, "type": "지정계좌"}

            ex_memo = existing.get("memo", "")
            if memo not in ex_memo:
                new_memo = ex_memo + ", " + memo if ex_memo else memo
                data[code]["accounts"][account_key] = {
                    "memo": new_memo,
                    "type": account_type or existing.get("type", "지정계좌"),
                    # [수정] 계좌 스코프별 등록일 저장. 최초 등록 시각을 유지하되,
                    #        기존 종목에 새 계좌가 추가돼도 최상위 date(최초 등록일)와 무관하게
                    #        해당 계좌의 실제 등록 시각이 표시되도록 한다.
                    "date": existing.get("date") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
        else:
            existing_memo = data[code].get("memo", "")
            if memo not in existing_memo:
                data[code]["memo"] = existing_memo + ", " + memo if existing_memo else memo

        _pkg().save_restricted_stocks(data)

# [추가] 제한 종목 삭제 헬퍼 함수 (계좌 지정 시 해당 계좌 사유만 삭제)
def remove_restricted_stock(code, cano=None, acnt=None):
    # [수정] load→수정→save 전체를 락으로 감싸 동시 등록/해제 시 lost update 방지
    with _RESTRICTED_LOCK:
        data = _pkg().load_restricted_stocks()
        if code not in data:
            return

        if cano:
            acnt_str = acnt if acnt is not None else ""
            account_key = f"{cano}-{acnt_str}"
            accounts = data[code].get("accounts", {})
            if account_key in accounts:
                del accounts[account_key]
                data[code]["accounts"] = accounts
        else:
            data[code]["memo"] = ""

        # 글로벌 사유도 없고, 계좌별 사유도 없으면 종목 자체를 삭제
        if not data[code].get("memo") and not data[code].get("accounts"):
            del data[code]

        _pkg().save_restricted_stocks(data)

# [추가] 수동 매수 '발주 시점' 제한 등록의 사후 정리.
#  발주 즉시 제한에 넣어 타이밍 윈도우를 막되, 그 매수가 끝내 체결되지
#  못한 경우(취소/거부/미체결)에는 제한을 자동 해제하여 잔여물을 남기지 않는다.
def schedule_buy_restriction_cleanup(code, cano, acnt, is_overseas=False):
    """비동기로 체결 여부를 추적하여 미체결 매수의 제한을 정리한다.
    - 잔고가 잡히면(체결) 제한을 유지하고 종료.
    - 진행 중 주문이 남아 있으면(지정가 대기 등) 계속 대기.
    - 잔고 0 + 진행 중 주문 없음 → 취소/거부로 보고 제한 해제.
    - 늦게 체결되는 주문은 체결 시점 등록 로직이 다시 제한을 넣으므로 안전."""
    def _get_qty():
        if is_overseas:
            bal = api.get_overseas_balance(cano, acnt)
            if bal is None:
                return None
            for item in bal:
                if item.get('ovrs_pdno') == code:
                    return int(float(item.get('ovrs_cblc_qty', 0) or item.get('ord_psbl_qty', 0)))
            return 0
        bal, _ = api.get_domestic_balance(cano, acnt)
        if bal is None:
            return None
        for item in bal:
            if item.get('pdno') == code:
                return int(item.get('hldg_qty', 0))
        return 0

    def _worker():
        try:
            trader = _pkg().AutoTrader()
            om = getattr(trader, 'order_manager', None)
        except Exception:
            om = None
        for _ in range(40):  # 최대 약 10분 추적 (15초 * 40)
            time.sleep(15)
            try:
                qty = _get_qty()
                if qty is None:
                    continue  # 조회 실패 → 재시도
                if qty > 0:
                    return  # 체결 확인 → 제한 유지
                # 잔고 0: 아직 진행 중인 주문이 있으면 계속 대기
                if om is not None and om.is_pending(code):
                    continue
                # 잔고 0 + 진행 중 주문 없음 → 미체결(취소/거부)로 판단 → 해제
                remove_restricted_stock(code, cano=cano, acnt=acnt)
                logger.info(f"[Restriction] {code} 수동 매수 미체결(취소/거부) 확인 → 계좌({cano}-{acnt}) 제한 해제")
                return
            except Exception as e:
                logger.error(f"수동 매수 제한 정리 검사 중 오류: {e}")
        logger.debug(f"[Restriction] {code} 매수 제한 정리 추적 시간 초과(보수적 유지)")

    threading.Thread(target=_worker, daemon=True).start()

# [추가] 일일 자산 상태 파일 경로 및 관리 함수 (재시작 시 손실 제한 기준 유지용)
DAILY_STATE_FILE = os.path.join(config.JSON_DIR, "daily_asset_state.json")

def load_daily_initial_asset(account_key):
    """계좌별 일일 시작 자산을 로드합니다."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    data = jsonio.load_json(DAILY_STATE_FILE, default={}) or {}
    if data.get("date") == today_str:
        accounts = data.get("accounts", {})
        if account_key in accounts and accounts[account_key] > 0:
            return accounts[account_key]
    return 0

def save_daily_initial_asset(account_key, asset_value):
    """계좌별 일일 시작 자산을 저장합니다."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    data = {"date": today_str, "accounts": {}}
    old_data = jsonio.load_json(DAILY_STATE_FILE, default={}) or {}
    if old_data.get("date") == today_str:
        data["accounts"] = old_data.get("accounts", {})

    data["accounts"][account_key] = asset_value
    jsonio.save_json(DAILY_STATE_FILE, data)

# [리팩토링] 시스템 트레이딩 운영 시간 판단 (단일 진입점)
#  - ConclusionMonitor._is_market_open / AutoTrader.is_market_open 이 동일 로직을
#    각자 들고 있던 중복을 제거하고 이 함수로 위임한다.
def is_system_market_open():
    """국내 정규장/시스템 트레이딩 운영 시간 확인 (config 설정 시간 따름)"""
    if api.is_holiday_today(): return False # 주말 및 공휴일(휴장일) 처리

    current_time = datetime.now().strftime("%H%M")
    start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0900")
    end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "1530")

    if start_time <= current_time <= end_time:
        # 단일가(동시호가) 구간 회피. 08:50~09:00은 NXT 프리마켓이 KRX 시가 단일가에 맞춰 쉬는
        # 시간이고(설정을 0800으로 넓힌 경우에만 해당), 15:20~15:30은 KRX 종가 단일가 구간이라
        # 체결가를 예측할 수 없어 진입/청산 판단을 보류한다.
        # → 기본값(0900~1530)에서 실효 운용 시간은 '즉시 체결이 되는' 09:00~15:20이다.
        #   15:30 정각도 포함한다 — 종료 시각 비교가 <= 라서, 기본값(1530)에서 KRX가 막 마감한
        #   그 1분만 매매가 켜지는 경계가 생긴다.
        # [Fix 2026-07-27] 종가 단일가 시작을 15:25로 잡고 있었다. KRX 장 마감 동시호가는
        #  15:20~15:30(10분)이라 15:20~15:25에 나간 주문은 접수만 되고 체결되지 않는다.
        #  미체결 자동취소(UNFILLED_ORDER_CANCEL_SECONDS, 기본 120초)에 걸려 2분 뒤 취소되는
        #  헛주문이 되므로, 접속매매가 끝나는 15:20을 경계로 맞춘다.
        if "0850" <= current_time < "0900": return False
        if "1520" <= current_time <= "1530": return False
        return True
    return False

# [리팩토링] 단일가(동시호가) 휴게 구간 판단 (단일 진입점)
#  - 상태 안내 문구들이 각자 시각 문자열만 비교하다 보니, 휴장일(주말·공휴일)에도 같은 시각대면
#    "단일가 매매 동기화 대기"로 안내되는 문제가 있었다. 휴장일에는 단일가 구간 자체가 없다.
def is_single_price_break(now=None):
    """지금이 거래일의 단일가(동시호가) 휴게 구간인지 확인 (휴장일에는 항상 False)

    KRX 장 마감 동시호가는 15:20~15:30(10분)이다 — is_system_market_open과 경계를 맞춘다.
    """
    if api.is_holiday_today(): return False

    current_time = (now or datetime.now()).strftime("%H%M")
    return ("0850" <= current_time < "0900") or ("1520" <= current_time < "1530")

# [추가] 주문 상태 상수 정의 (Order State Machine)
class OrderStatus:
    IDLE = "IDLE"
    ORDER_SENT = "ORDER_SENT"       # 주문 전송 (접수 대기)
    ACCEPTED = "ACCEPTED"           # 접수 완료 (미체결)
    PARTIAL_FILLED = "PARTIAL_FILLED" # 부분 체결
    FILLED = "FILLED"               # 전량 체결
    CANCELED = "CANCELED"           # 취소
    REJECTED = "REJECTED"           # 거부/에러

# [추가] DB 스키마 보정 및 가중치 관리 헬퍼 함수
def _active_db_path():
    """지금 열려 있는 DB 파일 경로.

    [중요] config.DB_FILE_PATH를 직접 쓰면 안 된다. 가상투자(mode 4)는
    db_manager.db.switch_path(config.PAPER_DB_FILE_PATH)로 **파일만** 갈아끼우고
    config.DB_FILE_PATH는 실계좌 경로 그대로다. 그래서 아래 함수들이 실계좌 DB를 열어
      · 조회: 가상투자 룰의 가중치를 찾지 못해 JSON 문자열이 dict로 바뀌지 않은 채
        남고, 그 문자열이 calculate_score의 weights.get()에서 AttributeError를 냈다.
        매도 루프가 그 예외를 삼켜, 개별 룰이 걸린 보유 종목이 [보유분석]에서 통째로
        사라졌다(2026-08-05 NAVER).
      · 저장: 가상투자에서 만든 가중치를 실계좌 DB에 썼다(분리 원칙 위반).
    """
    try:
        path = getattr(db_manager.db, 'db_path', None)
        if path:
            return path
    except Exception as e:
        logger.debug(f"활성 DB 경로 확인 실패: {e}")
    return config.DB_FILE_PATH


def _ensure_db_weights_column_logic():
    """stock_strategies 테이블에 weights 컬럼이 없으면 추가"""
    conn = None
    try:
        conn = sqlite3.connect(_active_db_path())
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_strategies'")
        if not cursor.fetchone(): return

        cursor.execute("PRAGMA table_info(stock_strategies)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'weights' not in columns:
            cursor.execute("ALTER TABLE stock_strategies ADD COLUMN weights TEXT")
            conn.commit()
    except Exception as e:
        logger.error(f"DB 스키마 업데이트 실패: {e}")
    finally:
        if conn:
            conn.close()

def _save_rule_weights_logic(code, weights):
    """가중치 정보를 DB에 직접 저장 (JSON 직렬화)"""
    conn = None
    try:
        _ensure_db_weights_column()
        weights_json = json.dumps(weights) if weights else None
        conn = sqlite3.connect(_active_db_path())
        cursor = conn.cursor()
        cursor.execute("UPDATE stock_strategies SET weights = ? WHERE code = ?", (weights_json, code))
        conn.commit()
    except Exception as e:
        logger.error(f"가중치 저장 실패: {e}")
    finally:
        if conn:
            conn.close()

def _enrich_rules_with_weights_logic(rules):
    """DB에서 weights 컬럼을 조회하여 룰 리스트에 병합"""
    if not rules: return rules
    conn = None
    try:
        _ensure_db_weights_column()
        conn = sqlite3.connect(_active_db_path())
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT code, weights FROM stock_strategies")
        rows = cursor.fetchall()
        
        weights_map = {}
        for row in rows:
            if row['weights']:
                try:
                    weights_map[row['code']] = json.loads(row['weights'])
                except Exception: pass
        
        # Row 객체일 수 있으므로 dict로 변환하며 병합
        new_rules = []
        for r in rules:
            r_dict = dict(r)
            if r_dict['code'] in weights_map:
                r_dict['weights'] = weights_map[r_dict['code']]
            elif 'weights' not in r_dict:
                r_dict['weights'] = None
            
            # None 값 초기화
            if r_dict.get('use_atr_stop') is None:
                r_dict['use_atr_stop'] = 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0
            new_rules.append(r_dict)
        return new_rules
    except Exception as e:
        logger.error(f"가중치 로드 실패: {e}")
        return rules
    finally:
        if conn:
            conn.close()

# [수정] 큐 시스템을 통한 실행 래퍼 함수들
def _ensure_db_weights_column():
    # 내부 로직이므로 별도 래핑 없이 호출되는 함수 내에서 처리되거나,
    # 필요 시 execute_custom을 사용. 여기서는 _save/_enrich 내부에서 호출되므로 로직만 분리.
    _ensure_db_weights_column_logic()

def _save_rule_weights(code, weights):
    if hasattr(db_manager.db, 'execute_custom'):
        db_manager.db.execute_custom(_save_rule_weights_logic, code, weights)
    else:
        _save_rule_weights_logic(code, weights)

def _enrich_rules_with_weights(rules):
    if hasattr(db_manager.db, 'execute_custom'):
        return db_manager.db.execute_custom(_enrich_rules_with_weights_logic, rules)
    else:
        return _enrich_rules_with_weights_logic(rules)


