"""배분액을 넘겨 '1주라도' 사는 것이 사이징 상한을 무력화하지 않는가.

[종전 구조] 배분액이 1주 값보다 작으면 배분액을 1주 값까지 끌어올렸다("최소 주문 금액
보정"). 가용 예수금 전체를 쓰는 버그를 막으려던 것인데, 그 결과 기초비중·리스크 한도·
변동성 타겟팅이 min 결합으로 합의한 상한이 1주 값 하나에 덮어써졌다.

큰 계좌에서는 발동하지 않는다. 시드 500만에서만 드러난다 — 관심목록 41종목 중 7종목
(17%)이 이 경로를 탄다.

  SK하이닉스 1,577,000원 · 목표 배분 500,000원 → 집행 1,577,000원 = 계좌의 31.5%
  1회 리스크 = 1,577,000 × 15% = 236,550원 = 4.7% > SYSTEM_RISK_PER_TRADE(4.0%)

[파리티] 백테스트(portfolio_backtest)는 같은 상황에서 건너뛴다. config.py에 기록된
사이징·리스크 결론은 전부 '못 사면 안 산다' 모델에서 나왔는데 실매매만 달랐다.

[고친 방향] MAX_POSITION_OVERSHOOT(기본 1.3) 이내일 때만 1주를 허용한다. 넘으면
진입하지 않는다 — 의도한 비중으로 담을 수 없는 종목을 억지로 담는 쪽이 위험하다.
"""
import pytest
from unittest.mock import patch

import config
from modules.auto_trade import AutoTrader
from modules import portfolio_backtest as pb

OK_ORDER = {'rt_cd': '0', 'output': {'ODNO': '1'}}


@pytest.fixture
def sizing_config():
    """이 파일이 의존하는 사이징 설정을 명시적으로 고정한다.

    [필수] 다른 테스트가 config.USE_VOLATILITY_TARGETING 등을 전역으로 끄고 복구하지
    않는다(test_auto_trade_risk.py 등). 그 상태를 물려받으면 배분액이 축소되지 않아
    가드가 발동할 조건 자체가 만들어지지 않고, 테스트는 '통과'해 버린다(공허한 통과).
    """
    saved = (config.USE_VOLATILITY_TARGETING, config.TARGET_VOLATILITY,
             config.VOLATILITY_SCALING_MIN, config.VOLATILITY_SCALING_MAX,
             config.SYSTEM_RISK_PER_TRADE)
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.25
    config.VOLATILITY_SCALING_MIN = 0.4
    config.VOLATILITY_SCALING_MAX = 2.0
    config.SYSTEM_RISK_PER_TRADE = 4.0
    yield
    (config.USE_VOLATILITY_TARGETING, config.TARGET_VOLATILITY,
     config.VOLATILITY_SCALING_MIN, config.VOLATILITY_SCALING_MAX,
     config.SYSTEM_RISK_PER_TRADE) = saved


@pytest.fixture
def trader(sizing_config):
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.initial_asset = 5_000_000        # 실거래 시드
    t.portfolio_heat_amt = 0.0
    yield t


# 관심종목 실측 ATR은 주가의 약 11%다(연변동 ~175%). 목표 변동성 0.25로는 배수가
#  하한(VOLATILITY_SCALING_MIN=0.4)에 눌리므로 배분액은 기초비중의 40%가 된다.
#  즉 시드 500만·4슬롯이면 기초 1,250,000 → 실제 배분 500,000원. 이것이 실제 조건이다.
ATR_RATIO = 0.11


def _cand(price, name="고가주", code="000660", atr_ratio=ATR_RATIO):
    return [{'code': code, 'name': name, 'price': price, 'score': 9.0,
             'rsi': 50, 'adx': 30, 'cci': 100, 'is_custom_rule': False,
             'atr': price * atr_ratio}]


def _buy(trader, price, *, cash=5_000_000, ratio=0.25, atr_ratio=ATR_RATIO):
    """매수 1회를 태우고 (주문 Mock, 주문 수량)을 돌려준다."""
    with patch.dict(config.SELL_STRATEGY, {'STOP_LOSS_RATE': -7.0, 'USE_ATR_STOP': False}), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch('modules.auto_trade.api.place_order', return_value=OK_ORDER) as place, \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10_000), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._execute_buy_orders(_cand(price, atr_ratio=atr_ratio), cash, ratio, 0, 4)
    qty = int(place.call_args[0][3]) if place.called else 0
    return place, qty


