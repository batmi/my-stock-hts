"""AutoTrader 의 주 루프 — 한 주기(장 상태·세션 전환·날짜 변경 → 매도·매수 판정 → 미체결 관리 → 계좌 감시)와 그 둘레.

  · 주 루프(_run_loop): 체결 감시 킬스위치, 운용 계좌 표기, 자정 날짜 변경(방어 모드 해제·백업·시작 자산 재측정),
    세션 전환·개장/마감/단일가 알림, 마감 뒤 스냅샷·청산 신호 점검, 잔고 0건 재확인, 주기 설정 스냅샷 안의 매도·매수·
    미체결 관리, 주기 사이 대기의 미체결 확인
  · 루프 예외: 연속 에러가 한도에 닿으면 서버가 정말 죽었는지 먼저 본다(_errors_are_not_the_server — 정상이면
    대기하지 않는다), 죽었으면 복구를 기다린다(_wait_for_server_recovery)
  · 주기 둘레: 주기 소요 계측(_record_cycle_duration), 메모리 경보(_warn_if_memory_low·_health_memory·_peak_rss_mb),
    정체 감시 값(loop_stall_seconds·loop_stall_threshold — 스케줄러가 부른다), DB 쓰기 실패 알림
    (_check_db_write_failures), 가상투자 마감 스냅샷(_snapshot_paper_closing_equity)

[왜 나눴나 · 2026-10-25 C1 6단계] C1 의 마지막 단계로 trader.py 에 남은 주기 제어를 떼어 냈다(기동·정지는
 lifecycle.py). 주 루프는 매도·매수·계좌 감시를 부르는 순서와 예외 처리만 들고 있어, 판정 코드와 따로 읽혀야 한다.

[어떻게 나눴나] 1~5단계와 같은 규약이다. 메서드 본문은 옮기면서 바꾸지 않았다. 바꾼 곳은 한 군데다.
 _health_memory(정적 메서드)가 클래스 이름으로 부르던 `AutoTrader._peak_rss_mb()` 두 곳을 `TraderLoopMixin._peak_rss_mb()`
 로 바꿨다. 이 모듈이 AutoTrader 를 import 하면 순환이 된다. 그 메서드를 바꿔 끼우는 코드는 없어 부르는 함수는 같다.
 주 루프만 쓰던 모듈 수준 이름(알림 억제 간격 둘·세션 비교 키 session_phase_key)도 함께 옮겼고, trader 가 다시
 import 해 trader.session_phase_key · trader.CODE_ERROR_ALERT_COOLDOWN 으로 부르는 곳이 그대로 동작한다. 모듈 전역
 이름은 trader.py 와 같은 객체를 가리키게 import 했고, 로거도 trader 와 같은 이름을 쓴다. 장 열림 판정(is_market_open)·
 로그(log)·방어 모드 전환은 여러 믹스인이 함께 써서 trader 에 남겼다.
 동등성 기준은 주기 재생 기록 여섯 시나리오(tests/test_cycle_replay.py)이고, 예외·대기 경로는 lifecycle 시나리오가 지난다.

[주의] 테스트에서 이 메서드들이 쓰는 전역을 바꿔 끼울 때는 trader 가 아니라 이 모듈을 지정한다
 (예: modules.auto_trade.loop.time.sleep, modules.auto_trade.loop.get_mystock_log_tail). 패키지 네임스페이스 경유
 호출은 _pkg() 그대로다.
"""
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

import api
import config
from core import context
from core import utils
from modules import account
from modules import db_manager
from modules.auto_trade.common import (_enrich_rules_with_weights, _get_trade_account, get_mystock_log_tail,
                                       get_restricted_stocks, is_plausible_baseline, is_single_price_break,
                                       save_daily_initial_asset)

console = config.console

