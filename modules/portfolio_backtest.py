"""다종목 포트폴리오 백테스트 (슬롯 경쟁·현금 제약·히트 캡 재현).

기존 ``backtest.simulate_strategy``는 '한 종목 × 계좌 전액'을 가정해 종목별 전략 검증에는
맞지만, 실제 운용에서 성과를 좌우하는 세 가지를 재현하지 못한다.

  1) 슬롯 경쟁(기회비용) — 동시에 N종목만 보유하므로 좋은 신호가 나도 자리가 없으면 못 산다.
  2) 현금 제약 — 피라미딩에 쓴 현금은 다른 종목 신규 진입에 못 쓴다.
  3) 포트폴리오 히트 캡 — 보유 전체의 오픈 리스크 합이 한도를 넘으면 신규 매수가 막힌다.

이 모듈은 하나의 계좌로 N슬롯을 굴리며 위 셋을 모두 반영한다. 진입·청산 판정은
``backtest.calculate_daily_status``(= ``analysis.classify_stock_state``/``calculate_score``)를
그대로 쓰고, 청산 체인(ATR 손절 → BEP → 시간청산 → 샹들리에 TS → 점수매도)과 사이징 3층
결합은 ``simulate_strategy``·``engine.RiskManager.allocate_budget``과 동일 순서·조건으로 맞췄다.

정합성 검증: 슬롯 1·기초비중 25%·히트캡 OFF로 돌려 종목별로 ``simulate_strategy``와 비교하면
관심종목 50개 기준 수익률 상관 0.9988(평균차 -0.12%p), MDD 평균 동일, 청산 805건 vs 803건이다.

[장중 스캔 모드 · 2026-08-16] 실매매는 진입·청산·증액을 **감시 주기마다 실시간가로** 판정한다.
 이 시뮬레이터는 오래 '하루 한 번, 종가'만 낼 수 있었고 그 차이는 작지 않다(실측: 청산 축만
 봐도 전체창 수익 94~114% vs 136~217%). 분봉 캐시(modules/intraday_bars)가 있으면 세 다리를
 모두 봉 단위로 판정한다 — intraday_bars(청산) · intraday_status+intraday_entry(진입) ·
 그 둘이 함께 있으면 증액까지. 증액이 봉 단위가 되면 평단이 오른 뒤 같은 날 다시 발동선에
 닿아 1 → 2 → 3차가 이어지는 실매매 동작이 그대로 재현된다.
 인자를 주지 않으면 종전(종가) 동작 그대로다.
"""
import logging
import math

import pandas as pd
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn

import config
from core import context
from core import indicators
from core import utils
from modules import backtest
from core import trading_cost

logger = logging.getLogger(__name__)

# 분봉 캐시는 선택 의존이다 — 없으면 종가 모델로 돌아간다(감사 도구 전용 경로가 아니라
#  메뉴 백테스트의 기본 경로가 되므로, 임포트 실패가 백테스트 전체를 막으면 안 된다).
try:
    from modules import intraday_bars as intraday_bars_mod
except Exception:      # pragma: no cover - 선택 의존
    intraday_bars_mod = None


# ==========================================================
# 시뮬레이션 코어
# ==========================================================
def precompute_status(dfs, thresholds):
    """종목별·일자별 (raw_score, sell_check_score, can_buy_state, state, reason)을 미리 계산한다.

    상태 판정은 하루 단위로 결정되므로 조합(슬롯 수·피라미딩 차수)을 바꿔 반복 실행할 때
    매번 다시 계산할 필요가 없다. 50종목 3년 기준 1초 남짓이며 이후 실행은 순수 루프가 된다.
    """
    out = {}
    for code, df in dfs.items():
        status, prev = {}, None
        for row in df.to_dict("records"):
            try:
                status[str(row["date"])] = backtest.calculate_daily_status(row, prev, thresholds=thresholds)
            except Exception:
                status[str(row["date"])] = (0.0, 0.0, False, "-", "데이터 부족")
            prev = row
        out[code] = status
    return out


def _atr_stop_rate(atr, price, atr_mult, day=None):
    """진입 시점 ATR 손절률(%).

    [SSOT 2026-08-09] 산식·캡은 engine.atr_stop_rate 가 단독 보유한다. 여기서 식을 다시
    쓰면 캡(MAX_ATR_STOP_LOSS_RATE)을 조정할 때 실매매·백테스트·포트폴리오 백테스트가
    서로 다른 손절선으로 돌 수 있다 — 캡의 타당성을 이 백테스트로 검증하려는 마당에
    그 전제가 깨지면 결과를 실매매에 옮길 수 없다. 반환 규약(0.0)만 여기서 맞춘다.
    """
    from modules.auto_trade import engine as _eng
    # [동적 손절 캡] 그 날짜의 지수 변동성 배율을 주입한다. 실매매는 trader가 주기마다
    #  모듈 상태를 갱신하고, 여기서는 과거 각 시점의 값을 직접 준다 — 같은 산식이다.
    ratio = backtest._VOL_REGIME_STATE["by_date"].get(day) if day else None
    return _eng.atr_stop_rate(atr, price, atr_mult=atr_mult, vol_ratio=ratio) or 0.0


# 가격이 선에 닿아서 나가는 청산 사유. 실매매에서 이들만 장중 실시간가로 트리거되고,
# 시간청산·점수하락은 판정 자체가 하루 단위다.
PRICE_EXIT_REASONS = ("손절", "ATR손절", "본전청산", "이익보호", "트레일링스탑")

# 청산으로 세는 사유 **전부**. 시뮬레이터의 승률·PF 분모(`sells`)와 감사 도구의 표본이
#  같은 어휘를 봐야 한다 — tools/audit_common.SELL_REASONS 가 이 값을 그대로 쓴다.
#  [2026-08-23] 종전에는 이 목록이 아래 sells 조립부에 리터럴로 박혀 있었고 "이익보호"가
#   빠져 있었다. 뒤따르는 `profit_amt != 0` 절이 가려 실제 표본은 온전했지만(그리고
#   PROFIT_LOCK_USE 는 기본 OFF다), 사유가 늘 때마다 갈라지는 자리였다.
#  [2026-08-25] "데이터종료"를 넣는다. 봉이 끊긴 종목의 포지션을 강제 청산하는 사유인데,
#   표본에서 빼면 종전의 결함(슬롯은 묶이고 손익은 어디에도 안 잡힌다)이 이름만 바꿔
#   되살아난다. 상장폐지 종목을 섞어 재는 감사(tools/audit_universe.py 축 B)에서는 이것이
#   **실제 손실**이므로 승률·PF 분모에 들어가야 한다.
EXIT_REASONS = ("ATR손절", "손절", "본전청산", "이익보호", "시간청산",
                "트레일링스탑", "점수하락", "교체", "데이터종료")


def decide_sell(*, price, high, avg, sl_rate, atr_applied, is_bep, holding_days,
                state, state_reason, raw_score, sell_check, ema60, atr,
                roll_high_5=0.0, roll_high_10=0.0, cfg=None):
    """백테스트의 청산 판정 — (sell: bool, reason: str).

    [왜 함수로 빼두는가] 이 판정은 실매매의 engine.DefaultStrategy.analyze_sell 과
    **별도 구현**이다. 두 구현이 어긋나면 백테스트 수치가 실매매를 설명하지 못하고,
    그 수치로 정한 파라미터의 근거가 통째로 흔들린다. 호출 가능한 형태여야 두 구현을
    같은 입력으로 나란히 돌려 대조할 수 있다(tools/audit_exit_parity.py).
    """
    c = cfg or {}
    use_atr = c.get("use_atr", True)
    use_time_stop = c.get("use_time_stop", True)
    time_stop_days = c.get("time_stop_days", 20)
    # [패리티] 실매매(engine.analyze_sell)는 TIME_STOP_MIN_PROFIT_RATE 를 문턱으로 쓰는데
    #  여기는 0(손실일 때만)이 하드코딩돼 있어 그 다이얼을 백테스트로 잴 수 없었다.
    #  config 기본값이 0.0이라 기본 동작은 종전과 같다.
    time_stop_min = c.get("time_stop_min", 0.0)
    ts_act = c.get("ts_act", 10.0)
    ts_callback = c.get("ts_callback", 5.0)
    # [SSOT] 폴백 리터럴은 config 정본(3.5)·실매매(engine.analyze_sell)와 맞춘다.
    #  종전 3.0은 키가 빠진 호출에서만 조용히 발현해, 백테스트만 더 좁은 콜백으로
    #  돌게 만든다(청산이 빨라져 fat-tail이 잘린다).
    ts_atr_mult = c.get("ts_atr_mult", 3.5)
    ts_breakeven = c.get("ts_breakeven", False)
    sell_score_limit = c.get("sell_score_limit", 4.0)

    loss_rate = (price - avg) / avg * 100
    max_profit = (high - avg) / avg * 100

    # [이익 보호선] 무장 전 구간 전용. 실매매(analyze_sell)와 같은 식을 써야 이 백테스트로
    #  정한 파라미터가 의미를 갖는다 — 산식은 engine.profit_lock_stop_rate가 단독 보유한다.
    is_lock = False
    if c.get("profit_lock_use", False):
        from modules.auto_trade.engine import profit_lock_stop_rate
        lock = profit_lock_stop_rate(max_profit, c.get("profit_lock_min_mfe"),
                                     c.get("profit_lock_giveback"))
        if lock is not None and lock > sl_rate:
            sl_rate, is_lock = lock, True

    sell, reason = False, ""
    if sl_rate != 0 and loss_rate <= sl_rate:
        sell = True
        if is_lock:
            reason = "이익보호"
        else:
            reason = "본전청산" if is_bep else ("ATR손절" if (use_atr and atr_applied) else "손절")
    else:
        # [체인을 소비하지 않는다] 종전에는 이 절이 elif 였다. 시간청산 조건이 참인데
        #  **유예**되면 reason 이 비어 있는 채로 체인이 끝나, 아래 트레일링 스탑 판정이
        #  그날 통째로 건너뛰어졌다 — 승자가 무너지는 순간 청산을 미루는 방향이다.
        #  바로 위 반익절 절이 같은 형태로 한 번 고쳐진 적이 있다(그 주석 참조).
        #  [실측 2026-09-01] 현재 설정에서 삼킨 사례는 0건이고, TIME_STOP_MIN_PROFIT_RATE 를
        #   5·10 으로 올려 유예를 146·417건까지 늘려도 여전히 0이다. 우연이 아니라 구조다 —
        #   유예 조건(최근 5일 고점 ≥ 10일 고점 = 고점을 최근에 찍었다)과 TS 발동 조건
        #   (고점 대비 콜백만큼 하락)이 논리적으로 거의 배타적이기 때문이다. 그래서 이 수정은
        #   **수치를 바꾸지 않는다.** 그럼에도 고치는 이유는 그 안전이 유예 조건의 형태에
        #   기대고 있어서, 그 조건을 손대는 순간 되살아나는 함정이기 때문이다.
        if use_time_stop and holding_days >= time_stop_days and loss_rate < time_stop_min:
            # 시간청산 유예: 매수 계열 상태 유지 + 상방 모멘텀(최근 5일 고점 ≥ 10일 고점)
            grace = state in ("매수", "강매수", "역매수", "상승", "대기") and \
                roll_high_5 >= roll_high_10
            if not grace:
                sell, reason = True, "시간청산"

        if not sell and high > 0 and (ts_breakeven or max_profit >= ts_act):
            drop = (high - price) / high * 100
            callback = ts_callback
            if use_atr and atr and atr > 0:
                dynamic = (atr * ts_atr_mult / high) * 100
                # [SSOT] 반납 상한(TS_MAX_GIVEBACK_RATIO)은 engine.giveback_callback_cap이 단독
                #  보유한다. 실매매(compute_trailing_stop)·단일종목 백테스트는 이미 이 캡을 쓰는데
                #  포트폴리오 백테스트만 순수 샹들리에로 돌고 있었다. 캡이 없으면 콜백이 더 커져
                #  청산이 늦고, 그만큼 백테스트가 실매매보다 낙관적으로 나온다
                #  (실측 2026-08-04: 청산 판정 불일치의 96%가 이 한 가지 · 3년 수익 +82.8%p 과대).
                from modules.auto_trade.engine import effective_callback
                callback = effective_callback(ts_callback, dynamic, max_profit)
            # [손익분기 연동] 되돌림 한 번(3.5 ATR)을 맞고도 본전 이상인 지점부터 무장한다.
            #  [SSOT] 발동선 산식은 engine.breakeven_activation_rate가 단독 보유한다 —
            #  백테스트가 실매매와 다른 식을 쓰면 튜닝 결과가 무의미해진다(콜백 캡과 같은 규약).
            if ts_breakeven:
                from modules.auto_trade.engine import (breakeven_activation_rate,
                                                       ts_activation_atr_mult)
                armed = max_profit >= breakeven_activation_rate(atr, avg, ts_callback,
                                                                ts_activation_atr_mult(), use_atr)
            else:
                armed = max_profit >= ts_act
            if armed and drop >= callback:
                sell, reason = True, "트레일링스탑"

    if not sell:
        # 점수 매도는 추세 구조 훼손(주가<60일선 또는 '매도' 상태) 동시 충족 시에만
        structure_broken = (state == "매도") or ema60 is None or price < ema60
        if sell_check < sell_score_limit and structure_broken:
            sell = True
            reason = state_reason if (sell_check == 0 and raw_score > 0) else "점수하락"
    return sell, reason


def build_sell_cfg(sell_cfg=None):
    """decide_sell 에 넘길 청산 설정 dict — config 에서 읽는 **단일 조립 지점**.

    [왜] 이 dict의 키가 하나라도 빠지면 decide_sell 은 조용히 자기 기본값으로 돌아간다.
    실매매(engine.analyze_sell)는 config 를 직접 읽으므로 그쪽만 새 값으로 돌고, 결과는
    '코드가 아니라 하네스 때문에 생긴 불일치'다. 실제로 tools/audit_exit_parity.py 가
    time_stop_min 을 빠뜨리고 있었고, TIME_STOP_MIN_PROFIT_RATE 를 0이 아닌 값으로 두면
    거짓 불일치가 났다(기본값이 0.0이라 드러나지 않았을 뿐이다).

    조립부가 둘이면 같은 실수가 반복되므로 여기 하나만 둔다. 청산 규칙에 스위치를 추가할
    때는 이 함수만 고치면 된다 — 빠뜨리면 tests/test_exit_parity.py 가 잡는다.
    """
    s = sell_cfg if sell_cfg is not None else config.SELL_STRATEGY
    time_stop_days = s.get("TIME_STOP_DAYS", 20)
    return {
        "use_atr": s.get("USE_ATR_STOP", True),
        "use_time_stop": s.get("TIME_STOP_USE", True) and time_stop_days > 0,
        "time_stop_days": time_stop_days,
        "time_stop_min": s.get("TIME_STOP_MIN_PROFIT_RATE", 0.0),
        "ts_act": s.get("TRAILING_STOP_ACTIVATION_RATE", 10.0),
        "ts_callback": s.get("TRAILING_STOP_CALLBACK_RATE", 5.0),
        "ts_atr_mult": s.get("TRAILING_ATR_MULTIPLIER", 3.5),
        "ts_breakeven": str(s.get("TS_ACTIVATION_MODE", "fixed")).lower() == "breakeven",
        "sell_score_limit": s.get("SELL_SCORE", 4.0),
        "profit_lock_use": s.get("PROFIT_LOCK_USE", False),
        "profit_lock_min_mfe": s.get("PROFIT_LOCK_MIN_MFE", 25.0),
        "profit_lock_giveback": s.get("PROFIT_LOCK_GIVEBACK", 0.5),
    }


