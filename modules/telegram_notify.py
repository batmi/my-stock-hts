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
from core import context
from core.executors import tg_sender_executor

logger = logging.getLogger(__name__)

# [안전장치] 발신 상태. 전송은 비동기(스레드 풀)라 호출부가 성공 여부를 알 수 없다.
#  그래서 종전에는 손절 경보가 안 갔는데도 간 줄 알고 넘어갔다 — 텔레그램이 죽었다는
#  사실을 텔레그램으로 알릴 수는 없으므로, 실패를 여기 모아 화면·로그·상태창에 드러낸다.
_delivery = {
    'sent': 0, 'failed': 0, 'consecutive_failed': 0,
    'last_error': '', 'last_failure_at': None, 'last_success_at': None,
    'lost': [],          # 끝내 못 보낸 메시지 요약(최근 것부터, 상한 아래 참조)
}
_delivery_lock = threading.Lock()
#  연속 실패가 이 수를 넘으면 화면에 크게 띄운다. 한두 번은 네트워크 순간 단절이지만
#  연속 실패는 '알림 경로가 죽었다'는 뜻이고, 운영자가 이걸 모르면 경보를 못 받는다.
DELIVERY_ALERT_THRESHOLD = 3
LOST_MESSAGE_KEEP = 20


def _record_delivery(ok, summary="", error=""):
    with _delivery_lock:
        if ok:
            _delivery['sent'] += 1
            _delivery['consecutive_failed'] = 0
            _delivery['last_success_at'] = time.time()
            return 0
        _delivery['failed'] += 1
        _delivery['consecutive_failed'] += 1
        _delivery['last_error'] = error
        _delivery['last_failure_at'] = time.time()
        _delivery['lost'].append((time.strftime("%H:%M:%S"), summary))
        del _delivery['lost'][:-LOST_MESSAGE_KEEP]
        return _delivery['consecutive_failed']


def get_delivery_health():
    """발신 상태 스냅샷 — 상태창(print_health)이 읽는다."""
    with _delivery_lock:
        return dict(_delivery, lost=list(_delivery['lost']))


def reset_delivery_health():
    """테스트·운영자 확인 후 초기화용."""
    with _delivery_lock:
        _delivery.update({'sent': 0, 'failed': 0, 'consecutive_failed': 0,
                          'last_error': '', 'last_failure_at': None,
                          'last_success_at': None, 'lost': []})


def _get_telegram_footer():
    """텔레그램 메시지용 계좌 정보 꼬리말 생성"""
    if not config.TELEGRAM_BOT_TOKEN:
        return

    cano = config.session.cano
    acnt = config.session.acnt_prdt_cd

    if getattr(config.session, 'is_paper', False):
        # 가상투자는 KIS 실전 시세를 쓰지만 계좌가 가상이므로 알림에서도 실전과 구분한다.
        #  session.cano 에는 안전장치로 문자열 'PAPER'가 박혀 있어(가로채기를 빠져나간
        #  계좌성 호출이 조용히 성공하지 않게 한다) 계좌번호로 쓸 수 없다. VIRT_ACC_NUM 을
        #  표시 전용으로 따로 읽어, 어느 계좌 앞으로 도는 인스턴스인지 식별되게 한다.
        #  (미설정이면 번호 없이 'PAPER'만 남는다 — 종전 표기와 같다)
        acc_label = "PAPER"
        cano = getattr(config.session, 'virt_cano', '') or ''
        acnt = getattr(config.session, 'virt_acnt_prdt_cd', '') or ''
    elif config.session.is_toss:
        acc_label = "토스"
    else:
        acc_label = "실전"
        # 시스템 트레이딩 컨텍스트(AUTO 계좌) 확인.
        #  실전만 수동/자동 계좌가 나뉘므로 라벨이 계좌를 가리키는 이름이 된다.
        if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
            cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            acc_label = "자동"

    # 계좌번호 뒤에 상품코드(01, 22 등)를 붙여 사용자가 정확한 계좌를 식별할 수 있도록 함
    full_acc = f"{cano}-{acnt}" if acnt else cano
    instance_name = config.TELEGRAM_INSTANCE_NAME

    return f"[{instance_name} | {acc_label} {full_acc}]" if full_acc \
        else f"[{instance_name} | {acc_label}]"

