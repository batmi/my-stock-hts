"""오늘의 '시작 자산'(계좌 차단기 분모 · 사이징 기준)이 그럴듯한 값인가.

[왜 이 테스트인가] 이 값의 저장 조건은 `tot_asset > 0` 뿐이었다. 어떤 양수든 그날의
기준이 되고, 한 번 저장되면 load 가 그대로 돌려주므로 하루 종일 고정된다. 기준선이
실제보다 작게 박히면 손실률이 늘 큰 양수로 계산돼 **차단기가 종일 발동하지 않는다** —
아무도 모르는 채로 보호 장치만 사라지는, 가장 나쁜 상태다.

코드는 이 실패 모드를 이미 알고 있었다. engine.check_loss_limit 의 주석이 "증권사 API
통신 오류로 주식 평가액이 0으로 수신되어 예수금만 계산될 때"라고 적어 두고 current_total 을
거른다. 정작 더 위험한 쪽(하루 고정되는 기준선)에는 같은 가드가 없었다.
"""
import pytest

from modules.auto_trade.common import BASELINE_SANITY_RATIO, is_plausible_baseline

KEY = "12345678-01"


def test_normal_day_passes():
    assert is_plausible_baseline(KEY, 10_000_000, last_known=10_200_000)


def test_ordinary_drawdown_still_passes():
    """평범한 손실까지 막으면 정상 운용이 기준선 없이 돌아간다."""
    assert is_plausible_baseline(KEY, 8_500_000, last_known=10_000_000)


def test_quote_outage_shape_is_rejected():
    """주식 평가액이 0으로 와서 예수금만 잡힌 형태 — 이게 실제 실패 모드다."""
    assert not is_plausible_baseline(KEY, 1_500_000, last_known=10_000_000)


def test_boundary_is_inclusive():
    last = 10_000_000
    assert is_plausible_baseline(KEY, int(last * BASELINE_SANITY_RATIO), last_known=last)
    assert not is_plausible_baseline(KEY, int(last * BASELINE_SANITY_RATIO) - 1, last_known=last)


def test_zero_and_negative_never_pass():
    for v in (0, -1, -10_000_000):
        assert not is_plausible_baseline(KEY, v, last_known=10_000_000)


@pytest.mark.parametrize("last", [None, 0])
def test_no_history_passes(last):
    """첫 운용이면 대조할 근거가 없다 — 막으면 아무도 기준선을 못 세운다."""
    assert is_plausible_baseline(KEY, 10_000_000, last_known=last)


def test_growth_is_never_suspicious():
    """입금·수익으로 늘어난 것은 의심 대상이 아니다."""
    assert is_plausible_baseline(KEY, 50_000_000, last_known=10_000_000)


def test_small_but_consistent_account_passes():
    """실제로 작은 계좌(모의·테스트)는 정상이다 — 절대 금액으로 자르지 않는다.

    운영 DB에 27원짜리 계좌가 실재한다(10,027 → 27). 절대 하한을 두면 그런 계좌가
    통째로 막히므로, 판단은 '직전 대비'로만 한다.
    """
    assert is_plausible_baseline(KEY, 27, last_known=27)
    assert not is_plausible_baseline(KEY, 27, last_known=10_027)
