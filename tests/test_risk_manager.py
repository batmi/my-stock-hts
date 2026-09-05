import pytest
import math
from modules.auto_trade import RiskManager
from modules.auto_trade import engine
from unittest.mock import patch
import config

# Mock Trader 클래스 (RiskManager 초기화용)
class MockTrader:
    def __init__(self, initial_asset=10_000_000):
        self.initial_asset = initial_asset
    def log(self, msg):
        pass # 테스트 중 로그 출력 생략
    def stop(self):
        pass # Mock stop method
    def halt_buys(self, reason, notify_msg=None):
        return True # [방어 모드] 신규 매수 중단 (청산은 계속)

@pytest.fixture
def risk_manager():
    trader = MockTrader()
    return RiskManager(trader)

def test_volatility_targeting_high_vol(risk_manager):
    """변동성이 높을 때 투자 비중 축소 테스트"""
    # 설정: 목표 변동성 20%, 최대 2배, 최소 0.3배
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.3
    
    avail_cash = 10_000_000
    invest_ratio = 0.1 # 기본 투자금 100만원 (자산의 10%)
    
    # 상황: ATR이 높음 -> 연환산 변동성 약 40% 가정
    # Annual Vol = (ATR / Price) * sqrt(252)
    # 0.4 ~= (ATR / 10000) * 15.87
    # ATR ~= 252
    current_price = 10000
    atr = 252 
    
    # 예상 Scale = 목표(0.2) / 현재(0.4) = 0.5
    # 투자금 = 1,000,000 * 0.5 = 500,000원
    
    invest_amt = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr, current_price=current_price
    )
    
    # 계산 오차 고려하여 범위 확인
    assert 490_000 <= invest_amt <= 510_000

def test_volatility_targeting_low_vol_capped_at_base(risk_manager):
    """[집중 캡] 저변동성 확대 스케일은 기초 비중(base)을 초과하지 못하고 클램프된다.

    변동성 관리 강조 fix: 종목당 명목 상한(SYSTEM_INVEST_PER_STOCK)을 넘는 몰빵을 방지하기 위해,
    변동성 타겟팅의 확대(scale>1) 결과가 base_amt를 넘지 않도록 최종 클램프한다.
    (예전에는 base×scale이 그대로 반영되어 한 종목이 자본의 최대 50%까지 커질 수 있었다.)"""
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0

    avail_cash = 10_000_000
    invest_ratio = 0.1 # 기본 투자금(base) 100만원

    # 상황: ATR이 낮음 -> 연환산 변동성 약 10% -> Scale ≈ 2.0 (확대)
    current_price = 10000
    atr = 63

    invest_amt = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr, current_price=current_price
    )

    # 확대 스케일(2.0)에도 불구하고 base(100만원)로 클램프됨
    assert invest_amt == 1_000_000

def test_risk_per_trade_limit(risk_manager):
    """리스크 기반 포지션 사이징 테스트 (손절폭이 클 때 비중 축소)"""
    config.SYSTEM_RISK_PER_TRADE = 1.0 # 1회 매매 시 자산의 1% 리스크만 허용
    config.USE_VOLATILITY_TARGETING = False # 변동성 타겟팅 끄고 테스트
    # 갭 버퍼는 별도 테스트에서 검증하므로 여기선 1.0으로 격리 (순수 리스크 수식 검증)
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.0)

    # 자산 1000만원 -> 1% 리스크 허용액 = 10만원

    avail_cash = 10_000_000
    invest_ratio = 0.5 # 기본 비중 50% (500만원) 시도

    # Case 1: 손절폭이 20%로 매우 큰 경우
    # 500만원 투자 시 손실액 = 100만원 -> 허용액(10만원) 초과
    # 적정 투자금 = 허용액(10만원) / 손절률(0.2) = 50만원
    invest_amt = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=-20.0, atr=None, current_price=None
    )

    assert invest_amt == 500_000

    # Case 2: 손절폭이 1%로 작은 경우
    # 500만원 투자 시 손실액 = 5만원 -> 허용액(10만원) 이내
    # 따라서 기본 비중(500만원) 그대로 투자
    invest_amt_safe = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=-1.0, atr=None, current_price=None
    )

    assert invest_amt_safe == 5_000_000


