"""AutoTrader 의 매도 — 주기마다의 매도 판정(손절·트레일링·시간청산·추세이탈·반익절)과 그 안전망.

판정 루프(_check_sell_conditions)와, 그 루프가 부르는 것들이다:
  · 시스템이 청산하지 못하는 포지션(트레이딩 제한·ETF 제외·시세 이상·판정 오류·매도가능수량 0·대기 주문에
    묶임)의 손절선 이탈 경보와, 미체결 매수가 걸린 채 손절선을 넘은 포지션의 매수 취소(손절 보호)
  · 트레일링 앵커(기록·봉 고가 복원)·반익절 이력·권리 조정(액면분할·무상증자) 보정
  · 장 마감 뒤 청산 신호 점검과 알림(주문 없음)
  · 오픈 리스크 산출에 넘길 직전 주기 실측 손절선

[왜 나눴나 · 2026-10-25 C1 4단계] 매도는 이 시스템의 주청산(트레일링 스탑)과 손절이 도는 자리이고, 판정 루프
 하나가 1,000줄을 넘는다. 매수·주기 제어와 한 파일에 섞여 있으면 청산 규칙 하나를 고칠 때 매매 파일 전체를
 함께 읽어야 하고, 매도 변경이 매수 변경과 구분되지 않는다.

[어떻게 나눴나] 1~3단계(reporting·market_risk·account_sync)와 같은 규약이다. 메서드 본문은 한 글자도 바꾸지
 않고 옮겼고, AutoTrader 가 이 믹스인을 상속한다 — self._check_sell_conditions() · patch.object(AutoTrader, ...)
 가 전과 같이 동작한다. 모듈 전역 이름(config·api·db_manager·datetime·time …)은 trader.py 와 **같은 객체**를
 가리키게 import 했다(tests/test_trader_split_parity.py 가 이름마다 대조한다). 로거도 trader 와 같은 이름을 쓴다.
 미관리 사유 문구·경보 횟수·앵커 복원 간격은 engine 에 있는 정본을 그대로 import 한다. 매수와 함께 쓰는
 지정가 제한폭 보정(_clamp_order_price)과 주기 제어가 부르는 마감 자산 스냅샷은 trader 에 남겼다.
 동등성 기준은 주기 재생 기록 네 시나리오(tests/test_cycle_replay.py — base·market_stress·account_sync·
 sell_paths)다.

[주의] 테스트에서 이 메서드들이 쓰는 전역을 바꿔 끼울 때는 trader 가 아니라 이 모듈을 지정한다
 (예: modules.auto_trade.selling.get_restricted_stocks). 패키지 네임스페이스 경유 호출은 _pkg() 그대로다.
"""
import concurrent.futures
import logging
import time
from datetime import datetime
from typing import Any, Callable

import api
import config
from core import indicators
from core import trading_cost
from core import utils
from modules import analysis
from modules import db_manager
from modules.auto_trade.common import _enrich_rules_with_weights, _get_trade_account, get_restricted_stocks
from modules.auto_trade.engine import (ANCHOR_RESTORE_INTERVAL_SEC, NO_SELLABLE_ALERT_CYCLES,
                                       STUCK_PENDING_ALERT_CYCLES, UNMANAGED_ANALYSIS_ERROR,
                                       UNMANAGED_BAD_PRICE, UNMANAGED_ETF, UNMANAGED_NO_SELLABLE,
                                       UNMANAGED_RESTRICTED, UNMANAGED_STALE_PRICE, UNMANAGED_STUCK_PENDING)

