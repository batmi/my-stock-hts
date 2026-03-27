import threading
import logging
import time
import requests
import re
import os
import math # [추가] 장전 브리핑 시 yfinance 데이터 계산용
from datetime import datetime, timedelta

import config
import context # [추가]
import api
import utils
import indicators
from modules import analysis, account, chart, db_manager, auto_trade, market, theme_analysis
from modules.auto_trade import AutoTrader

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
        self.last_briefing_date = None # [추가] 브리핑 중복 전송 방지용
        self.last_portfolio_date = None # [추가] 포트폴리오 진단 중복 방지용
        self.trader = AutoTrader() # 싱글톤 인스턴스 참조
        
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
            "/balance": self._cmd_balance,
            "/holdings": self._cmd_holdings,
            "/portfolio": self._cmd_portfolio, # [추가] 포트폴리오 AI 진단
            "/curate": self._cmd_curate, # [추가] AI 종목 큐레이션
            "/scan": self._cmd_scan, # [추가] 트레이딩뷰 스크리너
            "/rules": self._cmd_rules,
            "/profit": self._cmd_profit,
            "/restrict": self._cmd_restricted
        }

    def start(self):
        if not self.bot_token: return
        if not config.ENABLE_TELEGRAM: return # [추가] 텔레그램 비활성화 시 시작 안 함
        if self.is_running: return # [추가] 중복 실행 방지
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="TelegramBot")
        self.thread.start()
        logger.debug("[Telegram] 명령어 수신 대기 시작...")

        # [추가] 봇 재시작 알림 전송
        try:
            api.send_telegram_message("🤖 [시스템 알림] 텔레그램 봇이 재연결되었습니다.")
        except Exception as e:
            logger.error(f"[Telegram] 재시작 알림 전송 실패: {e}")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2)

    def _run_loop(self):
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        timeout = config.TELEGRAM_POLLING_TIMEOUT
        
        while self.is_running:
            try:
                # [추가] 장전 브리핑 발송 확인 스케줄러
                if getattr(config, 'AUTO_MORNING_BRIEFING_USE', False):
                    self._check_morning_briefing()

                # [추가] 장 종료 후 AI 포트폴리오 진단 리포트 스케줄러
                self._check_after_market_portfolio()

                params = {"offset": self.last_update_id + 1, "timeout": timeout, "allowed_updates": ["message"]}
                response = requests.get(url, params=params, timeout=timeout + 5)
                
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
                if self.is_running: logger.error(f"[Telegram] Polling Error: {e}")
                time.sleep(5) # 에러 시 대기

    def _handle_message(self, message):
        text = message.get('text', '').strip()
        chat_id = str(message.get('chat', {}).get('id'))
        
        # 설정된 Chat ID와 다르면 무시 (보안)
        if config.TELEGRAM_CHAT_ID and chat_id != str(config.TELEGRAM_CHAT_ID):
            return

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

        # [추가] /chart_종목코드 단축 명령어 지원
        if command.startswith('/chart_'):
            target = command.replace('/chart_', '')
            args = [target] + args
            command = '/chart'

        # 핸들러 호출
        if command in self.command_handlers:
            response = self.command_handlers[command](args)
            if response:
                self._send_reply(response)

    def _check_morning_briefing(self):
        """장전 브리핑 발송 시간 확인 및 트리거"""
        now = datetime.now()
        target_time_str = getattr(config, 'AUTO_MORNING_BRIEFING_TIME', "0830")
        today_str = now.strftime("%Y-%m-%d")
        
        if self.last_briefing_date == today_str:
            return
            
        try:
            # 문자열 시간을 datetime 객체로 파싱하여 비교 (1시간 이내에 봇이 켜졌을 때 발송 보장)
            target_dt = datetime.strptime(target_time_str, "%H%M").time()
            now_time = now.time()
            end_dt = (datetime.combine(datetime.today(), target_dt) + timedelta(hours=1)).time()
            
            if target_dt <= now_time <= end_dt:
                self.last_briefing_date = today_str # 중복 방지 즉시 마킹
                # 주말(토, 일)에는 발송하지 않음
                if now.weekday() < 5:
                    threading.Thread(target=self._send_morning_briefing, daemon=True).start()
        except Exception as e:
            logger.error(f"Morning briefing check error: {e}")

    def _send_morning_briefing(self):
        """글로벌 마켓 데이터를 수집하고 Gemini에게 브리핑을 요청하여 전송"""
        try:
            import yfinance as yf
            self._send_reply("⏳ [시스템 알림] 간밤의 글로벌 마켓 데이터를 수집하고 AI 장전 브리핑을 작성 중입니다...")
            
            target_indices = {
                "나스닥": "^IXIC", "S&P500": "^GSPC", 
                "달러환율": "KRW=X", "WTI 원유": "CL=F", 
                "VIX (변동성)": "^VIX", "비트코인": "BTC-USD"
            }
            market_data_str = ""
            
            for name, ticker in target_indices.items():
                try:
                    t = yf.Ticker(ticker)
                    fi = t.fast_info
                    last_price = fi.last_price
                    prev_close = fi.regular_market_previous_close
                    if last_price and prev_close and not math.isnan(last_price) and not math.isnan(prev_close):
                        rate = ((last_price - prev_close) / prev_close) * 100
                        
                        yh = getattr(fi, 'year_high', None)
                        yh_str = ""
                        if yh and not math.isnan(yh) and yh > 0:
                            yh_rate = ((last_price - yh) / yh) * 100
                            yh_str = f" (52주 고점대비 {yh_rate:+.1f}%)"
                            
                        status_desc = theme_analysis.evaluate_market_indicator(name, last_price, yh_rate)
                        status_str = f" -> [현재 상태: {status_desc}]" if status_desc else ""
                        
                        market_data_str += f"{name}: {last_price:,.2f} (전일대비 {rate:+.2f}%){yh_str}{status_str}\n"
                except Exception: pass
            
            if not market_data_str:
                market_data_str = "글로벌 마감 데이터 수집에 실패했습니다. (yfinance 응답 지연)"
                
            briefing = theme_analysis.generate_morning_briefing(market_data_str)
            if briefing:
                self._send_reply(briefing)
            else:
                self._send_reply("⚠️ AI 장전 브리핑 생성에 실패했습니다.")
        except Exception as e:
            logger.error(f"Morning briefing send error: {e}")

    def _check_after_market_portfolio(self):
        """장 종료 시간 5분 후 포트폴리오 진단 리포트 발송"""
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        
        if self.last_portfolio_date == today_str:
            return
            
        end_time_str = getattr(config, 'SYSTEM_TRADING_END_TIME', "1510") # 기본값 1510 (15:10)
        try:
            end_time = datetime.strptime(end_time_str, "%H%M").time()
            target_dt = datetime.combine(now.date(), end_time) + timedelta(minutes=5)
            
            if target_dt <= now <= target_dt + timedelta(hours=1):
                if now.weekday() < 5: # 주말 제외
                    self.last_portfolio_date = today_str
                    self._send_reply("🌅 [장 마감 알림] 정규장이 종료되었습니다.\n오늘의 AI 포트폴리오 리스크 진단을 시작합니다...")
                    threading.Thread(target=self._execute_portfolio_diagnosis, daemon=True).start()
        except Exception as e:
            logger.error(f"After market portfolio check error: {e}")
            
    def _cmd_portfolio(self, args):
        self._send_reply("⏳ [AI 포트폴리오 진단] 현재 자산 및 종목 데이터를 수집하여 AI 분석을 요청합니다. 잠시만 기다려주세요...")
        threading.Thread(target=self._execute_portfolio_diagnosis, daemon=True).start()
        return None
        
    def _execute_portfolio_diagnosis(self):
        try:
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                deposit = res['d2_deposit'] if res else 0
                
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []
                if not valid_holdings:
                    self._send_reply("📭 보유 종목이 없어 포트폴리오 진단을 수행할 수 없습니다.")
                    return
                    
                tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                total_asset = deposit + tot_evlu
                
                portfolio_str = f"총 자산: {total_asset:,}원 (보유 {len(valid_holdings)}종목)\n\n[보유 종목 비중]\n"
                for item in valid_holdings:
                    name = item['prdt_name']
                    eval_amt = int(item['evlu_amt'])
                    weight = (eval_amt / total_asset) * 100
                    profit_rate = float(item['evlu_pfls_rt'])
                    portfolio_str += f"- {name}: 비중 {weight:.1f}% (수익률 {profit_rate:+.2f}%)\n"
                    
                diagnosis = theme_analysis.generate_portfolio_diagnosis(portfolio_str)
                if diagnosis:
                    if diagnosis.startswith("⚠️"):
                        self._send_reply(diagnosis)
                    else:
                        self._send_reply(f"🛡️ [AI 포트폴리오 리스크 진단]\n\n{diagnosis}")
                else:
                    self._send_reply("⚠️ AI 포트폴리오 진단에 실패했습니다.")
        except Exception as e:
            logger.error(f"Portfolio diagnosis error: {e}")
            self._send_reply("⚠️ 포트폴리오 진단 중 오류가 발생했습니다.")
            
    def _cmd_curate(self, args):
        self._send_reply("⏳ [AI 종목 큐레이션] 실시간 시장 매크로 데이터 및 뉴스를 분석하여 주도주를 발굴 중입니다. 잠시만 기다려주세요...")
        threading.Thread(target=self._execute_curate, daemon=True).start()
        return None
        
    def _execute_curate(self):
        try:
            result = theme_analysis.generate_stock_curation()
            if result:
                if result.startswith("⚠️"):
                    self._send_reply(result)
                else:
                    msg = f"{result}\n\n💡 마음에 드는 종목이 있다면 터미널 HTS 메뉴 [7] 관심 종목 관리 -> [1] 종목 추가 를 통해 수동으로 등록해주세요."
                    self._send_reply(msg)
            else:
                self._send_reply("⚠️ AI 큐레이션 생성에 실패했습니다.")
        except Exception as e:
            logger.error(f"Curation error: {e}")
            self._send_reply("⚠️ 큐레이션 실행 중 오류가 발생했습니다.")

    def _cmd_scan(self, args):
        self._send_reply("⏳ [TradingView Screener] 시장을 스캔 중입니다. 잠시만 기다려주세요...")
        threading.Thread(target=self._execute_scan, args=(args,), daemon=True).start()
        return None
        
    def _execute_scan(self, args):
        try:
            from tradingview_screener import Query, Column
            import pandas as pd
        except ImportError:
            self._send_reply("⚠️ tradingview-screener 라이브러리가 설치되지 않았습니다. 터미널에서 pip install tradingview-screener 명령어를 실행해주세요.")
            return

        market = "korea"
        preset = "Pullback"
        
        if args:
            # New logic for single-letter options
            arg_str = "".join(args).lower()
            
            # Market check: 'u' for US, 'k' for Korea (default)
            # If 'k' is present, it forces Korea even if 'u' is also there.
            if 'k' in arg_str:
                market = "korea"
            elif 'u' in arg_str:
                market = "america"

            # Preset check
            preset_map = {
                't': "Pullback", 'm': "Momentum", 'b': "Rebound",
                'v': "Volume", 'd': "TopLosers"
            }
            
            found_preset = False
            for p_key, p_val in preset_map.items():
                if p_key in arg_str:
                    preset = p_val
                    found_preset = True
                    break
            
            # If no other preset is found, check for 'u' as a preset
            if not found_preset and 'u' in arg_str:
                preset = "TopGainers"

        try:
            query = Query().set_markets(market).select('name', 'description', 'close', 'change', 'volume', 'RSI', 'SMA20')
            
            if preset == "Pullback":
                query = query.where(Column('close') > Column('SMA20'), Column('RSI') < 40).order_by('volume', ascending=False)
                desc = "상승 추세 눌림목 (현재가 > 20일선 & RSI < 40)"
            elif preset == "Momentum":
                query = query.where(Column('close') > Column('SMA20'), Column('RSI') > 70).order_by('volume', ascending=False)
                desc = "강한 모멘텀 (현재가 > 20일선 & RSI > 70 & 거래량 상위)"
            elif preset == "Rebound":
                query = query.where(Column('RSI') < 30, Column('change') > 0).order_by('volume', ascending=False)
                desc = "바닥 반등 (RSI < 30 & 상승 반전)"
            elif preset == "Volume":
                query = query.where(Column('close') > Column('SMA20'), Column('volume') > 1000000).order_by('change', ascending=False)
                desc = "거래량 급증 (현재가 > 20일선 & 거래량 > 100만)"
            elif preset == "TopGainers":
                if market == "america":
                    query = query.where(Column('volume') > 100000, Column('close') >= 1.0).order_by('change', ascending=False)
                    desc = "당일 급상승 상위 15종목 (거래량 10만, 1달러 이상)"
                else:
                    query = query.where(Column('volume') > 100000).order_by('change', ascending=False)
                    desc = "당일 급상승 상위 15종목 (거래량 10만 이상)"
            elif preset == "TopLosers":
                if market == "america":
                    query = query.where(Column('volume') > 100000, Column('close') >= 1.0).order_by('change', ascending=True)
                    desc = "당일 급하락 상위 15종목 (거래량 10만, 1달러 이상)"
                else:
                    query = query.where(Column('volume') > 100000).order_by('change', ascending=True)
                    desc = "당일 급하락 상위 15종목 (거래량 10만 이상)"
            
            if preset in ["TopGainers", "TopLosers"]:
                query = query.limit(15)
            else:
                query = query.limit(20)
                
            count, df = query.get_scanner_data()
            
            if df is None or df.empty:
                self._send_reply(f"📭 조건에 맞는 종목이 없습니다.\n조건: {desc}")
                return
                
            msg = f"🔎 [시장 스캔 결과]\n• 시장: {'미국' if market == 'america' else '국내'}\n• 조건: {desc}\n\n"
            
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
                                fetched_name = out.get('prdt_abrv_name') or out.get('prdt_name') or out.get('rprs_mrkt_kor_name')
                                if fetched_name: kor_name = fetched_name
                        except Exception:
                            pass
                    if kor_name: name = kor_name

                close = row.get('close', 0)
                change = row.get('change', 0)
                rsi = row.get('RSI', 0)
                
                close_str = f"${close:,.2f}" if market == "america" else f"{int(close):,}원"
                rsi_str = f"RSI: {rsi:.1f}" if pd.notna(rsi) else ""
                
                msg += f"• {name} ({ticker})\n  {close_str} ({change:+.2f}%) {rsi_str}\n\n"
                
            msg += "💡 상세 분석을 원하시면 '/analyze 종목코드'를 입력하세요."
            self._send_reply(msg.strip())
            
        except Exception as e:
            logger.error(f"Screener Scan Error: {e}")
            self._send_reply(f"⚠️ 스크리너 실행 중 오류가 발생했습니다.\n{e}")

    # --- 명령어 핸들러 메서드 ---
    def _cmd_status(self, args):
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
            self.trader.stop()
            return "🛑 시스템 트레이딩 중단 요청을 처리했습니다."

    def _cmd_restart(self, args):
        msg = []
        if self.trader.is_running:
            self.trader.stop()
            msg.append("🛑 시스템 트레이딩 중단 완료.")
            time.sleep(1)  # 상태 정리 대기
        
        self.trader.start(interactive=False)
        msg.append("🚀 시스템 트레이딩을 재시작했습니다.")
        return "\n".join(msg)

    def _cmd_help(self, args):
        return (
            "🤖 [시스템 트레이딩 봇 도움말]\n\n"
            "• /help : 명령어 목록 확인\n"
            "• /start : 시스템 트레이딩 시작\n"
            "• /stop : 시스템 트레이딩 중단\n"
            "• /restart : 시스템 트레이딩 재시작\n"
            "• /status : 시스템 트레이딩 상태 조회\n"
            "• /config : 현재 트레이딩 전략 설정값 조회\n"
            "• /rules [종목] : 종목별 트레이딩 룰 조회\n"
            "• /restrict : 트레이딩 제한 종목 조회\n"
            "• /log : 최근 시스템 트레이딩 로그 조회\n"
            "• /report [기간] : 거래 성과 리포트 (d/w/m/n)\n"
            "• /profit [기간] : 거래 실현 손익 조회 (d/w/m/n)\n"
            "• /history [기간] : 거래 내역 조회 (d/w/m/n)\n"
            "• /market [그룹] : 지수 현황 (k/u/t/c/f/i/b/g)\n"
            "• /stocks : 현재 감시 중인 관심 종목 리스트\n"
            "• /signal <종목> : 종목 기술적 분석 및 진단\n"
            "• /analyze <종목> : AI 종목 기술적 분석\n"
            "• /ask <질문> : AI에게 주식/경제 관련 자유 질문\n"
            "• /chart [기간] <종목> : 차트 전송 (d/h/m)\n"
            "• /balance : 계좌 자산 및 예수금 조회\n"
            "• /holdings : 보유 종목 및 수익률 조회\n"
            "• /portfolio : AI 포트폴리오 리스크 진단\n"
            "• /curate : 실시간 시장 주도주 AI 추천\n"
            "• /scan [조건] : TV 스캔 (k/u & p/m/r/v/g/l)"

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
            't': "미국채 금리 (US Treasury Yields)",
            'c': "원자재 (Commodities)",
            'f': "환율 (Exchange Rates)",
            'i': "변동성/반도체 (Volatility/Semiconductors)",
            'b': "암호화폐 (Cryptocurrency)",
            'g': "글로벌 지수 (Global Indices)"
        }
        
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
        return self._analyze_stock(" ".join(args))
        
    def _cmd_analyze(self, args):
        if not args: return "⚠️ 종목명이나 코드를 입력해주세요.\n예: /analyze 삼성전자"
        
        keyword = " ".join(args)
        code, name, is_overseas = self._resolve_stock(keyword)
        if not code:
            return f"⚠️ '{keyword}' 종목을 찾을 수 없습니다."

        self._send_reply(f"⏳ '{name}({code})' 심층 진단 중...\n(차트 분석 + AI 뉴스 검색 중이므로 10~20초 소요됩니다)")
        
        try:
            df = api.get_chart_data(code, is_overseas)
            if df is None or df.empty:
                return f"⚠️ 차트 데이터를 불러올 수 없어 분석할 수 없습니다."
            
            ind = indicators.calculate_indicators(df)
            current_price = float(df.iloc[-1]['close'])
            
            # 상태 분류를 위한 전일 RSI 계산
            prev_rsi = None
            if len(df) >= 16:
                delta = df['close'].diff()
                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
                except: pass

            state, _, state_reason = analysis.classify_stock_state(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
            )

            score, _ = analysis.calculate_score(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
            )

            rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
            adx_val = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
            cci_val = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
            price_str = f"${current_price:,.2f}" if is_overseas else f"{int(current_price):,}원"
            tech_info = (
                f"• 현재가: {price_str}\n"
                f"• 시스템 상태: {state} (사유: {state_reason})\n"
                f"• 퀀트 점수: {score}점 / 10점 만점\n"
                f"• 핵심 지표: RSI {rsi_val} | ADX {adx_val} | CCI {cci_val}"
            )

            answer = theme_analysis.analyze_stock_with_gemini(code, name, tech_info)
            return f"🤖 [AI 종목 심층 진단] {name}({code})\n\n{answer}"
        except Exception as e:
            return f"⚠️ 진단 중 오류 발생: {e}"

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

        period_type = 'hourly'
        period_label = "시봉"
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
        restricted_stocks = auto_trade.load_restricted_stocks()

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
            name_display = name
            if code in restricted_stocks: name_display += "-"
            name_display += "+" # 개별 룰 목록이므로 항상 +
            
            memo_part = f"   메모: {r.get('memo', '')}\n" if r.get('memo') else ""
            
            # [추가] 가중치 표시
            w_str = "기본"
            if r.get('weights'):
                try:
                    w = r['weights']
                    if isinstance(w, str): w = json.loads(w)
                    if isinstance(w, dict):
                        w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"
                except: pass
                
            sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"

            msg += (f"\n• {name_display}({code})\n"
                    f"   매수: {r['buy_score']}점 / RSI {r.get('buy_rsi', 65.0)}↓ / 체결 {r.get('buy_vol_strength', 100.0)}%\n"
                    f"   청산: 익절 +{r['take_profit']}% / 과열 RSI {r.get('take_profit_rsi', 75.0)}↑ / TS +{r['ts_activation']}%(-{r['ts_callback']}%) / 기한 {r.get('time_stop_days', 10)}일\n"
                    f"   리스크: 비중 {r.get('invest_ratio', getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2))*100:.0f}% / 손절 {sl_str}\n"
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
            name_display = name
            name_display += "-" # 제한 종목 목록이므로 항상 -
            if code in rules_map: name_display += "+"
            
            memo = info.get('memo', '-')
            msg += f"\n• {name_display}({code})\n   메모: {memo}"
        return msg

    def _cmd_profit(self, args):
        days = 0
        if args:
            arg = args[0].lower()
            if arg in ["d", "day", "daily", "일간"]: days = 0
            elif arg in ["w", "week", "weekly", "주간"]: days = 7
            elif arg in ["m", "month", "monthly", "월간"]: days = 30
            elif arg.isdigit(): days = int(arg)
        
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
            
        raw_trades = db_manager.db.get_trades(start_date=start_dt, end_date=end_dt)
        
        trades = []
        for r in raw_trades:
            type_str = r.get('type', '')
            simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
            parsed_r = dict(r)
            parsed_r['type'] = simple_type
            trades.append(parsed_r)

        # [추가] 동일한 주문번호(odno)의 접수-체결 중복 내역 병합 및 제거
        if hasattr(self.trader, '_refine_trade_records'):
            trades = self.trader._refine_trade_records(trades)
            
        # [추가] 통계 계산을 위해 시간순(과거->최신)으로 정렬
        if trades:
            trades.sort(key=lambda x: x.get('time', ''))
            
        stats = self.trader._calculate_statistics(trades)
        
        # 매도(청산) 내역만 필터링하여 손익 합산
        sell_trades = [t for t in trades if "매도" in t.get('type', '') or "sell" in t.get('type', '').lower()]
        
        # [추가] 제한 종목 및 개별 룰 로드
        restricted_stocks = auto_trade.load_restricted_stocks()
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

    def _cmd_balance(self, args):
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
                
                if not config.session.is_simulation:
                    msg += f"    └ D+1: {data['d1_dep']:,}원\n"
                    msg += f"    └ D+2: {data['d2_dep']:,}원\n"
                else:
                    msg += f"    └ D+1: {data['d1_dep']:,}원\n"
                    msg += f"    └ D+2: {data['d2_dep']:,}원\n"

                msg += f"  • 외화: {data['dep_ovs']:,}원\n"
                
                # [추가] 주문가능금액 표시
                ord_psbl = data.get('order_possible')
                if ord_psbl is None:
                    try:
                        bal = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        if bal: ord_psbl = bal.get('order_possible', 0)
                    except: pass
                
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
            restricted_stocks = auto_trade.load_restricted_stocks()
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
                
                # 총 평가금액 (주식 + 예수금)
                tot_evlu = api.safe_int(s_data.get('tot_evlu_amt'))
                if tot_evlu == 0:
                    # API가 0을 줄 경우 직접 계산 (모의투자 등)
                    stock_evlu = api.safe_int(s_data.get('scts_evlu_amt'))
                    deposit = api.safe_int(s_data.get('prvs_rcdl_excc_amt'))
                    if deposit == 0: deposit = api.safe_int(s_data.get('dnca_tot_amt'))
                    tot_evlu = stock_evlu + deposit
                
                # 총 평가손익
                tot_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                
                # [추가] 수익률 계산
                tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
                if tot_pchs == 0: tot_pchs = calc_total_pchs
                
                total_rate = 0.0
                if tot_pchs > 0:
                    total_rate = (tot_profit / tot_pchs) * 100
                
                msg += f"\n\n 총 평가금액: {tot_evlu:,}원"
                msg += f"\n 총 평가손익: {tot_profit:+,}원 ({total_rate:+.2f}%)"

            return msg
        except Exception as e:
            return f"⚠️ 보유 종목 조회 중 오류 발생: {str(e)}"

    # --- 내부 로직 메서드 ---
    def _send_reply(self, text):
        api.send_telegram_message(text)

    def _get_market_status(self, target_group_names=None):
        """시장 지수(KOSPI/KOSDAQ/원자재/환율) 현황 조회"""
        msg = "📊 [시장 지수 현황]\n"
        
        # [수정] 통합 지수 리스트 사용
        targets = market.ALL_INDICES
        
        # 구분선(공백라인)을 넣을 지수명 리스트
        section_keys = ["나스닥 선물", "미국채 2년물 선물", "금", "달러인덱스", "VIX (변동성)", "비트코인", "Japan - 닛케이"]
        
        # [추가] 국내 지수 매핑 (analysis.get_domestic_index_data 호출용)
        domestic_map = {
            "코스피": "KOSPI", "코스피200": "KOSPI200",
            "코스닥": "KOSDAQ", "코스닥150": "KOSDAQ150"
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
                df = None
                # [수정] 국내 지수는 analysis 모듈을 통해 KIS API 우선 조회
                if name in domestic_map:
                    df = analysis.get_domestic_index_data(domestic_map[name])
                else:
                    df = api.get_chart_data(code, is_overseas=True)
                
                if df is None or df.empty:
                    msg += f"\n• {name}: 데이터 조회 실패"
                    continue
                
                current = df.iloc[-1]['close']
                prev = df.iloc[-2]['close'] if len(df) > 1 else current
                
                eval_price = current # 상태 판별용 원본 지수
                
                # [추가] 해외 지수는 fast_info 우선 적용 (실시간 국채 금리 추정 포함)
                if name not in domestic_map:
                    try:
                        fi = api.get_yf_fast_info(code)
                        if fi and fi.get('last_price'):
                            fast_current = float(fi['last_price'])
                            fast_prev = float(fi.get('regular_market_previous_close', fast_current))
                            
                            eval_price = fast_current
                            
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
                                            est_yield = fast_current - (f_rate / fut_info["duration"])
                                            fast_prev = fast_current
                                            fast_current = est_yield
                                            name = f"{name}(선물적용)"
                            
                            current = fast_current
                            prev = fast_prev
                    except: pass
                
                diff = current - prev
                rate = (diff / prev) * 100
                
                if "미국채" in name and "선물" not in name:
                    val_fmt = f"{current:,.2f}%"
                    msg += f"\n• {name} {val_fmt} ({diff:+.2f}p)"
                else:
                    val_fmt = f"{current:,.2f}"
                    if code == "KRW=X": val_fmt += "원"
                    msg += f"\n• {name} {val_fmt} ({rate:+.2f}%)"
                
                # 시장 국면 판단 로직 적용 (모든 지수)
                ma_series = df['close'].ewm(span=regime_ma_period, adjust=False).mean()
                ma_val = ma_series.iloc[-1]
                
                slope = 0
                if len(ma_series) >= 5:
                    slope = (ma_series.iloc[-1] - ma_series.iloc[-5]) / 5
                
                ind = indicators.calculate_indicators(df)
                adx = ind['adx']
                adx_val = adx if adx is not None else 0
                adx_threshold = config.MARKET_REGIME_PARAMS.get("REGIME_ADX_THRESHOLD", 20)
                
                if eval_price > ma_val and slope > 0 and adx_val >= adx_threshold:
                    trend = "📈" # 강세장
                elif eval_price < ma_val:
                    trend = "📉" # 약세장
                else:
                    trend = "📊" # 횡보장
                    
                msg += f" {trend}"
                    
            except Exception as e:
                msg += f"\n• {name}: 오류"
        
        return msg

    def _resolve_stock(self, keyword):
        """종목명/코드를 입력받아 (코드, 이름, 해외여부)를 반환하는 헬퍼 함수"""
        code = None
        name = None
        is_overseas = False
        
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
        restricted_stocks = auto_trade.load_restricted_stocks()
        
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
                except: pass

        try:
            # 3. 데이터 조회 및 분석
            df = api.get_chart_data(code, is_overseas)
            if df is None or df.empty:
                return f"⚠️ {name}({code}) 차트 데이터를 불러올 수 없습니다."
            
            ind = indicators.calculate_indicators(df)
            current_price = float(df.iloc[-1]['close'])
            
            # 52주 최고/최저 및 위치 계산 (최근 250일 데이터 기준)
            high_52 = df['high'].max()
            low_52 = df['low'].min()
            pos_52 = 0.0
            if high_52 > low_52:
                pos_52 = (current_price - low_52) / (high_52 - low_52) * 100
            
            # 전일 RSI 계산 (상태 분류용)
            # [수정] AutoTrader의 protected 메서드 대신 직접 계산 (결합도 감소)
            prev_rsi = None
            if len(df) >= 16:
                delta = df['close'].diff()
                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
                except: pass
                
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
                except: pass
                
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
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'],
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'),
                thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
            )
            score, _ = analysis.calculate_score(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'),
                weights=weights, smart_money=sm_flag
            )
            
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
            obv_state = "상승" if obv_trend else "하락"

            # MACD 상태
            macd_val = ind.get('macd')
            sig_val = ind.get('macd_signal')
            macd_state = "-"
            if macd_val is not None and sig_val is not None:
                macd_state = "골든" if macd_val > sig_val else "데드"

            # 이평선 상태
            ema_state = "혼조/역배열"
            if ind['ema_20'] is not None and ind['ema_60'] is not None and ind['ema_120'] is not None:
                if ind['ema_20'] > ind['ema_60'] > ind['ema_120']: ema_state = "정배열"
                elif ind['ema_20'] < ind['ema_60'] < ind['ema_120']: ema_state = "역배열"
            
            # [수정] 매수/보유 판단 로직 (보정된 기준 사용)
            buy_score_limit = buy_score
            buy_rsi_limit = buy_rsi
            
            is_buy_score = score >= buy_score_limit
            is_buy_rsi = (ind['rsi'] is not None) and (ind['rsi'] < buy_rsi_limit)
            is_safe_state = state not in ["매도", "주의"]
            
            if is_buy_score and is_buy_rsi and is_safe_state:
                buy_result = f"🔴 매수 가능 (조건 충족{regime_msg})"
            else:
                reasons = []
                if not is_safe_state: reasons.append(f"상태:{state}")
                if not is_buy_score: reasons.append(f"점수미달({score}<{buy_score_limit}{regime_msg})")
                if not is_buy_rsi:
                    rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "N/A"
                    reasons.append(f"RSI과열({rsi_val}>={buy_rsi_limit})")
                buy_result = f"🔵 매수 불가 ({', '.join(reasons)})"

            sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]
            is_sell_signal = (score < sell_score_limit) or (state == "매도")
            
            if is_sell_signal:
                reasons = []
                if state == "매도": reasons.append(f"상태:{state}")
                if score < sell_score_limit: reasons.append(f"점수하락({score}<{sell_score_limit})")
                sell_result = f"🔵 매도 ({', '.join(reasons)})"
            else:
                sell_result = "🟢 보유 (추세유지)"

            state_emoji_map = {"매수": "🔴", "강매수": "💥", "역매수": "🟣", "상승": "🟠", "관망": "⚪", "주의": "🟡", "매도": "🔵"}
            state_emoji = state_emoji_map.get(state, "")

            msg = f"🔍 [종목 진단{rule_tag}] {name_display}({code})\n"
            msg += f"현재가: {price_fmt}\n"
            msg += f"52주: {l52_fmt} ~ {h52_fmt} ({pos_52:.1f}%)\n"
            msg += f"종합 점수: {score}점 / 10점\n"
            msg += f"상태 분류: {state_emoji} {state} ({reason})\n"
            msg += f"매수 판단: {buy_result}\n"
            msg += f"보유 판단: {sell_result}\n"
            msg += f"\n[주요 지표]\n"
            msg += f"• EMA: {ema_state}\n"
            msg += f"• SAR: {sar_state}\n"
            msg += f"• MACD: {macd_state}\n"
            msg += f"• OBV: {obv_state}\n"
            msg += f"• RSI: {rsi_str}\n"
            msg += f"• ADX: {adx_str}\n"
            msg += f"• CCI: {cci_str}"
            
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
        restricted_stocks = auto_trade.load_restricted_stocks()
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
                self.trader.log(f"[Telegram] 차트 전송 성공: {filename}")
            else:
                self.trader.log(f"[Telegram] 차트 전송 실패: {filename}")
                self._send_reply("⚠️ 차트 전송에 실패했습니다. (로그 확인)")
                
        except Exception as e:
            self.trader.log(f"[Telegram] 차트 전송 중 예외 발생: {e}")
            self._send_reply(f"⚠️ 차트 전송 중 오류 발생: {str(e)}")
            logger.error(f"[Telegram] 차트 전송 예외: {e}")

    def _get_monitoring_list(self):
        """현재 감시 중인 종목 리스트 반환"""
        msg = "📋 [현재 감시 종목 리스트]\n"
        
        # [추가] 제한 종목 및 개별 룰 로드
        restricted_stocks = auto_trade.load_restricted_stocks()
        custom_rules = db_manager.db.get_all_stock_strategies()
        rules_map = {r['code']: True for r in custom_rules}
        
        groups = {
            "stocks_kr": "🇰🇷 국내주식",
            "etfs_kr": "🇰🇷 국내ETF",
            "stocks_us": "🇺🇸 미국주식",
            "etfs_us": "🇺🇸 미국ETF"
        }
        
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
                    if code in rules_map:
                        name += "+"
                        
                    msg += f"\n - {name} ({code})\n   /signal_{code} /chart_{code}"
                msg += "\n"
        
        if not has_stock:
            msg += "\n등록된 관심 종목이 없습니다."
            
        return msg

    def _get_strategy_config(self):
        """현재 매매 전략 설정값 반환"""
        # 시스템 상태 및 계좌 정보
        status_icon = "🟢" if self.trader.is_running else "🔴"
        status_text = "실행 중" if self.trader.is_running else "중지됨"
        
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        acc_label = "모의" if config.session.is_simulation else "실전"
        if not config.session.is_simulation and config.session.auto_cano:
            acc_label = "자동"

        msg = f"{status_icon} [시스템 상태: {status_text}]\n"
        msg += f"• 운용 계좌: {target_cano} ({acc_label})\n\n"
        msg += "⚙️ [매매 전략 설정]\n"
        
        # 매수 관련
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]
        msg += f"\n[매수 조건]\n"
        msg += f"• 종합 점수: {buy_score}점 이상 (상승: {rise_score}점)\n"
        msg += f"• RSI 상한: {buy_rsi} 미만\n"
        msg += f"• 체결강도: {buy_vol}% 이상\n"
        
        msg += f"\n[역추세 매수 (낙폭과대 반등)]\n"
        use_mr = config.ANALYSIS_THRESHOLDS.get('USE_MEAN_REVERSION', True)
        msg += f"• 사용 여부: {'ON' if use_mr else 'OFF'}\n"
        msg += f"• 발동 조건: RSI < {config.ANALYSIS_THRESHOLDS.get('MR_RSI_MAX', 40.0)} & 20일선 이격도 < {config.ANALYSIS_THRESHOLDS.get('MR_DISPARITY_MAX', 90.0)}%\n"
        msg += f"• 최소 체결강도: {config.ANALYSIS_THRESHOLDS.get('MR_VOL_STRENGTH', 120.0)}% 이상\n"

        # 매도 관련
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
        use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
        atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
        time_stop_min = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)

        msg += f"\n[매도 조건]\n"
        msg += f"• 익절: +{tp}%\n"
        
        half_tp_str = "ON (익절의 절반)" if config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False) else "OFF"
        msg += f"• 반익절: {half_tp_str}\n"
        
        fixed_sl_status = "OFF" if use_atr else "ON"
        msg += f"• 고정 손절: {fixed_sl_status} ({sl}%)\n"

        atr_str = f"ON (x{atr_mult})" if use_atr else "OFF"
        msg += f"• ATR 손절: {atr_str}\n"
        
        time_stop_str = f"ON ({time_stop_days}일 경과 & 수익 < {time_stop_min}%)" if use_time_stop else "OFF"
        msg += f"• 시간 청산: {time_stop_str}\n"

        msg += f"• 트레일링 스탑: +{ts_act}% 도달 후 -{ts_call}% 하락 시\n"
        msg += f"• 과열 매도: RSI {tp_rsi} 초과\n"
        msg += f"• 추세 이탈: 점수 {sell_score}점 미만\n"
        
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
            msg += f"• 기준: EMA {regime.get('REGIME_MA_PERIOD', 60)}일선 / ADX {regime.get('REGIME_ADX_THRESHOLD', 20)}\n"
            
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
            except Exception: pass

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
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2)
        max_holdings = getattr(config, 'SYSTEM_MAX_HOLDINGS', 10)
        use_filter = getattr(config, 'USE_MARKET_FILTER', True)
        filter_ma = getattr(config, 'MARKET_FILTER_MA', 20)
        filter_str = f"ON (SMA {filter_ma}일선)" if use_filter else "OFF"
        slippage = getattr(config, 'SLIPPAGE_RATE', 0.001)

        use_vol = getattr(config, 'USE_VOLATILITY_TARGETING', True)
        vol_target = getattr(config, 'TARGET_VOLATILITY', 0.2)
        vol_str = f"ON (목표 {vol_target*100:.0f}%)" if use_vol else "OFF"

        msg += f"\n[기타]\n"
        msg += f"• 종목당 투자비중: {invest_ratio*100:.0f}% (최대 {max_holdings}종목)\n"
        msg += f"• 슬리피지 비율: {slippage:.4f} ({slippage*100:.2f}%)\n"
        msg += f"• 시장 필터링: {filter_str}\n"
        msg += f"• 변동성 타겟팅: {vol_str}\n"
        
        # [추가] 개별 종목 룰 정보
        custom_rules = db_manager.db.get_all_stock_strategies()
        if custom_rules:
            msg += f"\n🔧 [개별 종목 룰 ({len(custom_rules)}개)]\n"
            for r in custom_rules:
                msg += f"• {r['name']}({r['code']})\n"
        
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

        # [수정] 전체 내역 조회 (체결 필터 제거)
        trades = db_manager.db.get_trades(limit=None, start_date=start_date)
        
        # [추가] 동일한 주문번호(odno)의 접수-체결 중복 내역 병합 및 제거
        if hasattr(self.trader, '_refine_trade_records'):
            trades = self.trader._refine_trade_records(trades)
        
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
        
        # [추가] 제한 종목 및 개별 룰 로드
        restricted_stocks = auto_trade.load_restricted_stocks()
        custom_rules = db_manager.db.get_all_stock_strategies()
        rules_map = {r['code']: True for r in custom_rules}
        
        for t in trades:
            type_str = t['type'].replace("buy", "매수").replace("sell", "매도").replace("AUTO", "자동").replace("(수동)", "").replace("수동", "")
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
            if "체결(추정)" in status: status = "체결 추정"
            
            if "체결" in status:
                status = f"✅ {status}"
            
            reason = t.get('reason')
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
            
            # 메시지 길이 제한 체크 (텔레그램 4096자 제한 대비 여유 있게 4000자)
            if len(msg) + len(item_msg) > 4000:
                msg += "\n\n...(메시지 길이 제한으로 이후 내역 생략)"
                break
            
            msg += item_msg
            
        return msg
