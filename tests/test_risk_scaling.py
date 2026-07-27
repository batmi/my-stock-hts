"""리스크 한도 동적 스케일링 테스트 (약세 국면·계좌 드로다운 연동)

[추세추종 2원칙] "자본대비 리스크에 한도를 둬야 한다" 구현부(_update_risk_scale,
_get_account_drawdown_pct)와 피라미딩 시장 필터 게이트를 검증한다.
"""
import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config


@pytest.fixture
def trader():
    t = AutoTrader()
    # 테스트 격리: 상태 초기화
    t.initial_asset = 10_000_000
    t.current_total_asset = 10_000_000
    t.risk_scale = 1.0
    t.risk_scale_reason = ""
    t._hwm_cache = 0.0
    t._hwm_cache_date = None
    t.market_index_status = {}
    return t


@pytest.fixture(autouse=True)
def restore_risk_params():
    orig = dict(config.RISK_SCALING_PARAMS)
    yield
    config.RISK_SCALING_PARAMS = orig


# =========================================================
# _update_risk_scale — 시장 국면 연동
# =========================================================
def _patch_regime(regime, whipsaw=None, score_adj=0.0):
    """국면 상세(get_market_regime_detail) 모킹 헬퍼."""
    return patch('modules.auto_trade.trader.analysis.get_market_regime_detail',
                 return_value={'regime': regime, 'score_adj': score_adj,
                               'moved_pct': 0.0, 'whipsaw_ratio': whipsaw, 'segments': 10})


def _regime_only_params(**kw):
    p = dict(config.RISK_SCALING_PARAMS,
             USE_REGIME_RISK_SCALING=True, USE_WHIPSAW_RISK_SCALING=False,
             PENDING_DOWN_RISK_SCALE=0.6, BEAR_RISK_SCALE=1.0,
             USE_DRAWDOWN_RISK_SCALING=False)
    p.update(kw)
    return p


def test_risk_scale_pending_down_regime(trader):
    """하락 미확정(PendDown) 국면이면 PENDING_DOWN_RISK_SCALE로 축소된다.

    15년 백테스트상 리스크를 줄여야 할 구간은 확정 Bear가 아니라 추세 붕괴 초기다."""
    config.RISK_SCALING_PARAMS = _regime_only_params()
    with _patch_regime("PendDown"):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.6)


def test_risk_scale_confirmed_bear_no_change_by_default(trader):
    """확정 약세(Bear)는 기본값(BEAR_RISK_SCALE=1.0)에서 축소하지 않는다.

    이미 확인 기준만큼 하락한 뒤라 향후 수익률이 오히려 양(+)이었기 때문."""
    config.RISK_SCALING_PARAMS = _regime_only_params()
    with _patch_regime("Bear"):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(1.0)


def test_risk_scale_bear_regime_opt_in(trader):
    """BEAR_RISK_SCALE를 1.0 미만으로 두면 확정 약세에도 축소가 적용된다(옵트인)."""
    config.RISK_SCALING_PARAMS = _regime_only_params(BEAR_RISK_SCALE=0.75)
    with _patch_regime("Bear"):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.75)


def test_risk_scale_bull_regime_no_change(trader):
    """강세/상승 미확정 국면이면 국면에 의한 축소는 없다(1.0 유지)."""
    config.RISK_SCALING_PARAMS = _regime_only_params()
    for regime in ("Bull", "PendUp", "Sideways"):
        trader.risk_scale = 1.0
        with _patch_regime(regime):
            trader._update_risk_scale()
        assert trader.risk_scale == pytest.approx(1.0), regime


# =========================================================
# _update_risk_scale — 휩소율 연동
# =========================================================
def _whipsaw_params(**kw):
    p = dict(config.RISK_SCALING_PARAMS,
             USE_REGIME_RISK_SCALING=False, USE_WHIPSAW_RISK_SCALING=True,
             WHIPSAW_LO=0.40, WHIPSAW_HI=0.75, WHIPSAW_MIN_SCALE=0.6,
             USE_DRAWDOWN_RISK_SCALING=False)
    p.update(kw)
    return p


