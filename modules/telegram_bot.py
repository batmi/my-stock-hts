import threading
import logging
import time
import requests
import re
import os
from rich.console import Console

import config
import api
import utils
import indicators
from modules import analysis, account, chart, db_manager
from modules.auto_trade import AutoTrader

logger = logging.getLogger(__name__)
console = config.console

class TelegramCommander:
    """텔레그램 명령어를 수신하고 처리하는 클래스"""
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.is_running = False
        self.thread = None
        self.last_update_id = 0
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
            "/chart": self._cmd_chart,
            "/stocks": self._cmd_stocks,
            "/config": self._cmd_config,
            "/history": self._cmd_history,
            "/log": self._cmd_log,
            "/balance": self._cmd_balance,
            "/holdings": self._cmd_holdings
        }

    def start(self):
        if not self.bot_token: return
        if not config.ENABLE_TELEGRAM: return # [추가] 텔레그램 비활성화 시 시작 안 함
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.debug("[Telegram] 명령어 수신 대기 시작...")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2)

    def _run_loop(self):
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        timeout = config.TELEGRAM_POLLING_TIMEOUT
        
        while self.is_running:
            try:
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
                    console.print("\n[bold red][Telegram] 충돌 감지: 다른 인스턴스가 이미 텔레그램 봇을 사용 중입니다.[/bold red]")
                    console.print("[bold red]기존 시스템 보호를 위해 현재 인스턴스의 텔레그램 폴링을 중단합니다.[/bold red]")
                    self.trader.log("[Telegram] 충돌 감지: 다른 인스턴스가 봇을 사용 중이어서 폴링을 중단합니다.")
                    self.is_running = False
                    break
                    
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

        # 핸들러 호출
        if command in self.command_handlers:
            response = self.command_handlers[command](args)
            if response:
                self._send_reply(response)

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
            "• /report : 매매 성과 리포트 조회\n"
            "• /market : 주요 시장 지수 현황\n"
            "• /signal <종목> : 종목 기술적 분석 및 진단\n"
            "• /chart <종목> : 기술적 분석 차트 이미지 전송\n"
            "• /stocks : 현재 감시 중인 관심 종목 리스트\n"
            "• /config : 현재 매매 전략 설정값 조회\n"
            "• /history [개수] : 체결 내역 조회 (기본 10건)\n"
            "• /log [줄수] : 최근 시스템 로그 조회 (기본 10줄)\n"
            "• /balance : 계좌 자산 및 예수금 조회\n"
            "• /holdings : 현재 보유 종목 및 수익률 조회"
        )

    def _cmd_report(self, args):
        return self.trader.get_performance_report()

    def _cmd_market(self, args):
        return self._get_market_status()

    def _cmd_signal(self, args):
        if not args: return "⚠️ 종목명이나 코드를 입력해주세요.\n예: /signal 삼성전자"
        return self._analyze_stock(" ".join(args))

    def _cmd_chart(self, args):
        if not args:
            self._send_reply("⚠️ 종목명이나 코드를 입력해주세요.\n예: /chart 삼성전자")
        else:
            self._send_chart(" ".join(args))
        return None # 차트는 별도 전송하므로 반환값 없음

    def _cmd_stocks(self, args):
        return self._get_monitoring_list()

    def _cmd_config(self, args):
        return self._get_strategy_config()

    def _cmd_history(self, args):
        count = 10
        if args and args[0].isdigit():
            count = int(args[0])
            if count > 50: count = 50
        return self._get_trade_history(count)

    def _cmd_log(self, args):
        count = 10
        if args and args[0].isdigit():
            count = int(args[0])
            if count > 20: count = 20
        return self.trader.get_recent_logs(count)

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
                
                msg = f"💰 [계좌 자산 현황]\n"
                msg += f"총 자산: {data['tot_asset']:,}원\n"
                msg += f"평가 손익: {data['sec_pl']:+,}원\n"
                msg += "\n"
                msg += f"예수금(원화): {data['dep_dom']:,}원\n"
                msg += f"예수금(외화): {data['dep_ovs']:,}원\n"
                msg += f"주문가능(D+2): {data['d2_dep']:,}원\n"
                msg += f"주식 평가: {data['sec_eval']:,}원"
                
                if data['ovrs_eval_krw'] > 0:
                    msg += f"\n(해외주식 포함: {data['ovrs_eval_krw']:,}원)"
                
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
                holdings, _ = api.get_domestic_balance(target_cano, acnt)
            
            if not holdings:
                return "📋 [보유 종목] 없음"
            
            msg = f"📋 [보유 종목 현황] ({len(holdings)}종목)\n"
            
            for item in holdings:
                name = item['prdt_name']
                qty = int(item['hldg_qty'])
                cur_price = int(item['prpr'])
                buy_price = float(item['pchs_avg_pric'])
                profit = int(item['evlu_pfls_amt'])
                rate = float(item['evlu_pfls_rt'])
                
                icon = "🔴" if profit > 0 else ("🔵" if profit < 0 else "⚪️")
                msg += f"\n{icon} {name} ({qty}주)\n   현재: {cur_price:,}원 | 평단: {buy_price:,.0f}원\n   손익: {profit:+,}원 ({rate:+.2f}%)"
            
            return msg
        except Exception as e:
            return f"⚠️ 보유 종목 조회 중 오류 발생: {str(e)}"

    # --- 내부 로직 메서드 ---
    def _send_reply(self, text):
        api.send_telegram_message(text)

    def _get_market_status(self):
        """시장 지수(KOSPI/KOSDAQ/원자재/환율) 현황 조회"""
        msg = "📊 [시장 지수 현황]\n"
        
        # 통합 리스트 (모두 yfinance 사용)
        targets = [
            ("KOSPI", "^KS11"),
            ("KOSDAQ", "^KQ11"),
            ("나스닥", "^IXIC"),
            ("S&P500", "^GSPC"),
            ("다우존스", "^DJI"),
            ("금", "GC=F"),
            ("은", "SI=F"),
            ("SOX(반도체)", "^SOX"),
            ("달러환율", "KRW=X")
        ]
        
        ma_period = getattr(config, 'MARKET_FILTER_MA', 20)
        
        for name, code in targets:
            try:
                df = api.get_chart_data(code, is_overseas=True)
                if df is None or df.empty:
                    msg += f"\n• {name}: 데이터 조회 실패"
                    continue
                
                current = df.iloc[-1]['close']
                prev = df.iloc[-2]['close'] if len(df) > 1 else current
                diff = current - prev
                rate = (diff / prev) * 100
                
                val_fmt = f"{current:,.2f}"
                if code == "KRW=X": val_fmt += "원"
                
                msg += f"\n• {name} {val_fmt} ({rate:+.2f}%)"
                
                # KOSPI/KOSDAQ의 경우 추세 정보 추가
                if name in ["KOSPI", "KOSDAQ"]:
                    ma_val = df['close'].ewm(span=ma_period, adjust=False).mean().iloc[-1]
                    trend = "📈" if current >= ma_val else "📉"
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
        if keyword.isdigit() and len(keyword) == 6:
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

            state, _ = analysis.classify_stock_state(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend')
            )
            score, _ = analysis.calculate_score(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend')
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

            # 이평선 상태
            ema_state = "혼조/역배열"
            if ind['ema_20'] and ind['ema_60'] and ind['ema_120']:
                if ind['ema_20'] > ind['ema_60'] > ind['ema_120']: ema_state = "정배열"
                elif ind['ema_20'] < ind['ema_60'] < ind['ema_120']: ema_state = "역배열"
            
            # 매수/보유 판단 로직
            buy_score_limit = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            buy_rsi_limit = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
            
            is_buy_score = score >= buy_score_limit
            is_buy_rsi = (ind['rsi'] is not None) and (ind['rsi'] < buy_rsi_limit)
            buy_result = "매수 가능" if (is_buy_score and is_buy_rsi) else "매수 불가"

            sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]
            is_sell_signal = (score < sell_score_limit) or (state == "위험")
            sell_result = "매도(추세이탈)" if is_sell_signal else "보유(추세유지)"

            msg = f"🔍 [종목 진단] {name}({code})\n"
            msg += f"현재가: {price_fmt}\n"
            msg += f"52주: {l52_fmt} ~ {h52_fmt} ({pos_52:.1f}%)\n"
            msg += f"종합 점수: {score}점 / 11점\n"
            msg += f"상태 분류: {state}\n"
            msg += f"매수 판단: {buy_result}\n"
            msg += f"보유 판단: {sell_result}\n"
            msg += f"\n[주요 지표]\n"
            msg += f"• 이평선: {ema_state}\n"
            msg += f"• SAR: {sar_state}\n"
            msg += f"• RSI: {rsi_str}\n"
            msg += f"• ADX: {adx_str}\n"
            msg += f"• CCI: {cci_str}"
            
            return msg
        except Exception as e:
            return f"⚠️ 분석 중 오류 발생: {str(e)}"
            
    def _send_chart(self, keyword):
        """차트 이미지를 생성하여 텔레그램으로 전송"""
        code, name, is_overseas = self._resolve_stock(keyword)
        
        if not code:
            self._send_reply(f"⚠️ '{keyword}' 종목을 찾을 수 없습니다.")
            return

        try:
            self._send_reply(f"⏳ {name}({code}) 차트 생성 중...")
            
            # 차트 생성 (config.CHART_DIR에 저장됨)
            chart.generate_visual_chart(code, name, is_overseas, open_file=False, dpi=100)
            
            # 파일 경로 추론
            safe_code = re.sub(r'[=\-\.\^]', '', code)
            filename = f"analysis_{safe_code}.png"
            file_path = os.path.join(config.CHART_DIR, filename)
            
            caption = f"📊 {name}({code}) 분석 차트"
            
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
                    msg += f"\n - {s['name']} ({s['code']})"
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
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        msg += f"\n[매수 조건]\n"
        msg += f"• 종합 점수: {buy_score}점 이상\n"
        msg += f"• RSI 상한: {buy_rsi} 미만\n"
        
        # 매도 관련
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
        
        msg += f"\n[매도 조건]\n"
        msg += f"• 익절(TP): +{tp}%\n"
        msg += f"• 손절(SL): {sl}%\n"
        msg += f"• 트레일링 스탑: +{ts_act}% 도달 후 -{ts_call}% 하락 시\n"
        msg += f"• 과열 매도: RSI {tp_rsi} 초과\n"
        msg += f"• 추세 이탈: 점수 {sell_score}점 미만\n"
        
        # 기타
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)
        msg += f"\n[기타]\n"
        msg += f"• 종목당 투자비중: {invest_ratio*100:.0f}%\n"
        
        return msg

    def _get_trade_history(self, limit=10):
        """최근 체결 내역 조회"""
        trades = db_manager.db.get_trades(limit=limit)
        if not trades:
            return "📭 거래 내역이 없습니다."
        
        msg = f"📜 [최근 체결 내역 ({len(trades)}건)]"
        for t in trades:
            type_str = t['type'].replace("buy", "매수").replace("sell", "매도").replace("AUTO", "자동").replace("수동", "")
            name = t['name']
            qty = t['qty']
            price = float(t['price'])
            price_str = f"{price:,.2f}" if price < 1000 and "." in str(price) else f"{int(price):,}"
            
            # 손익
            profit_msg = ""
            if "매도" in type_str:
                amt = t.get('profit_amt', 0)
                rate = t.get('profit_rate', 0.0)
                if amt is not None:
                    icon = "🔴" if amt > 0 else "🔵"
                    profit_msg = f"\n   └ {icon} {amt:+,}원 ({rate:+.2f}%)"
            
            date_str = t['time'][5:16] # MM-DD HH:MM
            item_msg = f"\n\n• {date_str} | {type_str}\n   {name} {qty}주 @ {price_str}{profit_msg}"
            
            # 메시지 길이 제한 체크 (텔레그램 4096자 제한 대비 여유 있게 4000자)
            if len(msg) + len(item_msg) > 4000:
                msg += "\n\n...(메시지 길이 제한으로 이후 내역 생략)"
                break
            
            msg += item_msg
            
        return msg
