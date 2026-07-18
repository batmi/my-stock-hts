import threading
import concurrent.futures
import logging
import time
import requests
import re
import json
import os
from datetime import datetime, timedelta, timezone

import config
import context # [추가]
import api
import utils
import indicators
from modules import analysis, account, chart, db_manager, auto_trade, market, theme_analysis
from modules.auto_trade import AutoTrader
from modules.executors import bot_executor
from modules.scheduler import SystemScheduler

logger = logging.getLogger(__name__)
console = config.console

class TelegramCommander:
    """텔레그램 명령어를 수신하고 처리하는 클래스"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(TelegramCommander, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized: return
        self._initialized = True
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.is_running = False
        self.thread = None
        self.last_update_id = 0
        self.trader = AutoTrader() # 싱글톤 인스턴스 참조
        self._trade_cache = {}
        self._trade_cache_lock = threading.Lock()
        self.session = requests.Session() # 커넥션 풀링(소켓 누수 방지)
        
        # [리팩토링] 명령어 핸들러 매핑
        self.command_handlers = {
            "/status": self._cmd_status,
            "/start": self._cmd_start,
            "/stop": self._cmd_stop,
            "/restart": self._cmd_restart,
            "/help": self._cmd_help,
            "/report": self._cmd_report,
            "/market": self._cmd_market,
            "/signal": self._cmd_signal,
            "/analyze": self._cmd_analyze, # [추가] AI 심층 진단
            "/ask": self._cmd_ask,
            "/chart": self._cmd_chart,
            "/stocks": self._cmd_stocks,
            "/config": self._cmd_config,
            "/history": self._cmd_history,
            "/log": self._cmd_log,
            "/preset": self._cmd_preset, # [추가] 시장 국면별 프리셋
            "/balance": self._cmd_balance,
            "/holdings": self._cmd_holdings,
            "/closing": self._cmd_closing, # [추가] AI 장 마감 종합 브리핑
            "/curate": self._cmd_curate, # [추가] AI 종목 큐레이션
            "/scan": self._cmd_scan, # [추가] 트레이딩뷰 스크리너
            "/news": self._cmd_news, # [추가] AI 최신 뉴스 검색
            "/memo": self._cmd_memo, # [추가] 종목 메모 관리
            "/rules": self._cmd_rules,
            "/profit": self._cmd_profit,
            "/restrict": self._cmd_restricted,
            "/pending": self._cmd_pending,           # [추가] 미체결 조회
            "/reserves": self._cmd_reserves,         # [추가] 예약 주문 현황 및 취소
            "/addrestrict": self._cmd_addrestrict,   # [추가] 제한 종목 추가
            "/delrestrict": self._cmd_delrestrict,   # [추가] 제한 종목 해제
            "/briefing": self._cmd_briefing,         # [추가] 온디맨드 시황 브리핑
            "/stats": self._cmd_stats                # [추가] 종목별 성과 분석
        }

    def start(self):
        if not self.bot_token: return
        if not config.ENABLE_TELEGRAM: return # [추가] 텔레그램 비활성화 시 시작 안 함
        if self.is_running: return # [추가] 중복 실행 방지
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="TelegramBot")
        self.thread.start()
        logger.debug("[Telegram] 명령어 수신 대기 시작...")

        SystemScheduler().start()

        # [추가] 봇 재시작 알림 전송
        try:
            api.send_telegram_message("🤖 [시스템 알림] 텔레그램 봇이 재연결되었습니다.", reply_markup=self._get_default_keyboard())
        except Exception as e:
            logger.error(f"[Telegram] 재시작 알림 전송 실패: {e}")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2)

        SystemScheduler().stop()

    def _run_loop(self):
        my_thread = threading.current_thread()
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        timeout = config.TELEGRAM_POLLING_TIMEOUT
        
        while self.is_running and self.thread is my_thread:
            try:
                params = {"offset": self.last_update_id + 1, "timeout": timeout, "allowed_updates": ["message"]}
                # 매번 새로운 소켓을 열지 않고 연결을 재사용
                response = self.session.get(url, params=params, timeout=timeout + 5)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('ok'):
                        for result in data.get('result', []):
                            self.last_update_id = result['update_id']
                            self._handle_message(result.get('message', {}))
                elif response.status_code == 409:
                    # Conflict: 다른 인스턴스가 이미 폴링 중임
                    # [수정] 자정 로그 로테이션이나 네트워크 재접속 시 일시적으로 발생할 수 있으므로 즉시 종료하지 않고 대기
                    logger.warning("[Telegram] 409 Conflict 감지. 30초 대기 후 재시도합니다.")
                    time.sleep(30)
                    
            except Exception as e:
                if self.is_running:
                    err_msg = str(e)
                    if "Max retries exceeded" in err_msg or "Network is unreachable" in err_msg or "Connection reset by peer" in err_msg:
                        logger.warning(f"[Telegram] 일시적인 네트워크 연결 오류로 폴링 재시도 중...")
                    else:
                        logger.error(f"[Telegram] Polling Error: {err_msg}")
                time.sleep(5) # 에러 시 대기

    def _handle_message(self, message):
        text = message.get('text', '').strip()
        chat_id = str(message.get('chat', {}).get('id'))
        
        # 설정된 Chat ID와 다르면 무시 (보안)
        if config.TELEGRAM_CHAT_ID and chat_id != str(config.TELEGRAM_CHAT_ID):
            return

        # [추가] 하단 고정 메뉴 버튼 텍스트 매핑
        button_map = {
            "📊 상태 요약": "/status",
            "🟢 상태 요약": "/status",
            "🔵 상태 요약": "/status",
            "🟡 상태 요약": "/status",
            "⚪ 상태 요약": "/status",
            "🔴 상태 요약": "/status",
            "💰 계좌 잔고": "/balance",
            "💼 보유 종목": "/holdings",
            "📝 관심 종목": "/stocks",
            "📈 시장 지수": "/market",
            "🔎 종목 스캔": "/scan k",
            "❓ 도움말": "/help",
            "📅 일간 손익": "/profit d",
            "📜 주간 거래": "/history w",
            "📊 월간 성과": "/report m",
            "⏳ 예약 현황": "/reserves",
            "🛑 거래 정지": "/stop",
            "▶️ 거래 시작": "/start"
        }
        if text in button_map:
            text = button_map[text]

        if not text.startswith('/'): return

        # 명령어 파싱 (인자 포함)
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        # [추가] /signal_종목코드 단축 명령어 지원
        if command.startswith('/signal_'):
            target = command.replace('/signal_', '')
            args = [target] + args
            command = '/signal'

        # [추가] /analyze_종목코드 단축 명령어 지원
        if command.startswith('/analyze_'):
            target = command.replace('/analyze_', '')
            args = [target] + args
            command = '/analyze'

        # [추가] /chart_종목코드 단축 명령어 지원
        if command.startswith('/chart_'):
            target = command.replace('/chart_', '')
            
            # [수정] 차트 유형(일/시/분)이 포함된 단축 명령어 지원 (/chart_h_005930 등)
            if target.startswith('h_'):
                args = ['h', target[2:]] + args
            elif target.startswith('m_'):
                args = ['m', target[2:]] + args
            elif target.startswith('d_'):
                args = ['d', target[2:]] + args
            else:
                args = [target] + args
            command = '/chart'

        # 핸들러 호출
        if command in self.command_handlers:
            def execute_cmd():
                # 명령어 처리에 앞서 접수 알림을 동기적(순차적)으로 먼저 완전히 전송
                # sleep 없이 실행 순서를 명확하게 보장함
                self._send_reply(f"⌨️ 명령어 접수: {command}", sync=True)
                try:
                    response = self.command_handlers[command](args)
                    if response:
                        self._send_reply(response)
                except Exception as e:
                    logger.error(f"[Telegram] 명령어({command}) 처리 중 에러: {e}")
                    self._send_reply("⚠️ 명령어 처리 중 시스템 오류가 발생했습니다.")
                    
            bot_executor.submit(execute_cmd)
        else:
            self._send_reply(f"⚠️ 지원하지 않는 명령어입니다: {command}\n전체 명령어 목록을 보려면 /help 를 입력해주세요.")

    def _cmd_briefing(self, args):
        self._send_reply("⏳ [AI 시황 브리핑] 실시간 글로벌 마켓 데이터를 수집하고 AI 시황 브리핑을 작성 중입니다. 잠시만 기다려주세요...")
        bot_executor.submit(SystemScheduler().execute_briefing)
        return None

    def _cmd_closing(self, args):
        self._send_reply("⏳ [AI 장 마감 종합 브리핑] 오늘 시장 흐름, 보유 종목 특이사항 및 당일 매매 내역을 종합 분석 중입니다. 잠시만 기다려주세요...")
        bot_executor.submit(SystemScheduler().execute_daily_closing_report)
        return None
            
    def _cmd_stats(self, args):
        keyword = " ".join(args).strip() if args else None
        return self._get_stock_stats_message(keyword)

    def _get_stock_stats_message(self, keyword=None):
        """종목별 매매 성과 분석 조회"""
        target_account = None
        if config.session.is_simulation:
            target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
        elif config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        trades = self._get_refined_trades_cached(target_account=target_account)

        if not trades:
            return "📭 매매 기록이 없습니다."

        # [추가] 체결된 내역만 통계에 포함 (접수, 취소 등 미체결 제외)
        trades = [r for r in trades if "체결" in r.get('order_status', '')]
        
        if not trades:
            return "📭 체결된 매매 기록이 없습니다."

        filter_code = None
        if keyword:
            code, name, _ = self._resolve_stock(keyword)
            if not code:
                return f"⚠️ '{keyword}' 종목을 찾을 수 없습니다."
            filter_code = code

        from collections import Counter
        stock_stats = {}
        buy_times_per_stock = {}

        trades.sort(key=lambda x: x.get('time', ''))

        for r in trades:
            code = r['code']
            if filter_code and code != filter_code:
                continue

            if code not in stock_stats:
                stock_stats[code] = {
                    'name': r['name'], 'buy': 0, 'sell': 0,
                    'profit': 0, 'rates': [], 'wins': 0,
                    'reasons': [], 'holding_secs': [],
                    'max_rate': -999.0, 'min_rate': 999.0
                }
            if code not in buy_times_per_stock:
                buy_times_per_stock[code] = []

            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except Exception as e:
                logger.debug(f"Date parse error in stats: {e}")
                dt = datetime.now()

            if r['type'] == 'buy':
                stock_stats[code]['buy'] += 1
                buy_times_per_stock[code].append(dt)
            elif r['type'] == 'sell':
                stock_stats[code]['sell'] += 1
                p = int(float(r.get('profit_amt') or 0))
                rate = float(r.get('profit_rate') or 0.0)

                stock_stats[code]['profit'] += p
                stock_stats[code]['rates'].append(rate)
                if p > 0: stock_stats[code]['wins'] += 1

                reason_raw = r.get('reason', '')
                reason_simple = "기타"
                if "반익절" in reason_raw: reason_simple = "반익절"
                elif "과열" in reason_raw: reason_simple = "과열매도"
                elif "익절" in reason_raw: reason_simple = "익절"
                elif "ATR손절" in reason_raw: reason_simple = "ATR손절"
                elif "손절" in reason_raw: reason_simple = "손절"
                elif "트레일링" in reason_raw: reason_simple = "트레일링스탑"
                elif "시간청산" in reason_raw: reason_simple = "시간청산"
                elif "추세" in reason_raw or "점수하락" in reason_raw or "매도진입" in reason_raw: reason_simple = "추세이탈"
                elif "수동" in reason_raw: reason_simple = "수동매도"
                stock_stats[code]['reasons'].append(reason_simple)

                if rate > stock_stats[code]['max_rate']: stock_stats[code]['max_rate'] = rate
                if rate < stock_stats[code]['min_rate']: stock_stats[code]['min_rate'] = rate

                if buy_times_per_stock[code]:
                    buy_dt = buy_times_per_stock[code].pop(0)
                    hold_sec = (dt - buy_dt).total_seconds()
                    stock_stats[code]['holding_secs'].append(hold_sec)

        if not stock_stats:
            return f"📭 '{keyword}' 종목의 매매 기록이 없습니다."

        msg = "📊 [종목별 성과 분석]\n"
        
        sorted_stats = sorted(stock_stats.items(), key=lambda x: x[1]['profit'], reverse=True)
        
        for code, stat in sorted_stats:
            s_cnt = stat['sell']
            win_rate = (stat['wins'] / s_cnt * 100) if s_cnt > 0 else 0.0
            avg_rate = (sum(stat['rates']) / s_cnt) if s_cnt > 0 else 0.0

            max_r = stat['max_rate'] if stat['max_rate'] != -999.0 else 0.0
            min_r = stat['min_rate'] if stat['min_rate'] != 999.0 else 0.0
            range_str = f"{max_r:+.1f}% / {min_r:+.1f}%" if s_cnt > 0 else "-"

            reason_str = "-"
            if stat['reasons']:
                c = Counter(stat['reasons'])
                most_common = c.most_common(1)[0]
                reason_str = f"{most_common[0]}({most_common[1]}회)"

            hold_str = "-"
            if stat['holding_secs']:
                avg_sec = sum(stat['holding_secs']) / len(stat['holding_secs'])
                if avg_sec < 60: hold_str = f"{int(avg_sec)}초"
                elif avg_sec < 3600: hold_str = f"{int(avg_sec//60)}분"
                elif avg_sec < 86400: hold_str = f"{int(avg_sec//3600)}시간"
                else: hold_str = f"{int(avg_sec//86400)}일"

            p_icon = "🔴" if stat['profit'] > 0 else ("🔵" if stat['profit'] < 0 else "⚪")

            item_msg = f"\n• {stat['name']} ({code})\n"
            item_msg += f"  매매: {stat['buy']} / {stat['sell']} (매수/매도)\n"
            item_msg += f"  승률: {win_rate:.1f}% | {p_icon} 총손익: {stat['profit']:+,}원\n"
            item_msg += f"  평균수익: {avg_rate:+.2f}% (Max/Min: {range_str})\n"
            item_msg += f"  주요사유: {reason_str} | 평균보유: {hold_str}\n"

            msg += item_msg

        return msg.strip()

    def _cmd_curate(self, args):
        self._send_reply("⏳ [AI 종목 큐레이션] 실시간 시장 매크로 데이터 및 뉴스를 분석하여 주도주를 발굴 중입니다. 잠시만 기다려주세요...")
        bot_executor.submit(self._execute_curate)
        return None
        
    def _execute_curate(self):
        try:
            result = theme_analysis.generate_stock_curation()
            if result:
                if result.startswith("⚠️"):
                    self._send_reply(result)
                else:
                    msg = f"{result}\n\n마음에 드는 종목이 있다면 터미널 HTS 메뉴 [7] 관심 종목 관리 -> [1] 종목 추가 를 통해 수동으로 등록해주세요."
                    self._send_reply(msg)
            else:
                self._send_reply("⚠️ AI 큐레이션 생성에 실패했습니다.")
        except Exception as e:
            logger.error(f"Curation error: {e}")
            self._send_reply("⚠️ 큐레이션 실행 중 오류가 발생했습니다.")

    def _cmd_scan(self, args):
        self._send_reply("⏳ [TradingView Screener] 시장을 스캔 중입니다. 잠시만 기다려주세요...")
        bot_executor.submit(self._execute_scan, args)
        return None
        
    def _execute_scan(self, args):
        try:
            from tradingview_screener import Query, Column
            import pandas as pd
        except ImportError:
            self._send_reply("⚠️ tradingview-screener 라이브러리가 설치되지 않았습니다. 터미널에서 pip install tradingview-screener 명령어를 실행해주세요.")
            return

        market = "korea"
        
        if args:
            arg_str = "".join(args).lower()
            if 'k' in arg_str:
                market = "korea"
            elif 'u' in arg_str:
                market = "america"

        presets = [
            ("TopGainers", "당일 급상승 상위 15종목"),
            ("TopLosers", "당일 급하락 상위 15종목"),
            ("GapUp", "갭상승 출발"),
            ("Breakout", "신고가 돌파 주도주"),
            ("Pullback", "정배열 눌림목"),
            ("VolumeMomentum", "폭발적 수급 유입"),
            ("OversoldRebound", "낙폭과대 바닥 탈출"),
            ("ValueTurnaround", "저평가 우량주 턴어라운드"),
            ("HighDividend", "고배당 상승 추세"),
            ("TrendReversal", "상승 추세 전환")
        ]

        # [단일 관리 지점] 프리셋 조건/설명/쿼리는 theme_analysis(SCREENER_*)에서 공유한다.
        #   텔레그램 프리셋 키 -> 정식 프리셋 ID 매핑
        tg_to_id = {
            "TopGainers": "gainers", "TopLosers": "losers", "GapUp": "gapup", "Breakout": "breakout", "Pullback": "pullback",
            "VolumeMomentum": "volume", "OversoldRebound": "oversold", "ValueTurnaround": "value",
            "HighDividend": "dividend", "TrendReversal": "reversal"
        }
        preset_conditions = {k: theme_analysis.screener_condition_str(market, cid) for k, cid in tg_to_id.items()}
        preset_desc = {k: theme_analysis.SCREENER_PRESETS[cid]["desc"] for k, cid in tg_to_id.items()
                       if theme_analysis.SCREENER_PRESETS[cid]["desc"]}

        try:
            market_str = "미국" if market == "america" else "국내"
            final_msg = f"🔎 [TradingView 시장 스캔 결과]\n• 시장: {market_str}\n"

            for preset_key, desc in presets:
                # 공용 빌더로 쿼리/후처리 생성 (메뉴6 종목트렌드분석과 동일 로직)
                query, post_fn = theme_analysis.build_screener_query(market, tg_to_id[preset_key])

                count, df = 0, None
                for attempt in range(3):
                    try:
                        count, df = query.get_scanner_data()
                        break
                    except Exception as e:
                        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                            if attempt < 2:
                                time.sleep(1.5)
                            else:
                                logger.warning(f"TradingView Screener Timeout: {preset_key}")
                        else:
                            raise e

                if df is not None and not df.empty and post_fn is not None:
                    df = post_fn(df)
                
                cond_str = f" {preset_conditions[preset_key]}" if preset_key in preset_conditions else ""
                final_msg += f"\n▶ {desc}{cond_str}\n"
                if preset_key in preset_desc:
                    final_msg += f"  : {preset_desc[preset_key]}\n"
                if df is None or df.empty:
                    final_msg += "  📭 조건에 맞는 종목 없음\n"
                    continue
                    
                for idx, row in df.iterrows():
                    ticker = str(row.get('name', '')).strip()
                    name = str(row.get('description', ticker)).strip()
                    
                    # 국내 주식인 경우 한글 종목명 변환 (알파벳으로만 구성된 경우 API 직접 호출)
                    if market == "korea":
                        kor_name = api.get_stock_name_by_code(ticker, is_overseas=False)
                        if not kor_name or kor_name == ticker or all(ord(c) < 128 for c in kor_name.replace(' ', '')):
                            try:
                                res = api.get_current_price_data(ticker, is_overseas=False)
                                if res and res.get('rt_cd') == '0':
                                    out = res.get('output', {})
                                    fetched_name = out.get('prdt_abrv_name') or out.get('prdt_name')
                                    if fetched_name: kor_name = fetched_name
                            except Exception:
                                pass
                        if kor_name: name = kor_name

                    close = row.get('close', 0)
                    change = row.get('change', 0)
                    rsi = row.get('RSI', 0)
                    adx = row.get('ADX', 0)
                    
                    close_str = f"${close:,.2f}" if market == "america" else f"{int(close):,}원"
                    rsi_str = f"RSI: {rsi:.1f}" if pd.notna(rsi) else ""
                    adx_str = f" / ADX: {adx:.1f}" if pd.notna(adx) else ""
                    
                    extra_str = ""
                    if preset_key in ["ValueRebound", "HighDividend"]:
                        per = row.get('price_earnings_ttm')
                        roe = row.get('return_on_equity')
                        div = row.get('dividend_yield_recent')
                        
                        per_str = f"PER:{per:.1f}" if pd.notna(per) else ""
                        roe_str = f"ROE:{roe:.1f}%" if pd.notna(roe) else ""
                        div_str = f"배당:{div:.2f}%" if pd.notna(div) else ""
                        
                        tags = [t for t in [per_str, roe_str, div_str] if t]
                        if tags: extra_str = f" | {' '.join(tags)}"
                    
                    final_msg += f"• {name} ({ticker})\n  {close_str} ({change:+.2f}%) {rsi_str}{adx_str}{extra_str}\n\n"
                
            final_msg += "\n상세 분석을 원하시면 '/analyze 종목코드'를 입력하세요."
            self._send_reply(final_msg.strip())
            
        except Exception as e:
            logger.error(f"Screener Scan Error: {e}")
            self._send_reply(f"⚠️ 스크리너 실행 중 오류가 발생했습니다.\n{e}")

    # --- 명령어 핸들러 메서드 ---
    def _cmd_status(self, args):
        self._send_reply("⏳ [시스템 상태 조회] 현재 상태 및 자산 정보를 수집 중입니다. 잠시만 기다려주세요...")
        return self.trader.get_status_message()

    def _cmd_start(self, args):
        if self.trader.is_running:
            return "⚠️ 이미 시스템 트레이딩이 실행 중입니다."
        else:
            self.trader.start(interactive=False)
            return "🚀 시스템 트레이딩을 시작했습니다."

    def _cmd_stop(self, args):
        if not self.trader.is_running:
            return "⚠️ 실행 중인 시스템 트레이딩이 없습니다."
        else:
            self.trader.stop(use_status=False)
            return "🛑 시스템 트레이딩 중단 요청을 처리했습니다."

    def _cmd_restart(self, args):
        msg = []
        if self.trader.is_running:
            self.trader.stop(use_status=False)
            msg.append("🛑 시스템 트레이딩 중단 완료.")
            time.sleep(1)  # 상태 정리 대기
        
        self.trader.start(interactive=False)
        msg.append("🚀 시스템 트레이딩을 재시작했습니다.")
        return "\n".join(msg)

    def _cmd_help(self, args):
        return (
            "🤖 [시스템 트레이딩 봇 도움말]\n\n"
            "⚙️ [시스템 제어]\n"
            "• /start : 자동매매 시작\n"
            "• /stop : 자동매매 중단\n"
            "• /restart : 자동매매 재시작\n"
            "• /status : 시스템 상태 조회\n"
            "• /config : 트레이딩 전략 설정값 조회\n"
            "• /preset [설정] : 시장 설정 프리셋 (b/r/s/d)\n\n"
            "💰 [계좌 및 자산]\n"
            "• /balance : 자산 및 예수금 조회\n"
            "• /holdings : 보유 종목 및 수익률 조회\n"
            "• /pending : 미체결 주문 내역 조회\n"
            "• /reserves  : 예약매매 현황 및 취소 (d)\n"
            "• /profit [기간] : 실현 손익 (d/w/m/n)\n"
            "• /history [기간] : 거래 내역 (d/w/m/n)\n"
            "• /report [기간] : 성과 리포트 (d/w/m/n)\n"
            "• /stats [종목] : 종목별 매매 성과 분석\n\n"
            "📈 [시장 및 종목 분석]\n"
            "• /market [그룹] : 지수 현황 (k/u/s/r/g/c/b)\n"
            "• /signal <종목/지수> : 기술적 분석 및 진단\n"
            "• /analyze <종목/지수> : AI 종목/지수 심층 진단\n"
            "• /chart [기간] <종목/지수> : 차트 전송 (d/h/m)\n"
            "• /briefing : 온디맨드 AI 시황 브리핑\n"
            "• /closing : AI 장 마감 종합 브리핑\n"
            "• /curate : 실시간 시장 주도주 AI 추천\n"
            "• /scan [시장] : 트레이딩뷰 종목 스캔 (k/u)\n"
            "• /news <종목> : AI 최신 뉴스 5개 및 링크\n"
            "• /ask <질문> : AI 주식/경제 자유 질문\n\n"
            "📝 [관리 및 기타]\n"
            "• /stocks : 감시 중인 관심 종목 리스트\n"
            "• /rules [종목] : 개별 트레이딩 룰 조회\n"
            "• /restrict : 트레이딩 제한 종목 조회\n"
            "• /memo [a/d/종목] : 메모 조회/추가/삭제\n"
            "• /addrestrict <종목> [사유] : 제한 종목 추가\n"
            "• /delrestrict <종목> : 제한 종목 해제\n"
            "• /log : 최근 시스템 트레이딩 로그\n"
            "• /help : 명령어 목록 확인"
        )

    def _cmd_report(self, args):
        days = 0 # 기본값: 당일(0)
        if args:
            arg = args[0].lower()
            if arg in ["d", "day", "daily", "일간"]: days = 0
            elif arg in ["w", "week", "weekly", "주간"]: days = 7
            elif arg in ["m", "month", "monthly", "월간"]: days = 30
            elif arg.isdigit(): days = int(arg)
        return self.trader.get_performance_report(days=days)

    def _cmd_market(self, args):
        group_map = {
            'k': "국내 지수 (Domestic Indices)",
            'u': "미국 지수 (US Indices)",
            's': "섹터 및 지표 (Sectors & Indicators)",
            'r': "금리 및 환율 (Rates & FX)",
            'g': "글로벌 지수 (Global Indices)",
            'c': "원자재 (Commodities)",
            'b': "암호화폐 (Cryptocurrency)"
        }
        
        self._send_reply("⏳ [시장 지수 조회] 지수 데이터를 수집 중입니다. 약간의 시간이 소요될 수 있습니다...")
        
        if not args:
            return self._get_market_status(None)
            
        keys = "".join(args).lower()
        target_groups = []
        invalid_keys = []
        
        for k in keys:
            if k in group_map:
                if group_map[k] not in target_groups:
                    target_groups.append(group_map[k])
            else:
                if k not in invalid_keys:
                    invalid_keys.append(k)
                    
        if invalid_keys:
            return f"⚠️ 잘못된 그룹 키가 포함되어 있습니다: {', '.join(invalid_keys)}\n(사용 가능: {', '.join(group_map.keys())})"
            
        return self._get_market_status(target_groups)

    def _cmd_signal(self, args):
        if not args: return "⚠️ 종목명이나 코드를 입력해주세요.\n예: /signal 삼성전자"
        keyword = " ".join(args)
        code, name, _ = self._resolve_stock(keyword)
        display_name = f"{name}({code})" if code else keyword
        self._send_reply(f"⏳ '{display_name}' 기술적 분석 및 진단 중...\n(데이터 조회 및 지표 계산에 약간의 시간이 소요될 수 있습니다)")
        return self._analyze_stock(keyword)
        
    def _cmd_analyze(self, args):
        if not args: return "⚠️ 종목명이나 코드를 입력해주세요.\n예: /analyze 삼성전자"
        
        keyword = " ".join(args)
        code, name, is_overseas = self._resolve_stock(keyword)
        if not code:
            return f"⚠️ '{keyword}' 종목을 찾을 수 없습니다."

        self._send_reply(f"⏳ '{name}({code})' 심층 진단 중...\n(차트 분석 + AI 모멘텀 분석 중이므로 10~20초 소요됩니다)")
        
        try:
            df = api.get_chart_data(code, is_overseas)
            if df is None or df.empty:
                return f"⚠️ 차트 데이터를 불러올 수 없어 분석할 수 없습니다."
            
            # [추가] 실시간 현재가 조회 및 차트 당일 고가/저가 실시간 갱신 (점수 불일치 방지)
            try:
                rt_price = api.get_current_price(code, is_overseas=is_overseas)
                indicators.apply_realtime_price(df, rt_price)
            except Exception: pass
            
            ind = indicators.calculate_indicators(df)
            current_price = float(df.iloc[-1]['close'])
            
            # 상태 분류를 위한 전일 RSI — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
            prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None

            w52_pos = 0.0
            if len(df) > 0:
                recent_df = df.tail(250)
                h52 = recent_df['high'].max()
                l52 = recent_df['low'].min()
                if h52 > l52:
                    w52_pos = (current_price - l52) / (h52 - l52) * 100
                    
            sm_flag, _ = analysis.check_smart_money_turnaround(code, is_overseas)
            
            # [추가] 시장 국면(적응형 임계값) 보정 적용
            score_adj = 0.0
            if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True) and not is_overseas:
                market_type = "KOSPI"
                try:
                    cp = api.get_current_price_data(code, False)
                    if cp.get('rt_cd') == '0' and "코스닥" in cp['output'].get('rprs_mrkt_kor_name', ''):
                        market_type = "KOSDAQ"
                except Exception: pass
                _, score_adj = analysis.get_market_regime(market_type)
                
            thresholds = {
                "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj,
                "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                "WEIGHTS": config.SCORING_WEIGHTS
            }

            state, _, state_reason = analysis.classify_stock_state(
                df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
            )

            score, _ = analysis.calculate_score(
                df=df, ind=ind, weights=thresholds.get('WEIGHTS') if thresholds else None, smart_money=sm_flag
            )
            score = round(score, 1)

            plus_di = ind.get('plus_di')
            minus_di = ind.get('minus_di')
            dmi_str = "-"
            if plus_di is not None and minus_di is not None:
                if plus_di > minus_di:
                    dmi_str = f"+DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
                elif minus_di > plus_di:
                    dmi_str = f"-DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
                else:
                    dmi_str = f"중립 ({plus_di:.1f} / {minus_di:.1f})"

            is_domestic_index = not is_overseas and code in ["KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"]
            from modules import market
            all_idx_codes = [c for n, c in market.ALL_INDICES]
            is_index = is_domestic_index or (is_overseas and code in all_idx_codes)

            rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
            adx_val = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
            cci_val = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
            
            if is_index:
                price_str = f"{current_price:,.0f}" if current_price >= 1000 else f"{current_price:,.2f}"
                tech_info = (
                    f"• 현재가: {price_str}\n"
                    f"• 시스템 상태: {state} (사유: {state_reason})\n"
                    f"• 핵심 지표: RSI {rsi_val} | ADX {adx_val} | CCI {cci_val} | DMI {dmi_str}"
                )
            else:
                price_str = f"${current_price:,.2f}" if is_overseas else f"{int(current_price):,}원"
                tech_info = (
                    f"• 현재가: {price_str}\n"
                    f"• 시스템 상태: {state} (사유: {state_reason})\n"
                    f"• 퀀트 점수: {score}점 / 10점 만점\n"
                    f"• 핵심 지표: RSI {rsi_val} | ADX {adx_val} | CCI {cci_val} | DMI {dmi_str}"
                )

            if is_index:
                answer = theme_analysis.analyze_index_with_gemini(code, name, tech_info)
                return f"🤖 [AI 지수 심층 진단] {name}({code})\n\n{answer}"
            else:
                answer = theme_analysis.analyze_stock_with_gemini(code, name, tech_info)
                return f"🤖 [AI 종목 심층 진단] {name}({code})\n\n{answer}"
        except Exception as e:
            return f"⚠️ 진단 중 오류 발생: {e}"

    def _cmd_memo(self, args):
        """종목 메모 관리 (조회, 추가, 삭제)"""
        if not args:
            # 전체 메모 요약 조회
            memos = utils.get_all_stock_memos()
            if not memos: return "📭 저장된 종목 메모가 없습니다."
            
            grouped_memos = {}
            for m in memos:
                if m['code'] not in grouped_memos:
                    grouped_memos[m['code']] = {'name': m['name'], 'count': 0, 'latest': m['memo'], 'date': m['updated_at']}
                grouped_memos[m['code']]['count'] += 1
                
            msg = "📋 [전체 종목 메모 현황]\n\n"
            for code, data in grouped_memos.items():
                msg += f"• {data['name']}({code}) : {data['count']}건\n"
            msg += "\n상세조회 : /memo [종목명]\n추가 : /memo a [종목] [내용]"
            return msg
            
        subcmd = args[0].lower()
        
        if subcmd == "a":
            if len(args) < 3: return "⚠️ 사용법: /memo a [종목명/코드] [메모 내용...]"
            code, name, _ = self._resolve_stock(args[1])
            if not code: return f"⚠️ '{args[1]}' 종목을 찾을 수 없습니다."
            memo_text = " ".join(args[2:])
            if utils.add_stock_memo(code, name, memo_text):
                return f"✅ '{name}' 종목에 메모가 추가되었습니다."
            return "⚠️ 메모 추가에 실패했습니다."
            
        elif subcmd == "d":
            if len(args) < 2: return "⚠️ 사용법: /memo d [메모ID 또는 종목명]"
            target = args[1]
            if target.isdigit():
                if utils.delete_stock_memo_by_id(int(target)):
                    return f"🗑 메모(ID: {target})가 삭제되었습니다."
                return "⚠️ 메모 삭제 실패. (존재하지 않는 ID)"
            else:
                code, name, _ = self._resolve_stock(" ".join(args[1:]))
                if not code: return f"⚠️ '{target}' 종목을 찾을 수 없습니다."
                utils.delete_all_stock_memos(code)
                return f"🗑 '{name}' 종목에 작성된 모든 메모가 삭제되었습니다."
                
        else:
            # 특정 종목 상세 조회
            keyword = " ".join(args)
            code, name, _ = self._resolve_stock(keyword)
            if not code: return f"⚠️ '{keyword}' 종목을 찾을 수 없습니다."
            memos = utils.get_stock_memos(code)
            if not memos: return f"📭 '{name}' 종목에 저장된 메모가 없습니다."
            
            msg = f"📝 [{name} ({code}) 메모 현황]\n\n"
            for m in memos:
                msg += f"ID: {m['id']} | {m['updated_at']}\n{m['memo']}\n\n"
            msg += "메모 삭제 : /memo d [ID]"
            return msg.strip()

    def _cmd_news(self, args):
        """/news <종목> : AI 기반 최신 뉴스 5개 및 링크 검색"""
        if not args:
            return "⚠️ 사용법: /news <종목명 또는 코드>\n(예: /news 삼성전자, /news TSLA)"
            
        keyword = " ".join(args)
        code, name, is_overseas = self._resolve_stock(keyword)
        
        display_name = name if name else keyword
        self._send_reply(f"🔍 '{display_name}'의 최신 뉴스를 수집 및 분석 중입니다... (약 10초 소요)")
        
        def _task():
            from modules import theme_analysis
            # 해외 주식은 네이버 국내 뉴스 크롤링이 안 되므로 코드를 넘기지 않음
            target_code = code if not is_overseas else None
            result = theme_analysis.get_latest_news_with_gemini(display_name, target_code)
            self._send_reply(result)
            
        bot_executor.submit(_task)
        return None

    def _cmd_ask(self, args):
        if not args:
            return "⚠️ 질문을 입력해주세요.\n예: /ask 오늘 삼성전자 주식 가격이 왜 오르는거야?"
        question = " ".join(args)
        
        self._send_reply(f"⏳ '{question}'\n\nAI가 분석 중입니다. 잠시만 기다려주세요...")
        
        answer = theme_analysis.ask_gemini(question)
        return f"🤖 [AI 답변]\n\n{answer}"

    def _cmd_chart(self, args):
        if not args:
            self._send_reply("⚠️ 종목명이나 코드를 입력해주세요.\n예: /chart [d/h/m] 삼성전자")
            return None

        period_type = 'daily'
        period_label = "일봉"
        keyword_args = args

        if args[0].lower() in ['d', 'h', 'm']:
            p = args[0].lower()
            if p == 'd': period_type, period_label = 'daily', '일봉'
            elif p == 'h': period_type, period_label = 'hourly', '시봉'
            elif p == 'm': period_type, period_label = 'intraday', '분봉'
            keyword_args = args[1:]
            
        if not keyword_args:
            self._send_reply("⚠️ 종목명이나 코드를 입력해주세요.\n예: /chart [d/h/m] 삼성전자")
            return None

        self._send_chart(" ".join(keyword_args), period_type, period_label)
        return None

    def _cmd_stocks(self, args):
        return self._get_monitoring_list()

    def _cmd_config(self, args):
        return self._get_strategy_config()

    def _cmd_rules(self, args):
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = auto_trade._enrich_rules_with_weights(custom_rules)
        if not custom_rules:
            return "📭 설정된 개별 종목 룰이 없습니다."
            
        # [추가] 제한 종목 로드
        restricted_stocks = auto_trade.get_restricted_stocks()

        target = " ".join(args).strip()
        filtered_rules = []

        if target:
            for r in custom_rules:
                if target.upper() == r['code'] or target in r['name']:
                    filtered_rules.append(r)
            
            if not filtered_rules:
                return f"📭 '{target}'에 대한 개별 룰 설정이 없습니다."
        else:
            filtered_rules = custom_rules

        msg = f"🔧 [개별 종목 룰 ({len(filtered_rules)}개)]\n"
        for r in filtered_rules:
            code = r['code']
            name = r['name']
            
            memo_part = f"   메모: {r.get('memo', '')}\n" if r.get('memo') else ""
            
            # [추가] 가중치 표시
            w_str = "기본"
            if r.get('weights'):
                try:
                    w = r['weights']
                    if isinstance(w, str): w = json.loads(w)
                    if isinstance(w, dict):
                        w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"
                except Exception as e:
                    logger.debug(f"Weight parse error in _cmd_rules: {e}")
                
            sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"

            msg += (f"\n• {name}({code})\n"
                    f"   매수: {r['buy_score']}점 / RSI {r.get('buy_rsi', 65.0)}↓ / 체결 {r.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get('BUY_VOL_STRENGTH', 100.0))}% / 비대칭 {r.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0))}배↑\n"
                    f"   청산: 익절 +{r['take_profit']}% / 과열 RSI {r.get('take_profit_rsi', 75.0)}↑ / TS +{r['ts_activation']}%(-{r['ts_callback']}%) / 기한 {r.get('time_stop_days', 10)}일\n"
                    f"   리스크: 비중 {r.get('invest_ratio', config.settings.SYSTEM_INVEST_PER_STOCK)*100:.0f}% / 손절 {sl_str}\n"
                    f"   가중치: {w_str}\n"
                    f"{memo_part}")
        return msg

    def _cmd_restricted(self, args):
        data = auto_trade.load_restricted_stocks()
        if not data:
            return "📭 트레이딩 제한 종목이 없습니다."
            
        # [추가] 개별 룰 로드
        custom_rules = db_manager.db.get_all_stock_strategies()
        rules_map = {r['code']: True for r in custom_rules}

        msg = f"🚫 [트레이딩 제한 종목 ({len(data)}개)]\n"
        for code, info in data.items():
            name = info.get('name', code)
            
            global_memo = info.get('memo', '')
            accounts = info.get('accounts', {})
            
            memo_parts = []
            if global_memo:
                memo_parts.append(f"전체: {global_memo}")
            for acc, acc_info in accounts.items():
                if isinstance(acc_info, str):
                    memo_parts.append(f"{acc.rstrip('-')}(지정계좌): {acc_info}")
                else:
                    a_type = acc_info.get("type", "지정계좌")
                    a_memo = acc_info.get("memo", "")
                    memo_parts.append(f"{acc.rstrip('-')}({a_type}): {a_memo}")
                
            display_memo = " | ".join(memo_parts) if memo_parts else "-"
            msg += f"\n• {name}({code})\n   메모: {display_memo}"
        return msg

    def _cmd_addrestrict(self, args):
        if not args:
            return "⚠️ 사용법: /addrestrict <종목명/코드> [사유]\n(예: /addrestrict 삼성전자 어닝쇼크)"
            
        keyword = args[0]
        code, name, is_overseas = self._resolve_stock(keyword)
        if not code:
            return f"⚠️ '{keyword}' 종목을 찾을 수 없습니다."
            
        memo = " ".join(args[1:]) if len(args) > 1 else "텔레그램 원격 차단"
        
        auto_trade.add_restricted_stock(code, name, memo, is_overseas=is_overseas)
        
        msg = f"🚫 [제한 종목 추가 완료]\n• 종목: {name}({code})\n• 사유: {memo}\n\n즉시 글로벌 자동매매 대상에서 차단되었습니다."
        return msg

    def _cmd_delrestrict(self, args):
        if not args:
            return "⚠️ 사용법: /delrestrict <종목명/코드>"
            
        keyword = " ".join(args)
        code, name, _ = self._resolve_stock(keyword)
        if not code:
            return f"⚠️ '{keyword}' 종목을 찾을 수 없습니다."
            
        data = auto_trade.load_restricted_stocks()
        if code in data:
            del data[code]
            auto_trade.save_restricted_stocks(data)
            return f"✅ [제한 종목 해제 완료]\n• 종목: {name}({code})\n\n자동매매 제한이 정상적으로 해제되었습니다."
        else:
            return f"⚠️ '{name}({code})' 종목은 현재 제한 목록에 없습니다."

    def _cmd_pending(self, args):
        self._send_reply("⏳ [미체결 주문 내역] 데이터를 조회 중입니다. 잠시만 기다려주세요...", sync=True)
        
        accounts = []
        if config.session.cano:
            accounts.append((config.session.cano, config.session.acnt_prdt_cd, "모의" if config.session.is_simulation else "실전"))
        
        if not config.session.is_simulation and config.session.auto_cano and config.session.auto_cano != config.session.cano:
            accounts.append((config.session.auto_cano, config.session.auto_acnt_prdt_cd, "자동"))

        msg = "⏳ [미체결 주문 내역]\n"
        has_any_orders = False
        
        for cano, acnt, label in accounts:
            with utils.AccountContext(cano):
                try:
                    dom_orders = api.get_domestic_open_orders(cano, acnt)
                except Exception as e:
                    logger.error(f"국내 미체결 조회 에러: {e}")
                    dom_orders = []
                    
                try:
                    us_orders = api.get_overseas_open_orders(cano, acnt)
                except Exception as e:
                    logger.error(f"해외 미체결 조회 에러: {e}")
                    us_orders = []
                
                if dom_orders or us_orders:
                    has_any_orders = True
                    msg += f"\n[{label} 계좌: {cano}-{acnt}]\n"
                    
                    for o in (dom_orders or []):
                        name = o.get('prdt_name')
                        pdno = o.get('pdno')
                        odno = o.get('odno')
                        rmn_qty = api.safe_int(o.get('rmn_qty', 0) or o.get('psbl_qty', 0))
                        ord_unpr = api.safe_int(o.get('ord_unpr', 0))
                        
                        sll_buy = o.get('sll_buy_dvsn_cd_name', '').strip()
                        if not sll_buy:
                            cd = o.get('sll_buy_dvsn_cd', '')
                            sll_buy = "매도" if cd == '01' else ("매수" if cd == '02' else cd)
                            
                        price_str = f"{ord_unpr:,}원" if ord_unpr > 0 else "시장가"
                        msg += f"• [국내] {sll_buy} | {name}({pdno})\n  잔량: {rmn_qty}주 | 단가: {price_str} | No.{odno}\n"
                        
                    for o in (us_orders or []):
                        name = o.get('prdt_name')
                        pdno = o.get('pdno')
                        odno = o.get('odno')
                        
                        qty_val = o.get('nccs_qty', 0)
                        if not qty_val or str(qty_val).strip() == "": qty_val = 0
                        rmn_qty = api.safe_int(float(qty_val))
                        
                        ord_unpr = 0.0
                        for key in ['ft_ord_unpr3', 'ft_ord_unpr', 'ord_unpr', 'ord_init_unpr', 'ovrs_ord_unpr']:
                            val = o.get(key)
                            if val and str(val).strip():
                                try:
                                    f_val = float(val)
                                    if f_val > 0:
                                        ord_unpr = f_val
                                        break
                                except ValueError:
                                    pass
                                
                        sll_buy_code = o.get('sll_buy_dvsn_cd')
                        sll_buy = "매수" if sll_buy_code == "02" else ("매도" if sll_buy_code == "01" else sll_buy_code)
                        
                        price_str = f"${ord_unpr:,.2f}" if ord_unpr > 0 else "시장가"
                        msg += f"• [해외] {sll_buy} | {name}({pdno})\n  잔량: {rmn_qty}주 | 단가: {price_str} | No.{odno}\n"
        
        if not has_any_orders:
            msg += "\n미체결 주문이 없습니다."
            
        return msg.strip()

    def _cmd_reserves(self, args):
        from modules import trading
        if not args:
            return trading.get_reserved_orders_summary()
            
        subcmd = args[0].lower()
        if subcmd in ["d", "del", "delete", "cancel", "c"]:
            if len(args) < 2:
                return "⚠️ 사용법: /reserves d [예약주문ID]\n(다중: 5, 6 / 전체: 0)"
                
            orders = db_manager.db.get_pending_reserved_orders()
            if not orders:
                return "📭 취소할 예약 주문이 없습니다."
                
            raw_ids = " ".join(args[1:]).replace(',', ' ').split()
            
            if "0" in raw_ids or "all" in [x.lower() for x in raw_ids]:
                cancel_ids = [str(o['id']) for o in orders]
            else:
                cancel_ids = [cid.strip() for cid in raw_ids if cid.strip().isdigit()]
            
            if not cancel_ids:
                return "⚠️ 유효한 예약 주문 ID를 입력해주세요."
                
            canceled_msgs = []
            
            for cid in cancel_ids:
                cancel_id = int(cid)
                target_order = next((o for o in orders if o['id'] == cancel_id), None)
                
                if not target_order:
                    canceled_msgs.append(f"⚠️ 대기 중인 예약 주문(ID: {cancel_id})을 찾을 수 없습니다.")
                    continue
                    
                db_manager.db.update_reserved_order_status(cancel_id, 'CANCELED')
                
                t_type = "매수" if target_order['order_type'] == 'buy' else "매도"
                canceled_msgs.append(f"🗑️ [예약 취소 완료] {target_order['name']} ({t_type}, ID: {cancel_id})")
                
            return "\n".join(canceled_msgs)
        else:
            return "⚠️ 알 수 없는 명령어입니다.\n사용법:\n• 전체 현황: /reserves\n• 특정 취소: /reserves d [ID,ID,...]"

    def _cmd_profit(self, args):
        days = 0
        if args:
            arg = args[0].lower()
            if arg in ["d", "day", "daily", "일간"]: days = 0
            elif arg in ["w", "week", "weekly", "주간"]: days = 7
            elif arg in ["m", "month", "monthly", "월간"]: days = 30
            elif arg.isdigit(): days = int(arg)
        
        period_str = "일간" if days == 0 else ("주간" if days == 7 else ("월간" if days == 30 else f"최근 {days}일"))
        self._send_reply(f"⏳ [{period_str} 손익 조회] 매매 내역 및 자산 데이터를 분석 중입니다. 데이터 양에 따라 시간이 다소 소요될 수 있습니다...")

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        start_dt = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        end_dt = today_str
        
        # [추가] 타겟 계좌 문자열 생성 및 현재 총 자산/기초 자산 조회
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
        if not config.session.is_simulation and not target_cano:
            target_cano = config.session.cano
            acnt = config.session.acnt_prdt_cd
            
        target_account = f"{target_cano}-{acnt}"
        
        current_asset = 0
        sec_buy = 0
        sec_pl = 0
        try:
            with utils.AccountContext(target_cano):
                asset_data = account.get_asset_status_data(target_cano, acnt)
                if asset_data:
                    current_asset = asset_data.get('tot_asset', 0)
                    sec_buy = asset_data.get('sec_buy', 0)
                    sec_pl = asset_data.get('sec_pl', 0)
        except Exception as e:
            logger.error(f"Profit 자산 조회 실패: {e}")

        initial_asset = db_manager.db.get_daily_asset(start_dt, target_account)

        # 시장 대비 성과 계산 (KOSPI)
        kospi_rate = 0.0
        try:
            kospi_df = analysis.get_domestic_index_data("KOSPI")
            if kospi_df is not None and not kospi_df.empty:
                s_dt = start_dt.replace('-', '')
                e_dt = end_dt.replace('-', '')
                
                if 'date' in kospi_df.columns:
                    def to_yyyymmdd(x):
                        if hasattr(x, 'strftime'): return x.strftime('%Y%m%d')
                        return str(x).replace('-', '')[:8]
                    
                    dates = kospi_df['date'].apply(to_yyyymmdd)
                    mask = (dates >= s_dt) & (dates <= e_dt)
                    period_df = kospi_df[mask]
                    if not period_df.empty:
                        first_idx = kospi_df.index.get_loc(period_df.index[0])
                        last_idx = kospi_df.index.get_loc(period_df.index[-1])
                        
                        if first_idx > 0:
                            start_val = kospi_df.iloc[first_idx - 1]['close']
                        else:
                            start_val = kospi_df.iloc[first_idx]['close']
                            
                        end_val = kospi_df.iloc[last_idx]['close']
                        if start_val > 0:
                            kospi_rate = ((end_val - start_val) / start_val) * 100
        except Exception as e:
            logger.error(f"KOSPI 지수 조회 실패: {e}")

        if days == 0:
            title = "📅 [일간 실현 손익]"
        elif days == 7:
            title = "📅 [주간 실현 손익 (최근 7일)]"
        elif days == 30:
            title = "📅 [월간 실현 손익 (최근 30일)]"
        else:
            title = f"📅 [기간별 실현 손익 (최근 {days}일)]"
            
        all_trades = self._get_refined_trades_cached(target_account=None)
        
        trades = []
        for t in all_trades:
            t_date = t.get('time', '')[:10]
            if start_dt <= t_date <= end_dt:
                trades.append(t)
                
        # [추가] 체결된 내역만 통계에 포함 (접수, 취소 등 미체결 제외)
        trades = [r for r in trades if "체결" in r.get('order_status', '')]
            
        stats = self.trader._calculate_statistics(trades)
        
        # 매도(청산) 내역만 필터링하여 손익 합산
        sell_trades = [t for t in trades if "매도" in t.get('type', '') or "sell" in t.get('type', '').lower()]
        
        # [추가] 제한 종목 및 개별 룰 로드
        restricted_stocks = auto_trade.get_restricted_stocks()
        custom_rules = db_manager.db.get_all_stock_strategies()
        rules_map = {r['code']: True for r in custom_rules}
        
        total_sell_amt = 0
        total_buy_amt_for_sell = 0
        gross_profit = 0
        gross_loss = 0
        
        for t in sell_trades:
            qty = int(float(t.get('qty', 0)))
            price = float(t.get('price', 0))
            profit = int(t.get('profit_amt') or 0)
            
            if profit > 0: gross_profit += profit
            else: gross_loss += abs(profit)
            
            sell_amt = qty * price
            buy_amt = sell_amt - profit
            
            total_sell_amt += sell_amt
            total_buy_amt_for_sell += buy_amt
            
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)
            
        msg = f"{title}\n기간: {start_dt} ~ {end_dt}\n\n"
        
        if not stats['sell_trades_exist']:
            msg += "실현된 손익이 없습니다."
        else:
            total_profit = stats['total_profit']
            tot_roi = (total_profit / total_buy_amt_for_sell * 100) if total_buy_amt_for_sell > 0 else 0.0
            
            msg += "[손익 현황]\n"
            if initial_asset and current_asset > 0:
                total_asset_profit = int(current_asset - initial_asset)
                total_asset_roi = (total_asset_profit / initial_asset * 100) if initial_asset > 0 else 0.0
                
                msg += f"총 계좌 시작 자산: {int(initial_asset):,}원\n"
                msg += f"총 계좌 현재 자산: {current_asset:,}원\n"
                msg += f"총 계좌 자산 증감: {total_asset_profit:+,}원 ({total_asset_roi:+.2f}%)\n"
            else:
                msg += "총 계좌 자산 증감: - (데이터 부족)\n"
                
            msg += f"총 실현 손익: {total_profit:+,}원 (매매원금 대비 {tot_roi:+.2f}%)\n"
            unrealized_roi = (sec_pl / sec_buy * 100) if sec_buy > 0 else 0.0
            msg += f"현재 평가 손익: {sec_pl:+,}원 ({unrealized_roi:+.2f}%)\n"
            
            total_invested = total_buy_amt_for_sell + sec_buy
            total_net_profit = total_profit + sec_pl
            strategy_roi = 0.0
            if total_invested > 0:
                strategy_roi = (total_net_profit / total_invested) * 100
            msg += f"현재 전략 손익: {total_net_profit:+,}원 (실현+평가 손익 {strategy_roi:+.2f}%)\n"

            msg += "\n[시장 대비 성과]\n"
            msg += f"코스피 지수: {kospi_rate:+.2f}%\n"
            
            if total_invested > 0:
                alpha = strategy_roi - kospi_rate
                # [수정] 초과/부진 여부를 명시적으로 표시하여 가독성 향상
                if alpha > 0:
                    msg += f"시장 대비 초과 수익 (Outperform): +{alpha:.2f}%\n"
                else:
                    msg += f"시장 대비 성과 (Underperform): {alpha:.2f}%\n"
            else:
                msg += "시장 대비 성과: -\n"
                
            msg += "\n[매매 요약]\n"
            msg += f"총 매입금액: {int(total_buy_amt_for_sell):,}원\n"
            msg += f"총 매도금액: {int(total_sell_amt):,}원\n"
            
            trade_profit = total_sell_amt - total_buy_amt_for_sell
            trade_roi = (trade_profit / total_buy_amt_for_sell * 100) if total_buy_amt_for_sell > 0 else 0.0
            msg += f"총 매매손익: {int(trade_profit):+,}원 ({trade_roi:+.2f}%)\n"
            
            pf_str = f"{profit_factor:.2f}" if profit_factor != 99.9 else "Inf"
            msg += f"평균 손익비: {pf_str}\n"

        return msg.strip()

    def _cmd_history(self, args):
        days = 0 # 기본값: 당일 조회
        if args:
            arg = args[0].lower()
            if arg in ["d", "day", "daily", "일간"]: days = 0
            elif arg in ["w", "week", "weekly", "주간"]: days = 7
            elif arg in ["m", "month", "monthly", "월간"]: days = 30
            elif arg.isdigit(): days = int(arg)
        return self._get_trade_history(days)

    def _cmd_log(self, args):
        return self.trader.get_recent_logs()

    def _cmd_preset(self, args):
        if not args:
            return self._get_preset_status()

        target = args[0].lower()
        preset_type = None
        if target in ['bull', 'b']:
            preset_type = 'bull'
        elif target in ['bear', 'r']:
            preset_type = 'bear'
        elif target in ['sideways', 's']:
            preset_type = 'sideways'
        elif target in ['default', 'd', 'reset']:
            preset_type = 'default'
            
        if not preset_type:
            return "⚠️ 알 수 없는 프리셋입니다. (b:강세, r:약세, s:횡보, d:초기화 중 선택)"
            
        from modules import settings
        msg = settings.apply_strategy_preset(preset_type, interactive=False)
        return msg

    def _get_preset_status(self):
        """현재 적용 중인 전략 프리셋 및 기본 설정과의 차이 조회 (/preset 무옵션)"""
        from modules import settings
        try:
            preset = settings.check_and_update_active_preset()
        except Exception:
            preset = getattr(config.settings, 'ACTIVE_PRESET', 'default')

        preset_display_map = {
            "default": ("🟢", "기본 (Default)"),
            "bull": ("🔴", "강세장 (Bull)"),
            "bear": ("🔵", "약세장 (Bear)"),
            "sideways": ("🟡", "횡보장 (Sideways)"),
            "custom": ("⚪", "커스텀 (Custom)"),
        }
        p_emoji, p_name = preset_display_map.get(preset, ("⚪", preset))
        msg = f"[전략 프리셋: {p_emoji} {p_name}]\n"

        lines = []
        try:
            if preset == "custom":
                # 커스텀: 현재 설정 전체를 기본값과 비교
                changed_items = config.get_custom_settings()
                for key, info in changed_items.items():
                    dict_key = info.get("key", key)
                    desc = getattr(config, 'CONFIG_DESCRIPTIONS', {}).get(dict_key, dict_key)
                    lines.append(f"• {desc}: {info['default']} ➔ {info['current']}")
            elif preset != "default":
                # 강세/약세/횡보: 프리셋 정의값을 기본 프리셋과 비교
                default_vals = settings.get_preset_values("default")
                preset_vals = settings.get_preset_values(preset)
                for k, v in preset_vals.items():
                    default_v = default_vals.get(k)
                    if v != default_v:
                        desc = getattr(config, 'CONFIG_DESCRIPTIONS', {}).get(k, k)
                        lines.append(f"• {desc}: {default_v} ➔ {v}")
        except Exception as e:
            logger.debug(f"Preset diff error: {e}")

        if preset == "default":
            msg += "\n기본 설정 그대로 운용 중입니다."
        elif lines:
            max_items = 30
            if len(lines) > max_items:
                rest = len(lines) - max_items
                lines = lines[:max_items] + [f"• ... 외 {rest}건 (설정 메뉴에서 확인)"]
            msg += "\n기본 설정과 다른 항목 (기본값 ➔ 현재값)\n"
            msg += "\n".join(lines)
        else:
            msg += "\n기본 설정과 다른 항목이 없습니다."

        msg += "\n\n프리셋 변경: /preset b(강세) · r(약세) · s(횡보) · d(초기화)"
        return msg

    def _cmd_balance(self, args):
        self._send_reply("⏳ [계좌 잔고 조회] 자산 및 잔고 정보를 수집 중입니다. 잠시만 기다려주세요...")
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
        
        if not config.session.is_simulation and not target_cano:
            target_cano = config.session.cano
            acnt = config.session.acnt_prdt_cd

        try:
            with utils.AccountContext(target_cano):
                # account 모듈의 통합 데이터 조회 함수 사용
                data = account.get_asset_status_data(target_cano, acnt)
                
                if not data:
                     return "⚠️ 자산 조회 실패 (데이터 없음)"
                
                # 수익률 계산
                roi = 0.0
                if data['sec_buy'] > 0:
                    roi = (data['sec_pl'] / data['sec_buy']) * 100
                
                # 총 예수금 계산
                total_deposit = data['dep_dom'] + data['dep_ovs']

                msg = f"💰 [계좌 자산 현황]\n"
                msg += f"총 평가금액: {data['tot_asset']:,}원\n"
                msg += f"총 예수금(D+0): {total_deposit:,}원\n"
                msg += f"  • 원화: {data['dep_dom']:,}원\n"
                
                msg += f"    └ D+1 (익일): {data['d1_dep']:,}원\n"
                msg += f"    └ D+2 (가수도): {data['d2_dep']:,}원\n"

                next_plus = data.get('next_day_plus', 0)
                next_minus = data.get('next_day_minus', 0)
                if next_plus > 0:
                    msg += f"    └ 익일결재(+): {next_plus:,}원\n"
                if next_minus > 0:
                    msg += f"    └ 익일결재(-): {next_minus:,}원\n"

                msg += f"  • 외화: {data['dep_ovs']:,}원\n"
                
                # [추가] 주문가능금액 표시
                ord_psbl = data.get('order_possible')
                if ord_psbl is None:
                    try:
                        bal = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        if bal: ord_psbl = bal.get('order_possible', 0)
                    except Exception as e:
                        logger.debug(f"ord_psbl fallback balance fetch error: {e}")
                
                if ord_psbl is None: ord_psbl = 0
                msg += f"주문가능금액: {ord_psbl:,}원\n"
                msg += f"출금가능금액: {data['withdraw']:,}원\n\n"
                
                msg += f"유가증권매입: {data['sec_buy']:,}원\n"
                msg += f"유가증권평가: {data['sec_eval']:,}원\n"
                if data.get('ovrs_eval_krw', 0) > 0:
                    msg += f"  └ 해외주식(원화): {data['ovrs_eval_krw']:,}원\n"

                msg += f"평가손익(보유): {data['sec_pl']:+,}원 ({roi:.2f}%)\n\n"
                
                msg += f"금일매수: {data['buy_today']:,}원\n"
                msg += f"금일매도: {data['sell_today']:,}원\n"
                msg += f"금일비용: {data['total_cost']:,}원\n"
                
                msg += f"실현손익: {data['realized_pl']:+,}원"

                return msg

        except Exception as e:
            return f"⚠️ 자산 조회 중 오류 발생: {str(e)}"

    def _cmd_holdings(self, args):
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
        
        if not config.session.is_simulation and not target_cano:
            target_cano = config.session.cano
            acnt = config.session.acnt_prdt_cd

        try:
            with utils.AccountContext(target_cano):
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
            
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []
            
            # [추가] 제한 종목 및 개별 룰 로드
            restricted_stocks = auto_trade.get_restricted_stocks()
            custom_rules = db_manager.db.get_all_stock_strategies()
            rules_map = {r['code']: True for r in custom_rules}

            if not valid_holdings:
                return "📋 [보유 종목] 없음"
            
            msg = f"📋 [보유 종목 현황] ({len(valid_holdings)}종목)\n"
            
            calc_total_pchs = 0 # [추가] 총 매입금액 직접 계산용 (API 0일 경우 대비)
            
            for item in valid_holdings:
                name = item['prdt_name']
                code = item['pdno']
                qty = int(item['hldg_qty'])
                cur_price = int(item['prpr'])
                buy_price = float(item['pchs_avg_pric'])
                eval_amt = int(item['evlu_amt'])
                profit = int(item['evlu_pfls_amt'])
                rate = float(item['evlu_pfls_rt'])
                
                calc_total_pchs += int(qty * buy_price)
                
                name_display = name
                if code in restricted_stocks: name_display += "-"
                if code in rules_map: name_display += "+"
                
                msg += f"\n{name_display} ({qty}주)\n   현재: {cur_price:,}원 | 평단: {buy_price:,.0f}원\n   평가: {eval_amt:,}원 | 손익: {profit:+,}원 ({rate:+.2f}%)"
            
            # [추가] 총 평가금액 및 손익 요약
            if summary and len(summary) > 0:
                s_data = summary[0]
                
                # 총 주식 평가금액
                stock_evlu = api.safe_int(s_data.get('scts_evlu_amt'))
                if stock_evlu == 0 and valid_holdings:
                    stock_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                
                # 총 평가손익
                tot_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                if tot_profit == 0 and valid_holdings:
                    tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                
                # [추가] 수익률 계산
                tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
                if tot_pchs == 0: tot_pchs = calc_total_pchs
                
                total_rate = 0.0
                if tot_pchs > 0:
                    total_rate = (tot_profit / tot_pchs) * 100
                
                msg += f"\n\n 총 평가금액: {stock_evlu:,}원"
                msg += f"\n 총 평가손익: {tot_profit:+,}원 ({total_rate:+.2f}%)"

            return msg
        except Exception as e:
            return f"⚠️ 보유 종목 조회 중 오류 발생: {str(e)}"

    # --- 내부 로직 메서드 ---
    def _get_refined_trades_cached(self, target_account=None):
        """DB에서 전체 거래 내역을 조회 및 정제(Refine)한 결과를 60초간 메모리에 캐싱"""
        now = time.time()
        is_sim = config.session.is_simulation
        cache_key = f"{is_sim}_{target_account}"
        
        with self._trade_cache_lock:
            cached = self._trade_cache.get(cache_key)
            if cached and now - cached['time'] < 60: # 60초 이내 캐시 반환 (속도 대폭 향상)
                return cached['data']
                
            raw_trades = db_manager.db.get_trades(limit=None, is_sim=is_sim, account=target_account)
            
            trades = []
            for r in reversed(raw_trades):
                type_str = r.get('type', '')
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                parsed_r = dict(r)
                parsed_r['type'] = simple_type
                trades.append(parsed_r)

            if hasattr(self.trader, '_refine_trade_records'):
                trades = self.trader._refine_trade_records(trades)
                
            if trades:
                # 통계 계산 최적화를 위해 시간순(과거->최신)으로 기본 정렬
                trades.sort(key=lambda x: x.get('time', ''))
                
            self._trade_cache[cache_key] = {'time': now, 'data': trades}
            return trades

    def _send_reply(self, text, reply_markup=None, is_urgent=False, sync=False):
        if reply_markup is None:
            reply_markup = self._get_default_keyboard()
        api.send_telegram_message(text, reply_markup=reply_markup, is_urgent=is_urgent, sync=sync)

    def _get_default_keyboard(self):
        """하단 고정 메뉴 버튼 (Reply Keyboard) 구성"""
        # [추가] 봇 명령어 수신이 비활성화(--no-bot)된 인스턴스인 경우,
        # 다른 메인 인스턴스의 키보드 상태를 덮어씌우지 않도록 키보드 마크업을 반환하지 않습니다.
        if not getattr(config, 'ENABLE_TELEGRAM', True):
            return None
            
        try:
            from modules import settings
            preset = settings.check_and_update_active_preset()
        except Exception:
            preset = getattr(config, 'ACTIVE_PRESET', 'default')
            
        if preset == 'bull': emoji = "🔴"
        elif preset == 'bear': emoji = "🔵"
        elif preset == 'sideways': emoji = "🟡"
        elif preset == 'default': emoji = "🟢"
        elif preset == 'custom': emoji = "⚪"
        else: emoji = "⚪"
            
        toggle_btn = {"text": "🛑 거래 정지"} if self.trader.is_running else {"text": "▶️ 거래 시작"}
        return {
            "keyboard": [
                [{"text": "❓ 도움말"}, {"text": "📈 시장 지수"}, {"text": "💰 계좌 잔고"}],
                [{"text": "📝 관심 종목"}, {"text": "💼 보유 종목"}, {"text": "⏳ 예약 현황"}],
                [{"text": "📅 일간 손익"}, {"text": "📜 주간 거래"}, {"text": "📊 월간 성과"}],
                [{"text": f"{emoji} 상태 요약"}, toggle_btn]
            ],
            "resize_keyboard": True
        }

    def _get_market_status(self, target_group_names=None):
        """시장 지수(KOSPI/KOSDAQ/원자재/환율) 현황 조회"""
        msg = "📊 [시장 지수 현황]\n"
        
        # [수정] 통합 지수 리스트 사용
        targets = market.ALL_INDICES

        # [추가] KIS 실전 전용 지수(코스피200선물·V코스피200)는 모의(1)/토스(3) 모드에서 제외
        #  (모의서버는 해당 TR 미지원/불안정, 토스는 대체 소스 없음 — 화면 출력 정책과 동일)
        if config.session.is_toss or config.session.is_simulation:
            targets = [(n, c) for n, c in targets if n not in ("코스피200선물", "V코스피200")]

        # 구분선(공백라인)을 넣을 지수명 리스트
        section_keys = ["나스닥 선물", "Japan - 닛케이", "SOX (반도체)", "달러인덱스", "미국채 5년물 금리", "금", "비트코인"]

        # [추가] 국내 지수 매핑 (analysis.get_domestic_index_data 호출용)
        domestic_map = {
            "코스피": "KOSPI", "코스피200": "KOSPI200",
            "코스닥": "KOSDAQ", "코스닥150": "KOSDAQ150",
            "V코스피200": "VKOSPI"
        }

        # [추가] 다중 그룹 필터링 로직
        if target_group_names:
            if isinstance(target_group_names, str):
                target_group_names = [target_group_names]
                
            group_indices = set()
            found_group_labels = []
            
            for g_info in config.INDICES_GROUPS.values():
                if g_info['name'] in target_group_names:
                    group_indices.update(g_info['indices'])
                    label = g_info['name'].split(" (")[0]
                    if label not in found_group_labels:
                        found_group_labels.append(label)
            
            if group_indices:
                targets = [(name, code) for name, code in targets if name in group_indices]
                msg = f"📊 [{' + '.join(found_group_labels)} 현황]\n"
            else:
                return f"⚠️ 지정한 그룹을 찾을 수 없습니다."

        regime_ma_period = config.MARKET_REGIME_PARAMS.get('REGIME_MA_PERIOD', 20)
        
        for name, code in targets:
            if name in section_keys:
                msg += "\n"
            
            try:
                current = None
                prev = None

                if name == "코스피200선물":
                    # [추가] 주간(F)/야간(CM) 세션 자동 전환 — 화면(메뉴 1)과 동일하게 시세 TR 사용
                    #  (야간 등락률 = 주간 종가 대비 KIS 제공값. 모드 1/3은 위에서 이미 제외됨)
                    fut_div = "CM" if market._k200_night_session() else "F"
                    fut_iscd = api.get_k200_futures_front_code()
                    fut_q = api.get_k200_futures_quote(fut_div, fut_iscd) if fut_iscd else None
                    if fut_q:
                        current = fut_q['current']
                        prev = current - fut_q['diff']
                        name = f"{name} {fut_div}"
                elif name in domestic_map:
                    # 국내 지수는 기존 방식대로 데이터 조회
                    df = analysis.get_domestic_index_data(domestic_map[name])
                    if df is not None and not df.empty:
                        current = df.iloc[-1]['close']
                        prev = df.iloc[-2]['close'] if len(df) > 1 else current
                else:
                    # 해외 지수는 무거운 차트 데이터 조회를 생략하고 즉시 fast_info 우선 사용
                    try:
                        fi = api.get_yf_fast_info(code)
                        if fi and fi.get('last_price'):
                            current = float(fi['last_price'])
                            prev = float(fi.get('regular_market_previous_close', current))
                            
                            # 금리 프록시 로직
                            fut_mapping = {
                                "미국채 5년물 금리": {"ticker": "ZF=F", "duration": 4.5},
                                "미국채 10년물 금리": {"ticker": "ZN=F", "duration": 7.5},
                                "미국채 30년물 금리": {"ticker": "ZB=F", "duration": 16.0}
                            }
                            if name in fut_mapping:
                                fut_info = fut_mapping[name]
                                fut_fi = api.get_yf_fast_info(fut_info["ticker"])
                                if fut_fi and fut_fi.get('last_price') and fut_fi.get('regular_market_previous_close'):
                                    f_curr = float(fut_fi['last_price'])
                                    f_prev = float(fut_fi['regular_market_previous_close'])
                                    if f_prev > 0:
                                        utc_hour = datetime.now(timezone.utc).hour
                                        if utc_hour < 13 or utc_hour >= 21:
                                            f_rate = (f_curr - f_prev) / f_prev * 100
                                            est_yield = current - (f_rate / fut_info["duration"])
                                            prev = current
                                            current = est_yield
                                            name = f"{name}(선물적용)"
                    except Exception: pass
                    
                    # fast_info 실패 시에만 fallback으로 차트 조회 수행
                    if current is None:
                        df = api.get_chart_data(code, is_overseas=True)
                        if df is not None and not df.empty:
                            current = df.iloc[-1]['close']
                            prev = df.iloc[-2]['close'] if len(df) > 1 else current
                
                if current is None or prev is None:
                    msg += f"\n• {name}: 데이터 조회 실패"
                    continue
                
                diff = current - prev
                rate = (diff / prev) * 100 if prev > 0 else 0
                
                if "미국채" in name and "선물" not in name:
                    val_fmt = f"{current:,.2f}%"
                    msg += f"\n• {name} {val_fmt} ({diff:+.2f}p)"
                else:
                    val_fmt = f"{current:,.2f}"
                    if code == "KRW=X": val_fmt += "원"
                    msg += f"\n• {name} {val_fmt} ({rate:+.2f}%)"
                
            except Exception as e:
                msg += f"\n• {name}: 오류"
        
        return msg

    def _resolve_stock(self, keyword):
        """종목명/코드를 입력받아 (코드, 이름, 해외여부)를 반환하는 헬퍼 함수"""
        code = None
        name = None
        is_overseas = False
        
        # 0. 시장 지수 검색 (종목보다 우선)
        from modules import market
        domestic_map = {
            "코스피": "KOSPI", "코스피200": "KOSPI200",
            "코스닥": "KOSDAQ", "코스닥150": "KOSDAQ150"
        }
        if keyword.upper() in domestic_map.values():
            name = next((k for k, v in domestic_map.items() if v == keyword.upper()), keyword)
            return keyword.upper(), name, False
            
        for n, c in market.ALL_INDICES:
            if keyword.lower() == n.lower() or keyword.upper() == c.upper():
                if n in domestic_map:
                    return domestic_map[n], n, False
                return c, n, True

        # 1. config에 등록된 종목에서 검색
        all_stocks = config.session.stock_data.get("stocks_kr", []) + config.session.stock_data.get("etfs_kr", [])
        for item in all_stocks:
            if keyword == item['code'] or keyword == item['name']:
                return item['code'], item['name'], False
        
        all_us = config.session.stock_data.get("stocks_us", []) + config.session.stock_data.get("etfs_us", [])
        for item in all_us:
            if keyword.upper() == item['code'] or keyword.lower() == item['name'].lower():
                return item['code'], item['name'], True
        
        # 2. 등록된 종목이 아니면 입력값을 코드로 간주하거나 이름으로 검색 시도
        if len(keyword) == 6 and keyword[0].isdigit() and keyword.isalnum():
            code = keyword
            name = api.get_stock_name_by_code(code, False) or keyword
            is_overseas = False
        # 영문/숫자/특수문자(.-)로 구성된 경우 해외 티커로 간주 (한글 제외)
        elif all(ord(c) < 128 for c in keyword):
            code = keyword.upper()
            name = api.get_stock_name_by_code(code, True) or keyword
            is_overseas = True
        
        return code, name, is_overseas

    def _analyze_stock(self, keyword):
        """특정 종목 기술적 분석 및 진단"""
        code, name, is_overseas = self._resolve_stock(keyword)
        
        if not code:
            return f"⚠️ '{keyword}' 종목을 찾을 수 없습니다.\n관심 종목에 등록된 종목명이나 코드를 입력해주세요."

        # [추가] 개별 룰 존재 여부 확인
        custom_rule = db_manager.db.get_stock_strategy(code)
        rule_tag = " [개별]" if custom_rule else ""
        
        # [추가] 제한 종목 로드
        restricted_stocks = auto_trade.get_restricted_stocks()
        
        name_display = name
        if code in restricted_stocks: name_display += "-"
        if custom_rule: name_display += "+"

        # [수정] 기본값 설정
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        weights = config.SCORING_WEIGHTS
        
        # [수정] 개별 룰 적용
        if custom_rule:
            buy_score = custom_rule['buy_score']
            buy_rsi = custom_rule['buy_rsi']
            if custom_rule.get('weights'):
                try:
                    w_data = custom_rule['weights']
                    if isinstance(w_data, str): weights = json.loads(w_data)
                    elif isinstance(w_data, dict): weights = w_data
                except Exception as e:
                    logger.debug(f"Weights parsing error: {e}")

        try:
            # 3. 데이터 조회 및 분석
            df = api.get_chart_data(code, is_overseas)
            if df is None or df.empty:
                return f"⚠️ {name}({code}) 차트 데이터를 불러올 수 없습니다."
            
            # [추가] 실시간 현재가 조회 및 차트 당일 고가/저가 실시간 갱신 (점수 불일치 방지)
            try:
                rt_price = api.get_current_price(code, is_overseas=is_overseas)
                indicators.apply_realtime_price(df, rt_price)
            except Exception: pass
            
            ind = indicators.calculate_indicators(df)
            current_price = float(df.iloc[-1]['close'])
            
            # 52주 최고/최저 및 위치 계산 (최근 250일 데이터 기준)
            high_52 = df['high'].max()
            low_52 = df['low'].min()
            pos_52 = 0.0
            if high_52 > low_52:
                pos_52 = (current_price - low_52) / (high_52 - low_52) * 100
            
            # 전일 RSI (상태 분류용) — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
            prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None
                
            # 52주 위치 계산 (슈퍼 모멘텀 판정용)
            w52_pos = 0.0
            if len(df) > 0:
                recent_df = df.tail(250)
                h52 = recent_df['high'].max()
                l52 = recent_df['low'].min()
                if h52 > l52:
                    w52_pos = (current_price - l52) / (h52 - l52) * 100

            # [수정] 적응형 임계값 적용
            score_adj = 0.0
            regime_msg = ""
            if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True) and not is_overseas:
                # 시장 구분 확인
                market_type = "KOSPI"
                try:
                    cp = api.get_current_price_data(code, False)
                    if cp.get('rt_cd') == '0' and "코스닥" in cp['output'].get('rprs_mrkt_kor_name', ''):
                        market_type = "KOSDAQ"
                except Exception as e:
                    logger.debug(f"Market type fetch error: {e}")
                
                regime, score_adj = analysis.get_market_regime(market_type)
                if score_adj != 0:
                    if not custom_rule: # [수정] 개별 룰이 없을 때만 보정 적용
                        buy_score += score_adj
                        regime_msg = f" [시장국면 보정 {score_adj:+.1f}점]"
                        
            sm_flag, _ = analysis.check_smart_money_turnaround(code, is_overseas)

            thresholds = {
                "BUY_SCORE": buy_score,
                "BUY_RSI_MAX": buy_rsi,
                "WEIGHTS": weights
            }

            state, _, reason = analysis.classify_stock_state(
                df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
            )
            score, _ = analysis.calculate_score(
                df=df, ind=ind, weights=weights, smart_money=sm_flag
            )
            score = round(score, 1)
            
            # 4. 메시지 구성
            rsi_str = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
            adx_str = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
            cci_str = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
            
            price_fmt = f"${current_price:,.2f}" if is_overseas else f"{int(current_price):,}원"
            h52_fmt = f"${high_52:,.2f}" if is_overseas else f"{int(high_52):,}원"
            l52_fmt = f"${low_52:,.2f}" if is_overseas else f"{int(low_52):,}원"
            
            # SAR 상태
            sar_state = "-"
            if ind['psar'] is not None:
                sar_state = "상승" if ind['psar'] < current_price else "하락"

            # OBV 상태
            obv_trend = ind.get('obv_trend')
            vol_sum = df['volume'].tail(5).sum() if df is not None and 'volume' in df.columns else 0
            if vol_sum == 0 or obv_trend is None:
                obv_state = "-"
            else:
                obv_state = "상승" if obv_trend else "하락"

            # MACD 상태
            macd_val = ind.get('macd')
            sig_val = ind.get('macd_signal')
            macd_state = "-"
            if macd_val is not None and sig_val is not None:
                macd_state = "골든" if macd_val > sig_val else "데드"

            # DMI 상태
            plus_di = ind.get('plus_di')
            minus_di = ind.get('minus_di')
            dmi_str = "-"
            if plus_di is not None and minus_di is not None:
                if plus_di > minus_di:
                    dmi_str = f"+DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
                elif minus_di > plus_di:
                    dmi_str = f"-DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
                else:
                    dmi_str = f"중립 ({plus_di:.1f} / {minus_di:.1f})"

            # 이평선 상태
            ema_state = "혼조/역배열"
            if ind.get('ema_20') is not None and ind.get('ema_60') is not None and ind.get('ema_120') is not None:
                if ind['ema_20'] > ind['ema_60'] > ind['ema_120']: ema_state = "정배열"
                elif ind['ema_20'] < ind['ema_60'] < ind['ema_120']: ema_state = "역배열"
            
            def _fmt_ema(v): return f"{int(v):,}" if not is_overseas else f"${v:,.2f}"
            e5 = _fmt_ema(ind.get('ema_5')) if ind.get('ema_5') is not None else "-"
            e20 = _fmt_ema(ind.get('ema_20')) if ind.get('ema_20') is not None else "-"
            e60 = _fmt_ema(ind.get('ema_60')) if ind.get('ema_60') is not None else "-"
            e120 = _fmt_ema(ind.get('ema_120')) if ind.get('ema_120') is not None else "-"

            # [수정] 매수/보유 판단 로직 (보정된 기준 사용)
            buy_score_limit = buy_score
            buy_rsi_limit = buy_rsi
            
            # [추가] 실시간 체결강도 및 매도잔량비 조회 (자동매매와 조건 일치)
            vol_strength = None
            ask_bid_ratio = None
            if not is_overseas:
                try:
                    vol_strength = api.get_realtime_vol_strength(code)
                    ask_bid_ratio = api.get_ask_bid_ratio(code, False)
                except Exception: pass
            
            is_buy_score = score >= buy_score_limit
            is_buy_rsi = (ind['rsi'] is not None) and (ind['rsi'] < buy_rsi_limit)
            is_safe_state = state not in ["매도", "주의"]
            
            # 자동매매와 동일한 수급 필터링 기준 산출
            min_vol = thresholds.get("BUY_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
            if state == "역매수":
                min_vol = thresholds.get("MR_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
            
            min_ask_bid_ratio = thresholds.get("BUY_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
            auto_adjust = thresholds.get("AUTO_ADJUST_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True)) if thresholds else config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True)
            if auto_adjust and min_ask_bid_ratio > 0 and min_vol > 0:
                min_ask_bid_ratio = round(min_ask_bid_ratio * (min_vol / 100.0), 2)
                
            is_vol_ok = True
            vol_reason = ""
            if vol_strength is not None:
                if vol_strength < min_vol:
                    is_vol_ok = False
                    vol_reason = f"체결미달({vol_strength:.1f}%<{min_vol}%)"
                elif ask_bid_ratio is not None and min_ask_bid_ratio > 0:
                    if ask_bid_ratio < min_ask_bid_ratio:
                        is_vol_ok = False
                        vol_reason = f"매도잔량부족(비율 {ask_bid_ratio:.2f}<{min_ask_bid_ratio})"
            
            if is_buy_score and is_buy_rsi and is_safe_state and is_vol_ok:
                buy_result = f"🔴 매수 가능 (조건 충족{regime_msg})"
            else:
                reasons = []
                if not is_safe_state: reasons.append(f"상태:{state}")
                if not is_buy_score: reasons.append(f"점수미달({score}<{buy_score_limit}{regime_msg})")
                if not is_buy_rsi:
                    rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "N/A"
                    reasons.append(f"RSI과열({rsi_val}>={buy_rsi_limit})")
                if not is_vol_ok:
                    reasons.append(vol_reason)
                buy_result = f"🔵 매수 불가 ({', '.join(reasons)})"

            # [추가] TradingView 종합 기술적 평가 (Technical Rating) 조회
            tv_rating_str = "조회 불가"
            try:
                import pandas as pd # [추가] pd 참조 누락 수정
                from tradingview_screener import Query, Column
                market_str = 'america' if is_overseas else 'korea'
                _, tv_df = Query().set_markets(market_str).select('Recommend.All').where(Column('name') == code).limit(1).get_scanner_data()
                if tv_df is not None and not tv_df.empty:
                    rating_val = tv_df.iloc[0].get('Recommend.All')
                    if pd.notna(rating_val):
                        if rating_val >= 0.5: tv_rating_str = f"🔴 Strong Buy ({rating_val:+.2f})"
                        elif rating_val >= 0.1: tv_rating_str = f"🔴 Buy ({rating_val:+.2f})"
                        elif rating_val > -0.1: tv_rating_str = f"⚪️ Neutral ({rating_val:+.2f})"
                        elif rating_val > -0.5: tv_rating_str = f"🔵 Sell ({rating_val:+.2f})"
                        else: tv_rating_str = f"🔵 Strong Sell ({rating_val:+.2f})"
            except Exception as e:
                logger.debug(f"TV rating fetch error: {e}")

            sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]
            # [추세추종] 점수 하락 매도는 추세 구조 훼손(현재가<60일선) 동반 시에만 발동 (실매매 analyze_sell과 동일 기준)
            ema60_now = ind.get('ema_60')
            structure_broken = ema60_now is None or current_price < ema60_now
            is_sell_signal = (state == "매도") or (score < sell_score_limit and structure_broken)

            if is_sell_signal:
                reasons = []
                if state == "매도": reasons.append(f"상태:{state}")
                if score < sell_score_limit and structure_broken: reasons.append(f"점수하락({score}<{sell_score_limit})+60일선 이탈")
                sell_result = f"🔵 매도 ({', '.join(reasons)})"
            elif score < sell_score_limit:
                sell_result = "🟢 보유 (점수 미달이나 60일선 위 추세 구조 유지)"
            else:
                sell_result = "🟢 보유 (추세유지)"

            state_emoji_map = {"매수": "🔴", "강매수": "💥", "역매수": "🟣", "상승": "🟠", "관심": "🟢", "관망": "⚪", "주의": "🟡", "매도": "🔵"}
            state_emoji = state_emoji_map.get(state, "")

            msg = f"🔍 [종목 진단{rule_tag}] {name_display}({code})\n"
            msg += f"현재가: {price_fmt}\n"
            msg += f"52주: {l52_fmt} ~ {h52_fmt} ({pos_52:.1f}%)\n"
            msg += f"종합 점수: {score}점 / 10점\n"
            msg += f"상태 분류: {state_emoji} {state} ({reason})\n"
            msg += f"매수 판단: {buy_result}\n"
            msg += f"보유 판단: {sell_result}\n"
            msg += f"TradingView 의견: {tv_rating_str}\n"
            msg += f"\n[주요 지표]\n"
            msg += f"• EMA: {ema_state}\n"
            msg += f"   5: {e5}\n"
            msg += f"   20: {e20}\n"
            msg += f"   60: {e60}\n"
            msg += f"   120: {e120}\n"
            msg += f"• SAR: {sar_state}\n"
            msg += f"• MACD: {macd_state}\n"
            msg += f"• OBV: {obv_state}\n"
            msg += f"• RSI: {rsi_str}\n"
            msg += f"• CCI: {cci_str}\n"
            msg += f"• ADX: {adx_str}\n"
            msg += f"• DMI: {dmi_str}"
            if vol_strength is not None:
                msg += f"\n• 체결강도: {vol_strength:.1f}%"
            if ask_bid_ratio is not None and ask_bid_ratio != 99.9:
                msg += f"\n• 매도/매수잔량비: {ask_bid_ratio:.2f}배"
            
            return msg
        except Exception as e:
            return f"⚠️ 분석 중 오류 발생: {str(e)}"
            
    def _send_chart(self, keyword, period_type='hourly', period_label='시봉'):
        """차트 이미지를 생성하여 텔레그램으로 전송"""
        code, name, is_overseas = self._resolve_stock(keyword)
        
        if not code:
            self._send_reply(f"⚠️ '{keyword}' 종목을 찾을 수 없습니다.")
            return
            
        # [추가] 제한 종목 및 개별 룰 로드
        restricted_stocks = auto_trade.get_restricted_stocks()
        custom_rule = db_manager.db.get_stock_strategy(code)
        
        name_display = name
        if code in restricted_stocks: name_display += "-"
        if custom_rule: name_display += "+"

        try:
            self._send_reply(f"⏳ {name_display}({code}) {period_label} 차트 생성 중...")
            
            # 차트 생성 (config.CHART_DIR에 저장됨)
            chart.generate_visual_chart(code, name, is_overseas, open_file=False, dpi=100, quiet=True, period_type=period_type)
            
            # 파일 경로 추론
            safe_code = re.sub(r'[=\-\.\^]', '', code)
            filename = f"analysis_{safe_code}_{period_type}.png"
            file_path = os.path.join(config.CHART_DIR, filename)
            
            caption = f"📊 {name_display}({code}) 분석 차트 ({period_label})"
            
            # api.send_telegram_photo 사용
            if api.send_telegram_photo(file_path, caption):
                logger.info(f"[Telegram] 차트 전송 성공: {filename}")
            else:
                logger.error(f"[Telegram] 차트 전송 실패: {filename}")
                self._send_reply("⚠️ 차트 전송에 실패했습니다. (로그 확인)")
                
        except Exception as e:
            self._send_reply(f"⚠️ 차트 전송 중 오류 발생: {str(e)}")
            logger.error(f"[Telegram] 차트 전송 중 예외 발생: {e}")

    def _get_monitoring_list(self):
        """현재 감시 중인 종목 리스트 반환"""
        msg = "📋 [현재 감시 종목 리스트]\n"
        
        # [추가] 제한 종목 및 개별 룰 로드
        restricted_stocks = auto_trade.get_restricted_stocks()
        custom_rules = db_manager.db.get_all_stock_strategies()
        rules_map = {r['code']: r for r in custom_rules}
        
        groups = {
            "stocks_kr": "🇰🇷 국내주식",
            "etfs_kr": "🇰🇷 국내ETF"
        }
        
        trader = auto_trade.AutoTrader()
        is_running = trader.is_running
        state_cache = trader.stock_state_cache if is_running else {}

        has_stock = False
        for key, label in groups.items():
            stocks = config.session.stock_data.get(key, [])
            if stocks:
                has_stock = True
                msg += f"\n{label}:"
                for s in stocks:
                    code = s['code']
                    name = s['name']
                    
                    # [추가] 상태 표시 (제한: -, 개별룰: +)
                    if code in restricted_stocks:
                        name += "-"
                    
                    rule = rules_map.get(code)
                    if rule:
                        name += "+"
                        
                    state_display = ""
                    if is_running:
                        state_str = state_cache.get(code)
                        if state_str:
                            state_emoji_map = {"매수": "🔴", "강매수": "💥", "역매수": "🟣", "상승": "🟠", "관심": "🟢", "관망": "⚪", "주의": "🟡", "매도": "🔵"}
                            emoji = state_emoji_map.get(state_str, "❓")
                            state_display = f" {emoji} {state_str}"
                    
                    msg += f"\n - {name} ({code}){state_display}\n   /signal_{code}  /analyze_{code}  /chart_{code}"
                msg += "\n"
        
        if not has_stock:
            msg += "\n등록된 관심 종목이 없습니다."
            
        if not is_running:
            msg += "\n⚠️ 시스템 트레이딩이 중지되어 있어 현재 상태가 표시되지 않습니다. (상태 확인: /signal_종목코드)"
            
        return msg

    def _get_strategy_config(self):
        """현재 매매 전략 설정값 반환"""
        # 시스템 상태 및 계좌 정보
        status_icon = "🟢" if self.trader.is_running else "🔴"
        status_text = "실행 중" if self.trader.is_running else "중지됨"
        
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        # [수정] 토스/모의는 단일계좌(시스템 트레이딩 계좌 = 기본 계좌). is_toss를 먼저 분기.
        if config.session.is_toss:
            acc_label = "토스"
        elif config.session.is_simulation:
            acc_label = "모의"
        elif config.session.auto_cano:
            acc_label = "자동"
        else:
            acc_label = "실전"

        msg = f"{status_icon} [시스템 상태: {status_text}]\n"
        msg += f"• 운용 계좌: {target_cano} ({acc_label})\n\n"
        msg += "⚙️ [매매 전략 설정]\n"
        
        # 매수 관련
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        buy_ask_bid = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
        auto_adj = "ON" if config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True) else "OFF"
        msg += f"\n[매수 조건]\n"
        msg += f"• 종합 점수: {buy_score}점 이상 (상승: {rise_score}점)\n"
        msg += f"• RSI 상한: {buy_rsi} 미만\n"
        msg += f"• 체결강도: {buy_vol}% 이상\n"
        msg += f"• 비대칭성 자동 연동: {auto_adj}\n"
        msg += f"• 매도잔량 비대칭성: {buy_ask_bid}배 이상 (100% 기준)\n"
        
        # [추세추종] 역매수는 OFF 고정 기조 — 켜져 있을 때만(비정상 상태 인지용) 상세를 표기
        use_mr = config.ANALYSIS_THRESHOLDS.get('USE_MEAN_REVERSION', True)
        if use_mr:
            msg += f"\n[역추세 매수 (낙폭과대 반등)]\n"
            msg += f"• 사용 여부: ON ⚠️ (추세추종 기조는 OFF 권장)\n"
            msg += f"• 발동 조건: RSI < {config.ANALYSIS_THRESHOLDS.get('MR_RSI_MAX', 40.0)} & 20일선 이격도 < {config.ANALYSIS_THRESHOLDS.get('MR_DISPARITY_MAX', 90.0)}%\n"
            msg += f"• 최소 체결강도: {config.ANALYSIS_THRESHOLDS.get('MR_VOL_STRENGTH', 120.0)}% 이상\n"

        # 매도 관련
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
        use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
        time_stop_min = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)

        msg += f"\n[매도 조건]\n"
        # [추세추종] 고정 익절/반익절은 미사용 기조 — 켜져 있을 때만(비정상 상태 인지용) 표기
        if tp > 0:
            msg += f"• 익절: +{tp}% ⚠️ (추세추종 기조는 미사용 권장)\n"
            half_tp_str = "ON (익절의 절반)" if config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True) else "OFF"
            msg += f"• 반익절: {half_tp_str}\n"
        else:
            msg += f"• 익절: 미사용 (트레일링 스탑 주도)\n"

        fixed_sl_status = "OFF" if use_atr else "ON"
        msg += f"• 고정 손절: {fixed_sl_status} ({sl}%)\n"

        atr_str = f"ON (x{atr_mult})" if use_atr else "OFF"
        msg += f"• ATR 손절: {atr_str}\n"
        
        time_stop_str = f"ON ({time_stop_days}일 경과 & 수익 < {time_stop_min}%)" if use_time_stop else "OFF"
        msg += f"• 시간 청산: {time_stop_str}\n"

        msg += f"• 트레일링 스탑: +{ts_act}% 도달 후 -{ts_call}% 하락 시 (샹들리에 ATR 동적 확대)\n"
        if tp_rsi > 0:
            msg += f"• 과열 매도: RSI {tp_rsi} 초과 ⚠️ (추세추종 기조는 미사용 권장)\n"
        msg += f"• 추세 이탈: 점수 {sell_score}점 미만 + 60일선 이탈\n"
        
        # [추가] 스코어링 가중치
        weights = config.SCORING_WEIGHTS
        msg += f"\n[스코어링 가중치]\n"
        msg += f"• 추세: {weights.get('TREND', 4.0)}\n"
        msg += f"• 모멘텀: {weights.get('MOMENTUM', 2.5)}\n"
        msg += f"• 강도: {weights.get('STRENGTH', 1.5)}\n"
        msg += f"• 시너지: {weights.get('SYNERGY', 2.0)}\n"

        # [추가] 적응형 임계값
        regime = config.MARKET_REGIME_PARAMS
        use_adaptive = "ON" if regime.get('USE_ADAPTIVE_THRESHOLD') else "OFF"
        msg += f"\n[적응형 임계값 ({use_adaptive})]\n"
        if regime.get('USE_ADAPTIVE_THRESHOLD'):
            msg += f"• 강세장 보정: {regime.get('BULL_SCORE_ADJ', -1.0):+.1f}점\n"
            msg += f"• 약세장 보정: {regime.get('BEAR_SCORE_ADJ', 1.0):+.1f}점\n"
            msg += f"• 횡보장 보정: {regime.get('SIDEWAYS_SCORE_ADJ', 0.0):+.1f}점\n"
            msg += f"• 기준: EMA {regime.get('REGIME_MA_PERIOD', 20)}일선 / ADX {regime.get('REGIME_ADX_THRESHOLD', 20)}\n"
            
            # [추가] 현재 시장 국면 정보
            try:
                k_regime, k_adj = analysis.get_market_regime("KOSPI")
                q_regime, q_adj = analysis.get_market_regime("KOSDAQ")
                
                r_map = {"Bull": "강세장", "Bear": "약세장", "Sideways": "횡보장"}
                k_str = r_map.get(k_regime, k_regime)
                q_str = r_map.get(q_regime, q_regime)
                
                msg += f"\n[현재 시장 국면]\n"
                msg += f"• KOSPI: {k_str} (보정 {k_adj:+.1f}점)\n"
                msg += f"• KOSDAQ: {q_str} (보정 {q_adj:+.1f}점)\n"
            except Exception as e:
                logger.debug(f"Market regime fetch error: {e}")

        # [추가] 기술적 지표 설정
        ind = config.INDICATOR_PARAMS
        msg += f"\n[기술적 지표]\n"
        msg += f"• RSI: {ind.get('RSI_PERIOD')} (Signal {ind.get('RSI_SIGNAL')})\n"
        msg += f"• MACD: {ind.get('MACD_FAST')}/{ind.get('MACD_SLOW')}/{ind.get('MACD_SIGNAL')}\n"
        msg += f"• CCI: {ind.get('CCI_WINDOW')} ({ind.get('CCI_UPPER')}/{ind.get('CCI_LOWER')})\n"
        msg += f"• ADX: {ind.get('ADX_PERIOD')}\n"
        msg += f"• SAR: {ind.get('SAR_AF_START')}/{ind.get('SAR_AF_STEP')}/{ind.get('SAR_AF_MAX')}\n"
        msg += f"• OBV: EMA {ind.get('OBV_MA_PERIOD')}\n"

        # 기타
        invest_ratio = config.settings.SYSTEM_INVEST_PER_STOCK
        max_holdings = config.settings.SYSTEM_MAX_HOLDINGS
        include_etf = getattr(config, 'SYSTEM_INCLUDE_ETF', False)
        etf_str = "포함" if include_etf else "제외"
        use_filter = getattr(config, 'USE_MARKET_FILTER', True)
        filter_ma = getattr(config, 'MARKET_FILTER_MA', 50)
        filter_str = f"ON (SMA {filter_ma}일선)" if use_filter else "OFF"
        slippage = getattr(config, 'SLIPPAGE_RATE', 0.002)

        use_vol = getattr(config, 'USE_VOLATILITY_TARGETING', True)
        vol_target = getattr(config, 'TARGET_VOLATILITY', 0.3)
        vol_str = f"ON (목표 {vol_target*100:.0f}%)" if use_vol else "OFF"

        msg += f"\n[기타]\n"
        msg += f"• 종목당 투자비중: {invest_ratio*100:.0f}% (최대 {max_holdings}종목, ETF {etf_str})\n"
        msg += f"• 슬리피지 비율: {slippage:.4f} ({slippage*100:.2f}%)\n"
        msg += f"• 시장 필터링: {filter_str}\n"
        msg += f"• 변동성 타겟팅: {vol_str}\n"
        
        # [추가] 개별 종목 룰 정보
        custom_rules = db_manager.db.get_all_stock_strategies()
        if custom_rules:
            msg += f"\n🔧 [개별 종목 룰 ({len(custom_rules)}개)]\n"
            for r in custom_rules:
                msg += f"• {r['name']}({r['code']})\n"
        
        # [추가] 기본 설정과 비교하여 변경된 커스텀 설정 항목 출력
        try:
            from config import GlobalSettings
            default_settings = getattr(GlobalSettings(), 'model_dump', GlobalSettings().dict)()
            current_settings = getattr(config.settings, 'model_dump', config.settings.dict)()
            
            changed_items = []
            for k, v in current_settings.items():
                if k in ["ACTIVE_PRESET"]: continue
                default_v = default_settings.get(k)
                if isinstance(v, dict) and isinstance(default_v, dict):
                    for sub_k, sub_v in v.items():
                        sub_default = default_v.get(sub_k)
                        if sub_v != sub_default:
                            desc = getattr(config, 'CONFIG_DESCRIPTIONS', {}).get(sub_k, "")
                            desc_str = f"\n  └ {desc}" if desc else ""
                            changed_items.append(f"• {sub_k}: {sub_default} ➔ {sub_v}{desc_str}")
                else:
                    if v != default_v:
                        desc = getattr(config, 'CONFIG_DESCRIPTIONS', {}).get(k, "")
                        desc_str = f"\n  └ {desc}" if desc else ""
                        changed_items.append(f"• {k}: {default_v} ➔ {v}{desc_str}")
                        
            if changed_items:
                msg += f"\n[커스텀 변경된 설정 내역]\n"
                msg += "\n".join(changed_items)
        except Exception as e:
            logger.debug(f"Changed settings extraction error: {e}")

        return msg

    def _get_trade_history(self, days=None):
        """거래 내역 조회 (기간별)"""
        now = datetime.now()
        end_date = now.strftime("%Y-%m-%d")
        start_date = None
        period_str = "전체"
        
        if days is not None:
            start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            if days == 0: period_str = "일간"
            elif days == 7: period_str = "주간"
            elif days == 30: period_str = "월간"
            else: period_str = f"최근 {days}일"

        all_trades = self._get_refined_trades_cached(target_account=None)
        
        trades = []
        for t in all_trades:
            t_date = t.get('time', '')[:10]
            if not start_date or t_date >= start_date:
                trades.append(t)
                
        # 최신 거래 내역이 상단에 오도록 정렬 (과거순 -> 최신순 정렬 역순)
        trades.sort(key=lambda x: x.get('time', ''), reverse=True)
        
        # 기간 표시 문자열 생성
        if start_date:
            period_msg = f"{start_date} ~ {end_date}"
        else:
            # 전체 조회인 경우 실제 데이터의 기간 표시
            if trades:
                min_date = trades[-1]['time'][:10]
                max_date = trades[0]['time'][:10]
                period_msg = f"{min_date} ~ {max_date} (전체)"
            else:
                period_msg = "전체"

        msg = f"📜 [거래 내역 ({period_str}) - {len(trades)}건]\n기간: {period_msg}"

        if not trades:
            return msg + "\n\n거래 내역이 없습니다."

        # [추가] 정제(중복 제거) 안내: 같은 주문의 접수→체결은 1건으로 병합되므로
        #  원본(메뉴) 거래 히스토리와 건수가 다를 수 있음을 알린다.
        msg += "\n💡 동일 주문의 접수→체결은 1건으로 합산 표시됩니다."

        # [추가] 제한 종목 및 개별 룰 로드
        restricted_stocks = auto_trade.get_restricted_stocks()
        custom_rules = db_manager.db.get_all_stock_strategies()
        rules_map = {r['code']: True for r in custom_rules}
        
        for t in trades:
            raw_type = str(t['type'])
            clean_type = raw_type.replace("buy", "매수").replace("BUY", "매수").replace("sell", "매도").replace("SELL", "매도").replace("AUTO", "자동")
            
            base_type = "기타"
            is_buy = "매수" in clean_type
            is_sell = "매도" in clean_type
            is_mod = "정정" in clean_type
            is_cancel = "취소" in clean_type
            
            if is_mod:
                if is_buy: base_type = "매수정정"
                elif is_sell: base_type = "매도정정"
                else: base_type = "정정"
            elif is_cancel:
                if is_buy: base_type = "매수취소"
                elif is_sell: base_type = "매도취소"
                else: base_type = "취소"
            elif is_buy: base_type = "매수"
            elif is_sell: base_type = "매도"
            
            tag_disp = "(자동)" if "자동" in clean_type else ("(수동)" if "수동" in clean_type else "(외부)")
            type_str = f"{base_type}{tag_disp}"
            name = t['name']
            code = t['code']
            qty = t['qty']
            price = float(t['price'])
            price_str = f"{price:,.2f}" if price < 1000 and "." in str(price) else f"{int(price):,}"
            
            total_val = price * float(qty)
            total_str = f"{total_val:,.2f}" if price < 1000 and "." in str(price) else f"{int(total_val):,}"
            
            # 손익
            profit_msg = ""
            if "매도" in type_str:
                amt = t.get('profit_amt', 0)
                rate = t.get('profit_rate', 0.0)
                if amt is not None:
                    profit_msg = f"\n   └ {amt:+,}원 ({rate:+.2f}%)"
            
            name_display = name
            if code in restricted_stocks: name_display += "-"
            if code in rules_map: name_display += "+"
            
            date_str = t['time'][5:19] # MM-DD HH:MM:SS
            
            status = t.get('order_status', '접수')
            raw_status = status
            if "부분체결" in status:
                status = "⏳ 부분체결"
            elif "체결(추정)" in status:
                status = "✅ 체결 추정"
            elif "취소(추정)" in status:
                status = "⚠️ 취소 추정"
            elif "거부" in status or "에러" in status or "REJECTED" in status.upper():
                status = "🚫 주문거부"
            elif "체결" in status:
                status = f"✅ {status}"
            elif "접수" in status:
                status = f"📥 {status}"
            elif "정정" in status:
                status = f"🔧 {status}"
            elif "취소" in status:
                status = f"🗑️ {status}"
            
            reason = t.get('reason') or ""
            
            # 스냅샷에서 상태(강매수 등) 추출하여 사유에 병합
            if t.get('snapshot'):
                try:
                    snap_data = json.loads(t['snapshot'])
                    state_val = snap_data.get('state')
                    if state_val and is_buy and state_val not in reason:
                        reason = f"[{state_val}] {reason}"
                except Exception: pass

            # [추가] 매수 사유 분류 커스텀 태그 적용
            if is_buy and reason:
                buy_tag = ""
                if "슈퍼모멘텀" in reason or "BREAKOUT" in reason: buy_tag = "돌파매수"
                elif "역매수" in reason or "역추세" in reason or "TRAILING_BUY" in reason: buy_tag = "눌림목"
                elif "조건 만족" in reason or "SCORE" in reason: buy_tag = "추세매수"
                elif "수동" in reason: buy_tag = "수동매수"
                
                if buy_tag and f"[{buy_tag}]" not in reason:
                    if reason.startswith("["): # [강매수] 등 스냅샷 태그 뒤에 병합
                        close_idx = reason.find("]")
                        if close_idx != -1:
                            reason = f"{reason[:close_idx+1]} [{buy_tag}]{reason[close_idx+1:]}"
                    else:
                        reason = f"[{buy_tag}] {reason}"

            # [추가] 매도 사유 분류 태그 적용
            if is_sell and reason:
                sell_tag = ""
                if "반익절" in reason: sell_tag = "반익절"
                elif "과열" in reason: sell_tag = "과열매도"
                elif "익절" in reason: sell_tag = "익절"
                elif "ATR" in reason and "손절" in reason: sell_tag = "ATR손절"
                elif "손절" in reason: sell_tag = "손절"
                elif "트레일링" in reason: sell_tag = "트레일링스탑"
                elif "시간" in reason and "청산" in reason: sell_tag = "시간청산"
                elif "추세" in reason or "점수" in reason or "매도진입" in reason: sell_tag = "추세이탈"
                elif "수동" in reason: sell_tag = "수동매도"
                
                if sell_tag and not reason.startswith("["):
                    reason = f"[{sell_tag}] {reason}"

            # [추가] 기간만료/발동실패 상태 사유 태그 적용
            if reason and ("기간만료" in raw_status or "발동실패" in raw_status):
                if not reason.startswith("["):
                    fail_tag = "기간만료" if "기간만료" in raw_status else "발동실패"
                    reason = f"[{fail_tag}] {reason}"

            # [추가] 예약취소 상태 사유 태그 적용
            if reason and ("예약취소" in raw_status or "RES_CAN" in str(t.get('odno', ''))):
                if not reason.startswith("["):
                    reason = f"[예약취소] {reason}"

            # [추가] 예약 주문 발동 사유 태그 적용
            if reason and ("예약" in clean_type or "예약발동" in status):
                if not reason.startswith("["):
                    reserve_tag = "예약매수" if is_buy else ("예약매도" if is_sell else "예약발동")
                    reason = f"[{reserve_tag}] {reason}"

            # [추가] 정정/취소 주문 사유 태그 적용
            if reason and (is_mod or is_cancel):
                if not reason.startswith("["):
                    mod_tag = "정정" if is_mod else "취소"
                    reason = f"[{mod_tag}] {reason}"

            # [추가] 부분체결 상태 사유 태그 적용
            if reason and "부분체결" in raw_status:
                if not reason.startswith("["):
                    reason = f"[부분체결] {reason}"

            # [추가] 체결(추정) 상태 사유 태그 적용
            if reason and "체결(추정)" in raw_status:
                if not reason.startswith("["):
                    reason = f"[체결추정] {reason}"

            # [추가] 취소(추정) 상태 사유 태그 적용
            if reason and "취소(추정)" in raw_status:
                if not reason.startswith("["):
                    reason = f"[취소추정] {reason}"

            # [추가] 주문거부 상태 사유 태그 적용
            if reason and ("거부" in raw_status or "에러" in raw_status or "REJECTED" in raw_status.upper()):
                if not reason.startswith("[") and not reason.startswith("🔴"):
                    reason = f"🔴 [주문거부] {reason}"

            # [추가] 접수(미체결) 상태 사유 태그 적용
            if reason and raw_status == "접수":
                if not reason.startswith("["):
                    reason = f"[미체결] {reason}"

            # [추가] 자동/수동/외부 사유 태그 적용
            if reason:
                if "자동" in clean_type and "[자동]" not in reason:
                    reason = f"[자동] {reason}"
                elif "수동" in clean_type and "[수동]" not in reason:
                    reason = f"[수동] {reason}"
                elif "자동" not in clean_type and "수동" not in clean_type and "예약" not in clean_type and "[외부]" not in reason:
                    reason = f"[외부] {reason}"

            reason_msg = ""
            if reason:
                # [수정] 사유 내에 대괄호가 포함된 경우 포맷팅 (줄바꿈 및 들여쓰기)
                if "[" in reason:
                    # 1. 대괄호 그룹 간 줄바꿈 (] [ -> ]\n         [)
                    formatted = reason.replace("] [", "]\n         [")
                    
                    # 2. 텍스트와 첫 대괄호 사이 줄바꿈
                    # 예: "조건 만족 [점수...]" -> "조건 만족\n         [점수...]"
                    first_bracket_idx = formatted.find("[")
                    if first_bracket_idx > 0:
                        pre_text = formatted[:first_bracket_idx].strip()
                        if pre_text:
                            formatted = f"{pre_text}\n         {formatted[first_bracket_idx:]}"
                    
                    reason_msg = f"\n   사유: {formatted}"
                
                # 기존 로직 (길이가 길고 소괄호가 있는 경우)
                elif len(reason) > 25 and "(" in reason:
                    parts = reason.partition("(")
                    if parts[0].strip():
                        reason_msg = f"\n   사유: {parts[0].strip()}\n         {parts[1]}{parts[2]}"
                    else:
                        reason_msg = f"\n   사유: {reason}"
                else:
                    reason_msg = f"\n   사유: {reason}"
            
            item_msg = f"\n\n• {date_str} | {type_str} | {status}\n   {name_display} {qty}주 @ {price_str}\n   평가: {total_str}원{profit_msg}{reason_msg}"
            
            msg += item_msg
            
        return msg