def _weighted_sl(position, default_sl):
    """보유 lot들의 수량가중 평균 ATR 손절률. 실매매의 매수기록 가중평균과 같은 규칙."""
    total_qty = weighted = 0.0
    for lot in position["lots"]:
        if lot["qty"] > 0 and lot["sl"] != 0.0:
            total_qty += lot["qty"]
            weighted += lot["qty"] * lot["sl"]
    return (weighted / total_qty, True) if total_qty > 0 else (default_sl, False)


def allocate_amount(equity, cash, invest_ratio, sl_rate, atr, price):
    """engine.RiskManager.allocate_budget과 동일한 3층 min 결합(기초비중·리스크·변동성)."""
    base_amt = int(equity * invest_ratio)
    amount = base_amt

    risk_per_trade = getattr(config, "SYSTEM_RISK_PER_TRADE", 4.0)
    if risk_per_trade > 0 and sl_rate and abs(sl_rate) > 0:
        params = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
        try:
            gap_buffer = max(1.0, float(params.get("GAP_RISK_BUFFER", 1.2)))
        except (TypeError, ValueError):
            gap_buffer = 1.2
        max_loss = equity * (risk_per_trade / 100.0)
        amount = min(amount, int(max_loss / ((abs(sl_rate) / 100.0) * gap_buffer)))

    if getattr(config, "USE_VOLATILITY_TARGETING", True) and atr and price > 0:
        annual_vol = (atr / price) * math.sqrt(252)
        if annual_vol > 0:
            scale = getattr(config, "TARGET_VOLATILITY", 0.25) / annual_vol
            scale = max(getattr(config, "VOLATILITY_SCALING_MIN", 0.4),
                        min(getattr(config, "VOLATILITY_SCALING_MAX", 2.0), scale))
            amount = min(amount, min(int(base_amt * scale), base_amt))

    return max(0, min(amount, cash))


def _trend_quality_cached(df, lookback):
    """{일자: 추세품질} — 같은 DataFrame·같은 룩백이면 다시 계산하지 않는다.

    감사 도구는 같은 dfs로 run_portfolio를 수천 번 부른다. 종목 44개 기준 한 번에
    120ms라 캐시가 없으면 짧은 창일수록 시뮬레이션 자체보다 순위 준비가 더 비싸진다.
    DataFrame.attrs는 판다스가 메타데이터용으로 두는 자리라 계산 결과에 영향이 없다.
    """
    key = f"_tq_map_{int(lookback)}"
    try:
        cached = df.attrs.get(key)
        if cached is not None:
            return cached
    except AttributeError:
        return indicators.trend_quality_map(df, lookback)
    out = indicators.trend_quality_map(df, lookback)
    df.attrs[key] = out
    return out


# 백테스트가 **재현하지 못하는** 실매매 청산 경로. decide_sell 에 아예 없는 것들이다.
#  (키, 재현 불가로 보는 조건, 사람이 읽을 이름)
#
# [왜 여기 있나] 전부 기본 OFF라 지금은 무해하다. 그러나 켜는 순간 백테스트는 그 청산을
#  **한 번도 밟지 않은 채** 그럴듯한 수익률을 내놓는다 — 아무도 모르는 것이 가장 나쁘다.
#  이 저장소는 정확히 그 형태의 결함을 이미 여러 번 겪었다(히트의 BEP 토글·이익보호선,
#  audit_exit_parity 의 time_stop_min 누락). 선언은 시간이 지나면 거짓이 되므로,
#  '알고 남긴 것'을 주석이 아니라 **경고**로 남긴다.
UNMODELED_SELL_TOGGLES = (
    ("TAKE_PROFIT_RATE", lambda v: (v or 0) > 0, "고정 익절"),
    ("TAKE_PROFIT_RSI", lambda v: (v or 0) > 0, "RSI 과열 익절"),
    ("HALF_TAKE_PROFIT_USE", bool, "반익절"),
    ("DEFENSIVE_HALF_SELL_USE", bool, "방어적 반매도"),
)


# 실매매에는 **늘 켜져 있는데** 백테스트가 아예 재현하지 못하는 진입 게이트.
#  [왜 매도 토글과 따로인가] 위 UNMODELED_SELL_TOGGLES 는 전부 기본 OFF라 '켜면 갈라진다'는
#   예방 경고였다. 이쪽은 반대다 — **기본값이 켜짐**이라 지금 이 순간에도 갈라져 있다.
#   체결강도·호가잔량비는 일봉에 존재하지 않는 값이고(실시간 체결·호가창), 개장 직후
#   보류는 종가 모델에 시각이 없어 밟을 수 없다. 백테스트는 이 셋이 **한 번도 막지 않은
#   세계**를 굴린다.
#  [크기] 라즈베리파이 관찰 모드(mode 1)의 신호 원장 실측, 2026-08-19~09-01:
#   매수 상태였던 (일,종목) 52건 중 **15건(28.8%)이 완전 차단**됐다
#   (체결강도 13 · 호가잔량비 4 · 상관 2, 중복 계상). 작지 않다.
#   ※ 그 기계는 시장 필터를 끄고 돌던 중이라 실전 비율과 같다고 볼 수는 없다.
#     자릿수만 참고하고, 게이트가 이익인지 손해인지는 tools/audit_gate_forward.py 가 답한다.
#  [왜 경고인가] 주석으로만 두면 시간이 지나 거짓이 된다 — 이 저장소가 반복해서 겪은
#   형태다. 매도 측과 정확히 같은 문(warn_if_unmodeled)에서 함께 알린다.
UNMODELED_ENTRY_GATES = (
    ("BUY_VOL_STRENGTH", lambda v: (v or 0) > 0, "체결강도 게이트"),
    ("BUY_ASK_BID_RATIO", lambda v: (v or 0) > 0, "호가잔량비 게이트"),
)


def unmodeled_entry_features():
    """지금 켜져 있는데 백테스트가 재현하지 못하는 **진입** 게이트 이름 목록."""
    at = getattr(config, 'ANALYSIS_THRESHOLDS', {}) or {}
    on = [name for key, is_on, name in UNMODELED_ENTRY_GATES if is_on(at.get(key))]
    # 개장 직후 보류는 config 최상위에 있다(종목별 룰이 없다).
    if getattr(config, 'SYSTEM_ENTRY_OPEN_DELAY_USE', False) and \
            int(getattr(config, 'SYSTEM_ENTRY_OPEN_DELAY_MINUTES', 0) or 0) > 0:
        # 종가 모델에는 시각이 없어 무동작이지만, 장중 스캔 모드에서는 실제로 갈린다.
        on.append("개장 직후 진입 보류")
    return on


def unmodeled_sell_features(sell_cfg=None):
    """지금 켜져 있는데 백테스트가 재현하지 못하는 청산 기능 이름 목록."""
    s = config.SELL_STRATEGY if sell_cfg is None else sell_cfg
    return [name for key, is_on, name in UNMODELED_SELL_TOGGLES if is_on(s.get(key))]


def _announce(msg, loud=True):
    logger.warning(msg)
    try:
        config.console.print(f"[bold yellow]{msg}[/]" if loud else f"[dim yellow]※ {msg}[/dim yellow]")
    except Exception:
        print(msg)


def warn_if_unmodeled(where="백테스트"):
    """재현 불가 기능이 켜져 있으면 알린다. 조용히 지나가지 않는 것이 요점이다.

    청산(기본 OFF · 켜면 갈라진다)과 진입(기본 ON · 지금 갈라져 있다)을 함께 본다.
    반환은 종전 호출부 호환을 위해 **청산 목록**이다(진입은 알리기만 한다).
    """
    on = unmodeled_sell_features()
    if on:
        _announce(f"[{where}] ⚠️ 실매매에는 있고 이 시뮬레이션에는 **없는** 청산이 켜져 있다: "
                  f"{' · '.join(on)}. 결과는 그 청산이 한 번도 일어나지 않은 세계의 것이다.")

    entry_on = unmodeled_entry_features()
    if entry_on:
        # 기본값이 켜짐이라 거의 항상 뜬다 — 그래서 조용한 톤으로 낸다. 요점은
        #  '경보'가 아니라 '이 결과가 무엇을 안 밟았는지'를 결과 옆에 남기는 것이다.
        _announce(f"[{where}] 실매매에만 있는 진입 게이트: {' · '.join(entry_on)}. "
                  f"이 시뮬레이션은 이 게이트가 한 번도 막지 않은 세계다 "
                  f"(차단 비중은 tools/audit_gate_forward.py 로 잰다).", loud=False)
    return on


def announce_smart_money_source(where="백테스트"):
    """수급(스마트머니) 축을 **어느 소스로** 굴렸는지 알린다.

    [왜] 이 축은 KRX_ID/KRX_PW 유무로 켜지고 꺼진다 — 있으면 전 구간(KRX), 없으면 최근
     30거래일만(KIS), 다 실패하면 전 구간 False. 자격증명이 다른 두 기계의 감사는 서로
     다른 전략을 잰 것인데, 결과에 그 상태가 남지 않아 비교할 때 확인할 방법이 없었다.
     실측 크기는 작지만(축 on/off = 수익 67.51%→67.86%) '몰라서 못 맞추는 것'과
     '알고 감안하는 것'은 다르다.
    """
    dist = backtest.smart_money_source_summary()
    if not dist:
        return dist
    parts = " · ".join(f"{k} {v}종목" for k, v in sorted(dist.items()))
    msg = f"[{where}] 수급(스마트머니) 출처: {parts}"
    if dist.get("KRX"):
        logger.info(msg)
    else:
        # KRX가 하나도 없다 = 이 축이 사실상 빠진 채로 도는 중이다. 눈에 띄어야 한다.
        msg += " — KRX_ID/KRX_PW 가 없으면 이 축은 최근 구간 밖에서 꺼진 것으로 계산된다."
        logger.warning(msg)
        try:
            config.console.print(f"[dim yellow]※ {msg}[/dim yellow]")
        except Exception:
            print(msg)
    return dist


# 실매매에는 늘 켜져 있는데 run_portfolio 는 **인자를 줘야만** 켜지는 게이트들.
#  주지 않으면 조용히 꺼진 채로 도는데, 감사 도구 대부분이 주지 않는다(실측: daily_loss_limit
#  1개 / reentry_block 1개 / oversize_limit 1개 뿐). 각각 따로 측정돼 '무해'로 판정됐지만
#  (→ live-only-axes-audited), '측정하고 무해로 둔 것'과 '아무도 안 준 것'은 다르다.
#  프로세스당 한 번만 알려 로그 도배 없이 그 사실을 드러낸다.
_HOOK_WARNED = set()


def _warn_missing_live_hooks(daily_loss_limit, reentry_block, oversize_limit):
    """실매매에 있는 게이트가 이 실행에 안 넘어왔으면 프로세스당 1회 알린다."""
    missing = []
    if not daily_loss_limit and getattr(config, "SYSTEM_DAILY_LOSS_LIMIT", 0) > 0:
        missing.append(f"방어 모드(일일 손실 -{config.SYSTEM_DAILY_LOSS_LIMIT:g}%)")
    if not reentry_block:
        missing.append("손절 후 재진입 차단")
    if not oversize_limit or oversize_limit <= 1.0:
        missing.append("최소 주문 금액 보정")
    for m in missing:
        if m not in _HOOK_WARNED:
            _HOOK_WARNED.add(m)
            logger.info(f"[백테스트] 실매매에는 있고 이 실행에는 없는 게이트: {m} "
                        f"(해당 인자를 넘기면 켜진다)")
    return missing


