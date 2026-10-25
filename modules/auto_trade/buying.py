"""AutoTrader 의 매수 — 주기마다의 신규 매수(후보 분석·게이트·주문 집행)와 보유 종목의 증액(피라미딩).

매수 판정(_check_buy_conditions)과, 그 판정이 부르는 것들이다:
  · 계좌 상태 게이트(방어 모드·개장 직후 진입 보류·미체결 현황 미확인·보유 슬롯 = 잔고 + 미체결·예수금)와
    당일 재진입 방어(손절가 위 재매수 차단·체결강도 허들)
  · 후보 분석(_analyze_candidates → 종목별 _analyze_candidate_worker): 트레이딩 제한·시장 필터·상관관계·추세품질
    상한·수급 게이트, 신호 원장·채점 경계 원장 기록, 우선순위 정렬(candidate_priority_key)
  · 주문 집행(_execute_buy_orders): 실시간가·오염 일봉 금지, 손절 기준·ATR 손절률 상한, 배분액·1주 값 상한,
    포트폴리오 히트 캡 선점·반납, 옛 고점·반익절 기록 정리, 결과 불명, 예약 매수 자동 취소
  · 피라미딩(_try_pyramid_buy — 매도 판정 루프가 '보유' 판정한 종목마다 부른다)

[왜 나눴나 · 2026-10-25 C1 5단계] 매수는 노출을 늘리는 쪽의 게이트가 모두 모인 자리이고, 판정·후보 분석·집행이
 1,400줄을 넘는다. 주기 제어와 한 파일에 섞여 있으면 진입 게이트 하나를 고칠 때 매매 파일 전체를 함께 읽어야
 하고, 매수 변경이 다른 변경과 구분되지 않는다.

[어떻게 나눴나] 1~4단계(reporting·market_risk·account_sync·selling)와 같은 규약이다. 메서드 본문은 옮기면서
 바꾸지 않았다. 바꾼 곳은 한 군데다. _collect_stop_exit_prices(정적 메서드)가 클래스 이름으로 읽던
 `AutoTrader._STOP_EXIT_PREFIXES` 를 `TraderBuyingMixin._STOP_EXIT_PREFIXES` 로 바꿨다. 이 모듈이 AutoTrader 를
 import 하면 순환이 된다. AutoTrader 가 이 믹스인을 상속하므로 self._check_buy_conditions() ·
 patch.object(AutoTrader, ...) 가 전과 같이 동작한다. 모듈 전역 이름(config·api·db_manager·datetime·time …)은
 trader.py 와 **같은 객체**를 가리키게 import 했다(tests/test_trader_split_parity.py 가 이름마다 대조한다). 로거도
 trader 와 같은 이름을 쓴다. 매수만 쓰던 모듈 함수 둘(candidate_priority_key·contaminated_chart_reason)도 함께
 옮겼고, trader 가 다시 import 해 trader.candidate_priority_key 로 부르는 곳이 그대로 동작한다. 매도와 함께 쓰는
 지정가 제한폭 보정(_clamp_order_price)·시장 구분(_get_stock_market_type)은 trader 에 남겼다.
 동등성 기준은 주기 재생 기록 다섯 시나리오(tests/test_cycle_replay.py — base·market_stress·account_sync·
 sell_paths·buy_paths)다.

[주의] 테스트에서 이 메서드들이 쓰는 전역을 바꿔 끼울 때는 trader 가 아니라 이 모듈을 지정한다
 (예: modules.auto_trade.buying.entry_open_delay_remaining). 패키지 네임스페이스 경유 호출은 _pkg() 그대로다.
"""
import concurrent.futures
import logging
import math
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

import pandas as pd

import api
import config
from core import context
from core import indicators
from core import utils
from modules import analysis
from modules import db_manager
from modules.auto_trade.common import (_enrich_rules_with_weights, _get_trade_account, entry_open_delay_remaining,
                                       get_restricted_stocks)

