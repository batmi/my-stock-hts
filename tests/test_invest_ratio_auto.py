"""종목당 투자 비중 '자동(0)' 모드 및 명목합 경고 검증.

- SYSTEM_INVEST_PER_STOCK = 0 → 1 / SYSTEM_MAX_HOLDINGS 로 자동 산출
- 개별 종목 룰의 invest_ratio 도 0/None 이면 전역(또는 자동)을 따른다
  (종전에는 룰 저장 시점 값이 박제돼 슬롯 수를 바꿔도 그 종목만 옛 비중으로 남았다)
- 명목합 100% 초과는 '경고'일 뿐 차단하지 않는다(의도적 오버커밋 허용)
"""
import pytest
import config


@pytest.fixture
def restore_settings():
    old_ratio = config.settings.SYSTEM_INVEST_PER_STOCK
    old_holdings = config.settings.SYSTEM_MAX_HOLDINGS
    yield
    config.settings.SYSTEM_INVEST_PER_STOCK = old_ratio
    config.settings.SYSTEM_MAX_HOLDINGS = old_holdings


# ---------------------------------------------------------
# 자동 모드
# ---------------------------------------------------------
@pytest.mark.parametrize("holdings,expected", [(2, 0.5), (3, 1 / 3), (4, 0.25), (5, 0.2), (6, 1 / 6)])
def test_auto_ratio_follows_slot_count(restore_settings, holdings, expected):
    """비중 0이면 슬롯 수만 바꿔도 명목합이 항상 100%로 유지된다."""
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.0
    config.settings.SYSTEM_MAX_HOLDINGS = holdings

    assert config.resolve_invest_ratio() == pytest.approx(expected)
    assert config.is_invest_ratio_auto() is True
    # 명목합 = 비중 × 슬롯수 = 100% → 경고 없음
    assert config.nominal_exposure_warning() is None


def test_explicit_ratio_overrides_auto(restore_settings):
    """0보다 큰 값을 넣으면 그 값이 그대로 쓰이고 자동 계산은 무시된다."""
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.4
    config.settings.SYSTEM_MAX_HOLDINGS = 4

    assert config.resolve_invest_ratio() == pytest.approx(0.4)
    assert config.is_invest_ratio_auto() is False


def test_format_marks_auto_mode(restore_settings):
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.0
    config.settings.SYSTEM_MAX_HOLDINGS = 4
    assert config.format_invest_ratio() == "25% (자동)"

    config.settings.SYSTEM_INVEST_PER_STOCK = 0.3
    assert config.format_invest_ratio() == "30%"


# ---------------------------------------------------------
# 개별 종목 룰 오버라이드
# ---------------------------------------------------------
@pytest.mark.parametrize("rule_ratio", [None, 0, 0.0, "", "0"])
def test_rule_zero_falls_back_to_global(restore_settings, rule_ratio):
    """개별 룰이 0/None/빈값이면 전역(자동)을 따라간다 — 슬롯 수 변경을 그대로 반영."""
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.0
    config.settings.SYSTEM_MAX_HOLDINGS = 5

    assert config.resolve_invest_ratio(rule_ratio) == pytest.approx(0.2)
    assert config.is_invest_ratio_auto(rule_ratio) is True


def test_rule_ratio_wins_over_global(restore_settings):
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.2
    config.settings.SYSTEM_MAX_HOLDINGS = 5

    assert config.resolve_invest_ratio(0.45) == pytest.approx(0.45)
    assert config.is_invest_ratio_auto(0.45) is False


def test_rule_ratio_garbage_is_ignored(restore_settings):
    """DB에서 온 값이 파싱 불가여도 예외 없이 전역으로 폴백한다."""
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.0
    config.settings.SYSTEM_MAX_HOLDINGS = 4

    assert config.resolve_invest_ratio("abc") == pytest.approx(0.25)


def test_slot_change_no_longer_strands_saved_rules(restore_settings):
    """[회귀] 4슬롯에서 자동 저장된 룰(0)은 6슬롯으로 바꾸면 함께 줄어든다."""
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.0
    config.settings.SYSTEM_MAX_HOLDINGS = 4
    saved_rule_ratio = 0.0  # 메뉴 기본값(자동)으로 저장된 개별 룰

    assert config.resolve_invest_ratio(saved_rule_ratio) == pytest.approx(0.25)

    config.settings.SYSTEM_MAX_HOLDINGS = 6
    assert config.resolve_invest_ratio(saved_rule_ratio) == pytest.approx(1 / 6)
    # 명목합도 100% 유지 (종전에는 0.25가 박제돼 108.5%가 됐다)
    assert config.nominal_exposure_warning(override_ratios=[saved_rule_ratio]) is None


# ---------------------------------------------------------
# 명목합 경고 (차단 아님)
# ---------------------------------------------------------
def test_overcommit_warns_but_allows(restore_settings):
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.4
    config.settings.SYSTEM_MAX_HOLDINGS = 4

    warn = config.nominal_exposure_warning()
    assert warn is not None
    assert "160%" in warn
    # 오버커밋이 허용된다는 사실이 문구에 명시돼야 한다
    assert "오버커밋 자체는 허용됩니다" in warn
    # 설정값 자체는 변경되지 않는다(경고일 뿐 차단/보정 아님)
    assert config.settings.SYSTEM_INVEST_PER_STOCK == pytest.approx(0.4)
    assert config.resolve_invest_ratio() == pytest.approx(0.4)


def test_undercommit_is_not_warned(restore_settings):
    """명목합 80%(현금 버퍼 의도)는 경고 대상이 아니다."""
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.2
    config.settings.SYSTEM_MAX_HOLDINGS = 4
    assert config.nominal_exposure_warning() is None


def test_individual_rules_counted_in_nominal_sum(restore_settings):
    """개별 룰 비중 + 잔여 슬롯 × 전역 비중으로 명목합을 계산한다."""
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.0   # 자동 25%
    config.settings.SYSTEM_MAX_HOLDINGS = 4

    # 개별 룰 2종목이 각 50% → 100% + 잔여 2슬롯 × 25% = 150%
    warn = config.nominal_exposure_warning(override_ratios=[0.5, 0.5])
    assert warn is not None
    assert "150%" in warn

    # 개별 룰 0(자동)은 오버라이드로 치지 않는다
    assert config.nominal_exposure_warning(override_ratios=[0.0, None]) is None


def test_zero_holdings_does_not_crash(restore_settings):
    assert config.nominal_exposure_warning(0.25, 0) is None


# ---------------------------------------------------------
# 사이징 연결 (allocate_budget 이 자동 비중을 실제로 쓴다)
# ---------------------------------------------------------
def test_allocate_budget_uses_auto_ratio(restore_settings):
    from modules.auto_trade import RiskManager

    class _T:
        initial_asset = 10_000_000
        def log(self, msg): pass

    config.settings.SYSTEM_INVEST_PER_STOCK = 0.0
    config.settings.SYSTEM_MAX_HOLDINGS = 5
    config.USE_VOLATILITY_TARGETING = False

    rm = RiskManager(_T())
    amt = rm.allocate_budget(10_000_000, config.resolve_invest_ratio(), stop_loss_rate=None)
    assert amt == 2_000_000  # 자산의 1/5