def test_gap_risk_buffer_reduces_size(risk_manager):
    """[갭 버퍼] 손절폭에 GAP_RISK_BUFFER를 곱해 리스크 기반 투자금을 보수적으로 축소"""
    config.SYSTEM_RISK_PER_TRADE = 1.0
    config.USE_VOLATILITY_TARGETING = False
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.25)

    # 허용액 10만원, 손절 -20% → 유효 손절폭 = 0.20 × 1.25 = 0.25
    # 투자금 = 100,000 / 0.25 = 400,000원 (버퍼 미적용 시 500,000원)
    invest_amt = risk_manager.allocate_budget(
        10_000_000, 0.5, stop_loss_rate=-20.0, atr=None, current_price=None
    )
    assert invest_amt == 400_000
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.2)


def test_risk_scale_shrinks_size(risk_manager):
    """[리스크 스케일링] risk_scale(<1)이 리스크 허용액을 배수만큼 축소"""
    config.SYSTEM_RISK_PER_TRADE = 1.0
    config.USE_VOLATILITY_TARGETING = False
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.0)
    risk_manager.trader.risk_scale = 0.5  # 약세/드로다운 축소 가정

    # 허용액 = 1000만 × 1% × 0.5 = 5만원, 손절 -20% → 5만/0.2 = 25만원
    invest_amt = risk_manager.allocate_budget(
        10_000_000, 0.5, stop_loss_rate=-20.0, atr=None, current_price=None
    )
    assert invest_amt == 250_000
    risk_manager.trader.risk_scale = 1.0

def test_volatility_targeting_scaling_limits(risk_manager):
    """변동성 타겟팅 스케일링 제한(Max/Min) 테스트"""
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 1.5 # 최대 1.5배로 제한
    config.VOLATILITY_SCALING_MIN = 0.5 # 최소 0.5배로 제한
    
    avail_cash = 10_000_000
    invest_ratio = 0.1 # 100만원
    current_price = 10000
    
    # 1. 극도로 낮은 변동성 (Scale > 1.5 상황)
    # 변동성 5% -> Scale 4.0 -> Max 1.5 적용, 다시 base(100만원)로 클램프됨
    # (변동성 관리 fix: 확대는 종목당 명목 상한 base를 넘을 수 없음)
    atr_low = 31.5 # ~5% vol
    invest_amt_max = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr_low, current_price=current_price
    )
    assert invest_amt_max == 1_000_000 # min(100만 * 1.5, base 100만) = 100만
    
    # 2. 극도로 높은 변동성 (Scale < 0.5 상황)
    # 변동성 80% -> Scale 0.25 -> Min 0.5 적용되어야 함
    atr_high = 504 # ~80% vol
    invest_amt_min = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr_high, current_price=current_price
    )
    assert invest_amt_min == 500_000 # 100만 * 0.5

def test_risk_and_volatility_caps_combine_by_min(risk_manager):
    """[중복 축소 방지] 리스크 캡과 변동성 캡은 곱셈이 아니라 min()으로 결합한다.

    실측 회귀(2026-07-23 GS건설): 자산 9,951,160 / 기초 25% / risk_scale 0.6 /
    ATR 손절 -15% / 연변동성 134%(scale는 하한 0.40에 클램프).
      종전(곱셈): min(기초, 리스크) 1,326,821 × 0.40 = 530,728  → 기초의 21%
      min 결합 : min(기초 2,487,790, 리스크 1,326,821, 변동성 995,116) = 995,116

    [2026-07-27] risk_scale을 리스크층이 아닌 기초 비중에 적용하도록 바꾸면서 기대값이 바뀐다.
      기초 2,487,790 × 0.6 = 1,492,674 → 변동성 캡 1,492,674 × 0.40 = 597,069
    min 결합이라는 검증 대상 자체는 그대로다(곱셈이었다면 더 작았을 것).
    """
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.40
    config.SYSTEM_RISK_PER_TRADE = 4.0
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.2)

    risk_manager.trader.initial_asset = 9_951_160
    risk_manager.trader.risk_scale = 0.6
    try:
        price, atr = 34_500, 2_912      # 연변동성 ≈ 134% → scale 0.149 → 하한 0.40
        amt = risk_manager.allocate_budget(
            9_951_160, 0.25, stop_loss_rate=-15.0, atr=atr, current_price=price
        )
        assert amt == 597_069           # (기초 2,487,790 × 배수 0.6) × 0.40 — 변동성 캡이 최소
        assert amt > 530_728            # 종전 곱셈 결과보다는 여전히 커야 한다

        # 손실액 캡은 여전히 불가침: 최종액 × 손절폭 ≤ 자산 × (리스크% × 스케일)
        assert amt * 0.15 <= 9_951_160 * (4.0 * 0.6 / 100.0)
    finally:
        risk_manager.trader.risk_scale = 1.0
        risk_manager.trader.initial_asset = 10_000_000