#  트레이더 로그와 같은 이름 — 주 루프가 남기는 로그도 'modules.auto_trade.trader' 로 모인다.
logger = logging.getLogger("modules.auto_trade.trader")


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자 — trader._pkg 와 같은 규약.

    테스트의 patch('modules.auto_trade.X') 가 이 모듈 안의 호출에도 걸리게 한다. (지연 import 라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


#  같은 DB 쓰기 실패를 다시 알리기까지의 간격(초). 디스크가 차면 매 주기 실패하므로
#  억제하지 않으면 알림이 도배되어 정작 중요한 경보가 묻힌다.
DB_WRITE_FAIL_ALERT_COOLDOWN = 1800.0
#  '서버는 정상인데 코드가 터진다'를 다시 알리기까지의 간격(초). 이 상태는 대기로
#  숨겨지지 않고 매 주기 반복되므로, 억제하지 않으면 알림이 도배된다.
CODE_ERROR_ALERT_COOLDOWN = 1800.0


#  휴장 판정은 달력일 기준이라 자정에 뒤집힌다(일 23:59 'holiday' → 월 00:00 'closed').
#  둘 다 거래가 없는 같은 상태인데 문자열만 달라 자정마다 '시장 상태 변경' 알림이 나갔다
#  (실측 2026-08-03 00:00 "장 마감 · KRX 종가"). 알림 비교에서는 한 상태로 묶는다.
_IDLE_SESSION_PHASES = frozenset({'closed', 'holiday'})


def session_phase_key(phase):
    """세션 전환 알림용 비교 키. 거래 없는 단계(마감·휴장)는 하나로 접는다."""
    return 'idle' if phase in _IDLE_SESSION_PHASES else phase


class TraderLoopMixin:
    """AutoTrader 의 주 루프 메서드. 단독으로 쓰지 않는다 — 트레이더 상태(self.order_manager·self.thread …)를 쓴다."""

    #  이 믹스인이 기대는 트레이더 상태·메서드(정의·초기값은 AutoTrader 와 다른 믹스인). 값 없는 선언이라 실행 시에는
    #  그쪽 것이 그대로 쓰인다. getattr 기본값으로 읽는 상태와 이 믹스인이 직접 쓰는 상태는 빠져 있다.
    is_running: bool
    thread: Any
    buy_halted: bool
    order_manager: Any
    log: Callable[..., None]
    is_market_open: Callable[..., bool]
    resume_buys: Callable[..., bool]
    _check_sell_conditions: Callable[..., Any]
    _check_buy_conditions: Callable[..., Any]
    _monitor_account_status: Callable[..., Any]
    _get_holdings_message: Callable[..., str]
    _health_cycle_text: Callable[..., Any]
    _scan_after_hours_sell_signals: Callable[..., Any]
    _update_market_indices_status: Callable[..., Any]
    _update_risk_scale: Callable[..., Any]
    _update_vol_regime: Callable[..., Any]

    def _check_db_write_failures(self):
        """새 쓰기 실패가 생겼으면 알린다(같은 원인 반복은 억제).

        DB 계층은 알림을 보내지 않는다(계층 분리). 대신 카운터를 여기서 읽어 올린다.
        """
        try:
            h = db_manager.db.get_write_failures()
        except Exception:
            return
        seen = getattr(self, '_db_write_fail_seen', 0)
        if h['count'] <= seen:
            return
        self._db_write_fail_seen = h['count']

        now = time.time()
        if now - getattr(self, '_db_write_fail_alerted_at', 0.0) < DB_WRITE_FAIL_ALERT_COOLDOWN:
            return

        free_mb = db_manager.db.disk_free_mb()
        msg = (f"DB 쓰기 실패 누적 {h['count']}건 (최근: {h['last_op']} — {h['last_error']})\n"
               f"디스크 여유 {free_mb:,.0f}MB\n"
               f"트레일링 최고가가 저장되지 않으면 재기동 후 청산선이 어긋납니다.")
        self.log(f"[DB 쓰기 실패] {msg}")
        #  [전달 확인 뒤 쿨다운 · 2026-09-07] 종전에는 보내기 전에 쿨다운을 찍었다.
        #   비동기 전송이라 실패가 예외로 오지 않으므로, 못 닿아도 그 쿨다운 동안 침묵한다.
        #   내용이 '디스크가 차서 트레일링 최고가가 저장되지 않는다' 라 놓치면 비싸다.
        if _pkg().alert_delivered(f"⚠️ [DB 쓰기 실패] {msg}", urgent=True):
            self._db_write_fail_alerted_at = now

    def _record_cycle_duration(self, secs, log=True):
        """[계측] 한 모니터링 주기의 소요 시간을 기록한다.

        SYSTEM_TRADING_INTERVAL은 주기가 끝난 뒤 쉬는 시간이므로, 실제 청산 감시 간격은
        (이 소요 시간 + interval)이다. 관심종목을 늘리면 후보 분석이 길어져 이 값만 커지고,
        그만큼 손절·트레일링 확인이 늦어진다. 유니버스를 어디까지 늘릴 수 있는지는
        수익률이 아니라 이 값이 정한다.

        최근 30회만 보관한다(라즈베리파이 1GB 환경에서 무한 증가 방지).
        """
        try:
            secs = float(secs)
        except (TypeError, ValueError):
            return
        if secs < 0:
            return
        self.last_cycle_secs = secs
        hist = getattr(self, 'cycle_secs_history', None)
        if hist is None:
            hist = self.cycle_secs_history = []
        hist.append(secs)
        if len(hist) > 30:
            del hist[:-30]
        if secs > getattr(self, 'cycle_secs_peak', 0.0):
            self.cycle_secs_peak = secs
        if log:
            interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
            self.log(f"모니터링 완료 (소요 {secs:.1f}초 · 다음 주기까지 {interval}초 대기 "
                     f"→ 청산 감시 간격 {secs + interval:.0f}초). 대기 중...")
        self._warn_if_memory_low()

    def _warn_if_memory_low(self):
        """가용 메모리가 OOM 문턱에 다가서면 **먼저 알린다.**

        [왜 · 2026-09-08 감사] 문턱(경고 120MB · 위험 60MB)은 이미 있었는데
         `get_health_message()` 안에만 있었다 — 사람이 `/health` 를 **쳐야** 보이는
         값이다. 그런데 OOM 은 종목 분석이 몰리는 순간에 나고, 그 순간 화면을 보고 있는
         사람은 없다(헤드리스 운영). 결국 메모리는 늘 사후에만 알려졌다: 프로세스가
         죽고, 밖의 cron 감시자가 사망을 통보하는 순서다([[deployment-raspberry-pi]]).
         알림 인프라는 이미 다 있으므로 여기서 한 번 밀어 준다.

        [왜 주기 끝인가] 이 자리가 한 주기의 최대 사용 직후다 — 후보 분석·차트가 방금
         돌았으므로 여유 메모리가 가장 적게 나온다. 한가한 시각의 값으로 안심하지 않는다.

        같은 등급으로는 하루 한 번만 보낸다. 매 주기(기본 60초) 보내면 경보가 소음이 되고,
        정작 다음 사고를 놓친다. 전달을 **확인한 뒤에** 표식을 남긴다([[unknown-vs-empty]]
        와 같은 이유 — 못 닿은 알림을 '보냈다'로 굳히면 그날은 영영 조용하다).
        """
        try:
            _rss, avail_mb, peak_mb = self._health_memory()
            if not avail_mb:
                return          # 비리눅스 등 — 못 재는 것은 경보하지 않는다
            warn_at = float(getattr(config, 'SYSTEM_MEMORY_WARN_MB', 120))
            risk_at = float(getattr(config, 'SYSTEM_MEMORY_RISK_MB', 60))
            level = "위험" if avail_mb < risk_at else ("경고" if avail_mb < warn_at else None)
            if level is None:
                self._mem_alert_key = None      # 회복 — 다음 악화는 다시 알린다
                return

            key = (level, datetime.now().strftime("%Y-%m-%d"))
            if getattr(self, '_mem_alert_key', None) == key:
                return
            msg = (f"⚠️ [메모리 {level}] 가용 메모리 {avail_mb:,.0f}MB "
                   f"(문턱 {risk_at if level == '위험' else warn_at:,.0f}MB)\n"
                   f"프로세스 피크 {peak_mb:,.0f}MB · 방금 주기 직후 측정값입니다.\n"
                   f"이대로면 OOM 으로 프로세스가 종료될 수 있습니다 — 종료되면 손절·"
                   f"트레일링 감시가 함께 멈춥니다. 관심종목 수를 줄이거나 재기동을 검토하세요.")
            if _pkg().alert_delivered(msg, urgent=(level == "위험")):
                self._mem_alert_key = key
            self.log(msg.splitlines()[0])
        except Exception as e:      # noqa: BLE001 - 경보 실패가 매매 주기를 멈추지 않는다
            logger.debug(f"[Health] 메모리 경보 확인 실패: {e}")

    def loop_stall_seconds(self):
        """마지막 '정상 루프 완료' 이후 흐른 초. 감시 대상이 아니면 None.

        [왜 필요한가 · 2026-09-04] 프로세스 죽음은 두 층이 본다 — 스케줄러가 스레드
         생존을, 밖의 감시자(hts_watchdog)가 하트비트 파일을 본다. 그런데 **루프가
         멈춘 채 스레드는 살아 있는 경우**는 둘 다 못 본다. 하트비트를 찍는 것은
         스케줄러 스레드지 매매 스레드가 아니고, 멈춘 스레드도 is_alive() 는 참이며,
         예외가 안 나므로 연속 에러도 0이다. 그동안 **손절·트레일링 감시가 통째로
         멈춰 있는데 아무 소리도 나지 않는다.**
         종전에도 이 지연은 계산됐지만 `get_status_message()` 안에만 있어, 운영자가
         상태 화면을 열어야 보였다.
        """
        if not self.is_running or getattr(self, 'waiting_for_server', False):
            return None            # 정지·의도된 대기는 정체가 아니다
        last = getattr(self, 'last_success_at', None)
        if not isinstance(last, datetime):
            return None            # 첫 주기 완료 전 — 여기서 판정하지 않는다
        return (datetime.now() - last).total_seconds()

    def loop_stall_threshold(self):
        """이 초를 넘기면 '루프가 멈췄다'로 본다.

        고정값을 쓰면 안 된다 — 관심종목이 늘면 한 주기가 그만큼 길어져(_record_cycle_duration)
        정상인데도 넘긴다. 실제 감시 간격(분석 평균 + 대기)의 5배를 쓰되 하한을 둔다.
        """
        _, gap = self._health_cycle_text()
        interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
        base = gap if gap else (interval * 2)
        return max(300.0, float(base) * 5)

    @staticmethod
    def _peak_rss_mb():
        """프로세스 수명 동안의 최대 상주 메모리(MB). 실패하면 0.

        [왜 피크가 따로 필요한가] 현재 RSS는 '지금 이 순간'의 값이라 한가한 시각에 보면
        낮게 나온다. OOM은 종목 분석이 몰리는 순간에 나는데 그 순간을 사람이 보고 있을
        수는 없다. ru_maxrss는 커널이 대신 기억해 준 고점이라 사후에 확인할 수 있다.

        [주의] 프로세스 수명 기준이므로 재기동하면 0부터 다시 쌓인다 — 갓 띄운 직후의
        피크는 아직 아무것도 말해 주지 않는다. 하루 장을 다 돈 뒤의 값이라야 근거가 된다.
        """
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS는 바이트, 그 외 POSIX는 KB 단위로 반환한다.
            is_mac = getattr(os, "uname", None) and os.uname().sysname == "Darwin"
            return usage / (1024 ** 2) if is_mac else usage / 1024
        except Exception:
            return 0.0

    @staticmethod
    def _health_memory():
        """프로세스/시스템 메모리 사용량(MB)을 추가 의존성 없이 조회한다.

        운영 환경(라즈베리파이 1GB)에서는 OOM이 자동매매 중단의 주된 원인이라
        관제 화면에서 상주 메모리·피크·가용 메모리를 함께 확인할 수 있게 한다.
        조회에 실패하면 0을 돌려주고 해당 항목만 표시에서 빠진다.
        """
        rss_mb = 0.0
        avail_mb = 0.0
        try:
            # 리눅스는 statm의 상주 페이지 수가 가장 정확하고 비용도 낮다.
            with open("/proc/self/statm") as fp:
                pages = int(fp.read().split()[1])
            rss_mb = pages * os.sysconf("SC_PAGE_SIZE") / (1024 ** 2)
        except Exception:
            # statm을 못 읽는 환경(비리눅스)에서는 피크값으로 대신한다 — 현재값보다 크지만
            # 아예 표시하지 못하는 것보다 낫고, OOM 판단에서 과소평가는 위험한 방향이다.
            rss_mb = TraderLoopMixin._peak_rss_mb()
        peak_mb = TraderLoopMixin._peak_rss_mb()
        try:
            with open("/proc/meminfo") as fp:
                for line in fp:
                    if line.startswith("MemAvailable:"):
                        avail_mb = int(line.split()[1]) / 1024
                        break
        except Exception:
            avail_mb = 0.0
        return rss_mb, avail_mb, peak_mb

    def _run_loop(self):
        my_thread = threading.current_thread()
        while self.is_running and self.thread is my_thread:
            try:
                self.last_cycle_at = datetime.now()
                target_cano = config.session.auto_cano
                with utils.AccountContext(target_cano):
                    current_market_status = self.is_market_open()
                    is_log_needed = current_market_status or getattr(self, '_first_loop_flag', True) or (self.was_market_open != current_market_status)
                    self._first_loop_flag = False
                    
                    if is_log_needed:
                        self.log("모니터링 주기 시작...")
                    
                    # [추가] Kill Switch: 체결 감시 시스템 상태 점검
                    # 체결 확인이 불가능한 상태에서는 신규 주문도 위험하므로 중단
                    conclusion_monitor = _pkg().ConclusionMonitor()
                    if not conclusion_monitor.is_healthy():
                        # 모니터를 즉시 깨워 재점검 유도 — 서버가 정상이면 카운터가 0으로 리셋되어
                        # 스스로 회복된다 (모니터가 조회를 쉬는 동안 카운터가 얼어붙는 교착 방지)
                        conclusion_monitor.check_now()
                        raise Exception(f"체결 감시 시스템 불안정 (연속 에러 {conclusion_monitor.consecutive_errors}회)")
                    
                    # [수정] 매 사이클 시작 시점에 수행하던 일일 손실 한도 강제 체크 로직 제거
                    # API Rate Limit 발생 시 잔고가 누락되어 가짜 비상 정지를 유발할 수 있으므로,
                    # API 호출 성공이 보장된 루프 후반부(_monitor_account_status)에서만 안전하게 손실 한도를 체크함
                    
                    # [추가] 현재 운용 계좌 정보 로깅
                    if target_cano and is_log_needed:
                        # [수정] 토스/모의는 단일계좌라 시스템 트레이딩 계좌 = 기본 계좌.
                        #        모드 플래그를 보지 않으면 토스가 '한투증권(자동)'으로 오표시되므로 is_toss도 분기.
                        # [Fix] 가상투자(mode 1)도 자기 분기가 없어 '한투증권(자동)'으로 떨어졌다.
                        #  실전 시세를 쓸 뿐 계좌는 가상이므로 실전 자동매매 계좌로 읽히면 위험하다.
                        #  라벨은 trading.py의 표기와 같은 '가상투자'로 맞춘다.
                        display_cano = target_cano
                        if getattr(config.session, 'is_paper', False):
                            acc_type = "가상투자"
                            # CANO는 fail-safe 센티널('PAPER')이라 계좌 식별에 쓸 수 없다.
                            #  VIRT_ACC_NUM을 표시 전용으로 읽어 어느 계좌 앞으로 도는지 남긴다.
                            _vc = getattr(config.session, 'virt_cano', '') or ''
                            _va = getattr(config.session, 'virt_acnt_prdt_cd', '') or ''
                            if _vc:
                                display_cano = f"{_vc}-{_va}" if _va else _vc
                        elif config.session.is_toss:
                            acc_type = "토스증권"
                        else:
                            #  [Fix 2026-09-06] 이 줄이 분기 **밖**에 있어 위에서 무엇을
                            #   골랐든 항상 "한투증권(자동)"으로 덮였다. 위 두 분기의 주석이
                            #   막으려던 것이 정확히 이 오표시인데, 마지막 한 줄이 그것을
                            #   무력화했다. 관제 화면(1874줄)과 하트비트(scheduler)에서
                            #   같은 모양을 각각 고쳤는데 주기 로그인 여기가 남아 있었다 —
                            #   가상투자·토스로 띄워도 매 주기 로그가 '한투증권(자동)'이라
                            #   말한다. 파이(가상)와 맥북(실전)을 함께 돌리는 운용에서
                            #   로그가 계좌 성격을 거짓말하면 장애 때 실계좌부터 뒤지게 된다.
                            acc_type = "한투증권(자동)"
                        self.log(f"운용 계좌: {display_cano} [{acc_type}]")
                    
                    
                    # [추가] 날짜 변경 감지 및 당일 기준 자산 재설정 (무중단 24시간 운용 지원)
                    current_date = datetime.now().date()
                    if current_date > self.last_log_date:
                        self.log("━" * 80)
                        self.log(f"📅 날짜 변경 감지: {self.last_log_date} -> {current_date}")
                        self.log("당일 기준 자산(initial_asset)을 새로 측정하여 갱신합니다.")
                        self.log("━" * 80)
                        self.last_log_date = current_date
                        self.initial_asset = 0
                        self.baseline_principal = 0  # [추가] 입금 감지 기준 원금도 당일 첫 측정 시 재산정되도록 리셋

                        # [안전장치] 일일 손실 한도는 '당일 시작 자산' 기준이므로, 기준이 재측정되는
                        #  날짜 변경 시점에 방어 모드(신규 매수 중단)도 함께 해제한다.
                        if self.buy_halted:
                            self.resume_buys(reason="날짜 변경 — 일일 손실 한도 기준 재설정")
                            api.send_telegram_message("🔄 [방어 모드 해제] 날짜가 변경되어 신규 매수를 재개합니다.")

                        # [Fix 2026-09-20 · 백업 복원 리허설] 백업은 기동 시 한 번(위 start 경로)뿐이었다.
                        #  무중단 운용에서는 재기동이 없으니 마지막 백업이 기동일에 멈춘다 — 파이가 한 달
                        #  돌면 한 달 묵은 백업만 남는다. 날짜가 바뀔 때 하루치를 뜬다(같은 날은 건너뛴다).
                        _bk = db_manager.db.backup()
                        self.log(f"[DB 백업] {os.path.basename(_bk)}" if _bk
                                 else "[DB 백업] 실패 — 백업 없이 진행합니다(운영자 확인 필요)")

                        try:
                            acnt = config.session.auto_acnt_prdt_cd
                            
                            # [수정] 해외 자산 누락 방지
                            asset_data = account.get_asset_status_data(target_cano, acnt)
                            #  [Fix 2026-09-06] 이 자리만 검사가 없었다. 같은 값을 쓰는
                            #   기동 경로(488줄)와 주기 갱신 경로(4353줄)는 둘 다
                            #   is_plausible_baseline 을 지나는데, **날짜 변경 갱신**만
                            #   `> 0` 이었다. 이 값은 그날의 손실 한도 분모이자 사이징
                            #   기준이고, 한 번 저장되면 하루 종일 고정된다.
                            #   게다가 집계가 일부 실패해도 tot_asset 은 숫자로 나온다 —
                            #   실측: 국내 잔고 조회만 실패하면 -36%(주식비중만큼). 이
                            #   시스템의 노출 상한이 40%라 0.5배 문턱에는 영영 닿지
                            #   않는다. 그래서 비율이 아니라 **결손 표식**을 본다.
                            _degraded = list((asset_data or {}).get('degraded') or [])
                            _acc_key = f"{target_cano}-{acnt}"
                            _tot = (asset_data or {}).get('tot_asset', 0)
                            if _degraded:
                                self.log(f"[기준선] 자산 집계가 온전하지 않아 갱신을 "
                                         f"미룹니다(결손: {', '.join(_degraded)}). "
                                         f"다음 주기에 다시 측정합니다.")
                            elif _tot > 0 and not is_plausible_baseline(_acc_key, _tot):
                                self.log(f"[기준선] 새 시작 자산 {_tot:,}원이 직전 이력과 "
                                         f"너무 어긋나 채택하지 않습니다. 다음 주기에 "
                                         f"다시 측정합니다.")
                            elif asset_data and _tot > 0:
                                self.initial_asset = asset_data['tot_asset']
                                saved_ok = save_daily_initial_asset(f"{target_cano}-{acnt}", self.initial_asset)
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                acc_str = f"{target_cano}-{acnt}"
                                db_ok = db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                                self.log(f"[초기화 완료] 새로운 당일 시작 자산 갱신: {self.initial_asset:,}원"
                                         + ("" if saved_ok else " ([bold red]파일 저장 실패[/] — 재기동 시 기준선 소실)")
                                         + ("" if db_ok else " ([bold red]이력 저장 실패[/] — 내일 드로다운 기준에서 오늘이 빠짐)"))
                        except Exception as e:
                            self.log(f"당일 시작 자산 갱신 실패: {e}")

                    # [추가] 장 시작/마감 상태 변경 감지 및 로그
                    # [추가] 국내장 세션 단계 전환 감지 및 텔레그램 알림
                    # [수정] 마감/휴장은 같은 '거래 없음'으로 접어 자정 날짜 변경만으로 알림이
                    #        나가지 않게 한다(session_phase_key 주석 참고).
                    current_phase = _pkg().session_phase_key(api.domestic_session_phase())
                    if self.last_session_phase is None:
                        self.last_session_phase = current_phase
                    elif self.last_session_phase != current_phase:
                        self.last_session_phase = current_phase
                        phase_label = api.market_session_label(False, False)
                        if phase_label:
                            phase_text = phase_label[0]
                            self.log(f"🔔 [시장 상태 변경] 세션 전환: {phase_text}")
                            msg = f"🔔 [시장 상태 변경]\n현재 시장 세션이 다음으로 전환되었습니다:\n👉 {phase_text}"
                            api.send_telegram_message(msg)

                    if self.was_market_open is not None:
                        if not self.was_market_open and current_market_status:
                            now_time_str = datetime.now().strftime("%H%M")
                            self.log("━" * 80)
                            if "0900" <= now_time_str < "0910":
                                self.log(f"📢 [정규장 시작] 정규 주식 시장 거래가 개시되었습니다. ({datetime.now().strftime('%H:%M')})")
                                msg = "🔔 [정규장 시작] 정규 주식 시장 거래가 개시되었습니다."
                            elif "1600" <= now_time_str < "1610":
                                # [2026-09-14] 15:30~16:00 은 시장 없는 휴게라 종전 '15:30 재개' 분기는 도달 불가.
                                #  거래 종료 시간을 2000 으로 넓힌 경우 16:00 에 KRX 애프터마켓으로 재개한다.
                                self.log(f"📢 [애프터마켓 개시] KRX 애프터마켓(16:00~20:00) 거래를 재개합니다. ({datetime.now().strftime('%H:%M')})")
                                msg = "🔔 [애프터마켓 개시] KRX 애프터마켓(16:00~20:00) 거래를 재개합니다. (ETF/ETN 제외)"
                            else:
                                self.log(f"📢 [거래 시작] 시스템 트레이딩 거래가 시작되었습니다. ({datetime.now().strftime('%H:%M')})")
                                msg = "🔔 [장 시작] 거래 가능 시간이 되었습니다."
                            self.log("━" * 80)
                            
                            msg += self._get_holdings_message(target_cano)
                            api.send_telegram_message(msg)
                        elif self.was_market_open and not current_market_status:
                            if is_single_price_break():
                                self.log("━" * 80)
                                self.log(f"⏸️ [휴게 시간] 거래소 단일가 매매 동기화를 위해 잠시 매매를 멈춥니다. ({datetime.now().strftime('%H:%M')})")
                                self.log("━" * 80)
                                
                                msg = f"⏸️ [휴게 시간] 거래소 단일가 매매 동기화를 위해 잠시 매매를 멈춥니다.\n(해당 시간: {datetime.now().strftime('%H:%M')} ~)"
                                api.send_telegram_message(msg)
                            else:
                                self.log("━" * 80)
                                self.log(f"💤 [거래 종료] 시스템 트레이딩 거래가 종료되었습니다. ({datetime.now().strftime('%H:%M')})")
                                self.log("━" * 80)
                                
                                msg = "🌙 [장 마감] 거래 시간이 종료되었습니다."
                                msg += self._get_holdings_message(target_cano)
                                api.send_telegram_message(msg)
                                
                                # [추가] 장 마감 후 AI 마감 브리핑 자동 실행 (기존 포트폴리오 진단 대체)
                                try:
                                    from modules.scheduler import SystemScheduler
                                    threading.Thread(target=utils.inherit_account_context(SystemScheduler().execute_daily_closing_report), daemon=True, name="DailyClosingReport").start()
                                    self.log("장 마감 종합 브리핑(AI) 작성을 백그라운드에서 시작합니다.")
                                except Exception as e:
                                    self.log(f"장 마감 브리핑 스케줄러 호출 실패: {e}")
                    
                    # [변경] 장 마감 시 분석 중단 (트래픽 감소)
                    if not current_market_status:
                        if is_log_needed:
                            if is_single_price_break():
                                self.log("시스템 상태: WAITING (단일가 매매 동기화 대기)")
                            elif api.is_holiday_today():
                                self.log("시스템 상태: WAITING (휴장일 - 분석 중지)")
                            else:
                                self.log("시스템 상태: WAITING (장 마감 - 분석 중지)")
                        self.was_market_open = current_market_status
                        # [관찰 모드] 종가 확정 후 마감 스냅샷 1회. 주기 스냅샷은 RUNNING
                        #  분기에만 있어 15:20 단일가 휴게부터는 찍히지 않는다.
                        if getattr(config.session, 'is_paper', False):
                            self._snapshot_paper_closing_equity()
                        # [관측성] 마감 후 청산 신호 1회 스캔. 분석을 통째로 멈추면 트래픽은
                        #  아끼지만, 종가가 확정된 뒤 손절·트레일링선을 이탈한 사실을 아무도
                        #  모르는 채로 다음 개장까지 간다 — 갭이 그대로 손실이 되는 구간이다.
                        #  주문은 내지 않는다(낼 수 없다). 알림만 보낸다.
                        self._scan_after_hours_sell_signals(target_cano)
                    else:
                        status_msg = "RUNNING"
                        self.log(f"시스템 상태: {status_msg}")
                        
                        # [추가] 시장 지수 상태 업데이트 (KOSPI/KOSDAQ)
                        if getattr(config, 'USE_MARKET_FILTER', True):
                            self._update_market_indices_status()

                        # [리스크 스케일링] 약세 국면·계좌 드로다운 반영 리스크 한도 배수 갱신 (주기당 1회)
                        self._update_risk_scale()

                        # [동적 손절 캡] 지수 변동성 배율 — 시장 필터 설정과 무관하게 갱신한다(값은 하루 한 번
                        #  바뀐다). 매도·매수 검사보다 먼저 — 신규 진입·증액의 손절률이 이 캡을 본다.
                        self._update_vol_regime()

                        # [관찰 모드] 가상 자산 일별 스냅샷 — 자산곡선·MDD 산출의 유일한 소스다.
                        #  같은 날 재호출은 덮어쓰므로 주기마다 불러도 하루 1행만 남는다.
                        if getattr(config.session, 'is_paper', False):
                            from modules import paper_broker
                            paper_broker.snapshot_equity()

                        # [최적화] 계좌 정보(잔고, 예수금)를 루프 시작 시 1회만 조회하여 공유
                        # 2 TPS 환경에서 중복 조회를 방지하여 성능 확보
                        acnt = config.session.auto_acnt_prdt_cd
                        
                        # 1. 잔고 조회
                        # [수정] 초기 구동 시 메인 스레드에서 조회한 데이터 재사용 (API 호출 절약)
                        if self.initial_holdings is not None:
                            holdings = self.initial_holdings
                            summary = self.initial_summary
                            self.initial_holdings = None
                            self.initial_summary = None
                        else:
                            holdings, summary = api.get_domestic_balance(target_cano, acnt)
                        
                        # [수정] 잔고 조회 실패 시 예외 발생 (Kill Switch 연동)
                        # 계좌 상태를 모르는 상태에서 매매를 진행하는 것은 위험함
                        if holdings is None:
                            raise Exception("잔고 조회 실패 (API 응답 없음)")

                        # [안전장치] '빈 잔고'는 조회 실패와 구분되지 않는다.
                        #  rt_cd='0' + output1=[] 는 정상적인 '보유 없음'이기도 하지만, 토스가
                        #  items를 비워 응답하거나 페이징이 어긋나면 같은 모양이 된다. 이때
                        #  len(holding_codes)==0 이 되어 매수 슬롯이 전부 열리므로, 실제로는
                        #  보유 중인데 SYSTEM_MAX_HOLDINGS 만큼 추가 매수가 나갈 수 있다.
                        #  → 직전 주기에 보유가 있었는데 0건이 되면 이번 주기 매수를 보류하고
                        #    다음 조회에서 다시 0건일 때만 진짜 청산으로 수용한다(1주기 지연).
                        #  매도는 막지 않는다. 보유가 없으면 어차피 할 일이 없고, 실재한다면
                        #  다음 주기에 다시 잡혀 손절 판정이 늦어질 이유가 없다.
                        #  재확인은 last_holdings_count 만으로 성립한다. 보류한 주기에 이 값이
                        #  0으로 내려가므로, 다음 주기가 또 0건이면 조건이 성립하지 않아 통과한다.
                        balance_unconfirmed = False
                        if not holdings and self.last_holdings_count > 0:
                            balance_unconfirmed = True
                            self.log(f"[잔고 확인] 보유 {self.last_holdings_count}건 → 0건. "
                                     f"조회 이상 가능성으로 이번 주기 매수를 보류하고 재확인합니다.")
                            logger.warning(f"[잔고 확인] 보유 종목이 {self.last_holdings_count}건에서 "
                                           f"0건으로 바뀌어 매수를 1주기 보류")
                        self.last_holdings_count = len(holdings)

                        # 2. 예수금 조회
                        # [최적화] 모의투자는 잔고 조회 결과(summary)에 예수금이 포함되어 있어 별도 호출 불필요
                        deposit_res = None
                        # [수정] 실전/모의 모두 summary 정보 우선 활용
                        if summary:
                            dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                            d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                            deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
                        
                        # 예수금이 0이거나 실전투자에서 정밀 조회가 필요한 경우 Fallback
                        if (not deposit_res or deposit_res['deposit'] == 0):
                            deposit_res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        
                        # API 호출 간격 조절 (Rate Limit 방지)
                        time.sleep(0.2)

                        # [최적화] 이번 주기 주문 활동 감지용 스냅샷
                        #  (주문이 하나도 없었다면 루프 말미 잔고/예수금 재조회를 생략해 REST 콜 절감)
                        with self.order_manager._lock:
                            _pending_before = bool(self.order_manager.pending_orders)
                        _sent_before = self.order_manager.orders_sent_count

                        # [최적화] 개별 룰(DB)·트레이딩 제한 종목(파일)을 주기당 1회만 로드해
                        #  매도/매수 검사에 공유 (기존: 각 검사가 개별 로드 → DB 연결·파일 I/O 2회씩)
                        _cycle_rules = _enrich_rules_with_weights(db_manager.db.get_all_stock_strategies())
                        _cycle_rules_map = {r['code']: r for r in _cycle_rules}
                        _cycle_cano, _cycle_acnt = _get_trade_account()
                        _cycle_restricted = get_restricted_stocks(_cycle_cano, _cycle_acnt)

                        # [수정] 락 범위 축소: 전체 로직을 감싸던 락 제거 (api.call_api 내부 락 활용)
                        # [주기 설정 스냅샷 · 2026-10-16] 아래 세 단계는 시작 시점의 설정 한 벌로
                        #  판단한다 — 도중에 메뉴·텔레그램이 바꾼 값은 다음 주기에 반영된다
                        #  (config.cycle_snapshot 주석). 워커 풀은 inherit_account_context 로 물려받는다.
                        with config.cycle_snapshot():
                            # 1. 매도 조건 점검 (리스크 관리)
                            self._check_sell_conditions(holdings, current_market_status, rules_map=_cycle_rules_map, restricted_stocks=_cycle_restricted)
                            # 2. 매수 조건 점검 ([안전장치] 잔고 0건 재확인 대기 중에는 건너뛴다)
                            if not balance_unconfirmed:
                                self._check_buy_conditions(holdings, deposit_res, rules_map=_cycle_rules_map, restricted_stocks=_cycle_restricted)
                            # 3. 미체결 주문 관리 (오래된 주문 취소) - 장 중에만 수행
                            self.order_manager.manage_unfilled_orders()
                        # 4. DB 쓰기 실패 확인 — 실패는 인메모리 캐시에 가려 세션 중엔
                        #    안 보이고, 재기동해야 소실이 드러난다. 그 전에 알린다.
                        self._check_db_write_failures()

                        # [수정] 루프 동안 매수/매도가 발생한 경우에만 최종 로깅 전 잔고와 예수금을 갱신.
                        #  주기 시작 시(직전) 조회한 스냅샷이 있고 주문 활동이 전혀 없었다면 계좌 상태가
                        #  변하지 않았으므로 재조회를 생략한다 (2 TPS 모의 환경에서 주기당 2~3콜+0.7초 절감).
                        with self.order_manager._lock:
                            _pending_after = bool(self.order_manager.pending_orders)
                        _had_order_activity = (
                            _pending_before or _pending_after
                            or self.order_manager.orders_sent_count > _sent_before
                        )
                        if _had_order_activity:
                            time.sleep(0.5)
                            try:
                                upd_holdings, upd_summary = api.get_domestic_balance(target_cano, acnt)
                                if upd_holdings is not None:
                                    holdings = upd_holdings
                                    summary = upd_summary

                                upd_dep = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                                if upd_dep: deposit_res = upd_dep
                            except Exception as e:
                                logger.debug(f"최종 상태 로깅을 위한 잔고 갱신 실패: {e}")

                        # [추가] 보유 종목 상태 로깅 및 자산 안전장치 체크
                        self._monitor_account_status(holdings, summary, deposit_res)
                        
                        # [추가] 관심 종목 변경 및 분석 제외 종목 메모리 캐시 정리
                        try:
                            valid_codes = {h['pdno'] for h in holdings}
                            for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
                                for item in config.session.stock_data.get(key, []):
                                    valid_codes.add(item['code'])
                                    
                            context.prune_stock_states(valid_codes)
                        except Exception as e:
                            logger.debug(f"상태 캐시 정리 중 오류: {e}")
                    
                    self.was_market_open = current_market_status
                    
                    # [계측] 주기 소요 시간 — SYSTEM_TRADING_INTERVAL은 '주기 후 쉬는 시간'이므로
                    #  실제 감시 간격은 (이 소요 시간 + interval)이다. 관심종목을 늘리면 이 값이
                    #  길어지고 그만큼 손절·트레일링 감시가 늦어지므로, 유니버스 상한의 실질 기준이 된다.
                    self._record_cycle_duration((datetime.now() - self.last_cycle_at).total_seconds(),
                                                log=is_log_needed)

                interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
                
                # [수정] 미체결 주문 확인 시 발생하는 API 호출 지연(Delay)이 누적되어
                # 모니터링 주기가 설정값(180초)을 크게 초과하는 문제를 해결하기 위해 절대 시간 기반 대기 적용
                wait_start_time = time.time()
                last_unfilled_check = wait_start_time
                last_idle_unfilled_check = wait_start_time
                
                while self.is_running:
                    now = time.time()
                    if now - wait_start_time >= interval:
                        break
                        
                    # 5초 주기 도달 시
                    if now - last_unfilled_check >= 5:
                        last_unfilled_check = time.time()
                        
                        with self.order_manager._lock:
                            has_pending = bool(self.order_manager.pending_orders)
                            
                        # 시스템 내부에 대기 중인 미체결 주문이 있다면 5초 주기로 즉각 확인
                        # 없다면 외부 HTS 등 타 매체 주문 감지를 위해 60초 간격으로만 최소한의 API 확인 수행
                        if has_pending or (now - last_idle_unfilled_check >= 60):
                            self.order_manager.manage_unfilled_orders()
                            last_idle_unfilled_check = time.time()
                            
                    time.sleep(1)
                
                # 정상 루프 완료 시 에러 카운트 초기화
                self.consecutive_errors = 0
                self.last_success_at = datetime.now()
                    
            except Exception as e:
                self.last_error_at = datetime.now()
                self.last_error_message = str(e)
                self.consecutive_errors += 1
                max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
                self.log(f"에러 발생({self.consecutive_errors}/{max_err}): {str(e)}")
                logger.error(f"시스템 트레이딩 루프 예외 발생 ({self.consecutive_errors}/{max_err}): {str(e)}")
                console.print(f"[dim red]⚠️ 에러 발생: {str(e)}[/dim red]")
                if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                    console.print(f"[bold red][ERROR] 시스템 트레이딩 루프 예외 발생 ({self.consecutive_errors}/{max_err}): {str(e)}[/bold red]")
                
                if self.consecutive_errors >= max_err:
                    # [안전장치] 대기에 들어가기 전에 **정말 서버가 죽었는지** 먼저 확인한다.
                    #  consecutive_errors는 API 오류만이 아니라 루프의 모든 예외에 오른다.
                    #  코드 쪽 예외로 한도가 차면 서버가 멀쩡한데도 아래 대기 루프가 주기를
                    #  통째로 붙잡고, 그 동안 매도 검사가 돌지 않아 손절·트레일링이 무감시가
                    #  된다. 일일 손실 한도에서 이미 같은 결함을 고쳤다(engine.check_loss_limit
                    #  주석: "정지는 매도 감시까지 꺼서 무방비 상태를 만든다").
                    if self._errors_are_not_the_server(str(e)):
                        self.consecutive_errors = 0
                        time.sleep(10)
                        continue        # 대기하지 않는다 — 다음 주기에 매도 검사를 다시 돈다

                    # [수정] 중단 대신 대기 모드로 전환
                    self.log(f"[장애 감지] 연속 에러 {max_err}회 발생. 서버 장애로 판단하여 대기 모드로 전환합니다.")

                    # [개선] 상세 알림 메시지 구성
                    err_reason = str(e)
                    if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                        console.print(f"\n[bold red][ERROR] 연속 에러 {max_err}회 초과! 자동매매 시스템이 대기 모드(정지)로 전환되었습니다. (사유: {err_reason})[/bold red]\n")
                    msg = f"🚨 [시스템 긴급 대기] 연속 에러 {max_err}회 발생\n매매를 일시 중단하고 서버 복구를 대기합니다.\n\n원인: {err_reason}\n\n서버 복구 확인 시 자동으로 재개됩니다."

                    # [추가] 진입 알림 쿨타임(10분) — 대기/복구가 짧은 주기로 반복(진동)해도 스팸 방지
                    now = time.time()
                    if now - self.last_wait_alert_time > 600:
                        # [추가] 에러 로그 꼬리 첨부 (1시간 쿨타임)
                        _attach_tail = now - self.last_emergency_alert_time > 3600
                        if _attach_tail:
                            log_tail = get_mystock_log_tail(20)
                            msg += f"\n\n📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```"

                        #  [전달 확인 뒤 표식 · 2026-09-07] 종전에는 쿨타임(10분)과
                        #   _wait_alert_sent 를 **보내기 전에** 찍었다. 비동기 전송이라
                        #   실패가 예외로 오지 않으므로 못 닿아도 10분간 침묵하고,
                        #   더 나쁜 것은 _wait_alert_sent 가 True 로 남는다는 점이다 —
                        #   진입 알림은 못 갔는데 **복구 알림만** 나가서, 운영자는 있지도
                        #   않았던 장애의 복구를 통보받는다(짝이 어긋난다).
                        if _pkg().alert_delivered(msg, urgent=True):
                            self.last_wait_alert_time = now
                            self._wait_alert_sent = True
                            if _attach_tail:
                                self.last_emergency_alert_time = now

                    # 대기 중임을 남긴다 — 정체 감시(scheduler._check_heartbeat)가 이
                    #  '의도된 멈춤'을 장애로 오탐하면 서버 장애 때마다 가짜 경보가 나간다.
                    self.waiting_for_server = True
                    try:
                        self._wait_for_server_recovery()
                    finally:
                        self.waiting_for_server = False

                    # 복구되어 리턴되면 에러 카운트 초기화 후 루프 재개
                    self.consecutive_errors = 0
                    continue
                
                time.sleep(10)

    def _errors_are_not_the_server(self, err_reason):
        """서버는 멀쩡한데 우리 쪽에서 터지고 있는가.

        [왜 구분하는가] 서버 장애라면 대기가 옳다 — 주문 자체가 나갈 수 없으니 붙잡고
        있어도 잃을 게 없다. 그러나 코드 쪽 예외라면 대기는 최악이다. 주문은 나갈 수
        있는데 매도 검사를 멈춰 손절·트레일링만 꺼진다.

        참이면 대기하지 않고 다음 주기로 넘어간다. 버그가 계속되면 에러가 다시 쌓여
        여기로 돌아오지만, 그 사이 **매 주기 매도 검사는 돈다**. 진동하더라도 무감시보다
        낫다(루프 말미 sleep이 하한을 잡아 준다).
        """
        try:
            healthy = api.check_server_health()
        except Exception:
            return False        # 확인조차 안 되면 서버 문제로 보고 대기한다(보수적)
        if not healthy:
            return False

        self.code_error_streaks = getattr(self, 'code_error_streaks', 0) + 1
        self.log(f"[장애 판정 정정] 연속 에러가 쌓였지만 서버는 정상입니다 — 코드 쪽 오류로 "
                 f"보고 대기하지 않습니다(매도 감시 유지). 원인: {err_reason}")
        logger.error(f"[킬스위치] 서버 정상 · 코드 오류 추정({self.code_error_streaks}회): {err_reason}")

        now = time.time()
        if now - getattr(self, '_code_error_alerted_at', 0.0) > CODE_ERROR_ALERT_COOLDOWN:
            #  [전달 확인 뒤 쿨다운 · 2026-09-07] 보내기 전에 찍으면, 비동기 전송이
            #   못 닿아도 그 쿨다운 동안 침묵한다. 본문이 "신규 매수는 계속되므로 원인
            #   확인이 필요합니다" 라 — 그 사이 매수는 계속 나간다.
            if _pkg().alert_delivered(
                    f"⚠️ [반복 오류] 연속 에러가 한도에 닿았으나 증권사 서버는 정상입니다.\n"
                    f"코드 쪽 오류로 보고 **대기하지 않습니다**(매도·손절 감시 유지).\n"
                    f"신규 매수는 계속되므로 원인 확인이 필요합니다.\n\n원인: {err_reason}",
                    urgent=True):
                self._code_error_alerted_at = now
        return True

    def _wait_for_server_recovery(self):
        """서버가 정상화될 때까지 대기"""
        check_interval = 60 # 1분마다 확인
        
        while self.is_running:
            time.sleep(check_interval)
            
            self.log("[장애 대기] 서버 상태 점검 중...")
            
            try:
                # 삼성전자 현재가 조회로 서버 상태 확인
                if api.check_server_health():
                    self.log("[서버 복구] 서버 정상화 확인. 매매를 재개합니다.")
                    # [추가] 서버 정상화가 확인되었으므로 체결 감시 에러 카운트도 리셋
                    # (장애 중 누적된 카운터가 Kill Switch를 계속 걸어 대기/복구가
                    #  무한 반복되는 교착 방지 — 이후 조회가 다시 실패하면 재누적됨)
                    _pkg().ConclusionMonitor().consecutive_errors = 0
                    # [수정] 진입 알림을 보냈을 때만 복구 알림 발송 (쿨타임으로 진입 알림이
                    # 생략된 반복 진동 구간에서는 복구 알림도 생략해 스팸 방지)
                    if self._wait_alert_sent:
                        #  복구 알림도 전달을 확인한 뒤에 짝을 푼다 — 못 닿았는데 풀어
                        #  버리면 다음 장애의 진입 알림만 남고 복구 통보는 영영 없다.
                        if _pkg().alert_delivered("✅ [서버 복구] KIS 서버가 정상화되었습니다.\n"
                                                  "자동매매를 재개합니다."):
                            self._wait_alert_sent = False
                    return
                else:
                    self.log("[장애 대기] 서버 여전히 응답 없음.")
            except Exception as e:
                self.log(f"[장애 대기] 점검 중 오류: {e}")

    def _snapshot_paper_closing_equity(self):
        """[관찰 모드] 종가가 확정된 뒤 그날 자산 스냅샷을 한 번 더 찍는다(종가로 덮음).

        [왜] 주기 스냅샷은 RUNNING 분기에서만 돈다. 15:20 단일가 휴게부터 상태가
         WAITING이라 그날 마지막 스냅샷은 15:19 장중가로 굳고, 종가 단일가(15:20~15:30)
         에서 확정된 종가는 자산곡선에 영원히 들어가지 못한다. 곡선이 유일한 소스인
         MDD·누적수익률·고점대비가 전부 종가 기준이 아니게 된다.
         (2026-08-28 실측: 곡선 3,743,000 = 15:19:40 값 / 확정 종가 3,744,000)

        [시각] 시계가 아니라 확정 여부를 본다 — api.krx_last_settled_day()가 오늘을
         가리켜야(마감 + 확정 여유) 그날 봉을 종가로 인정한다. 그 전에 찍으면 직전
         거래일 종가를 오늘 값으로 굳힌다.
        [빈도] 거래일당 1회. 휴장일에는 찍지 않는다 — 새 봉이 없는데 행을 만들면
         주말·공휴일이 '변동 없는 거래일'로 곡선에 남아 일수·고점 아래 일수가 부푼다.
         일봉을 못 받아 실시간가로 폴백한 종목이 하나라도 있으면 '찍었다'로 세지 않고
         다음 주기에 다시 덮는다(같은 날 재호출은 덮어쓰기다).
        """
        try:
            if api.is_holiday_today():
                return
            today = datetime.now().strftime("%Y%m%d")
            if self.paper_closing_snapshot_date == today:
                return
            if api.krx_last_settled_day() != today:
                return      # 아직 종가가 확정되지 않았다 — 다음 주기에 다시 본다
            from modules import paper_broker
            if not paper_broker.snapshot_equity():
                return
            self.paper_closing_snapshot_date = today
            self.log("[관찰 모드] 마감 자산 스냅샷 기록 (KRX 확정 종가 기준)")
        except Exception as e:
            # 기록 전용 경로다. 실패해도 매매 루프를 흔들면 안 된다.
            logger.debug(f"[관찰 모드 마감 스냅샷] 실패: {e}")
