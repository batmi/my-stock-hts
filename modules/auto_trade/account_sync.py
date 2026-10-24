"""AutoTrader 의 계좌 동기화 — 오늘의 자본 기준선·입출금 감지(장중·정지 중)·계좌 차단기(일일 손실 한도)·
주기마다의 계좌 상태 기록·기동 시 외부 체결 동기화.

[왜 나눴나 · 2026-10-25 C1 3단계] 이 메서드들은 '계좌가 지금 얼마이고, 그 변화 중 무엇이 손익이고 무엇이
 입출금인가'를 정한다. 그 결론(initial_asset·baseline_principal·net_transfer_today·current_total_asset·
 자산 이력의 순입출금)을 일일 손실 한도·사이징·드로다운이 함께 쓴다. 한 파일에 섞여 있으면 입출금 규칙 하나를
 고칠 때 매매 파일 전체를 함께 읽어야 하고, 계좌 변경이 매매 파일의 변경으로 보인다.

[어떻게 나눴나] 1·2단계(reporting·market_risk)와 같은 규약이다. 메서드 본문은 한 글자도 바꾸지 않고 옮겼고,
 AutoTrader 가 이 믹스인을 상속한다 — self.effective_baseline() · patch.object(AutoTrader, ...) 가 전과 같이
 동작한다. 모듈 전역 이름(config·api·account·db_manager·datetime …)은 trader.py 와 **같은 객체**를 가리키게
 import 했다(tests/test_trader_split_parity.py 가 이름마다 대조한다). 로거도 trader 와 같은 이름을 쓴다.
 이 메서드들만 쓰던 상수(차단기 경보 횟수·오프라인 입출금 문턱)도 함께 옮겼다. 동등성 기준은 주기 재생 기록
 세 시나리오(tests/test_cycle_replay.py — base·market_stress·account_sync)다.

[주의] 테스트에서 이 메서드들이 쓰는 전역·상수를 바꿔 끼우거나 읽을 때는 trader 가 아니라 이 모듈을 지정한다
 (예: modules.auto_trade.account_sync.OFFLINE_TRANSFER_FLOOR). 패키지 네임스페이스 경유 호출은 _pkg() 그대로다.
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import api
import config
from modules import account
from modules import db_manager
from modules import holdings_backfill
from modules.auto_trade.common import (_get_trade_account, is_plausible_baseline, load_daily_initial_asset,
                                       save_daily_initial_asset)

#  트레이더 로그와 같은 이름 — 계좌 동기화 코드가 남기는 로그도 'modules.auto_trade.trader' 로 모인다.
logger = logging.getLogger("modules.auto_trade.trader")

#  계좌 차단기(일일 손실 한도) 점검이 이만큼 연속 실패하면 알린다. 한 번은 일시적
#  데이터 결손일 수 있지만, 연속 실패는 '차단기가 꺼져 있다'는 뜻이다.
CIRCUIT_BREAKER_ALERT_FAILS = 3

#  [오프라인 입출금 확정 문턱] 원금 대조에는 잔돈이 남는다 — 매수 수수료는 실현손익에
#   들어가지 않고, 세금·배당도 원 단위로 어긋난다. 그 잡음을 입출금으로 확정하면
#   (출금 방향일 때) 자산 고점이 낮아져 리스크 한도가 조용히 열린다.
#   그래서 절대 하한(장중 감지와 같은 5만원)과 계좌 규모 대비 비율 중 **작은 쪽**을 쓴다.
#   비율을 함께 두는 이유: 잡음은 계좌 규모에 비례하는데 5만원 고정이면 소액 계좌에서
#   전 재산이 빠져나가도(실제 사례: 10,027원 계좌의 1만원 출금 → 가짜 드로다운 99.7%)
#   문턱을 못 넘는다.
OFFLINE_TRANSFER_ABS_MIN = 50_000.0
OFFLINE_TRANSFER_RATIO = 0.005
OFFLINE_TRANSFER_FLOOR = 100.0


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자 — trader._pkg 와 같은 규약.

    테스트의 patch('modules.auto_trade.X') 가 이 모듈 안의 호출에도 걸리게 한다. (지연 import 라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class TraderAccountSyncMixin:
    """AutoTrader 의 계좌 동기화 메서드. 단독으로 쓰지 않는다 — 트레이더 상태(self.initial_asset·self.risk_manager …)를 쓴다."""

    #  이 믹스인이 기대는 트레이더 상태·메서드(정의·초기값은 AutoTrader). 값 없는 선언이라 실행 시에는
    #  AutoTrader 의 것이 그대로 쓰인다. getattr 기본값으로 읽는 상태는 빠져 있다.
    initial_asset: Any
    baseline_principal: Any
    risk_manager: Any
    log: Callable[..., None]
    resume_buys: Callable[..., Any]
    _refine_trade_records: Callable[..., list]

    def effective_baseline(self):
        """리스크 판정에 쓸 **오늘의 자본 기준선** = 시작 자산 + 오늘 누적 순입출금.

        [왜 파생값인가] 종전에는 입출금이 감지되면 initial_asset 자체를 옮겼다. 그러려면
        3주기 확인·자동 조정 상한·파일 저장이 필요했고, 상한을 넘으면 사람이 손대기 전까지
        **기준이 틀린 채로 하루가 갔다**(출금이면 일일 손실 한도가 종일 헛발동한다).

        순입출금은 원금 불변량(현금+매입원가-실현손익)의 변화라 매 주기 정확히 다시 잴 수
        있다. 저장하지 않고 판정할 때마다 재면 상한도 대기도 필요 없고, 스냅샷이 한 번
        튀어도 다음 주기에 저절로 낫는다 — 잘못된 값이 굳지 않는다.

        [옮기지 않는다] 입출금이 감지돼도 initial_asset·baseline_principal 은 그대로 둔다.
        옮기면 이 파생값이 0이 되어 같은 결과가 나오지만, 옮기는 쪽은 되돌릴 수 없고
        추정이 틀리면 잘못된 기준이 그대로 굳는다. 여러 날에 걸친 드로다운 기준도 이력을
        고치지 않고 daily_asset_history.net_transfer 로 환산한다(get_max_daily_asset).
        """
        # 산식은 RiskManager._equity_baseline 이 단독 보유한다 — 차단기·사이징과
        #  갈라지면 세 장치가 서로 다른 자본을 보게 된다.
        return self.risk_manager._equity_baseline()

    def daily_pnl_base(self):
        """표시용 일일 손익의 분모. **판정과 같은 기준선**을 쓴다(입출금 보정 포함).

        [왜] 출금은 손실이 아니다. 원본 시작 자산으로 나누면 1,000만 계좌에서 300만을 뺀
        순간 화면·텔레그램·종료 요약이 모두 -30%를 띄운다. 판정(차단기·사이징)은 이미
        보정된 기준선을 보는데 표시만 안 보면, 운용자는 시스템이 못 본 손실이 난 줄 알고
        개입하게 된다 — 자동으로 도는 시스템에서 가장 나쁜 종류의 오표시다.
        """
        base = self.effective_baseline()
        return float(base) if base > 0 else float(self.initial_asset or 0)

    def transfer_note(self):
        """오늘 순입출금이 있으면 손익 옆에 붙일 꼬리말. 없으면 빈 문자열."""
        net = getattr(self, 'net_transfer_today', 0) or 0
        return f" ※{'입금' if net > 0 else '출금'} {abs(int(net)):,}원 제외" if net else ""

    @staticmethod
    def _offline_transfer_threshold(baseline_principal):
        """오프라인 입출금으로 확정할 최소 금액. (상수 주석 참조)"""
        base = abs(float(baseline_principal or 0))
        return max(OFFLINE_TRANSFER_FLOOR,
                   min(OFFLINE_TRANSFER_ABS_MIN, base * OFFLINE_TRANSFER_RATIO))

    def _realized_matches_broker(self, start_date, end_date, realized_db, tolerance):
        """구간 실현손익을 **우리가 다 알고 있는가**를 증권사 장부와 대조한다.

        [왜 필요한가] 우리 DB는 우리가 아는 매매만 담는다. 기동 동기화(holdings_backfill)는
         **지금 보유 중인 종목**을 역산해 채우므로, 시스템이 꺼진 사이에 사서 그 사이에 판
         왕복매매는 기록이 아예 없다(그 모듈의 [한계] 주석). 그러면 그 실현손익이 통째로
         가짜 입출금이 된다 — 손실 쪽이면 자산 고점을 낮춰 **리스크 한도가 조용히 열린다**.
         행이 없으니 '외부 매도' 표식으로도 잡을 수 없다. 증권사 장부만이 답을 안다.

        [값으로 쓰지 않고 대조만 한다] rlzt_pfls 가 제비용을 포함하는지가 계좌·응답에 따라
         다르게 관측된다. 그 숫자를 그대로 식에 넣으면 규약이 어긋난 순간 없는 입출금을
         만들어낸다. 그래서 두 해석(제비용 포함/미포함) 중 **하나라도** 우리 합계와 맞으면
         '알고 있다'로 보고 통과시키고, 둘 다 어긋날 때만 보류한다.

        조회 자체가 불가능하면(모의·토스·관찰 모드, 통신 실패) 대조를 건너뛴다 — 이 대조는
        추가 방어막이지 전제 조건이 아니다. 여기서 막으면 정작 필요한 보정까지 사라진다.
        """
        try:
            cano, acnt = _get_trade_account()
            book = account.fetch_period_realized(cano, acnt, start_date, end_date)
            if not book:
                return True                    # 대조 불가 — 기존 판정을 유지한다
            gap = min(abs(realized_db - book['realized']),
                      abs(realized_db - (book['realized'] - book['cost'])))
            if gap <= max(tolerance, 1000.0):
                return True
            self.log(f"[오프라인 입출금] 증권사 실현손익({book['realized']:+,}원)과 기록"
                     f"({realized_db:+,}원)이 {gap:,.0f}원 어긋납니다 — 우리가 모르는 매매가 "
                     f"있어 보정을 보류합니다. (수동 매매 기록은 보유 종목만 복원됩니다)")
            return False
        except Exception as e:
            logger.debug(f"[오프라인 입출금] 증권사 실현손익 대조 실패(보정은 계속): {e}")
            return True

    def _reconcile_offline_transfer(self, account_key, current_principal, realized_ok):
        """프로그램이 꺼져 있던 사이의 입출금을 스스로 되찾아 자산 이력에 적는다.

        [왜 필요한가] 장중 감지(_monitor_account_status)는 **그날 기준 원금이 잡힌 뒤**의
         변화만 본다. 프로그램이 꺼진 사이의 입출금은 잴 주체가 없어 영영 기록되지 않고,
         출금이면 옛 자산이 그대로 고점으로 남아 룩백(DD_LOOKBACK_DAYS, 90일) 내내
         가짜 드로다운이 리스크 한도를 묶는다.
         [실사고 2026-08-31] 계좌 44048158-01에서 프로그램이 꺼져 있던 7/28~8/4 사이에
          약 1만원이 빠졌다. 07-27의 10,027원이 고점으로 남아 드로다운이 99.7%로
          계산됐고(현재 27원), 운용자가 DB를 직접 고치기 전에는 풀리지 않았다.

        [식] 원금(현금+매입원가-실현손익)은 입출금이 없으면 그 사이의 실현손익만큼만
         움직인다. 따라서
             순입출금 = (오늘 원금) - (마지막으로 남긴 원금) - (그 사이의 실현손익)
         오늘의 실현손익은 양변에서 상쇄되므로 합산 구간은 [그날, 어제]다.

        [어느 행에 적는가] 마지막 스냅샷을 남긴 그날의 행이다. 자산 행은 그날 '시작'
         스냅샷이고 환산식은 'd일 이후에 일어난 입출금'을 d의 자산에 더하므로
         (get_max_daily_asset), 그 이후에 일어난 이 입출금은 그 행에 실려야 한다.
         오늘 행에 적으면 이미 반영된 오늘 자산에 한 번 더 더해져 이중 계산이 된다.

        [모르면 안 건드린다] 실현손익을 못 쟀거나(realized_ok=False), 대조점이 거래
         보존 기간 밖이면(그 사이 매도 기록이 지워져 실현손익 합이 잘린다) 아무것도
         하지 않는다. 잘린 실현손익은 그대로 가짜 '출금'이 되고, 출금 방향의 오탐은
         고점을 낮춰 **한도를 여는** 위험한 방향이다.

        반환: 기록한 금액(입금 +, 출금 −). 아무것도 안 했으면 0.
        """
        if not realized_ok or not current_principal or current_principal <= 0:
            return 0
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            # [하루 1회] 시세 결손 등으로 그날 기준 자산이 끝내 안 잡히면 이 블록이 매 주기
            #  다시 돌 수 있다. 그러면 같은 입출금을 주기마다 가산한다 — 오늘 대조점을 남기는
            #  것만으로는 못 막는다(대조점은 기준 자산이 잡혀야 저장되기 때문).
            if getattr(self, '_offline_reconcile_date', None) == today:
                return 0
            snap = db_manager.db.get_last_principal_snapshot(account_key, today)
            if not snap:
                return 0                       # 대조점 없음 = 이 계좌의 첫 운용
            last_date, last_principal = snap
            if last_principal <= 0:
                return 0
            if last_date >= today:
                return 0                       # 오늘 이미 대조를 끝냈다(같은 날 재기동).
                                               #  오늘 이후의 변화는 장중 감지가 맡는다 —
                                               #  여기서 또 재면 같은 입출금을 두 번 적는다.

            retention = int(getattr(config, 'DB_DATA_RETENTION_DAYS', 365) or 365)
            gap_days = (datetime.now() - datetime.strptime(last_date, "%Y-%m-%d")).days
            if retention > 0 and gap_days > retention - 7:
                self.log(f"[오프라인 입출금] 마지막 대조점({last_date})이 거래 보존 기간 밖이라 "
                         f"보정을 건너뜁니다 — 실현손익 합을 신뢰할 수 없습니다.")
                return 0

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            realized, ok = db_manager.db.get_realized_profit_between(
                last_date, yesterday, account_key)
            if not ok:
                self.log("[오프라인 입출금] 구간 실현손익을 신뢰할 수 없어 보정하지 않습니다.")
                return 0          # 못 쟀으면 표시하지 않는다 — 다음 주기에 다시 해 본다

            threshold = self._offline_transfer_threshold(last_principal)
            if not self._realized_matches_broker(last_date, yesterday, realized, threshold):
                return 0
            self._offline_reconcile_date = today      # 쟀다 = 오늘 몫은 끝났다

            residual = float(current_principal) - float(last_principal) - float(realized)
            if abs(residual) < threshold:
                return 0

            amount = int(round(residual))
            if not db_manager.db.add_net_transfer(last_date, account_key, amount):
                return 0                       # 그날 행이 없다 = 적을 자리가 없다

            self._hwm_cache_date = None        # 환산이 바뀌었으니 드로다운을 다시 잰다
            action_str = "입금" if amount > 0 else "출금"
            self.log(f"💰 정지 중 발생한 예수금 {action_str} 자동 반영: {amount:+,}원 "
                     f"(대조점 {last_date}, 구간 실현손익 {int(realized):+,}원)")
            try:
                api.send_telegram_message(
                    f"💰 [정지 중 {action_str} 자동 반영]\n"
                    f"프로그램이 꺼져 있던 사이({last_date} 이후)의 {action_str} "
                    f"약 {abs(amount):,}원을 확인해 자산 이력에 반영했습니다.\n\n"
                    f"✅ 드로다운 기준(자산 고점)이 이 금액을 빼고 계산되므로 "
                    f"조치할 것은 없습니다.\n"
                    f"(대조: {last_date} 원금 {int(last_principal):,}원 → 현재 "
                    f"{int(current_principal):,}원, 그 사이 실현손익 {int(realized):+,}원)")
            except Exception:
                pass
            return amount
        except Exception as e:
            logger.warning(f"[오프라인 입출금] 보정 실패 — 그대로 둡니다: {e}")
            return 0

    def _reevaluate_buy_halt_after_transfer(self, current_total, action_str="입출금"):
        """입출금으로 기준 자산을 옮긴 뒤, 방어 모드를 새 기준으로 다시 잰다.

        [왜] 출금은 기준 자산이 정정되기 **전** 3주기 동안 그대로 '손실'로 보인다. 그 사이
        일일 손실 한도가 걸리면 방어 모드가 켜지는데, 기준을 고쳐도 그것은 날짜가 바뀔
        때까지 풀리지 않았다(halt_buys는 당일 재발동만 막는다). 즉 **정상적인 출금 한 번이
        그날 신규 진입을 통째로 멈췄다.** 계좌 잔고가 수시로 변하는 실계좌에서는 흔한 일이다.

        일일 손실로 걸린 방어 모드만 다시 잰다 — 다른 사유(수동·장애)까지 풀면 안 된다.
        새 기준으로도 한도를 넘으면 그대로 둔다. 풀었는데 다음 주기에 진짜로 넘으면
        차단기가 다시 건다(같은 날 재발동은 halt_buys가 허용한다 — 해제로 날짜가 지워진다).
        """
        if getattr(self, 'buy_halt_kind', None) != 'daily_loss':
            return False
        try:
            limit_pct = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
            # 차단기와 **같은 기준선**을 써야 한다. initial_asset을 직접 보면, 기준을 옮기지
            #  않고 순입출금으로만 보정되는 경로(대부분의 경우)에서 영영 풀리지 않는다.
            baseline = self.effective_baseline()
            if baseline <= 0 or current_total <= 0:
                return False
            new_rate = (current_total - baseline) / baseline * 100.0
            if new_rate <= -limit_pct:
                return False        # 새 기준으로도 한도 초과 — 그대로 둔다
            self.resume_buys(f"{action_str} 반영으로 기준 자산 정정 "
                             f"(손익률 {new_rate:+.2f}% / 한도 -{limit_pct}%)")
            api.send_telegram_message(
                f"✅ [방어 모드 해제] {action_str}을 기준 자산에 반영했습니다.\n"
                f"다시 잰 손익률 {new_rate:+.2f}% (한도 -{limit_pct}%) — 신규 매수를 재개합니다.")
            return True
        except Exception as e:
            logger.debug(f"[입출금] 방어 모드 재평가 실패: {e}")
            return False

    def _sync_external_fills(self, cano, acnt, holdings):
        """기동 시 외부 체결을 DB에 채우고, 자동 계좌 외부 매수분은 제한 종목으로 올린다.

        [왜 기동 경로인가] 실시간 감지(ConclusionMonitor)는 get_today_history 라 '오늘'만
          본다. 시스템이 꺼져 있던 날의 외부 체결은 다음 날 켜도 들어오지 않는다.

        [왜 제한까지 거는가] 자동 계좌에서 운용자가 직접 산 종목을 시스템이 '자기
          포지션'으로 알고 관리하면, 제 손절 기준으로 운용자의 포지션을 청산한다.
          실시간 경로는 이미 같은 처리를 하는데 기동 경로에만 이 방어가 없었다.
          수동 계좌는 시스템이 보지도 팔지도 않으므로 제한이 필요 없다.

        동기화 실패가 자동매매 기동 자체를 막아선 안 된다 — 기록은 부가 정보다.
        """
        try:
            res = holdings_backfill.sync_account(
                cano, acnt, holdings=holdings,
                register_restrictions=holdings_backfill.is_system_account(cano))
        except Exception as e:
            self.log(f"[기동 동기화] 외부 체결 동기화 실패 — 매매는 계속합니다: {e}")
            return

        if res.get('error'):
            self.log(f"[기동 동기화] 실패 — 매매는 계속합니다: {res['error']}")
            return
        if not res['written'] and not res['restricted']:
            return

        self.log(f"[기동 동기화] 외부 체결 {res['written']}건을 기록했습니다."
                 + (f" 제한 등록: {', '.join(res['restricted'])}" if res['restricted'] else ""))

        msg = f"🔄 [기동 동기화] 정지 중 발생한 외부 체결 {res['written']}건을 기록했습니다."
        if res['restricted']:
            # [판정 근거를 밝힌다] '외부 매수' 판정은 '우리 DB에 주문 기록이 없다'가 근거다.
            #  시스템이 낸 주문은 접수 응답 즉시 기록되므로 보통 맞지만, 주문 응답이 유실되고
            #  대사까지 실패하면(그때 '주문 결과 불명' 알림이 나간다) 기록 없이 체결만 남는다.
            #  그 포지션이 여기서 제한으로 잡히면 시스템이 손절해 주지 않는다 — 운용자가
            #  뒤집을 수 있도록 근거와 해제 경로를 함께 적는다.
            msg += ("\n\n⛔ 아래 종목은 운용자가 직접 매수한 것으로 보고 "
                    "시스템 매매에서 제외했습니다.\n· " + "\n· ".join(res['restricted']))
            msg += ("\n\n판정 근거는 '이 계좌의 매수인데 시스템 주문 기록이 없다'입니다. "
                    "직전에 '주문 결과 불명(응답 유실)' 알림을 받으셨다면 시스템이 낸 주문일 수 "
                    "있습니다 — 그 경우 제한을 풀어야 손절·트레일링이 다시 돕니다 "
                    "(자동매매 메뉴의 제한 종목 관리).")
        if res['partial']:
            msg += ("\n\n⚠️ 조회 구간보다 과거에 진입해 일부만 복원된 종목:\n· "
                    + "\n· ".join(f"{n}({c}) {m}주" for c, n, m in res['partial']))
        #  본문에 "제한을 풀어야 손절·트레일링이 다시 돕니다"가 들어 있다 — 기동 시
        #  한 번만 나가는 안내이므로, 놓치면 그 종목이 무방비로 남은 것을 아무도 모른다.
        if not _pkg().alert_delivered(msg):
            logger.error("[보유분 복원] 결과 알림을 전달하지 못했습니다 — "
                         "제한 등록으로 손절이 멈춘 종목이 있어도 운영자가 알 수 없습니다.")

    def _run_account_circuit_breaker(self, current_total):
        """계좌 차단기 — 일일 손실 한도 점검과 기준 평가자산 갱신.

        [왜 따로 떼는가] 이 호출은 표시·로깅과 같은 try 블록에 있으면 안 된다. 감싸는
        핸들러가 `except Exception: pass` 라서, 손익 표시나 입출금 감지가 던지는 순간
        차단기가 **조용히** 건너뛰어진다. 게다가 그런 예외는 손실이 큰 날에 더 잘 난다
        (다룰 값이 많아지므로) — 정확히 차단기가 필요한 날에 꺼지는 구조였다.

        여기서도 예외를 잡되 **삼키지 않는다**. 실패를 세어 상태창에 드러내고, 반복되면
        알린다. 차단기가 안 도는 것을 아무도 모르는 상태가 가장 나쁘다.
        """
        try:
            # 비정상 급감(API 누락 의심) 데이터는 기준자산에 반영하지 않는다.
            # [Fix 2026-09-01] 기준을 initial_asset(원본)이 아니라 **입출금 보정된 기준선**과
            #  대야 한다. 자산의 절반이 넘는 출금은 정상 거래인데, 원본과 대면 그날 내내
            #  '비정상 급감'으로 읽혀 current_total_asset 이 출금 전 값에 얼어붙는다. 그 값은
            #  히트 캡의 분모이자 드로다운의 현재 자산이므로, 없는 돈 기준으로 한도가
            #  계산되고 드로다운은 과소평가된다(= 한도가 조용히 열린다).
            #  check_loss_limit 은 이미 같은 판정을 보정된 기준선으로 한다 — 둘을 맞춘다.
            _floor_base = self.effective_baseline() or self.initial_asset
            if current_total > 0 and not (_floor_base > 0
                                          and current_total < _floor_base * 0.5):
                self.current_total_asset = current_total
            self.risk_manager.check_loss_limit(current_total)
            self.circuit_breaker_ran_at = time.time()
            self.circuit_breaker_fails = 0
        except Exception as e:
            self.circuit_breaker_fails = getattr(self, 'circuit_breaker_fails', 0) + 1
            logger.error(f"[계좌 차단기] 일일 손실 한도 점검 실패({self.circuit_breaker_fails}회): {e}")
            self.log(f"[계좌 차단기] 점검 실패 — 일일 손실 한도가 감시되지 않고 있습니다: {e}")
            if self.circuit_breaker_fails == CIRCUIT_BREAKER_ALERT_FAILS:
                api.send_telegram_message(
                    f"⚠️ [계좌 차단기 이상] 일일 손실 한도 점검이 {self.circuit_breaker_fails}회 "
                    f"연속 실패했습니다.\n손실이 한도를 넘어도 신규 매수가 차단되지 않습니다.\n"
                    f"오류: {e}")

    def _monitor_account_status(self, holdings, summary, deposit_res):
        """현재 보유 종목 상태 로깅 및 자산 손실 제한(Loss Cut) 체크"""
        try:
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
            
            if not valid_holdings:
                self.log("보유 종목: 없음")
            else:
                # 한글 정렬 보정 헬퍼 함수
                def get_display_width(s):
                    return len(s) + sum(1 for c in s if ord(c) > 127)

                def pad(s, width, align='>'):
                    real_len = get_display_width(s)
                    pad_len = width - real_len
                    if pad_len < 0: pad_len = 0
                    if align == '<': return s + ' ' * pad_len
                    else: return ' ' * pad_len + s

                max_name_width = 20
                for item in valid_holdings:
                    name = f"{item['prdt_name']} ({item['pdno']})"
                    w = get_display_width(name)
                    if w > max_name_width:
                        max_name_width = w
                        
                name_col_width = max(30, max_name_width + 2)
                line_length = name_col_width + 95

                # 헤더 출력
                header = (
                    f"{pad('종목명', name_col_width, '<')} "
                    f"{pad('보유수량', 10, '>')} "
                    f"{pad('매입단가', 12, '>')} "
                    f"{pad('현재가', 12, '>')} "
                    f"{pad('매입금액', 15, '>')} "
                    f"{pad('평가금액', 15, '>')} "
                    f"{pad('평가손익', 14, '>')} "
                    f"{pad('수익률', 10, '>')}"
                )
                
                self.log("─" * line_length)
                self.log(header)
                self.log("─" * line_length)
                
                for item in valid_holdings:
                    name = f"{item['prdt_name']} ({item['pdno']})"
                    qty = api.safe_int(item.get('hldg_qty'))
                    buy_price = api.safe_float(item.get('pchs_avg_pric'), default=0.0)
                    cur_price = api.safe_int(item.get('prpr'))
                    # 매입금액: 실전 잔고(INQR_DVSN=01)·토스 어댑터는 pchs_amt가 0/누락으로 오므로
                    # 아래 합계 줄과 동일하게 평단×수량으로 복원한다.
                    pchs_amt = api.safe_int(item.get('pchs_amt')) or int(qty * buy_price)
                    eval_amt = api.safe_int(item.get('evlu_amt'))
                    profit = api.safe_int(item.get('evlu_pfls_amt'))
                    rate = api.safe_float(item.get('evlu_pfls_rt'), default=0.0)
                    
                    row_str = (
                        f"{pad(name, name_col_width, '<')} "
                        f"{pad(f'{qty:,}주', 10, '>')} "
                        f"{pad(f'{buy_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{cur_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{pchs_amt:,}원', 15, '>')} "
                        f"{pad(f'{eval_amt:,}원', 15, '>')} "
                        f"{pad(f'{profit:+,}원', 14, '>')} "
                        f"{pad(f'{rate:.2f}%', 10, '>')}"
                    )
                    self.log(row_str)
                
                self.log("─" * line_length)
                if summary and len(summary) > 0:
                    s_data = summary[0]
                    
                    # [수정] API 지연에 따른 왜곡 방지를 위해 보유 종목 개별 합산 값 사용
                    tot_pchs = 0
                    total_profit = 0
                    total_eval = 0
                    if valid_holdings:
                        tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in valid_holdings)
                        total_profit = sum(api.safe_int(h.get('evlu_pfls_amt')) for h in valid_holdings)
                        total_eval = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
                    
                    # 총 자산 계산 (예수금 + 평가금)
                    current_total = 0
                    deposit_d2 = 0
                    if deposit_res:
                        deposit_d2 = deposit_res.get('d2_real', 0)
                        if deposit_d2 == 0:
                            deposit_d2 = deposit_res.get('d2_deposit', 0)
                    
                    # [수정] account 모듈을 활용하여 해외 자산까지 완벽하게 포함된 총 자산 획득
                    target_cano = config.session.auto_cano
                    acnt_cd = config.session.auto_acnt_prdt_cd
                    
                    asset_data = account.get_asset_status_data(target_cano, acnt_cd)
                    
                    # [Fix] API 지연/오류로 인해 account 모듈 내부에서 주식 잔고가 누락(0)된 경우 감지
                    #  [Fix 2026-09-06] 종전 판정은 `sec_eval == 0` **하나**였다. 그러면
                    #   국내는 되고 해외만 실패한 경우나, 예수금만 실패한 경우처럼
                    #   **일부만 빠진** 총자산이 그대로 통과한다. 이제 집계 쪽이 어느
                    #   구간을 못 채웠는지 말해 주므로 그것을 본다.
                    is_asset_broken = False
                    _degraded = list((asset_data or {}).get('degraded') or [])
                    if asset_data and total_eval > 0 and asset_data.get('sec_eval', 0) == 0:
                        is_asset_broken = True
                    elif _degraded:
                        is_asset_broken = True
                        self.log(f"⚠️ 자산 집계 일부 실패(결손: {', '.join(_degraded)}) — "
                                 f"이번 주기 총자산을 대안 계산으로 대체합니다.")

                    if asset_data and not is_asset_broken:
                        current_total = asset_data.get('tot_asset', 0)
                        order_possible = asset_data.get('order_possible', deposit_d2)
                    else:
                        # Fallback (API 실패 시 기존 로직으로 대안 계산)
                        cash = deposit_d2 + (deposit_res.get('foreign_deposit', 0) if deposit_res else 0)
                        current_total = cash + total_eval
                        order_possible = deposit_res.get('order_possible', deposit_d2) if deposit_res else deposit_d2
                        
                        if is_asset_broken:
                            self.log(f"⚠️ 통합 자산 조회 이상 감지 (API 지연 추정). 안전을 위해 이전 자산({current_total:,}원)으로 대체합니다.")

                    # [Fix] 토스: 미체결 매수 주문에 묶인 현금을 자산에 보정한다.
                    # (매수가능금액은 예약 현금을 제외하므로, 주문 접수/취소 시 자산이 출렁여
                    #  '가짜 입금' 자동 감지 및 손실률 왜곡을 유발한다.)
                    # 조회 실패 시 보정값을 신뢰할 수 없으므로, 이번 주기의 입금 자동 감지는 건너뛴다.
                    toss_cash_reliable = True
                    if config.session.is_toss and current_total > 0:
                        try:
                            reserved_buy_cash = self._get_toss_open_buy_reserved(target_cano, acnt_cd)
                            if reserved_buy_cash > 0:
                                current_total += reserved_buy_cash
                                self.log(f"[토스 자산 보정] 미체결 매수 예약 현금 {reserved_buy_cash:,}원을 자산에 합산했습니다. (보정 후 총자산: {current_total:,}원)")
                        except Exception as e:
                            toss_cash_reliable = False
                            logger.debug(f"[Toss] 미체결 매수 예약 현금 조회 실패(입금 감지 스킵): {e}")

                    # [추가] 일일 손실 제한 체크
                    if current_total > 0:
                        is_first_init = False
                        # [Fix] 초기 자산 로드 실패(0원) 시, 첫 유효 조회 값으로 보정
                        if self.initial_asset == 0:
                            is_first_init = True
                            target_cano = config.session.auto_cano
                            acnt = config.session.auto_acnt_prdt_cd
                            acc_str = f"{target_cano}-{acnt}"
                            saved_initial = load_daily_initial_asset(acc_str)
                            if saved_initial > 0:
                                self.initial_asset = saved_initial
                                self.log(f"[시스템 보정] 기존 초기 자산 기록 로드: {self.initial_asset:,}원")
                            elif is_plausible_baseline(acc_str, current_total):
                                self.initial_asset = current_total
                                if save_daily_initial_asset(acc_str, self.initial_asset):
                                    self.log(f"[시스템 보정] 초기 자산 정보 갱신 및 저장: {self.initial_asset:,}원")
                                else:
                                    self.log(f"[시스템 보정] 초기 자산 {self.initial_asset:,}원 갱신 — "
                                             f"[bold red]저장 실패[/]: 재기동 시 기준선을 잃습니다")
                            else:
                                # [Fix 2026-09-01] 여기에 검사가 없어 initialize()의 안전장치가
                                #  무력했다. 기동 경로가 '직전 기록의 반토막 이하'라며 거부한
                                #  값을 이 루프가 한 주기 뒤 그대로 기준선으로 박았기 때문이다.
                                #  기준선이 작게 박히면 손실률이 늘 큰 양수라 **차단기가 종일
                                #  발동하지 않는다**. 0으로 두고(=차단기 스킵·사이징은 예수금
                                #  폴백) 다음 주기에 다시 잰다 — 시세가 돌아오면 스스로 낫는다.
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                if getattr(self, '_baseline_reject_date', None) != today_str:
                                    self._baseline_reject_date = today_str
                                    warn = (f"⚠️ [시작 자산 이상] 조회값 {current_total:,}원이 직전 "
                                            f"기록 대비 지나치게 작습니다.\n시세 결손(주식 평가액 0 "
                                            f"수신)이 의심되어 오늘 기준 자산으로 삼지 않습니다.\n"
                                            f"계좌 차단기(일일 손실 한도)가 그때까지 동작하지 "
                                            f"않습니다 — 시세가 정상화되면 스스로 잡습니다.")
                                    self.log(warn)
                                    #  [Fix 2026-09-06] 이 경보는 **하루 한 번**만 나간다
                                    #   (_baseline_reject_date 스로틀). 내용은 "계좌 차단기가
                                    #   동작하지 않는다"이므로 놓치면 그 사실을 온종일 모른다.
                                    #   전달을 확인한 뒤에 스로틀을 찍는다 — 못 닿았으면
                                    #   표식을 남기지 않아 다음 주기에 다시 시도한다
                                    #   (market_halt 의 '상태는 전달을 확인한 뒤에 뒤집는다'와 같은 규칙).
                                    if not _pkg().alert_delivered(warn, urgent=True):
                                        self._baseline_reject_date = None

                            # [추가] DB에 기록
                            if self.initial_asset > 0:
                                #  [Fix 2026-09-06] 종전에는 `except Exception: pass` 였다.
                                #   이 행이 없으면 내일의 드로다운 고점과 오프라인 입출금
                                #   대조점이 오늘치만큼 비고, 그 사실은 어디에도 남지 않았다.
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                try:
                                    _ok = db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                                except Exception as _e:
                                    _ok = False
                                    logger.debug(f"[기준선] 자산 스냅샷 저장 예외: {_e}")
                                if not _ok:
                                    self.log("[기준선] 오늘 시작 자산을 자산 이력에 남기지 "
                                             "못했습니다 — 내일의 드로다운 기준에서 오늘이 빠집니다")

                        # [안전장치] 계좌 차단기(일일 손실 한도)를 **표시 코드보다 먼저** 돌린다.
                        #  종전에는 이 함수 맨 끝(265줄 아래)에 있었고 함수 전체가
                        #  `except Exception: pass`로 묶여 있었다. 그래서 그 사이의 손익 표시·
                        #  입출금 감지·문자열 포맷 중 **어느 하나라도 던지면 차단기가 조용히
                        #  건너뛰어졌다** — 손실이 큰 날일수록 그 코드가 다룰 값이 많아진다.
                        #  기준자산이 정해진 직후, 표시와 무관한 이 자리가 옳다.
                        self._run_account_circuit_breaker(current_total)

                        profit_rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        
                        realized_profit = 0
                        # [중요] 실현손익을 못 구했는데 0으로 두면 그만큼이 그대로 '입출금'으로
                        #  둔갑한다. 오늘 -50만 실현했는데 조회가 실패하면 원금이 50만 커 보이고,
                        #  같은 조회가 매 주기 똑같이 실패하므로 아래 '3회 연속' 규칙이 방어가
                        #  아니라 오탐 확정 장치가 된다(이 파일의 기존 주석이 우려한 그 경로다).
                        #  0인 것과 못 구한 것을 갈라, 못 구했으면 감지 자체를 하지 않는다.
                        realized_ok = True
                        try:
                            today_str = datetime.now().strftime("%Y-%m-%d")
                            
                            target_account = None
                            if config.session.auto_cano:
                                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                                
                            today_trades = db_manager.db.get_trades(
                                start_date=today_str, end_date=today_str, 
                                is_sim=False, account=target_account
                            )
                            
                            today_trades_parsed = []
                            for r in reversed(today_trades):
                                type_str = r['type']
                                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                                parsed_r = dict(r)
                                parsed_r['type'] = simple_type
                                today_trades_parsed.append(parsed_r)
                            
                            today_trades_refined = self._refine_trade_records(today_trades_parsed)
                            sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
                            #  [2026-09-14] 원금 식(현금+매입원가−실현손익)에 넣는 실현손익은 **순손익**이어야
                            #   한다 — 현금에서는 비용이 이미 빠져 있다. 실거래 profit_amt 는 총차익이므로
                            #   cost_amt 를 뺀다(가상투자 행은 profit 이 순손익·cost_amt 0 이라 같은 식이 맞다).
                            #   빼지 않으면 왕복마다 비용만큼 원금이 줄어 보여 문턱을 넘는 날 '출금'으로 오인된다.
                            realized_profit = int(sum(int(t.get('profit_amt') or 0) - float(t.get('cost_amt') or 0)
                                                     for t in sell_trades))

                            # [외부 매도] 운용자가 HTS/MTS로 자동매매 계좌에서 직접 팔면 우리
                            #  주문 기록이 없어 실현손익이 0으로 남는다(conclusion 은 origin_trade
                            #  가 있어야 손익을 채운다). 그 금액은 '0'이 아니라 '모른다'다 —
                            #  0으로 세면 100만원 이익 실현이 **100만원 입금**으로 둔갑해
                            #  기준 자산이 부풀고(차단기가 늦어진다) 자산 이력에도 남는다.
                            #  이 파일의 원칙 그대로, 모르면 감지하지 않는다.
                            if any("매도" in str(r['type']) and "(외부)" in str(r['type'])
                                   and not (r['profit_amt'] or 0) for r in today_trades):
                                realized_ok = False
                                if getattr(self, '_ext_sell_gate_date', None) != today_str:
                                    self._ext_sell_gate_date = today_str
                                    self.log("[입출금감지] 실현손익을 모르는 외부(HTS/MTS) 매도가 "
                                             "있어 오늘 입출금 자동 감지를 보류합니다.")
                        except Exception as _rp_e:
                            realized_ok = False
                            logger.warning(f"[입출금감지] 당일 실현손익 조회 실패 — 감지를 보류한다: {_rp_e}")
                            
                        # [수정] 오프라인(프로그램 종료) 상태에서의 입출금까지 완벽히 감지하도록 수학적 불변 원리 적용
                        # 매매 손익이 아닌 외부 현금 입출금을 스스로 포착하여 일일 손실 제한(Loss Cut) 오작동을 방지합니다.
                        #
                        # [Fix] 현금은 반드시 current_total과 '같은 스냅샷'의 평가금으로 빼야 한다.
                        #  total_eval은 보유 종목 리스트 합산이고 current_total은 get_asset_status_data의
                        #  별도 재조회라, 두 값의 시세 스냅샷이 어긋나면 현금이 틀어진다(음수까지 나온다).
                        #  실측 2026-07-27: 같은 로그 한 줄에서 평가금이 3,540,000(88,500 기준)과
                        #  3,536,000(88,400 기준)으로 갈려 현금이 1,371원 대신 -2,629원으로 계산됐다.
                        #  장 마감 후에도 어긋났다는 건 한쪽이 캐시라는 뜻이고, 캐시면 오차가 매 주기
                        #  '동일하게' 반복되어 아래 3회 연속 확인 규칙이 방어가 아니라 오탐 확정 장치가 된다.
                        #  (보유가 커져 오차가 5만원을 넘으면 가짜 입출금 → initial_asset 이동 →
                        #   일일 손실 제한 기준 왜곡. 과거 같은 계열 버그가 비상정지를 오작동시켰다.)
                        #  asset_data 경로에서는 tot_asset = real_cash + dep_ovs + sec_eval 이므로
                        #  sec_eval을 빼면 현금이 정확히 나온다. 폴백 경로의 current_total은
                        #  cash + total_eval로 만든 값이라 total_eval을 빼는 것이 맞다.
                        if asset_data and not is_asset_broken:
                            current_cash = current_total - api.safe_int(asset_data.get('sec_eval', total_eval))
                        else:
                            current_cash = current_total - total_eval
                        current_principal = current_cash + tot_pchs - realized_profit

                        # [Fix] 입금 감지 기준은 '원금(현금+매입원가-실현손익)'이어야 한다.
                        # 원금은 입출금이 없으면 가격 변동/매매와 무관하게 불변(=시작현금+시작매입원가)이다.
                        # 과거에는 initial_asset(=시작 총자산=현금+평가금)과 비교했는데, 보유 종목에 평가손익이
                        # 있으면 매입원가≠평가금이라 그 차이(=시작 시점 평가손익)가 가짜 입출금으로 오인되었다.
                        # → 보유 종목 하락만으로 '가짜 입금'이 잡혀 기준자산이 부풀고 비상정지가 오작동했다.
                        if is_first_init or self.baseline_principal <= 0:
                            # [오프라인 입출금] 기준을 새로 잡기 **전에** 꺼져 있던 사이의
                            #  입출금부터 되찾는다. 새로 잡고 나면 대조할 것이 없어진다.
                            self._reconcile_offline_transfer(
                                f"{target_cano}-{acnt_cd}", current_principal, realized_ok)
                            self.baseline_principal = current_principal
                            # 재기동이 같은 날 다시 일어나도 이 기준으로 대조할 수 있게 남긴다.
                            try:
                                if not save_daily_initial_asset(f"{target_cano}-{acnt_cd}",
                                                                self.initial_asset,
                                                                principal=int(current_principal)):
                                    self.log("[기준선] 기준 원금 저장 실패 — 재기동 시 "
                                             "오프라인 입출금 감지가 꺼집니다")
                            except Exception as _e:
                                #  저장이 예외로 깨진 경우까지 조용히 넘기면, 위에서
                                #  bool 을 검사하는 뜻이 사라진다(같은 결과·같은 침묵).
                                self.log(f"[기준선] 기준 원금 저장 실패({_e}) — 재기동 시 "
                                         f"오프라인 입출금 감지가 꺼집니다")
                            # [날짜를 넘는 대조점] 파일(daily_state)은 날짜가 바뀌면 비워지므로
                            #  오프라인 입출금을 되찾을 수 없다. 자산 이력 행에 함께 남긴다.
                            #  자산이 0(시세 결손 의심)인 날은 남기지 않는다 — 그런 행은
                            #  get_max_daily_asset의 환산에서 빠져 보정이 사라진다. 그날을
                            #  건너뛰어도 다음 대조는 그 이전 스냅샷과 이뤄져 식은 그대로 성립한다.
                            if realized_ok and self.initial_asset > 0:
                                try:
                                    _ok = db_manager.db.save_daily_asset(
                                        datetime.now().strftime("%Y-%m-%d"),
                                        f"{target_cano}-{acnt_cd}", self.initial_asset,
                                        principal=int(current_principal))
                                except Exception as _e:
                                    _ok = False
                                    logger.debug(f"[입출금] 기준 원금 스냅샷 저장 예외: {_e}")
                                if not _ok:
                                    #  파일(daily_state)은 날짜가 바뀌면 비워지므로, 이 행이
                                    #  없으면 날짜를 넘는 오프라인 입출금 대조점이 사라진다.
                                    self.log("[입출금] 기준 원금 스냅샷을 남기지 못했습니다 — "
                                             "다음 기동에서 오프라인 입출금을 되찾지 못할 수 있습니다")

                        # [파생값] 오늘 누적 순입출금. 저장하지 않고 매 주기 다시 잰다.
                        #  기준 자산을 **옮기지 않아도** 차단기·사이징이 이 값으로 즉시 보정된다
                        #  (effective_baseline 참조). 옮기면 initial_asset과 baseline_principal이
                        #  같은 폭으로 움직여 이 값이 0이 되므로, 반영 전후의 유효 기준은 동일하다.
                        if toss_cash_reliable and realized_ok:
                            _net = int(current_principal - self.baseline_principal)
                            # [잡음 바닥] 원금 대조에는 잔돈이 남는다 — 매수 수수료는 현금만
                            #  깎고 매입원가에는 안 들어가서, 거래한 날마다 수십~수백원이
                            #  '입출금'으로 새어 나온다. 실측 2026-08-31: 가상계좌에 입출금이
                            #  없는데 net_transfer 77원이 기록됐다. 한 번은 무해하지만 매일
                            #  쌓이면 get_max_daily_asset 의 환산(고점)을 갉는다.
                            #  오프라인 경로는 같은 이유로 이미 이 바닥을 갖고 있었다 —
                            #  판정이 같으니 문턱도 같아야 한다([[seed-spend-guard-parity]]).
                            #  [Fix 2026-09-13] 종전에는 100원 **바닥만** 썼다. 그러면 매수가
                            #   2건 이상인 날은 수수료 합이 바닥을 넘어 그대로 '출금'이 된다
                            #   (실측 파이 가상계좌: 09-07 −119, 09-10 −437 = 그날 매수 수수료,
                            #   09-04 −11,444 = 수동매도 손익이 총액으로 적힌 차이). 누적
                            #   −11,923원이 고점 환산을 갉아 드로다운을 **과소** 계산했다 —
                            #   한도가 열리는 방향이다. 오프라인 경로와 같은 함수를 쓴다:
                            #   max(100, min(5만, 원금의 0.5%)) — 1,000만 계좌는 5만원,
                            #   10,027원 계좌는 100원. 소액 계좌의 진짜 출금은 여전히 잡힌다.
                            if abs(_net) < self._offline_transfer_threshold(self.baseline_principal):
                                _net = 0
                            #  [당일 보정] 이 값은 오늘의 차단기·사이징을 즉시 보정한다
                            #   (effective_baseline). DB 기록과 무관하게 항상 최신으로 둔다.
                            self.net_transfer_today = _net
                            # [여러 날 보정] 오늘 행에 남겨야 내일부터의 드로다운 기준이 맞는다.
                            #  이력을 옮기지 않고 이 값으로 환산한다(get_max_daily_asset).
                            #  값이 바뀔 때만 쓴다 — 매 주기 쓰면 파이3에 부담이고 의미도 없다.
                            #  [Fix 2026-09-06] '쓸지 말지'의 기준을 메모리 값이 아니라
                            #   **마지막으로 저장에 성공한 값**으로 바꾼다. 종전에는 쓰기 전에
                            #   메모리를 찍어서, 한 번 실패하면 다음 주기의 조건이 거짓이 되어
                            #   그날 내내 다시 시도하지 않았다(save_daily_asset 은 예외도 삼켰다).
                            #   그러면 오늘의 출금이 환산에서 빠져 90일(DD_LOOKBACK_DAYS) 동안
                            #   가짜 드로다운이 리스크 한도를 묶는다
                            #   ([[daily-asset-baseline-transfers]] · 실측 30.0% vs 0.0%).
                            if _net != getattr(self, '_net_transfer_saved', 0):
                                try:
                                    _saved = db_manager.db.save_daily_asset(
                                        datetime.now().strftime("%Y-%m-%d"),
                                        f"{target_cano}-{acnt_cd}", self.initial_asset,
                                        net_transfer=_net)
                                except Exception as _e:
                                    _saved = False
                                    logger.debug(f"[입출금] 일자별 순입출금 기록 실패: {_e}")
                                if _saved:
                                    self._net_transfer_saved = _net
                                    self._hwm_cache_date = None   # 환산이 바뀌었으니 다시 잰다
                                else:
                                    self.log(f"[입출금] 오늘 순입출금({_net:+,}원)을 자산 이력에 "
                                             f"남기지 못했습니다 — 다음 주기에 다시 시도합니다"
                                             f"(그때까지 드로다운이 실제보다 크게 잡힙니다).")
                        else:
                            self.net_transfer_today = 0   # 못 쟀으면 보정하지 않는다(옛 동작 유지)

                        if not is_first_init and toss_cash_reliable and realized_ok:
                            transfer_amt = current_principal - self.baseline_principal

                            # [Fix] 5만원 이상 원금 변동 발생 시 입출금으로 간주하되, 주문 체결 중 API 데이터 불일치(Lag)로 인한
                            # 오작동을 방지하기 위해 3회 연속(약 30초) 동일한 변동이 감지될 때만 실제 입출금으로 확정합니다.
                            if abs(transfer_amt) >= 50000 and self.baseline_principal > 0:
                                if not hasattr(self, '_pending_transfer_amt'):
                                    self._pending_transfer_amt = 0
                                    self._pending_transfer_count = 0
                                    
                                # 오차 범위 500원 이내면 동일한 변동으로 간주
                                if abs(self._pending_transfer_amt - transfer_amt) < 500:
                                    self._pending_transfer_count += 1
                                else:
                                    self._pending_transfer_amt = transfer_amt
                                    self._pending_transfer_count = 1
                                    
                                if self._pending_transfer_count >= 3:
                                    action_str = "입금" if transfer_amt > 0 else "출금"
                                    # [알림 전용] 감지된 입출금은 **기준선을 옮기지 않는다.**
                                    #  일일 손실 한도·사이징은 net_transfer_today 로, 드로다운은
                                    #  daily_asset_history.net_transfer 로 각각 파생 보정되므로
                                    #  옮길 상태가 없다. 여기서는 운용자에게 알리기만 한다.
                                    #  (종전에는 initial_asset 을 옮기고 자산 이력을 평행이동했다.
                                    #   되돌릴 수 없고, 추정이 틀리면 고점이 낮아져 드로다운을
                                    #   과소평가 = 리스크 한도가 조용히 열리는 방향이었다.
                                    #   그래서 30% 상한을 두고 초과분은 사람에게 넘겼는데,
                                    #   그 미반영분이 90일짜리 가짜 드로다운으로 남았다.)
                                    _sig = (datetime.now().strftime("%Y-%m-%d"),
                                            int(round(transfer_amt / 10000.0)))
                                    if getattr(self, '_transfer_alert_sig', None) != _sig:
                                        self._transfer_alert_sig = _sig
                                        self.log(f"💰 외부 예수금 {action_str} 자동 감지: {transfer_amt:+,}원 "
                                                 f"(리스크 기준은 자동 보정됩니다)")
                                        api.send_telegram_message(
                                            f"💰 [예수금 {action_str} 자동 감지]\n"
                                            f"약 {abs(int(transfer_amt)):,}원의 {action_str}을 확인했습니다.\n\n"
                                            f"✅ 일일 손실 한도·포지션 사이징·드로다운 기준은 이 금액을 "
                                            f"빼고 계산하므로 자동으로 맞춰집니다. 조치할 것은 없습니다.\n"
                                            f"(실제 {action_str}이 아니라면 잔고 조회 이상을 확인해 주세요)")
                            else:
                                if hasattr(self, '_pending_transfer_count'):
                                    self._pending_transfer_count = 0
                                    self._pending_transfer_amt = 0
                        realized_rate = (realized_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
                        daily_profit = current_total - self.initial_asset
                        daily_profit_rate = (daily_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
                        order_possible = deposit_res.get('order_possible', deposit_d2) if deposit_res else 0
                        
                        self.log(f"[증권 자산 현황] 증권 매입 금액: {tot_pchs:,}원 | 증권 평가 금액: {total_eval:,}원 | 증권 평가 손익: {total_profit:+,}원 ({profit_rate:+.2f}%) | 주문 가능 금액: {order_possible:,}원")
                        self.log(f"[오늘 자산 현황] 오늘 시작 자산: {self.initial_asset:,}원 | 오늘 현재 자산: {current_total:,}원 | 오늘 현재 손익: {daily_profit:+,}원 ({daily_profit_rate:+.2f}%) | 오늘 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)")

                        # (계좌 차단기·평가자산 갱신은 위 _run_account_circuit_breaker에서
                        #  이미 수행했다 — 표시 코드가 던져도 건너뛰지 않도록 앞으로 옮겼다)
                    else:
                        self.log(f"   총 평가금액: {total_eval:,}원  |  총 평가손익: {total_profit:+,}원")
                    
        except Exception: pass

    def _get_toss_open_buy_reserved(self, cano=None, acnt=None):
        """[토스 전용] 미체결 매수 주문에 묶인 현금(KRW)을 합산한다.

        토스의 '매수가능금액(cashBuyingPower)'은 미체결 매수 주문에 예약된 현금을
        제외한 값이라, 주문 접수/취소에 따라 변동한다. 이를 보정하지 않으면 자산/원금
        계산이 흔들려 입금 자동 감지가 오작동(가짜 입금)하고 손실률이 왜곡된다.
        반환값을 current_total에 더하면 '매수가능금액 + 예약현금 = 실제 현금'이 되어
        주문 상태와 무관하게 안정적인 값이 된다.
        """
        reserved = 0
        #  [Fix 2026-09-06] `or []` 가 **조회 실패를 '미체결 없음'으로** 만들었다.
        #   그러면 보정액이 0이 되어 자산이 예약 현금만큼 낮게 잡히는데, 호출부는 그것을
        #   정상 값으로 믿는다(toss_cash_reliable 이 True 로 남는다) — 위 독스트링이 막으려던
        #   바로 그 실패다: 가짜 입출금 감지, 손실률 왜곡, 그리고 그 왜곡이
        #   daily_asset_history 에 net_transfer 로 굳는다([[daily-asset-baseline-transfers]]).
        #   호출부에는 이미 예외를 받아 '이번 주기 입금 감지 스킵'으로 가는 팔이 있다.
        open_orders = api.get_domestic_open_orders(cano, acnt)
        if open_orders is None:
            raise RuntimeError("미체결 주문을 조회하지 못했습니다 — '미체결 없음'이 아닙니다")
        for o in open_orders:
            # KIS 형식: 02=매수, 01=매도
            if o.get('sll_buy_dvsn_cd') != '02':
                continue
            rmn = api.safe_int(o.get('rmn_qty')) or api.safe_int(o.get('ord_qty'))
            price = float(o.get('ord_unpr') or 0)
            if rmn > 0 and price > 0:
                reserved += int(rmn * price)
        return reserved

    def _get_total_estimated_asset(self):
        """현재 총 추정 자산(예수금 + 주식평가금) 계산"""
        try:
            cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            
            # [수정] 해외 자산 누락 방지를 위해 완벽하게 계산된 통합 자산 데이터 사용
            asset_data = account.get_asset_status_data(cano, acnt)
            if asset_data:
                return asset_data.get('tot_asset', 0)
        except Exception as e:
            logger.debug(f"자산 조회 중 예외 발생: {str(e)}")
        
        self.log(f"⚠️ 자산 조회 최종 실패. (KIS 서버 응답 지연)")
        return None