#  트레이더 로그와 같은 이름 — 매도 코드가 남기는 로그도 'modules.auto_trade.trader' 로 모인다.
logger = logging.getLogger("modules.auto_trade.trader")


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자 — trader._pkg 와 같은 규약.

    테스트의 patch('modules.auto_trade.X') 가 이 모듈 안의 호출에도 걸리게 한다. (지연 import 라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class TraderSellingMixin:
    """AutoTrader 의 매도 메서드. 단독으로 쓰지 않는다 — 트레이더 상태(self.order_manager·self.strategy …)를 쓴다."""

    #  이 믹스인이 기대는 트레이더 상태·메서드(정의·초기값은 AutoTrader). 값 없는 선언이라 실행 시에는
    #  AutoTrader 의 것이 그대로 쓰인다. getattr 기본값으로 읽는 상태와 이 믹스인이 직접 쓰는 상태는 빠져 있다.
    _lock: Any
    is_running: bool
    order_manager: Any
    risk_manager: Any
    strategy: Any
    trade_records: list
    trailing_stop_cache: dict
    unmanaged_stop_notified: dict
    after_hours_sell_notified: dict
    no_sellable_streak: dict
    stuck_pending_streak: dict
    _anchor_restore_at: dict
    log: Callable[..., None]
    set_stock_state: Callable[..., None]
    _clamp_order_price: Callable[..., Any]
    _get_stock_market_type: Callable[..., Any]
    _trade_account_key: Callable[..., str]
    _try_pyramid_buy: Callable[..., Any]

    def _live_risk_map(self, hold_codes):
        """오픈 리스크 산출에 넘길 '직전 주기 실측 손절선' 스냅샷 (보유분만).

        [왜 거르는가] 이 캐시는 매도 판정이 종목마다 채우고 스스로 지우지 않는다.
        판 종목의 값이 남아 있어도 히트는 잔고를 도니 곧바로 틀리진 않지만, 같은 종목을
        다시 담았을 때 **예전 포지션의 손절선**이 새 포지션의 리스크로 계상된다.
        보유분만 남기면 그 경로가 닫힌다(재진입 시엔 값이 없어 종전 근사로 돌아간다).
        """
        codes = set(hold_codes or ())
        with self._lock:
            self.holding_risk_cache = {k: v for k, v in self.holding_risk_cache.items()
                                       if k in codes}
            return dict(self.holding_risk_cache)

    def _last_known_sl_rate(self, code):
        """직전 주기의 매도 판정이 **실제로 쓴** 손절률(%). 없으면 None.

        [왜 필요한가] 안전망 두 곳(미체결 매수 취소 · 미관리 포지션 이탈 경보)은 주기
        앞머리에서 도느라 일봉이 없다. 그래서 매수 기록이 없거나 못 읽으면 전역 고정
        손절률로 떨어지는데, 매도 엔진은 같은 상황에서 진입 봉 ATR 로 복원한 값을 쓴다
        (entry_atr_stop_rate). 두 선이 갈리면 경보와 취소가 **조용히 늦는다** — 실측으로
        엔진이 -3.2% 를 보는 동안 안전망은 -7.0% 를 봤다. 손절선을 이미 지났는데
        마지막 안전망이 울지 않는 상태다.

        holding_risk_cache 는 직전 주기가 그 종목에 실제로 적용한 선이고(60초 전의 일봉
        ATR 이라 실질 차이가 없다) 메모리에 이미 있어 조회 비용이 0이다. 없으면 None 을
        돌려줘 종전 경로로 돌아간다 — 모르면 안 건드린다.
        """
        with self._lock:
            entry = (self.holding_risk_cache or {}).get(code) or {}
        try:
            rate = float(entry.get('sl_rate') or 0.0)
        except (TypeError, ValueError):
            return None
        return rate if rate < 0 else None

    def _effective_stop_loss_rate(self, buy_trades=None, rule=None, fallback_atr_rate=None):
        """포지션의 실효 손절률(%) — 매도 판정이 실제로 쓰는 그 값. 미사용(0)이면 None.

        [SSOT] 산식은 build_sell_thresholds가 단독 보유한다. 종전에는 이 자리와
         _alert_unmanaged_stop이 '수량가중평균'을 각자 복제해 **세 벌**이 돌았고, 세 벌이
         서로 달랐다 — 복제본은 ATR 손절 사용 여부도, 개별 룰의 손절 조이기도, 매수 기록이
         없는 포지션의 ATR 복원(fallback)도 보지 않는다. 그래서 '손절 보호'(미체결 매수
         취소)와 '미관리 포지션 이탈 경보'가 정작 매도 엔진과 다른 선을 보고 판단했다.
         경보·취소는 손절이 안 도는 포지션의 마지막 안전망이라, 그 선이 실제 판정과
         어긋나면 조용히 늦거나 조용히 빨라진다.

        fallback_atr_rate: 매수 기록이 없을 때 쓸 복원값(일봉이 있는 호출부만 넘긴다).
        """
        thresholds = _pkg().build_sell_thresholds(
            rule=rule, buy_trades=buy_trades, fallback_atr_rate=fallback_atr_rate)
        try:
            sl_rate = float(thresholds.get("STOP_LOSS_RATE",
                                           config.SELL_STRATEGY.get("STOP_LOSS_RATE", -7.0)))
        except (TypeError, ValueError):
            return None
        return sl_rate if sl_rate < 0 else None

    def _cancel_pending_buy_on_stop_loss(self, code, name, item, buy_trades=None, rule=None):
        """미체결 '매수'가 걸린 채 체결분이 손절선을 이탈하면 그 매수를 즉시 취소한다.

        [왜 취소가 먼저인가] 매수 주문을 열어 둔 채 같은 종목을 팔면 서로 싸운다 —
        손절로 턴 물량을 남은 매수가 다시 담을 수 있다. 그래서 이번 주기에는 매수만
        거둬들이고, 주문이 종결되어 pending이 풀린 다음 주기에 정상 청산 경로를 태운다.
        자동 취소 타임아웃(UNFILLED_ORDER_CANCEL_SECONDS)을 기다리지 않는 것이 요점이다.

        [매도 주문은 건드리지 않는다] 취소 대상은 '매수'뿐이다. 청산 주문을 취소하면
        포지션이 그대로 남아 손절이 되레 무산된다. 매수·매도가 함께 걸려 있으면 매수만
        거둔다(청산 중인 종목에 추가로 담는 것을 막는 쪽이 항상 옳다).
        """
        try:
            #  [0% 는 '모름'이 아니다 · 2026-09-06] 종전에는 `float(... or 0.0)` 이었다.
            #   0% 는 손절선(음수)보다 위라 이 보호가 통째로 건너뛰어진다 — 손절 중인
            #   종목에 미체결 매수가 열린 채 남는다. 판정은 holding_profit_rate 하나가
            #   갖는다(평단·현재가로 복원하고, 그것도 안 되면 모른다고 답한다).
            profit_rate = _pkg().holding_profit_rate(item)
            if profit_rate is None:
                logger.warning(f"[손절 보호] {code} 수익률을 구할 수 없어 판정을 보류한다")
                return
            sl_rate = self._effective_stop_loss_rate(
                buy_trades, rule=rule, fallback_atr_rate=self._last_known_sl_rate(code))
            if sl_rate is None or profit_rate > sl_rate:
                return

            with self.order_manager._lock:
                odnos = list((self.order_manager.pending_orders.get(code) or {}).keys())
            if not odnos:
                return

            buy_odnos = []
            unknown_odnos = []
            for odno in odnos:
                t_type = ""
                try:
                    #  미체결 매수는 당일 주문이다. 날짜로 좁히지 않으면 옛 '매도' 행이
                    #  잡혀 이 매수가 취소 대상에서 빠진다 — 손절 중인 종목에 매수가
                    #  열린 채 남는다. 못 찾으면 아래 unknown 분기가 보수적으로 처리한다.
                    tr = db_manager.db.get_trade_by_odno(odno, on_date=utils.odno_scope_date())
                    t_type = str((tr or {}).get('type', ''))
                except Exception as e:
                    #  [모름은 말한다 · 2026-09-05] 매수/매도는 여기서만 갈린다
                    #  (pending_orders 는 상태만 들고 side 를 모른다). 조회가 깨지면
                    #  이 주문은 매수여도 취소 대상에서 빠지고, 손절 중인 종목에 매수가
                    #  그대로 열려 있게 된다. 취소를 강행하지는 않는다 — 매도를 잘못
                    #  취소하면 청산 자체가 무산된다 — 대신 **조용히 넘어가지 않는다**.
                    logger.warning(f"[손절 보호] {code} 주문 {odno} 종류 조회 실패: {e}")
                    unknown_odnos.append(odno)
                    continue
                is_sell = "매도" in t_type or "sell" in t_type.lower()
                if not is_sell and ("매수" in t_type or "buy" in t_type.lower()):
                    buy_odnos.append(odno)

            if unknown_odnos:
                self.log(f"[손절 보호] {name}({code}) 미체결 주문 {len(unknown_odnos)}건의 "
                         f"매수/매도 구분을 확인하지 못해 취소하지 않았습니다 "
                         f"(No.{', '.join(str(o) for o in unknown_odnos)}) — "
                         f"손절 중인 종목에 매수가 열려 있을 수 있습니다")

            for odno in buy_odnos:
                # qty=0 은 '잔량 전부 취소'(QTY_ALL_ORD_YN=Y) — 잔량을 몰라도 안전하다.
                res = api.revise_cancel_order("domestic", "cancel", odno, code, 0, "0", "02", "00")
                if isinstance(res, dict) and res.get('rt_cd') == '0':
                    self.log(f"[손절 보호] {name}({code}) 수익률 {profit_rate:.2f}% ≤ 손절 {sl_rate:.2f}% "
                             f"→ 미체결 매수(No.{odno})를 즉시 취소했습니다. 다음 주기에 청산합니다.")
                    api.send_telegram_message(
                        f"🛑 [손절 보호] {name}({code})\n"
                        f"수익률 {profit_rate:.2f}% (손절 기준 {sl_rate:.2f}%)\n"
                        f"미체결 매수를 취소해 체결분의 청산 경로를 확보했습니다.")
                else:
                    msg = (res or {}).get('msg1') if isinstance(res, dict) else res
                    self.log(f"[손절 보호] {name}({code}) 미체결 매수 취소 실패: {msg}")
        except Exception as e:
            logger.debug(f"[손절 보호] 미체결 매수 취소 처리 실패({code}): {e}")

    def _apply_corporate_action(self, code, name, item, buy_price, highest_price):
        """[안전장치] 액면분할·무상증자를 감지해 트레일링 최고가를 같은 배율로 보정한다.

        판정은 engine.detect_corporate_action이 단독 보유한다(순수 함수). 여기서는 잔고
        한 줄에서 평단·매입금액을 꺼내 직전 주기 값과 비교하고, 결과를 DB에 반영할 뿐이다.

        보정하지 않으면 5:1 분할에서 고점만 분할 전 값으로 남아 트레일링 스탑이 즉시
        발동하고 시장가로 강제 청산된다. 백테스트 데이터는 수정주가라 이 경로를 재현하지
        못하므로, 실계좌 투입 전에 코드로 막아둔다.

        반환: 보정된 최고가(보정이 없으면 받은 값 그대로).
        """
        try:
            qty = api.safe_int(item.get('hldg_qty'))
            if qty <= 0 or buy_price <= 0:
                return highest_price
            # 매입금액은 실전 잔고(INQR_DVSN=01)·토스 어댑터에서 0/누락으로 오므로
            #  같은 정의(수량 × 평단)로 복원한다 — 잔고 표시부(_print_holdings)와 동일 규칙.
            pchs_amt = api.safe_int(item.get('pchs_amt')) or int(qty * buy_price)

            #  [Fix 2026-09-06] 조회 실패는 '기준 없음'이 아니다. 종전에는 (0,0)이 와서
            #   배율 1.0 으로 흘렀고, 함수 끝에서 기준값이 **조정 후 값으로 덮였다** —
            #   그 뒤로는 분할을 영영 감지하지 못한다. 모르면 이번 주기를 통째로 건너뛴다
            #   (아래 update_position_ref 도 실행되지 않는다).
            try:
                ref_avg, ref_amt = db_manager.db.get_position_ref(code)
            except Exception as _re:
                logger.warning(f"[권리 조정] {code} 기준값을 읽지 못해 이번 주기 판정을 "
                               f"건너뜁니다(기준값도 옮기지 않습니다): {_re}")
                return highest_price
            ratio, reason = _pkg().detect_corporate_action(ref_avg, ref_amt, buy_price, pchs_amt)

            rescale_failed = False
            if ratio != 1.0:
                lines = [f"🔀 [권리 조정] {name}({code})", reason]

                #  ① 트레일링 최고가는 같은 배율로 환산한다 — 시스템이 만든 값이고,
                #     환산하면 조정 전과 정확히 같은 판정이 나오기 때문이다.
                new_high = db_manager.db.rescale_highest_price(code, ratio) if highest_price > 0 else None
                #  [Fix 2026-09-05] 환산 실패를 조용히 넘기면 안 된다. 실패하면 앵커가
                #   **분할 전 값**으로 남는다 — 5:1 분할이면 청산선이 현재가의 4배쯤에
                #   서므로 다음 주기에 트레일링이 즉시 발동해 시장가로 강제 청산된다.
                #   이 함수가 존재하는 이유가 바로 그것을 막는 것이다.
                rescale_failed = (highest_price > 0 and not new_high)
                if new_high:
                    with self._lock:
                        self.trailing_stop_cache[code] = new_high
                    highest_price = new_high
                    lines.append(f"· 트레일링 최고가 {ratio:.4f}배 보정 (→ {new_high:,.0f}원)")
                elif rescale_failed:
                    lines.append(f"· ⚠️ 트레일링 최고가 환산 실패 — 앵커가 조정 전 값({highest_price:,.0f}원)"
                                 f"으로 남아 있습니다. 다음 주기에 다시 시도합니다.")
                    logger.error(f"[권리 조정] {code} 최고가 환산 실패(ratio={ratio:.4f}, "
                                 f"anchor={highest_price}) — 기준값을 옮기지 않고 재시도합니다.")

                #  ② 예약 주문은 환산하지 않고 **취소**한다 — 목표가는 운영자가 조정 전
                #     가격을 보고 직접 정한 값이라, 기계적으로 환산해도 의도한 자리가
                #     아니다. 그대로 두면 목표가는 이미 도달한 것처럼, 추적 극값은 폭락한
                #     것처럼 보여 어느 쪽이든 즉시 오발동한다.
                canceled = db_manager.db.cancel_reserved_orders_on_corp_action(
                    code, f"권리 조정 감지({reason})로 자동 취소")
                if canceled is None:
                    #  실패는 '취소할 것이 없었다'와 다르다 — 조정 전 가격의 예약이
                    #  남으면 곧바로 오발동한다.
                    lines.append("· ⚠️ 예약 주문을 취소하지 못했습니다 — 조정 전 가격의 "
                                 "예약이 남아 곧 오발동할 수 있습니다. 직접 취소해 주세요.")
                    logger.error(f"[권리 조정] {code} 예약 취소 실패 — 조정 전 가격의 "
                                 f"예약이 남아 있습니다")
                    canceled = []
                for o in canceled:
                    lines.append(f"· 예약 {'매수' if o.get('order_type') == 'buy' else '매도'} 취소: "
                                 f"{o.get('condition_type')} "
                                 f"{float(o.get('target_price') or 0):,.0f} / {o.get('qty')}주")
                    db_manager.db.insert_trade(
                        f"{'매수' if o.get('order_type') == 'buy' else '매도'}취소(예약)",
                        code, name, o.get('qty'), o.get('order_price', 0),
                        f"RES_CORP_{o.get('id')}", order_status="취소",
                        reason=f"권리 조정({reason})으로 예약 자동 취소")
                if canceled:
                    lines.append("→ 조정 후 가격 기준으로 다시 설정해 주세요.")

                self.log(f"[권리 조정] {name}({code}) {reason} · 배율 {ratio:.4f} "
                         f"· 예약 취소 {len(canceled)}건")
                logger.warning(f"[권리 조정] {code} {reason} ratio={ratio:.4f} "
                               f"high={new_high} canceled={len(canceled)}")
                #  취소는 되돌릴 수 없고 본문이 "조정 후 가격 기준으로 다시 설정해 주세요"라고
                #  사람의 조치를 요구한다. 못 닿았으면 그 사실 자체를 남긴다.
                if not _pkg().alert_delivered("\n".join(lines)):
                    logger.error(f"[권리 조정] 알림을 전달하지 못했습니다 — {name}({code}) "
                                 f"예약 {len(canceled)}건이 취소된 사실이 운영자에게 닿지 않았습니다.")

            # 배율이 1이어도 기준값은 항상 최신으로 옮겨야 다음 주기 비교가 성립한다.
            #  [Fix 2026-09-05] **다만 환산에 실패했으면 옮기지 않는다.** 기준값을 조정 후
            #   값으로 옮기면 다음 주기의 detect_corporate_action 이 배율 1.0 을 보고
            #   분할을 **다시는 감지하지 못한다** — 앵커는 조정 전 값으로 영구히 남고,
            #   그 종목은 트레일링 즉시 발동으로 강제 청산된다. 옮기지 않으면 다음 주기가
            #   같은 배율을 다시 감지해 환산을 재시도한다(알림이 한 번 더 나가는 대가).
            if rescale_failed:
                pass
            elif (ref_avg, ref_amt) != (float(buy_price), float(pchs_amt)):
                db_manager.db.update_position_ref(code, buy_price, pchs_amt)
        except Exception as e:
            # 보정 실패가 매도 분석 자체를 막아서는 안 된다(원래 값으로 계속 진행).
            logger.debug(f"[권리 조정] 판정 실패({code}): {e}")
        return highest_price

    def _already_half_sold(self, code):
        """이 종목이 이미 반익절됐는가. **모르면 '했다'로 답한다.**

        [왜 그 방향인가 · 2026-09-06] 틀리는 두 방향의 대가가 다르다.
          · '안 했다'로 틀리면 → 이미 반쪽 판 포지션을 **또 판다**(실제 돈이 나간다)
          · '했다'로 틀리면   → 이번 반익절을 걸렀을 뿐, 청산 체인은 그대로 흐른다
            (트레일링 스탑이 주청산이고, 반익절은 기본 OFF 인 보조 수단이다)
        캐시가 적재되지 않았으면 여기서 한 번 더 읽는다 — 기동 때의 DB 실패가 세션
        내내 굳지 않게 한다([[unknown-vs-empty]] · '실패를 캐시에 굳힌다').
        """
        if not getattr(self, 'half_tp_cache_loaded', False):
            try:
                self.half_tp_cache = db_manager.db.get_all_half_tp()
                self.half_tp_cache_loaded = True
                self.log("[반익절] 이력을 다시 읽었습니다.")
            except Exception as e:
                logger.warning(f"[반익절] {code} 이력을 읽지 못해 '이미 반익절함'으로 "
                               f"다룹니다(중복 매도 방지): {e}")
                return True
        return code in self.half_tp_cache

    def _cached_anchor(self, code):
        """기록된 트레일링 앵커(최고가). 캐시에 없으면 DB에서 읽어 채운다. 없으면 0.0.

        [Fix 2026-09-06] **조회에 실패했으면 캐시에 넣지 않는다.** 종전에는 실패도
         '기록 없음'과 같은 None 으로 와서 0.0 이 세션 캐시에 박혔고, 그 뒤로는 DB 가
         회복돼도 다시 읽지 않았다(캐시에 값이 있으므로). 앵커 0 은 트레일링 스탑 판정을
         통째로 건너뛰게 한다 — 주청산 수단이 그 종목에서 조용히 꺼진다.
         이번 호출만 0.0 으로 다루고, 다음 호출이 다시 읽는다.
        """
        with self._lock:
            cached = self.trailing_stop_cache.get(code)
            if cached is not None:
                return cached
        try:
            val = db_manager.db.get_highest_price(code)
        except Exception as e:
            logger.warning(f"[TrailingStop] {code} 앵커 조회 실패 — 이번 주기만 0 으로 "
                           f"다루고 캐시하지 않습니다(다음 주기에 다시 읽습니다): {e}")
            return 0.0
        cached = val if val is not None else 0.0
        with self._lock:
            self.trailing_stop_cache[code] = cached
        return cached

    def _restore_trailing_anchor(self, code, name, entry_date, highest_price=None,
                                 df=None, is_overseas=False):
        """진입일 이후 봉 고가가 기록된 앵커보다 높으면 그것으로 올린다. 확정된 앵커를 돌려준다.

        [왜 필요한가 · 2026-08-24] 주기마다의 갱신은 **봇이 보고 있는 동안의 현재가**만
        쌓으므로 다음 구간이 통째로 비어 있었다:
          · HTS·앱에서 직접 산 포지션 — 봇이 처음 본 날의 현재가부터 시작한다
          · 재기동·정지 구간의 고점 — 그 사이 고가가 앵커에 남지 않는다
          · 매수 주문이 심는 앵커 — 보유 중인 종목에 1주만 더 담아도 그 체결가가 앵커로
            기록되고(update_highest_price는 단조라 내리진 않지만, 기록이 없던 종목엔
            매수가가 그대로 앵커가 된다) 실제 고점이 사라진다
        백테스트는 진입 봉 고가에서 시작해 봉 고가의 러닝맥스를 쓴다
        (portfolio_backtest: pos["high"] = max(pos["high"], row["high"])). 즉 이 복원은
        실매매를 백테스트의 정의 쪽으로 되돌리는 것이다.

        [왜 매도 분석 밖에서도 부르나] 자동 매도에서 제외된 ETF는 판정 루프에 닿기 전에
        빠져나가, 저 세 구멍을 메울 기회가 영영 없었다. 앵커를 쓰는 쪽(메뉴 9-2 보유 분석)은
        매번 봉에서 되짚으므로 화면은 맞지만 DB에는 매수가가 그대로 남는다. **쓰는 주체는
        트레이더 하나로 묶는다** — 표시 경로에서 쓰면 메뉴 9-5의 가상 포지션(사용자가 입력한
        임의의 매수일)과 다른 계좌의 잔고가 같은 테이블(code가 PK)로 흘러들고, 단조 갱신이라
        한 번 높게 오염되면 되돌릴 수 없다(청산선이 올라가 오청산이 나간다).

        df 를 주지 않으면 여기서 조회한다(ETF 경로). 이때는 주기마다 차트를 새로 집는 셈이라
        종목당 1시간에 한 번으로 묶는다 — 앵커는 일봉 고가에서 나오므로 그보다 잦게 볼 이유가
        없고, 라즈베리파이에서 캐시 만료(6시간)와 겹치면 종목마다 KIS 호출이 붙는다.
        highest_price 를 주지 않으면 기록된 앵커를 직접 읽는다(스로틀에 걸리면 읽지도 않는다).
        실패하면 앵커를 건드리지 않는다.
        """
        if df is None:
            now = time.time()
            if now - self._anchor_restore_at.get(code, 0.0) < ANCHOR_RESTORE_INTERVAL_SEC:
                return highest_price
            self._anchor_restore_at[code] = now
        if highest_price is None:
            highest_price = self._cached_anchor(code)
        if not entry_date:
            return highest_price
        try:
            if df is None:
                df = api.get_chart_data(code, is_overseas=is_overseas)
            derived_high = _pkg().anchor_high_since(code, df, entry_date, is_overseas=is_overseas)
        except Exception as e:
            logger.debug(f"[TrailingStop] {code} 앵커 복원 실패: {e}")
            return highest_price

        if not derived_high or derived_high <= highest_price:
            return highest_price

        db_manager.db.update_highest_price(code, derived_high)
        with self._lock:
            self.trailing_stop_cache[code] = derived_high
        self.log(f"[TrailingStop] {name}({code}) 앵커 복원: "
                 f"{highest_price:,.0f} → {derived_high:,.0f} "
                 f"(진입일 {entry_date} 이후 봉 고가)")
        return derived_high

    def _alert_unmanaged_stop(self, code, name, item, kind, buy_trades=None, rule=None):
        """[안전장치] 자동 매도 대상에서 제외된 보유 포지션의 손절선 이탈 경보

        [추세추종 원칙] "탈출 전략이 없다면 포지션을 잡지 마라."
        트레이딩 제한 종목(수동 홀딩)과 ETF(SYSTEM_INCLUDE_ETF=False)는 의도적으로 매도 분석에서
        제외되므로 시스템이 손절하지 않는다. 자동 청산까지 하면 '수동 관리' 의도를 깨므로,
        대신 손절선 이탈 사실을 알려 사용자가 직접 판단할 수 있게 한다.

        손절선은 매수 기록에 저장된 실제 손절률(수량가중평균)을 쓰고, 없으면 전역 STOP_LOSS_RATE.
        같은 종목의 반복 알림은 24시간 스로틀하며, 손절선 위로 회복하면 스로틀을 풀어
        재이탈 시 다시 알린다.
        """
        try:
            # [중요] 평가손익률이 없을 때 0으로 두면 안 된다. 0은 손절선(음수) 위라서
            #  아래 분기가 '회복했다'로 읽고 경보를 건너뛰는 것은 물론 스로틀까지 푼다.
            #  이 경보는 시스템이 손절해 주지 않는 포지션의 **마지막 안전망**인데, 잔고
            #  데이터가 부실할수록 조용해지는 구조였다(토스 어댑터는 일부 필드가 0/누락으로
            #  온다 — 같은 이유로 pchs_amt는 이미 수량×평단으로 복원하고 있다).
            #  없으면 평단과 현재가로 직접 구하고, 그것도 안 되면 판단을 미룬다.
            profit_rate = _pkg().holding_profit_rate(item)
            if profit_rate is None:
                logger.warning(f"[미관리 경보] {name}({code}) 평가손익률을 구할 수 없어 판정을 보류한다 "
                               f"— 스로틀은 건드리지 않는다")
                return

            # [SSOT] 손절선은 매도 엔진이 쓰는 그 값이어야 한다(_effective_stop_loss_rate).
            #  종전에는 여기에 수량가중평균을 한 벌 더 복제해 두어, ATR 손절 OFF·개별 룰
            #  조이기 같은 조건에서 실제 판정과 다른 선을 보고 경보했다.
            #  기록이 없거나 못 읽었으면 직전 주기가 실제로 쓴 선을 이어받는다.
            #  기록이 있으면 그것이 항상 우선한다(build_sell_thresholds 의 우선순위).
            sl_rate = self._effective_stop_loss_rate(
                buy_trades, rule=rule, fallback_atr_rate=self._last_known_sl_rate(code))
            if sl_rate is None:
                return  # 손절 기준 자체가 없으면(0=미사용) 경보할 기준도 없다

            if profit_rate > sl_rate:
                # 손절선 위로 회복 — 다음 이탈 때 다시 알리도록 스로틀 해제
                self.unmanaged_stop_notified.pop(code, None)
                return

            #  [전달 확인 뒤에 찍는다] 스로틀 값은 '보낸 시각'이 아니라 **다음 알림 가능
            #   시각**이다. 종전에는 보내기 전에 찍었는데, send_telegram_message 는 기본이
            #   비동기라 실패해도 예외가 없어 네트워크가 끊긴 채로 '보냈다'가 굳었다. 이
            #   경보는 위 독스트링대로 '시스템이 손절해 주지 않는 포지션의 마지막 안전망'
            #   이라, 한 번 놓치면 손절선 아래에서 24시간 침묵한다.
            now = time.time()
            if now < self.unmanaged_stop_notified.get(code, 0):
                return

            qty = api.safe_int(item.get('hldg_qty', 0))
            eval_amt = api.safe_int(item.get('evlu_amt', 0))
            loss_amt = api.safe_int(item.get('evlu_pfls_amt', 0))

            # 같은 '못 빠져나온다'라도 원인이 두 갈래다. 문구를 뭉뚱그리면 운영자가
            #  할 수 있는 조치를 오판한다 — 제외는 설정 문제고, 매도 불가는 시장 문제다.
            blocked = (kind == UNMANAGED_NO_SELLABLE)
            title = "매도 실패" if blocked else "자동매도 제외 종목"
            cause = (f"매도를 시도했으나 증권사 매도가능수량이 0입니다 ({kind}).\n"
                     f"시스템이 스스로 청산할 수 없는 상태입니다."
                     if blocked else
                     f"{kind}으로 자동 매도 대상에서 제외되어 있어 시스템이 손절하지 않습니다.")

            self.log(f"⚠️ [손절선 이탈 경보] {name}({code}): 수익률 {profit_rate:.2f}% ≤ 손절 기준 {sl_rate:.2f}% "
                     f"— {kind}이라 시스템이 청산하지 못합니다.")
            delivered = _pkg().alert_delivered(
                f"⚠️ [손절선 이탈 — {title}]\n\n"
                f"종목: {name}({code})\n"
                f"수익률: {profit_rate:.2f}% (손절 기준: {sl_rate:.2f}%)\n"
                f"보유: {qty:,}주 / 평가금 {eval_amt:,}원 / 평가손익 {loss_amt:,}원\n\n"
                f"사유: {cause}\n"
                f"직접 청산 여부를 판단해 주세요. (동일 종목 재알림은 24시간 후)")
            #  전달되면 24시간, 실패하면 짧게 — 침묵도 도배도 아닌 쪽으로 재시도한다.
            self.unmanaged_stop_notified[code] = now + (86400 if delivered else _pkg().ALERT_RETRY_SEC)
            if not delivered:
                self.log(f"[손절선 이탈 경보] {name}({code}) 전송 실패 — "
                         f"{int(_pkg().ALERT_RETRY_SEC)}초 뒤 다시 시도합니다.")
        except Exception as e:
            logger.debug(f"[손절선 이탈 경보] {code} 처리 실패: {e}")

    def _scan_after_hours_sell_signals(self, target_cano):
        """[관측성] 장 마감 후 청산 신호를 하루 한 번 스캔해 알린다. (주문 없음)

        메인 루프는 마감과 함께 분석을 통째로 멈춘다(트래픽 절감). 그래서 종가가
        확정된 뒤 손절선·트레일링선을 이탈해도 다음 개장까지 아무도 모른다 —
        추세추종에서 가장 비싼 공백이다("탈출 전략이 없다면 포지션을 잡지 마라").

        주문은 내지 않는다. 마감 뒤에는 낼 수 없고, 다음 개장 때 그 시점 가격으로
        정식 판정이 다시 돈다. 여기서 하는 일은 사실을 알리는 것뿐이다.

        [시각] 종가 단일가(15:20~15:30)가 끝나 일봉이 확정된 뒤에 돈다. 그 전에 돌면
        접속매매 마지막 가격으로 판정해 종가와 어긋난다.
        [빈도] 거래일당 1회. 휴장일에는 돌지 않는다(판정할 새 봉이 없다).
        """
        try:
            if not getattr(config, 'AFTER_HOURS_SELL_ALERT', True):
                return
            if api.is_holiday_today():
                return
            now = datetime.now()
            after = getattr(config, 'AFTER_HOURS_SELL_ALERT_TIME', "1535")
            if now.strftime("%H%M") < after:
                return
            today = now.strftime("%Y%m%d")
            # [기준] 일봉이 확정된 뒤에만 판정한다. 설정 시각(기본 15:35)이 확정 여유
            #  (15:40)보다 이르면 잔고의 현재가가 아직 NXT 체결가라 종가와 어긋난다 —
            #  '종가가 확정된 뒤에 돈다'는 이 함수의 전제를 시계가 아니라 확정 여부로 센다.
            if api.krx_last_settled_day() != today:
                return
            if self.after_hours_scan_date == today:
                return

            acnt = config.session.auto_acnt_prdt_cd
            holdings, _summary = api.get_domestic_balance(target_cano, acnt)
            #  [2026-09-07] holdings is None 은 **조회 실패**다. 빈 목록(보유 없음)과
            #   같은 자리에서 걸러 내면 '오늘 볼 것이 없었다'로 굳는다([[unknown-vs-empty]]).
            if holdings is None:
                logger.warning("[장마감 청산 신호 스캔] 잔고를 읽지 못했습니다 — "
                               "다음 주기에 다시 봅니다(보유 없음이 아닙니다)")
                return
            if not holdings:
                self.after_hours_scan_date = today      # 정말로 볼 것이 없는 날
                return

            self.log("[장마감] 청산 신호 점검 (주문 없음 · 알림 전용)")
            rules = _enrich_rules_with_weights(db_manager.db.get_all_stock_strategies())
            self._check_sell_conditions(
                holdings, is_market_open=False,
                rules_map={r['code']: r for r in rules},
                restricted_stocks=get_restricted_stocks(*_get_trade_account()))
            #  [표식은 끝난 뒤 · 2026-09-07] 종전에는 잔고 조회 **전에** 찍었다. 그래서
            #   조회가 실패하거나 판정이 도중에 던지면 그 거래일의 마감 후 점검이 통째로
            #   사라졌고, 하필 _alert_after_hours_sell 이 '전달을 확인한 뒤 기록한다 —
            #   실패하면 다음 주기에 다시 시도'로 고쳐 둔 재시도까지 여기서 막혔다.
            #   이 스캔은 종가 확정 뒤 손절·트레일링 이탈을 알리는 유일한 경로이고,
            #   놓치면 하룻밤 갭이 그대로 손실이 된다.
            self.after_hours_scan_date = today
        except Exception as e:      # noqa: BLE001
            # 알림 전용 경로다. 실패해도 매매 루프를 흔들면 안 된다 — 다만 조용하지는 않다.
            logger.warning(f"[장마감 청산 신호 스캔] 실패 — 다음 주기에 다시 봅니다: "
                           f"{type(e).__name__}: {e}")

    def _alert_after_hours_sell(self, code, name, item, reason, current_price, order_price, qty):
        """[관측성] 장 마감 후 감지된 매도 신호를 알린다.

        마감 뒤에는 주문을 낼 수 없어 종전에는 로그 한 줄만 남고 끝났다. 청산이 다음
        개장까지 밀리는데 운영자가 그 사실을 알 방법이 없었다 — 손절·트레일링이면
        하룻밤의 갭이 그대로 손실이므로, 직접 판단할 기회를 준다.

        [주의] 이 신호는 확정이 아니다. 다음 개장 때 그 시점 가격으로 다시 판정하므로
        신호가 사라질 수도, 다른 사유로 바뀔 수도 있다. 문구에 그대로 적는다.

        같은 종목·같은 사유는 마감 세션당 한 번만 보낸다(주기마다 재감지되므로).
        사유가 바뀌면 다시 알린다 — 트레일링과 손절은 운영자가 할 판단이 다르다.
        """
        try:
            if self.after_hours_sell_notified.get(code) == reason:
                return

            profit_rate = _pkg().holding_profit_rate(item)
            if profit_rate is None:
                #  알림 본문에 0.0% 를 적으면 갭 전 판단을 그르친다. 모르면 적지 않는다.
                logger.warning(f"[마감후 신호] {code} 수익률을 구할 수 없어 알림을 보류한다")
                return
            eval_amt = api.safe_int(item.get('evlu_amt', 0))
            pfls_amt = api.safe_int(item.get('evlu_pfls_amt', 0))

            #  전달을 확인한 뒤에 '보냈음'을 기록한다(위 손절선 경보와 같은 이유).
            #  실패하면 기록하지 않아 다음 주기에 다시 시도한다 — 마감 후 갭 전에
            #  운영자가 판단할 기회를 주는 것이 이 알림의 목적이다.
            delivered = _pkg().alert_delivered(
                f"🔔 [장마감 후 매도 신호]\n\n"
                f"종목: {name}({code})\n"
                f"사유: {reason}\n"
                f"수익률: {profit_rate:+.2f}% / 평가손익 {pfls_amt:,}원\n"
                f"보유: {qty:,}주 / 평가금 {eval_amt:,}원\n"
                f"기준가: {int(current_price):,}원 (예상 주문가 {order_price:,}원)\n\n"
                f"장이 마감되어 주문은 전송되지 않았습니다.\n"
                f"다음 개장 시 그 시점 가격으로 다시 판정합니다 — 신호가 유지되면 "
                f"자동 청산되고, 사라지면 보유를 유지합니다.")
            if delivered:
                self.after_hours_sell_notified[code] = reason
        except Exception as e:
            logger.debug(f"[장마감 매도 신호 알림] {code} 처리 실패: {e}")

    def _check_sell_conditions(self, holdings, is_market_open=True, rules_map=None, restricted_stocks=None):
        # [정리] 보유가 끝난 종목의 '매도 불가 연속 횟수'를 버린다. 남겨 두면 나중에 같은
        #  종목을 재매수했을 때 옛 횟수에 이어붙어 첫 일시적 0에서 곧바로 오경보가 난다.
        try:
            held = {h['pdno'] for h in (holdings or []) if api.safe_int(h.get('hldg_qty')) > 0}
            for gone in [c for c in self.no_sellable_streak if c not in held]:
                del self.no_sellable_streak[gone]
            for gone in [c for c in self.stuck_pending_streak if c not in held]:
                del self.stuck_pending_streak[gone]
            # 장이 열리면 마감 후 알림 스로틀을 푼다 — 신호가 개장 후에도 살아 있으면
            #  정상 청산되고, 그날 마감 뒤 다시 감지되면 그때 다시 알려야 한다.
            if is_market_open:
                self.after_hours_sell_notified.clear()
            else:
                for gone in [c for c in self.after_hours_sell_notified if c not in held]:
                    del self.after_hours_sell_notified[gone]
        except Exception:
            pass

        # [WS] 시스템 트레이딩 종목을 실시간 피드에 최우선으로 등록한다.
        #  보유종목(포지션, 최우선) → 매수후보 순서로 priority. 매수후보는 국내주식 + (ETF 포함 설정 시)국내 ETF.
        #  ETF 미포함 설정이면 ETF는 시스템 대상이 아니므로 '그 외(other) 로테이션'으로 둔다.
        try:
            from brokers import realtime
            hold_codes = [h['pdno'] for h in (holdings or [])]
            cand_codes = [s['code'] for s in config.session.stock_data.get('stocks_kr', [])]
            etf_codes = [s['code'] for s in config.session.stock_data.get('etfs_kr', [])]
            if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
                realtime.update_symbols(hold_codes + cand_codes + etf_codes, [])
            else:
                realtime.update_symbols(hold_codes + cand_codes, etf_codes)

            # [WS] 커버리지 진단: 시스템 종목 수가 WS 동시 용량을 넘으면 초과분은 현재가/호가를
            #  REST로 폴백(로테이션)하므로 모의투자(2 TPS)에서 분석이 느려질 수 있다. 상태 변화 시에만 1회 로그.
            cov = realtime.coverage()
            #  [2026-09-09] 토스 피드는 시세를 구독하지 않아 커버리지 항목이 None 이다
            #   ('0종목 커버'가 아니라 '재는 축이 아님'). 아래 비교는 숫자를 전제하므로
            #   None 이면 건너뛴다 — 종전이면 TypeError 가 바깥 except 에 삼켜져,
            #   커버리지 진단이 조용히 사라진 채 아무도 몰랐다.
            if cov and cov.get('capacity') is not None:
                sig = (cov.get('priority'), cov.get('capacity'), cov.get('rest_fallback'), cov.get('ob_covered'))
                if sig != getattr(self, '_ws_cov_sig', None):
                    self._ws_cov_sig = sig
                    if cov.get('rest_fallback', 0) > 0:
                        self.log(f"[WS] 시스템 종목 {cov['priority']}개 > 동시 용량 {cov['capacity']}개 "
                                 f"→ {cov['rest_fallback']}개는 현재가 REST 폴백(분석 지연 가능). "
                                 f"관심목록 축소를 권장합니다.")
                    else:
                        self.log(f"[WS] 커버리지 양호: 현재가 {cov['price_covered']}개 / 호가 {cov['ob_covered']}개 "
                                 f"실시간 구독(시스템 {cov['priority']}개 전부 커버).")
        except Exception: pass

        # [최적화] 인자로 전달받은 holdings 사용
        if not holdings:
            self.portfolio_heat_amt = 0.0  # 보유 없음 = 오픈 리스크 0 (매수 경로의 히트 캡 판정용)
            self.portfolio_heat_unknown = False
            with self._lock:
                self.holding_risk_cache.clear()
            return

        # [추가] 개별 룰 로드 ([최적화] 루프에서 주기당 1회 로드해 전달받으면 재조회 생략)
        if rules_map is None:
            #  [Fix 2026-09-06] 매도 쪽은 **멈추지 않는다** — 매도 검사를 거르면 손절·
            #   트레일링이 통째로 꺼지고, 그것이 룰을 잃는 것보다 훨씬 비싸다. 다만
            #   조용히 넘기지 않는다: 룰은 손절을 **조이는 방향만** 허용되므로
            #   (build_sell_thresholds), 룰을 잃는 것은 곧 그 종목의 손절선이 넓어지는
            #   것이다 — 운용자가 못 박은 지시가 이번 주기에 빠진 사실을 남긴다.
            try:
                custom_rules = db_manager.db.get_all_stock_strategies(strict=True)
            except Exception as _re:
                self.log(f"[개별 룰] 읽지 못했습니다 — 이번 주기의 매도 판정은 전역 "
                         f"기준으로 돕니다(룰로 조여 둔 손절이 그만큼 넓어집니다): "
                         f"{type(_re).__name__}: {_re}")
                custom_rules = []
            custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
            rules_map = {r['code']: r for r in custom_rules}

        # [추가] 트레이딩 제한 종목 로드 (현재 시스템 트레이딩 계좌 기준으로 필터링)
        if restricted_stocks is None:
            _trade_cano, _trade_acnt = _get_trade_account()
            restricted_stocks = get_restricted_stocks(_trade_cano, _trade_acnt)

        # [최적화] 종목별 개별 DB 조회(최근 매수/보유분 매수 내역)를 주기 시작 시 배치 쿼리로 일괄 로드
        #  (기존: 보유 종목 × 최대 5쿼리 → 배치 3쿼리, 저사양 SD카드 SQLite I/O 절감)
        _all_hold_codes = [h['pdno'] for h in holdings]
        # [계좌 귀속] trades 는 모든 모드·계좌가 한 파일을 공유한다(토스·한투가 같은
        #  테이블에 쌓인다). 계좌로 거르지 않으면 같은 종목을 두 계좌에서 들고 있을 때
        #  **남의 계좌 매수 기록**으로 손절선(수량가중평균)·오픈 리스크·진입일이 계산된다.
        _acct = self._trade_account_key()
        #  [Fix 2026-09-06] 종전에는 이 두 조회가 실패해도 **빈 dict** 를 돌려줘서,
        #   호출부가 그것을 "이 종목들은 매수 기록이 전혀 없다"로 읽었다. 그 기록이
        #   손절선의 근거다 — 실측: 진입 시 기록된 -3.2% 가 전역 고정 -7.0% 로 떨어져
        #   손절폭이 2.19배가 된다. 매도 엔진 자체는 일봉 ATR 로 복원해 버티지만
        #   (entry_atr_stop_rate), **안전망 두 곳**(미체결 매수 취소·미관리 이탈 경보)은
        #   차트를 안 보므로 그대로 전역값으로 떨어진다. 이제 못 읽은 것은 못 읽었다고
        #   답하므로, 그때는 직전 주기가 **실제로 쓴** 선을 이어받는다.
        latest_buy_map = db_manager.db.get_latest_buy_trades(_all_hold_codes, account=_acct)
        buy_trades_map = db_manager.db.get_buy_trades_for_current_holdings(_all_hold_codes, account=_acct)
        self._buy_records_unknown = buy_trades_map is None
        if self._buy_records_unknown:
            self.log("[손절 기준] 매수 기록을 읽지 못했습니다 — 이번 주기의 손절선은 "
                     "직전 주기 실측값과 일봉 ATR 로 복원합니다(전역 고정폭으로 넓히지 "
                     "않습니다).")
            buy_trades_map = {}
        if latest_buy_map is None:
            self.log("[진입일] 최근 매수 기록을 읽지 못했습니다 — 진입일 복원과 트레일링 "
                     "앵커 복원이 이번 주기에는 근거를 잃습니다.")
            latest_buy_map = {}
        # 진입일(보유수량이 0 → 1 이상이 된 시점) — 시간청산 기준
        entry_date_map = db_manager.db.get_position_entry_dates(_all_hold_codes, account=_acct)

        # [추가] 포트폴리오 히트(총 오픈 리스크) 스냅샷 갱신 — 같은 주기의 피라미딩/신규 매수 캡 판정에 사용
        # [알려진 한계] 기준은 **잔고**다. 직전 주기에 낸 매수·증액 주문이 아직 체결되지
        #  않았으면 그 오픈 리스크는 여기 안 잡혀, 체결될 때까지 히트가 과소평가된다
        #  (주기 안에서의 이중 사용은 매수/증액 경로의 '주문 전 선점'이 막지만, 그 선점분은
        #  다음 주기의 이 재계산으로 리셋된다).
        #  [2026-09-08 정정] 종전 주석은 과소평가 폭을 '그 한 주문분'이라고 적었다. 틀렸다 —
        #   한 주기에 빈 슬롯 수만큼 주문이 나가므로 폭은 그 주문들의 합이고, 게다가 슬롯을
        #   **잔고로만** 세던 탓에 미체결이 남은 다음 주기가 같은 자리를 다시 비어 있다고
        #   보아 주문이 겹겹이 쌓일 수 있었다. 슬롯 판정을 '잔고 + 미체결'로 바꿔
        #   (occupied_slot_codes) 그 누적을 끊었으므로, 지금의 과소평가 폭은 **한 주기에
        #   나간 주문분**으로 묶인다. 증액(피라미딩)은 슬롯을 쓰지 않으므로 여기서 제외다.
        try:
            self.portfolio_heat_amt = self.risk_manager.compute_portfolio_heat(
                holdings, buy_trades_map, live_map=self._live_risk_map(_all_hold_codes))
            self.portfolio_heat_unknown = False
        except Exception as e:
            # [fail-closed] 0으로 두면 '오픈 리스크 없음'이 되어 히트 캡의 예산이 통째로 열린다.
            #  못 센 것과 없는 것은 다르다 — 못 셌다고 표시하고 신규 진입을 막는다.
            self.portfolio_heat_unknown = True
            logger.warning(f"[히트] 오픈 리스크 산출 실패 — 신규 진입을 보류한다: {e}")

        # [최적화] 보유 종목 실시간 데이터 일괄 수집 (Micro-Cache 사전 예열)
        codes_to_prefetch = []
        for item in holdings:
            code = item['pdno']
            qty = api.safe_int(item.get('ord_psbl_qty'))
            if not self.order_manager.is_pending(code) and qty > 0:
                codes_to_prefetch.append(code)
                
        if codes_to_prefetch:
            api.prefetch_multiple_current_prices(codes_to_prefetch, is_overseas=False, include_investor=False, prefer_ws=True)

        # [수정] 일괄 예열 캐시를 활용하므로 워커별 딜레이를 대폭 단축 (Rate Limit 안전장치 유지)
        tps = config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 0.1  
        
        # [추가] 시장 국면 판단 (적응형 임계값용) - 매도 분석 시에도 상태 분류를 위해 필요
        market_regime_adj = {}
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", False):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj

        # [Fix: Point 1] 매도 분석 루프 병렬화 (ThreadPoolExecutor 활용)
        def _sell_worker(item):
            if not self.is_running: return
            
            code = item['pdno']; name = item['prdt_name']
            
            # [추가] 트레이딩 제한 종목은 매도 분석에서 완전히 제외 (수동 매수/홀딩용)
            #  [Fix] 단, 시스템이 손절하지 않는 포지션이므로 손절선 이탈 시 경보는 발송한다.
            if code in restricted_stocks:
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}: {UNMANAGED_RESTRICTED}")
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_RESTRICTED, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                return
            
            if self.order_manager.is_pending(code):
                self.set_stock_state(code, None)
                # [보호 공백 해소] 주문이 걸려 있는 동안 이 종목은 매도 판정에서 통째로 빠진다.
                #  부분체결로 이미 확보된 물량이 손절선을 이탈해도 주문이 종결될 때까지
                #  청산되지 않는다("탈출 전략이 없다면 포지션을 잡지 마라"와 충돌).
                #  손절 상황이면 미체결 '매수'를 즉시 취소해, 다음 주기에 정상 청산되게 한다.
                self._cancel_pending_buy_on_stop_loss(code, name, item, buy_trades_map.get(code),
                                                     rule=rules_map.get(code))
                # [관측성] 종전에는 DEBUG 로그라 화면·파일 어디에도 남지 않았다. 이 스킵은
                #  손절·트레일링을 통째로 끄는 경로이므로 **항상** 남긴다 — 매도가 안 나가는데
                #  이유를 알 수 없던 원인이 이것이었다(2026-08-05).
                odnos = self.order_manager.pending_odnos(code)
                streak = self.stuck_pending_streak.get(code, 0) + 1
                self.stuck_pending_streak[code] = streak
                self.log(f"[분석스킵] {name}({code}): 진행 중인 주문 존재 "
                         f"({', '.join(str(o) for o in odnos) or '?'} · {streak}회 연속) "
                         f"— 이 동안 손절·트레일링 판정이 건너뛰어집니다")
                if streak >= STUCK_PENDING_ALERT_CYCLES:
                    self._alert_unmanaged_stop(code, name, item, UNMANAGED_STUCK_PENDING, rule=rules_map.get(code),
                                               buy_trades=buy_trades_map.get(code))
                return
            self.stuck_pending_streak.pop(code, None)

            # [추가] 연장거래 시간에는 그 시장이 취급하지 않는 종목의 매도를 스킵한다.
            #  NXT 창: ETF 및 NXT 비거래 종목. KRX 애프터(16:00~20:00): ETF/ETN(일반 주권만 거래).
            is_nxt_market = api.nxt_order_window()
            etf_untraded = api.domestic_etf_untraded_window()
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            # [수정] ETF 판정을 관심목록뿐 아니라 종목명 휴리스틱까지 포함하도록 일원화
            #  (보유만 하고 관심목록에 없는 ETF/ETN도 식별)
            is_domestic_etf = (not is_overseas_stock) and api.is_domestic_etf_etn(code, name)

            if etf_untraded and not is_overseas_stock:
                if is_domestic_etf or (is_nxt_market and hasattr(api, 'is_nxt_tradeable') and not api.is_nxt_tradeable(code)):
                    self.set_stock_state(code, None)
                    # [앵커 복원] 이 분기는 아래 ETF 제외보다 **먼저** 걸린다. 여기서 그냥
                    #  돌아서면 NXT 프리마켓(08:00~08:50)에만 봇을 켜는 운용에서는
                    #  ETF 앵커가 영영 복원되지 않는다. 주문과 무관한 기록이므로 여기서도 남긴다.
                    self._restore_trailing_anchor(
                        code, name,
                        _pkg().resolve_entry_date(entry_date_map.get(code),
                                                  latest_buy_map.get(code)))
                    return

            # [추가] ETF 포함 여부가 False면 보유 ETF는 자동 매도 대상에서도 제외한다.
            #  (SYSTEM_INCLUDE_ETF는 매수 필터이지만, 사용자 요청에 따라 매도도 제외하여
            #   ETF는 전적으로 수동 관리하도록 한다. 단 시스템이 손절하지 않으므로 주의)
            if is_domestic_etf and not getattr(config, 'SYSTEM_INCLUDE_ETF', False):
                self.set_stock_state(code, None)
                self.log(f"[매도스킵] {name}({code}): {UNMANAGED_ETF}")
                # [Fix] 시스템이 손절하지 않는 포지션이므로 손절선 이탈 시 경보는 발송한다.
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_ETF, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                # [앵커 복원] 매도 판정에서 빠져도 앵커는 남겨 둔다. 여기서 돌아서면 이 종목의
                #  trailing_stops 는 영영 갱신되지 않아, 마지막 매수의 체결가가 앵커로 굳는다
                #  (102780: 4월 진입·실제 고가 36,360인데 8월 1주 추가 매수로 25,500이 기록).
                #  ETF 포함 설정을 켜는 순간 그 값이 곧바로 청산선의 근거가 되고, DB만 보고
                #  판단하는 도구·감사도 같은 값을 읽는다. 주문은 내지 않으므로 부작용이 없다.
                self._restore_trailing_anchor(
                    code, name,
                    _pkg().resolve_entry_date(entry_date_map.get(code), latest_buy_map.get(code)))
                return

            qty = api.safe_int(item.get('ord_psbl_qty'))
            #  [던지지 말고 복원한다 · 2026-09-06] 종전에는 `api.safe_float(item.get('evlu_pfls_rt'), default=0.0)`
            #   라, 증권사가 이 필드를 비우면 ValueError 로 **그 종목의 매도 판정이 통째로
            #   건너뛰어졌다**(손절·트레일링이 그 주기에 돌지 않는다). 필요한 값은 바로
            #   아래 두 줄에 있다 — holding_profit_rate 가 그 둘로 정확히 복원한다.
            profit_rate = _pkg().holding_profit_rate(item)
            #  [Fix 2026-09-06] 이 두 줄도 하드 서브스크립트였다 — 위 주석이 "필요한 값은
            #   바로 아래 두 줄에 있다"고 가리키는 그 줄들이다. 증권사가 이 필드를 비우면
            #   (KIS 는 키를 주고 빈 문자열을 담는다) float('') 가 ValueError 를 내고,
            #   holding_profit_rate 가 복원한 수익률을 써 보기도 전에 그 종목의 매도 판정이
            #   통째로 건너뛰어진다. 고친 자리 바로 옆에 같은 함정이 남아 있었다.
            current_price = api.safe_float(item.get('prpr'), default=0.0)
            buy_price = api.safe_float(item.get('pchs_avg_pric'), default=0.0)
            if profit_rate is None:
                self.log(f"[매도 보류] {name}({code}) 수익률을 구할 수 없습니다 "
                         f"(평가손익률·평단·현재가 모두 미확보) — 이번 주기 판정을 건너뜁니다")
                return
            
            time.sleep(safe_delay)
            
            if not self.is_running: return # 대기 후 재확인
            
            if qty <= 0:
                # [관측성] 위 대기 주문 스킵과 같은 이유로 항상 남긴다. 잔고에는 보이는데
                #  주문 가능 수량만 0이면 시스템은 그 포지션을 지켜주지 못한다.
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}({code}): 주문 가능 수량 0 "
                         f"— 손절·트레일링 판정이 건너뛰어집니다")
                return

            # [안전장치] 현재가가 0/음수면 판정 자체가 불가능하다. 잔고 응답의 prpr은 거래정지·
            #  장 시작 전·API 이상에서 0으로 올 수 있는데, 그대로 태우면 수익률이 -100%로 계산되어
            #  본전청산·트레일링이 동시에 오발동하고(실측: analyze_sell이 '본전청산(-100.0%)'로
            #  매도 판정), 주문가도 0원이 되어(order_price<=0 폴백이 current_price=0을 되돌림)
            #  지정가 0원 매도가 전송된다. 거부되더라도 그 종목은 is_pending으로 다음 주기의 매도
            #  분석에서 빠져, 정작 진짜 손절이 필요할 때 막힌다.
            #  판정 불가이므로 주문을 내지 않고 보류하되, 시스템이 지켜주지 못하는 포지션이므로
            #  손절선 이탈 경보는 보낸다(트레이딩 제한·ETF 제외와 같은 취급).
            if current_price <= 0:
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}({code}): 현재가 이상({current_price}) — {UNMANAGED_BAD_PRICE}")
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_BAD_PRICE, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                return

            # [안전장치] 시세 조회에 실패해 폴백한 값(_price_stale)은 판정 근거가 될 수 없다.
            #  관찰 모드는 실패 시 직전 정상가로 폴백하는데, 그 값으로 손절을 판정하면
            #  실제로는 이미 손절선을 넘겼는데도 옛 가격으로 '아직 괜찮다'는 답이 나온다.
            #  실계좌(KIS)는 조회 실패 시 잔고 자체가 None이 되어 주기가 통째로 멈추므로,
            #  같은 취급(판정 보류 + 경보)이 실거래 동작과도 일치한다.
            if item.get('_price_stale'):
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}({code}): {UNMANAGED_STALE_PRICE}")
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_STALE_PRICE, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                return

            rule = rules_map.get(code)
            market_type = self._get_stock_market_type(code)
            score_adj = market_regime_adj.get(market_type, 0.0)
            
            # [최적화] 주기 시작 시 배치 로드한 결과 사용 (종목별 개별 쿼리 제거)
            # [Fix] 보유일수는 '최근 매수'가 아니라 진입일(보유수량이 0 → 1 이상이 된 시점) 기준.
            #  분할 매수·피라미딩으로 1주만 더 담아도 시간청산 시계가 0으로 리셋되던 문제.
            last_buy = latest_buy_map.get(code)
            holding_days, is_mr_holding = _pkg().resolve_holding_context(
                last_buy, entry_date=entry_date_map.get(code))
            entry_date = _pkg().resolve_entry_date(entry_date_map.get(code), last_buy)

            highest_price = self._cached_anchor(code)

            # [안전장치] 액면분할·무상증자로 평단이 재조정되면 최고가만 조정 전 값으로 남아
            #  트레일링 스탑이 즉시 오발동한다(5:1 분할 → drop_rate 80% → 시장가 강제 매도).
            #  최고가 갱신은 단조 증가라 스스로 내려오지 못하므로 여기서 같은 배율로 보정한다.
            highest_price = self._apply_corporate_action(code, name, item, buy_price, highest_price)

            if current_price > buy_price:
                if highest_price == 0.0 or current_price > highest_price:
                    db_manager.db.update_highest_price(code, current_price)
                    with self._lock:
                        self.trailing_stop_cache[code] = current_price
                    highest_price = current_price

            df = api.get_chart_data(code, is_overseas=is_overseas_stock)
            
            # [추가] 차트 데이터 당일 종가/고가/저가 실시간 갱신 (지표 불일치 완벽 방지)
            #  모든 장 종료 후에는 반영하지 않는다(KRX 확정 종가 유지). 손절·트레일링 판정은
            #  아래 analyze_sell에 current_price를 그대로 넘기므로 실시간 대응에는 영향이 없다.
            indicators.apply_realtime_price(df, api.chart_overlay_price(current_price, is_overseas_stock))

            highest_price = self._restore_trailing_anchor(
                code, name, entry_date, highest_price, df=df, is_overseas=is_overseas_stock)

            # [SSOT] 임계값 조립은 build_sell_thresholds가 단독 보유한다.
            #  잔고 화면(메뉴 9-2)의 보유 분석도 같은 함수를 호출해 판정이 갈리지 않게 한다.
            # [최적화] 주기 시작 시 배치 로드한 buy_trades_map 사용 (종목별 개별 쿼리 제거)
            # [Fix] 매수 기록이 없는 포지션(HTS 직접 매수)은 진입 시점 봉의 ATR에서 손절률을
            #  복원한다. 차트가 필요하므로 df 확보 뒤로 옮겼다(그 전까지 thresholds는 쓰이지 않는다).
            thresholds = _pkg().build_sell_thresholds(
                rule=rule, score_adj=score_adj, buy_trades=buy_trades_map.get(code, []),
                fallback_atr_rate=_pkg().entry_atr_stop_rate(df, entry_date)
            )

            already_half_sold = self._already_half_sold(code)
            result = self.strategy.analyze_sell(code, name, df, current_price, buy_price, profit_rate, thresholds=thresholds, already_half_sold=already_half_sold, holding_days=holding_days, is_mr_holding=is_mr_holding, highest_price=highest_price)
            
            # [추가] 분석 성공 시 상태 업데이트
            self.set_stock_state(code, result['state'])
            
            ind = result['ind']

            # [히트] 이번 주기 판정이 실제로 쓴 손절선 재료를 남긴다. 오픈 리스크 산출은
            #  주기 앞머리(잔고 직후)에서 도는데 그 자리엔 차트가 없어, 종전에는 매수 시점
            #  손절률에서 ATR을 역산했다 — 추세가 길어질수록 실제 ATR이 커져 실제 콜백이
            #  넓어지는 만큼 리스크를 과소 계상한다(방어가 무뎌지는 한 방향).
            #  다음 주기가 이 값을 쓴다(60초 전의 일봉 ATR이라 실질 차이가 없다).
            try:
                with self._lock:
                    self.holding_risk_cache[code] = {
                        'sl_rate': float(thresholds.get(
                            "STOP_LOSS_RATE", config.SELL_STRATEGY.get("STOP_LOSS_RATE", -7.0)) or 0.0),
                        'atr': float(ind.get('atr') or 0.0),
                    }
            except (TypeError, ValueError):
                pass

            rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
            adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
            cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
            action_str = "매도" if result['action'] == 'sell' else "보유"
            rule_msg = " [개별 룰 적용]" if rule else ""
            extra_info = ""
            if thresholds:
                bs = thresholds.get('BUY_SCORE')
                if bs is not None: extra_info += f", 기준={bs:.1f}"
                w = thresholds.get('WEIGHTS')
                if w: extra_info += f", 가중치={w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

            self.log(f"[보유분석] {name}({code}): 수익률={profit_rate:.2f}%, 점수={result['score']}, 상태={result['state']}, 판단={action_str}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}{extra_info}{rule_msg}")

            if result['action'] == 'sell':
                reason = result['reason']
                score = result['score']
                sell_ratio = result.get('sell_ratio', 1.0)
                
                if sell_ratio < 1.0:
                    target_sell_qty = int(qty * sell_ratio)
                    if target_sell_qty < 1:
                        self.log(f"매도 보류: {name} - 보유 수량({qty}주) 부족으로 분할 매도({reason}) 스킵 (최종 목표 대기)")
                        return
                else:
                    target_sell_qty = qty
                
                if rule: reason += " [개별 룰 적용]"
                if thresholds.get("ATR_APPLIED_SL_RATE") is not None and "손절" in reason: reason = reason.replace("손절", "ATR손절")
                
                raw_order_price = current_price * (1 - config.SLIPPAGE_RATE)
                #  ETF·ETN 은 호가 격자가 다르다(2,000원 이상 5원 단일). 주권 표로
                #  반올림하면 손절 지정가가 최대 tick/2 위로 밀려 체결이 늦어진다.
                order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False,
                                                       is_etf=is_domestic_etf))
                if order_price <= 0: order_price = int(current_price)
                # [안전장치] 하한가에 락된 날 '현재가 - 슬리피지'는 제한폭 밖이라 주문이 거부된다.
                #  손절이 가장 필요한 날 접수조차 되지 않으므로 제한폭 안으로 되돌린다.
                order_price = int(self._clamp_order_price(code, order_price))

                if not is_market_open:
                    self.log(f"[장마감] 매도 신호 감지 (주문 미전송): {name} - {reason}")
                    self._alert_after_hours_sell(code, name, item, reason,
                                                 current_price, order_price, target_sell_qty)
                    return

                real_qty = api.fetch_sellable_quantity(code)

                # [안전장치] 조회 실패(None)는 '팔 수 없음'이 아니다. 종전에는 실패도 0으로
                #  와서 아래 분기가 매도를 중단시켰다 — 일시적 API 오류가 손절을 거르는
                #  결과로 이어진다. 매수 경로는 같은 상황에서 예수금 폴백으로 주문을 내고,
                #  수동 매매 화면도 잔고 수량으로 폴백한다. 자동 청산만 반대 방향이었다.
                #  추세추종에서 못 파는 비용 > 못 사는 비용이므로, 이미 확보한 잔고 수량으로
                #  낸다. 정말 팔 수 없는 상태라면 증권사가 거부하고, 그 비용이 훨씬 싸다.
                if real_qty is None:
                    held_qty = api.safe_int(item.get('hldg_qty'))
                    self.log(f"매도 수량 조회 실패: {name} — 보유 {held_qty}주 기준으로 진행합니다 "
                             f"(조회 실패를 '매도 불가'로 읽지 않는다)")
                    real_qty = held_qty

                if real_qty < target_sell_qty:
                    if real_qty > 0:
                        self.log(f"매도 수량 조정: {name} {target_sell_qty}주 -> {real_qty}주")
                        target_sell_qty = real_qty
                        self.no_sellable_streak.pop(code, None)
                    else:
                        # [안전장치] 매도를 결정했는데 팔 수 없는 상태다. 종전에는 로그 한 줄만
                        #  남기고 조용히 끝나, 거래정지·상장폐지처럼 시스템이 스스로 빠져나올 수
                        #  없는 포지션이 운영자 모르게 방치됐다. 다만 미체결 취소 직후 한 주기
                        #  정도는 정상적으로 0이 되므로, 연속 관측될 때만 경보한다.
                        streak = self.no_sellable_streak.get(code, 0) + 1
                        self.no_sellable_streak[code] = streak
                        self.log(f"매도 중단: {name} 주문 가능 수량 부족 "
                                 f"(미체결 존재 가능성 · {streak}회 연속)")
                        if streak >= NO_SELLABLE_ALERT_CYCLES:
                            self._alert_unmanaged_stop(code, name, item, UNMANAGED_NO_SELLABLE, rule=rules_map.get(code),
                                                       buy_trades=buy_trades_map.get(code))
                        return
                else:
                    self.no_sellable_streak.pop(code, None)

                self.log(f"매도 실행: {name} - {reason}")
                # [비용] 종전에는 KIS 잔고의 '평가손익'(evlu_pfls_amt)을 그대로 실현손익으로
                #  적었다. 그것은 매도 판단 시점의 미실현 손익이라 매도 수수료·거래세도,
                #  체결가와 판단가의 차이도 빠져 있다. 여기서는 왕복 비용을 뺀 값으로 적고,
                #  매입가를 함께 남겨 체결 확인 단계에서 실제 체결가로 다시 계산하게 한다.
                try:
                    sell_buy_price = float(item.get('pchs_avg_pric') or 0)
                except (TypeError, ValueError):
                    sell_buy_price = 0.0
                try:
                    ref_price = float(order_price) or float(item.get('prpr') or 0)
                except (TypeError, ValueError):
                    ref_price = 0.0
                #  [2026-09-14] 실거래는 총차익·가상투자는 순손익 — 정책은 trading_cost.realized_profit 하나.
                est_profit, est_rate, est_cost = trading_cost.realized_profit(
                    sell_buy_price, ref_price, target_sell_qty)
                if sell_buy_price <= 0:      # 매입가를 못 구하면 종전 값으로 폴백
                    #  [Fix 2026-09-06] 종전에는 api.safe_int(item.get('evlu_pfls_amt')) 였다. KIS 는 값이
                    #   없을 때 키를 주고 **빈 문자열**을 담으므로 int('') 가 ValueError 를 낸다.
                    #   그 예외는 **주문이 이미 나간 뒤에** 터져 매도 후처리(반익절 캐시 기록·
                    #   전량매도 시 예약 일괄취소·거래기록)를 통째로 건너뛰고, 워커의 포괄
                    #   except 가 "이번 주기에 손절·트레일링 판정을 받지 못했습니다"라는
                    #   **사실과 다른** 경보까지 띄운다(방금 판 종목이다).
                    est_profit, est_rate, est_cost = api.safe_int(item.get('evlu_pfls_amt')), profit_rate, 0.0
                try:
                    odno = self.order_manager.send_order(code, target_sell_qty, "sell", name=name, profit_amt=int(est_profit), profit_rate=est_rate, reason=reason, score=score, price=order_price, rule=rule, buy_price=sell_buy_price, cost_amt=est_cost)
                except _pkg().OrderOutcomeUnclear as e:
                    #  [Fix 2026-09-08] 매도는 방향이 반대다 — 접수됐는데 '안 됐다'로 두면
                    #   다음 주기가 같은 수량을 다시 판다. 부분 체결이었다면 보유보다 많이
                    #   팔린다. 그래서 여기서 예외를 잡아 **이번 종목의 매도 처리를 끝내고**
                    #   운용자에게 넘긴다. 아래 `if odno:` 블록(거래기록·반익절 동기화·예약
                    #   일괄취소·앵커 정리)은 접수가 확인된 주문에만 해야 하는 일이라
                    #   건너뛰는 것이 맞다.
                    #   바깥 워커의 포괄 except 로 흘려보내지 않는다 — 그쪽은 "이번 주기에
                    #   손절·트레일링 판정을 받지 못했습니다"라는 **사실과 다른** 경보를 띄운다.
                    self.log(f"매도 결과 불명: {name} - 접수 여부를 확인하지 못했습니다 ({e}). "
                             f"다음 주기의 잔고 조회로 확인합니다 — 재전송하지 않습니다.")
                    return
                if odno:
                    record = {
                        "type": "sell", "code": code, "name": name, "qty": target_sell_qty,
                        "price": float(order_price), "profit_rate": profit_rate,
                        #  DB 에 적히는 값(send_order 의 profit_amt)과 같은 수를 쓴다.
                        #  종전에는 여기만 평가손익(evlu_pfls_amt)이라 같은 매도가 두 화면에서
                        #  다른 손익으로 보였고, 하드 서브스크립트라 값이 비면 여기서 터졌다.
                        "profit_amt": int(est_profit), "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "odno": odno
                    }
                    self.trade_records.append(record)
                    
                    # [Fix: Point 2] 반익절 캐시 DB 동기화
                    if sell_ratio < 1.0: 
                        self.half_tp_cache.add(code)
                        db_manager.db.insert_half_tp(code)
                    else:
                        target_cano = config.session.auto_cano
                        target_acnt = config.session.auto_acnt_prdt_cd
                        canceled_cnt = db_manager.db.cancel_reserved_sell_orders(target_cano, target_acnt, code)
                        if canceled_cnt is None:
                            #  실패를 0('취소할 것이 없었다')으로 읽으면, 보유가 없는데
                            #  매도 예약이 남아 나중에 오발동한다.
                            self.log(f"[예약취소] {name} 대기 중인 매도 예약을 취소하지 "
                                     f"못했습니다 — 보유가 없는데 예약이 남아 있습니다.")
                            api.send_telegram_message(
                                f"⚠️ [예약 취소 실패] {name}({code})\n"
                                f"전량 매도 후 대기 중인 매도 예약을 취소하지 못했습니다.\n"
                                f"예약 목록에서 확인해 주세요.")
                        elif canceled_cnt > 0:
                            self.log(f"[예약취소] 전량 매도로 인해 대기 중이던 {name} 매도 예약 주문 {canceled_cnt}건 자동 취소")
                            api.send_telegram_message(f"🗑 [예약 취소] {name}({code}) 전량 매도로 인해 대기 중이던 매도 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.")
                            
                        # [Fix] 앵커(트레일링 최고가·반익절 기록) 정리는 체결 확정(FILLED) 시점으로 유예.
                        #  접수 시점에 지우면 미체결 취소 시 포지션은 남는데 앵커만 현재가로 리셋되어
                        #  샹들리에 TS 감시가 느슨해지는 문제가 있었다 (정리는 OrderManager.update_order_status에서 수행)
                        with self.order_manager._lock:
                            self.order_manager.sell_cleanup_odnos[str(odno)] = code
                            
                    # [추가] 매수 로직(상관관계 분석 등)에서 이미 매도한 종목을 보유 중인 것으로 오인하지 않도록 메모리 잔고 즉시 차감
                    try:
                        item['hldg_qty'] = str(max(0, api.safe_int(item.get('hldg_qty')) - target_sell_qty))
                    except Exception: pass
            else:
                # [추세추종] 보유 판정 시 피라미딩(수익 포지션 증액) 평가
                self._try_pyramid_buy(code, name, qty, current_price, profit_rate, result, last_buy, is_market_open, rule=rule)

        # 병렬 처리 실행
        # [최적화] 모의투자도 워커 2개로 병렬화 (2 TPS 제한은 api 레이어의 스로틀이 보장하므로
        #  REST 대기 구간이 겹쳐져 주기당 소요 시간이 단축됨)
        max_workers = 5

        def _sell_worker_guarded(item):
            """[관측성] 매도 판정의 예외를 반드시 회수해 로그·경보로 남긴다.

            종전에는 executor.submit 결과를 wait만 하고 result()를 부르지 않아, 워커에서
            난 예외가 어디에도 남지 않았다. 그러면 그 종목은 [보유분석] 줄도 [분석스킵]
            줄도 없이 화면에서 사라지고, 손절·트레일링이 매 주기 조용히 건너뛰어진다
            (2026-08-05: 개별 룰의 NULL 컬럼 하나로 analyze_sell이 TypeError로 죽었는데
            로그에 아무 흔적이 없었다). 판정 못 한 포지션은 '보호되지 않는 포지션'이므로
            트레이딩 제한·ETF 제외와 같은 취급으로 손절선 이탈 경보까지 보낸다.
            """
            try:
                return _sell_worker(item)
            except Exception as e:
                code = item.get('pdno')
                name = item.get('prdt_name') or code
                try:
                    self.set_stock_state(code, None)
                    self.log(f"[분석실패] {name}({code}): 매도 판정 중 오류 — "
                             f"{type(e).__name__}: {e} — 이번 주기에 손절·트레일링 판정을 받지 못했습니다")
                    logger.exception(f"[매도분석] {code} 판정 실패")
                    self._alert_unmanaged_stop(code, name, item, UNMANAGED_ANALYSIS_ERROR, rule=rules_map.get(code),
                                               buy_trades=buy_trades_map.get(code))
                except Exception:
                    logger.exception(f"[매도분석] {code} 실패 처리 중 2차 오류")

        # [계좌 라우팅] 워커 스레드는 제출 스레드의 계좌 컨텍스트를 상속하지 않는다
        #  (threading.local). 감싸지 않으면 이 안에서 나가는 손절·트레일링 매도 주문과
        #  매도가능수량 조회가 자동매매 계좌가 아닌 수동 계좌로 향한다. 반드시 제출
        #  스레드에서 래핑한다 — utils.inherit_account_context 주석 참조.
        _sell_task = utils.inherit_account_context(_sell_worker_guarded)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="at_sell") as executor:
            futures = [executor.submit(_sell_task, item) for item in holdings]
            concurrent.futures.wait(futures)
