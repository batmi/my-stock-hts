"""AutoTrader 의 수명주기 — 기동(초기화·안전 점검·시작 알림)과 정지(최종 자산·미체결 잔존 알림).

  · 초기화(initialize): 잔고·예수금 조회, 트레일링 앵커·반익절 이력 캐시 적재, 거래소 미체결 복구(재기동 중복 주문
    방지), 꺼져 있던 동안의 외부 체결 동기화, 오늘의 시작 자산(결손·급감이면 박지 않는다)
  · 시작(start): 모드별 자격증명·확인, 같은 계좌 중복 실행 잠금(_acquire_instance_lock), DB 무결성·백업·디스크 점검
    (_check_db_health), 주 루프 스레드 기동, 시작 알림(적용 전략·보유 현황)
  · 정지(stop): 루프 정지와 잠금 해제, 최종 자산·오늘 매매 요약·감시가 끊기는 보유·미체결 잔존 알림

[왜 나눴나 · 2026-10-25 C1 6단계] C1 의 마지막 단계로 trader.py 에 남은 주기 제어를 떼어 냈다. 기동·정지는 하루에
 한두 번 도는 경로이고 주 루프(loop.py)는 매 주기 도는 경로다. 둘을 한 파일에 두면 기동 안전장치 하나를 고칠 때도
 1,800줄을 함께 읽어야 했다.

[어떻게 나눴나] 1~5단계(reporting·market_risk·account_sync·selling·buying)와 같은 규약이다. 메서드 본문은 옮기면서
 바꾸지 않았다. AutoTrader 가 이 믹스인을 상속하므로 trader.start() · patch.object(AutoTrader, ...) 가 전과 같이
 동작한다. 모듈 전역 이름(config·api·db_manager·datetime·Progress …)은 trader.py 와 **같은 객체**를 가리키게
 import 했다(tests/test_trader_split_parity.py 가 이름마다 대조한다). 로거도 trader 와 같은 이름을 쓴다. 매도도 부르는
 _trade_account_key, 엔진·계좌 동기화가 부르는 방어 모드 전환(halt_buys·resume_buys)은 trader 에 남겼다.
 동등성 기준은 주기 재생 기록 여섯 시나리오(tests/test_cycle_replay.py)이고, 이 경로는 lifecycle 시나리오가 지난다.

[주의] 테스트에서 이 메서드들이 쓰는 전역을 바꿔 끼울 때는 trader 가 아니라 이 모듈을 지정한다
 (예: modules.auto_trade.lifecycle.load_daily_initial_asset). 패키지 네임스페이스 경유 호출은 _pkg() 그대로다.
"""
import concurrent.futures
import logging
import os
import threading
from datetime import datetime
from typing import Any, Callable

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt

import api
import config
from core import context
from core import utils
from modules import account
from modules import db_manager
from modules import instance_lock
from modules.auto_trade.common import (DISK_FREE_WARN_MB, _get_trade_account, is_plausible_baseline,
                                       is_single_price_break, load_daily_initial_asset, load_daily_principal,
                                       save_daily_initial_asset, trade_account_key)

console = config.console

