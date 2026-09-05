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
from core import jsonio
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
from core import context # [추가]
import api
from core import utils
from core import indicators
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


def format_holdings_block(valid_holdings, title="보유 종목 현황", name_decorator=None, analysis_results=None, show_auto_status=True):
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
        mfe_str = ""
        atr_str = ""
        ts_str = ""
        if analysis_results and item.get('pdno') in analysis_results:
            res = analysis_results[item['pdno']]
            
            # 상태
            state_val = res.get('state') or "-"
            emoji_key = state_val.split('(')[0]
            if res.get('action') == 'sell':
                state_val = "청산"
                emoji_key = "청산"
                
            state_emoji_map = {"매수": "🔴", "강매수": "🟣", "역매수": "🟤", "상승": "🟠", "대기": "🟠", "관심": "🟢", "관망": "⚪", "주의": "🟡", "매도": "🔵", "청산": "🔵"}
            emoji = state_emoji_map.get(emoji_key, "❓")
            
            score = res.get('score')
            score_str = f" {score:.1f}" if isinstance(score, (int, float)) else ""
            auto_str = ""
            if show_auto_status:
                auto_str = " 수동" if res.get('unmanaged') else " 자동"
            state_str = f"\n   상태: {emoji} {state_val}{score_str}{auto_str}"
            
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
                # 발동선은 breakeven 모드에서 종목마다 다르므로 무장 전후 모두 병기한다.
                act = ts.get('activation') or 0
                dynamic = _pkg().ts_activation_dynamic()
                if not ts.get('armed'):
                    tag = f"손익분기 +{act:.1f}%" if dynamic else f"+{act:.1f}%"
                    if buy_price > 0 and act > 0:
                        arm_price = buy_price * (1 + act / 100)
                        ts_str = f"\n   TS: {round(arm_price):,} 도달 시 ({tag})"
                        # 발동가만으로는 '어디서 잘리나'를 알 수 없다. 고점이 발동가일 때의
                        #  콜백으로 환산해 그때 생길 청산선까지 붙인다.
                        cb = ts.get('callback') or 0
                        highest = res.get('highest_price') or 0
                        if cb > 0 and highest > 0:
                            cb = max(config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0),
                                     cb * highest / arm_price)
                            # 한 줄에 이어 붙이면 모바일에서 임의로 접혀 '→' 앞뒤가 끊긴다.
                            #  줄을 나누고 들여써서 발동가에 딸린 값임을 눈으로 묶는다.
                            ts_str += (f"\n          → 청산선 "
                                       f"{round(arm_price * (1 - cb / 100)):,} (-{cb:.1f}%)")
                    else:
                        ts_str = f"\n   TS: {tag} 도달 시 "
                else:
                    ts_str = f"\n   TS: {round(ts['stop_price']):,} (-{ts['callback']:.1f}%)"
                    if dynamic and act > 0:
                        ts_str += f" [발동 +{act:.1f}%]"

            # 최고가(MFE)
            highest = res.get('highest_price') or 0
            if highest > 0:
                mfe = res.get('max_profit_rate') or 0.0
                mfe_str = f"\n   최고가: {round(highest):,} ({mfe:+.1f}%)"

        # [표기] 한 줄에 두 항목을 '|'로 묶지 않는다. 모바일 폭에서 임의로 접히면
        #  묶인 두 값의 경계가 흐려지고, 아래 상태·최고가·ATR·TS 줄과 들여쓰기가 어긋나
        #  세로로 훑어 읽을 수 없다. 한 줄에 한 항목으로 통일한다.
        msg += (
            f"\n\n• {name_display} ({qty}주)"
            f"\n   현재: {cur_price:,}원"
            f"\n   평단: {buy_price:,.0f}원"
            f"\n   평가: {eval_amt:,}원"
            f"\n   손익: {profit:+,}원 ({rate:+.2f}%)"
            f"{state_str}{mfe_str}{atr_str}{ts_str}"
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
    if getattr(config.session, 'is_toss', False):
        return config.session.cano, config.session.acnt_prdt_cd
    cano = getattr(config.session, 'auto_cano', None) or config.session.cano
    acnt = getattr(config.session, 'auto_acnt_prdt_cd', None) or config.session.acnt_prdt_cd
    return cano, acnt


def trade_account_key():
    """매매 계좌를 DB `trades.account` 와 같은 'cano-acnt' 문자열로 만든다.

    [왜 함수인가] 이 문자열은 매수 기록을 계좌로 가르는 필터 키다(db_manager._account_clause).
     호출부마다 f-string을 짜면 한쪽만 다른 규칙을 쓰게 되고, 그러면 **남의 계좌 매수 기록**
     으로 손절선·진입일이 계산된다 — 화면과 실제 판정이 조용히 갈리는 자리다.
    """
    cano, acnt = _get_trade_account()
    return f"{cano or ''}-{acnt or ''}"

#  경보 전달 확인 헬퍼는 modules/telegram_notify 가 정본이다 — market_halt(서킷브레이커·VI)도
#  같은 규칙이 필요한데, 그쪽이 auto_trade 패키지를 끌어오게 둘 수는 없다. 여기서는 이름만
#  들여온다(기존 `patch('modules.auto_trade.alert_delivered')` 는 그대로 동작한다).
from modules.telegram_notify import ALERT_RETRY_SEC, alert_delivered      # noqa: E402,F401


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

    # 2. 판정 정본(analysis.get_market_type: KIS 마스터 → KRX 상장목록)
    #    종전에는 여기서 현재가 응답의 rprs_mrkt_kor_name 을 봤는데, 그 필드는 토스 모드
    #    응답에 없어 토스에서는 이 단계가 통째로 무동작이었다.
    try:
        resolved = analysis.get_market_type(code)
    except Exception:       # noqa: BLE001 - 판정 실패는 아래 폴백으로 흡수한다
        resolved = None
    if resolved:
        cache[code] = resolved
        return resolved

    # 3. 판정 불가 — 이번 호출에만 'KOSPI'로 진행하고 **캐시에는 넣지 않는다**.
    #    넣으면 일시적 조회 실패 하나가 프로세스가 살아 있는 내내 그 종목을 코스피로
    #    굳힌다(시장 필터·적응형 임계값이 이 값으로 볼 지수를 고른다).
    logger.debug(f"[시장구분] 판정 불가({code}) — 이번 주기만 KOSPI로 진행(캐시 보류)")
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

def update_restricted_stock(code, memo, old_cano=None, old_acnt=None,
                            new_cano=None, new_acnt=None, account_type=None):
    """제한 한 건의 사유·적용 범위를 **덮어쓴다**. 성공하면 True.

    [왜 add 로는 안 되나] add_restricted_stock 은 같은 종목을 다시 등록하면 메모를 ', ' 로
    이어붙인다 — 여러 번 걸린 이유를 잃지 않으려는 설계다. 그래서 그 함수로는 오타 하나
    고칠 수 없었고, 유일한 수단이 '해제 후 재등록'이었다(등록일이 오늘로 밀려, 언제부터
    막아 둔 종목인지가 사라진다). 변경은 이어붙이지 않고 그 자리를 대체한다.

    범위(글로벌 ↔ 지정계좌)를 옮길 때도 **최초 등록일을 지킨다** — 옮긴 것은 적용 범위이지
    제한을 새로 건 것이 아니기 때문이다.
    """
    with _RESTRICTED_LOCK:
        data = _pkg().load_restricted_stocks()
        if code not in data:
            return False
        entry = data[code]
        accounts = entry.setdefault("accounts", {})

        def _key(cano, acnt):
            return f"{cano}-{acnt if acnt is not None else ''}"

        # 최초 등록일 — 옮기기 전 자리에서 읽는다(없으면 종목 등록일).
        if old_cano:
            old = accounts.get(_key(old_cano, old_acnt))
            kept_date = (old.get("date") if isinstance(old, dict) else None) or entry.get("date")
            old_type = old.get("type") if isinstance(old, dict) else None
        else:
            kept_date, old_type = entry.get("date"), None
        kept_date = kept_date or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 옮기기 전 자리를 비운다(같은 자리면 아래에서 그대로 덮인다).
        if old_cano:
            accounts.pop(_key(old_cano, old_acnt), None)
        else:
            entry["memo"] = ""

        if new_cano:
            accounts[_key(new_cano, new_acnt)] = {
                "memo": memo,
                "type": account_type or old_type or "지정계좌",
                "date": kept_date,
            }
        else:
            entry["memo"] = memo
            entry["date"] = entry.get("date") or kept_date

        entry["accounts"] = accounts
        # 사유가 통째로 비면(빈 메모로 바꾼 경우) 해제와 같은 뜻이다.
        if not entry.get("memo") and not accounts:
            del data[code]
        _pkg().save_restricted_stocks(data)
        return True


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

def current_holding_qty(code, cano, acnt, is_overseas=False):
    """계좌의 해당 종목 보유수량. **조회 실패는 None('모름')** — 0(없음)과 구분한다.

    [계좌 컨텍스트 · 2026-09-05] cano 를 TR 파라미터로 넘기는 것만으로는 부족하다.
     **어느 앱키·토큰으로 나가는가**는 threading.local(use_auto_account)이 정하는데
     (core.utils.get_common_headers · api.auth.get_current_token · api.http._real_bucket_key),
     이 함수는 제한 정리 추적처럼 **새로 띄운 데몬 스레드**에서 불린다 — 그 스레드에서
     플래그는 미설정(=수동)이라 자동 계좌 잔고를 수동 앱키로 묻게 된다. 계좌가 갈린
     실전(mode 2)에서 그 조회는 실패하고, 실패는 여기서 None 이 되어 호출부가
     '모름 → 제한 유지'로 굳는다. 그러면 그 종목의 손절·트레일링이 영영 멈춘다.
     cano 를 아는 곳에서 컨텍스트도 함께 세운다.
    """
    def _qty(*values):
        """수량 필드를 읽는다. **못 읽으면 None('모름')** — 0(없음)으로 넘기지 않는다.

        [Fix 2026-09-05] 종전에는 `int(item.get('hldg_qty', 0))` 이었다. dict.get 의
         기본값은 키가 **없을 때만** 쓰이는데, 증권사 응답은 값이 없을 때 키를 주고
         **빈 문자열**을 담는다 → int('') 가 ValueError 를 내고 이 함수 밖으로 나갔다.
         호출부(제한 정리 추적)는 그 예외를 '조회 실패'로 세어 5회 재시도 뒤
         '제한 해제 보류'로 끝낸다 = 그 종목의 손절·트레일링이 영영 멈춘다.
         읽을 수 없는 수량을 0(=전량 매도)으로 단정하지도 않는다 — 그러면 반대로
         아직 들고 있는 수동 포지션의 제한이 풀린다. 모르는 것은 모른다고 답한다.
        """
        for v in values:
            if v is None:
                continue
            s = str(v).strip().replace(',', '')
            if not s:
                continue
            try:
                return int(float(s))
            except (TypeError, ValueError):
                logger.warning(f"[Restriction] {code} 보유수량 필드를 읽을 수 없습니다: {v!r}")
                return None
        return None

    with utils.AccountContext(cano):
        if is_overseas:
            bal = api.get_overseas_balance(cano, acnt)
            if bal is None:
                return None
            for item in bal:
                if item.get('ovrs_pdno') == code:
                    #  잔고에 실려 있는데 수량 칸이 비었다 = 응답이 이상하다 → '모름'.
                    return _qty(item.get('ovrs_cblc_qty'), item.get('ord_psbl_qty'))
            return 0
        bal, _ = api.get_domestic_balance(cano, acnt)
        if bal is None:
            return None
        for item in bal:
            if item.get('pdno') == code:
                return _qty(item.get('hldg_qty'))
        return 0


# [추가] 수동 매수 '발주 시점' 제한 등록의 사후 정리.
#  발주 즉시 제한에 넣어 타이밍 윈도우를 막되, 그 매수가 끝내 체결되지
#  못한 경우(취소/거부/미체결)에는 제한을 자동 해제하여 잔여물을 남기지 않는다.
def schedule_buy_restriction_cleanup(code, cano, acnt, is_overseas=False,
                                     pre_qty=None, odno=None):
    """비동기로 체결 여부를 추적하여 미체결 매수의 제한을 정리한다.
    - 체결이 확인되면 제한을 유지하고 종료.
    - 진행 중 주문이 남아 있으면(지정가 대기 등) 계속 대기.
    - 체결 흔적 없음 + 진행 중 주문 없음 → 취소/거부로 보고 제한 해제.
    - 늦게 체결되는 주문은 체결 시점 등록 로직이 다시 제한을 넣으므로 안전.

    [왜 pre_qty·odno 가 필요한가] 종전 판정은 '잔고 > 0 = 체결'이었다. **이미 들고 있던
     종목**을 수동으로 추가 매수하면 그 주문이 취소돼도 기존 보유분 때문에 잔고가 0이
     아니라, 제한이 영원히 남는다 = 시스템이 자기 포지션의 손절·트레일링을 멈춘다.
     그래서 ① 그 주문번호의 체결 기록 ② 발주 전 대비 **늘어난** 잔고, 두 가지로 판정한다.
     pre_qty 를 넘기지 않으면 종전과 같이 '잔고 > 0'으로 동작한다(하위 호환).
     pre_qty=None(조회 실패)도 같은 자리로 떨어진다 — 모르면 보수적으로 유지한다.
    """
    base_qty = int(pre_qty) if isinstance(pre_qty, int) and pre_qty > 0 else 0

    def _worker():
        try:
            trader = _pkg().AutoTrader()
            om = getattr(trader, 'order_manager', None)
        except Exception:
            om = None
        for _ in range(40):  # 최대 약 10분 추적 (15초 * 40)
            time.sleep(15)
            try:
                # 체결 기록이 있으면 확정이다(잔고 조회보다 정확하고 싸다).
                if odno:
                    try:
                        #  방금 낸 주문을 쫓는 중이다 — 오늘 것만 본다(odno 는 당일 채번).
                        if db_manager.db.check_trade_exists(
                                odno, "체결", on_date=datetime.now().strftime('%Y-%m-%d')):
                            return
                    except Exception:
                        pass
                qty = _pkg().current_holding_qty(code, cano, acnt, is_overseas)
                if qty is None:
                    continue  # 조회 실패 → 재시도
                if qty > base_qty:
                    return  # 잔고가 늘었다 = 체결 → 제한 유지
                # 늘지 않았다: 아직 진행 중인 주문이 있으면 계속 대기
                if om is not None and om.is_pending(code):
                    continue
                # 체결 흔적 없음 + 진행 중 주문 없음 → 미체결(취소/거부)로 판단 → 해제
                remove_restricted_stock(code, cano=cano, acnt=acnt)
                logger.info(f"[Restriction] {code} 수동 매수 미체결(취소/거부) 확인 → 계좌({cano}-{acnt}) 제한 해제")
                return
            except Exception as e:
                logger.error(f"수동 매수 제한 정리 검사 중 오류: {e}")
        logger.debug(f"[Restriction] {code} 매수 제한 정리 추적 시간 초과(보수적 유지)")

    threading.Thread(target=_worker, daemon=True).start()

# [추가] 일일 자산 상태 파일 경로 및 관리 함수 (재시작 시 손실 제한 기준 유지용)
DAILY_STATE_FILE = os.path.join(config.JSON_DIR, "daily_asset_state.json")

def _daily_state_entry(account_key):
    """오늘 자의 계좌 항목을 dict로 정규화해 돌려준다(없으면 빈 dict).

    옛 형식은 값이 숫자 하나(시작 자산)였다. 형식을 바꾸면서 옛 파일을 그대로 읽어야
    한다 — 기동 중 형식이 바뀌었다고 그날 기준선을 잃으면 차단기가 통째로 꺼진다.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    data = jsonio.load_json(DAILY_STATE_FILE, default={}) or {}
    if data.get("date") != today_str:
        return {}
    entry = (data.get("accounts") or {}).get(account_key)
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, (int, float)):
        return {"asset": entry}
    return {}


def load_daily_initial_asset(account_key):
    """계좌별 일일 시작 자산을 로드합니다."""
    asset = _daily_state_entry(account_key).get("asset") or 0
    return asset if asset > 0 else 0


def load_daily_principal(account_key):
    """계좌별 기준 원금(현금+매입원가-실현손익)을 로드합니다. 없으면 0.

    [왜 저장하는가] 이 값은 가격 변동·매매와 무관하게 **입출금이 없으면 불변**이라
    외부 입출금을 가려내는 유일한 불변량이다. 종전에는 메모리에만 있어서, 프로그램이
    꺼진 사이에 입출금이 있고 같은 날 다시 켜면 기준 원금이 **입출금 이후 상태로**
    새로 잡혔다 — 그러면 차이가 0이라 그 입출금은 영영 감지되지 않는다.
    반면 시작 자산(initial_asset)은 파일에서 옛 값 그대로 복원되므로, 출금이면 분모가
    높게 남아 차단기가 헛발동하고 사이징 기준도 부푼 채로 하루를 보낸다.
    """
    principal = _daily_state_entry(account_key).get("principal") or 0
    return principal if principal > 0 else 0

# 직전 영업일 대비 이 비율 아래면 '시세 결손으로 예수금만 잡힌 응답'을 의심한다.
#  engine.check_loss_limit 이 current_total 에 이미 쓰고 있는 것과 같은 기준(0.5)이다.
BASELINE_SANITY_RATIO = 0.5

# 위쪽 '경고' 배수. 넘어도 **거부하지 않고 로그만 남긴다.**
#  [실측 2026-08-23] 가상투자 자산 이력에 10,028,670 → 20,028,670 행이 남았다. 차이가
#  정확히 시드(1,000만)라 자산에 시드가 한 번 더 더해진 것이다. 그 행 하나로 자산 고점이
#  두 배가 되고 드로다운이 -49.98%가 된다(실데이터 재현). db_manager.get_max_daily_asset
#  의 고립 이상치 제거가 잡아 -1.05%로 끝났고, 다음 일요일에는 재현되지 않았다.
#
#  [왜 거부하지 않는가] 거부하면 **정당한 입금 다음 날 기준선이 옛 값으로 굳는다.**
#   1,000만 계좌에 4,000만을 입금하면 오늘 기준선은 5,000만이 맞는데, 막으면 분모가
#   1,000만으로 남아 손실률이 늘 큰 양수가 된다 = 차단기가 종일 발동하지 않는다. 이 함수가
#   막으려던 바로 그 실패 모드다(test_growth_is_never_suspicious 가 이 결정을 고정한다).
#   드문 중복 계상을 막자고 입금일마다 보호 장치를 끄는 것은 남는 장사가 아니다.
#   드로다운 쪽 방어는 get_max_daily_asset 이 이미 맡고 있으므로, 여기서는 **보이게만** 한다.
#  [값] 1.5 — db_manager.HWM_OUTLIER_RATIO 와 같다. 같은 판단('이 자산 값이 이력에서
#   튀는가')을 두 곳이 다른 배수로 하면 한쪽만 짖는다. 실측 사고는 1.997배였다 —
#   2.0 으로 뒀다면 정확히 그 행을 놓쳤을 것이다.
BASELINE_SANITY_MAX_RATIO = 1.5


def is_plausible_baseline(account_key, tot_asset, last_known=None):
    """오늘의 시작 자산으로 받아들여도 되는 값인가.

    [왜 필요한가] 이 값은 계좌 차단기(일일 손실 한도)의 분모이면서 동시에 포지션 사이징의
    기준 자산이다. 그런데 저장 조건이 `tot_asset > 0` 뿐이라 어떤 양수든 그날의 기준이 되고,
    한 번 저장되면 load 가 그대로 돌려주므로 **하루 종일 고정된다**.

    코드는 이 실패 모드를 이미 알고 있다 — engine.check_loss_limit 의 주석이
    "증권사 API 통신 오류로 주식 평가액이 0으로 수신되어 예수금만 계산될 때"라고 적어 두고
    current_total 을 그 기준으로 거른다. 정작 더 위험한 쪽(기준선)에는 가드가 없었다.
    기준선이 실제보다 작게 박히면 손실률이 늘 큰 양수로 계산돼 **차단기가 종일 발동하지
    않는다**. 아무도 모르는 채로 보호 장치만 사라지는 것이 가장 나쁜 상태다.

    last_known 이 없으면(첫 운용·이력 없음) 판단 근거가 없으므로 통과시킨다.

    [양쪽을 본다] 종전에는 하한만 봤다. 그런데 기준선이 실제보다 **크게** 박히는 것도
     같은 종류의 사고다 — 자산 고점이 부풀어 가짜 드로다운이 생기고, 리스크 스케일링이
     그 값으로 조여진다. 2026-08-23 실측이 정확히 그것이었다(위 상수 주석).
    """
    if tot_asset <= 0:
        return False
    if last_known is None:
        try:
            from modules import db_manager
            last_known = db_manager.db.get_last_daily_asset(
                account_key, datetime.now().strftime("%Y-%m-%d"))
        except Exception:
            last_known = None
    if not last_known or last_known <= 0:
        return True
    if tot_asset > last_known * BASELINE_SANITY_MAX_RATIO:
        # 거부하지 않는다 — 아래 상수 주석 참조. 보이게만 한다.
        logger.warning(
            f"[기준선] 자산이 직전 대비 {tot_asset / last_known:.1f}배다 "
            f"({last_known:,.0f} → {tot_asset:,.0f}). 입금이라면 정상이고, 아니라면 "
            f"중복 계상이다 — daily_asset_history 를 확인하십시오.")
    return tot_asset >= last_known * BASELINE_SANITY_RATIO


def save_daily_initial_asset(account_key, asset_value, principal=None):
    """계좌별 일일 시작 자산(과 기준 원금)을 저장합니다. **성공 여부를 돌려준다.**

    principal 을 주지 않으면 이미 저장된 값을 그대로 둔다 — 시작 자산만 고치는 호출이
    기준 원금을 지워 버리면, 재기동 때 오프라인 입출금 감지가 다시 꺼진다.

    [왜 반환값이 필요한가 · 2026-09-05] jsonio.save_json 은 실패를 bool 로 알리는데
     여기서 그것을 버렸다. 그러면 그 세션 동안은 메모리 값으로 정상 동작해 아무도
     모르고, **재기동해야 소실이 드러난다**. 이 파일은 일일 손실 한도(비상 정지)의
     분모이자 드로다운 리스크 스케일링의 기준선이다 — 잃으면 그날의 낙폭이 조용히
     사라지고 차단기가 리셋된다(paper_broker._clear_daily_baseline 주석이 같은 사고를
     '파일을 지우면'으로 적어 뒀다. 저장 실패는 지운 것과 같은 결과다).
     운영기는 램 1GB·SD 카드 라즈베리파이라 디스크 가득참·IO 오류가 실재한다.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    data = {"date": today_str, "accounts": {}}
    old_data = jsonio.load_json(DAILY_STATE_FILE, default={}) or {}
    if old_data.get("date") == today_str:
        data["accounts"] = old_data.get("accounts", {})

    prev = data["accounts"].get(account_key)
    entry = dict(prev) if isinstance(prev, dict) else {}
    entry["asset"] = asset_value
    if principal is not None:
        entry["principal"] = principal
    data["accounts"][account_key] = entry
    ok = jsonio.save_json(DAILY_STATE_FILE, data)
    if not ok:
        logger.error(
            f"[기준선] 일일 시작 자산 저장 실패({account_key} = {asset_value:,.0f}원) — "
            f"이 세션은 메모리 값으로 계속하지만, 재기동하면 오늘의 기준선을 잃습니다"
            f"(일일 손실 한도·드로다운 기준이 현재 자산으로 다시 잡힙니다).")
    return ok

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

KRX_REGULAR_OPEN_TIME = "0900"   # KRX 정규장 개장 (프리마켓 설정과 무관한 고정 기준)


# [진입 게이트] 개장 직후 신규 진입 보류 (청산에는 관여하지 않는다)
def entry_open_delay_remaining(now=None):
    """개장 직후 보류가 걸려 있으면 남은 초, 아니면 0을 돌려준다.

    **신규 진입에만 쓴다.** 손절·트레일링은 이 함수를 보지 않는다 — 거래 시간
    설정(SYSTEM_TRADING_START_TIME)을 늦춰 같은 효과를 내려 하면 매수와 청산이 함께
    멈춰 개장 직후 갭 구간이 무방비가 된다(is_system_market_open 참조).

    기준 시각은 KRX 정규장 개장(0900)과 거래 시작 시간 중 **늦은 쪽**이다. 프리마켓까지
    운용하려고 START_TIME을 0800으로 넓힌 경우에도 막아야 하는 구간은 08:00~08:30이
    아니라 09:00~09:30이다(tools/audit_time_of_day.py가 잰 것이 그것이다).
    """
    if not getattr(config, 'SYSTEM_ENTRY_OPEN_DELAY_USE', True):
        return 0
    try:
        minutes = int(getattr(config, 'SYSTEM_ENTRY_OPEN_DELAY_MINUTES', 30) or 0)
    except (TypeError, ValueError):
        minutes = 30
    if minutes <= 0:
        return 0

    now = now or datetime.now()
    start = str(getattr(config, 'SYSTEM_TRADING_START_TIME', "0900") or "0900")
    base = max(start, KRX_REGULAR_OPEN_TIME)
    try:
        open_at = now.replace(hour=int(base[:2]), minute=int(base[2:4]),
                              second=0, microsecond=0)
    except (ValueError, IndexError):
        return 0

    # 개장 전(예: 프리마켓 구간)에는 보류를 걸지 않는다 — is_system_market_open이
    #  이미 판단한 '거래 가능 시간'에 대해서만 진입 시점을 미루는 게이트다.
    if now < open_at:
        return 0
    remain = (open_at + timedelta(minutes=minutes) - now).total_seconds()
    return int(remain) if remain > 0 else 0


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

def effective_time_stop_days(code=None, rule=None):
    """이 종목에 **실제로 적용되는** 시간청산 일수. 개별 룰이 있으면 그 값이 이긴다.

    [왜 필요한가 · 2026-09-04] 청산 판정(engine.analyze_sell)은
    `thresholds["TIME_STOP_DAYS"] = _rv('time_stop_days', 전역)` 으로 개별 룰을 존중하는데,
    화면들은 전역 config 만 읽었다 — 룰로 기한을 바꾼 종목에서 잔고의 'D-n'과 보유일
    경고가 실제 청산 시점과 어긋났다. 표시선은 판정과 같은 값을 읽어야 한다.

    rule 을 이미 들고 있으면 넘긴다(표를 그리며 종목마다 DB 를 다시 뒤지지 않도록).
    """
    default = config.SELL_STRATEGY["TIME_STOP_DAYS"]
    if rule is None and code:
        try:
            from modules import db_manager
            rule = db_manager.db.get_stock_strategy(code)
        except Exception as e:      # noqa: BLE001 - 조회 실패는 전역값으로 (판정도 그렇게 폴백한다)
            logger.debug(f"[TimeStop] 개별 룰 조회 실패({code}): {e}")
            rule = None
    if not rule:
        return default
    val = rule.get('time_stop_days')
    return default if val in (None, "") else int(val)


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

    [중요] config.DB_FILE_PATH를 직접 쓰면 안 된다. 가상투자(mode 1)는
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


