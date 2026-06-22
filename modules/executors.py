"""
애플리케이션 전역에서 재사용할 스레드 풀(Thread Pool) 관리 모듈
"""
import concurrent.futures
import os

# [메모리 최적화] 저사양 환경(라즈베리파이 등) 대응 풀 크기 조정
#  - 다중 스레드는 스택/glibc 아레나 등으로 RSS를 키운다. 코어 수가 적은 기기에서는 워커 수를
#    줄여 동시 작업량과 메모리 피크를 함께 낮춘다.
#  - CPU 코어가 4개 이하(라즈베리파이3 등)면 축소 프로파일을 적용한다.
_LOW_SPEC = (os.cpu_count() or 1) <= 4

# AI API(Gemini) 호출용 글로벌 스레드 풀
ai_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3 if _LOW_SPEC else 10, thread_name_prefix="GeminiAI")

# 일반 I/O(크롤링, yfinance 단건 조회 등) 처리용 글로벌 스레드 풀
io_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4 if _LOW_SPEC else 10, thread_name_prefix="ThemeIO")

# 텔레그램 명령어 백그라운드 처리 및 메시지 발송 전용 스레드 풀
bot_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3 if _LOW_SPEC else 5, thread_name_prefix="TgCmdWorker")
tg_sender_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="TgSender")