def run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=4,
                  pyramiding_max=None, heat_cap_pct=None, invest_ratio=None,
                  atr_mult=None, market_filter_dates=None, reserved_cash=0.0,
                  risk_scale_by_date=None, oversize_limit=None,
                  ts_act_fn=None, pyr_trigger_fn=None, sl_rate_fn=None,
                  profit_lock_dates=None, rank_fn=None, rotation=None, probe_fn=None,
                  entry_gate=None, pyr_intraday=False, pyr_per_day=None, pyr_fill_cap=None,
                  exit_intraday=False, exit_path="low_first", exit_intraday_only=None,
                  pyr_reset_time_stop=False, exit_next_open=False,
                  intraday_bars=None, bar_stop_times=None, bar_ts_times=None,
                  bar_ts_defer=None, intraday_pyramid=None, bar_pyr_times=None,
                  pyr_next_open=False, sell_structure_ma=None,
                  intraday_status=None, intraday_entry=False, entry_bar_times=None,
                  buy_score_fn=None, daily_loss_limit=None, invest_ratio_fn=None,
                  reentry_block=False, heat_basis="cost"):
    """N슬롯 포트폴리오 시뮬레이션.

    Args:
        dfs: {code: DataFrame} 지표 계산이 끝난 일봉(날짜 오름차순).
        status: precompute_status 결과.
        dates: 시뮬레이션 대상 거래일(오름차순 'YYYYMMDD' 문자열).
        slots: 동시 보유 종목 수. invest_ratio 미지정 시 기초 비중은 1/slots.
        market_filter_dates: {code: set(차단일)} 신규 진입 차단일. 매도에는 영향이 없고,
            피라미딩 증액은 실매매(trader._try_pyramid_buy)와 동일하게
            PYRAMIDING_REQUIRE_HEALTHY_MARKET이 켜져 있으면 함께 보류된다.
        reserved_cash: 운용자가 수동 운용 등으로 묶어둔 금액. 같은 계좌에 있으므로
            사이징 기준 자산(_equity)에는 포함되지만 시스템이 집행할 수는 없다.
        risk_scale_by_date: {날짜: 배수} 일별 리스크 배수. **기초 비중과 히트 캡에 곱한다.**
            [정정 2026-08-19] 종전 설명은 "실매매는 리스크층에만 곱해 사실상 무력하므로
            이것은 실험용 경로"라고 적혀 있었으나, 그 서술은 2026-07-27 이전 상태다.
            실매매도 그때 적용 지점을 기초 비중으로 옮겼다(engine.allocate_budget —
            리스크층이 최종액을 결정하는 일이 없어 배수가 사실상 무력이었던 것이 이유다).
            따라서 이 경로는 실험용이 아니라 **실매매와 같은 지점**이다. 다만 실매매는
            리스크층에도 배수를 곱하고(max_loss_amt) 여기서는 곱하지 않는다 —
            현행 파라미터에서 실데이터 615건 매수 기준 배분액 불일치 0건이며,
            그 조건은 tests/test_sizing_parity.py 가 고정한다.
            콜러블 fn(day, equity) -> 배수 도 받는다 — 계좌 드로다운 축처럼 시뮬레이션 자신의
            자산곡선에 의존하는 축은 사전 계산이 불가능하기 때문이다(tools/audit_drawdown_axis.py).
        sl_rate_fn: fn(row, price, atr_mult) -> 손절률(%, 음수). **실험용 경로**로, 손절 캡
            (MAX_ATR_STOP_LOSS_RATE)을 고정값이 아니라 종목·시점에 따라 바꿨을 때의 효과를
            재기 위한 훅이다(tools/audit_atr_damping.py). None이면 종전과 같이
            engine.atr_stop_rate(=config의 고정 캡)를 쓴다.
        profit_lock_dates: 이익보호선(PROFIT_LOCK)을 켤 거래일 집합. **실험용 경로**로,
            '국면 조건부로만 의미가 있을 수 있다'는 기록(config.PROFIT_LOCK_USE 주석)을
            재기 위한 훅이다(tools/audit_slot_cost.py). None이면 config의 전역 ON/OFF를 쓴다.

        rotation: 슬롯 교체 규칙. None(기본)이면 교체 없음 — 종전 동작 그대로다.
            dict로 주면 슬롯이 찬 날 '가장 약한 보유'와 '최상위 후보'를 견줘 교체한다.
              margin: 후보점수 - 보유점수가 이 값 이상일 때만 (기본 2.0)
              min_days: 최소 보유일 미만은 교체 대상에서 제외 (churn 방지)
              only_unarmed: TS 무장 경험이 없는 보유만 대상 — 추세를 만든 승자를
                  잘라내지 않기 위한 가드. 추세추종에서 가장 중요한 안전장치다.
              only_losing: 평가손실 중인 보유만 대상
            보유 점수는 sell_check(매도 상태면 0)를 쓴다 — 매도 판정과 같은 잣대다.
            (tools/audit_slot_rotation.py)

        reentry_block: 당일 손절/본전청산으로 나간 종목을 **그 손절가 이상**에서 되사지
            않는다(실매매 trader.py의 REENTRY_BLOCK_ABOVE_STOP_PRICE). **실험용 경로**로,
            실매매에만 있고 백테스트에는 없던 게이트를 재현한다(tools/audit_reentry_block.py).
            분봉 진입 경로에서만 의미가 있다 — 종가 모델은 매도·매수가 같은 가격이라
            '더 비싸게 되사기'라는 현상 자체가 없다.

        invest_ratio_fn: fn(day, code) -> 그 종목에 쓸 기초 비중(0~1). **실험용 경로**로,
            '슬롯마다 1/N 균등'이라는 전제를 흔들어 보기 위한 훅이다(점수 가중·변동성
            역가중 등, tools/audit_allocation.py). None이면 종전과 같이 invest_ratio 고정.

        daily_loss_limit: 그날 자산이 전일 대비 이 비율(%, 양수) 이상 빠지면 그날의 신규
            매수·증액을 멈춘다(청산은 그대로). **실험용 경로**로, 실매매에만 있는 방어 모드
            (engine.check_loss_limit → SYSTEM_DAILY_LOSS_LIMIT)를 재현한다
            (tools/audit_daily_loss_limit.py). None이면 종전과 같이 아무 제약이 없다.

        buy_score_fn: fn(day, code) -> 그날 그 종목에 적용할 매수 문턱. **실험용 경로**로,
            실매매에만 있는 적응형 임계값(시장 국면별 ±SCORE_ADJ, engine.build_buy_thresholds)을
            재현하기 위한 훅이다(tools/audit_adaptive_threshold.py). None이면 종전과 같이
            thr["BUY_SCORE"] 고정값을 쓴다.

        entry_gate: fn(day, code, held_codes) -> True면 그날 그 종목을 후보에서 뺀다.
            실매매에만 있고 백테스트에는 없는 진입 게이트(상관관계 보류 등)를 재현하기 위한
            훅이다(tools/audit_entry_gate_parity.py). 순위 실험은 '후보가 슬롯보다 많을 때
            누가 들어가는가'를 묻는데, 후보 집합 자체가 실매매와 다르면 그 답을 실매매로
            옮길 수 없다 — 그래서 게이트를 백테스트 쪽에 맞춰 넣고 다시 잰다.

        probe_fn: fn(day, candidates, free_slots) -> None. 매수 직전의 후보 목록(정렬 후)과
            남은 슬롯 수를 그대로 넘기는 **계측 전용** 훅이다. 순위 실험의 타당성은
            '후보가 슬롯보다 많았는가'와 '경계에서 동점이 몇 번이었는가'에 달렸는데,
            rank_fn만으로는 남은 슬롯 수를 알 수 없어 경쟁을 후보 2개 이상으로 어림할
            수밖에 없었다(tools/audit_scoring_weights.py). 반환값은 쓰지 않는다.

        rank_fn: fn(score, code, row, day) -> 정렬키. 후보가 슬롯보다 많을 때 **무엇이
            슬롯을 차지하는가**를 바꾸는 훅이다(tools/audit_scoring_weights.py). 가중치를
            바꾸는 것만으로는 '점수라는 잣대 자체가 값을 하는가'를 물을 수 없어서 둔다 —
            무작위 순위를 대조군으로 세워야 비교 대상이 생긴다. 게이트(BUY_SCORE 통과)는
            건드리지 않고 순서만 정한다.
              None(기본)  : 실매매와 같은 (점수 → 추세품질 → 52주위치). 위 '진입 순위'
                            주석 참조 — 2026-08-18 이전에는 점수만 보는 정렬이 기본이었다.
              "legacy"    : 그 옛 정렬(점수만, 동점은 등록 순서). 과거 기록값 재현 전용.
              콜러블      : 실험용 순위.

        pyr_reset_time_stop: 증액할 때 시간청산 시계를 0으로 되돌리는가. **기본 False =
            실매매와 같음.** 실매매는 2026-07-29(engine.resolve_entry_date)부터 보유일수를
            진입일(보유수량 0→1 시점)으로 재므로 증액해도 시계가 리셋되지 않는다. 이
            백테스트는 그 이틀 전에 작성돼 옛 동작(True)을 2026-08-16까지 들고 있었다
            — 주석에는 "실매매와 동일하게"라고 적혀 있었지만 사실이 아니었다.
            True 는 그 이전 기록값을 재현할 때만 쓴다(tools/audit_timestop_reset.py).

        intraday_bars / bar_stop_times / bar_ts_times: **실제 분봉으로 하루를 되감는다.**
            intraday_bars = {code: {날짜: [(HHMM, o, h, l, c, v), ...]}} (modules.intraday_bars).
            주면 exit_intraday 의 고가·저가 근사 대신 봉 순서대로 판정한다 — 고가·저가 선후
            가정(exit_path)이 필요 없어지고, 판정 시점을 봉으로 고정할 수 있다.
              · 판정 가격 = 그 봉의 **종가**(= 그 시점의 현재가). 실매매가 주기마다 보는 값과
                같은 의미다. 봉의 저가를 쓰면 '매 순간 감시'가 되어 실매매보다 과하다.
              · 트레일링 고점 = 그 봉까지의 고가 러닝맥스(실매매의 highest_price와 같은 갱신).
              · 지표(ATR)는 **전일 확정 봉**을 쓴다 — 그 시점에 확정된 정보만 쓰기 위해서다.
            sell_structure_ma: 점수하락 매도의 '구조 훼손' 판정에 쓸 이동평균 컬럼명.
              None이면 현행 "EMA60". 없는 컬럼명을 주면 그 조건이 통과 처리되어(=None)
              사실상 조건이 제거된다 — 60일이라는 기간이 한 번도 스윕된 적 없어 열어둔
              **감사 전용 통로**다(tools/audit_sell_structure_ma.py). 실매매는 EMA60 고정.
            bar_pyr_times: 증액을 **어느 봉에서만** 판정할지(분봉 경로). None이면 모든 봉
              = 현 실매매의 장중 추격. {"1400"}을 주면 그 봉의 종가(=15:00 가격)로 하루
              한 번만 판정한다 — 시스템은 15:20~15:30 종가 단일가에 매매하지 않으므로
              접속매매 구간에서 실제로 쓸 수 있는 마지막 판정 시점이 그것이다.
            pyr_next_open: 증액을 **일봉 종가로 판정하고 익일 시가에 체결**한다. 종가 확인
              이라는 이점에서 선견(lookahead)을 걷어낸 형태다 — 종가를 보고 그 종가에
              사는 것은 불가능하지만, 종가를 보고 다음 날 시가에 사는 것은 가능하다.
              켜면 분봉 증액 경로보다 우선한다(청산은 분봉 그대로 두고 증액 축만 바꾼다).
            intraday_pyramid: 증액도 봉 단위로 판정할지. None(기본)이면 분봉과 시점판정이
                둘 다 있을 때 자동으로 켜진다(= 실매매). **청산 축만 재는 감사에서는 False로
                꺼야 한다** — 켜두면 증액 건수가 함께 변해 두 축이 섞이고, '종가 판정 팔이
                일봉 모델과 일치하는가'라는 자기검증이 성립하지 않는다.

            bar_ts_defer: 트레일링 청산이 **그 자리에서 체결되지 않는 경우**를 가정한다.
                판정을 마감 쪽으로 미루면 "주문을 냈는데 못 팔면 어쩌나"가 실제 위험이 된다
                — 15:20~15:30은 KRX 종가 단일가라 시스템이 아예 매매하지 않고
                (auto_trade.common.is_system_market_open), 미체결은 120초 뒤 자동취소된다.
                  None       : 판정한 봉의 종가에 체결(기본)
                  "close"    : 그날 종가(단일가)까지 밀려서 체결
                  "next_open": 그날 못 팔고 다음 거래일 시가에 체결 — 하룻밤 갭을 전부 맞는
                               최악 가정이다. 이 팔이 이기면 체결 위험은 결론을 못 뒤집는다.
                손절 다리에는 적용하지 않는다(즉시 집행이 전제).
            bar_stop_times / bar_ts_times: 각 다리를 **어느 봉에서만** 판정할지. None이면 모든
                봉(= 현 실매매). {"1400"} 처럼 주면 그 봉의 종가 시점에만 판정한다
                — 60분봉에서 14:00 봉의 종가는 15:00 가격이므로 '마감 30분 전 1회 판정'이
                되고, 종가를 미리 아는 이점이 원천적으로 없다(tools/audit_exit_bars.py).

        intraday_status / entry_bar_times: **진입도 분봉으로 되감는다.**
            intraday_status = {code: {날짜: {HHMM: (raw, chk, can_buy, state, reason, rsi,
            w52, atr, close, high)}}} (modules.intraday_bars.precompute_intraday_status).
            실매매는 주기마다 미확정 장중 봉으로 다시 채점하므로, 같은 날 안에서 신호가
            켜졌다 꺼지면 체결 여부가 '몇 시에 스캔했는가'에 달린다 — 일봉 백테스트에
            없는 자유도다. 주면 그 자유도를 그대로 재현한다(tools/audit_entry_bars.py).
              intraday_entry: 진입을 분봉으로 돌릴지. **False면 진입은 종가 그대로**이고
                intraday_status 는 아래 ATR 용도로만 쓰인다 — 청산 축만 재는 감사에서
                진입까지 같이 바뀌면 두 축이 섞인다.
              entry_bar_times: None이면 모든 봉에서 스캔(= 현 실매매), {"1400"} 처럼 주면
                그 봉의 종가 시점에만 스캔한다.
            같은 자료의 ATR을 **청산 분봉 경로에도** 써서 지표 기준을 맞춘다(실매매는
            당일 진행 봉을 덮어 지표를 계산한다 — engine.analyze_sell 경로와 같은 규약).

        exit_next_open: 종가 판정으로 난 청산을 **다음 거래일 시가**에 집행한다(하룻밤 보유).
            '종가 팔은 그날 종가를 미리 아는 이점이 섞였다'는 반론을 daily 데이터만으로
            깨기 위한 비관 브래킷이다 — 실제 마감 직전(15:20) 집행은 종가 체결과 익일 시가
            체결 **사이**에 있으므로, 두 끝이 모두 장중 체결을 이기면 그 사이도 이긴다.
            장중 청산(exit_intraday)으로 나간 건에는 적용되지 않는다.

        exit_intraday / exit_path: 청산을 '언제' 집행하는가. 기본(False)은 종전 동작 —
            **모든 청산이 종가 체결**이고 일봉의 low 는 어디에도 쓰이지 않는다. 그런데
            실매매의 손절·트레일링 트리거는 항상 실시간가다(config.USE_KRX_CLOSE_AFTER_HOURS
            주석). 즉 실매매는 장중에 선을 이탈하는 즉시 나가는데, 청산 다이얼(손절폭·TS
            발동·콜백·BEP)은 전부 종가 체결 세계에서 정해졌다(tools/audit_exit_timing.py).
              exit_intraday=True: 그날 저가가 청산선을 이탈하면 그 선에서 체결한다
                  (갭하락이면 시가). 가격성 사유(손절·ATR손절·본전청산·이익보호·트레일링스탑)
                  만 장중으로 옮기고, 시간청산·점수하락은 종가 판정 그대로 둔다.
              exit_intraday_only: "stop"이면 손절 계열만, "ts"면 트레일링만 장중으로 옮긴다
                  (나머지 다리는 종가 판정으로 남는다). 두 다리 중 어느 쪽이 값을 치르는지
                  가르기 위한 분해축이다. None이면 가격성 사유 전부.
              exit_path: 일봉은 고가·저가의 **선후를 모르므로** 두 극단을 다 재서 띠로 본다.
                  "low_first"  — 트레일링선을 전일까지의 고점으로 긋는다(보수적 기본값).
                  "high_first" — 오늘 고가까지 반영해 선을 올린 뒤 저가를 맞힌다(더 자주 걸림).

        pyr_intraday / pyr_per_day / pyr_fill_cap: 증액을 '하루 몇 번, 어느 가격에' 넣는가.
            기본값(False, 1, None)은 종전 동작 그대로 — 하루 1회, 종가 판정·종가 체결이다.
            **실매매는 감시 주기마다 실시간가로 판정하므로 하루에 2·3차까지 갈 수 있는데**
            일봉 백테스트는 구조상 하루 1회만 낼 수 있어 그 차이를 잰 적이 없었다
            (tools/audit_pyramid_perday.py).
              pyr_intraday=True: 그날 고가가 발동선에 닿으면 발동선에서 체결한다(갭 상승이면 시가).
              pyr_per_day<=0: 증액으로 평단이 오른 뒤 같은 날 다시 발동선에 닿으면 반복한다.
              pyr_fill_cap: 전일 종가 대비 이 %를 넘는 가격은 체결 불가로 본다(상한가 호가 공백).
            ※ 판정에 쓰는 state는 그날 종가로 계산된 값이라 장중 체결에는 앞을 본다. 세 팔이
              같은 편향을 공유하므로 짝비교는 성립하지만, 절대 수치는 낙관 쪽이다.

        ts_act_fn / pyr_trigger_fn: fn(atr, price) -> 발동 기준(%). **실험용 경로**로,
            TS 감시 시작·피라미딩 증액의 고정 임계(+10%)를 종목 변동성에 맞춰 동적으로
            바꿨을 때의 효과를 재기 위한 훅이다(tools/audit_trigger_dials.py).
            None이면 config의 고정값을 그대로 쓴다(기본 동작 불변).

    하루 처리 순서는 실매매와 같다: 매도 → 피라미딩 → 신규 매수(점수 높은 순).
    """
    sell_cfg, thr = config.SELL_STRATEGY, config.ANALYSIS_THRESHOLDS
    # 증액 판정의 SSOT. 실행당 한 번만 묶는다 — 게이트는 하루×보유 종목마다 불리고
    #  감사 도구는 run_portfolio를 수천 번 돌린다(engine은 무거워 모듈 최상단에선 안 부른다).
    from modules.auto_trade.engine import pyramid_gate_ok as _pyramid_gate_ok
    invest_ratio = invest_ratio if invest_ratio is not None else (1.0 / max(1, slots))
    atr_mult = atr_mult if atr_mult is not None else sell_cfg.get("ATR_STOP_MULTIPLIER", 2.0)
    if heat_cap_pct is None:
        heat_cap_pct = getattr(config, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0)
    _warn_missing_live_hooks(daily_loss_limit, reentry_block, oversize_limit)

    # [사이징 파리티] 1주 값이 배분액을 넘을 때 얼마까지 초과 집행을 허용하는가.
    #  종전 백테스트는 무조건 건너뛰었는데(=1.0) 실매매는 무제한 허용이라 두 경로가
    #  달랐다. 시드 500만·고가주에서 이 차이가 계좌 비중 3배까지 벌어진다.
    if oversize_limit is None:
        oversize_limit = getattr(config, "MAX_POSITION_OVERSHOOT", 1.0)
    oversize_limit = float(oversize_limit or 1.0)

    # [하루 증액 횟수] None이면 자동 — 분봉 모드에서는 실매매와 같이 **제한 없음**,
    #  일봉 모드에서는 구조상 하루 1회다(하루에 한 번밖에 판정할 수 없다).
    _bar_pyr = (intraday_pyramid if intraday_pyramid is not None
                else bool(intraday_bars and intraday_status))
    pyr_day_cap = pyr_per_day if pyr_per_day is not None else (0 if _bar_pyr else 1)
    pyr_max = pyramiding_max if pyramiding_max is not None else thr.get("PYRAMIDING_MAX_COUNT", 1)
    pyr_use = thr.get("PYRAMIDING_USE", True) and pyr_max > 0
    pyr_trigger = thr.get("PYRAMIDING_PROFIT_TRIGGER", 10.0)
    pyr_ratio = thr.get("PYRAMIDING_RATIO", 0.5)
    # 실매매는 시장 필터가 켜져 있으면 약세 시장에서 증액도 보류한다(노출 확대 금지).
    pyr_require_healthy = (getattr(config, "USE_MARKET_FILTER", True)
                           and thr.get("PYRAMIDING_REQUIRE_HEALTHY_MARKET", True))

    time_stop_min = sell_cfg.get("TIME_STOP_MIN_PROFIT_RATE", 0.0)
    use_atr = sell_cfg.get("USE_ATR_STOP", True)
    default_sl = sell_cfg["STOP_LOSS_RATE"]
    sell_score_limit = sell_cfg["SELL_SCORE"]
    sell_ma_col = sell_structure_ma or "EMA60"
    ts_act = sell_cfg.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    ts_breakeven = str(sell_cfg.get("TS_ACTIVATION_MODE", "fixed")).lower() == "breakeven"
    lock_use = sell_cfg.get("PROFIT_LOCK_USE", False)
    lock_min_mfe = sell_cfg.get("PROFIT_LOCK_MIN_MFE", 25.0)
    lock_giveback = sell_cfg.get("PROFIT_LOCK_GIVEBACK", 0.5)
    ts_callback = sell_cfg.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
    ts_atr_mult = sell_cfg.get("TRAILING_ATR_MULTIPLIER", 3.5)   # [SSOT] 정본 3.5
    time_stop_days = sell_cfg["TIME_STOP_DAYS"]
    use_time_stop = sell_cfg.get("TIME_STOP_USE", True) and time_stop_days > 0
    bep_stop = sell_cfg.get("BREAK_EVEN_STOP_RATE", 0.5)
    bep_default = sell_cfg.get("BREAK_EVEN_PROFIT_RATE", 5.0)
    use_bep = sell_cfg.get("USE_BREAK_EVEN_STOP", False)

    buy_score = thr["BUY_SCORE"]
    buy_rsi = thr["BUY_RSI_MAX"]
    super_use = thr.get("SUPER_MOMENTUM_USE", True)
    super_score = thr.get("SUPER_MOMENTUM_SCORE", 8.0)
    super_w52 = thr.get("SUPER_MOMENTUM_W52_POS", 90.0)
    super_rsi = thr.get("SUPER_BUY_RSI_MAX", 80.0)
    slippage = getattr(config, "SLIPPAGE_RATE", 0.002)

    rows = {code: {str(r["date"]): r for r in df.to_dict("records")} for code, df in dfs.items()}
    parsed = {d: pd.to_datetime(d, format="%Y%m%d") for d in dates}
    # 종목별 **마지막 봉**. 상장폐지·장기 거래정지·데이터 실패로 봉이 끊긴 뒤에도
    #  포지션이 남아 있으면 그날이 이 세계에서 팔 수 있는 마지막 날이다(_close_on_data_end).
    last_bar = {code: max(r) for code, r in rows.items() if r}

    # ---------- 진입 순위: 기본값이 실매매다 ----------
    # [2026-08-18] 종전 기본 정렬은 **점수 하나만** 보고 동점을 dict 등록 순서로 갈랐다.
    #  점수는 0~10을 0.5로 끊은 21개 값뿐이라 슬롯 당락 경계의 45~52%가 동점이고(99,110건
    #  실측), 그 자리를 관심종목 등록 순서라는 임의 상수가 채우고 있었다. 결과는 계측기
    #  결함이다 — 근거 없이 진입 후보를 무작위로 차단하기만 해도 기준선을 거의 항상 이기고
    #  (기준선 순위 12장 중 13/13·12/13), 같은 표본에서 수익이 252.0 vs 419.4%로 갈린다.
    #  실매매(trader.candidate_priority_key)는 처음부터 (점수 → 추세품질 → 52주위치 →
    #  체결강도)로 갈라 왔으므로, **매매가 아니라 계측기만 달랐다.**
    #  그래서 기본값을 실매매 쪽으로 옮긴다. 감사자가 rank_fn을 기억해야만 옳은 수치가
    #  나오는 구조는 같은 사고를 반복한다(실제로 60개 넘는 도구가 기본값으로 재고 있었다).
    #  체결강도는 실시간 체결 데이터라 백테스트에 없다 — 3단까지만 재현하고, 그 아래는
    #  여전히 등록 순서다(동점이 3단을 모두 통과하는 일은 드물다).
    #  rank_fn="legacy"를 주면 옛 동작(점수만)으로 돌아간다. 과거 기록값을 재현할 때만 쓸 것.
    rank_diag = {"calls": 0, "no_tq": 0}
    # [추세품질 상한] 실매매 게이트(trader의 tq_cap_skip)를 그대로 재현한다. 300 위에서는
    #  전방수익이 꺾이고 꼬리가 잘리므로 진입을 막는다 — 근거는 config
    #  ANALYSIS_THRESHOLDS['TREND_QUALITY_MAX'] 주석. 이력 부족은 실매매와 같이 통과.
    #  0을 주면 해제된다(옛 수치를 재현할 때).
    _tq_lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    _tq_cap = float(config.ANALYSIS_THRESHOLDS.get("TREND_QUALITY_MAX", 0) or 0)
    _tq = ({code: _trend_quality_cached(df, _tq_lb) for code, df in dfs.items()}
           if (_tq_cap > 0 or rank_fn is None) else {})
    if rank_fn == "legacy":
        rank_fn = None
    elif rank_fn is None:
        _NEG = float("-inf")

        def rank_fn(sc, code, row, day):
            # 이력 부족(None)은 실매매와 같이 동점 안에서 최하순위 — 검증 불가는 못 산다.
            v = _tq.get(code, {}).get(str(day))
            rank_diag["calls"] += 1
            if v is None:
                rank_diag["no_tq"] += 1
            return (sc, _NEG if v is None else v, float(row.get("w52_pos", 0.0) or 0.0))
    # 분봉 리플레이용 전일 확정 봉(지표는 그 시점에 확정된 것만 쓴다).
    prev_rows = {}
    if intraday_bars:
        for code, df in dfs.items():
            recs = df.to_dict("records")
            prev_rows[code] = {str(r["date"]): recs[i - 1] for i, r in enumerate(recs) if i}

    # 익일 시가 체결용 (날짜 → 그 종목의 다음 거래일). 안 쓰면 만들지 않는다.
    next_day = {}
    if exit_next_open or bar_ts_defer == "next_open" or pyr_next_open:
        for code, df in dfs.items():
            dt = [str(x) for x in df["date"]]
            next_day[code] = {d: dt[i + 1] for i, d in enumerate(dt[:-1])}

    # 상한가 체결 가드용 전일 종가. 가드를 안 쓰면 만들지 않는다(메모리).
    prev_close = {}
    if pyr_fill_cap:
        for code, df in dfs.items():
            cl, dt = df["close"].tolist(), [str(x) for x in df["date"]]
            prev_close[code] = {d: (cl[i - 1] if i else None) for i, d in enumerate(dt)}

    reserved_cash = float(reserved_cash or 0.0)
    cash = float(initial_capital) - reserved_cash
    positions, trades, equity_curve, cash_ratios, full_slot_cash = {}, [], [], [], []
    peak, mdd, slot_usage = initial_capital, 0.0, 0
    max_pos_weight = max_buy_weight = max_buy_risk = 0.0
    risk_cap_breaches = 0
    risk_per_trade_cap = getattr(config, "SYSTEM_RISK_PER_TRADE", 4.0) or float("inf")
    # [소액 시드 진단] 배분액이 1주 값에 못 미쳐 버려진 기회. 시드가 작을수록 급증한다.
    skipped_qty0, pyramid_blocked_qty0 = 0, 0
    # 히트 캡이 실제로 물린 횟수 — '안 걸리는 다이얼'과 '걸리는 다이얼'을 가른다.
    heat_capped_buys, heat_capped_pyr = 0, 0
    # [진단] TS 발동 기준이 실제로 구속한 일수 — 콜백은 충족했는데 MFE가 기준 미달이라
    #  청산이 보류된 날. 이 값이 0이면 발동 기준을 올려도 내려도 결과가 같다는 뜻이다.
    ts_gated_days = 0
    # 배분액을 넘겨(1주 강제) 집행한 매수 — 사이징 상한이 깨진 횟수.
    oversized_buys = 0
    # 슬롯 교체로 비운 포지션 수(rotation=None이면 항상 0).
    rotations = 0
    # 장중 청산으로 나간 건수와, 청산선 산식이 decide_sell 과 어긋난 건수(0이어야 정상).
    intraday_exits = intraday_mismatch = 0

    # 종목별 **최근에 알려진 종가**. 그날 봉이 없는 보유 종목을 평가할 때 쓴다.
    #  [2026-08-25] 종전 _equity 는 봉 없는 종목을 합계에서 통째로 뺐다. 거래정지 하루만
    #  걸려도 그날 자산이 그 포지션만큼 꺼졌다가 다음 날 돌아왔고, MDD 는 그 인공 구덩이를
    #  실제 낙폭으로 셌다(합성 실측: -7.7% → -34.0%). **직전 마크로 평가하는 것**이 맞다 —
    #  주가가 사라진 것이 아니라 그날의 시세가 없을 뿐이다. 마크는 그날까지 본 값만 쓰므로
    #  앞을 보지 않는다(마지막 봉으로 소급 평가하면 미래를 당겨쓰게 된다).
    mark_px = {}

    def _mark(px):
        return px if px and not (isinstance(px, float) and math.isnan(px)) and px > 0 else None

    def _equity(day):
        total = cash + reserved_cash
        for c, p in positions.items():
            row = rows[c].get(day)
            px = _mark(row["close"]) if row is not None else mark_px.get(c)
            if px:
                total += p["qty"] * px
        return total

    def _effective_sl(position, hwm=None):
        """현재 유효 손절률(BEP 상향 반영). BEP는 실매매와 같은 토글을 따른다.

        hwm: 고점을 밖에서 주입한다(장중 청산 모사에서 '오늘 고가 반영 전' 고점을 쓰기 위함).
        """
        sl, applied = _weighted_sl(position, default_sl)
        if not use_bep:
            return sl, applied, False
        peak = position["high"] if hwm is None else hwm
        max_profit = (peak - position["avg"]) / position["avg"] * 100
        activation = abs(sl) if (applied and sl < 0) else bep_default
        if max_profit >= activation and sl < bep_stop:
            return bep_stop, applied, True
        return sl, applied, False

    stop_px_today = {}          # {code: 그날 마지막 손절 체결가} — reentry_block 전용
    reentry_blocked = 0         # 게이트가 실제로 막은 진입 횟수(빈도부터 센다)

    def _do_sell(code, pos, day, sell_price, reason, holding_days, max_profit, is_bep):
        """청산 1건 집행. 종가 경로와 장중 경로가 같은 회계를 쓰도록 한 군데로 모은다."""
        nonlocal cash
        if reentry_block and str(reason).startswith(("손절", "ATR손절", "본전청산")):
            # 같은 날 여러 번이면 마지막 값이 남는다 — 실매매 _collect_stop_exit_prices와 같다.
            stop_px_today[code] = sell_price
        amount = pos["qty"] * sell_price
        amount -= trading_cost.sell_fee(amount)
        # 보고 손익은 왕복(매수+매도) 비용을 모두 뺀다. 현금(cash)에는 매수 수수료가
        # 진입 시점에 이미 빠져 있으므로 이 값을 잔고에 더하지 않는다.
        profit, _ = trading_cost.net_realized_profit(pos["avg"], sell_price, pos["qty"])
        cash += amount
        trades.append({
            "code": code, "date": day, "reason": reason, "profit_amt": profit,
            "profit": profit / (pos["qty"] * pos["avg"]) * 100, "days": holding_days,
            # [진단] 슬롯 점유·수익 반납을 재려면 실현손익만으로는 부족하다.
            #  mfe = 보유 중 최대 평가수익률, armed = TS 무장 경험, bep = 청산 시
            #  손절선이 본전선까지 올라와 있었는가.
            "mfe": max_profit, "armed": bool(pos.get("ts_armed_ever")), "bep": bool(is_bep),
            # [감사 가능성] 청산 체결가. 종전에는 매수·증액에만 fill 이 있어, **청산가가
            #  그날 봉 안의 실현 가능한 가격인지 사후에 검증할 수 없었다**(장중 모드는
            #  체결가 규약이 경로마다 다르다: 선·시가·봉 종가·익일 시가).
            #  tests/test_intraday_replay.py 가 이 값으로 불변식을 건다.
            "fill": sell_price, "qty": pos["qty"],
        })
        del positions[code]

    def _close_on_data_end(code, pos):
        """봉이 끊긴 종목의 포지션을 **마지막 봉의 종가**로 청산한다.

        [왜 필요한가 · 2026-08-25] 종전에는 그날 봉이 없으면 매도 루프가 그냥 건너뛰었다
         (`row is None → continue`). 그래서 상장폐지·장기 거래정지·데이터 실패로 봉이 끊기면
         포지션이 창 끝까지 살아남아 **슬롯을 영구 점유**하고, `_equity` 는 봉 없는 종목을
         합계에서 빼므로 **투입 자본이 자산곡선에서 통째로 증발**했다(최종 자산도 같은 자를
         쓴다). 청산 기록이 없으니 승률·PF·꼬리 표본에서도 빠졌다. 합성 2종목 실측에서
         동결 1건이 시드의 34.6%를 지우고 MDD 를 -7.7% → -36.3% 로 부풀렸다.

        [가격을 무엇으로 두는가] 마지막으로 **알 수 있었던** 종가다. 슬리피지는 얹지 않는다
         — 이건 전략이 내린 매매 판정이 아니라 자료가 끝나서 하는 정리이고, 없는 호가를
         지어내지 않는 것이 낫다. 다만 상장폐지 종목을 섞어 재는 감사에서는 실제 회수액이
         마지막 종가보다 훨씬 낮은 것이 보통이므로, 이 청산은 **낙관 쪽으로 치우친다**.
         생존 편향의 크기를 재는 도구는 그 점을 감안해 읽어야 한다.
        """
        last_day = last_bar.get(code)
        if last_day is None:
            return
        last_row = rows[code][last_day]
        px = last_row["close"]
        if px is None or (isinstance(px, float) and math.isnan(px)) or px <= 0:
            return
        holding_days = (parsed[last_day] - pos["buy_dt"]).days
        mfe = (pos["high"] - pos["avg"]) / pos["avg"] * 100 if pos["avg"] > 0 else 0.0
        _do_sell(code, pos, last_day, px, "데이터종료", holding_days, mfe, False)

    def _pyramid_gate(price, pos, state, trigger):
        """증액 판정 — 차수·수익률·상태.

        [SSOT 2026-08-19] 판정식은 engine.pyramid_gate_ok 가 단독 보유한다. 종전에는
        같은 조건 셋을 이 함수 안 세 경로(익일시가·분봉·종가)에 각각 옮겨 적었고,
        실매매(engine.analyze_pyramid)와도 따로 적혀 있었다. 백테스트가 실매매를 검증하는
        구조에서 판정식이 갈라지면 검증이 무의미해진다. 여기서는 '가격 → 수익률' 환산만
        맡는다(경로마다 어느 가격으로 판정하는지가 다르다: 종가·고가·봉 종가).
        """
        if not price or pos["avg"] <= 0:
            return False
        profit_rate = (price - pos["avg"]) / pos["avg"] * 100.0
        return _pyramid_gate_ok(profit_rate, state, pos["pyr"], pyr_max, trigger)

    def _pyramid_once(code, pos, day, price, ref_row, nth):
        """증액 1건 집행. 일봉 경로와 분봉 경로가 같은 사이징·회계를 쓰게 모은다.

        ref_row: 손절률 계산에 쓸 지표 소스(일봉 경로는 그날 봉, 분봉 경로는 그 시점 ATR).
        반환 False면 그날은 더 얹을 수 없다(수량·현금·히트 부족).
        """
        nonlocal cash, heat_budget, pyramid_blocked_qty0, heat_capped_pyr
        add_qty = int(pos["qty"] * pyr_ratio)
        if add_qty < 1:
            # 보유 수량이 적으면(1주 등) 증액 비율 0.5로는 1주도 안 나온다 = 피라미딩 불발
            pyramid_blocked_qty0 += 1
            return False
        add_price = utils.adjust_to_tick(price * (1 + slippage), False) or price
        add_qty = min(add_qty, int(cash / add_price))
        add_sl = default_sl
        if use_atr:
            add_sl = (sl_rate_fn(ref_row, add_price, atr_mult) if sl_rate_fn is not None
                      else _atr_stop_rate(ref_row.get("ATR", 0), add_price, atr_mult, day))
        if heat_budget is not None and add_sl:
            affordable = heat_budget / (add_price * (abs(add_sl) / 100.0))
            if int(max(0, affordable)) < add_qty:
                heat_capped_pyr += 1
            add_qty = min(add_qty, int(max(0, affordable)))
        if add_qty < 1:
            return False

        cost = add_qty * add_price
        cash -= cost + trading_cost.buy_fee(cost)
        if heat_budget is not None:
            heat_budget -= cost * (abs(add_sl) / 100.0)
        pos["avg"] = (pos["qty"] * pos["avg"] + cost) / (pos["qty"] + add_qty)
        pos["qty"] += add_qty
        pos["lots"].append({"qty": add_qty, "sl": add_sl})
        pos["pyr"] += 1
        if pyr_reset_time_stop:
            # [옛 동작 재현 전용] 실매매는 진입일 기준이라 리셋하지 않는다.
            pos["buy_dt"] = parsed[day]
        trades.append({"code": code, "date": day, "reason": f"피라미딩{pos['pyr']}차",
                       "profit_amt": 0, "profit": 0, "days": 0,
                       "nth_today": nth, "fill": add_price})
        return True

    def _ts_act_eff(pos, row):
        """그 포지션·그날의 트레일링 발동선(%). 매도 루프와 히트 산출이 같은 값을 봐야 한다."""
        if ts_act_fn is not None:
            return float(ts_act_fn(row.get("ATR", 0), pos["avg"]))
        if ts_breakeven:
            from modules.auto_trade.engine import (breakeven_activation_rate,
                                                   ts_activation_atr_mult)
            return breakeven_activation_rate(row.get("ATR", 0), pos["avg"],
                                             ts_callback, ts_activation_atr_mult(), use_atr)
        return ts_act

    def _intraday_stop_level(pos, row, hwm, day, ts_act_eff, only_override="__keep__"):
        """장중에 **먼저 닿는** 청산선. (가격, 사유) 또는 None.

        [왜 여기서 다시 계산하는가] decide_sell 은 '이 가격이면 파는가'만 답할 뿐 청산선
        자체를 돌려주지 않는데, 장중 체결가는 그 선이다. 그래서 선의 산식만 여기서 뒤집는다
        — 게이트(무장 여부·이익보호·BEP)는 decide_sell 과 같은 SSOT 헬퍼를 그대로 쓰고,
        아래 호출부가 매번 decide_sell(price=저가) 과 교차검증해 어긋나면 세어 보고한다.
        [먼저 닿는 선] 가격이 내려오며 더 높은 선을 먼저 통과하므로, decide_sell 의
        if/elif 우선순위가 아니라 **선의 높이**로 고른다.
        """
        avg = pos["avg"]
        sl_rate, atr_applied, is_bep = _effective_sl(pos, hwm=hwm)
        max_profit = (hwm - avg) / avg * 100 if avg > 0 else 0.0
        is_lock = False
        lock_on = (day in profit_lock_dates) if profit_lock_dates is not None else lock_use
        if lock_on:
            from modules.auto_trade.engine import profit_lock_stop_rate
            lock = profit_lock_stop_rate(max_profit, lock_min_mfe, lock_giveback)
            if lock is not None and lock > sl_rate:
                sl_rate, is_lock = lock, True

        only = exit_intraday_only if only_override == "__keep__" else only_override
        cands = []
        if sl_rate != 0 and only != "ts":
            r = ("이익보호" if is_lock
                 else "본전청산" if is_bep
                 else ("ATR손절" if (use_atr and atr_applied) else "손절"))
            cands.append((avg * (1 + sl_rate / 100.0), r, is_bep))
        if hwm > 0 and only != "stop":
            atr = row.get("ATR", 0) or 0
            callback = ts_callback
            if use_atr and atr > 0:
                from modules.auto_trade.engine import effective_callback
                callback = effective_callback(ts_callback, (atr * ts_atr_mult / hwm) * 100,
                                              max_profit)
            if ts_breakeven:
                from modules.auto_trade.engine import (breakeven_activation_rate,
                                                       ts_activation_atr_mult)
                armed = max_profit >= breakeven_activation_rate(atr, avg, ts_callback,
                                                                ts_activation_atr_mult(), use_atr)
            else:
                armed = max_profit >= ts_act_eff
            if armed:
                cands.append((hwm * (1 - callback / 100.0), "트레일링스탑", is_bep))
        if not cands:
            return None
        return max(cands, key=lambda x: x[0])

    def _price_exit_confirmed(pos, price, hwm, atr_val, row, state, state_reason,
                              raw_score, sell_check, holding_days, day, ts_act_eff):
        """그 가격이면 decide_sell 도 **가격성 사유로** 판다고 답하는가.

        [왜 있나] _intraday_stop_level 은 청산선을 직접 계산한다(decide_sell 은 선을
        돌려주지 않으므로). 두 산식이 갈라지면 장중 모드의 청산가가 조용히 틀어지는데,
        수익률은 그럴듯하게 나오므로 아무도 모른다. 그래서 집행할 때마다 같은 상황을
        decide_sell 에 되물어 어긋난 횟수를 센다(결과의 intraday_mismatch, 0이어야 정상).

        시간청산은 종가 판정이라 여기서는 끈다 — 켜면 우선순위가 가려 거짓 불일치가 난다.
        cfg 는 config 가 아니라 **이 실행의 파라미터**로 짠다(감사 도구가 다이얼을 쓸어
        바꾸므로 build_sell_cfg 를 쓰면 스윕 값이 아니라 config 값으로 대조하게 된다).
        """
        _sl, _applied, _bep = _effective_sl(pos, hwm=hwm)
        chk, chk_reason = decide_sell(
            price=price, high=hwm, avg=pos["avg"],
            sl_rate=_sl, atr_applied=_applied, is_bep=_bep,
            holding_days=holding_days, state=state, state_reason=state_reason,
            raw_score=raw_score, sell_check=sell_check, ema60=row.get(sell_ma_col),
            atr=atr_val, roll_high_5=row.get("roll_high_5", 0),
            roll_high_10=row.get("roll_high_10", 0),
            cfg={"use_atr": use_atr, "use_time_stop": False,
                 "time_stop_days": time_stop_days, "ts_act": ts_act_eff,
                 "time_stop_min": time_stop_min,
                 "ts_callback": ts_callback, "ts_atr_mult": ts_atr_mult,
                 "ts_breakeven": ts_breakeven,
                 "sell_score_limit": sell_score_limit,
                 "profit_lock_use": (day in profit_lock_dates
                                     if profit_lock_dates is not None else lock_use),
                 "profit_lock_min_mfe": lock_min_mfe,
                 "profit_lock_giveback": lock_giveback})
        return bool(chk and chk_reason in PRICE_EXIT_REASONS)

    prev_equity = None      # 방어 모드 판정용 전일 자산
    for day in dates:
        stop_px_today.clear()   # 게이트는 '당일'만 — 실매매도 today_trades로 하루치만 본다
        for _c, _p in positions.items():          # 보유 종목의 마크 갱신(그날 봉이 있는 것만)
            _r = rows[_c].get(day)
            if _r is not None and _mark(_r["close"]):
                mark_px[_c] = _r["close"]
        equity_curve.append(_equity(day))
        if equity_curve[-1] > 0:
            cash_ratios.append(cash / equity_curve[-1] * 100)
            # [집중도] 한 종목이 계좌에서 차지하는 최대 비중. 사이징 상한이 실제로
            #  지켜지는지는 수익·MDD가 아니라 이 값에 먼저 드러난다.
            for c, p in positions.items():
                if day in rows[c]:
                    w = p["qty"] * rows[c][day]["close"] / equity_curve[-1] * 100
                    max_pos_weight = max(max_pos_weight, w)
        peak = max(peak, equity_curve[-1])
        if peak > 0:
            mdd = min(mdd, (equity_curve[-1] - peak) / peak * 100)

        # ---------- 1) 매도 ----------
        for code in list(positions.keys()):
            row = rows[code].get(day)
            if row is None:
                # 봉이 **아예 끝난** 종목이면 마지막 봉으로 정리한다. 중간에 하루 빠진
                #  것(거래정지 등)은 종전대로 건너뛴다 — 재개일에 판정이 다시 돈다.
                if day > last_bar.get(code, day):
                    _close_on_data_end(code, positions[code])
                continue
            price = row["close"]
            if price is None or (isinstance(price, float) and math.isnan(price)) or price <= 0:
                continue

            pos = positions[code]
            raw_score, sell_check, _can_buy, state, state_reason = status[code][day]
            holding_days = (parsed[day] - pos["buy_dt"]).days

            # [익일 시가 체결] 어제 종가 판정으로 예약된 청산을 오늘 시가에 집행한다.
            pending = pos.get("exit_pending")
            if pending:
                op = row.get("open") or price
                sell_price = utils.adjust_to_tick(op * (1 - slippage), False) or op
                _do_sell(code, pos, day, sell_price, pending[0], holding_days,
                         pending[1], pending[2])
                continue

            ts_act_eff = _ts_act_eff(pos, row)

            # ---------- [분봉 리플레이] 실제 장중 봉으로 하루를 되감는다 ----------
            bars = (intraday_bars or {}).get(code, {}).get(day) if intraday_bars else None
            if bars:
                prev_row = prev_rows.get(code, {}).get(day) or row
                st_day = (intraday_status or {}).get(code, {}).get(day) or {}
                hwm = pos["high"]
                done = False
                for hhmm, _bo, bh, _bl, bc, _bv in bars:
                    hwm = max(hwm, bh)   # 실매매의 highest_price 갱신과 같은 시점
                    legs = []
                    if bar_stop_times is None or hhmm in bar_stop_times:
                        legs.append("stop")
                    if bar_ts_times is None or hhmm in bar_ts_times:
                        legs.append("ts")
                    if not legs:
                        continue
                    only = None if len(legs) == 2 else legs[0]
                    # 지표(ATR)는 실매매와 같이 '그 시점의 진행 봉'으로 계산한 값을 쓴다.
                    #  없으면 전일 확정 봉으로 폴백한다(그 시점에 확정된 정보).
                    st_now = st_day.get(hhmm)
                    ref = ({"ATR": st_now[7]} if st_now else prev_row)
                    hit = _intraday_stop_level(pos, ref, hwm, day, ts_act_eff, only)
                    if hit and bc <= hit[0]:
                        # [자기검증] 분봉 경로에는 종전에 대조가 없었다 — 장중 결론
                        #  대부분이 이 경로에서 나오는데 정작 산식이 갈라져도 아무 신호가
                        #  없었다. 두 다리를 다 켠 실행에서만 전수 대조가 성립한다.
                        if only is None and not _price_exit_confirmed(
                                pos, bc, hwm, (ref.get("ATR", 0) or 0), row, state,
                                state_reason, raw_score, sell_check, holding_days,
                                day, ts_act_eff):
                            intraday_mismatch += 1
                        mfe = (hwm - pos["avg"]) / pos["avg"] * 100
                        if mfe >= ts_act_eff:
                            pos["ts_armed_ever"] = True
                        is_ts = hit[1] == "트레일링스탑"
                        if (is_ts and bar_ts_defer == "next_open"
                                and next_day.get(code, {}).get(day)):
                            # 그날은 못 팔았다 — 다음 거래일 시가에 나간다(하룻밤 갭 감수).
                            pos["exit_pending"] = (hit[1], mfe, hit[2])
                            done = True
                            break
                        # 체결가는 '그 시점의 현재가'(봉 종가)다. 선 위에서 체결됐다고
                        #  가정하면 실매매보다 유리해진다.
                        raw_px = row["close"] if (is_ts and bar_ts_defer == "close") else bc
                        exec_price = utils.adjust_to_tick(raw_px * (1 - slippage), False) or raw_px
                        intraday_exits += 1
                        _do_sell(code, pos, day, exec_price, hit[1], holding_days, mfe, hit[2])
                        done = True
                        break
                if done:
                    continue
                pos["high"] = max(pos["high"], hwm)

            # ---------- [장중 청산 모사] 실매매의 손절·TS는 실시간가로 친다 ----------
            if bars:
                pass
            elif exit_intraday:
                hwm = pos["high"]
                if exit_path == "high_first":
                    hwm = max(hwm, row["high"])
                low = row.get("low", 0) or 0
                hit = _intraday_stop_level(pos, row, hwm, day, ts_act_eff) if low > 0 else None
                if hit and low <= hit[0]:
                    level, hit_reason, hit_bep = hit
                    # [자기검증] 같은 상황을 decide_sell 에 저가로 물어봐도 가격성 사유로
                    #  팔아야 한다(_price_exit_confirmed).
                    if exit_intraday_only is None and not _price_exit_confirmed(
                            pos, low, hwm, row.get("ATR", 0), row, state, state_reason,
                            raw_score, sell_check, holding_days, day, ts_act_eff):
                        # 한쪽 다리만 장중으로 옮긴 경우 decide_sell 은 다른 다리를 답할 수
                        #  있으므로, 전수 대조는 두 다리를 다 켠 실행에서만 의미가 있다.
                        intraday_mismatch += 1
                    # 갭하락으로 시가가 이미 선 아래면 그 시가가 체결가다.
                    raw = min(level, row["open"]) if (row.get("open") or 0) > 0 else level
                    exec_price = utils.adjust_to_tick(raw * (1 - slippage), False) or raw
                    mfe = (hwm - pos["avg"]) / pos["avg"] * 100
                    if mfe >= ts_act_eff:
                        pos["ts_armed_ever"] = True
                    intraday_exits += 1
                    _do_sell(code, pos, day, exec_price, hit_reason, holding_days, mfe, hit_bep)
                    continue

            pos["high"] = max(pos["high"], row["high"])
            loss_rate = (price - pos["avg"]) / pos["avg"] * 100
            max_profit = (pos["high"] - pos["avg"]) / pos["avg"] * 100
            sl_rate, atr_applied, is_bep = _effective_sl(pos)

            # [무장 래치 — 기각됨. 켜지 말 것] 발동선은 매일 '현재 봉' ATR로 다시 계산되므로
            #  변동성이 오르면 문턱이 올라가 이미 무장된 TS가 풀린다(실측 2026-08-09:
            #  가상진입 30,333건 기준 해제율이 10년 내내 22~40%인데 2026년 70.9%).
            #  armed는 어디에도 저장되지 않아 래치가 없다 — 결함으로 의심해 반사실을 쟀다.
            #
            #  결과는 반대였다. 래치 ON은 10년 5구간 중 4구간에서 열위이고 최근 구간에서
            #  가장 크게 진다(구간5 수익 113.2%→74.6%, 상위10% 72.4→55.1, 수익승 2/15).
            #  무장 해제는 버그가 아니라 적응 장치다 — 변동성이 커질 때 트레일링 보호를
            #  풀어 포지션에 숨 쉴 공간을 준다. 래치를 걸면 고변동 구간에서 조기 발동해
            #  승자를 잘라내고 fat-tail이 먼저 무너진다. 재검증용으로만 남긴다.
            # [진단] 보유 중 한 번이라도 무장선을 넘었는가. 판정에는 쓰지 않는다
            #  (래치가 아니다 — 아래 TS_ARM_LATCH와 무관하게 기록만 한다).
            if max_profit >= ts_act_eff:
                pos["ts_armed_ever"] = True

            ts_breakeven_eff = ts_breakeven and ts_act_fn is None
            if config.SELL_STRATEGY.get("TS_ARM_LATCH", False):
                if pos.get("ts_armed") or max_profit >= ts_act_eff:
                    pos["ts_armed"] = True
                    ts_act_eff = -1e9
                    # decide_sell은 ts_breakeven이 켜져 있으면 발동선을 스스로 다시 구해
                    #  ts_act를 무시한다. 래치가 덮이지 않게 여기서 내려준다.
                    ts_breakeven_eff = False

            sell, reason = decide_sell(
                price=price, high=pos["high"], avg=pos["avg"], sl_rate=sl_rate,
                atr_applied=atr_applied, is_bep=is_bep, holding_days=holding_days,
                state=state, state_reason=state_reason, raw_score=raw_score,
                sell_check=sell_check, ema60=row.get(sell_ma_col), atr=row.get("ATR", 0),
                roll_high_5=row.get("roll_high_5", 0), roll_high_10=row.get("roll_high_10", 0),
                cfg={"use_atr": use_atr, "use_time_stop": use_time_stop,
                     "time_stop_days": time_stop_days, "ts_act": ts_act_eff,
                     "time_stop_min": time_stop_min,
                     "ts_callback": ts_callback, "ts_atr_mult": ts_atr_mult,
                     "ts_breakeven": ts_breakeven_eff,
                     "sell_score_limit": sell_score_limit,
                     "profit_lock_use": (day in profit_lock_dates
                                         if profit_lock_dates is not None else lock_use),
                     "profit_lock_min_mfe": lock_min_mfe,
                     "profit_lock_giveback": lock_giveback})

            # [진단] 발동 기준만 없었다면 TS로 팔렸을 날 (기준이 실제로 구속하는가)
            if not sell and pos["high"] > 0 and max_profit < ts_act_eff:
                _cb = ts_callback
                _atr = row.get("ATR", 0) or 0
                if use_atr and _atr > 0:
                    _cb = max(ts_callback, (_atr * ts_atr_mult / pos["high"]) * 100)
                if (pos["high"] - price) / pos["high"] * 100 >= _cb:
                    ts_gated_days += 1

            if sell:
                if exit_next_open and next_day.get(code, {}).get(day):
                    # 오늘은 팔지 않고 예약만 건다 — 하룻밤 갭을 그대로 맞는다.
                    pos["exit_pending"] = (reason, max_profit, is_bep)
                    continue
                sell_price = utils.adjust_to_tick(price * (1 - slippage), False) or price
                _do_sell(code, pos, day, sell_price, reason, holding_days, max_profit, is_bep)

        # ---------- 히트(총 오픈 리스크) 예산 ----------
        # 계좌 드로다운 축은 시뮬레이션 자신의 자산곡선에 의존하는 피드백 루프라
        #  사전 계산이 불가능하다 → 콜러블(day, equity)도 받는다.
        if callable(risk_scale_by_date):
            day_scale = float(risk_scale_by_date(day, _equity(day)) or 1.0)
        else:
            day_scale = float((risk_scale_by_date or {}).get(day, 1.0) or 1.0)
        day_scale = min(1.0, day_scale) if day_scale > 0 else 1.0

        # ---------- 방어 모드(일일 손실 한도) ----------
        # 실매매는 그날 시작 자산 대비 손실이 한도에 닿으면 신규 매수를 멈추고 청산 감시만
        #  남긴다. 일봉 세계에서는 '전일 종가 자산 대비 오늘 종가 자산'이 그날의 손실률이다.
        halted_today = False
        if daily_loss_limit and daily_loss_limit > 0 and prev_equity:
            eq_today = _equity(day)
            if eq_today > 0 and (eq_today - prev_equity) / prev_equity * 100 <= -daily_loss_limit:
                halted_today = True
        prev_equity = _equity(day)

        # ---------- 포트폴리오 히트(총 오픈 리스크) ----------
        # [패리티 2026-09-01] 종전에는 `수량 × 종가 × |손절률|` 이었다. 실매매
        #  (engine.RiskManager.compute_portfolio_heat)와 **세 군데**가 달랐다:
        #   ① 청산선이 아니라 진입 손절률만 봤다(TS 무장 상향·이익보호를 반영하지 않아,
        #      이미 이익이 잠긴 포지션도 최초 손절폭만큼 예산을 계속 먹었다).
        #   ② 기준이 종가라, 손절선이 고정된 채 값만 올라도 히트가 부풀었다.
        #   ③ 그래서 백테스트 히트가 실매매식의 2.3배였고, 캡이 배분액을 깎은 매수가
        #      8장 합 799건 — 무동작 다이얼이 아니라 **모든 감사에 다른 세기로 걸려 있었다.**
        #  이제 청산선은 _intraday_stop_level(매도 경로와 같은 SSOT)에서 받고,
        #  기준은 heat_basis 가 정한다.
        #    "cost"(기본·현행 실매매) : 수량 × max(0, 매수가 − 청산선) — 진입 대비 자본 손실.
        #       손절선이 매수가 위로 올라간 포지션(TS 무장·BEP)은 리스크 0이 되어 캡에서
        #       빠진다. 미실현 이익 반납은 자본 손실이 아니고, 그 관리는 TS의 일이다.
        #    "mark"(폐기된 종전 정의) : 수량 × max(0, 종가 − 청산선). 대조군으로만 남긴다 —
        #       추세가 잘 될수록 히트가 부풀어 증액이 막히는 성질을 재기 위한 것이다.
        heat_budget = None
        if heat_cap_pct and heat_cap_pct > 0:
            heat = 0.0
            for code, pos in positions.items():
                row = rows[code].get(day)
                if row is None:
                    continue
                lvl = _intraday_stop_level(pos, row, pos["high"], day, _ts_act_eff(pos, row))
                if lvl is not None:
                    stop = lvl[0]
                else:
                    sl_rate, _applied, _bep = _effective_sl(pos)
                    if sl_rate >= 0:
                        continue      # 청산 기준이 없다 — 셀 수 있는 리스크가 아니다
                    stop = pos["avg"] * (1 + sl_rate / 100.0)
                if heat_basis == "legacy":
                    # [대조군] 2026-09-01 이전 백테스트 산식. 이전 감사 수치가 어떤
                    #  세기의 캡 아래에서 나왔는지 되짚기 위해서만 남긴다.
                    sl_rate, _applied, _bep = _effective_sl(pos)
                    if sl_rate < 0:
                        heat += pos["qty"] * row["close"] * (abs(sl_rate) / 100.0)
                    continue
                ref = pos["avg"] if heat_basis == "cost" else row["close"]
                heat += pos["qty"] * max(0.0, ref - stop)
            heat_budget = _equity(day) * (heat_cap_pct * day_scale) / 100.0 - heat

        # ---------- 2) 피라미딩 (수익 포지션 증액) ----------
        # 방어 모드에서는 증액도 신규 매수와 같이 멈춘다(실매매의 '신규 매수 중단'과 동일).
        if pyr_use and not halted_today:
            for code, pos in list(positions.items()):
                row = rows[code].get(day)
                if row is None or pos["pyr"] >= pyr_max or pos.get("exit_pending"):
                    continue  # 청산 예약된 포지션에는 얹지 않는다
                if pyr_require_healthy and market_filter_dates and day in market_filter_dates.get(code, ()):
                    continue
                # [분봉 경로] 봉 순서대로 판정한다. 증액하면 평단이 올라 다음 발동선도 함께
                #  올라가므로, 하루종일 밀려 올라가는 날에는 같은 날 2·3차가 이어질 수 있다
                #  — 실매매(_try_pyramid_buy)에 하루 제한이 없어 실제로 일어나는 일이다.
                #  [한계] 청산은 이 블록보다 먼저 하루치를 끝내므로, 증액으로 오른 평단이
                #   같은 날의 청산선에는 반영되지 않는다(다음 날부터 반영). 증액일과 청산일이
                #   겹치는 경우에만 생기는 오차다.
                if pyr_next_open:
                    # [익일 시가] 판정은 오늘 일봉 종가, 체결은 다음 거래일 시가.
                    #  '종가로 확인된 돌파에만 얹는다'를 선견 없이 재현하는 유일한 형태다.
                    _r, _c, _cb, _state, _rs = status[code][day]
                    if _state not in ("매수", "강매수"):
                        continue
                    nd = next_day.get(code, {}).get(day)
                    nrow = rows[code].get(nd) if nd else None
                    if nrow is None:
                        continue
                    done_today = 0
                    while pyr_day_cap <= 0 or done_today < pyr_day_cap:
                        trig = pyr_trigger
                        if pyr_trigger_fn is not None:
                            trig = float(pyr_trigger_fn(row.get("ATR", 0), pos["avg"]))
                        if not _pyramid_gate(row["close"], pos, _state, trig):
                            break
                        if not _pyramid_once(code, pos, day, nrow["open"], row, done_today + 1):
                            break
                        done_today += 1
                    continue

                use_bar_pyr = (intraday_pyramid if intraday_pyramid is not None
                               else bool(intraday_bars and intraday_status))
                bar_st = ((intraday_status or {}).get(code, {}).get(day)
                          if use_bar_pyr else None)
                if bar_st:
                    done_today = 0
                    for hhmm in sorted(bar_st):
                        if bar_pyr_times is not None and hhmm not in bar_pyr_times:
                            continue
                        if pos["pyr"] >= pyr_max:
                            break
                        if pyr_day_cap > 0 and done_today >= pyr_day_cap:
                            break
                        st = bar_st[hhmm]
                        b_state, b_atr, b_close = st[3], st[7], st[8]
                        trig = pyr_trigger
                        if pyr_trigger_fn is not None:
                            trig = float(pyr_trigger_fn(b_atr, pos["avg"]))
                        if not _pyramid_gate(b_close, pos, b_state, trig):
                            continue
                        if not _pyramid_once(code, pos, day, b_close, {"ATR": b_atr},
                                             done_today + 1):
                            break   # 수량·현금·히트 부족은 그날 내내 풀리지 않는다
                        done_today += 1
                    continue

                _raw, _chk, _can, state, _reason = status[code][day]
                # [하루 다회] 기본은 하루 1회·종가 판정(종전 동작 그대로). pyr_intraday를 켜면
                #  실매매처럼 '장중 발동선 도달'로 판정하고, pyr_per_day<=0이면 평단이 오른 뒤
                #  같은 날 다시 발동선에 닿는 만큼 반복한다(tools/audit_pyramid_perday.py).
                done_today = 0
                while pyr_day_cap <= 0 or done_today < pyr_day_cap:
                    trigger = pyr_trigger
                    if pyr_trigger_fn is not None:
                        trigger = float(pyr_trigger_fn(row.get("ATR", 0), pos["avg"]))
                    need = pos["avg"] * (1 + trigger / 100.0)
                    if pyr_intraday:
                        # 장중 판정은 '고가가 발동선에 닿았는가' — 게이트에 넘기는 가격도 고가다.
                        high = row.get("high", 0) or 0
                        if not _pyramid_gate(high, pos, state, trigger):
                            break
                        # 갭 상승으로 시가가 이미 발동선 위면 첫 감시 주기의 가격 = 시가다.
                        #  2회차부터는 장중에 발동선을 통과하는 순간이므로 발동선 자체가 체결가.
                        price = max(need, row["open"]) if done_today == 0 else need
                        if pyr_fill_cap and prev_close.get(code, {}).get(day):
                            # [상한가 미체결] 발동가가 전일 종가 대비 상한 근처면 매수 체결이
                            #  사실상 불가능하다(호가가 비어 있다). 낙관 편향을 걷어내는 가드.
                            if price >= prev_close[code][day] * (1 + pyr_fill_cap / 100.0):
                                break
                    else:
                        price = row["close"]
                        if not _pyramid_gate(price, pos, state, trigger):
                            break

                    if not _pyramid_once(code, pos, day, price, row, done_today + 1):
                        break
                    done_today += 1

        # ---------- 3) 신규 매수 (점수 높은 순) ----------
        slot_usage += len(positions)
        # [만재 현금] 슬롯이 다 찼을 때의 현금 비율 — 피라미딩에 쓸 수 있는 여력의 실제 지표.
        #  전체 평균 현금은 슬롯이 덜 찬 기간이 섞여 과대평가되므로 따로 잰다.
        if len(positions) >= slots and equity_curve[-1] > 0:
            full_slot_cash.append(cash / equity_curve[-1] * 100)

        rotated_out_today = None

        def _candidates_for(day):
            """그날 매수 조건을 통과한 후보를 순위대로 돌려준다.

            [SSOT] 종전에는 이 블록이 매수 경로 안에만 있었다. 교체(rotation) 판정도
            같은 후보 집합을 봐야 하므로 함수로 뺀다 — 두 벌이 되면 '교체는 사는데
            매수는 안 사는' 유령 조합이 생긴다.
            """
            out = []
            for code, stock_rows in rows.items():
                if code in positions:
                    continue
                # [되사기 금지] 교체로 방금 비운 종목은 그날 후보에서 뺀다. 없으면 같은 날
                #  같은 종가에 팔고 되사는 일이 생긴다 — 포지션은 그대로인데 왕복 비용만
                #  나가는 순손실이다(정규 매도로 슬롯이 2칸 이상 열린 날에 발생한다).
                if code == rotated_out_today:
                    continue
                row = stock_rows.get(day)
                if row is None:
                    continue
                raw_score, _chk, can_buy, state, _reason = status[code][day]
                bs = buy_score if buy_score_fn is None else buy_score_fn(day, code)
                if not can_buy or not (raw_score >= bs or state == "역매수"):
                    continue
                is_super = super_use and raw_score >= super_score and row.get("w52_pos", 0) >= super_w52
                if row["RSI"] >= (super_rsi if is_super else buy_rsi):
                    continue
                if market_filter_dates and day in market_filter_dates.get(code, ()):
                    continue
                # [추세품질 상한] 종목 축의 모멘텀 크래시 방어(실매매 tq_cap_skip과 같은 판정).
                if _tq_cap > 0:
                    _q = _tq.get(code, {}).get(day)
                    if _q is not None and _q >= _tq_cap:
                        continue
                # [실매매 게이트] 보유 종목과의 관계로 걸리는 조건(상관관계 등)은 그날의
                #  보유 구성에 달렸으므로 여기서 묻는다 — 사전 계산으로는 재현되지 않는다.
                if entry_gate is not None and entry_gate(day, code, tuple(positions)):
                    continue
                out.append((raw_score, code, row))
            if rank_fn is None:
                # legacy — 점수만 보고 동점은 등록 순서. 과거 기록값 재현 전용이다.
                out.sort(reverse=True, key=lambda item: item[0])
            else:
                # 동점 처리까지 종전과 같게 두려면 정렬 자체를 갈아끼운다(점수는 그대로 전달).
                out.sort(reverse=True,
                         key=lambda item: rank_fn(item[0], item[1], item[2], day))
            return out

        # ---------- 3-a) 슬롯 교체 (선택) ----------
        # [무엇] 슬롯이 다 찬 상태에서 '보유 중 가장 약한 것'보다 뚜렷하게 강한 후보가
        #  있으면 약한 쪽을 비우고 자리를 내준다. 청산 룰이 걸려야만 슬롯이 풀리는
        #  현행에는 이 경로가 없다 — 후보가 아무리 강해도 4칸이 차 있으면 그냥 못 산다.
        # [위험] 추세추종에서 교체는 양날이다. 달리는 승자를 잘라내면 fat-tail이 죽고,
        #  왕복 비용(매수+매도)을 매번 문다. 그래서 가드를 함께 잰다(아래 rotation 키).
        # 하루에 한 건만 교체한다. 여러 칸을 한꺼번에 갈아치우면 그날 하루의 점수
        #  스냅숏에 포트폴리오 전체를 거는 셈이 되고, 왕복 비용도 배로 문다.
        if rotation and len(positions) >= slots:
            margin = float(rotation.get("margin", 2.0))
            min_days = int(rotation.get("min_days", 0))
            only_unarmed = bool(rotation.get("only_unarmed", False))
            only_losing = bool(rotation.get("only_losing", False))
            cands = _candidates_for(day)
            if cands:
                best_score = cands[0][0]
                weakest, weak_score = None, None
                for code, pos in positions.items():
                    row = rows[code].get(day)
                    if row is None:
                        continue
                    held_days = (parsed[day] - pos["buy_dt"]).days
                    if held_days < min_days:
                        continue
                    if only_unarmed and pos.get("ts_armed_ever"):
                        continue
                    price = row["close"]
                    if only_losing and price > pos["avg"]:
                        continue
                    _raw, chk, _cb, _st, _rs = status[code][day]
                    if weak_score is None or chk < weak_score:
                        weakest, weak_score = code, chk
                if weakest is not None and best_score - weak_score >= margin:
                    pos = positions[weakest]
                    row = rows[weakest][day]
                    price = row["close"]
                    sell_price = utils.adjust_to_tick(price * (1 - slippage), False) or price
                    amount = pos["qty"] * sell_price
                    amount -= trading_cost.sell_fee(amount)
                    profit, _ = trading_cost.net_realized_profit(pos["avg"], sell_price, pos["qty"])
                    cash += amount
                    _sl, _atr_ap, _bep = _effective_sl(pos)
                    trades.append({
                        "code": weakest, "date": day, "reason": "교체",
                        "profit_amt": profit,
                        "profit": profit / (pos["qty"] * pos["avg"]) * 100,
                        "days": (parsed[day] - pos["buy_dt"]).days,
                        "mfe": (pos["high"] - pos["avg"]) / pos["avg"] * 100,
                        "armed": bool(pos.get("ts_armed_ever")), "bep": bool(_bep),
                    })
                    del positions[weakest]
                    rotated_out_today = weakest
                    rotations += 1

        def _candidates_at_bar(day, hhmm):
            """그 시각 봉 시점의 후보. status/지표 모두 '진행 중 봉' 기준이다."""
            out = []
            for code in rows:
                if code in positions or code == rotated_out_today:
                    continue
                st = (intraday_status.get(code, {}).get(day) or {}).get(hhmm)
                if st is None:
                    continue
                raw_score, _chk, can_buy, state, _rs, rsi, w52, atr, close, high = st
                bs = buy_score if buy_score_fn is None else buy_score_fn(day, code)
                if not can_buy or not (raw_score >= bs or state == "역매수"):
                    continue
                is_super = super_use and raw_score >= super_score and w52 >= super_w52
                if rsi >= (super_rsi if is_super else buy_rsi):
                    continue
                if market_filter_dates and day in market_filter_dates.get(code, ()):
                    continue
                if _tq_cap > 0:
                    _q = _tq.get(code, {}).get(day)
                    if _q is not None and _q >= _tq_cap:
                        continue
                if entry_gate is not None and entry_gate(day, code, tuple(positions)):
                    continue
                out.append((raw_score, code, {"ATR": atr, "close": close, "high": high,
                                              "RSI": rsi, "w52_pos": w52}))
            # [2026-08-18] 분봉 경로도 종가 경로와 같은 순위를 쓴다. 종전에는 여기만 점수
            #  단독 정렬이라, 장중 스캔 모드에서는 동점이 여전히 등록 순서로 갈렸다
            #  ([[backtest-tiebreak-parity]]와 같은 결함이 한 군데 더 남아 있던 것이다).
            if rank_fn is None:
                out.sort(reverse=True, key=lambda item: item[0])
            else:
                out.sort(reverse=True,
                         key=lambda item: rank_fn(item[0], item[1], item[2], day))
            return out

        def _buy(code, row, price_src, day):
            """후보 1건 집행. 종가 경로와 분봉 경로가 같은 사이징·회계를 쓰게 모은다."""
            nonlocal cash, max_buy_weight, max_buy_risk, risk_cap_breaches
            nonlocal skipped_qty0, oversized_buys, heat_budget, heat_capped_buys
            buy_price = utils.adjust_to_tick(price_src * (1 + slippage), False) or price_src
            sl_rate = default_sl
            if use_atr:
                sl_rate = (sl_rate_fn(row, buy_price, atr_mult) if sl_rate_fn is not None
                           else _atr_stop_rate(row.get("ATR", 0), buy_price, atr_mult, day))
            _ratio = invest_ratio if invest_ratio_fn is None else invest_ratio_fn(day, code)
            amount = allocate_amount(_equity(day), cash, _ratio * day_scale, sl_rate,
                                     row.get("ATR", 0), buy_price)
            if heat_budget is not None and sl_rate:
                capped = max(0, heat_budget / (abs(sl_rate) / 100.0))
                if capped < amount:
                    heat_capped_buys += 1
                amount = min(amount, capped)
            qty = int(amount / buy_price)
            if qty < 1:
                # 배분액 < 1주 값. 실매매는 여기서 배분액을 1주 값까지 끌어올린다
                #  (trader._execute_buy_orders의 '최소 주문 금액 보정'). 그 초과 허용
                #  배수를 oversize_limit로 재현한다 — 1.0이면 종전처럼 건너뛴다.
                if oversize_limit <= 1.0 or amount <= 0 or buy_price > amount * oversize_limit:
                    skipped_qty0 += 1
                    return False
                if buy_price > cash:
                    skipped_qty0 += 1
                    return False
                qty = 1
                oversized_buys += 1

            # [진입 시점 계측] 최대 비중은 피라미딩이 지배하므로 사이징 상한이 지켜졌는지는
            #  '진입 순간'을 봐야 드러난다. 1회 리스크가 SYSTEM_RISK_PER_TRADE를 넘는
            #  매수 건수도 함께 센다 — 그것이 이 가드가 지키려는 바로 그 불변식이다.
            eq_now = _equity(day) or 1
            max_buy_weight = max(max_buy_weight, qty * buy_price / eq_now * 100)
            if sl_rate:
                buy_risk = qty * buy_price * (abs(sl_rate) / 100.0) / eq_now * 100
                max_buy_risk = max(max_buy_risk, buy_risk)
                if buy_risk > risk_per_trade_cap:
                    risk_cap_breaches += 1

            cash -= qty * buy_price + trading_cost.buy_fee(qty * buy_price)
            if heat_budget is not None:
                heat_budget -= qty * buy_price * (abs(sl_rate) / 100.0)
            positions[code] = {"qty": qty, "avg": buy_price,
                               "lots": [{"qty": qty, "sl": sl_rate}],
                               "high": row.get("high", buy_price), "buy_dt": parsed[day],
                               "pyr": 0}
            # 진입 당일에도 마크를 세워 둔다 — 그 봉이 그 종목의 마지막 봉이면
            #  다음 날 마크 갱신 루프가 돌 기회가 없다.
            mark_px[code] = _mark(row.get("close")) or buy_price
            trades.append({"code": code, "date": day, "reason": "매수",
                           "profit_amt": 0, "profit": 0, "days": 0})
            return True

        if halted_today:
            # 방어 모드 — 그날 신규 진입만 멈춘다. 위의 청산·손절 판정은 이미 끝났다.
            pass
        elif intraday_status and intraday_entry and len(positions) < slots:
            # 봉 시각 순서대로 스캔한다 — 먼저 조건을 채운 후보가 슬롯을 가져간다.
            times = sorted({t for c in rows
                            for t in ((intraday_status.get(c, {}).get(day) or {}).keys())})
            for hhmm in times:
                if len(positions) >= slots:
                    break
                if entry_bar_times is not None and hhmm not in entry_bar_times:
                    continue
                for _score, code, srow in _candidates_at_bar(day, hhmm):
                    if len(positions) >= slots:
                        break
                    px = srow["close"]
                    spx = stop_px_today.get(code)
                    if reentry_block and spx and px >= spx:
                        reentry_blocked += 1
                        continue    # 판 값보다 비싸게 되사지 않는다
                    _buy(code, srow, px, day)
        elif len(positions) < slots:
            candidates = _candidates_for(day)
            if probe_fn is not None:
                probe_fn(day, candidates, slots - len(positions))

            for _score, code, row in candidates:
                if len(positions) >= slots:
                    break
                _buy(code, row, row["close"], day)

    final_asset = _equity(dates[-1]) if dates else initial_capital
    # '교체'도 실현손익이 있는 청산이다. profit_amt != 0 조건에 대부분 걸리지만,
    # 손익이 정확히 0인 교체가 빠져 승률·PF 분모가 흔들리지 않게 명시한다.
    sells = [t for t in trades
             if t["reason"] in EXIT_REASONS or t["profit_amt"] != 0]
    gross_profit = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    gross_loss = abs(sum(t["profit_amt"] for t in sells if t["profit_amt"] < 0))
    return {
        "final_asset": final_asset,
        "total_return": (final_asset - initial_capital) / initial_capital * 100,
        "mdd": mdd,
        "pf": (gross_profit / gross_loss) if gross_loss else float("inf"),
        "win": sum(1 for t in sells if t["profit_amt"] > 0),
        "loss": sum(1 for t in sells if t["profit_amt"] <= 0),
        "trades": trades,
        "sells": sells,
        "pyramid_count": sum(1 for t in trades if "피라미딩" in t["reason"]),
        "intraday_exits": intraday_exits,          # 장중 선 이탈로 나간 건수
        "reentry_blocked": reentry_blocked,        # 손절가 재진입 게이트가 막은 횟수
        "intraday_mismatch": intraday_mismatch,    # 청산선 산식 자기검증 실패(0이어야 정상)
        "avg_slots": slot_usage / len(dates) if dates else 0.0,
        "avg_cash_ratio": (sum(cash_ratios) / len(cash_ratios)) if cash_ratios else 0.0,
        # 슬롯 만재 시점의 평균 현금 비율 — 피라미딩 여력의 실제 지표
        "full_slot_cash_ratio": (sum(full_slot_cash) / len(full_slot_cash)) if full_slot_cash else 0.0,
        "full_slot_days": len(full_slot_cash),
        "skipped_qty0": skipped_qty0,                    # 1주도 못 사서 넘긴 진입 기회
        "pyramid_blocked_qty0": pyramid_blocked_qty0,    # 보유 수량이 적어 불발된 증액 기회
        "ts_gated_days": ts_gated_days,                  # TS 발동 기준이 청산을 막은 일수
        "oversized_buys": oversized_buys,                # 배분액을 넘겨 집행한 매수(1주 강제)
        "heat_capped_buys": heat_capped_buys,            # 히트 캡이 배분액을 깎은 신규 매수
        "heat_capped_pyr": heat_capped_pyr,              # 히트 캡이 수량을 깎은 증액
        "rotations": rotations,                          # 슬롯 교체로 비운 포지션 수
        "max_pos_weight": max_pos_weight,                # 한 종목의 최대 계좌 비중(%, 피라미딩 포함)
        "max_buy_weight": max_buy_weight,                # 진입 순간의 최대 비중(%)
        "max_buy_risk": max_buy_risk,                    # 진입 1회의 최대 리스크(계좌 대비 %)
        "risk_cap_breaches": risk_cap_breaches,          # SYSTEM_RISK_PER_TRADE를 넘긴 매수 건수
        # 동점 가름에 추세품질을 못 쓴 비율(%) — 워밍업 오염 경보. 앞부분 lookback-1일은
        #  이력이 없어 동점이 다시 등록 순서로 떨어진다. 이 값이 크면 그 창의 순위 결론은
        #  옛 기본 정렬과 다를 바 없으므로, 감사 도구는 dates 앞을 잘라 0에 가깝게 만들 것.
        "rank_no_tq_pct": (rank_diag["no_tq"] / rank_diag["calls"] * 100
                           if rank_diag["calls"] else 0.0),
        "equity": equity_curve,
    }