def test_base_ratio_change_now_moves_final_amount(risk_manager):
    """기초 비중을 올리면 최종 매수금도 (리스크 캡 한도까지) 실제로 늘어난다.

    종전 곱셈 결합에서는 리스크 캡이 바인딩하면 기초 비중을 2배로 올려도 최종액이
    똑같아 설정이 사문화됐다(0.25·0.5 모두 530,728원).

    [2026-07-27] risk_scale이 기초 비중에 적용되면서 변동성 캡도 함께 내려가므로,
    이 조건에서는 리스크 캡(1,326,821)이 아니라 변동성 캡이 상한이 된다.
    검증 대상('기초 비중을 올리면 최종액이 실제로 따라 오른다')은 그대로 성립한다.
    """
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MIN = 0.40
    config.VOLATILITY_SCALING_MAX = 2.0
    config.SYSTEM_RISK_PER_TRADE = 4.0
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.2)

    risk_manager.trader.initial_asset = 9_951_160
    risk_manager.trader.risk_scale = 0.6
    try:
        kw = dict(stop_loss_rate=-15.0, atr=2_912, current_price=34_500)
        small = risk_manager.allocate_budget(9_951_160, 0.25, **kw)
        large = risk_manager.allocate_budget(9_951_160, 0.50, **kw)
        assert large > small
        assert small == 597_069         # (9,951,160 × 0.25 × 0.6) × 0.40
        assert large == 1_194_139       # (9,951,160 × 0.50 × 0.6) × 0.40 — 변동성 캡에서 멈춘다
        assert large <= 9_951_160 * 0.50 * 0.6   # 기초 비중(집중 캡)을 넘지 않는다
    finally:
        risk_manager.trader.risk_scale = 1.0
        risk_manager.trader.initial_asset = 10_000_000


@patch('modules.auto_trade.api.send_telegram_message')
def test_check_loss_limit_triggered(mock_tg, risk_manager):
    """손실 한도 초과 시 '신규 매수 중단(방어 모드)' 테스트

    [추세추종] 시스템 정지(stop)는 매도 감시까지 꺼서 손절을 무력화하므로 호출되지 않아야 한다.
    """
    risk_manager.trader.initial_asset = 10_000_000
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0 # 5% limit

    # Current asset: 9,000,000 (-10% loss)
    current_total = 9_000_000

    with patch.object(risk_manager.trader, 'stop') as mock_stop, \
         patch.object(risk_manager.trader, 'halt_buys', return_value=True) as mock_halt:
        risk_manager.check_loss_limit(current_total)

        mock_halt.assert_called_once()
        mock_stop.assert_not_called()
        assert "방어 모드" in mock_halt.call_args.kwargs['notify_msg']

def test_check_loss_limit_safe(risk_manager):
    """손실 한도 이내일 때 테스트"""
    risk_manager.trader.initial_asset = 10_000_000
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0

    # Current asset: 9,600,000 (-4% loss)
    current_total = 9_600_000

    with patch.object(risk_manager.trader, 'stop') as mock_stop:
        risk_manager.check_loss_limit(current_total)
        mock_stop.assert_not_called()


