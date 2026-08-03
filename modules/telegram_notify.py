# modules/telegram_notify.py
"""텔레그램 발신(전송) 계층.

api.py에서 분리된 send_telegram_message / send_telegram_photo 구현.
기존 호출부와의 호환을 위해 api.py가 동일 이름으로 재수출(re-export)하므로,
호출·테스트 patch 는 계속 api.send_telegram_message 방식으로 동작한다.
(수신/명령 처리는 modules/telegram_bot.py 담당)
"""
import json
import logging
import os
import re
import threading
import time

import requests

import config
import context
from modules.executors import tg_sender_executor

logger = logging.getLogger(__name__)


def _get_telegram_footer():
    """텔레그램 메시지용 계좌 정보 꼬리말 생성"""
    if not config.TELEGRAM_BOT_TOKEN:
        return

    cano = config.session.cano
    acnt = config.session.acnt_prdt_cd
    
    # 가상투자는 토스 시세를 쓰지만 계좌가 가상이므로 알림에서도 실전과 구분해야 한다
    if getattr(config.session, 'is_paper', False):
        acc_label = "가상"
    elif config.session.is_toss:
        acc_label = "토스"
    else:
        acc_label = "모의" if config.session.is_simulation else "실전"

    # 시스템 트레이딩 컨텍스트(AUTO 계좌) 확인
    if not config.session.is_simulation and not config.session.is_toss and getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt = config.session.auto_acnt_prdt_cd
        acc_label = "자동"

    # 계좌번호 뒤에 상품코드(01, 22 등)를 붙여 사용자가 정확한 계좌를 식별할 수 있도록 함
    full_acc = f"{cano}-{acnt}" if acnt else cano
    instance_name = config.TELEGRAM_INSTANCE_NAME
    
    return f"[{instance_name} | {acc_label} {full_acc}]"