#  트레이더 로그와 같은 이름 — 기동·정지 코드가 남기는 로그도 'modules.auto_trade.trader' 로 모인다.
logger = logging.getLogger("modules.auto_trade.trader")


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자 — trader._pkg 와 같은 규약.

    테스트의 patch('modules.auto_trade.X') 가 이 모듈 안의 호출에도 걸리게 한다. (지연 import 라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class TraderLifecycleMixin:
    """AutoTrader 의 기동·정지 메서드. 단독으로 쓰지 않는다 — 트레이더 상태(self.order_manager …)를 쓴다."""

    #  이 믹스인이 기대는 트레이더 상태·메서드(정의·초기값은 AutoTrader 와 다른 믹스인). 값 없는 선언이라 실행 시에는
    #  그쪽 것이 그대로 쓰인다. getattr 기본값으로 읽는 상태와 이 믹스인이 직접 쓰는 상태는 빠져 있다.
    order_manager: Any
    log: Callable[..., None]
    is_market_open: Callable[..., bool]
    resume_buys: Callable[..., bool]
    daily_pnl_base: Callable[..., Any]
    _run_loop: Callable[..., None]
    _trade_account_key: Callable[..., str]
    _refine_trade_records: Callable[..., list]
    _sync_external_fills: Callable[..., Any]

    def initialize(self):
        """
        자동매매 시작에 필요한 모든 초기화 작업을 병렬로 수행합니다.
        (자산 조회, DB 캐시 로드, 초기 자산 설정 등)
        """
        if self.initialized:
            return True

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
            transient=True,
            disable=not api._is_screen_output_allowed() # [추가] 텔레그램 스레드 등 백그라운드에서는 상태바 숨김
        ) as progress:
            # [수정] 모의투자는 예수금을 잔고 summary에서 유도하므로 작업 2개(잔고/DB), 실전은 3개(+예수금)
            _init_total = 3
            task = progress.add_task("[cyan]자동매매 세션 초기화 중...[/cyan]", total=_init_total)
            
            target_cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                # 병렬 실행을 위한 변수
                results = {}

                def _fetch_balance():
                    progress.update(task, description="[cyan]잔고/평가금 조회...[/cyan]")
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    progress.advance(task)
                    return "balance", (holdings, summary)

                def _fetch_deposit():
                    progress.update(task, description="[cyan]예수금 상세 조회...[/cyan]")
                    deposit_res = api.get_deposit_balance(target_cano, acnt)
                    progress.advance(task)
                    return "deposit", deposit_res

                def _load_db_caches():
                    progress.update(task, description="[cyan]DB 캐시 로드...[/cyan]")
                    try:
                        ts_cache = db_manager.db.get_all_trailing_stops()
                    except Exception as _te:
                        #  앵커는 종목별로 다시 읽는 길이 있다(_cached_anchor) — 비워 둬도
                        #  스스로 낫는다. 흔적만 남긴다.
                        logger.warning(f"[기동] 트레일링 앵커 캐시 적재 실패 — 종목별로 "
                                       f"다시 읽습니다: {_te}")
                        ts_cache = {}
                    try:
                        half_cache = db_manager.db.get_all_half_tp()
                    except Exception as _he:
                        #  반익절은 다르다. 빈 집합은 '아무도 반익절 안 했다'로 읽혀
                        #  이미 반쪽 판 종목을 **또 판다**. 못 읽었다는 사실을 들고 간다.
                        logger.error(f"[기동] 반익절 이력 적재 실패: {_he}")
                        half_cache = None
                    # [재기동 복구] 거래소에 살아 있는 미체결 주문을 메모리 추적에 되살린다.
                    #  이걸 안 하면 첫 주기에 같은 종목으로 두 번째 주문이 나간다.
                    #  DB 캐시 작업에 얹어 시작 시 API 호출이 몰리지 않게 한다(라즈베리파이 OOM).
                    ok = self.order_manager.restore_pending_orders(target_cano, acnt)
                    progress.advance(task)
                    return "caches", (ts_cache, half_cache, ok)


                #  [계좌 컨텍스트 · 2026-09-05] use_auto_account 는 threading.local 이라
                #   AccountContext 안에서 제출해도 워커 스레드에는 상속되지 않는다. 풀리면
                #   그 요청은 **수동 앱키·수동 토큰**으로 나가고(core.utils.get_common_headers,
                #   api.auth.get_current_token) TPS 도 수동 버킷에서 깎인다
                #   (api.http._real_bucket_key). 자동 계좌 잔고를 수동 키로 묻는 꼴이다.
                #   매도·후보 풀은 이미 감싸져 있었는데 기동 초기화 풀만 빠져 있었다.
                _init_task = utils.inherit_account_context
                with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="at_init") as executor:
                    # [수정] 모의투자는 잔고 summary에 예수금이 포함되어 있어 별도 예수금 API 호출이 불필요.
                    # 초기화 시 중복 잔고조회(get_domestic_balance)+예수금조회가 2-TPS 경합으로 재시도
                    # 폭주를 일으켜 메모리가 폭증하던 문제를 제거한다. (실전만 별도 예수금 조회 수행)
                    futures = [executor.submit(_init_task(_fetch_balance)),
                               executor.submit(_init_task(_load_db_caches))]
                    futures.append(executor.submit(_init_task(_fetch_deposit)))
                    for future in concurrent.futures.as_completed(futures):
                        key, value = future.result()
                        results[key] = value

                # 결과 처리
                holdings, summary = results.get("balance", (None, None))
                ts_cache, half_cache, pending_restored = results.get("caches", ({}, set(), False))
                # 조회 실패는 '미체결 없음'과 다르다 — 모르는 상태에서 신규 매수를 더 내지
                # 않는다. 매도는 막지 않는다(청산 경로는 어떤 경우에도 열려 있어야 한다).
                self.pending_restore_ok = bool(pending_restored)

                deposit_res = results.get("deposit")

                if holdings is None or deposit_res is None:
                    raise Exception("자산/예수금 조회 실패 (API 응답 없음)")

                # [기동 동기화] 시스템이 꺼져 있던 동안의 외부 체결을 DB에 채운다.
                #  ConclusionMonitor의 실시간 감지는 '오늘'만 본다(get_today_history). 운용자가
                #  NXT 애프터나 비운용일에 HTS로 매매하면 그 기록은 영구 누락됐다.
                #  보유수량 역산이라 얼마나 오래 꺼져 있었든 필요한 만큼만 조회한다.
                #  잔고가 확정된 뒤에 돌린다 — 위 병렬 블록 안에서는 잔고가 아직 없다.
                self._sync_external_fills(target_cano, acnt, holdings)

                self.trailing_stop_cache = ts_cache
                self.half_tp_cache = half_cache if half_cache is not None else set()
                self.half_tp_cache_loaded = half_cache is not None
                if not self.half_tp_cache_loaded:
                    self.log("[기동] 반익절 이력을 읽지 못했습니다 — 판정 때 다시 읽고, "
                             "그래도 못 읽으면 추가 반익절을 내지 않습니다(중복 매도 방지).")
                self.initial_holdings = holdings
                self.initial_summary = summary
                #  하트비트(scheduler)가 사망 알림에 싣는 '보유 종목 수'가 이 값이다. 종전에는
                #  첫 매매 주기의 잔고 조회에서만 갱신돼, 휴장일에 재기동하면 다음 거래일
                #  09:00까지 0으로 남았다(실측 2026-09-13 파이: 보유 4종목인데 holdings=0).
                #  그 사이 프로세스가 죽으면 "포지션이 무방비" 줄이 빠진 채 알림이 나간다.
                #  기동 시 잔고를 이미 손에 들고 있으니 여기서 바로 씨딩한다.
                self.last_holdings_count = len(holdings)
                
                # [수정] 초기 총자산 산출.
                # 기존에는 get_asset_status_data를 재호출했으나, 이는 startup 직후 잔고조회를
                # 또 수행(+해외잔고/체결내역)하여 짧은 시간에 KIS 호출이 몰리고, 그 중 일부가
                # 재시도(타임아웃/EGW00201) 경로로 빠지며 네이티브 메모리가 폭증(OOM)하는 원인이었다.
                # 모의투자는 이미 조회한 잔고 summary의 총평가금(tot_evlu_amt)으로 유도하여
                # 추가 KIS 호출을 제거한다. (실전만 해외자산 포함 통합 조회 유지)
                asset_data = account.get_asset_status_data(target_cano, acnt)
                tot_asset = asset_data.get('tot_asset', 0) if asset_data else 0

                #  [Fix 2026-09-08] 장중 경로(_degraded 검사 2곳)는 결손을 보고 대안 계산으로
                #   비키는데, **하루의 기준선을 박는 이 자리만** 그 신호를 안 봤다. 결손이 있는
                #   총자산은 '그만큼 작은 정상 숫자'라 아래 is_plausible_baseline 의 반토막
                #   문턱에 닿지 않는다(노출 상한이 40%라 주식이 통째로 빠져도 못 닿는다).
                #   기준선은 하루를 지배하고 되돌릴 수 없으므로, 모르면 박지 않는다.
                _degraded = list((asset_data or {}).get('degraded') or [])
                if tot_asset > 0 and _degraded:
                    tot_asset = 0
                    warn = (f"⚠️ [시작 자산 보류] 자산 집계에서 결손이 있습니다"
                            f"({', '.join(_degraded)}).\n"
                            f"이 값을 오늘 기준 자산으로 삼지 않습니다 — 계좌 차단기"
                            f"(일일 손실 한도)가 동작하지 않으니 확인해 주세요.")
                    self.log(warn)
                    api.send_telegram_message(warn)
                    #  첫 주기의 계좌 감시도 결손이면 같은 보류를 한다(account_sync) — 같은 날
                    #  같은 사실을 두 번 알리지 않도록 그쪽 스로틀을 찍어 둔다.
                    self._baseline_defer_date = datetime.now().strftime("%Y-%m-%d")

                if tot_asset > 0:
                    account_key = f"{target_cano}-{acnt}"
                    saved_initial = load_daily_initial_asset(account_key)
                    if saved_initial > 0:
                        self.initial_asset = saved_initial
                        # [오프라인 입출금] 기준 원금도 함께 복원한다. 복원하지 않으면 아래
                        #  감시 루프가 기준 원금을 **입출금 이후 상태로** 새로 잡아 차이가
                        #  0이 되고, 프로그램이 꺼진 사이의 입출금이 영영 감지되지 않는다.
                        #  그러면 시작 자산만 옛 값으로 남아(출금이면 높게) 차단기가 헛발동하고
                        #  사이징 기준도 부푼 채로 하루가 간다.
                        self.baseline_principal = load_daily_principal(account_key)
                    elif is_plausible_baseline(account_key, tot_asset):
                        self.initial_asset = tot_asset
                        if not save_daily_initial_asset(account_key, self.initial_asset):
                            self.log("[기준선] 당일 시작 자산을 파일에 저장하지 못했습니다 — "
                                     "재기동하면 오늘의 손실 한도·드로다운 기준이 리셋됩니다")
                        #  파일 저장의 성공 여부는 바로 위에서 검사한다 — 같은 기준선을 담는
                        #  DB 쪽도 같게 다룬다(둘 중 하나만 검사하면 절반은 조용히 사라진다).
                        if not db_manager.db.save_daily_asset(
                                datetime.now().strftime("%Y-%m-%d"), account_key, self.initial_asset):
                            self.log("[기준선] 당일 시작 자산을 자산 이력에 남기지 못했습니다 — "
                                     "내일의 드로다운 기준에서 오늘이 빠집니다")
                    else:
                        # [안전장치] 직전 영업일 대비 반토막 이하 — 시세 결손으로 예수금만 잡힌
                        #  응답을 의심한다. 이 값을 기준선으로 박으면 차단기의 분모가 작아져
                        #  손실률이 종일 큰 양수로 계산되고, 보호 장치가 조용히 사라진다.
                        #  저장하지 않고 0으로 둔다 — 사이징은 예수금 기준으로 폴백하고,
                        #  차단기가 꺼진 사실은 아래 알림으로 드러난다.
                        self.initial_asset = 0
                        last = db_manager.db.get_last_daily_asset(
                            account_key, datetime.now().strftime("%Y-%m-%d"))
                        warn = (f"⚠️ [시작 자산 이상] 조회값 {tot_asset:,}원이 직전 기록"
                                f" {int(last or 0):,}원 대비 지나치게 작습니다.\n"
                                f"시세 결손(주식 평가액 0 수신)이 의심되어 오늘 기준 자산으로"
                                f" 삼지 않습니다.\n"
                                f"계좌 차단기(일일 손실 한도)가 동작하지 않으니 확인해 주세요.")
                        self.log(warn)
                        api.send_telegram_message(warn)
                else:
                    self.initial_asset = 0

                self.initialized = True
                return True
        return False

    def _acquire_instance_lock(self):
        """이 계좌의 자동매매 배타 잠금을 잡는다. 실패하면 시작하지 않는다.

        선점자가 있다는 것은 '다른 프로세스가 이미 이 계좌로 매매 중'이라는 뜻이다.
        그대로 시작하면 서로의 미체결을 모른 채 같은 종목에 각자 주문을 낸다.
        """
        self.start_block_reason = ""
        try:
            lock = instance_lock.InstanceLock(self._trade_account_key())
            if lock.acquire():
                self.instance_lock = lock
                return True
        except Exception as e:
            # 잠금 장치 자체가 고장 났다고 매매를 막지는 않는다(잠금은 보조 안전장치다).
            self.log(f"[중복 실행 검사] 잠금 처리 실패 — 검사를 건너뜁니다: {e}")
            return True

        holder = f" ({lock.holder})" if lock.holder else ""
        msg = (f"자동매매를 시작할 수 없습니다 — 같은 계좌({self._trade_account_key()})로 "
               f"이미 다른 프로세스가 매매 중입니다{holder}.")
        #  [2026-09-07] 사유가 로그에만 남으면 원격(텔레그램)에서 /start 를 친 운용자는
        #   왜 안 떴는지 알 수 없다. 그가 화면 앞에 없다는 것이 이 경로의 전제다.
        self.start_block_reason = f"같은 계좌로 다른 프로세스가 이미 매매 중입니다{holder}"
        self.log(f"[중복 실행 차단] {msg}")
        if api._is_screen_output_allowed():
            console.print(f"\n[bold red]{msg}[/bold red]")
            console.print("[dim]두 인스턴스가 동시에 돌면 서로의 미체결 주문을 몰라 "
                          "같은 종목에 중복 주문이 나갑니다.[/dim]")
        api.send_telegram_message(f"⛔ [중복 실행 차단] {msg}")
        return False

    def _release_instance_lock(self):
        lock = getattr(self, 'instance_lock', None)
        if lock is not None:
            self.instance_lock = None
            try:
                lock.release()
            except Exception as e:
                logger.debug(f"[InstanceLock] 해제 실패: {e}")

    def _check_db_health(self):
        """DB 무결성 확인 + 당일 백업. 무결성이 깨졌으면 매매를 시작하지 않는다.

        [왜 fail-closed 인가] 이 DB에는 평단·트레일링 최고가·손절 기준이 들어 있다.
        잔고는 증권사에 있으니 '무엇을 들고 있는지'는 복구되지만, '어디서 자를지'는
        여기에만 있다. 손상된 채로 돌리면 트레일링 최고가가 사라져 청산이 어긋나고,
        그 오류는 조용히 손실로만 나타난다. 멈추는 쪽이 낫다.

        백업은 실패해도 막지 않는다 — 백업이 없다고 지금 매매가 틀리지는 않는다.
        """
        ok, detail = db_manager.db.check_integrity()
        if not ok:
            msg = (f"DB 무결성 검사 실패 — 자동매매를 시작하지 않습니다.\n"
                   f"경로: {db_manager.db.db_path}\n결과: {detail}")
            self.log(f"[DB 이상] {msg}")
            if api._is_screen_output_allowed():
                console.print(f"\n[bold red]{msg}[/bold red]")
                console.print("[dim]db/backups 의 최근 백업으로 복구한 뒤 다시 시작하세요. "
                              "(평단·트레일링 최고가·손절 기준이 이 파일에만 있습니다)[/dim]")
            api.send_telegram_message(f"⛔ [DB 이상] {msg}")
            #  원격 /start 의 응답이 사유를 싣는다(telegram_bot._start_failure_reason) — 잠금 거부와 같은 규약.
            self.start_block_reason = "DB 무결성 검사 실패 — 손절 기준이 든 DB 가 손상됐습니다(백업으로 복구 필요)"
            return False

        path = db_manager.db.backup()
        if path:
            self.log(f"[DB 백업] {os.path.basename(path)}")
        else:
            # 백업 실패로 매매를 막지는 않되, 조용히 넘기지도 않는다.
            self.log("[DB 백업] 실패 — 백업 없이 진행합니다(운영자 확인 필요)")

        # [안전장치] 디스크가 차면 쓰기가 실패한다 — 트레일링 최고가·거래 기록이 사라진다.
        #  **막지는 않는다.** 손상과 달리 지금 읽는 값은 옳고, 여기서 멈추면 보유 포지션의
        #  손절 감시까지 함께 멈춘다. 대신 크게 알린다.
        free_mb = db_manager.db.disk_free_mb()
        if 0 <= free_mb < DISK_FREE_WARN_MB:
            msg = (f"디스크 여유 공간 부족: {free_mb:,.0f}MB — DB 쓰기가 실패하면 "
                   f"트레일링 최고가·거래 기록이 사라집니다(매매는 계속합니다).")
            self.log(f"[디스크 경고] {msg}")
            if api._is_screen_output_allowed():
                console.print(f"\n[bold yellow]⚠ {msg}[/bold yellow]")
            api.send_telegram_message(f"⚠️ [디스크 경고] {msg}")
        return True

    def _report_start_failure(self, reason):
        """기동이 초기화에서 멈췄다는 사실을 화면 밖으로도 알린다(화면 로그·텔레그램·차단 사유).

        [2026-10-25] 중복 실행 차단·DB 이상은 텔레그램을 보내고(잠금 거부는 차단 사유도 남긴다), 초기화 예외만
         mystock.log(logger.error)와 콘솔에 남았다. 텔레그램 /start 는 is_running 으로 실패는 알아채도 사유가 비었고,
         `--auto` 기동(main.py)은 아무도 응답을 받지 않아 실패 자체를 아무도 몰랐다 — 부팅 직후 네트워크가 늦어
         기동 잔고 조회가 실패하면, 프로세스와 하트비트는 살아 있는 채 자동매매만 꺼져 있다(외부 감시자는 프로세스
         사망만 본다). 그동안 손절·트레일링 감시가 돌지 않는다.
        """
        reason = str(reason or "원인 미상")[:300]
        self.start_block_reason = f"기동 초기화 실패 — {reason}"
        self.log(f"[기동 실패] 자동매매를 시작하지 못했습니다 — {reason}")
        api.send_telegram_message(
            f"⛔ [기동 실패] 자동매매를 시작하지 못했습니다.\n사유: {reason}\n\n"
            f"손절·트레일링 감시가 돌지 않습니다. 원인을 확인한 뒤 다시 시작해 주세요(/start).")

    def start(self, interactive=True):
        if self.is_running:
            if interactive:
                console.print("\n[yellow]이미 자동매매가 실행 중입니다.[/yellow]")
            return
        
        self.log("━━━ 자동매매 시스템 시작 프로세스 진입 ━━━")

        # [안전장치] 시작 시 방어 모드는 초기화한다. 손실 한도 조건이 여전히 유효하면
        #  첫 주기의 check_loss_limit이 즉시 재발동시키므로 안전 수준은 유지된다.
        if getattr(self, 'buy_halted', False):
            self.resume_buys(reason="시스템 재시작")

        if config.session.is_toss:
            # [추가] 토스: 단일 계좌 + 토스 API 사용. 별도 KIS AUTO 계좌가 필요 없다.
            if not config.session.toss_app_key or not config.session.toss_app_secret or not config.session.cano:
                if api._is_screen_output_allowed():
                    console.print("[bold red]오류: 토스 시스템 트레이딩을 실행하려면 토스 API 설정이 필요합니다.[/bold red]")
                    console.print("[dim]환경 변수 TOSS_APP_KEY, TOSS_APP_SECRET, TOSS_ACC_NUM을 설정해주세요.[/dim]")
                return

            # [안내] 토스는 주식계좌를 하나만 내준다. 한투 실전처럼 자동매매 전용 계좌로
            #  갈라 둘 수 없으므로, 시스템 트레이딩도 수동 주문과 같은 계좌에서 돈다.
            #  운용자가 알아야 할 사실이라(예수금·보유수량을 수동 매매와 나눠 쓴다)
            #  시작 확인 앞에서 명시한다.
            if interactive:
                console.print("\n[bold magenta]!!! 경고: 토스증권 실계좌에서 시스템 트레이딩을 시작합니다 !!![/bold magenta]")
                console.print(f"운용 계좌: [bold yellow]{config.session.cano}[/bold yellow] (토스증권, 실제 자산 거래)")
                console.print("[dim]토스증권은 주식계좌를 하나만 제공합니다 — 시스템 트레이딩이 수동 주문과 "
                              "같은 계좌를 사용합니다(자동매매 전용 계좌 없음).[/dim]")
                console.print("[dim]따라서 수동으로 낸 주문·보유 종목이 자동매매의 예수금과 슬롯을 함께 씁니다.[/dim]")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 토스증권 자동매매를 시작합니다(수동 주문과 같은 계좌).[/bold cyan]")
        elif config.session.is_paper:
            if interactive:
                virt_acc_str = os.environ.get("VIRT_ACC_NUM", "")
                display_acc = virt_acc_str.replace("PAPER-", "") if virt_acc_str.startswith("PAPER-") else virt_acc_str
                console.print(f"\n운용 계좌: [bold yellow]PAPER | {display_acc}[/bold yellow]")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 가상 투자 자동매매를 시작합니다.[/bold cyan]")
        else:
            if not config.session.auto_app_key or not config.session.auto_cano:
                if api._is_screen_output_allowed():
                    console.print("[bold red]오류: 실전 투자 모드에서 시스템 트레이딩을 실행하려면 별도의 자동매매 계좌 설정이 필요합니다.[/bold red]")
                    console.print("[dim]환경 변수 AUTO_APP_KEY, AUTO_APP_SECRET, AUTO_ACC_NUM을 설정해주세요.[/dim]")
                return

            if interactive:
                console.print("\n[bold red]!!! 경고: 실전 투자 모드에서 시스템 트레이딩을 시작합니다 !!![/bold red]")
                console.print(f"운용 계좌: [bold yellow]{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}[/bold yellow] (시스템 트레이딩 전용)")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 실전 투자 자동매매를 시작합니다.[/bold cyan]")

        # [안전장치] 같은 계좌로 엔진이 두 개 뜨면 서로의 미체결 주문을 모른다 —
        #  각자 같은 종목에 매수를 내고, 재기동 복구도 이걸 못 막는다(둘 다 거래소
        #  미체결을 자기 주문으로 읽는다). 매매를 시작하기 전에 잠근다.
        if not self._acquire_instance_lock():
            return

        # [안전장치] 손절 기준이 든 DB가 깨졌으면 매매하지 않는다(잠금은 되돌린다).
        if not self._check_db_health():
            self._release_instance_lock()
            return

        try:
            # [수정] 초기화 로직 분리
            if not self.initialized:
                if not self.initialize():
                    self._release_instance_lock()
                    self._report_start_failure("초기화가 완료되지 않았습니다")
                    if api._is_screen_output_allowed():
                        console.print("[bold red]시스템 초기화에 실패하여 자동매매를 시작할 수 없습니다.[/bold red]")
                        if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                            console.print("[bold red][ERROR] 시스템 초기화 실패[/bold red]")
                    return

            self.is_running = True
            self.start_time = datetime.now()
            self.consecutive_errors = 0
            self.was_market_open = self.is_market_open()
            self._first_loop_flag = True
            self.market_status_notified = {}
            context.SYSTEM_LOGGER = self.log

            self.thread = threading.Thread(target=self._run_loop, daemon=True, name="AutoTrader")
            self.thread.start()

            if api._is_screen_output_allowed():
                console.print("\n[green]자동매매 시스템이 시작되었습니다. (백그라운드)[/green]")
            self.log("시스템 시작")
            
            # [추가] 장 마감 상태에서 시작했을 경우 명확한 안내 메시지 출력
            if not self.was_market_open:
                self.log("━" * 85)
                if is_single_price_break():
                    self.log("⏸️ [휴게 시간 대기] 현재는 단일가 매매 동기화 시간입니다. 거래 재개 시 자동으로 매매가 개시됩니다.")
                elif api.is_holiday_today() or datetime.now().weekday() > 4:
                    self.log("💤 [휴장일 대기] 오늘은 주말 또는 공휴일입니다. 다음 거래일에 자동으로 매매가 개시됩니다.")
                else:
                    self.log("💤 [장 마감 대기] 현재는 거래 시간이 아닙니다. 장 시작 시 자동으로 매매가 개시됩니다.")
                self.log("━" * 85)
            
            # [수정] 시작 메시지 생성 로직은 초기화 시 저장된 데이터 활용
            holdings = self.initial_holdings
            summary = self.initial_summary
            #  [Fix 2026-10-25] 시작 자산을 보류한 날(집계 결손·직전 대비 급감·조회 실패)에는
            #   '초기 자산 0원'이 찍혔고, 예수금도 그 조건에 묶여 함께 0원이었다 — 잔고가 빈
            #   계좌와 글자가 같다. 보류 사유는 위의 경고가 따로 알린다. 예수금은 기동 잔고
            #   조회(summary)에서 읽으므로 시작 자산과 무관하게 보이고, 그 조회가 없을 때만
            #   '확인 불가'로 적는다(못 읽은 것은 0원이 아니다).
            deposit = None
            if summary:
                deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                if deposit == 0:
                    deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))

            msg = f"🟢 [시스템 시작] 자동매매가 시작되었습니다.\n"
            if self.initial_asset > 0:
                msg += f"초기 자산: {self.initial_asset:,}원"
            else:
                msg += "초기 자산: 보류 — 결손 없는 자산 조회에서 잡습니다(그때까지 계좌 차단기 미동작)"
            msg += f"\n현재 예수금: {deposit:,}원" if deposit is not None else "\n현재 예수금: 확인 불가"
            
            # [복원] 상세 자산 현황 추가
            stock_eval_amt = 0
            if summary and len(summary) > 0:
                s_data = summary[0]
                stock_eval_amt = api.safe_int(s_data.get('scts_evlu_amt'))
                total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                
                tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
                if tot_pchs == 0 and holdings:
                    tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in holdings if api.safe_int(h.get('hldg_qty')) > 0)
                
                rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                msg += f"\n증권 평가 자산: {stock_eval_amt:,}원"
                msg += f"\n증권 평가 손익: {total_profit:+,}원 ({rate:+.2f}%)"

            # [복원] 전략 설정 요약 정보 추가
            buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
            buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]
            sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
            ts_act = _pkg().ts_activation_label()
            ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
            sell_score = config.SELL_STRATEGY["SELL_SCORE"]
            tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
            invest_ratio_str = config.format_invest_ratio()

            use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
            time_stop_days = config.SELL_STRATEGY["TIME_STOP_DAYS"]

            msg += "\n\n⚙️ [적용 전략]"
            if config.session.is_toss:
                # 토스는 체결강도 미제공 → 매도잔량비 게이트로 대체
                buy_abr = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
                msg += f"\n• 매수: {buy_score}점↑ & RSI {buy_rsi}↓ & 매도잔량비 {buy_abr}배↑"
            else:
                msg += f"\n• 매수: {buy_score}점↑ & RSI {buy_rsi}↓ & 체결강도 {buy_vol}%↑"
            # [수정] 0=미사용 규칙(RSI 과열·고정 익절)은 조건을 표시하지 않는다
            #  ("RSI 0.0 초과"/"익절 +0.0%"처럼 OFF 규칙이 활성으로 보이던 표시 모순 해소)
            msg += f"\n• 매도: {sell_score}점 미만+60일선 이탈"
            if tp_rsi > 0:
                msg += f" / RSI {tp_rsi} 초과"

            if tp > 0:
                tp_str = f"+{tp}%"
                if use_half_tp:
                    half_tp_rate = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_RATE", tp / 2.0)
                    tp_str += f" (반익절 +{half_tp_rate:.1f}%)"
            else:
                tp_str = "미사용 (추세추종: TS 주청산)"
            
            if use_atr_stop:
                sl_str = f"ATR 동적손절 (x{atr_mult})"
            else:
                sl_str = f"고정 {sl}%"
            
            msg += f"\n• 익절: {tp_str}"
            msg += f"\n• 손절: {sl_str}"
            msg += f"\n• 트레일링: {ts_act} 도달 후 -{ts_call}%"
            if use_time_stop:
                msg += f"\n• 시간청산: {time_stop_days}일 경과"
            msg += f"\n• 비중: 종목당 {invest_ratio_str}"
                
            # [복원] 보유 종목 현황 추가
            valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []

            if valid_holdings:
                from modules import account
                analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
                msg += "\n\n" + _pkg().format_holdings_block(valid_holdings, analysis_results=analysis_results)
            else:
                msg += "\n\n📋 [보유 종목] 없음"
                if stock_eval_amt > 0:
                    msg += " (⚠️ 평가금액 존재 - API 데이터 불일치)"

            target_cano = config.session.auto_cano
            with utils.AccountContext(target_cano):
                from modules.telegram_bot import TelegramCommander
                reply_markup = TelegramCommander()._get_default_keyboard()
                api.send_telegram_message(msg, reply_markup=reply_markup)

            # 초기화에 사용된 데이터는 비워줌
            self.initial_holdings = None
            self.initial_summary = None
            self.initialized = False

        except Exception as e:
            # 매매 루프가 뜨지 못했으면 잠금을 붙들고 있을 이유가 없다.
            # (붙들면 다음 실행 시도가 '이미 실행 중'으로 거부된다)
            if not self.is_running:
                self._release_instance_lock()
            logger.error(f"자동매매 시작 실패: {e}")
            #  루프가 뜬 뒤의 예외(시작 알림 조립 중 등)는 매매가 돌고 있으니 기동 실패가 아니다.
            if not self.is_running:
                self._report_start_failure(e)
            if api._is_screen_output_allowed():
                console.print(f"[bold red]자동매매 시작 실패: {e}[/bold red]")
                if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                    console.print(f"[bold red][ERROR] 자동매매 시작 실패: {e}[/bold red]")

            if self.initial_asset > 0:
                target_cano = config.session.auto_cano
                acnt = config.session.auto_acnt_prdt_cd
                account_key = f"{target_cano}-{acnt}"
                saved_msg = " (당일 기준 복원)" if load_daily_initial_asset(account_key) > 0 else " (당일 기준 저장)"
                self.log(f"시스템 시작 자산: {self.initial_asset:,}원{saved_msg}")

    def stop(self, use_status=True):
        if not self.is_running:
            if use_status:
                console.print("\n[yellow]실행 중인 자동매매가 없습니다.[/yellow]")
            return
            
        def _stop_logic():
            self.is_running = False
            _pkg().ConclusionMonitor().stop() # [추가] 체결 감시 모니터 종료
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout=15) # [수정] 타임아웃 연장 (종목 분석 등 백그라운드 스레드 정상 종료 대기)
            # 루프가 멈춘 뒤에 잠금을 푼다 — 먼저 풀면 정지 중에 다른 인스턴스가 끼어든다.
            self._release_instance_lock()

        if use_status:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=console,
                transient=True
            ) as progress:
                progress.add_task("[cyan]시스템 중단 요청 처리 중...[/cyan]", total=None)
                _stop_logic()
        else:
            _stop_logic()

        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            if use_status:
                console.print("\n[bold red]경고: 시스템 트레이딩 스레드가 응답하지 않습니다. (DB/API 작업 지연)[/bold red]")
                console.print("[dim]강제로 중단 절차를 진행합니다. 일부 데이터가 누락될 수 있습니다.[/dim]")

        if use_status:
            console.print("\n[red]자동매매 시스템이 중단되었습니다.[/red]")
            # 정상 정지 완료는 ERROR가 아님 → 진단(TRACE/DEBUG)에서만 중립 색으로 표기
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                console.print("[dim]시스템 트레이딩 정지 완료[/dim]")
            
        self.log("시스템 중단")
        
        # [수정] 텔레그램 전송 시 AUTO 계좌 정보가 포함되도록 컨텍스트 설정
        msg = f"⚪️ [시스템 종료] 자동매매가 종료되었습니다.\n시작 자산: {self.initial_asset:,}원"
        
        # [수정] 스레드가 종료된 경우에만 자산 및 보유 종목 조회 (락 충돌 방지)
        if not self.thread or not self.thread.is_alive():
            # [추가] 종료 시 최종 자산 현황 요약 전송
            deposit = 0
            stock_eval = 0
            final_asset = 0
            is_data_valid = False # [추가] 데이터 유효성 플래그

            target_cano = config.session.auto_cano
            with utils.AccountContext(target_cano):
                try:
                    # 1. 예수금 조회
                    acnt = config.session.auto_acnt_prdt_cd
                    res = api.get_deposit_balance(target_cano, acnt)
                    if res:
                        # [수정] 자산 계산 시 D+2 예수금(가수도금) 사용 (매도 대금 포함) - start()와 통일
                        # [Fix] 주문가능금액(d2_deposit)이 아닌 실제 D+2 가수도금(d2_real)을 사용하여 50원 오차 등 왜곡 방지
                        d2_val = res.get('d2_real', 0)
                        if d2_val == 0:
                            d2_val = res.get('d2_deposit', 0)
                        deposit = d2_val + res.get('foreign_deposit', 0)
                        is_data_valid = True
                    else:
                        deposit = 0

                    # 2. 잔고 및 평가금 조회
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    tot_profit = 0
                    tot_pchs = 0
                    unmanaged_count = 0     # 정지로 감시가 끊기는 포지션 수

                    if holdings:
                        valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                        unmanaged_count = len(valid_holdings)
                        stock_eval = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
                        tot_profit = sum(api.safe_int(h.get('evlu_pfls_amt')) for h in valid_holdings)
                        tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in valid_holdings)
                    elif summary and len(summary) > 0:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt', 0))
                        tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl', 0))

                    if is_data_valid:
                        final_asset = deposit + stock_eval
                        # 기준선은 입출금 보정을 담아 float 로 온다. 금액 표시는 원 단위이므로
                        #  여기서 정수로 되돌린다 — 안 그러면 '+100,000.0원'이 나간다.
                        _base = self.daily_pnl_base()
                        profit = int(round(final_asset - _base))
                        profit_rate = 0.0 if _base <= 0 else (profit / _base) * 100
                        stock_rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0

                        msg += f"\n최종 예수금: {deposit:,}원"
                        msg += f"\n증권 평가 자산: {stock_eval:,}원"
                        msg += f"\n증권 평가 손익: {tot_profit:+,}원 ({stock_rate:+.2f}%)"
                        msg += f"\n금일 최종 손익: {profit:+,}원 ({profit_rate:+.2f}%)"
                    else:
                        msg += "\n(⚠️ 종료 시 자산 정보 조회 실패 - 서버 응답 없음)"

                    # [안전장치] 정지는 매도 감시 루프까지 함께 끈다. 이 코드베이스는 같은
                    #  이유로 일일 손실 한도 초과와 Kill Switch를 '정지'에서 '방어 모드'로
                    #  바꿨다(engine.check_loss_limit 주석: "정지는 포지션을 청산하지 않고
                    #  매도 감시 루프까지 함께 끄기 때문에 무방비 상태가 된다").
                    #  명시적 정지까지 막을 일은 아니다 — 다만 무엇을 껐는지는 알려야 한다.
                    #  종전 종료 알림은 자산만 보고하고 이 사실을 말하지 않았다.
                    #  종목 목록은 붙이지 않는다 — 같은 알림 아래에 '최종 보유 종목 현황'이
                    #  수익률까지 담아 그대로 이어지므로 두 번 나열하는 셈이었다.
                    if unmanaged_count:
                        msg += (f"\n\n⛔ 보유 {unmanaged_count}종목의 "
                                f"손절·트레일링 감시가 함께 멈춥니다.")

                    # [추가] 금일 매매 요약 집계
                    buy_cnt = 0
                    sell_cnt = 0
                    best_stock = None
                    worst_stock = None
                    max_p = 0
                    min_p = 0
                    try:
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        target_account = (f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}" if config.session.auto_cano else None)
                        today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=False, account=target_account)
                        
                        today_trades_parsed = []
                        for r in reversed(today_trades):
                            simple_type = "buy" if "매수" in r['type'] or "buy" in r['type'].lower() else "sell"
                            parsed_r = dict(r)
                            parsed_r['type'] = simple_type
                            today_trades_parsed.append(parsed_r)
                            
                        today_trades_refined = self._refine_trade_records(today_trades_parsed)
                        # [추가] 체결된 내역만 당일 매매 요약에 포함
                        today_trades_refined = [r for r in today_trades_refined if "체결" in r.get('order_status', '')]
                        
                        buy_cnt = len([x for x in today_trades_refined if x['type'] == 'buy'])
                        sell_cnt = len([x for x in today_trades_refined if x['type'] == 'sell'])
                        
                        stock_profits = {}
                        for t in today_trades_refined:
                            if t['type'] == 'sell':
                                code = t.get('code', 'unknown')
                                name = t.get('name', 'Unknown')
                                p_amt = int(float(t.get('profit_amt') or 0))
                                if code not in stock_profits:
                                    stock_profits[code] = {'name': name, 'profit': 0}
                                stock_profits[code]['profit'] += p_amt
                                
                        for code, info in stock_profits.items():
                            if info['profit'] > max_p:
                                best_stock = info
                                max_p = info['profit']
                            if info['profit'] < min_p:
                                worst_stock = info
                                min_p = info['profit']
                    except Exception as e:
                        self.log(f"종료 시 매매 요약 조회 실패: {e}")
                        
                    msg += f"\n오늘 매매 요약: 매수 {buy_cnt}건 / 매도 {sell_cnt}건"
                    if best_stock:
                        msg += f"\n최고 수익: {best_stock['name']} (+{max_p:,}원)"
                    if worst_stock:
                        msg += f"\n최대 손실: {worst_stock['name']} ({min_p:,}원)"

                    # [수정] 보유수량 0 초과인 종목만 필터링
                    valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []

                    if valid_holdings:
                        from modules import account
                        analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
                        msg += "\n\n" + _pkg().format_holdings_block(valid_holdings, title="최종 보유 종목 현황", analysis_results=analysis_results)
                    else:
                        msg += "\n\n📋 [최종 보유 종목] 없음"
                        if not is_data_valid:
                            msg += " (조회 실패 가능성 있음)"
                except Exception as e:
                    self.log(f"종료 시 자산/잔고 조회 실패: {e}")
                    msg += "\n(자산 조회 실패)"
        else:
            msg += "\n(시스템 응답 지연으로 최종 자산 정보 생략)"
            self.log("스레드 종료 지연으로 최종 자산/잔고 조회 생략")

        # [운영 안전] 미체결 주문은 시스템을 꺼도 거래소에 살아 있다. 정지 뒤에 체결되면
        #  손절·트레일링 감시가 전혀 없는 포지션이 된다. 위 '최종 보유 종목'은 정지 시점의
        #  잔고라 그 주문을 보여주지 못하고 "보유 없음"으로 끝나므로, 운용자는 무감시
        #  포지션이 생겼다는 것을 알 방법이 없다. 정지 시점에 명시적으로 알린다.
        try:
            pending = []
            om = getattr(self, 'order_manager', None)
            if om is not None:
                with om._lock:
                    for _code, _orders in (om.pending_orders or {}).items():
                        if _orders:
                            pending.append((_code, len(_orders)))
            if pending:
                total = sum(n for _, n in pending)
                detail = ", ".join(f"{c}({n}건)" for c, n in pending)
                msg += (f"\n\n⚠️ [미체결 주문 {total}건 잔존] {detail}\n"
                        f"시스템을 꺼도 거래소의 주문은 살아 있습니다. 정지 후 체결되면 "
                        f"손절·트레일링 감시가 없는 포지션이 됩니다 — 증권사 앱에서 "
                        f"취소하거나 시스템을 다시 켜 주십시오.")
        except Exception as e:
            self.log(f"종료 시 미체결 주문 확인 실패: {e}")

        target_cano = config.session.auto_cano
        with utils.AccountContext(target_cano):
            from modules.telegram_bot import TelegramCommander
            reply_markup = TelegramCommander()._get_default_keyboard()
            api.send_telegram_message(msg, reply_markup=reply_markup)
        
        # [추가] 로거 연결 해제 (메시지 전송 후 해제)
        context.SYSTEM_LOGGER = None