@pytest.mark.parametrize("whipsaw,expected", [
    (0.0,  1.0),    # 교차가 전부 성공 — 추세가 잘 먹히는 장
    (0.40, 1.0),    # 하한 경계
    (0.575, 0.8),   # 중간 → 선형 보간
    (0.75, 0.6),    # 상한 경계 → 최소 배수
    (1.0,  0.6),    # 상한 초과에도 최소 배수 유지
    (None, 1.0),    # 산출 불가(교차 표본 부족) → 축소 없음
])
def test_whipsaw_risk_scale_interpolation(trader, whipsaw, expected):
    """휩소율 → 배수 선형 보간이 경계 포함해 의도대로 동작한다."""
    config.RISK_SCALING_PARAMS = _whipsaw_params()
    with _patch_regime("Bull", whipsaw=whipsaw):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(expected)


def test_whipsaw_and_regime_compound(trader):
    """국면 배수 × 휩소율 배수는 같은 시장 안에서 곱으로 결합된다."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=True, USE_WHIPSAW_RISK_SCALING=True,
                                      PENDING_DOWN_RISK_SCALE=0.6, BEAR_RISK_SCALE=1.0,
                                      WHIPSAW_LO=0.40, WHIPSAW_HI=0.75, WHIPSAW_MIN_SCALE=0.6,
                                      USE_DRAWDOWN_RISK_SCALING=False)
    with _patch_regime("PendDown", whipsaw=0.75):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.6 * 0.6)


def test_worst_of_two_markets_is_used(trader):
    """KOSPI/KOSDAQ 배수가 다르면 더 보수적인(작은) 쪽을 채택한다 — 곱하지 않는다."""
    config.RISK_SCALING_PARAMS = _regime_only_params()
    per_market = {
        "KOSPI": {'regime': "Bull", 'score_adj': -0.5, 'moved_pct': 8.0,
                  'whipsaw_ratio': None, 'segments': 10},
        "KOSDAQ": {'regime': "PendDown", 'score_adj': 0.5, 'moved_pct': -2.0,
                   'whipsaw_ratio': None, 'segments': 10},
    }
    with patch('modules.auto_trade.trader.analysis.get_market_regime_detail',
               side_effect=lambda m: per_market[m]):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.6)
    assert "KOSDAQ" in trader.risk_scale_reason


# =========================================================
# _update_risk_scale — 드로다운 연동
# =========================================================
def _patch_regime_neutral():
    return _patch_regime("Bull")


def test_risk_scale_drawdown_level1(trader):
    """드로다운 1단계(5%~10%) 진입 시 DD_SCALE_1 적용."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=False, USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75, DD_LEVEL_2=10.0, DD_SCALE_2=0.5)
    # HWM 1000만, 현재 930만 → DD 7% → 1단계
    trader.current_total_asset = 9_300_000
    with _patch_regime_neutral(), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.75)


def test_risk_scale_drawdown_level2(trader):
    """드로다운 2단계(≥10%) 진입 시 DD_SCALE_2 적용."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=False, USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75, DD_LEVEL_2=10.0, DD_SCALE_2=0.5)
    # HWM 1000만, 현재 880만 → DD 12% → 2단계
    trader.current_total_asset = 8_800_000
    with _patch_regime_neutral(), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.5)


def test_risk_scale_regime_and_drawdown_compound(trader):
    """국면 배수 × 드로다운 배수는 곱으로 결합된다."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=True, USE_WHIPSAW_RISK_SCALING=False,
                                      PENDING_DOWN_RISK_SCALE=0.6, BEAR_RISK_SCALE=1.0,
                                      USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75, DD_LEVEL_2=10.0, DD_SCALE_2=0.5)
    trader.current_total_asset = 8_800_000  # DD 12% → 0.5
    with _patch_regime("PendDown"), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.6 * 0.5)