# ==========================================================
# 데이터 준비
# ==========================================================
def prepare_universe(targets, days, progress_cb=None, is_overseas=False):
    """대상 종목의 일봉·지표·시장필터 차단일을 준비한다.

    is_overseas: 해외 종목 프레임을 준비한다. 포트폴리오 백테스트 자체는 국내 전용이지만,
     청산 패리티 감사(tools/audit_exit_parity.py)가 해외 일봉으로도 두 구현을 대조할 수
     있어야 해서 통로만 열어둔다. 기본값은 종전과 같다.

    Returns: (dfs, market_filter_dates, dates, failed)
    """
    from datetime import datetime, timedelta

    # 이 함수는 메뉴 백테스트와 감사 도구 전부가 지나는 문이다 — 재현 못 하는 청산이
    #  켜져 있으면 여기서 한 번 알린다(run_portfolio 안에서 부르면 주기마다 쏟아진다).
    warn_if_unmodeled()
    backtest.reset_smart_money_source()

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    dfs, mf_dates, failed = {}, {}, []
    for code, name in targets:
        try:
            df = backtest.get_backtest_data(code, is_overseas, days)
            if df is None or df.empty:
                failed.append(name)
                continue
            df = backtest._append_smart_money_signal(df, code, is_overseas)
            df = backtest.compute_price_indicators(df)
            df["roll_high_5"] = df["high"].rolling(5, min_periods=1).max()
            df["roll_high_10"] = df["high"].rolling(10, min_periods=1).max()

            mask = df["date"].astype(str) >= cutoff
            start_idx = mask.idxmax() if mask.any() else 0
            if len(df) - start_idx < 100:
                failed.append(name)
                continue
            dfs[code] = df.iloc[start_idx:].reset_index(drop=True)

            backtest.prepare_market_filter(code, is_overseas, days)
            mf_dates[code] = set(backtest._MARKET_FILTER_STATE.get("dates") or set())
        except Exception:
            failed.append(name)
        if progress_cb:
            progress_cb(name)

    # [동적 손절 캡] 날짜별 지수 변동성 배율. 실패하면 빈 dict → 배율 1.0(고정 캡).
    backtest.prepare_vol_regime(days, is_overseas)

    # 수급 축을 어느 소스로 굴렸는지 남긴다 — 감사끼리 비교할 때 이 줄이 전제다.
    announce_smart_money_source()

    dates = sorted({str(d) for df in dfs.values() for d in df["date"]})
    return dfs, mf_dates, dates, failed


