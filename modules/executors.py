"""
애플리케이션 전역에서 재사용할 스레드 풀(Thread Pool) 관리 모듈
"""
import concurrent.futures

# AI API(Gemini) 호출용 글로벌 스레드 풀
ai_executor = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="GeminiAI")

# 일반 I/O(크롤링, yfinance 단건 조회 등) 처리용 글로벌 스레드 풀
io_executor = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="ThemeIO")

# 텔레그램 명령어 백그라운드 처리 및 메시지 발송 전용 스레드 풀
bot_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="TgCmdWorker")
tg_sender_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="TgSender")