def test_risk_scale_no_drawdown(trader):
    """고점 근처(드로다운 < 1단계)면 축소 없음."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=False, USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75)
    trader.current_total_asset = 9_800_000  # DD 2%
    with _patch_regime_neutral(), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(1.0)


def test_drawdown_pct_uses_initial_asset_as_hwm_floor(trader):
    """HWM은 DB 고점과 당일 시작자산 중 큰 값. DB가 낮아도 시작자산이 바닥선."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, DD_LOOKBACK_DAYS=90)
    trader.initial_asset = 10_000_000
    trader.current_total_asset = 9_000_000
    trader._hwm_cache_date = None
    with patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=8_000_000):
        dd = trader._get_account_drawdown_pct()
    # HWM = max(DB 800만, 시작 1000만) = 1000만 → DD = (1000-900)/1000 = 10%
    assert dd == pytest.approx(10.0)


# =========================================================
# 피라미딩 시장 필터 게이트
# =========================================================
def test_pyramid_blocked_in_bear_market(trader):
    """시장 필터 '보류'(지수<SMA) 시장의 종목은 피라미딩 증액을 보류한다."""
    config.USE_MARKET_FILTER = True
    with patch.dict(config.ANALYSIS_THRESHOLDS, {"PYRAMIDING_REQUIRE_HEALTHY_MARKET": True}):
        trader.market_index_status = {"KOSPI": {"is_healthy": False, "current": 2000}}
        trader.strategy = MagicMock()
        trader.strategy.analyze_pyramid.return_value = (True, "피라미딩 1차")
        # 국내 KOSPI 종목으로 판별되도록 패치
        with patch.object(trader, '_get_stock_market_type', return_value="KOSPI"), \
             patch.object(trader, 'log') as mock_log:
            result = {'state': '매수', 'score': 8.0, 'ind': {}}
            trader._try_pyramid_buy('005930', '삼성전자', 10, 60000, 12.0,
                                    result, last_buy=None, is_market_open=True, rule=None)
            # 증액 주문 경로로 진입하지 않고 보류 로그만 남아야 함
            assert any("약세" in str(c) for c in mock_log.call_args_list)


# =========================================================
# 시장별 배수 분리 (KOSPI/KOSDAQ은 별개 시장)
# =========================================================
def _patch_regime_per_market(mapping):
    """시장별로 다른 국면 상세를 돌려주는 모킹 헬퍼. mapping = {시장: (regime, whipsaw)}"""
    def _detail(m_type, *a, **kw):
        regime, whipsaw = mapping[m_type]
        return {'regime': regime, 'score_adj': 0.0, 'moved_pct': 0.0,
                'whipsaw_ratio': whipsaw, 'segments': 10}
    return patch('modules.auto_trade.trader.analysis.get_market_regime_detail',
                 side_effect=_detail)


def test_risk_scale_is_computed_per_market(trader):
    """코스닥만 나쁠 때 코스피 배수는 1.0으로 남아야 한다.

    종전에는 두 시장 중 열위 쪽 하나를 계좌 전체에 적용해, 코스닥이 톱니장이면
    코스피 종목까지 축소됐다(2026-07-27 SK텔레콤 매수 로그에서 발견)."""
    config.RISK_SCALING_PARAMS = dict(
        config.RISK_SCALING_PARAMS, USE_REGIME_RISK_SCALING=True,
        USE_WHIPSAW_RISK_SCALING=True, USE_DRAWDOWN_RISK_SCALING=False,
        PENDING_DOWN_RISK_SCALE=0.6, BEAR_RISK_SCALE=1.0)
    with _patch_regime_per_market({"KOSPI": ("Bull", 0.0), "KOSDAQ": ("PendDown", 0.9)}):
        trader._update_risk_scale()

    assert trader.risk_scale_by_market["KOSPI"] == pytest.approx(1.0)
    assert trader.risk_scale_by_market["KOSDAQ"] < 0.6
    # 계좌 단위 값(히트 캡용)은 여전히 열위 시장 기준을 유지한다
    assert trader.risk_scale == pytest.approx(trader.risk_scale_by_market["KOSDAQ"])