# ==========================================================
# CLI
# ==========================================================
def run_portfolio_backtest():
    """관심종목 전체를 N슬롯 포트폴리오로 굴리는 백테스트 (메뉴 진입점)."""
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        utils.clear_screen()
        menu_items = [
            ("1", "국내 주식", "Domestic Stock"),
            ("2", "국내 ETF", "Domestic ETF"),
            ("3", "국내 전체 (주식+ETF)", "All Domestic"),
        ]
        choice = utils.show_menu("포트폴리오 백테스팅 (Portfolio Backtest)", menu_items, default_choice="3")
        if choice.lower() in ["b", "q"]:
            return False
        if choice not in ("1", "2", "3"):
            continue

        keys = {"1": ["stocks_kr"], "2": ["etfs_kr"], "3": ["stocks_kr", "etfs_kr"]}[choice]
        targets = []
        for key in keys:
            for item in config.session.stock_data.get(key, []):
                targets.append((item["code"], item["name"]))
        if not targets:
            config.console.print("[yellow]대상 종목이 없습니다. 관심종목을 먼저 등록하세요.[/yellow]")
            utils.pause()
            continue

        max_holdings = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
        pyr_default = config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_MAX_COUNT", 1)
        config.console.print(f"\n[dim]대상 {len(targets)}종목 · 현재 설정: 슬롯 {max_holdings} · 피라미딩 {pyr_default}차[/dim]")

        val = Prompt.ask("분석 기간(일)", default="1095")
        if val.lower() in ["b", "q"]:
            continue
        # [방어] 숫자가 아닌 입력(예: 슬롯 프롬프트용 '2,3,4'를 여기에 잘못 입력)에 ValueError가
        #  나면 메인 루프의 치명 오류 핸들러까지 올라가 텔레그램 경보가 울린다. 기본값으로 되돌린다.
        try:
            days = max(200, int(val))
        except (TypeError, ValueError):
            config.console.print("[yellow]숫자가 아니어서 기본값 1095일로 진행합니다.[/yellow]")
            days = 1095
        val = Prompt.ask("동시 보유 슬롯 수 [dim](쉼표로 여러 개 비교 가능: 4,6)[/dim]", default=str(max_holdings))
        if val.lower() in ["b", "q"]:
            continue
        slot_list = [int(s) for s in val.replace(" ", "").split(",") if s.isdigit()] or [max_holdings]
        val = Prompt.ask("피라미딩 차수 [dim](쉼표로 여러 개 비교 가능: 1,2)[/dim]", default=str(pyr_default))
        if val.lower() in ["b", "q"]:
            continue
        pyr_list = [int(s) for s in val.replace(" ", "").split(",") if s.isdigit()] or [pyr_default]
        val = Prompt.ask("초기 자본(원)", default="10000000")
        if val.lower() in ["b", "q"]:
            continue
        try:
            initial = max(1_000_000, int(val.replace(",", "").strip()))
        except (TypeError, ValueError):
            config.console.print("[yellow]숫자가 아니어서 기본값 10,000,000원으로 진행합니다.[/yellow]")
            initial = 10_000_000

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), console=config.console, transient=True) as progress:
            task = progress.add_task(f"[cyan]데이터 준비 중 (0/{len(targets)})[/cyan]", total=len(targets))
            done = {"n": 0}

            def _tick(_name):
                done["n"] += 1
                progress.update(task, advance=1,
                                description=f"[cyan]데이터 준비 중 ({done['n']}/{len(targets)})[/cyan]")

            dfs, mf_dates, dates, failed = prepare_universe(targets, days, progress_cb=_tick)

        if not dfs or not dates:
            config.console.print("[red]사용 가능한 데이터가 없습니다.[/red]")
            utils.pause()
            continue

        # ---------- 장중 스캔 모드 (실매매와 같은 체결 시점) ----------
        # [왜 기본인가] 실매매는 진입·청산·증액을 **감시 주기마다 실시간가로** 판정한다.
        #  종가 모델은 하루 한 번만 낼 수 있어 실매매와 다른 세계를 잰다(2026-08-16 실측:
        #  청산만 봐도 전체창 수익 94~114% vs 136~217%). 분봉 캐시가 있으면 실매매 쪽에
        #  맞춘다 — 증액도 봉마다 판정하므로 하루에 2·3차가 이어지는 것까지 재현된다.
        # [대가] 분봉이 있는 종목·기간으로 창이 좁아진다. 아래에 무엇이 왜 빠졌는지 표시한다.
        bars = status_bars = None
        use_bars = False
        if intraday_bars_mod is not None:
            probe = {c: intraday_bars_mod.load(c, "60m") for c in list(dfs)[:1]}
            if any(v is not None for v in probe.values()):
                ans = Prompt.ask("장중 스캔 모드 [dim](실매매와 동일한 체결 시점, 분봉 필요)[/dim]",
                                 choices=["y", "n"], default="y")
                use_bars = (ans == "y")
        if use_bars:
            with config.console.status("[cyan]분봉·시점판정 확인 중...[/cyan]"):
                bars, status_bars, keep, drop = intraday_bars_mod.gate_universe(dfs)
                bar_dates = intraday_bars_mod.covered_dates(bars, dates)
            if not keep or not bar_dates:
                config.console.print("[yellow]분봉으로 돌릴 수 있는 종목/기간이 없어 "
                                     "종가 모델로 진행합니다. "
                                     "tools/fetch_intraday_tv.py → tools/build_intraday_status.py "
                                     "를 먼저 실행하세요.[/yellow]")
                use_bars = False
            else:
                names_of = {c: n for c, n in targets}
                if drop:
                    shown = ", ".join(f"{names_of.get(c, c)}({w})" for c, w in drop[:6])
                    config.console.print(f"[dim]※ 장중 스캔 제외 {len(drop)}종목: {shown}"
                                         f"{' 외' if len(drop) > 6 else ''}[/dim]")
                config.console.print(
                    f"[cyan]장중 스캔 모드[/cyan] — {len(keep)}종목 · {len(bar_dates)}거래일 "
                    f"({bar_dates[0]}~{bar_dates[-1]}) · 진입·청산·증액을 모두 봉 단위로 판정합니다.")
                dfs = {c: dfs[c] for c in keep}
                mf_dates = {c: mf_dates.get(c, set()) for c in keep}
                dates = bar_dates

        thresholds = {
            "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
            "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
            "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
            "WEIGHTS": config.SCORING_WEIGHTS,
        }
        with config.console.status("[cyan]일별 상태·점수 계산 중...[/cyan]"):
            status = precompute_status(dfs, thresholds)

        table = Table(title=f"\n포트폴리오 백테스팅 — {len(dfs)}종목 · {len(dates)}거래일 · 초기자본 {initial:,}원",
                      box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("슬롯", justify="center")
        table.add_column("피라미딩", justify="center")
        table.add_column("최종자산", justify="right")
        table.add_column("총수익률", justify="right")
        table.add_column("MDD", justify="right")
        table.add_column("PF", justify="right")
        table.add_column("승률", justify="right")
        table.add_column("청산", justify="right")
        table.add_column("증액", justify="right")
        table.add_column("평균 슬롯", justify="right")

        for slots in slot_list:
            for pyr in pyr_list:
                extra = ({"intraday_bars": bars, "intraday_status": status_bars,
                          "intraday_entry": True} if use_bars else {})
                res = run_portfolio(dfs, status, dates, initial_capital=initial, slots=slots,
                                    pyramiding_max=pyr, market_filter_dates=mf_dates, **extra)
                n_sell = len(res["sells"])
                win_rate = res["win"] / n_sell * 100 if n_sell else 0.0
                ret_color = "red" if res["total_return"] > 0 else "blue"
                table.add_row(
                    str(slots), f"{pyr}차",
                    f"{res['final_asset']:,.0f}원",
                    f"[{ret_color}]{res['total_return']:+.2f}%[/]",
                    f"[blue]{res['mdd']:.2f}%[/]",
                    f"{res['pf']:.2f}",
                    f"{win_rate:.1f}%",
                    f"{n_sell}건",
                    f"{res['pyramid_count']}건",
                    f"{res['avg_slots']:.2f}/{slots}",
                )

        config.console.print(table)
        if failed:
            config.console.print(f"[dim]※ 데이터 부족으로 제외: {len(failed)}종목 ({', '.join(failed[:5])}"
                                 f"{' 외' if len(failed) > 5 else ''})[/dim]")
        config.console.print("[dim]※ 슬롯 경쟁·현금 제약·포트폴리오 히트 캡이 모두 반영된 단일 경로 결과입니다. "
                             "종목 구성이 바뀌면 값이 크게 달라질 수 있습니다.[/dim]")
        if use_bars:
            same_day = sum(1 for r in [res] for t in r["trades"] if t.get("nth_today", 1) >= 2)
            config.console.print(f"[dim]※ 장중 스캔 모드 결과입니다(실매매와 같은 체결 시점). "
                                 f"마지막 조합에서 같은 날 2회 이상 증액 {same_day}건. "
                                 f"종가 모델로 낸 과거 수치와 직접 비교하지 마세요.[/dim]")
        utils.pause()
    return True