def test_the_allocation_really_is_reduced_by_volatility_targeting(trader, sizing_config):
    """[전제 확인] 이 테스트들이 가정하는 배분액(기초의 40%)이 실제로 나오는가.

    이게 성립하지 않으면 아래 테스트들은 상한이 발동하지 않는 조건에서
    '통과'해 버린다(공허한 통과).
    """
    amt = trader.risk_manager.allocate_budget(
        5_000_000, 0.25, stop_loss_rate=-7.0,
        atr=1_577_000 * ATR_RATIO, current_price=1_577_000)
    assert 400_000 <= amt <= 600_000, (
        f"배분액이 {amt:,}원 — 변동성 타겟팅이 예상대로 축소하지 않았다")


# ───────────────────── 상한이 지켜지는가 ─────────────────────

def test_wildly_overpriced_stock_is_skipped(trader):
    """[핵심] 1주 값이 배분액의 3배면 사지 않는다.

    시드 500만·목표 배분 125만원인데 SK하이닉스 1주는 157만원 이상이다.
    사면 계좌의 31.5%가 한 종목에 들어가고 1회 리스크가 한도를 넘는다.
    """
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 1.3):
        place, _ = _buy(trader, 1_577_000)
    assert not place.called, "배분액의 3배짜리 종목을 1주 강제로 샀다"


def test_slightly_overpriced_stock_is_allowed(trader):
    """[대조군] 상한 이내면 산다 — 보류가 상시면 기능이 죽는다.

    1주 값이 배분액을 조금 넘는 정도는 반올림 손실 수준이라 허용한다.
    """
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 1.3):
        place, qty = _buy(trader, 600_000)     # 배분액 500,000 → 1.2배
    assert place.called, "상한 이내인데 매수가 막혔다"
    assert qty == 1


def test_normal_priced_stock_is_unaffected(trader):
    """가드는 배분액이 1주 값에 못 미칠 때만 관여한다 — 평상시 경로는 그대로다."""
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 1.3):
        place, qty = _buy(trader, 50_000)      # 배분액 500,000 → 여러 주
    assert place.called, "정상 종목이 막혔다"
    assert qty >= 5, f"정상 종목의 수량이 1주로 눌렸다: {qty}"


def test_the_cap_is_configurable(trader):
    """상한을 넓히면 종전 동작(무제한)에 가까워진다 — 운영자가 되돌릴 수 있어야 한다."""
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 99.0):
        place, qty = _buy(trader, 1_577_000)
    assert place.called and qty == 1, "상한을 풀었는데도 막혔다"


def test_cap_of_one_forbids_any_overshoot(trader):
    """1.0은 초과 집행 금지 — 백테스트 종전 동작과 같아진다."""
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 1.0):
        place, _ = _buy(trader, 520_000)       # 배분액 500,000보다 조금 비싸다
    assert not place.called


def test_zero_budget_never_buys(trader):
    """배분액이 0이면 어떤 상한에서도 사지 않고, 0으로 나누지도 않는다."""
    logged = []
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 99.0), \
         patch.object(trader, 'log', side_effect=lambda m, *a, **k: logged.append(m)):
        place, _ = _buy(trader, 100_000, ratio=0.0)
    assert not place.called
    assert any("배분액 0원" in m for m in logged), \
        f"0원 배분을 배수로 표기하려다 깨졌거나 사유가 없다: {logged}"


def test_the_boundary_is_inclusive(trader):
    """정확히 상한 배수면 허용한다 — 경계에서 한쪽으로 미끄러지지 않게 못박는다."""
    price = 650_000                     # 500,000 × 1.3
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 1.3), \
         patch.object(config, 'SLIPPAGE_RATE', 0.0), \
         patch.object(trader.risk_manager, 'allocate_budget', return_value=500_000), \
         patch.dict(config.SELL_STRATEGY, {'STOP_LOSS_RATE': -7.0, 'USE_ATR_STOP': False}), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch('modules.auto_trade.api.place_order', return_value=OK_ORDER) as place, \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10_000), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._execute_buy_orders(_cand(price), 5_000_000, 0.25, 0, 4)
    assert place.called, "정확히 상한 배수인데 막혔다(경계가 배타적으로 굳었다)"


