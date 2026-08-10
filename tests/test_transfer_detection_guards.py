"""외부 입출금 자동 감지가 기준 자산을 함부로 옮기지 않는가.

[왜 이 테스트인가] 감지되면 initial_asset을 직접 이동시키는데, 그 값은 계좌 차단기의
분모이자 포지션 사이징의 기준이다. 오탐 한 번이 두 장치를 동시에 틀어 놓는다.

이 파일의 기존 주석이 이미 우려를 적어 뒀다 — "캐시면 오차가 매 주기 동일하게 반복되어
3회 연속 확인 규칙이 방어가 아니라 오탐 확정 장치가 된다." 그 우려의 구체적 경로가
실제로 있었다: 당일 실현손익 조회 실패가 `except: pass`로 삼켜져 0이 되고, 그 차액이
그대로 '입출금'으로 둔갑한다. 같은 조회가 매 주기 똑같이 실패하므로 3회 연속도 충족된다.
"""
import pytest

from modules.auto_trade import trader as T


def _limit(initial_asset):
    return max(int(initial_asset * T.AUTO_TRANSFER_MAX_RATIO), T.AUTO_TRANSFER_MIN_LIMIT)


def test_constants_are_sane():
    assert 0 < T.AUTO_TRANSFER_MAX_RATIO < 1
    assert T.AUTO_TRANSFER_MIN_LIMIT > 0


def test_ordinary_deposit_is_within_the_bound():
    """평범한 입금(시드의 몇 %)까지 막으면 기준이 낡은 채로 남아 손익률이 틀어진다."""
    assert 500_000 <= _limit(10_000_000)


def test_account_sized_jump_is_outside_the_bound():
    """계좌 규모에 맞먹는 '입금'은 잔고 조회 이상을 의심해야 한다."""
    assert 9_000_000 > _limit(10_000_000)


def test_small_account_keeps_an_absolute_floor():
    """소액 계좌에서 비율만 쓰면 상한이 지나치게 좁아져 정상 입금도 매번 보류된다."""
    assert _limit(100_000) == T.AUTO_TRANSFER_MIN_LIMIT


@pytest.mark.parametrize("equity,amt,ok", [
    (10_000_000, 1_500_000, True),    # 시드 15% — 자동 반영
    (10_000_000, 3_000_000, True),    # 경계(30%)
    (10_000_000, 3_000_001, False),   # 경계 초과
    (10_000_000, -8_000_000, False),  # 출금 방향도 같은 상한
    (5_000_000, 900_000, True),       # 절대 하한 안쪽
])
def test_bound_applies_in_both_directions(equity, amt, ok):
    assert (abs(amt) <= _limit(equity)) is ok


@pytest.mark.parametrize("realized,expect_action", [
    (-500_000, "출금"),   # 손실을 놓치면 원금이 작아 보인다 → 가짜 '출금'
    (500_000, "입금"),    # 이익을 놓치면 원금이 커 보인다 → 가짜 '입금'
])
def test_realized_profit_failure_shape_is_a_false_transfer(realized, expect_action):
    """실현손익을 못 구해 0으로 두면 그 금액이 그대로 가짜 입출금이 된다.

    원금 = 현금 + 매입원가 - 실현손익 이다. 실현손익이 0으로 바뀌면 원금이 정확히
    그 금액만큼 어긋나고, 감지 문턱(50,000)을 훌쩍 넘는다.

    **손실을 놓치는 쪽이 더 위험하다.** 가짜 '출금'으로 잡혀 기준 자산이 줄면 손실률의
    분모가 작아져 차단기가 더 늦게 걸린다 — 손실이 난 날에 보호가 약해지는 방향이다.
    """
    cash, pchs = 3_000_000, 7_000_000
    principal_ok = cash + pchs - realized
    principal_broken = cash + pchs - 0
    drift = principal_broken - principal_ok
    assert abs(drift) == abs(realized)
    assert abs(drift) >= 50_000, "문턱 아래면 애초에 문제가 아니다"
    assert ("입금" if drift > 0 else "출금") == expect_action


def test_detection_is_gated_on_realized_ok():
    """감지 진입 조건에 realized_ok 가 포함돼 있어야 한다(회귀 방지)."""
    import inspect
    src = inspect.getsource(T.AutoTrader._monitor_account_status)
    assert "realized_ok" in src
    assert "not is_first_init and toss_cash_reliable and realized_ok" in src


def test_bound_is_actually_applied_in_source():
    """상한 검사가 실제 코드 경로에 있어야 한다 — 상수만 있고 안 쓰면 무의미하다."""
    import inspect
    src = inspect.getsource(T.AutoTrader._monitor_account_status)
    assert "AUTO_TRANSFER_MAX_RATIO" in src and "AUTO_TRANSFER_MIN_LIMIT" in src
    assert "반영하지 않습니다" in src
