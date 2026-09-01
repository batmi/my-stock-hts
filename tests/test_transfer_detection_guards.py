"""입출금 오탐이 리스크 판정을 망가뜨리지 못하는가.

[이 파일의 역사] 종전에는 감지되면 initial_asset(차단기의 분모이자 사이징의 기준)을 직접
옮겼다. 오탐 한 번이 두 장치를 동시에 틀어 놓으므로 자동 조정에 상한(기준자산 30%·최소
100만원)을 두고 초과분은 사람에게 넘겼다. 이 파일은 그 상한을 고정하고 있었다.

[무엇이 바뀌었나] 이제 아무것도 옮기지 않는다.
  · 일일 손실 한도·사이징 → net_transfer_today(매 주기 다시 재는 파생값)를 빼고 계산
  · 드로다운 HWM        → daily_asset_history.net_transfer 로 **읽을 때** 환산
원본을 고치지 않으므로 오탐이 굳지 않고, 다음 주기에 저절로 낫는다. 그래서 상한도
사람도 필요 없어졌다 — 상한이 오히려 문제였다(미반영 출금이 90일짜리 가짜 드로다운으로
남았다: 2026-08-23 가상계좌 사고).

[그래서 지금 지켜야 할 것] 오탐 자체는 여전히 일어날 수 있다. 그 오탐이
  ① 되돌릴 수 없는 상태를 만들지 않을 것
  ② 다음 주기에 스스로 교정될 것
  ③ 애초에 '모르는 상태'에서는 감지하지 않을 것
"""
import inspect

import pytest

from modules.auto_trade import trader as T


# ───────────────── ① 되돌릴 수 없는 상태를 만들지 않는다 ─────────────────

def test_detection_never_moves_the_start_asset():
    """[핵심] 감지가 initial_asset·baseline_principal 을 옮기면 오탐이 굳는다."""
    src = inspect.getsource(T.AutoTrader._monitor_account_status)
    assert "self.initial_asset +=" not in src, "감지가 시작 자산을 옮긴다"
    assert "self.baseline_principal +=" not in src, "감지가 기준 원금을 옮긴다"


def test_detection_never_shifts_the_asset_history():
    """이력 평행이동은 되돌릴 수 없고, 오탐이면 고점이 낮아져 한도가 열린다."""
    src = inspect.getsource(T.AutoTrader._monitor_account_status)
    assert "shift_daily_assets" not in src


def test_the_retired_bound_is_gone():
    """[회귀 방지] 상한이 되살아나면 미반영 출금이 다시 90일짜리 가짜 드로다운을 남긴다."""
    assert not hasattr(T, "AUTO_TRANSFER_MAX_RATIO")
    assert not hasattr(T, "AUTO_TRANSFER_MIN_LIMIT")
    src = inspect.getsource(T.AutoTrader._monitor_account_status)
    assert "반영하지 않습니다" not in src


# ───────────────── ② 다음 주기에 스스로 교정된다 ─────────────────

@pytest.mark.parametrize("wrong,right", [(-500_000, 0), (900_000, -200_000), (0, 3_000_000)])
def test_a_wrong_estimate_is_replaced_not_accumulated(wrong, right):
    """파생값은 매 주기 **덮어쓴다** — 틀린 값이 누적되거나 남지 않는다."""
    t = T.AutoTrader()
    t.initial_asset = 10_000_000
    t.net_transfer_today = wrong
    assert t.effective_baseline() == 10_000_000 + wrong
    t.net_transfer_today = right
    assert t.effective_baseline() == 10_000_000 + right


def test_the_baseline_never_goes_negative():
    """터무니없는 오탐이 와도 분모가 음수가 되면 안 된다(손실률 부호가 뒤집힌다)."""
    t = T.AutoTrader()
    t.initial_asset = 10_000_000
    t.net_transfer_today = -99_000_000
    assert t.effective_baseline() == 0.0


# ───────────────── ③ 모르면 감지하지 않는다 ─────────────────