# =========================================================
# [추가] 포트폴리오 히트(총 오픈 리스크) 계산·예산 테스트
# =========================================================
import threading

class MockHeatTrader(MockTrader):
    """compute_portfolio_heat가 참조하는 트레일링 캐시/락 포함 Mock"""
    def __init__(self, initial_asset=10_000_000):
        super().__init__(initial_asset)
        self._lock = threading.RLock()
        self.trailing_stop_cache = {}
        self.portfolio_heat_amt = 0.0


def _holding(code, qty, buy, cur):
    return {'pdno': code, 'hldg_qty': str(qty), 'pchs_avg_pric': str(buy), 'prpr': str(cur)}


def test_portfolio_heat_basic(monkeypatch):
    """오픈 리스크 = 수량 × (매수가 - 손절선). 손절률은 매수기록 수량가중 평균 사용"""
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_ACTIVATION_RATE', 10.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)
    trader.trailing_stop_cache['005930'] = 10000.0  # 최고가=매수가 → BEP/TS 미발동

    heat = rm.compute_portfolio_heat(
        [_holding('005930', 10, 10000, 10000)],
        {'005930': [{'qty': 10, 'stop_loss_rate': -5.0}]},
    )
    # 손절선 = 10000×0.95=9500 → 리스크 = 10주 × 500원
    assert heat == pytest.approx(10 * 500)


def test_portfolio_heat_bep_uplift_follows_toggle(monkeypatch):
    """BEP 상향은 USE_BREAK_EVEN_STOP을 따른다 — 없는 손절선을 가정하면 안 된다.

    [왜 양쪽을 다 거는가] 이 함수는 2026-08-19까지 토글을 보지 않고 **항상** 손절선을
     매수가로 끌어올렸다. 현행 기본값이 OFF라 실제 청산 로직에는 존재하지 않는 손절선을
     가정한 셈이고, 그만큼 오픈 리스크를 과소 계상했다(실측 히트 -30%, 히트 예산 초과일
     0.8% → 0.0%). 함수 독스트링이 표방하는 '보수적 = 과대평가' 방향과 정반대다.
     한쪽만 걸면 다음 사람이 조건을 지우고도 테스트를 통과시킬 수 있으므로 둘 다 건다.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_ACTIVATION_RATE', 10.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)

    # 최고가 +6% ≥ 손절폭 5%(ATR 동기화) → BEP 발동선은 넘겼다. 무장 여부만 다르다.
    trader.trailing_stop_cache['000001'] = 10600.0
    holding = [_holding('000001', 10, 10000, 10300)]
    trades = {'000001': [{'qty': 10, 'stop_loss_rate': -5.0}]}

    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', True)
    # 손절선이 본전(10000)으로 상향 → 자본 리스크 0 (더 이상 원금을 잃지 않는다)
    assert rm.compute_portfolio_heat(holding, trades) == pytest.approx(0.0)

    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    # 상향 없음 → 손절선은 그대로 9500 → 리스크 = 10주 × (10000 - 9500)
    assert rm.compute_portfolio_heat(holding, trades) == pytest.approx(10 * 500)


def test_portfolio_heat_ts_uplift(monkeypatch):
    """트레일링이 무장했으면 손절선이 고점 기준으로 올라가 오픈 리스크가 줄어든다.

    TS는 BEP 토글과 무관하다 — 주청산 경로라 항상 살아 있다.
    다만 상향폭은 **실효 콜백**(샹들리에)이 정한다 — 아래 SSOT 테스트 참조.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_ACTIVATION_RATE', 10.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'ATR_STOP_MULTIPLIER', 2.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_ATR_MULTIPLIER', 3.5)

    # 역산 ATR = 5%×10000/2 = 250 → 실효 콜백 = max(5%, 250×3.5/12000) = 7.29%
    #  → 손절선 = 12000×(1-7.29%) = 11125. 매수가(10000) 위이므로 자본 리스크는 0이다 —
    #  진입 대비 기준에서 '무장한 승자'는 캡을 쓰지 않는다(추세추종의 요점).
    trader.trailing_stop_cache['000002'] = 12000.0
    heat2 = rm.compute_portfolio_heat(
        [_holding('000002', 10, 10000, 11500)],
        {'000002': [{'qty': 10, 'stop_loss_rate': -5.0}]},
    )
    assert heat2 == pytest.approx(0.0)
    # 무장 전(손절선 9500 → 5,000원)보다 작다 — 상향 자체는 여전히 일어난다.
    assert heat2 < 10 * (10000 - 9500)


