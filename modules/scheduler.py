import threading
import logging
import time
import math
from datetime import datetime, timedelta, timezone

import config
import api
from modules import theme_analysis, account, heartbeat
from modules.telegram_notify import alert_delivered
from modules.auto_trade import AutoTrader
from core import utils

logger = logging.getLogger(__name__)

class SystemScheduler:
    """백그라운드 스케줄링 및 타이머 작업을 전담하는 클래스"""
    #  하루 한 번짜리 알림이 전달에 실패했을 때 같은 날 다시 거는 간격(초).
    #  수집이 무거워서(DART·yfinance 전종목) 짧게 두면 파이가 견디지 못한다.
    CALENDAR_RETRY_SEC = 600
    #  휴장 안내·장전 브리핑처럼 조회가 가벼운 하루 1회 알림의 재시도 간격(초).
    NOTICE_RETRY_SEC = 120
    #  생존 신호 쓰기가 연속 몇 번 실패하면 알릴 것인가. 도장 주기가 60초이므로
    #  3회면 약 3분 — 밖의 감시자가 사망으로 판정하기(약속 시각 = 3주기 + 60초) 직전이다.
    BEAT_FAIL_ALERT_STREAK = 3
    _instance = None
    _instance_lock = threading.RLock()

    #  [동시성 2026-09-05] 싱글톤 생성을 락으로 감싼다. 종전 `if cls._instance is None:` 는
    #   검사와 대입 사이가 열려 있었고, 더 나쁜 것은 **인스턴스를 먼저 대입하고 속성을 그 뒤에
    #   채운다**는 점이었다 — 두 번째 스레드는 그 사이에 들어와 '있다'고 보고 반쯤 만들어진
    #   객체를 그대로 가져간다. 초기화 도중에 파일 I/O(로거 생성)·DB 접근이 있어 GIL 이 실제로
    #   놓이므로 이론상의 경합이 아니다(실측: 8스레드 중 7개가 미완성 객체를 받는다).
    #   기동 순서상 열려 있다 — main 이 텔레그램 봇 스레드를 먼저 띄우고(telegram_cmd.start())
    #   스케줄러·트레이더는 그 뒤에 처음 만든다. 봇 스레드의 명령 처리는 이 생성자를 부른다.
    def __new__(cls, *args, **kwargs):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super(SystemScheduler, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        #  [동시성 2026-09-05] __init__ **전체**를 락 안에 둔다. __new__ 만 잠그면
        #   그것이 인스턴스를 돌려준 뒤 __init__ 이 끝나기 전에 다른 스레드가 들어와,
        #   `_initialized` 만 보고 **아직 채워지지 않은 객체**를 완성품으로 가져간다
        #   (실측: SystemScheduler.trader 가 없는 객체를 받는다). 가드만 잠가서는
        #   한 겹 아래로 같은 구멍이 내려갈 뿐이다.
        with self._instance_lock:
            if self._initialized:
                return
            self._initialized = True
            self.is_running = False
            self.thread = None
            self.last_holiday_notified_date = None
            self.last_briefing_date = None
            self.last_calendar_alert_date = None
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
        # 도장을 찍던 스레드가 내려간다 — '앞으로 신호가 없는 건 정상'임을 남겨야
        #  밖의 감시자가 사망으로 오해하지 않는다.
        heartbeat.stopped(reason="스케줄러 종료", mode=self._heartbeat_context().get("mode"))
        logger.debug("[Scheduler] 스케줄러 종료 완료")

    def _run_loop(self):
        while self.is_running:
            try:
                self._check_holiday_notification()
                if getattr(config, 'AUTO_MORNING_BRIEFING_USE', False):
                    self._check_morning_briefing()
                if getattr(config.settings, 'AUTO_DISCLOSURE_ALERT_USE', True):
                    self._check_disclosure_alerts()
                if getattr(config.settings, 'AUTO_CALENDAR_ALERT_USE', True):
                    self._check_calendar_alerts()
                #  [Fix 2026-09-04] 종전에는 CB 스위치(MARKET_HALT_ALERT_USE) 하나로
                #   check() 진입 자체를 막았다. market_halt 는 CB 와 VI 를 독립 스위치로
                #   설계했는데(그 안에서 각각 다시 본다), 여기서 한꺼번에 막히니 CB 를 끈
                #   사용자는 VI 를 켜도 아무 일도 일어나지 않았다 — 메뉴 토글이 거짓말을 했다.
                #   둘 중 하나라도 켜져 있으면 넘기고, 무엇을 볼지는 check() 가 정한다.
                if (getattr(config, 'MARKET_HALT_ALERT_USE', True)
                        or getattr(config, 'MARKET_HALT_VI_USE', False)):
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

    def _check_calendar_alerts(self):
        """임박한 경제 이벤트·배당/실적 일정 알림 (하루 1회, 지정 시각 이후 첫 순회).

        공시(30분 폴링)와 달리 하루 한 번이면 충분한 일정 정보다. 수집이 DART·yfinance
        전종목 조회라 무거워서 별도 스레드로 돌리고, 발송 여부와 무관하게 하루 한 번만 시도한다.
        주말도 거른다 — 월요일 일정의 D-1(일요일) 알림까지 챙기기 위해서다.

        [하루 표식은 **일이 끝난 뒤** 찍는다 · 2026-09-07] 종전에는 워커를 띄우기 전에
         날짜를 찍고 결과를 보지 않았다. events.check_and_alert_calendar 는 2026-09-04 에
         '전달을 확인한 뒤에만 표시한다 — 실패하면 다음 기회에 다시 시도'로 고쳤는데,
         그 **다음 기회를 여기서 없애고 있었다**(실측: 워커가 실패해도 같은 날 재시도 0회).
         D-1 알림은 다음 날 보내 봐야 소용이 없으므로 그 날 안에 다시 걸어야 한다.
         다만 무한 재시도는 안 된다 — 수집이 DART·yfinance 전종목 조회라 파이에서 무겁다.
         발송 창(3시간) 안에서 CALENDAR_RETRY_SEC 간격으로만 다시 본다.
        """
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if getattr(self, 'last_calendar_alert_date', None) == today_str:
            return
        if getattr(self, '_calendar_worker_running', False):
            return          # 앞 주기 워커가 아직 돌고 있다 — 겹쳐 띄우지 않는다
        last_try = getattr(self, '_last_calendar_attempt', None)
        if last_try and (now - last_try).total_seconds() < self.CALENDAR_RETRY_SEC:
            return

        target_str = getattr(config.settings, 'AUTO_CALENDAR_ALERT_TIME', "0820")
        try:
            target = datetime.strptime(target_str, "%H%M")
        except ValueError:
            target = datetime.strptime("0820", "%H%M")
        # 발송 창을 3시간으로 잡는다 — 낮에 켠 인스턴스가 한밤중에 '오늘 일정'을 쏘지 않도록
        end = (target + timedelta(hours=3)).time()
        if not (target.time() <= now.time() <= end):
            return

        self._last_calendar_attempt = now

        def _run():
            try:
                from modules.manage import events as calendar_events
                #  1 = 보냈다 / 0 = 보낼 것이 없었다 / -1 = 전달 확인 실패.
                #  앞의 둘만 '오늘 할 일은 끝'이다.
                if calendar_events.check_and_alert_calendar() >= 0:
                    self.last_calendar_alert_date = today_str
                else:
                    logger.warning("[Scheduler] 캘린더 알림 전달 미확인 — "
                                   f"{self.CALENDAR_RETRY_SEC}초 뒤 오늘 안에 다시 시도합니다")
            except Exception as e:      # noqa: BLE001
                logger.error(f"[Scheduler] 캘린더 알림 체크 오류 — 다시 시도합니다: {e}",
                             exc_info=True)
            finally:
                self._calendar_worker_running = False

        self._calendar_worker_running = True
        try:
            threading.Thread(target=_run, daemon=True, name="CalendarAlert").start()
        except Exception as e:      # noqa: BLE001
            self._calendar_worker_running = False
            logger.error(f"[Scheduler] 캘린더 알림 스레드 기동 실패: {e}")

    def _check_market_halt(self):
        """서킷브레이커(CB)/VI 시장정지 감지 및 알림 (KIS:CB+VI, 토스:VI)."""
        try:
            from modules.market_halt import MarketHaltMonitor
            MarketHaltMonitor().check()
        except Exception as e:
            logger.error(f"[Scheduler] 시장정지 점검 오류: {e}")

    def _check_holiday_notification(self):
        """평일 공휴일(휴장일) 아침 안내 메시지 전송 스케줄러.

        [하루 표식은 일이 끝난 뒤 · 2026-09-07] 종전에는 창에 들어서자마자 날짜를 찍고
         그 뒤에 조회·발송을 했다. 그래서 api.is_holiday_today() 가 던지거나 텔레그램이
         끊겨 있으면 **그날 안내는 영영 나가지 않았다** — 예외는 로그로만 남고, 하루가
         이미 소비된 뒤라 남은 창(1시간) 동안 다시 시도하지 않는다.
         보낼 것이 없는 날(평일 정상 개장·주말)은 그대로 찍고, 보낼 것이 있는 날은
         **전달을 확인한 뒤에** 찍는다([[unknown-vs-empty]] 와 같은 계열 —
         '못 보냈다'가 '보냈다'로 굳지 않게).
        """
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        if getattr(self, 'last_holiday_notified_date', None) == today_str:
            return
        last_try = getattr(self, '_last_holiday_attempt', None)
        if last_try and (now - last_try).total_seconds() < self.NOTICE_RETRY_SEC:
            return

        try:
            target_dt = datetime.strptime("0830", "%H%M").time()
            now_time = now.time()
            end_dt = (datetime.combine(datetime.today(), target_dt) + timedelta(hours=1)).time()

            if not (target_dt <= now_time <= end_dt):
                return
            self._last_holiday_attempt = now
            if now.weekday() >= 5:
                self.last_holiday_notified_date = today_str    # 보낼 것이 없는 날
                return

            is_kr_holiday = api.is_holiday_today()
            is_us_holiday = api.is_us_holiday_today()

            kr_name = api.get_holiday_name(today_str.replace("-", ""), 'KR')
            us_name = api.get_holiday_name(today_str.replace("-", ""), 'US')

            kr_str = f" '{kr_name}'" if kr_name else " 공휴일"
            us_str = f" '{us_name}'" if us_name else " 공휴일"

            if is_kr_holiday and is_us_holiday:
                msg = (f"🏖️ [시스템 알림] 오늘은{kr_str}로 인해 국내 및 미국 주식 시장이 "
                       f"모두 휴장합니다.\n자동매매 시스템은 매매 없이 대기 모드를 유지합니다.")
            elif is_kr_holiday:
                msg = (f"🏖️ [시스템 알림] 오늘은 한국{kr_str}로 '국내 주식 시장'이 휴장합니다.\n"
                       f"(단, 미국 주식 시장은 정상적으로 개장합니다.)\n"
                       f"자동매매 시스템은 국내장 시간 동안 대기 모드를 유지합니다.")
            elif is_us_holiday:
                msg = (f"🏖️ [시스템 알림] 오늘은 미국{us_str}로 '미국 주식 시장'이 휴장합니다.\n"
                       f"(국내 주식 시장은 정상 개장합니다.)")
            else:
                self.last_holiday_notified_date = today_str    # 정상 개장 — 보낼 것이 없다
                return

            if alert_delivered(msg):
                self.last_holiday_notified_date = today_str
            else:
                logger.warning(f"[Scheduler] 휴장 안내 전달 미확인 — "
                               f"{self.NOTICE_RETRY_SEC}초 뒤 오늘 안에 다시 시도합니다")
        except Exception as e:      # noqa: BLE001
            logger.error(f"Holiday notification check error — 다시 시도합니다: {e}")

    def _check_morning_briefing(self):
        """장전 브리핑 발송 시간 확인 및 트리거.

        [하루 표식은 일이 끝난 뒤 · 2026-09-07] 휴장 안내와 같은 자리다 — 창에 들어서자마자
         날짜를 찍었으므로, 예고 메시지 전송이 실패하거나 브리핑 스레드가 뜨지 못해도
         그날은 이미 소비됐다. 예고가 전달된 것을 확인한 뒤 찍는다(브리핑 본문 생성 실패는
         execute_briefing 이 스스로 알린다 — 그건 '보낼 것이 없는' 경우가 아니라 결과다).
        """
        now = datetime.now()
        target_time_str = getattr(config, 'AUTO_MORNING_BRIEFING_TIME', "0830")
        today_str = now.strftime("%Y-%m-%d")

        if getattr(self, 'last_briefing_date', None) == today_str:
            return
        last_try = getattr(self, '_last_briefing_attempt', None)
        if last_try and (now - last_try).total_seconds() < self.NOTICE_RETRY_SEC:
            return

        try:
            target_dt = datetime.strptime(target_time_str, "%H%M").time()
            now_time = now.time()
            end_dt = (datetime.combine(datetime.today(), target_dt) + timedelta(hours=1)).time()

            if not (target_dt <= now_time <= end_dt):
                return
            self._last_briefing_attempt = now
            if now.weekday() >= 5:
                self.last_briefing_date = today_str    # 주말 — 보낼 것이 없다
                return

            if not alert_delivered("⏳ [시스템 알림] 간밤의 글로벌 마켓 데이터를 수집하고 "
                                   "AI 장전 브리핑을 작성 중입니다..."):
                logger.warning(f"[Scheduler] 장전 브리핑 예고 전달 미확인 — "
                               f"{self.NOTICE_RETRY_SEC}초 뒤 오늘 안에 다시 시도합니다")
                return
            threading.Thread(target=self.execute_briefing, daemon=True,
                             name="MorningBriefing").start()
            self.last_briefing_date = today_str
        except Exception as e:      # noqa: BLE001
            logger.error(f"Morning briefing check error — 다시 시도합니다: {e}")

    def _heartbeat_context(self):
        """사망 알림에 실을 상황 정보. 조회 비용이 있는 것은 넣지 않는다
        (이미 들고 있는 캐시값만 읽는다 — 하트비트가 API를 부르면 본말이 전도된다)."""
        try:
            #  [Fix 2026-09-04] 종전에는 마지막 줄이 분기 밖에 있어 무엇으로 떴든
            #   항상 "실전"으로 덮였다. 파이(가상투자)와 맥북(실전) 두 인스턴스를
            #   함께 돌리는데, 사망 알림이 둘 다 '실전'이라고 하면 어느 쪽이 죽었는지
            #   모른 채 실계좌부터 확인하게 된다. 알림의 값어치가 여기에 달려 있다.
            if getattr(config.session, 'is_paper', False):
                mode = "가상투자"
            elif getattr(config.session, 'is_toss', False):
                mode = "토스"
            else:
                mode = "실전"
        except Exception:
            mode = None
        return {
            "mode": mode,
            "instance": getattr(config.settings, 'TELEGRAM_INSTANCE_NAME', 'HTS'),
            "running": bool(getattr(self.trader, 'is_running', False)),
            "holdings": getattr(self.trader, 'last_holdings_count', None),
        }

    def _check_heartbeat(self):
        """1분 주기 하트비트 점검.

        두 층이다.
         (1) **프로세스 안** — 자동매매 스레드가 죽었는지·연속 에러가 한계인지 보고 알린다.
             프로세스가 살아 있어야 동작하므로, 프로세스 자체의 죽음은 볼 수 없다.
         (2) **프로세스 밖** — logs/heartbeat.json 에 '다음 도장을 언제까지 찍겠다'는
             약속을 남긴다. OOM 킬처럼 프로세스가 통째로 사라지면 이 약속이 지나가고,
             밖에서 도는 감시자(tools/hts_watchdog.py, cron)가 그때 알린다.
             되살리지는 않는다 — 알리기만 한다(modules/heartbeat.py 주석 참조).
        """
        now = time.time()
        if now - self.last_heartbeat_time > 60:
            self.last_heartbeat_time = now
            ctx = self._heartbeat_context()
            beat_ok = heartbeat.beat(interval_sec=60, running=ctx["running"], mode=ctx["mode"],
                                     instance=ctx["instance"], holdings=ctx["holdings"])
            is_problem = False
            msg = ""

            #  [추가 2026-09-07] **감시 장치 자신이 고장 났다.** 도장을 못 찍으면 밖의
            #   감시자(cron)는 곧 살아 있는 이 프로세스를 사망으로 판정한다. 그 알림을
            #   받은 운영자는 접속해서 프로세스가 멀쩡한 것을 보고 '감시자가 이상하다'고
            #   결론짓는다 — 진짜 원인(SD 카드 가득참·IO 오류)은 어디에도 안 보인다.
            #   프로세스는 아직 살아 있으니 여기서는 텔레그램이 나간다. 그때 말해 준다.
            #   한 번의 실패로 울리지는 않는다(순간 IO 지연) — 연속으로 이어질 때만.
            if beat_ok is False:
                streak = heartbeat.beat_failure_streak(mode=ctx["mode"])
                if streak >= self.BEAT_FAIL_ALERT_STREAK:
                    is_problem = True
                    msg = (f"생존 신호 파일을 {streak}회 연속 쓰지 못했습니다. 프로세스는 "
                           f"살아 있지만 밖의 감시자는 곧 '사망'으로 알립니다 — 그 알림은 "
                           f"거짓입니다. 디스크 여유 공간과 logs/ 쓰기 권한을 확인하세요.")
            
            if self.trader.is_running and self.trader.thread and not self.trader.thread.is_alive():
                is_problem = True
                msg = "자동매매 스레드가 예기치 않게 종료되었습니다. (크래시 의심)"
                
            max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
            if self.trader.consecutive_errors >= max_err:
                is_problem = True
                msg = f"시스템 연속 에러가 한계치({max_err}회)에 도달했습니다."

            # [추가 2026-09-04] **멈춘 채 살아 있는 루프**. 위 두 검사에 걸리지 않는다 —
            #  스레드는 is_alive() 참이고, 예외가 안 나므로 연속 에러도 0이며, 하트비트를
            #  찍는 것은 매매 스레드가 아니라 이 스케줄러 스레드다. 그동안 손절·트레일링
            #  감시가 통째로 멈춰 있는데 어느 층도 소리를 내지 않았다.
            #  지연 자체는 종전에도 계산됐지만 상태 화면 안에만 있어 운영자가 열어야 보였다.
            try:
                stall = self.trader.loop_stall_seconds()
                limit = self.trader.loop_stall_threshold()
            except Exception as e:      # noqa: BLE001 - 감시가 감시를 죽이지 않게
                logger.debug(f"[하트비트] 루프 정체 점검 실패: {e}")
                stall = None
            if stall is not None and stall > limit:
                is_problem = True
                msg = (f"자동매매 루프가 {int(stall)}초째 한 주기를 끝내지 못했습니다"
                       f"(정상 간격의 5배인 {int(limit)}초 초과). 스레드는 살아 있으나 "
                       f"손절·트레일링 감시가 멈춰 있을 수 있습니다.")
                
            # [추가 2026-09-06] **매매 스레드 말고 나머지 감시 스레드들.** 종전에는 이
            #  점검이 self.trader.thread 하나만 봤다. 프로세스가 살아 있으면 밖의 cron
            #  감시자(tools/hts_watchdog.py)는 아무 이상을 못 보고, 감시기 자신은
            #  is_running 이 True 라 스스로 '실행 중'이라 답한다 — 죽은 채로 영원히
            #  살아 있는 상태가 어느 층에도 보이지 않았다.
            dead = self._dead_monitor_threads()
            if dead:
                is_problem = True
                msg = ("감시 스레드가 예기치 않게 종료되었습니다: "
                       + ", ".join(f"{n}({w})" for n, w in dead))

            if not hasattr(self, '_last_problem_msg'):
                self._last_problem_msg = ""
                
            if is_problem:
                if self._last_problem_msg != msg:
                    api.send_telegram_message(f"🚨 [시스템 하트비트 이상 감지]\n{msg}\n서버 접속 후 시스템 상태를 확인해주세요.")
                    self._last_problem_msg = msg
            else:
                self._last_problem_msg = ""
                
    #  점검 대상 감시 스레드. (표시 이름, 잃는 것, 싱글톤을 얻는 함수, 스레드 속성)
    #   여기 없는 스레드는 죽어도 아무도 모른다 — 새 감시 루프를 만들면 여기에 적는다.
    def _monitor_threads(self):
        specs = []
        try:
            from modules import auto_trade
            specs.append(("체결 감시", "체결 확정이 멈춰 매수가 원장에 오르지 않습니다",
                          auto_trade.ConclusionMonitor(), "thread"))
        except Exception as e:      # noqa: BLE001 - 감시가 감시를 죽이지 않게
            logger.debug(f"[하트비트] 체결 감시 상태를 읽지 못했습니다: {e}")
        try:
            from modules.reserved_order_monitor import ReservedOrderMonitor
            specs.append(("예약 주문 감시", "예약 손절·익절이 발동하지 않습니다",
                          ReservedOrderMonitor(), "monitor_thread"))
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[하트비트] 예약 주문 감시 상태를 읽지 못했습니다: {e}")
        try:
            from modules import journal_sync
            specs.append(("매매일지 연동", "매매일지 전송이 큐에 쌓이기만 합니다",
                          journal_sync.JournalSyncWorker(), "thread"))
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[하트비트] 매매일지 연동 상태를 읽지 못했습니다: {e}")
        return specs

    def _dead_monitor_threads(self):
        """'돌아야 하는데 죽은' 감시 스레드 목록. [(이름, 잃는 것), ...]

        판정은 세 조건이 모두 맞을 때만이다 — 스스로 돌고 있다고 말하고(is_running),
        스레드 객체가 있으며, 그 스레드가 죽어 있다. 아직 안 띄운 감시기(thread=None)나
        stop() 으로 내려간 것(is_running=False)은 사고가 아니다.
        """
        dead = []
        for name, what_is_lost, obj, attr in self._monitor_threads():
            try:
                if not getattr(obj, 'is_running', False):
                    continue
                thread = getattr(obj, attr, None)
                if thread is not None and not thread.is_alive():
                    dead.append((name, what_is_lost))
            except Exception as e:  # noqa: BLE001 - 감시가 감시를 죽이지 않게
                logger.debug(f"[하트비트] {name} 스레드 상태 점검 실패: {e}")
        return dead

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
            target_cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                deposit = res['d2_deposit'] if res else 0
                
                #  holdings is None = 조회 실패다. '보유 없음'이라고 알리면 운영자는
                #  포지션이 정리된 줄 안다 — 마감 후 갭 전에 가장 비싼 오해다([[unknown-vs-empty]]).
                if holdings is None:
                    api.send_telegram_message(
                        "⚠️ 잔고를 조회하지 못해 장 마감 브리핑을 만들지 못했습니다.\n"
                        "(보유 종목이 없다는 뜻이 아닙니다 — 증권사 API 응답 실패)")
                    return
                valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []
                if not valid_holdings:
                    api.send_telegram_message("📭 보유 종목이 없어 장 마감 브리핑을 수행할 수 없습니다.")
                    return
                    
                #  [Fix 2026-09-06] 하드 서브스크립트 하나가 **브리핑 전체**를 죽였다.
                #   KIS 는 값이 없을 때 키를 주고 빈 문자열을 담는다 — int('') 는 ValueError 다.
                #   한 종목이 이상하다고 그날의 마감 브리핑을 통째로 잃을 이유는 없다.
                tot_evlu = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
                total_asset = deposit + tot_evlu

                portfolio_str = f"총 자산: {total_asset:,}원 (보유 {len(valid_holdings)}종목)\n\n[보유 종목 비중]\n"
                for item in valid_holdings:
                    name = item.get('prdt_name') or item.get('pdno') or '?'
                    eval_amt = api.safe_int(item.get('evlu_amt'))
                    weight = (eval_amt / total_asset * 100) if total_asset > 0 else 0.0
                    rate_raw = api.safe_float(item.get('evlu_pfls_rt'), default=None)
                    #  수익률을 모르면 0%('변동 없음')로 적지 않는다 — AI 브리핑의 입력이다.
                    rate_str = f"수익률 {rate_raw:+.2f}%" if rate_raw is not None else "수익률 모름"
                    portfolio_str += f"- {name}: 비중 {weight:.1f}% ({rate_str})\n"
                    
                report = theme_analysis.generate_daily_closing_report(portfolio_str)
                if report:
                    if report.startswith("⚠️"): api.send_telegram_message(report)
                    else: api.send_telegram_message(f"🛡️ [AI 장 마감 종합 브리핑]\n\n{report}")
                else:
                    api.send_telegram_message("⚠️ AI 장 마감 브리핑 생성에 실패했습니다.")
        except Exception as e:
            logger.error(f"Daily closing report error: {e}")
            api.send_telegram_message("⚠️ 장 마감 브리핑 생성 중 오류가 발생했습니다.")