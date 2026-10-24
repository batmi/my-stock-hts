"""보유 포지션 청산 규칙 — 실매매·가상투자·백테스트가 함께 부르는 **순수 판단 계층**.

[왜 · 2026-10-16] 청산 판정이 두 벌이었다.
    실매매   : engine.DefaultStrategy.analyze_sell
    백테스트 : portfolio_backtest.decide_sell
 일부 산식(ATR 손절·실효 콜백·발동선·이익보호선)은 공유했지만 **우선순위 체인**은 각자
 다시 적혀 있었고, tests/test_exit_parity.py 가 둘을 나란히 돌려 어긋남을 잡는 구조였다.
 재구현은 갈라진다 — 실제로 이 저장소는 그 체인에서 같은 결함(시간청산 유예가 TS 를
 삼키던 elif)을 두 곳에서 따로 고쳤다. 이제 체인은 여기 하나다. 두 호출부는 각자의
 데이터(실시간가·일봉 행)와 사유 문자열 형식만 어댑터로 연결한다.

[이 함수가 하지 않는 것] API 조회·DB 저장·알림·주문. 입력은 전부 인자로 받는다.

[우선순위 — 위에서 먼저 걸리는 것이 이긴다]
  1) 익절 계열(반익절 → 익절 → 수익보존)   ※ TAKE_PROFIT_RATE > 0 일 때만(기본 OFF)
  2) 가격 손절선 — 이익보호선 > 본전청산(BEP) > ATR/고정 손절 (선 하나, 사유만 다르다)
  3) 시간청산 — 매수 계열 상태 + 상방 모멘텀(5일 고점 ≥ 10일 고점)이면 유예
  4) 트레일링 스탑(샹들리에) — 3)이 유예돼도 판정한다(체인을 소비하지 않는다)
  5) RSI 과열 익절 → 방어적 반매도       ※ 기본 OFF, 일봉이 있을 때만
  6) '매도' 상태 / 점수하락+60일선 이탈(추세 이탈)  ※ 일봉이 있을 때만
     역추세 보유는 유예 기간 안이면 6)을 보류한다

[두 호출부 사이에 **남아 있는** 규칙 차이 — 이름 붙은 스위치로 드러낸다]
  require_judged      실매매 True: 상태 판정이 불가('-')면 점수매도를 내지 않는다
                      (2026-09-06, '모름'을 '이탈'로 읽던 결함). 백테스트는 종전대로 False.
  state_sell_always   실매매 True: '매도' 상태면 SELL_SCORE 와 무관하게 판다. 백테스트는
                      sell_check(=0) < SELL_SCORE 로 판정해 SELL_SCORE ≤ 0 이면 팔지 않는다.
 (시간청산 일수 ≤ 0 을 '끔'으로 읽는 것도 호출부 몫이다 — 실매매 어댑터만 그렇게 읽는다.)
 둘 다 기본 설정에서는 결과가 같다(SELL_SCORE=4.0, 백테스트 보유 중 상태 '-' 0건 —
 tests/test_exit_rules.py 가 잰다). 차이를 없애는 것은 판단 변경이라 이번 리팩터링에
 섞지 않았다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 사유 코드 — 호출부가 자기 형식의 문자열로 바꾼다(실매매 로그·DB, 백테스트 집계 어휘).
HALF_TAKE_PROFIT = "half_take_profit"
TAKE_PROFIT = "take_profit"
PROFIT_PRESERVE = "profit_preserve"
PROFIT_LOCK = "profit_lock"
BEP_STOP = "bep_stop"
ATR_STOP = "atr_stop"
STOP_LOSS = "stop_loss"
TIME_STOP = "time_stop"
TRAILING_STOP = "trailing_stop"
RSI_TAKE_PROFIT = "rsi_take_profit"
DEFENSIVE_HALF = "defensive_half"
STATE_SELL = "state_sell"
TREND_EXIT = "trend_exit"

PRICE_STOP_CODES = (PROFIT_LOCK, BEP_STOP, ATR_STOP, STOP_LOSS)
GRACE_STATES = ("매수", "강매수", "역매수", "상승", "대기")


@dataclass(frozen=True, slots=True)
class ExitRules:
    """한 판단에 쓰는 청산 설정 — 호출부가 config/룰/실험 파라미터에서 조립해 고정한다."""
    use_atr: bool = True
    use_time_stop: bool = True
    time_stop_days: int = 20
    time_stop_min: float = 0.0
    time_stop_strict: bool = False        # [실험 · bear_swing] 유예 없는 정산형 시간청산
    ts_act: float = 10.0
    ts_callback: float = 5.0
    ts_atr_mult: float = 3.5
    ts_breakeven: bool = False
    sell_score_limit: float = 4.0
    profit_lock_use: bool = False
    profit_lock_min_mfe: float | None = None
    profit_lock_giveback: float | None = None
    # 본전청산(BEP) — 이 계층이 직접 올릴 때. 호출부가 이미 올린 손절률을 주면 bep_preset 으로.
    bep_use: bool = False
    bep_activation: float = 5.0
    bep_stop: float = 0.5
    # 익절 계열·RSI 익절·방어적 반매도·역추세 유예 — 실매매 전용(기본 OFF, 백테스트 미모델)
    tp_rate: float = 0.0
    half_tp_use: bool = False
    tp_rsi: float = 0.0
    super_use: bool = False
    super_score: float = 8.0
    super_w52: float = 90.0
    super_tp_rsi: float = 90.0
    defensive_half_use: bool = False
    mr_grace_loss_rate: float = -7.0
    # 두 호출부의 남은 규칙 차이(모듈 독스트링)
    require_judged: bool = True
    state_sell_always: bool = True


@dataclass(frozen=True, slots=True)
class ExitInputs:
    """한 시점의 포지션·시세·지표. 시점은 호출부가 정한다(실매매=지금, 백테스트=그날)."""
    price: float                  # 판정 가격(실매매 현재가, 백테스트 종가/장중가)
    avg: float                    # 평균 매입가
    high: float                   # 보유 이후 최고가(트레일링 앵커). 0 이면 '모름'
    profit_rate: float            # 수익률(%) — 호출부 정의 그대로 받는다
    holding_days: int = 0
    sl_rate: float = 0.0          # 기본 손절률(%) — ATR 적용 손절 또는 고정 손절
    atr_applied: bool = False
    bep_preset: bool = False      # sl_rate 가 이미 본전청산으로 올라와 있다(백테스트 lot 경로)
    atr: float | None = None
    state: str = ""
    state_reason: str = ""
    score: float = 0.0            # 점수매도 판정 점수(반올림된 값)
    ema60: float | None = None
    roll_high_5: float | None = None   # None = 유예 판정 불가(이력 부족) → 유예 없음
    roll_high_10: float | None = None
    has_bars: bool = True         # 일봉이 있어 5)·6) 을 판정할 수 있는가
    rsi: float | None = None
    psar: float | None = None
    ema_5: float | None = None
    w52_pos: float = 0.0
    already_half_sold: bool = False
    is_mr_holding: bool = False


@dataclass(slots=True)
class ExitDecision:
    sell: bool
    code: str | None              # 사유 코드(위 상수). 보유면 None
    sell_ratio: float = 1.0
    applied_sl_rate: float = 0.0  # 실제로 적용된 손절선(BEP·이익보호 반영)
    is_bep: bool = False
    is_lock: bool = False
    max_profit_rate: float = 0.0
    ts: dict | None = None        # engine.compute_trailing_stop 결과(표시용 청산선 포함)
    time_stop_due: bool = False   # 시간청산 조건은 섰다(유예됐을 수 있다)
    time_stop_graced: bool = False
    mr_grace_applied: bool = False
    is_super: bool = False
    actual_tp_rsi: float = 0.0
    marks_half_sold: bool = False  # 다음 상태 제안: 반매도 기록을 남겨야 한다
    notes: tuple = field(default_factory=tuple)


def evaluate_exit(inp: ExitInputs, rules: ExitRules) -> ExitDecision:
    """보유 포지션 하나의 청산 판정(순수 함수)."""
    # 산식은 engine 이 단독 보유한다(ATR 손절·실효 콜백·발동선·이익보호선) — 여기서 다시 쓰지 않는다.
    from modules.auto_trade.engine import compute_trailing_stop, profit_lock_stop_rate

    price, avg, high, pr = inp.price, inp.avg, inp.high, inp.profit_rate
    sl_rate = inp.sl_rate
    is_bep = bool(inp.bep_preset)

    max_profit = ((high - avg) / avg) * 100 if (high and high > 0 and avg and avg > 0) else 0.0

    # ── 손절선 상향: 본전청산 → 이익보호(더 높은 쪽만 남긴다)
    if rules.bep_use and high and high > 0 and avg and avg > 0:
        if max_profit >= rules.bep_activation and sl_rate < rules.bep_stop:
            sl_rate, is_bep = rules.bep_stop, True
    is_lock = False
    if rules.profit_lock_use and avg and avg > 0:
        lock = profit_lock_stop_rate(max_profit, rules.profit_lock_min_mfe, rules.profit_lock_giveback)
        if lock is not None and lock > sl_rate:
            sl_rate, is_lock, is_bep = lock, True, False

    ts = compute_trailing_stop(high, avg, price, ind={"atr": inp.atr},
                               ts_activation=rules.ts_act, ts_callback=rules.ts_callback,
                               ts_atr_mult=rules.ts_atr_mult, use_atr_stop=rules.use_atr,
                               breakeven_mode=rules.ts_breakeven)

    d = ExitDecision(sell=False, code=None, applied_sl_rate=sl_rate, is_bep=is_bep, is_lock=is_lock,
                     max_profit_rate=max_profit, ts=ts)

    def _sell(code, ratio=1.0):
        d.sell, d.code, d.sell_ratio = True, code, ratio
        d.marks_half_sold = ratio < 1.0
        return d

    # 1) 익절 계열 — 반익절 후 잔여(Let profit run)는 체인을 소비하지 않는다
    #  [버그 이력] 종전에는 목표 도달 시 pass 로 체인이 끝나 아래 손절/시간청산/트레일링 스탑이
    #   전부 차단되어, 고수익 구간에서 TS 가 영원히 발동하지 않았다. 해당 케이스를 조건에서
    #   제외해 체인이 계속 흐르도록 했다. 수익보존 하한 +0.5% 는 익절 목표를 3% 이하로 둔 경우
    #   '수익보존' 명목의 손실 매도를 막는다.
    tp = rules.tp_rate
    half = rules.half_tp_use
    if tp > 0 and half and not inp.already_half_sold and pr >= tp / 2.0:
        return _sell(HALF_TAKE_PROFIT, 0.5)
    if tp > 0 and pr >= tp and not (half and inp.already_half_sold):
        return _sell(TAKE_PROFIT)
    if (tp > 0 and half and inp.already_half_sold and high and high > 0
            and max_profit >= tp and pr <= max(tp - 3.0, 0.5)):
        return _sell(PROFIT_PRESERVE)

    # 2) 가격 손절선
    if sl_rate != 0 and pr <= sl_rate:
        if is_lock:
            return _sell(PROFIT_LOCK)
        if is_bep:
            return _sell(BEP_STOP)
        return _sell(ATR_STOP if (rules.use_atr and inp.atr_applied) else STOP_LOSS)

    # 3) 시간청산(유예 가능) — 유예돼도 4) 를 막지 않는다
    #  [체인을 소비하지 않는다 · 2026-09-01] 종전에는 TS 절이 elif 였다. 시간청산 조건이 참인데
    #   **유예**되면 사유가 빈 채로 체인이 끝나, 그날 트레일링 스탑 판정이 통째로 건너뛰어졌다
    #   — 승자가 무너지는 순간 청산을 미루는 방향이다. 백테스트 10년에서 삼킨 사례 0건이고
    #   TIME_STOP_MIN_PROFIT_RATE 를 5·10 으로 올려 유예를 146·417건까지 늘려도 0이다(유예 조건
    #   '최근 5일 고점 ≥ 10일 고점'과 TS 발동 '고점 대비 콜백만큼 하락'이 거의 배타적이다).
    #   그래도 그 안전이 유예 조건의 형태에 기대고 있어 함정을 없앴다.
    #  (일수 ≤ 0 을 '끔'으로 읽는 것은 호출부 몫이다 — 실매매는 끄고, 백테스트 cfg 는 그대로 쓴다)
    if rules.use_time_stop and inp.holding_days >= rules.time_stop_days and pr < rules.time_stop_min:
        d.time_stop_due = True
        grace = (inp.state in GRACE_STATES and inp.roll_high_5 is not None and inp.roll_high_10 is not None
                 and inp.roll_high_5 >= inp.roll_high_10)
        if rules.time_stop_strict:
            grace = False
        if not grace:
            return _sell(TIME_STOP)
        d.time_stop_graced = True

    # 4) 트레일링 스탑
    if ts and ts.get("triggered"):
        return _sell(TRAILING_STOP)

    if not inp.has_bars:
        return d

    # 5) RSI 과열 익절 · 방어적 반매도 (기본 OFF)
    d.actual_tp_rsi = rules.tp_rsi
    if rules.super_use and inp.score >= rules.super_score and inp.w52_pos >= rules.super_w52:
        d.actual_tp_rsi, d.is_super = rules.super_tp_rsi, True
    if rules.tp_rsi > 0 and inp.rsi is not None and inp.rsi > d.actual_tp_rsi:
        return _sell(RSI_TAKE_PROFIT)
    if (rules.defensive_half_use and not inp.already_half_sold and inp.state != "매도"
            and inp.psar is not None and inp.ema_5 is not None
            and pr >= rules.time_stop_min and price < inp.psar and price < inp.ema_5):
        return _sell(DEFENSIVE_HALF, 0.5)

    # 6) '매도' 상태 / 점수하락 + 추세 구조 훼손(60일선 이탈)
    #  [추세추종] 점수 하락 단독으로는 팔지 않는다 — 주청산(샹들리에 TS)의 fat-tail 추종을
    #   점수 매도가 조기에 잘라내지 않도록 구조 훼손을 함께 요구한다(sell-structure 버팀목).
    #  [판정이 성립했을 때만 판다 · 2026-09-06 — require_judged]
    #   일봉 '그릇'이 있어도 내용이 못 쓸 수 있다 — 마지막 봉 결측(yfinance 가 최신 종가를 자주
    #   비운다)이나 상장 초기라 60일선이 없을 때. 그때 채점은 '모름'이 아니라 **숫자**를 주고
    #   (실측: 지표 전무 → 1.0~1.5점) ema60 이 None 이라 structure_broken 도 참이 되어, 점수매도가
    #   전량 청산을 냈다 — "추세이탈(-/점수하락+60일선이탈) [점수:1.0, RSI:-, ADX:-, CCI:-]".
    #   판정 가능 여부는 classify_stock_state 가 단독으로 안다(입력이 없으면 '-'). 그 값을 쓴다.
    #   [백테스트] 그쪽 EMA60 은 ewm(adjust=False)라 0번 봉부터 값이 있고 결측이면 NaN(None 아님)이라
    #   이 팔은 백테스트에서 죽은 코드다 — 그래서 decide_sell 은 종전대로 False 로 부른다.
    fire, code = trend_exit_fires(inp.state, inp.score, inp.ema60, price, rules.sell_score_limit,
                                  require_judged=rules.require_judged,
                                  state_sell_always=rules.state_sell_always)
    if fire:
        if (inp.is_mr_holding and inp.holding_days <= rules.time_stop_days
                and pr > rules.mr_grace_loss_rate):
            d.mr_grace_applied = True       # 역추세 보유 유예 기간 — 보류
            return d
        return _sell(code)
    return d


def trend_exit_fires(state, score, ema60, price, sell_score_limit, require_judged=True, state_sell_always=True):
    """6) '매도' 상태 / 점수하락 + 60일선 이탈 — (발동, 사유 코드). evaluate_exit 와 화면이 같이 쓴다.

    [2026-10-24] 개별 종목 진단 화면(analysis.diagnose_stock)의 '보유 판단'이 이 식을 따로 들고 있었고,
     2026-09-06 의 require_judged(판정 불가 '-' 상태에서는 점수매도를 내지 않는다)를 따라오지 않아
     상장 초기·결측 종목을 '매도(추세이탈)'로 보였다 — 시스템은 그 종목을 팔지 않는다.
    기본 인자는 실매매 규약(require_judged·state_sell_always 둘 다 True)이다. 백테스트는 False 로 부른다
    (모듈 독스트링의 '남아 있는 규칙 차이'). 백테스트는 '매도' 상태의 점수를 0 으로 넘긴다(sell_check) —
    state_sell_always=False 여도 같은 비교로 재현된다.
    """
    judged = state not in ("", "-")
    structure_broken = ema60 is None or price < ema60
    if state == "매도":
        return bool(state_sell_always or score < sell_score_limit), STATE_SELL
    return bool((judged or not require_judged) and score < sell_score_limit and structure_broken), TREND_EXIT