def test_portfolio_heat_ts_callback_matches_exit_logic(monkeypatch):
    """[SSOT] 히트가 가정하는 TS 청산선 = 청산 로직(compute_trailing_stop)의 청산선.

    [왜 걸어 두는가] 2026-08-29까지 이 함수는 콜백으로 고정 하한
     (TRAILING_STOP_CALLBACK_RATE, 5%)만 썼다. 실제 주청산선은 샹들리에
     max(하한, ATR×TRAILING_ATR_MULTIPLIER)이고 max이므로 **항상 하한 이상**이다
     — 즉 실제보다 높은 손절선을 가정해 오픈 리스크를 과소 계상했다. 히트 캡은
     리스크 스케일링의 실효 방어 경로라(config SYSTEM_MAX_PORTFOLIO_RISK 주석)
     과소 계상은 방어를 그만큼 무르게 만든다.
     ATR 하나로 두 경로를 같은 자리에 세워, 한쪽만 바뀌면 실패하게 한다.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'ATR_STOP_MULTIPLIER', 2.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_ATR_MULTIPLIER', 3.5)

    buy, high, cur, qty = 10000.0, 14000.0, 13000.0, 10
    atr = 600.0                       # 손절률 -12% = ATR 600 × 배수 2 / 매수가
    sl_rate = -(atr * 2 / buy) * 100

    trader.trailing_stop_cache['000004'] = high
    heat = rm.compute_portfolio_heat(
        [_holding('000004', qty, buy, cur)],
        {'000004': [{'qty': qty, 'stop_loss_rate': sl_rate}]},
    )

    info = engine.compute_trailing_stop(highest_price=high, buy_price=buy,
                                        current_price=cur, ind={'atr': atr})
    assert info['armed'], "이 표본은 TS가 무장한 상태여야 한다"
    assert heat == pytest.approx(qty * max(0.0, buy - info['stop_price']))

    # 고정 콜백(옛 산식)이었다면 청산선을 高×0.95 = 13300 으로 봤을 것이다. 진입 대비
    #  기준에서는 두 선 다 매수가 위라 결과가 0으로 같아지므로, 청산선 자체를 건다.
    _t, detail = rm.compute_portfolio_heat(
        [_holding('000004', qty, buy, cur)],
        {'000004': [{'qty': qty, 'stop_loss_rate': sl_rate}]}, detail=True)
    assert detail['000004'][0] == pytest.approx(info['stop_price'])
    assert detail['000004'][0] < high * (1 - 5.0 / 100.0), \
        "고정 하한 콜백으로 되돌아갔다 — 실제보다 높은 청산선을 가정한다"


def test_portfolio_heat_locked_position_zero_risk(monkeypatch):
    """손절선이 현재가 위(이익 잠김)면 해당 포지션 리스크는 0"""
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_ACTIVATION_RATE', 10.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)

    # 최고가 15000 → TS선 14250 > 현재가 14000 → 리스크 0 (음수 아님)
    trader.trailing_stop_cache['000003'] = 15000.0
    heat = rm.compute_portfolio_heat(
        [_holding('000003', 10, 10000, 14000)],
        {'000003': [{'qty': 10, 'stop_loss_rate': -5.0}]},
    )
    assert heat == 0.0


def test_portfolio_risk_budget_left(monkeypatch):
    """히트 캡까지 남은 예산 계산 및 캡 미사용(0) 시 None"""
    trader = MockHeatTrader(10_000_000)
    rm = RiskManager(trader)

    monkeypatch.setattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0, raising=False)
    trader.portfolio_heat_amt = 400_000
    assert rm.portfolio_risk_budget_left() == pytest.approx(600_000)

    monkeypatch.setattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 0.0, raising=False)
    assert rm.portfolio_risk_budget_left() is None


def test_portfolio_risk_budget_uses_current_asset(monkeypatch):
    """[히트 캡 기준자산] 현재 평가자산이 있으면 그 값을 기준으로 예산 산출 (보수적)"""
    trader = MockHeatTrader(10_000_000)
    rm = RiskManager(trader)
    monkeypatch.setattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0, raising=False)

    # 장중 손실로 현재자산이 800만원으로 감소 → 예산도 함께 축소
    trader.current_total_asset = 8_000_000
    trader.portfolio_heat_amt = 0
    assert rm.portfolio_risk_budget_left() == pytest.approx(800_000)  # 800만 × 10%


def test_portfolio_risk_budget_scaled_by_risk_scale(monkeypatch):
    """[리스크 스케일링] risk_scale(<1)이 히트 캡 예산을 배수만큼 축소"""
    trader = MockHeatTrader(10_000_000)
    rm = RiskManager(trader)
    monkeypatch.setattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0, raising=False)
    trader.portfolio_heat_amt = 0
    trader.risk_scale = 0.5  # 약세/드로다운 축소

    # 실효 캡 = 10% × 0.5 = 5% → 1000만 × 5% = 50만원
    assert rm.portfolio_risk_budget_left() == pytest.approx(500_000)
    assert rm.effective_portfolio_cap() == pytest.approx(5.0)
    trader.risk_scale = 1.0

# ─────────── 실측 손절선(live_map) — 역산 근사가 리스크를 과소 계상한다 ───────────

def test_portfolio_heat_uses_live_atr_instead_of_back_derivation(monkeypatch):
    """[핵심] 히트는 **지금의 ATR**로 청산선을 잡아야 한다 — 진입 시점 역산은 과소 계상한다.

    역산(ATR = |진입 손절률|×매수가/배수)은 '진입 시점의 변동성이 지금도 그대로'라는
    가정이다. 추세추종에서는 변동성이 커지는 쪽이 흔하고, 그러면 트레일링 **발동선**이
    뒤로 밀린다. 역산은 그 사실을 모르므로 아직 무장하지도 않은 포지션을 '무장했다'고
    보고 청산선을 매수가 위로 올려 버린다 = **자본 리스크를 0으로 착각한다.**

    이 표본이 정확히 그 경우다. 진입 후 ATR이 250 → 600이 되면 발동선은 8.1% → 22.0%로
    밀리는데, 지금 수익률은 +15%다 — 실제로는 아직 무장 전이고 원금의 5%가 걸려 있다.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TS_ACTIVATION_MODE', 'breakeven')
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'ATR_STOP_MULTIPLIER', 2.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_ATR_MULTIPLIER', 3.5)

    buy, high, cur, qty = 10000.0, 11500.0, 11400.0, 10
    live_atr = 600.0                       # 지금의 ATR (진입 시 250에서 확대)
    trades = {'000005': [{'qty': qty, 'stop_loss_rate': -5.0}]}   # 역산 ATR = 250
    holding = [_holding('000005', qty, buy, cur)]
    trader.trailing_stop_cache['000005'] = high

    assert engine.breakeven_activation_rate(250.0, buy, 5.0) < 15.0 < \
        engine.breakeven_activation_rate(live_atr, buy, 5.0), \
        "이 표본은 '역산은 무장, 실제는 대기'여야 의미가 있다"

    before = rm.compute_portfolio_heat(holding, trades)
    after = rm.compute_portfolio_heat(
        holding, trades, live_map={'000005': {'sl_rate': -5.0, 'atr': live_atr}})

    assert before == 0.0, "역산은 이 포지션을 '리스크 없음'으로 본다"
    assert after == pytest.approx(qty * (buy - buy * 0.95)), \
        "실제로는 아직 무장 전이라 원금의 5%가 걸려 있다"