@pytest.mark.parametrize("realized,expect_action", [
    (-500_000, "출금"),   # 손실을 놓치면 원금이 작아 보인다 → 가짜 '출금'
    (500_000, "입금"),    # 이익을 놓치면 원금이 커 보인다 → 가짜 '입금'
])
def test_realized_profit_failure_shape_is_a_false_transfer(realized, expect_action):
    """실현손익을 못 구해 0으로 두면 그 금액이 그대로 가짜 입출금이 된다.

    원금 = 현금 + 매입원가 - 실현손익 이다. 실현손익이 0으로 바뀌면 원금이 정확히
    그 금액만큼 어긋나고, 감지 문턱(50,000)을 훌쩍 넘는다.

    **손실을 놓치는 쪽이 더 위험하다.** 가짜 '출금'으로 잡히면 기준선이 낮아져 손실률의
    분모가 작아지고, 차단기가 더 늦게 걸린다 — 손실이 난 날에 보호가 약해지는 방향이다.
    그래서 못 구했으면 아예 재지 않는다(아래 두 테스트).
    """
    cash, pchs = 3_000_000, 7_000_000
    drift = (cash + pchs - 0) - (cash + pchs - realized)
    assert abs(drift) == abs(realized)
    assert abs(drift) >= 50_000, "문턱 아래면 애초에 문제가 아니다"
    assert ("입금" if drift > 0 else "출금") == expect_action


def test_detection_is_gated_on_realized_ok():
    """감지 진입 조건에 realized_ok 가 포함돼 있어야 한다(회귀 방지)."""
    src = inspect.getsource(T.AutoTrader._monitor_account_status)
    assert "not is_first_init and toss_cash_reliable and realized_ok" in src


def test_the_derived_correction_is_gated_on_the_same_conditions():
    """[핵심] 파생 보정도 같은 게이트를 지나야 한다.

    감지(알림)만 막고 보정은 그대로 두면, 못 잰 실현손익이 그대로 기준선을 흔든다 —
    게이트를 통과 못 하면 보정하지 않는다(0으로 되돌린다).
    """
    src = inspect.getsource(T.AutoTrader._monitor_account_status)
    assert "if toss_cash_reliable and realized_ok:" in src
    assert "self.net_transfer_today = 0" in src, "못 잰 경우 보정을 끄는 경로가 없다"


def test_a_stale_correction_does_not_survive_a_failed_measurement():
    """조회가 실패한 주기에 이전 보정값이 살아남으면 틀린 기준이 유지된다."""
    t = T.AutoTrader()
    t.initial_asset = 10_000_000
    t.net_transfer_today = -3_000_000
    t.net_transfer_today = 0                      # 못 잰 주기가 하는 일
    assert t.effective_baseline() == 10_000_000


# ==========================================================
# 잡음 바닥 — 장중 기록 경로에도 오프라인과 같은 문턱이 있어야 한다 (2026-09-01)
#
# [실측 2026-08-31] 가상계좌에 입출금이 없는데 daily_asset_history.net_transfer 에 77원이
# 기록됐다. 원인은 current_principal = 현금 + 매입원가 − 실현손익 인데 **매수 수수료는
# 현금만 깎고 매입원가에는 안 들어가는** 것이다. 거래한 날마다 잔차가 남는다.
#
# 한 번은 무해하지만 매일 쌓이면 get_max_daily_asset 의 환산(고점)을 갉는다. 오프라인
# 경로는 같은 이유로 이미 OFFLINE_TRANSFER_FLOOR 를 갖고 있었다 — 같은 판정이면 같은
# 문턱이어야 한다.
# ==========================================================

def test_rounding_noise_is_not_a_transfer():
    """실측값 77원은 입출금이 아니다."""
    from modules.auto_trade.trader import OFFLINE_TRANSFER_FLOOR

    assert abs(77) < OFFLINE_TRANSFER_FLOOR, \
        "잡음 바닥이 실측 잔차(77원)를 못 거른다"


def test_the_floor_is_low_enough_for_a_small_account():
    """5만원(알림 문턱)을 쓰면 소액 계좌의 진짜 출금이 통째로 사라진다.

    운영 DB에 10,027원 계좌의 1만원 출금 사례가 있다 — 그 계좌엔 전 재산이다.
    이 값은 사이징·차단기의 보정에 쓰이므로 알림 문턱과 같으면 안 된다.
    """
    from modules.auto_trade.trader import OFFLINE_TRANSFER_FLOOR

    assert OFFLINE_TRANSFER_FLOOR <= 10_000


def test_the_recording_path_actually_applies_the_floor():
    """산식이 아니라 **그 자리**에 문턱이 걸렸는지 본다 — 종전엔 여기만 비어 있었다."""
    import inspect
    from modules.auto_trade import trader as tr

    src = inspect.getsource(tr)
    i = src.index("_net = int(current_principal - self.baseline_principal)")
    window = src[i:i + 1400]
    assert "abs(_net) < OFFLINE_TRANSFER_FLOOR" in window, \
        "장중 net_transfer 기록 경로에 잡음 바닥이 없다"