def test_drawdown_applies_to_both_markets(trader):
    """계좌 드로다운은 시장과 무관하므로 두 시장 배수에 공통으로 곱해진다."""
    config.RISK_SCALING_PARAMS = dict(
        config.RISK_SCALING_PARAMS, USE_REGIME_RISK_SCALING=False,
        USE_WHIPSAW_RISK_SCALING=False, USE_DRAWDOWN_RISK_SCALING=True,
        DD_LEVEL_1=5.0, DD_SCALE_1=0.75, DD_LEVEL_2=10.0, DD_SCALE_2=0.5)
    with _patch_regime_per_market({"KOSPI": ("Bull", 0.0), "KOSDAQ": ("Bull", 0.0)}), \
         patch.object(trader, '_get_account_drawdown_pct', return_value=6.0):
        trader._update_risk_scale()

    assert trader.risk_scale == pytest.approx(0.75)
    assert trader.risk_scale_by_market["KOSPI"] == pytest.approx(0.75)
    assert trader.risk_scale_by_market["KOSDAQ"] == pytest.approx(0.75)


def test_allocate_budget_uses_own_market_scale(trader):
    """사이징은 그 종목이 속한 시장의 배수를 쓴다 (코스닥 톱니장 → 코스피 종목 영향 없음)."""
    from modules.auto_trade.engine import RiskManager

    trader.risk_scale = 0.5                                   # 계좌 단위(열위 = 코스닥)
    trader.risk_scale_by_market = {"KOSPI": 1.0, "KOSDAQ": 0.5}
    trader.risk_scale_reason_by_market = {"KOSPI": "", "KOSDAQ": "휩소율 62% x0.50"}
    rm = RiskManager(trader)

    assert rm.current_risk_scale("KOSPI") == pytest.approx(1.0)
    assert rm.current_risk_scale("KOSDAQ") == pytest.approx(0.5)
    assert rm.current_risk_scale() == pytest.approx(0.5)       # 생략 시 계좌 단위(보수적)

    # 리스크층이 구속되도록 손절폭을 넓게 잡아 시장별 차이를 드러낸다
    kw = dict(stop_loss_rate=-30.0, atr=None, current_price=None)
    kospi = rm.allocate_budget(10_000_000, 1.0, market_type="KOSPI", **kw)
    kosdaq = rm.allocate_budget(10_000_000, 1.0, market_type="KOSDAQ", **kw)
    assert kospi > kosdaq
    assert kosdaq == pytest.approx(kospi * 0.5, rel=0.01)


def test_risk_scale_actually_shrinks_allocation(trader):
    """[2026-07-27] 배수가 기초 비중에 적용되어 배분액이 실제로 줄어야 한다.

    종전에는 리스크층에만 곱했는데 그 층이 구속하지 않아, 배수가 0.45 미만이 되기 전까지
    배분액이 1원도 변하지 않았다(사실상 무력). 변동성 타겟팅이 구속하는 상황에서도
    배수에 비례해 줄어드는지 검증한다."""
    from modules.auto_trade.engine import RiskManager

    trader.risk_scale_by_market = {"KOSPI": 1.0, "KOSDAQ": 1.0}
    trader.risk_scale_reason_by_market = {}
    rm = RiskManager(trader)

    # 변동성 캡이 확실히 구속하도록 고변동성 종목을 준다 (ATR/price 6.4% → 연 100%)
    kw = dict(stop_loss_rate=-13.0, atr=6_400.0, current_price=100_000.0)
    full = rm.allocate_budget(10_000_000, 0.25, market_type="KOSPI", **kw)

    trader.risk_scale_by_market = {"KOSPI": 0.6, "KOSDAQ": 0.6}
    trader.risk_scale = 0.6
    reduced = rm.allocate_budget(10_000_000, 0.25, market_type="KOSPI", **kw)

    assert reduced < full, "배수를 낮췄는데 배분액이 그대로면 방어가 작동하지 않는 것"
    assert reduced == pytest.approx(full * 0.6, rel=0.02)
