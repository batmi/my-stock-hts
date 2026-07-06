import threading
import logging
import time
import math
from datetime import datetime, timedelta, timezone

import config
import api
from modules import theme_analysis, account
from modules.auto_trade import AutoTrader
import utils

logger = logging.getLogger(__name__)

class SystemScheduler:
    """백그라운드 스케줄링 및 타이머 작업을 전담하는 클래스"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(SystemScheduler, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized: return
        self._initialized = True
        self.is_running = False
        self.thread = None
        self.last_holiday_notified_date = None
        self.last_briefing_date = None
        self.last_heartbeat_time = time.time()
        self.trader = AutoTrader()

    def start(self):
        if not getattr(config, 'ENABLE_TELEGRAM', True): return
        if self.is_running: return
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="SystemScheduler")
        self.thread.start()
        logger.debug("[Scheduler] 백그라운드 스케줄러 시작...")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2)
        logger.debug("[Scheduler] 스케줄러 종료 완료")

    def _run_loop(self):
        while self.is_running:
            try:
                self._check_holiday_notification()
                if getattr(config, 'AUTO_MORNING_BRIEFING_USE', False):
                    self._check_morning_briefing()
                if getattr(config.settings, 'AUTO_DISCLOSURE_ALERT_USE', False):
                    self._check_disclosure_alerts()
                if getattr(config, 'MARKET_HALT_ALERT_USE', True):
                    self._check_market_halt()
                self._check_heartbeat()
            except Exception as e:
                logger.error(f"[Scheduler] 스케줄러 루프 에러: {e}", exc_info=True)

            time.sleep(10) # 10초 주기 체크

    def _check_disclosure_alerts(self):
        """관심종목 중대 공시 텔레그램 알림 (평일, 30분 간격 폴링)."""
        now = datetime.now()
        last = getattr(self, 'last_disclosure_check', None)
        if last and (now - last).total_seconds() < 1800:
            return
        self.last_disclosure_check = now
        if now.weekday() >= 5:
            return
        try:
            from modules.manage import disclosure
            threading.Thread(target=disclosure.check_and_alert_disclosures, daemon=True).start()
        except Exception as e:
            logger.error(f"[Scheduler] 공시 알림 체크 오류: {e}")

    def _check_market_halt(self):
        """서킷브레이커(CB)/VI 시장정지 감지 및 알림 (KIS:CB+VI, 토스:VI)."""
        try:
            from modules.market_halt import MarketHaltMonitor
            MarketHaltMonitor().check()
        except Exception as e:
            logger.error(f"[Scheduler] 시장정지 점검 오류: {e}")

    def _check_holiday_notification(self):
        """평일 공휴일(휴장일) 아침 안내 메시지 전송 스케줄러"""
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        
        if getattr(self, 'last_holiday_notified_date', None) == today_str:
            return
            
        try:
            target_dt = datetime.strptime("0830", "%H%M").time()
            now_time = now.time()
            end_dt = (datetime.combine(datetime.today(), target_dt) + timedelta(hours=1)).time()
            
            if target_dt <= now_time <= end_dt:
                self.last_holiday_notified_date = today_str 
                if now.weekday() < 5:
                    is_kr_holiday = api.is_holiday_today()
                    is_us_holiday = api.is_us_holiday_today()
                    
                    kr_name = api.get_holiday_name(today_str.replace("-", ""), 'KR')
                    us_name = api.get_holiday_name(today_str.replace("-", ""), 'US')
                    
                    kr_str = f" '{kr_name}'" if kr_name else " 공휴일"
                    us_str = f" '{us_name}'" if us_name else " 공휴일"
                    
                    if is_kr_holiday and is_us_holiday:
                        api.send_telegram_message(f"🏖️ [시스템 알림] 오늘은{kr_str}로 인해 국내 및 미국 주식 시장이 모두 휴장합니다.\n자동매매 시스템은 매매 없이 대기 모드를 유지합니다.")
                    elif is_kr_holiday:
                        api.send_telegram_message(f"🏖️ [시스템 알림] 오늘은 한국{kr_str}로 '국내 주식 시장'이 휴장합니다.\n(단, 미국 주식 시장은 정상적으로 개장합니다.)\n자동매매 시스템은 국내장 시간 동안 대기 모드를 유지합니다.")
                    elif is_us_holiday:
                        api.send_telegram_message(f"🏖️ [시스템 알림] 오늘은 미국{us_str}로 '미국 주식 시장'이 휴장합니다.\n(국내 주식 시장은 정상 개장합니다.)")
        except Exception as e:
            logger.error(f"Holiday notification check error: {e}")

    def _check_morning_briefing(self):
        """장전 브리핑 발송 시간 확인 및 트리거"""
        now = datetime.now()
        target_time_str = getattr(config, 'AUTO_MORNING_BRIEFING_TIME', "0830")
        today_str = now.strftime("%Y-%m-%d")
        
        if getattr(self, 'last_briefing_date', None) == today_str: return
            
        try:
            target_dt = datetime.strptime(target_time_str, "%H%M").time()
            now_time = now.time()
            end_dt = (datetime.combine(datetime.today(), target_dt) + timedelta(hours=1)).time()
            
            if target_dt <= now_time <= end_dt:
                self.last_briefing_date = today_str 
                if now.weekday() < 5:
                    api.send_telegram_message("⏳ [시스템 알림] 간밤의 글로벌 마켓 데이터를 수집하고 AI 장전 브리핑을 작성 중입니다...")
                    threading.Thread(target=self.execute_briefing, daemon=True).start()
        except Exception as e:
            logger.error(f"Morning briefing check error: {e}")

    def _check_heartbeat(self):
        """1분 주기 하트비트 점검"""
        now = time.time()
        if now - self.last_heartbeat_time > 60:
            self.last_heartbeat_time = now
            is_problem = False
            msg = ""
            
            if self.trader.is_running and self.trader.thread and not self.trader.thread.is_alive():
                is_problem = True
                msg = "자동매매 스레드가 예기치 않게 종료되었습니다. (크래시 의심)"
                
            max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
            if self.trader.consecutive_errors >= max_err:
                is_problem = True
                msg = f"시스템 연속 에러가 한계치({max_err}회)에 도달했습니다."
                
            if not hasattr(self, '_last_problem_msg'):
                self._last_problem_msg = ""
                
            if is_problem:
                if self._last_problem_msg != msg:
                    api.send_telegram_message(f"🚨 [시스템 하트비트 이상 감지]\n{msg}\n서버 접속 후 시스템 상태를 확인해주세요.")
                    self._last_problem_msg = msg
            else:
                self._last_problem_msg = ""
                
    def execute_briefing(self):
        """글로벌 마켓 데이터를 수집하고 Gemini에게 브리핑을 요청하여 전송"""
        try:
            market_data_str = theme_analysis._get_macro_context_str()
            briefing = theme_analysis.generate_morning_briefing(market_data_str)
            if briefing: api.send_telegram_message(briefing)
            else: api.send_telegram_message("⚠️ AI 장전 브리핑 생성에 실패했습니다.")
        except Exception as e:
            logger.error(f"Morning briefing send error: {e}")
            
    def execute_daily_closing_report(self):
        """현재 계좌 정보 및 당일 매매 내역을 바탕으로 AI 장 마감 종합 브리핑 생성 및 전송"""
        try:
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                deposit = res['d2_deposit'] if res else 0
                
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []
                if not valid_holdings:
                    api.send_telegram_message("📭 보유 종목이 없어 장 마감 브리핑을 수행할 수 없습니다.")
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
                    
                report = theme_analysis.generate_daily_closing_report(portfolio_str)
                if report:
                    if report.startswith("⚠️"): api.send_telegram_message(report)
                    else: api.send_telegram_message(f"🛡️ [AI 장 마감 종합 브리핑]\n\n{report}")
                else:
                    api.send_telegram_message("⚠️ AI 장 마감 브리핑 생성에 실패했습니다.")
        except Exception as e:
            logger.error(f"Daily closing report error: {e}")
            api.send_telegram_message("⚠️ 장 마감 브리핑 생성 중 오류가 발생했습니다.")