def send_telegram_message(message, reply_markup=None, is_urgent=False, sync=False):
    """텔레그램 메시지 전송 (시스템 트레이딩 알림용)"""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    account_info = _get_telegram_footer()
    
    # [추가] 마크다운 링크 패턴([text](url))을 임시 토큰으로 변환 (Rich 태그 제거 및 이스케이프 영향 방지)
    link_map = {}
    def _stash_link(match):
        token = f"__LINK_{len(link_map)}__"
        link_map[token] = (match.group(1), match.group(2)) # text, url
        return token
    
    clean_message = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _stash_link, message)

    # [추가] rich 라이브러리 색상 태그 제거 (텔레그램 전송용)
    # 예: [red]텍스트[/] -> 텍스트. 소문자로 시작하는 태그만 제거하여 [시스템] 등은 유지
    clean_message = re.sub(r'\[/?[a-z]+(?:[\s=][^\]]*)?\]', '', clean_message)

    # [추가] HTML 이스케이프 처리 (HTML 파싱 모드 사용 시 필수)
    clean_message = clean_message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # [추가] AI가 무작위로 생성하는 마크다운 굵게(**) 기호 일괄 제거
    clean_message = clean_message.replace("**", "")

    # [추가] 마크다운 헤더(#) 및 수평선(---) 기호 일괄 제거
    clean_message = re.sub(r'^#{1,6}\s*', '', clean_message, flags=re.MULTILINE)
    clean_message = re.sub(r'^[-*_]{3,}\s*$', '', clean_message, flags=re.MULTILINE)

    # [추가] 마크다운 링크 복원 (HTML <a> 태그로 변환)
    for token, (text, url) in link_map.items():
        # 텍스트 부분도 이스케이프 처리
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        clean_message = clean_message.replace(token, f'<a href="{url}">{safe_text}</a>')

    # [수정] 종목 코드에 링크 자동 적용 (네이버 증권)
    # 패턴: 괄호 안의 6자리 숫자/영문(국내) 또는 영문 대문자(해외) -> 예: (005930), (0080G0), (AAPL)
    def add_stock_link(match):
        code = match.group(1)
        
        # [추가] 일반 영문 단어나 보조지표명 등이 해외 티커로 오인되어 링크되는 현상 방지
        exclude_words = {"ON", "OFF", "RSI", "MACD", "ATR", "SMA", "EMA", "CCI", "ADX", "SAR", "OBV", "ETF", "TS", "RUN", "STOP", "WAIT"}
        if code in exclude_words:
            return f"({code})"

        # 1. 국내 주식 (6자리)
        if len(code) == 6:
            # [수정] 국내 주식: 트레이딩뷰 심볼 오버뷰 페이지 (유료/앱 설치 팝업 우회)
            url = f"https://kr.tradingview.com/symbols/KRX-{code}/"
        # 2. 해외 주식
        else:
            # 거래소 정보 확인 (config.session.exchange_cache 활용)
            exchange = config.session.exchange_cache.get(code, "")
            tv_exchange = ""
            
            # 트레이딩뷰 해외주식 거래소 접미사 매핑
            if exchange in ["NAS", "NASD"]: tv_exchange = "NASDAQ"
            elif exchange in ["NYS", "NYSE"]: tv_exchange = "NYSE"
            elif exchange in ["AMS", "AMEX"]: tv_exchange = "AMEX"
            
            if tv_exchange:
                url = f"https://kr.tradingview.com/symbols/{tv_exchange}-{code}/"
            else:
                # 거래소 정보가 없으면 티커만으로 접근 (트레이딩뷰가 자동 라우팅)
                url = f"https://kr.tradingview.com/symbols/{code}/"
                
        return f'(<a href="{url}">{code}</a>)'
    
    clean_message = re.sub(r'\(([0-9A-Z]{6}|[A-Z]{1,5})\)', add_stock_link, clean_message)

    # [수정] 계좌 정보를 메시지 가장 마지막에 추가 (가독성을 위해 한 줄 공백 추가)
    final_msg = f"{clean_message.rstrip()}\n\n{account_info}"

    # [수정] 전송 메시지 로그 기록 (시스템 로그로 변경)
    log_content = final_msg.replace('\n', ' | ')
    logger.info(f"[Telegram] 메시지 발송: {log_content}")

    # [추가] 4000자 분할 로직 (긴 메시지 자동 분할 전송)
    MAX_LEN = 4000
    msg_chunks = []
    if len(final_msg) <= MAX_LEN:
        msg_chunks.append(final_msg)
    else:
        lines = final_msg.split('\n')
        current_chunk = ""
        for line in lines:
            if len(current_chunk) + len(line) + 1 > MAX_LEN:
                if current_chunk:
                    msg_chunks.append(current_chunk.strip())
                    current_chunk = line + "\n"
                else:
                    msg_chunks.append(line[:MAX_LEN])
                    current_chunk = line[MAX_LEN:] + "\n"
            else:
                current_chunk += line + "\n"
        if current_chunk:
            msg_chunks.append(current_chunk.strip())

    def _send_task():
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        
        # [추가] 화면 디버그 로그 (요청)
        if context.is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
            config.console.print(f"[dim cyan][TRACE] REQ (TELEGRAM) | POST {url}[/dim cyan]")
            if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                config.console.print(f"[dim cyan]  > Message: {message.replace(chr(10), ' ')}[/dim cyan]")

        # [수정] 재시도 로직 추가 (최대 3회)
        max_retries = 3
        for i, chunk in enumerate(msg_chunks):
            data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
            
            if reply_markup and i == len(msg_chunks) - 1:
                data["reply_markup"] = json.dumps(reply_markup)

            success_chunk = False
            for attempt in range(max_retries):
                try:
                    current_timeout = 1 + (attempt * 0.5)
                    res = requests.post(url, data=data, timeout=current_timeout)
                    
                    if context.is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim magenta][TRACE] RES (TELEGRAM) Status:{res.status_code} Chunk {i+1}/{len(msg_chunks)} ({attempt+1}/{max_retries})[/dim magenta]")
                        if config.SCREEN_DEBUG_LEVEL == "DEBUG" and res.status_code != 200:
                             config.console.print(f"[dim red]  > Error: {res.text}[/dim red]")
                    
                    if res.status_code == 200:
                        logger.info(f"[Telegram] 전송 성공 (Chunk {i+1}/{len(msg_chunks)})")
                        success_chunk = True
                        break
                    else:
                        logger.error(f"[Telegram] 전송 실패 (Chunk {i+1}/{len(msg_chunks)}, {attempt+1}/{max_retries}) Status: {res.status_code}, Msg: {res.text}")
                except Exception as e:
                    # [추가] 네트워크 오류 등 긴 에러 메시지 축약
                    error_msg = str(e)
                    if "Network is unreachable" in error_msg:
                        error_msg = "네트워크 통신 불가 (Network is unreachable)"
                    elif "Max retries exceeded" in error_msg:
                        error_msg = "서버 접속 지연 (Connection Timeout)"

                    if context.is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim red][TRACE] ERR (TELEGRAM) {error_msg} ({attempt+1}/{max_retries})[/dim red]")
                    logger.error(f"[Telegram] 전송 중 오류 발생 (Chunk {i+1}/{len(msg_chunks)}, {attempt+1}/{max_retries}): {error_msg}")
                
                if attempt < max_retries - 1:
                    # [수정] 네트워크 단절 시 복구될 시간을 벌기 위해 점진적 대기 (1초 -> 2초 -> 4초)
                    time.sleep(2 ** attempt)
                    
            if not success_chunk:
                logger.error(f"[Telegram] 최종 전송 실패 (Chunk {i+1}/{len(msg_chunks)})")

    # [수정] 긴급 발송 여부에 따라 큐(Queue) 대기열 우회 처리
    if sync:
        _send_task()
    elif is_urgent:
        threading.Thread(target=_send_task, daemon=True, name="TgUrgentSender").start()
    else:
        # 핵심 매매 로직 블로킹 방지를 위해 스레드 풀로 위임 (비동기 전송)
        tg_sender_executor.submit(_send_task)