#  트레이더 로그와 같은 이름 — 매수 코드가 남기는 로그도 'modules.auto_trade.trader' 로 모인다.
logger = logging.getLogger("modules.auto_trade.trader")


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자 — trader._pkg 와 같은 규약.

    테스트의 patch('modules.auto_trade.X') 가 이 모듈 안의 호출에도 걸리게 한다. (지연 import 라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


def candidate_priority_key(c):
    """[추세추종] 매수 후보 우선순위 정렬 키 — 게이트(매수 점수 통과)와 랭킹을 분리한다.

    점수 1순위, 추세 품질(회귀 모멘텀 = 연환산 기울기 × R²)은 그 **동점을 가르는** 2순위다.
    이력 부족(None)은 검증 불가로 보아 동점 안에서 최하순위. (3순위 이하: 52주 위치 → 체결강도)

    [실증 2026-08-12] 종전에는 추세 품질이 1순위였다. "점수는 이진 신호 합산이라 동점이
     흔하고 추세의 강도·지속성을 구분하지 못한다"는 것이 근거였는데, 동점이 흔하다는 관찰은
     맞았지만 **1순위를 통째로 넘기는 것은 실측상 열위**였다.
     (tools/audit_scoring_weights.py --only C · tools/audit_entry_gate_parity.py,
      10년 15회 × 25종목 짝비교)

       · 추세품질 1순위(종전) : 하위 4구간 19/60 · 게이트 반영 후 20/60 · 고변동 3-0-12
                                10년 수익 384.9 → 286.8, 고변동 상위10% 141.8 → 62.8
       · 점수 1순위 + 추세품질 동점가름(현행) : 하위 4구간 33/60 (채택 기준 31/60 통과)
                                MAR승 34/60 · 꼬리승 34/60 · MDD 동일(-29.9)

     추세 품질을 1순위로 두면 슬롯 경쟁에서 점수가 **한 번도** 주인을 가르지 못한다 —
     추세 품질은 연속값이라 동점이 0%라서, 2순위인 점수는 죽은 조항이었다. 그 상태로
     10년 수익의 4분의 1과 고변동 구간의 fat-tail 절반을 잃고 있었다.
     반대로 점수를 1순위로 두면 경쟁일의 25~32%가 동점이 되는데, 종전 백테스트는 그
     동점을 관심종목 등록 순서로 갈랐다. 그 임의 상수 자리에 추세 품질을 넣은 것이
     지금 이 순서다 — 두 측정(상관관계 게이트 OFF/ON)에서 모두 기준을 넘겼다.

    [남은 한계] 체결강도·호가비 게이트는 실시간 체결 데이터라 백테스트로 재현할 수 없어
     실매매의 후보 풀은 측정보다 좁다. 또 후보가 남은 슬롯보다 많았던 날은 전체 거래일의
     11.0%뿐이다 — 이 순위 축은 드물게 개입하고 크게 남기므로, 표본이 얇다는 점을 감안해
     다른 축보다 신뢰구간을 넓게 잡을 것.
    """
    tq = c.get('trend_quality')
    return (-c['score'],
            -(tq if tq is not None else float('-inf')),
            -(c.get('w52_pos') or 0.0), -(c.get('vol_strength') or 0.0))


def contaminated_chart_reason(code):
    """이 종목의 지표가 **NXT 가 섞인 토스 캔들**로 계산됐으면 그 사유, 아니면 None.

    [진입만 막는다 · 2026-10-05] 토스 모드에서 KRX 공식 일봉(Open API·네이버)을 못 받으면
     api.toss 가 토스 캔들로 대신하고 그 종목을 get_krx_fallback() 에 올린다. 그 일봉은 NXT
     장전·장후 체결이 섞여 ATR 이 6~15% 부풀고 ADX 가 최대 9.45 어긋난다 — ATR 은 손절폭 →
     수량 → 포트폴리오 리스크로 전파된다. 그래서 **신규 매수·피라미딩은 하지 않는다.**
     청산 경로는 막지 않는다: 보유 중인 종목의 손절·트레일링이 장애 때문에 멈추는 것이
     오염된 ATR 로 계산하는 것보다 위험하다.
    """
    if not getattr(config.session, 'is_toss', False):
        return None
    try:
        return api.get_krx_fallback().get(str(code))
    except Exception:       # noqa: BLE001 - 조회 실패가 매수를 여는 쪽으로 가지 않게 한다
        return "폴백 상태 확인 실패"


class TraderBuyingMixin:
    """AutoTrader 의 매수 메서드. 단독으로 쓰지 않는다 — 트레이더 상태(self.order_manager·self.risk_manager …)를 쓴다."""

    #  이 믹스인이 기대는 트레이더 상태·메서드(정의·초기값은 AutoTrader). 값 없는 선언이라 실행 시에는
    #  AutoTrader 의 것이 그대로 쓰인다. getattr 기본값으로 읽는 상태와 이 믹스인이 직접 쓰는 상태는 빠져 있다.
    _lock: Any
    is_running: bool
    order_manager: Any
    risk_manager: Any
    strategy: Any
    market_index_status: dict
    consecutive_errors: int
    buy_halt_reason: str
    initial_asset: Any
    half_tp_cache: set
    trade_records: list
    trailing_stop_cache: dict
    log: Callable[..., None]
    set_stock_state: Callable[..., None]
    _clamp_order_price: Callable[..., Any]
    _get_stock_market_type: Callable[..., Any]

    def _try_pyramid_buy(self, code, name, held_qty, current_price, profit_rate, result, last_buy, is_market_open, rule=None):
        """[추세추종] 수익 포지션 증액(피라미딩) 시도

        보유분석에서 '보유' 판정된 종목에 대해, 수익으로 추세가 검증되었고(수익률 트리거 이상)
        매수 신호가 유지 중이면 보유 수량의 일정 비율만큼 1회 한정(기본) 증액한다.
        물타기(손실 추가매수)와 정반대로, 손실 종목에는 절대 발동하지 않는다.
        """
        try:
            # 국내 종목만 지원 (시스템 트레이딩 매수 경로와 동일 범위)
            if not (len(code) == 6 and code[0].isdigit() and code.isalnum()):
                return

            # [안전장치] 방어 모드에서는 증액(노출 확대)도 신규 매수와 동일하게 보류한다.
            if getattr(self, 'buy_halted', False):
                return

            # [오염 일봉 증액 금지] 신규 매수와 같은 가드([[seed-spend-guard-parity]]).
            _contam = contaminated_chart_reason(code)
            if _contam:
                self.log(f"피라미딩 보류: {name} - KRX 공식 일봉 미확보({_contam}), "
                         f"토스 캔들(NXT 포함) 지표로는 증액하지 않습니다.")
                return

            # [안전장치] 미체결 주문 현황을 모르면 증액도 보류한다 — 신규 매수와 같은 이유다
            #  (_check_buy_conditions의 pending_restore_ok 게이트). 재기동 직후 복구 조회가
            #  실패하면 is_pending 맵이 비어 있어 '미체결 없음'과 '모름'이 구분되지 않는다.
            #  그 상태로 증액하면 거래소에 이미 걸린 주문을 못 본 채 두 번째를 낸다
            #  (주문 유실은 재전송하지 않는다는 규약과 같은 자리). manage_unfilled_orders가
            #  성공하면 자동 해제된다.
            if not getattr(self, 'pending_restore_ok', True):
                self.log(f"피라미딩 보류: {name} - 미체결 주문 현황 미확인 (중복 주문 방지)")
                return

            # [증액 횟수] 권위 소스는 trailing_stops.pyramid_count 다(재시작에도 유지되고,
            #  신규 진입 시 delete_trailing_stop 으로 함께 지워져 자동으로 0이 된다).
            #
            #  종전에는 최근 매수 **사유 문자열**을 정규식으로 파싱했다. 그 기록이 유실되면
            #  (DB 쓰기 실패·수동 정리) 횟수가 0으로 읽혀 상한을 넘겨 계속 증액된다.
            #  증액은 보유수량의 50%씩이라 1 → 1.5 → 2.25 → 3.375 로 커지는데, 횟수가
            #  계속 0이면 여기서 멈추지 않고 한 종목이 계좌를 삼킨다.
            #
            #  [모르면 보류] 조회 실패(-1)는 '0회'가 아니다. 리스크를 키우는 동작이므로
            #  불확실하면 하지 않는다 — 증액을 거른 대가는 놓친 수익뿐이다.
            db_count = db_manager.db.get_pyramid_count(code)
            if db_count < 0:
                self.log(f"피라미딩 보류: {name} - 증액 횟수를 확인할 수 없습니다(DB 조회 실패)")
                return

            #  구 버전 포지션 호환: 사유 마커가 더 크면 그쪽을 믿는다(보수적).
            legacy_count = 0
            if last_buy:
                m = re.search(r'피라미딩\s*(\d+)차', str(last_buy.get('reason', '')))
                if m:
                    legacy_count = int(m.group(1))
            pyramid_count = max(db_count, legacy_count)

            ok, reason = self.strategy.analyze_pyramid(profit_rate, result['state'], result['score'], pyramid_count)
            if not ok:
                return

            # [리스크 관리] 시장 필터 게이트: 신규 매수가 차단되는 약세 시장(지수<SMA)에서는
            # 검증된 포지션이라도 증액(노출 확대)을 보류한다. (기존 보유·청산에는 영향 없음)
            # [Fix] 신규 매수 경로와 동일하게 fail-closed — 지수 판단 불가(데이터 장애·캐시 없음)도 보류.
            if (getattr(config, 'USE_MARKET_FILTER', True)
                    and config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_REQUIRE_HEALTHY_MARKET", True)):
                m_type = self._get_stock_market_type(code)
                m_stat = self.market_index_status.get(m_type)
                if not isinstance(m_stat, dict) or not m_stat.get('is_healthy', False):
                    cause = "판단 불가(데이터 없음)" if not isinstance(m_stat, dict) or m_stat.get('unknown') else "약세"
                    self.log(f"피라미딩 보류: {name} - {m_type} 지수 {cause}(시장 필터)로 증액 보류")
                    return

            if not is_market_open:
                self.log(f"[장마감] 피라미딩 신호 감지 (주문 미전송): {name} - {reason}")
                return
            if self.order_manager.is_pending(code):
                return

            ratio = config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_RATIO", 0.5)
            add_qty = int(held_qty * ratio)
            if add_qty < 1:
                return

            raw_order_price = current_price * (1 + config.SLIPPAGE_RATE)
            order_price = int(utils.adjust_to_tick(
                raw_order_price, is_overseas=False,
                is_etf=api.is_domestic_etf_etn(code, name)))
            if order_price <= 0:
                order_price = int(current_price)
            # [안전장치] 상한가에 락되면 '현재가 + 슬리피지'가 제한폭을 넘어 거부된다.
            order_price = int(self._clamp_order_price(code, order_price))

            max_qty = api.fetch_buyable_quantity(code, order_price)
            if max_qty < add_qty:
                if max_qty < 1:
                    self.log(f"피라미딩 보류: {name} - 예수금 부족 (필요:{add_qty}주)")
                    return
                add_qty = max_qty

            # 증액분 손절률: 신규 매수와 동일하게 현재 ATR 기준으로 계산 (가중평균 손절선에 자동 반영)
            # [Fix] 신규 매수 경로(_execute_buy_orders)와 동일하게 개별 룰의 손절 설정을 우선 적용
            sl_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            if rule:
                sl_rate = rule.get('stop_loss', sl_rate)
                if rule.get('use_atr_stop') is not None:
                    use_atr_stop = bool(rule['use_atr_stop'])
                if rule.get('atr_stop_multiplier') is not None:
                    atr_mult = rule['atr_stop_multiplier']
            ind = result.get('ind') or {}
            atr_val = ind.get('atr', 0) or 0
            if use_atr_stop and atr_val > 0 and current_price > 0:
                # [SSOT] 신규 매수와 같은 함수를 쓴다 (engine.atr_stop_rate).
                sl_rate = _pkg().atr_stop_rate(atr_val, current_price, atr_mult=atr_mult) or sl_rate

            # [추세추종 안전장치] "탈출 전략이 없다면 포지션을 잡지 마라" — 신규 매수
            #  (_execute_buy_orders)와 같은 게이트다. 종전에는 증액 경로에만 이 검사가 없어,
            #  ATR 손절 OFF + 고정 손절 0(둘 다 사용자 설정으로 가능)이면 청산 기준 없는
            #  포지션을 더 키웠다. 게다가 아래 히트 캡은 sl_rate<0 일 때만 도는 구조라
            #  리스크 회계에서도 통째로 빠졌다(add_risk=0 → 예산 확인 자체를 건너뜀).
            #  개별 룰의 stop_loss가 NULL이면 값이 None으로 들어오는 것도 여기서 걸린다
            #  (종전에는 아래 비교에서 TypeError가 나 '피라미딩 오류'로만 남았다).
            try:
                sl_rate = float(sl_rate)
            except (TypeError, ValueError):
                sl_rate = 0.0
            if sl_rate >= 0:
                self.log(f"피라미딩 보류: {name} - 손절 기준 없음 "
                         f"(ATR 손절 {'ON(ATR 미확보)' if use_atr_stop else 'OFF'} + 고정 손절 0). "
                         f"청산 기준과 손실액 상한이 모두 없는 증액은 하지 않습니다.")
                return

            # [추가] 포트폴리오 히트 캡: 증액분 리스크가 남은 예산을 넘으면 피라미딩 보류.
            #  (_sell_worker 스레드 동시 실행 대비, 예산 확인과 선점을 락으로 원자화)
            #  (sl_rate < 0 은 위 게이트가 보장한다 — 조건부로 두면 '손절 없음'이 다시
            #   히트 캡 우회 경로가 된다)
            add_risk = (add_qty * order_price) * (abs(sl_rate) / 100.0)
            reserved_heat = False
            if add_risk > 0:
                with self._lock:
                    # 이 경로엔 예수금 변수가 없다. 기준자산이 없을 때의 폴백으로
                    #  '지금 살 수 있는 금액'(매수가능수량×주문가)을 넘긴다.
                    budget_left = self.risk_manager.portfolio_risk_budget_left(
                        avail_cash=max(max_qty, 0) * order_price)
                    if budget_left is not None:
                        if add_risk > budget_left:
                            cap = self.risk_manager.effective_portfolio_cap()
                            self.log(f"피라미딩 보류: {name} - 포트폴리오 총 리스크 한도({cap:.1f}%) 초과 "
                                     f"(증액 리스크 {add_risk:,.0f}원 > 남은 예산 {max(budget_left, 0):,.0f}원)")
                            return
                        self.portfolio_heat_amt += add_risk
                        reserved_heat = True

            # [순서] 횟수를 **먼저** 올리고 주문한다. 반대면 '주문은 나갔는데 횟수는 그대로'가
            #  되어 다음 주기에 같은 증액이 또 나간다. 기록만 되고 주문이 실패하면 증액
            #  기회를 하나 잃을 뿐이므로, 이쪽이 안전한 방향이다.
            if not db_manager.db.bump_pyramid_count(code, pyramid_count):
                if reserved_heat:
                    with self._lock:
                        self.portfolio_heat_amt -= add_risk
                self.log(f"피라미딩 보류: {name} - 증액 횟수를 기록하지 못했습니다(DB 쓰기 실패). "
                         f"기록 없이 증액하면 상한을 넘겨 반복됩니다.")
                return

            self.log(f"피라미딩 실행: {name} +{add_qty}주 - {reason}")
            try:
                odno = self.order_manager.send_order(code, add_qty, "buy", name=name, reason=reason, score=result['score'], price=order_price, stop_loss_rate=sl_rate)
            except _pkg().OrderOutcomeUnclear as e:
                #  신규 매수와 같은 이유로 선점분을 반납하지 않는다(위 _execute_buy_orders 주석).
                #   증액 횟수는 이미 올려 뒀다 — 그대로 둔다. 살아 있는 주문을 세지 않으면
                #   다음 주기가 상한을 넘겨 한 번 더 증액한다.
                self.log(f"피라미딩 중단: {name} - 주문 접수 여부를 확인하지 못했습니다 ({e}). "
                         f"선점한 리스크 예산과 증액 횟수를 그대로 둡니다.")
                return
            if not odno and reserved_heat:
                with self._lock:
                    self.portfolio_heat_amt -= add_risk  # 주문 실패 시 선점분 반납
            if odno:
                record = {
                    "type": "buy", "code": code, "name": name, "qty": add_qty,
                    "price": order_price, "reason": reason,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "odno": odno,
                    "stop_loss_rate": sl_rate
                }
                self.trade_records.append(record)
        except Exception as e:
            self.log(f"[피라미딩 오류] {name}: {e}")

    def occupied_slot_codes(self, holding_codes):
        """지금 슬롯을 차지하고 있는 종목 코드들 — **잔고 + 미체결 주문**.

        [왜 미체결까지 세나 · 2026-09-08] 슬롯 상한(SYSTEM_MAX_HOLDINGS)은 잔고만 보고
         판정했다. 그런데 직전 주기에 낸 매수 주문이 아직 체결되지 않았으면 그 종목은
         잔고에 없다 — 같은 슬롯이 다음 주기에 **다시 비어 보인다.**

           주기 N   : 보유 2 / 상한 4 → 두 자리가 비어 A·B 매수 (미체결)
           주기 N+1 : 보유 여전히 2 → **또 두 자리가 비어** C·D 매수
           네 주문이 모두 체결되면 6종목. 상한은 4다.

         A·B 자신은 is_pending 으로 후보에서 빠지지만 그것은 **같은 종목**의 중복만 막는다.
         슬롯은 '몇 자리를 썼나'의 문제라 종목이 달라도 넘친다.
         미체결 자동 취소는 UNFILLED_ORDER_CANCEL_SECONDS(기본 120초) 뒤이고 감시 주기는
         그보다 짧으므로, 이 창은 상시 열려 있다(취소가 연속 실패하는 경로도 따로 있다).

        pending_orders 는 매수/매도를 구분하지 않으므로 **합집합**으로 센다. 보유 중인
         종목의 미체결 매도는 이미 잔고에 있어 중복되지 않고, 잔고에서 사라진 뒤에도
         남아 있는 미체결이라면 그 자리는 아직 정리 중이라 비었다고 볼 수 없다.
        """
        occupied = set(holding_codes or ())
        try:
            om = self.order_manager
            with om._lock:
                occupied |= {c for c, orders in (om.pending_orders or {}).items() if orders}
        except Exception as e:      # noqa: BLE001 - 못 세면 잔고만으로 두되 흔적을 남긴다
            self.log(f"[슬롯] 미체결 주문을 세지 못했습니다 — 잔고만으로 판정합니다: {e}")
        return occupied

    def _check_buy_conditions(self, holdings, deposit_res, is_market_open=True, rules_map=None, restricted_stocks=None):
        # [안전장치] 방어 모드(일일 손실 한도 초과 등)에서는 신규 진입만 차단한다.
        #  매도 검사(_check_sell_conditions)는 이 게이트 앞에서 이미 수행되므로 손절 감시는 유지된다.
        if getattr(self, 'buy_halted', False):
            if self.consecutive_errors == 0:  # 로그 도배 방지
                self.log(f"매수 스킵: 방어 모드 — {self.buy_halt_reason or '신규 매수 중단'}")
            return

        # [진입 게이트] 개장 직후 신규 진입 보류. 방어 모드와 같은 자리에 두는 이유는
        #  '신규 매수만 막고 청산 감시는 그대로 둔다'는 성질이 같기 때문이다 —
        #  _check_sell_conditions는 이 함수 밖에서 이미 수행된다.
        #  [근거] 개장 첫 30분 진입은 스파이크를 고점에서 추격하고 되돌림을 맞는다.
        #   tools/audit_time_of_day.py(30m·씨드5) 전체 136.7% vs 현행 131.1%(47-0-28),
        #   같은 차단량 무작위 대조 5장 전승 — 상세는 config.SYSTEM_ENTRY_OPEN_DELAY_USE 주석.
        if is_market_open:
            delay_left = entry_open_delay_remaining()
            if delay_left > 0:
                if self.consecutive_errors == 0:  # 로그 도배 방지
                    self.log(f"매수 스킵: 개장 직후 진입 보류 — {delay_left // 60}분 "
                             f"{delay_left % 60}초 남음 (청산 감시는 계속됩니다)")
                return

        # [안전장치] 미체결 주문 현황을 모르면 신규 매수를 보류한다. 재기동 직후 복구
        #  조회가 실패한 상태로 매수하면, 이미 거래소에 걸린 주문을 못 보고 같은 종목에
        #  두 번째 주문을 낸다. manage_unfilled_orders가 성공하면 자동 해제된다.
        if not getattr(self, 'pending_restore_ok', True):
            if self.consecutive_errors == 0:
                self.log("매수 스킵: 미체결 주문 현황 미확인 — 중복 주문 방지를 위해 보류")
            return

        # [수정] 매수 대상 확장을 위해 국내 주식 및 국내 ETF 리스트 병합 (그룹 정보 추가)
        targets = []
        for item in config.session.stock_data.get("stocks_kr", []):
            item_copy = dict(item)
            item_copy['group'] = 'stocks_kr'
            targets.append(item_copy)
            
        # [수정] ETF 포함 여부 설정에 따라 대상에 추가
        if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
            for item in config.session.stock_data.get("etfs_kr", []):
                item_copy = dict(item)
                item_copy['group'] = 'etfs_kr'
                targets.append(item_copy)
            
        if not targets: return
        
        # [추가] 필터링 카운트 초기화 (매 주기마다 갱신)
        self.skipped_by_market_filter_count = {"KOSPI": 0, "KOSDAQ": 0}
        skipped_stocks = [] # [추가] 시장 필터링으로 보류된 종목 리스트
        
        # [추가] 보유 종목 조회 (중복 매수 방지 및 그룹 정보 매핑)
        holding_codes = set()
        holding_names_map = {}
        holding_groups_map = {}
        
        code_to_group = {}
        for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
            for item in config.session.stock_data.get(key, []):
                code_to_group[item['code']] = key
                
        if holdings:
            for h in holdings:
                # [추가] 이번 루프의 매도 로직에서 수량이 0이 된 종목은 제외
                if api.safe_int(h.get('hldg_qty')) <= 0:
                    continue
                code = h['pdno']
                holding_codes.add(code)
                holding_names_map[code] = h['prdt_name']
                holding_groups_map[code] = code_to_group.get(code, 'stocks_kr')
        
        # [수정] 최대 보유 종목 수 체크 (투자 비중은 개별 룰이 없으면 전역/자동값을 따른다)
        invest_ratio = config.resolve_invest_ratio()
        max_holdings = config.SYSTEM_MAX_HOLDINGS

        can_buy = True
        # [추가] 스킵 사유를 문자열로 들고 간다. 종전에는 여기서만 로그로 흘리고 버려서,
        #  뒤에서 후보를 못 산 이유가 '조건 미달'이라는 뭉뚱그린 문구로만 남았다.
        #  두 로그는 시각이 벌어져(분석 한 바퀴) 붙여 읽기도 어렵다.
        buy_skip_reason = ""
        # [신호 원장] 계좌 상태로 막힌 사실을 원장까지 들고 간다. 종전에는 로그로만 남아,
        #  원장을 읽는 감사가 '게이트를 통과했는데 왜 안 샀나'에 답할 수 없었다.
        buy_block = None

        #  [Fix 2026-09-08] 슬롯은 잔고가 아니라 '잔고 + 미체결'로 센다(occupied_slot_codes).
        slot_codes = self.occupied_slot_codes(holding_codes)
        if len(slot_codes) >= max_holdings:
            pending_only = len(slot_codes) - len(holding_codes)
            buy_skip_reason = (f"보유 슬롯 가득 참 — {len(slot_codes)}/{max_holdings}종목"
                               + (f" (보유 {len(holding_codes)} + 미체결 {pending_only})"
                                  if pending_only else "")
                               + f" (투자비중 {config.format_invest_ratio()})")
            if self.consecutive_errors == 0: # 로그 도배 방지
                self.log(f"매수 스킵: 최대 보유 종목 수({max_holdings}개) 도달 (투자비중 {config.format_invest_ratio()} 기준) - 종목분석은 계속 진행합니다.")
            can_buy = False
            buy_block = 'slot'

        # 예수금 확인 (API 직접 호출)
        avail_cash = 0
        if deposit_res:
            avail_cash = deposit_res['d2_deposit'] # 주문 가능 금액은 D+2 예수금 기준
        else:
            if not can_buy:
                avail_cash = 0
            else:
                return # 조회 실패 시 매수 중단

        # [수정] 최소 주문 가능 금액 하향 조정 (50,000 -> 1,000) 및 로그 추가
        min_cash = 1000
        if can_buy and avail_cash < min_cash:
            buy_skip_reason = f"예수금 부족 — {avail_cash:,}원 (최소 {min_cash:,}원)"
            if self.consecutive_errors == 0: # 로그 도배 방지
                 self.log(f"매수 스킵: 예수금 부족 ({avail_cash:,}원 < {min_cash:,}원) - 종목분석은 계속 진행합니다.")
            can_buy = False
            buy_block = 'cash'
            
        # [추가] 개별 룰 로드 ([최적화] 루프에서 주기당 1회 로드해 전달받으면 재조회 생략)
        if rules_map is None:
            #  [Fix 2026-09-06] 빈 목록은 '개별 룰이 없다'로 읽혀 모든 종목이 전역값으로
            #   판정된다. 룰은 매수 기준(BUY_SCORE·RSI 상한)과 종목당 비중까지 정하므로,
            #   룰을 잃은 채 낸 매수는 운용자가 못 박은 조건 밖에서 나간 주문이다.
            #   진입을 한 주기 미루는 것은 되돌릴 수 있다.
            try:
                custom_rules = db_manager.db.get_all_stock_strategies(strict=True)
            except Exception as _re:
                self.log(f"매수 보류: 종목별 개별 룰을 읽지 못했습니다 — 전역값으로 "
                         f"판정하면 운용자가 정한 조건 밖에서 주문이 나갑니다 "
                         f"({type(_re).__name__}: {_re})")
                return
            custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
            rules_map = {r['code']: r for r in custom_rules}

        # [추가] 당일 매도 이력 확인 및 재진입 허들(체결강도) 설정
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_account = None
        if config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        #  [Fix 2026-09-06] 이 조회는 **게이트 두 개의 입력**이다 — 재진입 체결강도 허들과
        #   '손절가보다 비싸게 되사지 않는다'. 종전에는 실패해도 빈 목록이 돌아와
        #   "오늘 판 종목이 없다"로 읽혔고, 두 게이트가 동시에 사라졌다. 그 게이트가
        #   막으려던 것이 2026-08-05 실측의 손절·재매수 반복 루프인데(103.1% → 127.3%
        #   → 127.5% 로 스스로 세운 허들을 넘으며 왕복 스프레드만큼 손실 누적),
        #   조회 한 번이 실패하면 정확히 그 상태로 돌아간다.
        #   못 읽었으면 이번 주기의 **매수만** 미룬다 — 진입을 한 주기 미루는 것은
        #   되돌릴 수 있고, 방어 없이 낸 재매수는 되돌릴 수 없다. 매도 검사는 다른
        #   경로라 그대로 돈다(손절·트레일링은 멈추지 않는다).
        try:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str,
                                                    is_sim=False, account=target_account,
                                                    strict=True)
        except TypeError:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=False)
            if target_account:
                today_trades = [t for t in today_trades if t.get('account') == target_account]
        except Exception as _te:
            self.log(f"매수 보류: 당일 매매 이력을 읽지 못해 재진입 방어(체결강도 허들·"
                     f"손절가 재매수 차단)를 세울 수 없습니다 — 다음 주기에 다시 봅니다 "
                     f"({type(_te).__name__}: {_te})")
            return
                
        sold_today = set(t['code'] for t in today_trades if "sell" in t.get('type', '').lower() or "매도" in t.get('type', ''))
        
        reentry_hurdles = {}
        # [최적화] 당일 매도 종목의 최근 매수 내역을 배치 쿼리로 일괄 조회
        #  None = 못 읽었다. 재진입 허들은 '없으면 통과'라 조용히 열린다 — 흔적을 남긴다.
        _sold_latest_buys = db_manager.db.get_latest_buy_trades(sold_today, account=target_account)
        if _sold_latest_buys is None:
            self.log("[재진입 허들] 당일 매도분의 매수 기록을 읽지 못했습니다 — "
                     "이번 주기에는 허들 없이 판정합니다.")
            _sold_latest_buys = {}
        for scode in sold_today:
            last_buy = _sold_latest_buys.get(scode)
            if last_buy:
                reason = last_buy.get('reason', '')
                match = re.search(r'체결강도:\s*([0-9.]+)%', reason)
                if match:
                    reentry_hurdles[scode] = float(match.group(1))
                else:
                    reentry_hurdles[scode] = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)

        # [추세추종] 손절로 잘린 종목을 그 손절가보다 **비싸게** 되사지 않는다.
        #  체결강도 허들만으로는 못 막는다 — 재진입할 때마다 그 값이 갱신되어 스스로 세운
        #  허들을 스스로 넘는다(2026-08-05 실측: 103.1% → 127.3% → 127.5%로 통과하며
        #  매 주기 손절·재매수를 반복, 왕복 스프레드만큼 실현 손실이 누적됐다).
        #  추세가 진짜로 돌아섰다면 눌림에서 다시 잡히므로, 이 게이트는 '더 비싸게 되사기'만
        #  정확히 막는다. 익절·트레일링 청산은 대상이 아니다(상승 추세의 정상 재진입까지
        #  막으면 추세추종에 역행한다).
        stop_exit_prices = self._collect_stop_exit_prices(today_trades)

        # 1. 후보 분석
        candidates = self._analyze_candidates(targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map, restricted_stocks=restricted_stocks, stop_exit_prices=stop_exit_prices, buy_block=buy_block)
        
        # 2. 매수 집행
        if candidates:
            if not is_market_open:
                self.log(f"[장마감] 매수 후보 감지 (주문 미전송): {len(candidates)}종목")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                return

            if not can_buy:
                # [수정] '조건 미달'은 사실과 반대였다 — 이 종목들은 매수 조건을 통과한
                #  후보이고, 막은 것은 계좌 상태(슬롯·예수금)다. 무엇을 풀어야 살 수 있는지
                #  이 줄만 보고 알 수 있어야 한다.
                self.log(f"[매수스킵] 매수 조건 충족 {len(candidates)}종목 — 주문 미전송 "
                         f"({buy_skip_reason or '매수 불가 상태'})")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                if buy_skip_reason.startswith("보유 슬롯"):
                    self.log("   └ 보유 종목이 청산되어 슬롯이 비면 다음 주기에 재평가됩니다.")
                return

            #  주문 실행부도 같은 셈을 써야 한다 — 여기만 잔고로 세면 게이트는 막는데
            #  실행부는 자리가 있다고 믿는다.
            self._execute_buy_orders(candidates, avail_cash, invest_ratio,
                                     len(self.occupied_slot_codes(holding_codes)), max_holdings)

    #  손실 청산 계열의 사유 접두어. 이 사유로 나간 종목은 같은 날 그 가격 위에서 되사지 않는다.
    #  (익절·반익절·트레일링·시간청산은 제외 — 추세가 살아 있는 상태의 청산이라 재진입이 정당하다)
    #  'ATR손절'을 따로 둔다 — 매도 실행부(selling)가 ATR 이 적용된 손절의 사유를 '손절(…)'에서
    #  'ATR손절(…)'로 바꿔 적으므로 '손절' 접두어에 걸리지 않는다. 빠져 있던 동안(2026-08-05 게이트
    #  도입 ~ 2026-10-25) 기본 손절 방식(ATR)의 손절에는 이 게이트가 한 번도 걸리지 않았다.
    #  백테스트(portfolio_backtest reentry_block)는 처음부터 셋을 모두 본다.
    _STOP_EXIT_PREFIXES = ("손절", "ATR손절", "본전청산")

    @staticmethod
    def _collect_stop_exit_prices(today_trades):
        """당일 손절 청산의 체결가를 종목별로 모은다. {code: 가장 최근 손절가}

        같은 날 여러 번 손절됐다면 가장 최근 값을 쓴다 — 직전 손절가가 지금 유효한 기준이다.
        """
        out = {}
        for t in sorted(today_trades or [], key=lambda x: str(x.get('time') or '')):
            type_str = str(t.get('type') or '')
            if "sell" not in type_str.lower() and "매도" not in type_str:
                continue
            reason = str(t.get('reason') or '')
            if not reason.startswith(TraderBuyingMixin._STOP_EXIT_PREFIXES):
                continue
            try:
                price = float(str(t.get('price') or '0').replace(',', ''))
            except (TypeError, ValueError):
                continue
            if price > 0:
                out[t['code']] = price   # 시간순이므로 마지막 것이 남는다
        return out

    def _analyze_candidate_worker(self, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map, io_pool=None, stop_exit_prices=None, buy_block=None):
        """(내부함수) 매수 후보 분석용 단일 워커

        io_pool: 차트/체결강도/호가 동시 조회용 공유 스레드풀 (None이면 자체 생성 — 하위 호환)
        """
        if not self.is_running: return None # [추가] 중지 요청 시 즉시 종료
        
        try:
            # [추가] 시스템 트레이딩 스레드임을 마킹 (API 우선순위 획득용)
            context.trade_context.is_system_trading = True
            
            # [추가] API 호출 전 대기 (Rate Limit 방지 - 스레드별 분산 효과)
            time.sleep(safe_delay)
            
            if not self.is_running: return None # 대기 후 재확인
            
            code = item['code']; name = item['name']
            
            # 1. 트레이딩 제한 종목 체크
            if code in restricted_stocks:
                self.set_stock_state(code, None)
                return {'type': 'restricted_skip', 'name': name}

            # [추가] 연장거래 시간에는 그 시장이 취급하지 않는 종목을 스킵한다.
            #  NXT 창: ETF 및 NXT 비거래 종목. KRX 애프터(16:00~20:00): ETF/ETN(일반 주권만 거래).
            is_nxt_market = api.nxt_order_window()
            etf_untraded = api.domestic_etf_untraded_window()
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            if etf_untraded and not is_overseas_stock:
                is_etf = item.get('group') == 'etfs_kr'
                if is_etf or (is_nxt_market and hasattr(api, 'is_nxt_tradeable') and not api.is_nxt_tradeable(code)):
                    self.set_stock_state(code, None)
                    where = "대체거래소(NXT)" if is_nxt_market else "KRX 애프터마켓"
                    return {'type': 'log_only', 'log': f"[연장거래스킵] {name}({code}): {where} 거래 불가 종목(ETF 포함)으로 분석을 스킵합니다."}
            
            # 2. 진행 중인 주문 체크
            if self.order_manager.is_pending(code):
                # [관측성] 종전에는 조용히 빠졌다. 그러면 그 종목은 [분석] 줄도 [분석스킵] 줄도
                #  없이 화면에서 사라져, 왜 후보에서 빠졌는지 운영자가 알 수 없다
                #  (2026-08-05: 손절 직후 NAVER가 종목분석에서 통째로 사라졌다).
                self.set_stock_state(code, None)
                odnos = self.order_manager.pending_odnos(code)
                return {'type': 'log_only',
                        'log': f"[분석스킵] {name}({code}): 진행 중인 주문 존재 "
                               f"({', '.join(str(o) for o in odnos) or '?'}) — 매수 판정을 건너뜁니다"}

            # 3. 보유 종목 체크 (보유분석에서 다루므로 후보 분석에서는 조용히 제외한다)
            if code in holding_codes: return None
            
            # 4. 시장 지수 필터링 (종목별 적용)
            # [Fix] fail-closed: 상태 캐시가 아예 없는 경우(첫 주기 전·조회 실패)도 '판단 불가'로 보아
            #  신규 매수를 보류한다. 기존에는 캐시가 없으면 필터를 통과시켜, 시장 방향을 모르는
            #  상태에서 진입이 이뤄질 수 있었다. ('모르겠으면 아무것도 하지 마라')
            if getattr(config, 'USE_MARKET_FILTER', True):
                market_type = self._get_stock_market_type(code)
                market_stat = self.market_index_status.get(market_type)
                if not isinstance(market_stat, dict) or not market_stat.get('is_healthy', False):
                    self.set_stock_state(code, None)
                    #  [Fix 2026-09-08] 원장에도 남긴다. 종전에는 이 반환에 'ledger' 키가
                    #   아예 없어, **필터를 켠 계좌의 원장이 통째로 비었다** — 실전
                    #   trade_history.db 는 원장 신설(2026-08-19)부터 2026-09-08까지 0행이다.
                    #   그 상태에서는 '왜 안 샀나'를 원장으로 답할 수 없고, 0행이 '신호가
                    #   없었다'인지 '계측이 돌지 않았다'인지조차 가릴 수 없다(원장을 만든
                    #   이유가 바로 그 구분이다). 판정 전에 잘렸으므로 점수·상태는 없다 —
                    #   전용 칸(blocked_market)에 세고, 게이트 차단율의 분자에는 섞지 않는다.
                    return {'type': 'market_skip', 'name': name, 'market_type': market_type,
                            'ledger': {'code': code, 'name': name, 'outcome': 'market'}}
            
            if not self.is_running: return None # API 호출 전 최종 확인
            
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            # [최적화/#6] 호가창(order_book)은 ask_bid_ratio 수급 게이트에만 쓰인다. 이 종목의 유효
            # 임계값(BUY_ASK_BID_RATIO; 개별 룰 우선)이 0이면 게이트가 꺼져 있어 호가 조회가 무의미하므로
            # 생략한다. 토스는 체결강도 미제공으로 호가비가 유일한 수급지표이므로 항상 조회한다.
            _ab_rule = rules_map.get(code)
            #  룰의 NULL(지정 안 함)은 전역값 — rule.get(키, 기본값)은 NULL 에 None 을 준다.
            _ab_thr = _pkg().rule_value(_ab_rule, 'buy_ask_bid_ratio',
                                        config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0))
            _need_ob = config.session.is_toss or (_ab_thr or 0) > 0

            # 5. [최적화] 차트, 체결강도, 호가(수급비율) 데이터 병렬(동시) 조회
            #  호가는 10호가 상세가 아니라 수급 게이트용 비율만 필요하므로 get_ask_bid_ratio를 쓴다.
            #  (WS 호가 총잔량이 신선하면 REST 없이 계산 → 종목당 호가 REST 1콜 절감)
            #  [최적화] 공유 io_pool 사용 시 후보마다 풀을 생성/파괴하지 않는다.
            _local_pool = None
            ex = io_pool
            if ex is None:
                _local_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="at_cand_io")
                ex = _local_pool
            try:
                # [계좌 컨텍스트] 이 풀도 별도 스레드다 — 워커가 물려받은 컨텍스트를
                #  한 번 더 넘겨야 시세 조회가 같은 앱키로 나간다.
                _io = utils.inherit_account_context
                fut_chart = ex.submit(_io(api.get_chart_data), code, is_overseas=is_overseas_stock)
                fut_vol = ex.submit(_io(api.get_realtime_vol_strength), code) if not is_overseas_stock else None

                df = fut_chart.result()
                try: vol_strength = fut_vol.result() if fut_vol else None
                except Exception: vol_strength = None
                # [최적화] 호가는 여기서 당기지 않는다 — 점수·상태·체결강도 게이트를 모두
                #  통과한 종목에만 아래에서 조회한다(지연 조회). 판정 결과는 동일하다.
                ask_bid_ratio = None
            finally:
                if _local_pool is not None:
                    _local_pool.shutdown(wait=False)

            if df is None or df.empty:
                # [관측성] 차트가 없으면 판정 자체가 불가능하다. 조용히 빠지면 그 종목이
                #  왜 후보에서 사라졌는지 알 수 없다.
                self.set_stock_state(code, None)
                return {'type': 'log_only',
                        'log': f"[분석스킵] {name}({code}): 차트 데이터 없음 — 매수 판정 불가"}

            # [수정] 캐시된 차트 데이터의 당일 미확정 종가를 실시간 최신 현재가로 업데이트
            # (종목 분석 메뉴와 시스템 트레이딩 간의 지표 및 점수 불일치 원천 차단)
            realtime_price = 0.0
            try:
                realtime_price = api.get_current_price(code, is_overseas=is_overseas_stock)
                # 모든 장 종료 후에는 지표용 봉을 갱신하지 않는다(KRX 확정 종가 유지).
                indicators.apply_realtime_price(df, api.chart_overlay_price(realtime_price, is_overseas_stock))
            except Exception as e:      # noqa: BLE001 - 아래 price_is_realtime 이 결과를 들고 간다
                logger.debug(f"[분석] 실시간 현재가 조회 예외({code}): {e}")

            # [주문가 보호] 지표는 KRX 확정 종가로 계산하되, 매수 주문 단가는 항상 실시간가를 쓴다.
            #  이 값이 _execute_buy_orders의 cand['price'] → 주문 지정가가 되므로, NXT 시간대에
            #  KRX 종가로 굳으면 호가에서 벗어나 체결되지 않는다.
            #  [실패를 들고 간다 · 2026-09-04] get_current_price 는 실패해도 예외가 아니라 0을
            #   돌려준다. 종전에는 그 0이 조용히 '직전 확정 종가'로 폴백해 **주문 지정가·ATR
            #   손절폭·포지션 수량 세 가지가 한꺼번에** 어긋난 채 주문이 나갔다(셋 다 이 값을
            #   분모로 쓴다). 관심종목 44개 1년 실측으로 그 오차는 중앙값 2.0%·90분위 6.8%·
            #   최대 30.0%다. 분석·점수는 어차피 확정 종가 기준이므로 그대로 두고, **주문만**
            #   막는다(_execute_buy_orders). 판단 불가는 매수 허용이 아니다.
            price_is_realtime = bool(realtime_price and realtime_price > 0)
            current_price = float(realtime_price) if price_is_realtime else float(df.iloc[-1]['close'])

            # [추가] 상관계수 필터링
            correlation_skip_msg = None
            if getattr(config, 'USE_CORRELATION_FILTER', True) and holdings_dfs:
                corr_threshold = getattr(config, 'CORRELATION_THRESHOLD', 0.7)
                cand_ret = df.set_index('date')['close'].astype(float).ffill().pct_change(fill_method=None).dropna()
                cand_group = item.get('group', 'stocks_kr') # 후보 종목의 그룹
                
                for hold_code, hold_info in holdings_dfs.items():
                    hold_group = holding_groups_map.get(hold_code, 'stocks_kr')
                    
                    # [추가] 같은 그룹(국내주식-국내주식, 국내ETF-국내ETF)끼리만 상관계수 비교
                    if cand_group != hold_group:
                        continue
                        
                    hold_name = hold_info['name']
                    # [최적화] _analyze_candidates에서 사전계산된 수익률 시리즈 재사용
                    #  (없으면 1회만 계산해 memoize — 후보×보유 조합마다 재계산 방지)
                    hold_ret = hold_info.get('ret')
                    if hold_ret is None:
                        hold_df = hold_info.get('df')
                        if hold_df is None or hold_df.empty: continue
                        hold_ret = hold_df.set_index('date')['close'].astype(float).ffill().pct_change(fill_method=None).dropna()
                        hold_info['ret'] = hold_ret
                    if hold_ret.empty: continue

                    combined = pd.concat([cand_ret, hold_ret], axis=1, join='inner').dropna()
                    if len(combined) > 30:
                        corr = combined.iloc[:, 0].corr(combined.iloc[:, 1])
                        if corr >= corr_threshold:
                            correlation_skip_msg = f"[상관관계 보류] (보유·미체결 {hold_name} 상관계수: {corr:.2f} >= {corr_threshold})"
                            break

            # [추세추종] 상대강도(RS) 게이트: 소속 지수(KOSPI/KOSDAQ)보다 약한 종목의 신규 진입 차단.
            #   같은 +15%라도 지수가 +20%인 장에서는 열등주 — 지수 대비 초과수익이 없는 종목은
            #   '확실한 추세'가 아니라고 보고 게이트에서 제외한다 (약추세 진입 = 큰 손실의 원천).
            #   룩백은 RS_FILTER_LOOKBACK(>0) 우선, 0이면 스코어링 '가격 모멘텀'과 동일(MOMENTUM_LOOKBACK).
            #   종목 이력 부족·지수 조회 실패 시에는 통과(fail-open — 데이터 장애가 매수 전면 중단으로 번지지 않게).
            rs_skip_msg = None
            if getattr(config, 'USE_RS_FILTER', False) and not is_overseas_stock:
                mom_lb = getattr(config, 'RS_FILTER_LOOKBACK', 0) or config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK', 126)
                if len(df) > mom_lb:
                    try:
                        past_close = float(df['close'].iloc[-(mom_lb + 1)])
                    except (TypeError, ValueError):
                        past_close = 0.0
                    if past_close > 0:
                        stock_mom = (current_price / past_close - 1) * 100
                        idx_mom = analysis.get_index_momentum(self._get_stock_market_type(code), lookback=mom_lb)
                        if idx_mom is not None and stock_mom <= idx_mom:
                            rs_skip_msg = f"[RS필터 보류] ({mom_lb}일 수익률 {stock_mom:+.1f}% ≤ 지수 {idx_mom:+.1f}%)"

            # 룰 및 임계값 설정
            rule = rules_map.get(code)
            market_type = self._get_stock_market_type(code)
            score_adj = market_regime_adj.get(market_type, 0.0)

            # [SSOT] 매도 경로(build_sell_thresholds)와 같은 규약으로 조립한다 —
            #  룰의 NULL 컬럼은 전역 기본값으로 되돌리고 가중치는 dict로 확정한다.
            #  개별 룰이 걸렸다는 이유로 종목이 분석 결과 없이 사라지면 안 된다.
            thresholds = _pkg().build_buy_thresholds(rule=rule, score_adj=score_adj)
            
            # 전략 실행
            result = self.strategy.analyze_buy(code, name, df, current_price, vol_strength=vol_strength, thresholds=thresholds, ask_bid_ratio=ask_bid_ratio)
            if not result:
                self.set_stock_state(code, None)
                return None
            self._note_score_boundary(code, name, result)

            # [추세추종] 추세품질 상한 게이트 — '너무 가파른' 추세는 사지 않는다.
            #  추세품질은 단조가 아니다. 300 위에서는 전방수익이 음수로 꺾이고 꼬리가
            #  잘린다(상위10% 56.6 → 14.2, ATR손절 20.5 → 45.5%). 종목 축의 모멘텀
            #  크래시라 '강할수록 좋다'는 직관과 반대다 — 근거는 config의
            #  ANALYSIS_THRESHOLDS['TREND_QUALITY_MAX'] 주석.
            #  이력 부족(None)은 통과시킨다(fail-open) — 데이터가 없다고 막으면 신규 상장·
            #  데이터 장애가 매수 전면 중단으로 번진다. 순위에서는 이미 최하위로 밀리므로
            #  이중으로 벌하지 않는다.
            #  [위치] 상관·RS 게이트와 같은 높이에 둔다. 매수 상태가 아닌 종목도 아래에서
            #  이 변수를 읽으므로(보류 집계 규약) 분기 안에서 만들면 NameError가 난다.
            #  전역 방어 게이트라 개별 룰로 덮지 않는다 — build_buy_thresholds를 거치지
            #  않고 config에서 직접 읽는다.
            tq_cap_skip_msg = None
            _tq_cap = float(config.ANALYSIS_THRESHOLDS.get('TREND_QUALITY_MAX', 300.0) or 0)
            _tq_now = result.get('trend_quality')
            if _tq_cap > 0 and _tq_now is not None and _tq_now >= _tq_cap:
                tq_cap_skip_msg = (f"[추세품질 상한] (추세품질 {_tq_now:,.0f} >= {_tq_cap:,.0f}"
                                   f" — 모멘텀 크래시 구간)")

            # [최적화] 호가(매도잔량비) 지연 조회.
            #  종전에는 **모든 후보**의 호가를 점수 계산 전에 미리 당겼다. 그런데 호가비는
            #  analyze_buy의 마지막 수급 게이트에만 쓰이고, 거기까지 도달하는 종목은 주기당
            #  0~3개다. WS 등록 한도(41건)가 현재가로 다 차서 호가 구독은 0개라, 이 조회는
            #  전부 REST로 나간다 — 관심종목 40개면 매 주기 40콜이 호가에만 소모됐다.
            #  (2026-08-05: 계정 실제 유량이 명목보다 낮아 EGW00201이 상시 발생했고, 한도를
            #   낮추는 것으로는 해결되지 않았다. 부하는 호출 수로 줄여야 한다.)
            #  판정 결과는 동일하다 — 여기서 쓰는 min_ask_bid_ratio는 analyze_buy가 이미
            #  자동보정까지 마쳐 돌려준 값이고, 원래 게이트와 같은 비교를 그대로 한다.
            #  호가 조회 실패(None)도 종전과 같이 '통과'로 다룬다(fail-open 동작 유지).
            if result['action'] == "buy" and _need_ob:
                min_abr = result.get('min_ask_bid_ratio') or 0
                if min_abr > 0:
                    try:
                        ask_bid_ratio = api.get_ask_bid_ratio(code, is_overseas_stock)
                    except Exception:
                        ask_bid_ratio = None
                    result['ask_bid_ratio'] = ask_bid_ratio
                    if ask_bid_ratio is not None and ask_bid_ratio < min_abr:
                        result['action'] = 'wait'
                        result['vol_reject_reason'] = f"매도비:{ask_bid_ratio:.2f}<{min_abr}"

            # [추가] 분석 성공 시 상태 업데이트
            self.set_stock_state(code, result['state'])
            
            # 로그 출력을 위한 문자열 구성
            rsi_val = f"{result['rsi']:.1f}" if result['rsi'] is not None else "-"
            adx_val = f"{result['adx']:.1f}" if result['adx'] is not None else "-"
            cci_val = f"{result['cci']:.1f}" if result['cci'] is not None else "-"
            sar_val = result.get('psar')
            if sar_val is not None:
                sar_str = "상승" if current_price > sar_val else "하락"
            else:
                sar_str = "-"
            macd_val = result.get('macd'); sig_val = result.get('macd_signal')
            macd_str = "골든" if macd_val is not None and sig_val is not None and macd_val > sig_val else "데드"
            obv_trend = result.get('obv_trend')
            obv_str = "상승" if obv_trend is True else ("하락" if obv_trend is False else "-")
            sm_str = "O" if result.get('smart_money') else "X"
            vol_val = f"{result['vol_strength']:.1f}%" if result.get('vol_strength') else "-"
            rule_msg = " [개별 룰 적용]" if rule else ""
            
            is_buy_state = result['state'] in ["매수", "강매수", "역매수"]

            # [신호 원장] 판정 결과를 로그 문자열이 아니라 **판정한 자리에서** 넘긴다.
            #  종전에는 감사가 [분석] 줄을 정규식으로 되읽었는데, `[매도비:3.92]`(정보 표기)와
            #  `매도비:3.92<1.0`(차단)이 한 글자 차이라 실제로 한 번 뒤집어 읽은 적이 있다
            #  (차단율 1.3% → 75%). 여기서 넘기면 그 위험이 원천적으로 없다.
            #  매수 상태였던 주기만 남긴다 — 신호가 아니었던 것까지 세면 차단율의 분모가
            #  부풀어 기회비용을 과대평가한다.
            def _ledger(outcome):
                if not is_buy_state:
                    return None
                row = {'code': code, 'name': name, 'outcome': outcome,
                       'score': result.get('score'), 'state': result['state'],
                       'vol': result.get('vol_strength'),
                       'abr': result.get('ask_bid_ratio')}
                # 계좌 상태(슬롯 만석·예수금 부족)는 게이트를 통과한 신호에만 의미가 있다.
                #  게이트가 이미 막은 종목까지 슬롯 탓으로 세면 기회비용이 부풀어,
                #  '슬롯을 늘리면 이만큼 더 샀을 것'이라는 틀린 결론을 부른다.
                if outcome == 'passed' and buy_block:
                    row['blocked_by'] = buy_block
                return row

            # [추가] 가짜 체결강도로 걸러진 경우 사유 표시 (매수 시그널일 때만)
            vol_reject_msg = ""
            if is_buy_state:
                if result.get('vol_reject_reason'):
                    vol_reject_msg = f" [{result['vol_reject_reason']}]"
                elif result.get('ask_bid_ratio') is not None:
                    vol_reject_msg = f" [매도비:{result['ask_bid_ratio']:.2f}]"
            
            log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}"
            
            if result['action'] == "buy":
                reentry_msg = ""

                # [추세추종] 당일 손절로 잘린 종목을 그 손절가 이상에서 되사지 않는다.
                #  체결강도 허들은 재진입마다 갱신되어 스스로 넘어가므로 이 경로를 못 막는다
                #  (2026-08-05 실측: 손절 → 10초 뒤 1,000원 비싸게 재매수를 매 주기 반복,
                #   왕복 스프레드만큼 실현 손실만 쌓였다). 추세가 진짜로 살아 있다면 눌림에서
                #  다시 잡히므로, '판 값보다 비싸게 되사기'만 정확히 막는다.
                stop_px = (stop_exit_prices or {}).get(code)
                if (stop_px and current_price >= stop_px
                        and getattr(config, 'REENTRY_BLOCK_ABOVE_STOP_PRICE', True)):
                    return {'type': 'log_only', 'ledger': _ledger('reentry'),
                            'log': f"[분석스킵] {name}({code}): 당일 손절가 재진입 불가 "
                                   f"(현재가 {current_price:,.0f} >= 손절가 {stop_px:,.0f}) "
                                   f"— 더 비싸게 되사지 않습니다"}

                if code in reentry_hurdles:
                    req_vol = reentry_hurdles[code]
                    vol_strength_val = result.get('vol_strength')
                    if config.session.is_toss:
                        # [추가] 토스: 체결강도 미제공 → 매도잔량비(ask_bid_ratio)로 당일 재진입 판단
                        # [알려진 비대칭 — 무동작에 가깝다] 여기 쓰는 min_abr은 analyze_buy가
                        #  일반 매수 게이트에서 이미 적용한 값과 같다. 즉 이 자리에 온 후보는
                        #  그 검사를 통과한 뒤라, 실제로 걸리는 경우는 '호가를 못 구했다(None)'
                        #  하나뿐이다. KIS의 '직전 진입 체결강도를 경신해야 한다'는 자기 갱신
                        #  허들에 해당하는 장치가 토스에는 없고, 당일 재진입 방어는 위의
                        #  손절가 게이트만 남는다.
                        #  고치려면 새 진입 필터를 넣는 일이 되므로 같은 차단율 무작위 대조가
                        #  먼저다(config ANALYSIS_THRESHOLDS 주석의 진입 필터 채택 규칙).
                        #  현재 동작은 tests/test_reentry_hurdle.py 가 못 박고 있다.
                        abr = result.get('ask_bid_ratio')
                        min_abr = result.get('min_ask_bid_ratio', 0) or 0
                        if abr is None or (min_abr > 0 and abr < min_abr):
                            log_msg = f"[분석스킵] {name}({code}): 당일 재진입 불가 (매도비 {abr if abr is not None else 0:.2f} < {min_abr:.2f})"
                            return {'type': 'log_only', 'log': log_msg, 'ledger': _ledger('reentry')}
                        else:
                            reentry_msg = f"당일 재진입(매도비 {abr:.2f})"
                    elif vol_strength_val is None or vol_strength_val <= req_vol:
                        log_msg = f"[분석스킵] {name}({code}): 당일 재진입 불가 (체결강도 {vol_strength_val if vol_strength_val else 0:.1f}% <= 기존매수 {req_vol:.1f}%)"
                        return {'type': 'log_only', 'log': log_msg, 'ledger': _ledger('reentry')}
                    else:
                        reentry_msg = f"당일 재진입(기존 {req_vol:.1f}% 경신)"

                candidate_data = {
                    'code': code, 'name': name, 'price': current_price,
                    'price_is_realtime': price_is_realtime,
                    'score': result['score'], 'rsi': result['rsi'], 'adx': result['adx'], 'cci': result['cci'], 'atr': result.get('atr', 0), 'vol_strength': result.get('vol_strength'),
                    'w52_pos': result.get('w52_pos', 0.0),  # [추세추종] 52주 위치 (우선순위 정렬용)
                    'trend_quality': result.get('trend_quality'),  # [추세추종] 추세 품질(회귀 모멘텀, 랭킹 1순위 키)
                    'ask_bid_ratio': result.get('ask_bid_ratio'),  # [추가] 토스 수급 지표(체결강도 대체)
                    'is_custom_rule': bool(rule), 'rule': rule, 'state': result['state'],
                    'state_reason': result.get('state_reason', ''),
                    'reentry_msg': reentry_msg
                }
                reentry_log = f" [{reentry_msg}]" if reentry_msg else ""
                stale_mark = "" if price_is_realtime else "(직전종가·실시간 조회 실패)"
                log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}{stale_mark}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}{reentry_log}"
                
                if correlation_skip_msg:
                    log_msg += f" {correlation_skip_msg}"
                    return {'type': 'correlation_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('corr')}
                elif rs_skip_msg:
                    log_msg += f" {rs_skip_msg}"
                    return {'type': 'rs_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('rs')}
                elif tq_cap_skip_msg:
                    log_msg += f" {tq_cap_skip_msg}"
                    return {'type': 'tq_cap_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('tq')}

                return {'type': 'candidate', 'data': candidate_data, 'log': log_msg,
                        'ledger': _ledger('passed')}
            else:
                # [보류 집계의 의미] '보류'는 **살 수 있었는데 게이트가 막았다**는 뜻이어야 한다.
                #  애초에 매수 상태가 아니었던 종목까지 보류로 세면, 주기 말미 요약
                #  ("유사 테마로 매수 보류 N종목")이 실제 기회비용을 부풀린다. 운용자가
                #  그 숫자를 보고 상관 임계값이나 RS 설정을 조정하면 틀린 근거로 판단하게 된다.
                #  종전에는 사유 문구만 is_buy_state로 걸러내고 반환 타입은 그대로 두어,
                #  집계에는 들어가는데 로그엔 이유가 없는 상태였다.
                if correlation_skip_msg and is_buy_state:
                    log_msg += f" {correlation_skip_msg}"
                    return {'type': 'correlation_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('corr')}
                elif rs_skip_msg and is_buy_state:
                    log_msg += f" {rs_skip_msg}"
                    return {'type': 'rs_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('rs')}
                elif tq_cap_skip_msg and is_buy_state:
                    log_msg += f" {tq_cap_skip_msg}"
                    return {'type': 'tq_cap_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('tq')}

                # [게이트 분류] action이 buy가 아닌데 상태는 매수인 경우 = 수급 게이트가 막았다.
                #  사유 문자열은 engine.analyze_buy가 만든 것을 그대로 분기 판단에만 쓴다
                #  (로그를 되읽는 것이 아니라 같은 프로세스의 구조화된 값이다).
                _reason = (result.get('vol_reject_reason') or "") if is_buy_state else ""
                if _reason.startswith("체결:"):
                    _out = 'gate_vol'
                elif _reason.startswith("매도비:"):
                    _out = 'gate_abr'
                elif _reason:
                    _out = 'gate_hold'          # "체결강도 미확인(보류)"
                else:
                    _out = 'other'
                return {'type': 'log_only', 'log': log_msg, 'ledger': _ledger(_out)}
        except Exception as e:
            # [관측성] 종전에는 `except Exception: return None` 이었다. 후보 하나가 던지면
            #  그 종목은 아무 흔적 없이 사라졌고 — 로그도, 신호 원장 행도 남지 않았다.
            #  원장이 비면 게이트 차단율의 분모가 조용히 줄어 tools/audit_gate_forward.py 의
            #  표본이 편향된다. 바깥 as_completed 쪽에 같은 목적의 가드가 있지만, 여기서
            #  먼저 삼켜 버리므로 그 가드는 닿지 않는다. 매도측(_sell_worker_guarded)과
            #  같은 형태로 남긴다.
            self.log(f"[분석실패] {item.get('name') or item.get('code')}"
                     f"({item.get('code')}): 매수 판정 중 오류 — {type(e).__name__}: {e}")
            logger.exception(f"[매수분석] {item.get('code')} 판정 실패")
            return None

    # [관측 · 2026-10-16] 채점 경계 원장(db.record_score_boundary)용 주기 수집기.
    #  매수 판정 워커(at_cand 풀)가 analyze_buy 직후 사건이 있던 종목만 넣고, 주기 끝에
    #  신호 원장과 같은 자리에서 한 번에 쓴다. 판단에는 쓰지 않는다. 직전 확산 상태는
    #  메모리에만 둔다 — 재시작 뒤 첫 관측은 '뒤집힘'으로 세지 않는다(관측이 덜 셀 뿐).
    _BOUNDARY_LOCK = threading.Lock()

    def _note_score_boundary(self, code, name, result):
        try:
            diag = result.get('score_diag') if isinstance(result, dict) else None
            if not isinstance(diag, dict):
                return
            with self._BOUNDARY_LOCK:
                last = self.__dict__.setdefault('_boundary_last_exp', {})
                prev = last.get(code)
                last[code] = diag.get('expanding')
                flip = prev is not None and prev != diag.get('expanding')
                #  '사건' = 비율 경계 근처 · 직전 대비 확산 뒤집힘 · 가격 모멘텀 fail-open.
                #  (확산 조건이 문턱을 가르는 것 자체는 매수 후보 대부분에서 상시라 사건이 아니다)
                if not (diag.get('near') or flip or diag.get('fail_open')):
                    return
                rows = self.__dict__.get('_boundary_rows')
                if rows is not None:
                    rows.append({'code': code, 'name': name, 'diag': diag, 'flip': flip})
        except Exception as e:
            logger.debug(f"[경계계측] 수집 생략({code}): {e}")

    def _drain_boundary_rows(self):
        with self._BOUNDARY_LOCK:
            rows = self.__dict__.get('_boundary_rows') or []
            self.__dict__['_boundary_rows'] = None
            return rows

    def _analyze_candidates(self, targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map, restricted_stocks=None, stop_exit_prices=None, buy_block=None):
        candidates = []
        skipped_stocks = []
        ledger_rows = []               # [신호 원장] 이 주기의 매수 신호 판정 (주기 끝에 1회 기록)
        with self._BOUNDARY_LOCK:
            self.__dict__['_boundary_rows'] = []   # [관측] 이 주기의 채점 경계 사건
        restricted_skipped_stocks = [] # [추가] 트레이딩 제한 스킵 리스트
        correlation_skipped_stocks = [] # [추가] 상관관계 스킵 리스트
        rs_skipped_stocks = [] # [추세추종] 상대강도(RS) 필터 스킵 리스트
        tq_cap_skipped_stocks = [] # [추세추종] 추세품질 상한(모멘텀 크래시) 스킵 리스트

        # [추가] 트레이딩 제한 종목 로드 (현재 시스템 트레이딩 계좌 기준으로 필터링)
        #  ([최적화] 루프에서 주기당 1회 로드해 전달받으면 파일 재조회 생략)
        if restricted_stocks is None:
            _trade_cano, _trade_acnt = _get_trade_account()
            restricted_stocks = get_restricted_stocks(_trade_cano, _trade_acnt)
        
        # [추가] 시장 국면 판단 (적응형 임계값용)
        market_regime_adj = {} # Market Type -> Score Adj
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", False):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj
                if self.consecutive_errors == 0:
                    # [추가] 시장 필터링 상태 로그
                    filter_status_str = ""
                    if getattr(config, 'USE_MARKET_FILTER', True):
                        market_stat = self.market_index_status.get(m_type)
                        if market_stat and isinstance(market_stat, dict):
                            is_healthy = market_stat.get('is_healthy', True)
                            filter_status_str = "허용" if is_healthy else "보류"
                            filter_status_str = f" | 필터링: {filter_status_str}"
                    self.log(f"[{m_type}] 시장 국면: {regime} (매수기준 {adj:+.1f}점){filter_status_str}")

        # [추가] 보유 종목의 차트 데이터 수집 (상관계수 분석용)
        holdings_dfs = {}
        use_corr_filter = getattr(config, 'USE_CORRELATION_FILTER', True)
        #  [Fix 2026-09-08] 비교 대상은 잔고가 아니라 '잔고 + 미체결'이다(슬롯과 같은 셈).
        #   백테스트에서는 매수가 즉시 포지션이 되어 **다음 판정의 held 에 들어간다**
        #   (portfolio_backtest 의 entry_gate(day, code, tuple(positions))). 실매매에서만
        #   체결 전까지 보이지 않아, 이 필터를 검증한 모델보다 실매매가 더 허용적이었다.
        #   후보끼리 비교하지 않는 것은 양쪽이 같은 설계 범위이므로 건드리지 않는다
        #   (한 번에 만든 후보 목록을 같은 스냅샷으로 판정한다).
        corr_ref_codes = self.occupied_slot_codes(holding_codes) if use_corr_filter else set()
        if use_corr_filter and corr_ref_codes:
            for code in corr_ref_codes:
                is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
                df = api.get_chart_data(code, is_overseas)
                if df is not None and not df.empty:
                    #  미체결분은 이름이 없을 수 있다 — 코드로 표기한다(대조에는 영향 없음).
                    name = holding_names_map.get(code, code)
                    # [최적화] 수익률 시리즈를 주기당 1회만 계산 (워커에서 후보×보유 조합마다 재계산 방지)
                    try:
                        ret = df.set_index('date')['close'].astype(float).ffill().pct_change(fill_method=None).dropna()
                    except Exception:
                        ret = None
                    holdings_dfs[code] = {'name': name, 'df': df, 'ret': ret}

        # [최적화] 분석 대상 종목 실시간 데이터 일괄 수집 (Micro-Cache 사전 예열)
        codes_to_prefetch = []
        for item in targets:
            code = item['code']
            if code in restricted_stocks or code in holding_codes or self.order_manager.is_pending(code):
                continue
            
            codes_to_prefetch.append(code)
            
        if codes_to_prefetch:
            # [수정] 시장 구분(_get_stock_market_type)에 필요한 현재가 정보를 먼저 일괄 prefetch 합니다.
            # 이렇게 하면 _analyze_candidate_worker 내부에서 개별 API 호출을 방지할 수 있습니다.
            api.prefetch_multiple_current_prices(codes_to_prefetch, is_overseas=False, include_investor=False, prefer_ws=True)

        # [수정] 일괄 예열 캐시를 활용하므로 워커별 딜레이를 대폭 단축 (Rate Limit 안전장치 유지)
        tps = config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 0.1

        # [병렬 처리] 사용자 작업과의 충돌 및 모의투자 API 제한(2 TPS) 고려
        # (실전: 5개, 모의: 2개 - ThrottledSession이 병목 없이 안전하게 제어함)
        max_workers = 5

        # [최적화] 워커 내부의 차트/체결강도/호가 동시 조회용 I/O 풀을 공유
        #  (기존에는 후보 종목마다 ThreadPoolExecutor(3)를 생성/파괴 — 저사양 환경에서 오버헤드)
        io_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers * 3, thread_name_prefix="cand_io")
        # [계좌 컨텍스트] trade_context 는 threading.local 이라 워커로 상속되지 않는다.
        #  감싸지 않으면 워커의 시세 조회가 자동 계좌가 아니라 **수동 계좌 앱키**로 나간다
        #  (core.utils.get_common_headers). 매도 워커는 이미 이렇게 감싸고 있었는데
        #  매수측만 빠져 있었다 — 워커가 is_system_trading 만 손으로 세우고 있던 것이
        #  그 흔적이다. 반드시 제출 스레드에서 만든다.
        _cand_task = utils.inherit_account_context(self._analyze_candidate_worker)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="at_cand") as executor:
                futures = [executor.submit(_cand_task, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map, io_pool=io_pool, stop_exit_prices=stop_exit_prices, buy_block=buy_block) for item in targets]

                for future in concurrent.futures.as_completed(futures):
                    if not self.is_running: break
                    # [관측성] 한 종목의 예외가 나머지 후보 분석을 통째로 중단시키면 안 된다.
                    #  종전에는 result()의 예외가 그대로 올라와 남은 종목이 조용히 분석되지
                    #  않았다. 실패한 종목만 로그로 남기고 나머지는 계속 본다.
                    try:
                        res = future.result()
                    except Exception as e:
                        self.log(f"[분석실패] 매수 후보 판정 중 오류 — {type(e).__name__}: {e}")
                        logger.exception("[매수분석] 후보 판정 실패")
                        continue
                    if res:
                        if res.get('ledger'):
                            ledger_rows.append(res['ledger'])
                        if res['type'] == 'candidate':
                            self.log(res['log'])
                            candidates.append(res['data'])
                        elif res['type'] == 'log_only':
                            self.log(res['log'])
                        elif res['type'] == 'restricted_skip':
                            restricted_skipped_stocks.append(res['name'])
                        elif res['type'] == 'market_skip':
                            m_type = res.get('market_type', 'KOSPI')
                            if m_type in self.skipped_by_market_filter_count:
                                self.skipped_by_market_filter_count[m_type] += 1
                            skipped_stocks.append(res['name'])
                        elif res['type'] == 'correlation_skip':
                            self.log(res['log'])
                            correlation_skipped_stocks.append(res['name'])
                        elif res['type'] == 'rs_skip':
                            self.log(res['log'])
                            rs_skipped_stocks.append(res['name'])
                        elif res['type'] == 'tq_cap_skip':
                            self.log(res['log'])
                            tq_cap_skipped_stocks.append(res['name'])
        finally:
            io_pool.shutdown(wait=False)

        # [신호 원장] 주기당 쓰기 1회. 실패해도 매매는 그대로 간다 — 계측이 매매를 막지 않는다.
        if ledger_rows:
            try:
                db_manager.db.record_signal_ledger(
                    datetime.now().strftime("%Y%m%d"), ledger_rows)
            except Exception as e:
                logger.warning(f"[Ledger] 신호 원장 기록 생략: {e}")
        boundary_rows = self._drain_boundary_rows()
        if boundary_rows:
            try:
                db_manager.db.record_score_boundary(
                    datetime.now().strftime("%Y%m%d"), boundary_rows)
            except Exception as e:
                logger.warning(f"[Ledger] 채점 경계 원장 기록 생략: {e}")

        # [추가] 트레이딩 제한 종목 스킵 로그 기록
        if restricted_skipped_stocks:
            self.log(f"[매수 스킵] 트레이딩 제한 종목 ({len(restricted_skipped_stocks)}개): {', '.join(restricted_skipped_stocks)}")

        # [추가] 시장 필터링 보류 종목 로그 기록
        if skipped_stocks:
            # [Fix] '약세라서 보류'와 '지수 판단 불가라서 보류'는 원인이 다르므로 로그에서 구분한다.
            _unknown_markets = [m for m, s in self.market_index_status.items()
                                if isinstance(s, dict) and s.get('unknown')]
            _cause = (f"지수 판단 불가({', '.join(_unknown_markets)}) 매수 보류"
                      if _unknown_markets else "하락장 매수 보류")
            self.log(f"[시장 필터링] {_cause} ({len(skipped_stocks)}종목): {', '.join(skipped_stocks)}")

        # [추가] 상관관계 보류 종목 로그 기록
        if correlation_skipped_stocks:
            self.log(f"[상관관계 보류] 보유·미체결 종목과 유사 테마로 매수 보류 ({len(correlation_skipped_stocks)}종목): {', '.join(correlation_skipped_stocks)}")

        # [추세추종] 상대강도(RS) 필터 보류 종목 로그 기록
        if rs_skipped_stocks:
            self.log(f"[RS필터 보류] 지수 대비 약세로 매수 제외 ({len(rs_skipped_stocks)}종목): {', '.join(rs_skipped_stocks)}")

        # [추세추종] 추세품질 상한 보류 종목 로그 기록 — 이 줄이 주기마다 여러 건 찍히면
        #  유니버스가 과열된 것이다(config TREND_QUALITY_MAX 주석의 '되돌릴 조건').
        if tq_cap_skipped_stocks:
            _cap = config.ANALYSIS_THRESHOLDS.get('TREND_QUALITY_MAX', 300.0)
            self.log(f"[추세품질 상한] 과열 추세(추세품질 {_cap:,.0f} 이상)로 매수 제외 "
                     f"({len(tq_cap_skipped_stocks)}종목): {', '.join(tq_cap_skipped_stocks)}")

        # [추세추종] 우선순위 정렬 — 추세 품질(회귀 모멘텀) 1순위 (근거는 candidate_priority_key docstring)
        candidates.sort(key=candidate_priority_key)

        # [추가] 선정된 후보군 우선순위 로그 출력
        if candidates:
            # [문구] 로그는 한 번 읽고 이해돼야 하고, 무엇보다 실제 정렬 순서와 같아야 한다.
            #  2026-08-12 실증으로 1순위가 추세품질에서 점수로 바뀌었다(candidate_priority_key
            #  docstring 참조) — 순서를 옛 문구로 남겨두면 로그를 믿고 원인을 엉뚱한 데서 찾게 된다.
            #  산식(연환산 기울기 × R²)과 밴드는 도움말 '색상 조건' 표 맨 아래로 옮겼다 —
            #  매 주기 찍히는 로그에 같은 설명을 되풀이할 이유가 없다.
            self.log(f"[매수 후보 선정] 총 {len(candidates)}종목 (우선순위순) "
                     f"— 점수가 1순위이고, 점수가 같으면 추세품질이 높을수록 검증된 추세며, "
                     f"매수 여부는 가르지 않고(게이트 아님) 순위만 정한다")
            for i, c in enumerate(candidates):
                tq = c.get('trend_quality')
                tq_disp = f"{tq:.0f} ({indicators.describe_trend_quality(tq)})" if tq is not None else "- (이력부족)"
                w52_disp = f"{c['w52_pos']:.0f}%" if c.get('w52_pos') else "-"
                vol_disp = f"{c['vol_strength']:.1f}%" if c.get('vol_strength') else "-"
                # 표기 순서도 정렬 순서와 맞춘다 — 점수가 앞이고 추세품질이 그 동점을 가른다.
                self.log(f"   {i+1}순위: {c['name']} (점수:{c['score']}, 추세품질:{tq_disp}, 52주위치:{w52_disp}, 체결:{vol_disp})")
        
        return candidates


    def _execute_buy_orders(self, candidates, avail_cash, invest_ratio, current_holdings_count, max_holdings):
        for cand in candidates:
            if not self.is_running: break

            # [판단 불가는 매수 허용이 아니다] 실시간 현재가를 못 받은 후보는 주문하지 않는다.
            #  아래에서 이 값 하나로 주문 지정가·ATR 손절폭·포지션 수량이 전부 정해진다.
            #  직전 확정 종가로 대신하면 셋이 같은 방향으로 함께 틀어진다(1년 실측 중앙값 2.0%·
            #  90분위 6.8%·최대 30.0%). 못 사는 종목이 생기는 건 다음 주기에 회복된다.
            if not cand.get('price_is_realtime', True):
                self.log(f"매수 보류: {cand.get('name', '')}({cand.get('code', '')}) - "
                         f"실시간 현재가 조회 실패. 직전 확정 종가로는 주문가·손절폭·수량이 "
                         f"함께 어긋나므로 진입하지 않습니다(다음 주기 재평가).")
                continue

            # [오염 일봉 진입 금지] 토스 캔들(NXT 혼입)로 계산된 지표로는 사지 않는다.
            _contam = contaminated_chart_reason(cand.get('code', ''))
            if _contam:
                self.log(f"매수 보류: {cand.get('name', '')}({cand.get('code', '')}) - KRX 공식 일봉 "
                         f"미확보({_contam}), 토스 캔들(NXT 포함) 지표로는 진입하지 않습니다.")
                continue

            # [수정] 최소 주문 가능 금액 하향 조정
            if avail_cash < 1000:
                self.log(f"매수 중단: 잔여 예수금 부족 ({avail_cash:,}원)")
                break
            
            # [추가] 최대 보유 종목 수 도달 시 추가 매수 중단
            if current_holdings_count >= max_holdings:
                self.log(f"매수 중단: 최대 보유 종목 수({max_holdings}개) 도달")
                break

            # [추가] 손절률, ATR 여부 및 투자 비중 확인 (개별 룰 or 전역 설정)
            sl_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            cand_invest_ratio = invest_ratio

            if cand.get('rule'):
                rule = cand['rule']
                sl_rate = rule.get('stop_loss', sl_rate)
                if rule.get('use_atr_stop') is not None:
                    use_atr_stop = bool(rule['use_atr_stop'])
                if rule.get('atr_stop_multiplier') is not None:
                    atr_mult = rule['atr_stop_multiplier']
                # [수정] 개별 룰의 비중이 0/None이면 '자동' → 전역(또는 슬롯 균등 분할)을 따른다.
                #   종전에는 룰 저장 시점의 값이 박제돼 슬롯 수를 바꿔도 그 종목만 옛 비중으로
                #   남아 명목합이 조용히 100%를 넘었다.
                cand_invest_ratio = config.resolve_invest_ratio(rule.get('invest_ratio'))

            atr_val = cand.get('atr', 0)
            price_val = cand.get('price', 0)
            atr_sl_rate = None # DB 저장용
            
            if use_atr_stop and atr_val > 0 and price_val > 0:
                # [SSOT 2026-08-09] 산식·캡은 engine.atr_stop_rate 가 단독 보유한다.
                #  종전에는 이 자리와 피라미딩·백테스트가 각자 같은 식을 복제해 6벌이 있었다.
                #  캡(MAX_ATR_STOP_LOSS_RATE)을 조정하면 실매매와 백테스트가 갈라질 수 있는
                #  구조였고, 그 캡의 타당성을 백테스트로 검증하려던 참이라 먼저 통합한다.
                uncapped = -((atr_val * atr_mult / price_val) * 100)
                sl_rate = _pkg().atr_stop_rate(atr_val, price_val, atr_mult=atr_mult)
                if sl_rate is not None and sl_rate > uncapped:
                    self.log(f"[리스크 조정] ATR 손절률({uncapped:.1f}%)이 최대 한도({sl_rate:.1f}%)를 초과하여 조정됩니다.")

            # [추세추종 안전장치] "탈출 전략이 없다면 포지션을 잡지 마라"
            #  ATR 손절이 꺼져 있고(또는 ATR 미확보) 고정 손절도 0(미사용)이면 이 매수는
            #  청산 기준이 없는 포지션이 된다. 게다가 allocate_budget은 손절폭이 0이면
            #  리스크 캡을 건너뛰어 '손실액 상한'까지 함께 사라진다(집중 캡만 남음).
            #  기본 설정에서는 도달하지 않고(ATR 손절 ON·고정 -7%), 사용자가 전역이나
            #  개별 룰에서 둘 다 끈 경우에만 걸린다 → 매수를 진행하지 않고 건너뛴다.
            if not sl_rate or abs(sl_rate) <= 0:
                self.log(f"[매수 보류] {cand.get('name', '')}({cand.get('code', '')}) 손절 기준 없음 — "
                         f"ATR 손절 {'ON(ATR 미확보)' if use_atr_stop else 'OFF'} + 고정 손절 0(미사용). "
                         f"청산 기준과 손실액 상한이 모두 사라지므로 진입하지 않습니다. "
                         f"(설정 > 매도 전략에서 ATR 손절을 켜거나 고정 손절률을 지정하세요)")
                continue

            # [수정] 자산 배분 로직 개선: 마지막 슬롯인 경우 남은 예수금 전액 투자
            remaining_slots = max_holdings - current_holdings_count
            
            # 1. 예산 할당 계산 (변동성 타겟팅 및 리스크 관리 적용)
            calc_amt = self.risk_manager.allocate_budget(
                avail_cash, cand_invest_ratio, stop_loss_rate=sl_rate,
                atr=cand.get('atr'), current_price=cand.get('price'),
                market_type=self._get_stock_market_type(cand['code']))
            
            if remaining_slots == 1:
                # 마지막 종목일 때: 변동성 타겟팅/리스크 관리가 꺼져있다면 잔여 예수금 전액 사용, 켜져 있다면 계산된 금액 준수
                if not getattr(config, 'USE_VOLATILITY_TARGETING', True) and getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0) <= 0:
                    invest_amt = avail_cash
                else:
                    invest_amt = calc_amt
            else:
                invest_amt = calc_amt

            # [수정] 지정가 주문을 위해 현재가(정수) 확보
            current_price = int(cand['price'])

            # [수정] 슬리피지 비율 적용 및 호가 정렬 (체결 확률 증대)
            raw_order_price = current_price * (1 + config.SLIPPAGE_RATE)
            order_price = int(utils.adjust_to_tick(
                raw_order_price, is_overseas=False,
                is_etf=api.is_domestic_etf_etn(cand['code'], cand.get('name'))))
            # [안전장치] 상한가에 락되면 '현재가 + 슬리피지'가 제한폭을 넘어 거부된다.
            order_price = int(self._clamp_order_price(cand['code'], order_price))

            # [사이징 상한] 배분액이 1주 값에 못 미칠 때의 처리.
            #  종전에는 무조건 배분액을 1주 값까지 끌어올렸다(가용 예수금 전체를 쓰는 버그
            #  방지). 그러나 그러면 기초비중·리스크 한도·변동성 타겟팅이 min 결합으로 합의한
            #  상한이 1주 값 하나에 덮어써진다. 큰 계좌에서는 발동하지 않지만 시드 500만에서는
            #  관심목록의 17%(고가주)가 이 경로를 타 목표의 3배까지 집행됐다.
            #  → 초과 배수가 MAX_POSITION_OVERSHOOT 이내일 때만 1주를 허용하고, 넘으면
            #    진입하지 않는다. 못 사는 종목이 생기는 건 시드의 한계이지 고쳐야 할 버그가
            #    아니다 — 의도한 비중으로 담을 수 없는 종목을 억지로 담는 쪽이 위험하다.
            if invest_amt < order_price:
                overshoot_cap = float(getattr(config, 'MAX_POSITION_OVERSHOOT', 1.3) or 1.0)
                if invest_amt <= 0 or order_price > invest_amt * overshoot_cap:
                    ratio = f"{order_price / invest_amt:.1f}배" if invest_amt > 0 else "배분액 0원"
                    self.log(f"매수 보류: {cand.get('name', '')}({cand.get('code', '')}) - "
                             f"1주 {order_price:,}원이 배분액 {int(invest_amt):,}원의 {ratio} "
                             f"(상한 {overshoot_cap:.1f}배). 의도한 비중으로 담을 수 없는 종목입니다.")
                    continue
                invest_amt = order_price

            # [추가] 포트폴리오 히트 캡: 보유 전체 오픈 리스크 + 신규 리스크가 한도를 넘으면 축소/보류.
            #  종목당 한도(SYSTEM_RISK_PER_TRADE)와 별개로 '동시 다발 손절' 합산 손실을 통제한다.
            #  손절률이 없는(>=0) 경우 리스크 추정이 불가하므로 게이트를 건너뛴다(allocate_budget과 동일 기조).
            if sl_rate and sl_rate < 0:
                budget_left = self.risk_manager.portfolio_risk_budget_left(avail_cash=avail_cash)
                if budget_left is not None:
                    cap = self.risk_manager.effective_portfolio_cap()
                    new_risk = invest_amt * (abs(sl_rate) / 100.0)
                    if new_risk > budget_left:
                        allowed_amt = int(max(budget_left, 0) / (abs(sl_rate) / 100.0))
                        if allowed_amt < order_price:
                            # 한도에 '닿아서' 막는 것과 한도를 '계산 못 해서' 막는 것은 다르다.
                            #  같은 문구로 찍으면 데이터 결손을 정상 동작으로 읽게 된다.
                            if getattr(self, 'portfolio_heat_unknown', False):
                                why = "오픈 리스크 산출 실패 — 한도를 계산할 수 없어 보류한다"
                            elif not (getattr(self, 'current_total_asset', 0) or self.initial_asset):
                                why = "기준자산 미확보 — 한도를 계산할 수 없어 보류한다"
                            else:
                                why = (f"포트폴리오 총 리스크 한도({cap:.1f}%) 도달 "
                                       f"(현재 오픈 리스크 {self.portfolio_heat_amt:,.0f}원, "
                                       f"남은 예산 {max(budget_left, 0):,.0f}원)")
                            self.log(f"매수 보류: {cand['name']} - {why}")
                            continue
                        self.log(f"[히트 캡] {cand['name']} 투자금 축소: {invest_amt:,}원 → {allowed_amt:,}원 "
                                 f"(총 오픈 리스크 한도 {cap:.1f}% 준수)")
                        invest_amt = allowed_amt

            # [수정] 단순 계산 대신 API를 통해 정확한 매수 가능 수량 조회
            # 지정가 주문 시 해당 가격 기준으로 조회
            max_qty = api.fetch_buyable_quantity(cand['code'], order_price)
            
            # [추가] API가 0을 반환할 경우 로컬 예수금 기반 Fallback 계산
            if max_qty <= 0 and avail_cash > 0:
                max_qty = int((avail_cash * 0.998) / order_price)
            
            # 자산 배분 비중 적용 수량
            target_qty = int(invest_amt / order_price)
            
            # 실제 주문 수량은 (목표 수량)과 (API 조회 가능 수량) 중 작은 값
            qty = min(target_qty, max_qty)
            
            # [개선] 예수금(로컬) 부족 시 수량 자동 조정
            if avail_cash < (qty * order_price):
                qty = int(avail_cash / order_price)
            
            # [추가] 예수금 부족 로그
            if qty < 1:
                self.log(f"매수 실패: {cand['name']} - 매수 가능 수량 부족 (목표:{target_qty}, 가능:{max_qty}) | 예수금:{avail_cash:,}원, 필요:{order_price:,}원(1주)")
                continue
            
            rsi_val = f"{cand['rsi']:.1f}" if cand['rsi'] is not None else "-"
            adx_val = f"{cand['adx']:.1f}" if cand['adx'] is not None else "-"
            cci_val = f"{cand['cci']:.1f}" if cand.get('cci') is not None else "-"
            vol_val = f"{cand['vol_strength']:.1f}%" if cand.get('vol_strength') is not None else "-"
            
            # [수정] 매수 사유 포맷 분기 (일반/역매수)
            is_mr_buy = cand.get('state') == "역매수"
            state_reason = cand.get('state_reason', '')
            is_super = "슈퍼 모멘텀" in state_reason
            
            reason = "역매수 반등" if is_mr_buy else "조건 만족"
            if is_super:
                reason += "(슈퍼모멘텀)"
                
            if cand.get('reentry_msg'):
                reason += f" [{cand['reentry_msg']}]"
                
            if cand.get('is_custom_rule'):
                reason += " [개별 룰 적용]"
            
            # [수정] 토스는 체결강도 미제공 → 매도잔량비로 표기(재진입 허들 파싱과 무관)
            if config.session.is_toss:
                abr = cand.get('ask_bid_ratio')
                abr_val = f"{abr:.2f}" if abr is not None else "-"
                reason += f" [점수:{cand['score']}, RSI:{rsi_val}, 매도비:{abr_val}]"
            else:
                reason += f" [점수:{cand['score']}, RSI:{rsi_val}, 체결강도:{vol_val}]"
            
            atr_msg = ""
            if atr_val > 0 and price_val > 0:
                annual_vol = (atr_val / price_val) * math.sqrt(252) * 100
                atr_msg += f"[ATR:{int(atr_val):,}/변동성:{annual_vol:.1f}%]"
            
            # [ATR손절:-7%] 는 걷어냈다 — 바로 아래 청산선이 같은 값을 **가격까지 붙여**
            #  적는다. 두 번 적으면 한쪽만 고쳐질 때 기록끼리 어긋난다.
            if atr_msg:
                reason += f" {atr_msg}"

            # [기록] 진입 시점의 청산선을 함께 남긴다 — %만으로는 나중에 그 선이 어디였는지
            #  역산해야 하고, 청산이 끝난 종목은 화면 어디에도 그 값이 남지 않는다.
            #  TS 는 아직 무장 전이라 '언제 켜지고(발동가) 그때 어디서 잘리나'를 계산해 둔다.
            levels = _pkg().format_exit_levels(
                order_price, sl_rate=sl_rate,
                label=("ATR" if use_atr_stop else "고정"),
                atr=(atr_val if atr_val > 0 else None),
                is_usd=utils.is_usd_quoted(cand['code']))
            if levels:
                reason += f" {levels}"

            # [Fix] 신규 포지션 매수 전 이전 보유분의 잔존 상태 초기화.
            #  외부(MTS/HTS) 전량 매도는 엔진 매도 경로를 거치지 않아 트레일링 최고가·반익절
            #  DB 기록이 남는데(최고가 UPSERT는 단조 증가), 재시작 후 재매수 시 잔존 최고가로
            #  max_profit이 과대 계산되어 매수 직후 BEP/TS가 오발동(신규 포지션 즉시 청산)하는
            #  것을 방지한다. (후보군은 보유 종목을 제외하므로 이 시점은 항상 신규 포지션)
            # [히트 캡 선점] 주문을 내기 **전에** 예산을 잡는다. 종전에는 주문 성공 뒤에
            #  더했는데, 그 사이(네트워크 왕복)에 매도 워커의 피라미딩이 같은 예산을 보고
            #  자기 몫을 잡을 수 있어 합계가 캡을 넘었다. 피라미딩 경로는 이미 확인과
            #  선점을 락으로 원자화하고 실패 시 반납한다 — 같은 규약으로 맞춘다.
            #  [원자성] 위쪽 예산 확인은 배분액 기준이고 락 밖이다. 확인과 선점 사이에
            #   다른 경로(피라미딩)가 예산을 잡으면 합계가 캡을 넘으므로, **확정 수량으로
            #   다시 확인하고 같은 락 안에서 선점**한다. 경합이 없으면 이 재확인은 무동작이다
            #   (여기 도달한 시점의 qty×주문가는 위에서 통과한 배분액 이하다).
            new_risk_amt = (qty * order_price) * (abs(sl_rate) / 100.0) if (sl_rate and sl_rate < 0) else 0.0
            if new_risk_amt > 0:
                with self._lock:
                    budget_now = self.risk_manager.portfolio_risk_budget_left(avail_cash=avail_cash)
                    if budget_now is not None and new_risk_amt > budget_now:
                        self.log(f"매수 보류: {cand['name']} - 포트폴리오 총 리스크 예산이 "
                                 f"주문 직전에 소진됨 (필요 {new_risk_amt:,.0f}원 > 남은 "
                                 f"{max(budget_now, 0):,.0f}원)")
                        continue
                    self.portfolio_heat_amt += new_risk_amt

            #  [Fix 2026-09-06] 옛 앵커를 **지우지 못했으면 이 매수를 내지 않는다.**
            #   이 삭제가 매수 직전에 도는 이유는 하나다 — 새 포지션이 이전 포지션의
            #   고점을 물려받지 않게 하는 것. 실패해도 그 세션은 멀쩡해 보이고(아래에서
            #   메모리 캐시를 pop 하므로) **재기동해야** 드러난다: DB 의 옛 고점이 실려
            #   와 샹들리에 TS 가 '고점 대비 폭락'으로 읽고 즉시 시장가 청산을 때린다.
            #   진입을 한 주기 미루는 것은 되돌릴 수 있고, 그 청산은 되돌릴 수 없다.
            if not db_manager.db.delete_trailing_stop(cand['code']):
                self.log(f"매수 보류: {cand['name']} - 이전 포지션의 트레일링 고점을 "
                         f"지우지 못했습니다(재기동 시 그 고점을 물려받아 즉시 청산될 수 "
                         f"있습니다). 다음 주기에 다시 시도합니다.")
                if new_risk_amt > 0:
                    with self._lock:
                        self.portfolio_heat_amt -= new_risk_amt   # 선점분 반납
                continue
            if not db_manager.db.delete_half_tp(cand['code']):
                #  이쪽은 방향이 다르다 — 남아 있으면 '이미 반익절함'으로 읽혀 반익절·
                #  익절 분기를 거를 뿐, 없는 매도를 만들지는 않는다. 진입은 진행하고
                #  사실만 남긴다.
                self.log(f"[반익절] {cand['name']} 이전 기록을 지우지 못했습니다 — "
                         f"재기동하면 이 포지션이 '이미 반익절함'으로 읽힐 수 있습니다.")
            with self._lock:
                self.trailing_stop_cache.pop(cand['code'], None)

            self.log(f"매수 실행: {cand['name']} - {reason}")

            # [수정] 매수 시 사유와 점수, 그리고 지정가 가격을 DB 저장을 위해 전달
            try:
                odno = self.order_manager.send_order(cand['code'], qty, "buy", name=cand['name'], reason=reason, score=cand['score'], price=order_price, rule=cand.get('rule'), stop_loss_rate=sl_rate)
            except _pkg().OrderOutcomeUnclear as e:
                #  [Fix 2026-09-08] 접수 여부를 모른다 — **선점분을 반납하지 않는다.**
                #   반납하면 살아 있을지 모르는 포지션의 리스크가 장부에서 사라지고,
                #   바로 아래 후보가 같은 예산을 다시 쓴다. 히트 캡은 브로커가 막아 주지
                #   않는 우리 쪽 장부라 그것을 막을 것이 없다. 남겨 두면 최악의 경우
                #   이번 주기에 한 자리를 덜 쓸 뿐이고, 다음 주기 잔고 조회가 바로잡는다.
                #   그리고 이 주기의 매수를 여기서 멈춘다 — 장부가 확실하지 않은 채로
                #   계속 사는 것은 캡을 세운 이유를 지운다.
                self.log(f"매수 중단: {cand['name']} - 주문 접수 여부를 확인하지 못했습니다 "
                         f"({e}). 선점한 리스크 예산은 남겨 두고 이번 주기의 매수를 멈춥니다.")
                break
            if not odno and new_risk_amt > 0:
                with self._lock:
                    self.portfolio_heat_amt -= new_risk_amt   # 주문 실패 시 선점분 반납
            if odno:
                # [추가] 매수 주문 성공 시 대기 중인 예약 매수 취소 방어 로직 (중복 진입 방지)
                target_cano = config.session.auto_cano
                target_acnt = config.session.auto_acnt_prdt_cd
                canceled_cnt = db_manager.db.cancel_reserved_buy_orders(target_cano, target_acnt, cand['code'])
                if canceled_cnt is None:
                    #  실패를 0('취소할 것이 없었다')으로 읽으면, 남아 있는 예약 매수가
                    #  나중에 그대로 발동해 같은 종목에 두 번째 포지션이 생긴다.
                    self.log(f"[예약취소] {cand['name']} 대기 중인 매수 예약을 취소하지 "
                             f"못했습니다 — 그 예약이 발동하면 같은 종목에 두 번째 "
                             f"진입이 나갑니다. 확인해 주세요.")
                    api.send_telegram_message(
                        f"⚠️ [예약 취소 실패] {cand['name']}({cand['code']})\n"
                        f"신규 매수 후 대기 중인 매수 예약을 취소하지 못했습니다.\n"
                        f"그 예약이 발동하면 같은 종목에 두 번째 진입이 나갑니다 — "
                        f"메뉴에서 확인해 주세요.")
                elif canceled_cnt > 0:
                    self.log(f"[예약취소] 신규 매수로 인해 대기 중이던 {cand['name']} 매수 예약 주문 {canceled_cnt}건 자동 취소")
                    api.send_telegram_message(f"🗑 [예약 취소] {cand['name']}({cand['code']}) 신규 매수로 인해 대기 중이던 매수 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.")
                
                self.half_tp_cache.discard(cand['code']) # 신규 매수 시 기존 반익절 캐시 방어적 초기화
                avail_cash -= (qty * order_price)
                current_holdings_count += 1 # [추가] 보유 종목 수 증가 반영
                # (히트 캡 선점은 주문 전에 끝냈다 — 위 new_risk_amt 참조)
                record = {
                    "type": "buy",
                    "code": cand['code'],
                    "name": cand['name'],
                    "qty": qty,
                    "price": order_price,
                    "reason": reason,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "odno": odno,
                    "stop_loss_rate": sl_rate # [추가] 계산된 손절률 기록 (매도 시 참조 가능하도록)
                }
                self.trade_records.append(record)