def test_portfolio_heat_uses_live_stop_when_there_is_no_buy_record(monkeypatch):
    """매수 기록이 없는 포지션(HTS·MTS 직접 매수)의 손절선도 실제 판정과 맞아야 한다.

    여기서는 기록이 없으면 전역 STOP_LOSS_RATE로 떨어진다. 그러나 실제 매도 판정은
    build_sell_thresholds가 **진입 봉 ATR에서 복원한** 손절률을 쓴다(최대
    MAX_ATR_STOP_LOSS_RATE, 현행 -15%). 전역값(-7%)보다 넓으므로 종전 산식은
    같은 방향으로 리스크를 과소 계상했다.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    monkeypatch.setitem(config.SELL_STRATEGY, 'STOP_LOSS_RATE', -7.0)
    # 손실 중이라 TS는 무장하지 않는다 — 손절선 하나만 비교한다.
    holding = [_holding('000006', 10, 10000, 9800)]
    trader.trailing_stop_cache['000006'] = 10000.0

    before = rm.compute_portfolio_heat(holding, {})
    after = rm.compute_portfolio_heat(
        holding, {}, live_map={'000006': {'sl_rate': -15.0, 'atr': 750.0}})

    assert before == pytest.approx(10 * (10000 - 9300))    # 전역 -7%
    assert after == pytest.approx(10 * (10000 - 8500))     # 실제 판정선 -15%


def test_portfolio_heat_falls_back_when_live_values_are_unusable(monkeypatch):
    """[모르면 안 건드린다] 실측값이 없거나 숫자가 아니면 종전 근사로 돌아간다.

    이 캐시는 직전 주기의 매도 판정이 채운다 — 기동 직후나 분석에 실패한 종목은
    비어 있다. 비었다고 리스크를 0으로 보거나 예외를 내면, 채우려던 구멍보다
    큰 구멍이 생긴다.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    trader.trailing_stop_cache['000007'] = 10000.0
    holding = [_holding('000007', 10, 10000, 9800)]
    trades = {'000007': [{'qty': 10, 'stop_loss_rate': -5.0}]}

    base = rm.compute_portfolio_heat(holding, trades)
    for live in ({}, {'000007': {}}, {'000007': {'sl_rate': None, 'atr': None}},
                 {'000007': {'sl_rate': 'x', 'atr': 'y'}},
                 {'000007': {'sl_rate': 3.0, 'atr': -5.0}},      # 양수 손절·음수 ATR = 쓸 수 없다
                 {'999999': {'sl_rate': -15.0, 'atr': 900.0}}):  # 다른 종목의 값
        assert rm.compute_portfolio_heat(holding, trades, live_map=live) == pytest.approx(base), live