def send_telegram_message(message, reply_markup=None, is_urgent=False, sync=False):
    """텔레그램 메시지 전송 (시스템 트레이딩 알림용).

    [반환값] sync=True 일 때만 **실제 전달 여부(bool)** 를 돌려준다. 비동기 전송은
     스레드/큐에 넘기고 즉시 돌아오므로 그 시점에는 성패를 알 수 없어 None 이다.

    [왜 이 구분이 필요한가 · 2026-09-04] '보냈다'를 DB에 표시해 중복을 막는 알림
     (공시·캘린더)이 이 함수를 try 로 감싸고 성공으로 간주했다. 이 함수는 비동기라
     예외를 던지지 않으므로 그 try 는 아무것도 잡지 못한다 — 파이의 네트워크가 끊긴
     동안 도착한 공시는 '발송 완료'로 굳어 **영영 다시 오지 않는다**. 표시하기 전에
     확인할 수단을 준다.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False if sync else None

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
    # [Fix] 닫기 태그는 이름이 없을 수 있다([/] = 가장 최근 태그 닫기). 종전 패턴은 이름을
    #  1자 이상 요구해([a-z]+) '[/]'를 못 지웠고, 화면용 문자열을 그대로 보내는 경로에서
    #  꼬리에 '[/]'가 그대로 노출됐다(공시 상세 '매출대비 5.4%[/]' 등 — 알림·조회 양쪽).
    #  열기 태그는 이름을 계속 요구한다(그래야 '[시스템]' 같은 대괄호 표기가 살아남는다).
    clean_message = re.sub(r'\[/[a-z]*\]|\[[a-z]+(?:[\s=][^\]]*)?\]', '', clean_message)

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
        #  IR: 공시 제목의 '기업설명회(IR)개최'가 매 건마다 걸린다 — 실제 티커(NYSE)이긴 하나
        #   관심종목에 없고 오인 빈도가 압도적이라 ON(ON Semiconductor)과 같은 기준으로 제외한다.
        exclude_words = {"ON", "OFF", "RSI", "MACD", "ATR", "SMA", "EMA", "CCI", "ADX", "SAR", "OBV", "ETF", "TS", "RUN", "STOP", "WAIT", "IR"}
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
        sent_all = True         # 청크가 하나라도 못 가면 그 메시지는 전달 실패다
        last_error = ""
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
                        last_error = f"HTTP {res.status_code}: {str(res.text)[:100]}"
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
                    last_error = error_msg
                    logger.error(f"[Telegram] 전송 중 오류 발생 (Chunk {i+1}/{len(msg_chunks)}, {attempt+1}/{max_retries}): {error_msg}")
                
                if attempt < max_retries - 1:
                    # [수정] 네트워크 단절 시 복구될 시간을 벌기 위해 점진적 대기 (1초 -> 2초 -> 4초)
                    time.sleep(2 ** attempt)
                    
            if not success_chunk:
                logger.error(f"[Telegram] 최종 전송 실패 (Chunk {i+1}/{len(msg_chunks)})")
                sent_all = False

        # [안전장치] 못 간 메시지를 로그·화면에 남긴다. 알림 경로가 죽은 상태에서
        #  '알림이 조용하다'를 '이상 없음'으로 읽으면 손절 경보를 통째로 놓친다.
        summary = " ".join(str(message).split())[:120]
        if sent_all:
            _record_delivery(True)
            return True
        streak = _record_delivery(False, summary=summary, error=last_error)
        logger.error(f"[Telegram] 미전달 메시지(연속 {streak}건째): {summary}")
        if streak >= DELIVERY_ALERT_THRESHOLD and context.is_screen_output_allowed():
            config.console.print(
                f"[bold red]⚠ 텔레그램 전송이 연속 {streak}건 실패했습니다 — "
                f"알림이 도착하지 않고 있습니다.[/bold red]")
            config.console.print(f"[dim red]  마지막 오류: {last_error or '알 수 없음'}[/dim red]")
            config.console.print(f"[dim red]  미전달: {summary}[/dim red]")
        return False

    # [수정] 긴급 발송 여부에 따라 큐(Queue) 대기열 우회 처리
    if sync:
        return _send_task()
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