def send_telegram_photo(file_path, caption=None):
    """텔레그램 사진 전송"""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    if not os.path.exists(file_path):
        logger.error(f"[Telegram] 전송할 파일이 없습니다: {file_path}")
        return False

    account_info = _get_telegram_footer()
    final_caption = f"{caption}\n\n{account_info}" if caption else account_info

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
    chat_id = config.TELEGRAM_CHAT_ID
    
    logger.info(f"[Telegram] 사진 전송 시작: {os.path.basename(file_path)}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            with open(file_path, 'rb') as f:
                data = {"chat_id": chat_id, "caption": final_caption}
                files = {"photo": (os.path.basename(file_path), f, 'image/png')}
                
                # 이미지 전송은 시간이 더 걸릴 수 있으므로 타임아웃을 넉넉하게 설정
                res = requests.post(url, data=data, files=files, timeout=30)
            
            if res.status_code == 200:
                logger.info("[Telegram] 사진 전송 성공")
                return True
            else:
                logger.error(f"[Telegram] 사진 전송 실패({attempt+1}/{max_retries}) Status: {res.status_code}, Msg: {res.text}")
                
        except Exception as e:
            # [추가] 네트워크 오류 등 긴 에러 메시지 축약
            error_msg = str(e)
            if "Network is unreachable" in error_msg:
                error_msg = "네트워크 통신 불가 (Network is unreachable)"
            elif "Max retries exceeded" in error_msg:
                error_msg = "서버 접속 지연 (Connection Timeout)"

            if context.is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] ERR (TELEGRAM PHOTO) {error_msg}[/dim red]")
            logger.error(f"[Telegram] 사진 전송 중 오류({attempt+1}/{max_retries}): {error_msg}")
        
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
            
    return False