def test_open_risk_is_measured_from_the_entry_not_the_mark(monkeypatch):
    """[정의] 오픈 리스크 = 진입 대비 손실. 현재가가 올라도 히트는 커지지 않는다.

    [왜 이 정의인가 · 2026-09-01] 종전에는 '현재가 → 손절선'으로 쟀다. 그러면 손절선이
    고정된 채 현재가만 올라도 히트가 부푼다 — 실측(한국콜마)으로 +10%에서 1.80배,
    TS 무장 직전 최대 2.45배. 피라미딩 발동선(+10%)과 TS 무장(+23%) 사이가 정확히 히트
    최대 구간이라, **추세가 잘 될수록 증액이 캡에 막혔다**(2026-08-28 실운영에서 206주기
    차단). 추세추종의 정반대다. 벌어 둔 미실현 이익을 되뱉는 것은 자본의 손실이 아니고,
    그 반납을 관리하는 장치는 캡이 아니라 트레일링 스탑이다.

    이 테스트가 깨지면 그 성질이 되돌아온 것이다.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TS_ACTIVATION_MODE', 'breakeven')
    trades = {'000008': [{'qty': 10, 'stop_loss_rate': -5.0}]}
    trader.trailing_stop_cache['000008'] = 10000.0     # 고점 = 매수가 → TS 대기

    at_entry = rm.compute_portfolio_heat([_holding('000008', 10, 10000, 10000)], trades)
    up_8pct = rm.compute_portfolio_heat([_holding('000008', 10, 10000, 10800)], trades)
    assert at_entry == pytest.approx(10 * 500)
    assert up_8pct == pytest.approx(at_entry), "이익이 나자 히트가 부풀었다 — 기준이 현재가로 돌아갔다"


def test_detail_reports_the_stop_line_per_position(monkeypatch):
    """표시부가 총합에서 손절선을 되짚지 않아도 되게 한다.

    되짚기(충분히 높은 가격을 넣고 빼기)는 진입 대비 기준에서 성립하지 않는다 —
    이익이 잠긴 포지션은 리스크가 0이라 되짚을 것이 없다.
    """
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_BREAK_EVEN_STOP', False)
    trader.trailing_stop_cache['000009'] = 10000.0
    total, detail = rm.compute_portfolio_heat(
        [_holding('000009', 10, 10000, 9800)],
        {'000009': [{'qty': 10, 'stop_loss_rate': -5.0}]}, detail=True)
    assert total == pytest.approx(10 * 500)
    assert detail['000009'] == (pytest.approx(9500.0), pytest.approx(5000.0))
    assert rm.compute_portfolio_heat([], {}, detail=True) == (0.0, {})


# ==========================================================
# [상시 구속층이 조용히 빠지지 않게 한다] (2026-09-06)
# ==========================================================
#  allocate_budget 독스트링의 실측: "이 층(2-리스크)은 최종액을 결정하지 않는다 —
#  관심종목 50종목 전부에서 3)변동성이 구속하고 2)의 상한은 항상 그보다 크다."
#  그러므로 ATR 을 못 구하면 실제로 구속하던 층이 통째로 빠지고 배분액이 2)까지 올라간다
#  — 변동성을 모르는 종목을 **더 크게** 사는 방향이다(실측 624,941원 → 1,000,000원, +60%).
#  설정으로 끄면 settings 가 요란하게 경고하는 안전장치인데
#  (USE_VOLATILITY_TARGETING: "MDD -20%→-30%"), 값이 없어 꺼지면 로그 한 줄도 없었다.

class _RecordingTrader(MockTrader):
    def __init__(self):
        super().__init__()
        self.lines = []

    def log(self, msg):
        self.lines.append(msg)


@pytest.fixture
def recording_rm():
    """이 파일의 다른 테스트가 config 를 전역으로 바꾸므로 필요한 값만 직접 못박는다."""
    saved = (config.TARGET_VOLATILITY, config.VOLATILITY_SCALING_MAX,
             config.VOLATILITY_SCALING_MIN, config.USE_VOLATILITY_TARGETING)
    config.TARGET_VOLATILITY = 0.25
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.4
    config.USE_VOLATILITY_TARGETING = True
    trader = _RecordingTrader()
    yield RiskManager(trader), trader
    (config.TARGET_VOLATILITY, config.VOLATILITY_SCALING_MAX,
     config.VOLATILITY_SCALING_MIN, config.USE_VOLATILITY_TARGETING) = saved


_KW = dict(avail_cash=10_000_000, invest_ratio=0.1, stop_loss_rate=-7.0, current_price=10000)


def test_ATR을_못_구하면_배분액이_실제로_올라간다(recording_rm):
    """전제 확인 — 이 차이가 없으면 아래 경고는 의미가 없다."""
    rm, _ = recording_rm
    with_atr = rm.allocate_budget(atr=252, **_KW)
    without = rm.allocate_budget(atr=None, **_KW)
    assert without > with_atr, (
        f"ATR 유무로 배분액이 같다 — 변동성 층이 구속하지 않는 설정이다 "
        f"({with_atr:,} vs {without:,})")


def test_변동성캡_미적용을_로그에_남긴다(recording_rm):
    rm, trader = recording_rm
    rm.allocate_budget(atr=None, **_KW)
    text = "\n".join(trader.lines)
    assert "변동성캡:미적용" in text, (
        f"구속하던 층이 빠졌는데 로그에 아무 흔적이 없다: {text}")


def test_정상_적용은_종전대로_찍힌다(recording_rm):
    rm, trader = recording_rm
    rm.allocate_budget(atr=252, **_KW)
    text = "\n".join(trader.lines)
    assert "변동성캡:" in text and "미적용" not in text
