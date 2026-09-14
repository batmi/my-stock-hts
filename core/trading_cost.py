"""거래비용(수수료·증권거래세·슬리피지)의 단일 계산 지점.

백테스트·관찰모드(paper_broker)·실거래 손익 기록이 **같은 식**을 쓰게 하려고 분리했다.
세 곳이 각자 계산하면 성과가 갈리고, 그 차이가 전략 때문인지 비용 모델 때문인지
분리되지 않는다. 2026-08-10 이전이 정확히 그 상태였다:

  - 백테스트: 슬리피지 편도 0.2% + 매도 수수료 0.23%(무명 리터럴), **매수 수수료 없음**
  - 관찰모드: 수수료 매수 0.015% / 매도 0.23%, **슬리피지 없음**
  - 실거래 DB: 매도 '판단 시점'의 평가손익을 그대로 실현손익으로 기록 — 비용도 실제
    체결가도 반영되지 않음

요율은 config 하나만 본다(증권사·세법이 정하는 사실이라 전략 설정에 두지 않는다).
국내는 BUY/SELL_FEE_RATE, 해외는 OVERSEAS_BUY/SELL_FEE_RATE — 증권거래세가 국내
세목이므로 요율 자체가 갈린다(2026-08-10 분리).
"""
import config


def buy_fee(amount, is_overseas=False):
    """매수 위탁수수료. 국내는 원 단위 절사(거래소 관행), 해외는 소수점 유지."""
    if is_overseas:
        return float(amount) * config.OVERSEAS_BUY_FEE_RATE
    return int(float(amount) * config.BUY_FEE_RATE)


def sell_fee(amount, is_overseas=False):
    """매도 비용. 국내는 위탁수수료 + 증권거래세, 해외는 위탁수수료만.

    [주의] 증권거래세는 국내 세목이다. 해외에 국내 요율을 쓰면 없는 세금을 물리면서
      정작 국내보다 비싼 해외 수수료는 빠진다(2026-08-10 분리 전이 그랬다).
    """
    if is_overseas:
        return float(amount) * config.OVERSEAS_SELL_FEE_RATE
    return int(float(amount) * config.SELL_FEE_RATE)


def round_trip_cost(buy_price, sell_price, qty, is_overseas=False):
    """왕복 거래비용(매수 수수료 + 매도 수수료·세)."""
    return (buy_fee(float(buy_price) * qty, is_overseas)
            + sell_fee(float(sell_price) * qty, is_overseas))


def gross_realized_profit(buy_price, sell_price, qty):
    """총차익 = (매도가 − 매입가) × 수량. 비용을 빼지 않는다. (profit_amt, profit_rate%) 반환."""
    buy_price = float(buy_price or 0)
    sell_price = float(sell_price or 0)
    qty = int(qty or 0)
    basis = buy_price * qty
    if qty <= 0 or basis <= 0 or sell_price <= 0:
        return 0.0, 0.0
    gross = (sell_price - buy_price) * qty
    return gross, (gross / basis) * 100


def costs_in_realized():
    """실현손익(trades.profit_amt)에 거래비용을 포함하는가 — **가상투자(관찰 모드)만 True**.

    [운용자 결정 2026-09-14] 실제 매매(한투·토스)의 손익은 수동·자동 모두 "실제 매매에서 발생한
     차익"(총차익)으로 적고 보인다 — 3,460 매수 → 3,455 매도면 −5원이지 −12원이 아니다.
     가상투자는 체결·비용을 시스템이 흉내 내므로 백테스트와 같은 순손익을 유지한다.
    """
    try:
        return bool(getattr(config.session, 'is_paper', False))
    except Exception:      # noqa: BLE001 - 세션이 없으면(도구·테스트) 실거래 규약
        return False


def realized_profit(buy_price, sell_price, qty, is_overseas=False):
    """기록·알림·통계에 쓰는 실현손익 — (profit_amt, profit_rate%, cost_amt) 반환.

    정책은 costs_in_realized() 하나가 정한다:
      · 실거래(기본): profit = 총차익, cost_amt = 왕복 비용(따로 저장)
      · 가상투자     : profit = 순손익(비용 차감), cost_amt = 0 (이미 빠져 있다)

    [왜 비용을 따로 남기나] 원금 불변량(꺼져 있던 사이의 입출금 감지, trader._reconcile_offline_transfer)
     은 원금 = 현금 + 매입원가 − 실현손익 이라는 식이다. 현금에서는 비용이 이미 빠져 있으므로
     실현손익이 총차익이면 왕복마다 비용만큼 원금이 줄어 보이고, 그 누적이 문턱을 넘는 순간
     '출금'으로 오인된다([[daily-asset-baseline-transfers]] 사고와 같은 경로). 그래서 그 식만
     profit_amt − cost_amt 를 쓴다(db.get_realized_profit_between). 두 모드 모두 그 차가 순손익이다.
    """
    gross, grate = gross_realized_profit(buy_price, sell_price, qty)
    if gross == 0 and grate == 0:
        return 0.0, 0.0, 0.0            # 입력 불량 — '모름'
    cost = round_trip_cost(buy_price, sell_price, qty, is_overseas)
    if costs_in_realized():
        net = gross - cost
        return net, (net / (float(buy_price) * int(qty))) * 100, 0.0
    return gross, grate, float(cost)


def net_realized_profit(buy_price, sell_price, qty, is_overseas=False):
    """실현손익 = 총손익 − 왕복 비용. (profit_amt, profit_rate%) 반환.

    [왜 왕복인가] 매수 수수료는 진입 때 이미 현금에서 나갔지만, '이 거래로 얼마를
    벌었나'는 양쪽 비용을 모두 뺀 값이어야 한다. 한쪽만 빼면 총이익이 매도비용
    부근인 거래가 실제로는 손실인데 통계에 '승'으로 잡힌다 — 승률·손익비가 파라미터
    판단의 근거이므로 그 왜곡이 그대로 설정 결정으로 넘어간다.

    [주의] 현금 잔고를 갱신하는 쪽(백테스트 balance, 관찰모드 cash)은 매수 시점에
      매수 수수료를 이미 차감하므로, 여기서 나온 profit을 잔고에 더하면 이중 차감이
      된다. 이 함수는 **보고용 지표**다.
    """
    buy_price = float(buy_price or 0)
    sell_price = float(sell_price or 0)
    qty = int(qty or 0)
    basis = buy_price * qty
    # 가격이 0이면 '체결가 미상'이지 -100% 손실이 아니다. 그대로 계산하면 조용히 틀린
    # 숫자가 승률·손익비에 섞인다 — 알 수 없는 것은 0으로 두고 호출부가 판단하게 한다.
    if qty <= 0 or basis <= 0 or sell_price <= 0:
        return 0.0, 0.0

    gross = (sell_price - buy_price) * qty
    profit = gross - round_trip_cost(buy_price, sell_price, qty, is_overseas)
    return profit, (profit / basis) * 100


def apply_slippage(price, side, mult=1.0):
    """체결 불리 방향으로 슬리피지를 얹은 가격. side: 'buy' | 'sell'.

    호가 정렬(utils.adjust_to_tick)은 호출부가 필요할 때 따로 한다 — 관찰모드는
    가상 체결이라 호가 단위로 맞출 이유가 없고, 백테스트는 이미 정렬하고 있다.
    """
    rate = float(getattr(config, 'SLIPPAGE_RATE', 0.0) or 0.0) * float(mult)
    if rate <= 0:
        return float(price)
    return float(price) * (1 + rate) if str(side).lower() == 'buy' else float(price) * (1 - rate)