def test_the_shipped_default_blocks_the_real_case(trader):
    """[핵심] 배포되는 기본값 자체가 가드로 동작해야 한다.

    위 테스트들은 상한을 명시적으로 patch 하므로, config 기본값이 무제한으로
    바뀌어도 전부 통과한다. 실제로 라즈베리파이에서 도는 것은 기본값이다.
    """
    # [주의] config.MAX_POSITION_OVERSHOOT는 json/dynamic_config.json이 덮어쓴 값이다.
    #  라즈베리파이의 dynamic_config.json에는 이 키가 없으므로(신규) 거기서 실제로
    #  쓰이는 것은 Pydantic 기본값이다 → 클래스 기본값을 직접 확인해야 한다.
    shipped = config.GlobalSettings().MAX_POSITION_OVERSHOOT
    assert shipped < 3.0, f"배포 기본값이 {shipped}배 — 사실상 무제한이다"

    # 실효값(JSON 반영)도 함께 확인한다. 둘이 어긋나면 기기마다 다르게 동작한다.
    assert config.MAX_POSITION_OVERSHOOT == shipped, (
        f"dynamic_config.json({config.MAX_POSITION_OVERSHOOT})과 "
        f"기본값({shipped})이 다르다 — 맥과 라파의 동작이 갈린다")

    place, _ = _buy(trader, 1_577_000)      # patch 없이 = 배포 기본값
    assert not place.called, "기본 설정에서 SK하이닉스 1주(계좌 31.5%)가 그대로 나갔다"


def test_skip_reason_is_logged(trader):
    """왜 안 샀는지 남아야 한다 — 조용히 건너뛰면 '왜 매수가 없지'가 된다."""
    logged = []
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 1.3), \
         patch.object(trader, 'log', side_effect=lambda m, *a, **k: logged.append(m)):
        _buy(trader, 1_577_000)
    assert any("배분액" in m and "상한" in m for m in logged), f"보류 사유가 없다: {logged}"


# ───────────────── 지키려는 불변식 자체 ─────────────────

def test_one_share_risk_stays_within_the_per_trade_cap(trader):
    """[핵심 불변식] 1회 리스크가 SYSTEM_RISK_PER_TRADE를 넘지 않아야 한다.

    ATR 손절 15%·시드 500만·한도 4% → 한 종목에 넣을 수 있는 최대 금액은
    5,000,000 × 4% / 15% = 1,333,333원. 그보다 비싼 1주는 사면 안 된다.
    """
    price = 1_577_000
    with patch.object(config, 'MAX_POSITION_OVERSHOOT', 1.3), \
         patch.object(config, 'SYSTEM_RISK_PER_TRADE', 4.0):
        place, _ = _buy(trader, price)

    if place.called:
        risk = price * 0.15 / trader.initial_asset * 100
        pytest.fail(f"1회 리스크 {risk:.1f}%로 한도 4.0%를 넘겼다")


# ───────────────── 백테스트 파리티 ─────────────────

def test_backtest_models_the_same_policy():
    """[파리티] 백테스트와 실매매가 같은 규칙을 써야 한다.

    두 경로가 다르면 config.py에 기록된 사이징 결론이 실매매에 적용되지 않는다.
    run_portfolio는 oversize_limit 미지정 시 config를 읽어야 한다.
    """
    import inspect
    src = inspect.getsource(pb.run_portfolio)
    assert "MAX_POSITION_OVERSHOOT" in src, \
        "백테스트가 실매매와 같은 설정을 보지 않는다 — 두 경로가 갈라진다"


def test_backtest_skips_what_live_skips():
    """같은 조건에서 백테스트도 초과 집행을 거부해야 한다."""
    # oversize_limit=1.0 이면 1주도 못 사는 후보를 건너뛴다(종전 백테스트 동작).
    import inspect
    src = inspect.getsource(pb.run_portfolio)
    assert "oversize_limit <= 1.0" in src and "skipped_qty0 += 1" in src
