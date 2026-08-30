"""계좌 잔고가 수시로 변할 때 리스크 계산이 흔들리지 않는가.

[왜 이 축인가] `initial_asset`(당일 시작 자산) 하나에 시스템의 안전장치가 전부 매달려 있다.
  · 사이징 기초 비중        engine.allocate_budget  (initial_asset x 비중)
  · 종목당 손실 상한        engine.allocate_budget  (initial_asset x SYSTEM_RISK_PER_TRADE)
  · 일일 손실 차단기의 분모  engine.check_loss_limit
  · 히트 캡 기준자산 폴백    engine.portfolio_risk_budget_left
  · 드로다운 HWM 후보       trader._get_account_drawdown_pct
입출금은 손익이 아닌데 이 값을 움직인다. 그래서 시스템은 '원금'(현금+매입원가-실현손익)
이라는 불변량으로 입출금만 가려내 기준을 옮긴다. 이 파일은 그 경로의 구멍을 막는다.

[막는 구멍 두 가지]
  ① 프로그램이 꺼진 사이의 입출금 — 기준 원금이 메모리에만 있어, 같은 날 재기동하면
     입출금 **이후** 상태로 새로 잡혀 차이가 0이 된다. 그 입출금은 영영 감지되지 않고,
     시작 자산만 옛 값으로 남는다(출금이면 높게 → 차단기 헛발동 + 사이징 기준 부풀음).
  ② 출금이 부른 헛 방어 모드 — 확정에 3주기가 걸리는데 그 사이 손실률이 -10%를 넘으면
     방어 모드가 걸리고, 기준을 고쳐도 **날짜가 바뀔 때까지** 안 풀렸다.
     정상적인 출금 한 번이 그날 신규 진입을 통째로 멈춘다.
"""
from datetime import datetime, date
from unittest.mock import MagicMock, patch

import pytest

import config
from modules.auto_trade import common as at_common
from modules.auto_trade import AutoTrader

ACC = "44048158-01"


@pytest.fixture
def state(tmp_path, monkeypatch):
    """일일 상태 파일을 테스트용으로 격리한다."""
    monkeypatch.setattr(at_common, "DAILY_STATE_FILE", str(tmp_path / "daily.json"))
    return at_common


# ───────────────────── ① 기준 원금이 재기동을 견디는가 ─────────────────────

def test_the_principal_survives_a_restart(state):
    """[핵심] 이 값이 살아남아야 오프라인 입출금을 가려낼 수 있다."""
    state.save_daily_initial_asset(ACC, 10_000_000, principal=9_500_000)
    assert state.load_daily_initial_asset(ACC) == 10_000_000
    assert state.load_daily_principal(ACC) == 9_500_000


def test_saving_only_the_asset_keeps_the_principal(state):
    """시작 자산만 고치는 호출이 기준 원금을 지우면 감지가 다시 꺼진다."""
    state.save_daily_initial_asset(ACC, 10_000_000, principal=9_500_000)
    state.save_daily_initial_asset(ACC, 12_000_000)          # principal 미지정
    assert state.load_daily_principal(ACC) == 9_500_000
    assert state.load_daily_initial_asset(ACC) == 12_000_000


def test_the_old_plain_number_format_still_loads(state):
    """[하위 호환] 형식이 바뀌었다고 그날 기준선을 잃으면 차단기가 통째로 꺼진다."""
    from core import jsonio
    jsonio.save_json(state.DAILY_STATE_FILE,
                     {"date": datetime.now().strftime("%Y-%m-%d"),
                      "accounts": {ACC: 10_000_000}})
    assert state.load_daily_initial_asset(ACC) == 10_000_000
    assert state.load_daily_principal(ACC) == 0      # 없으면 0 — 새로 잡는다


def test_yesterdays_state_is_not_reused(state):
    from core import jsonio
    jsonio.save_json(state.DAILY_STATE_FILE,
                     {"date": "2020-01-01",
                      "accounts": {ACC: {"asset": 10_000_000, "principal": 9_000_000}}})
    assert state.load_daily_initial_asset(ACC) == 0
    assert state.load_daily_principal(ACC) == 0


def test_other_accounts_are_preserved(state):
    state.save_daily_initial_asset("A-1", 1_000_000, principal=900_000)
    state.save_daily_initial_asset("B-1", 2_000_000, principal=1_900_000)
    assert state.load_daily_principal("A-1") == 900_000
    assert state.load_daily_initial_asset("B-1") == 2_000_000


def test_an_unknown_account_reports_zero(state):
    state.save_daily_initial_asset(ACC, 10_000_000, principal=9_500_000)
    assert state.load_daily_initial_asset("NOBODY-1") == 0
    assert state.load_daily_principal("NOBODY-1") == 0


# ───────────────────── ② 출금이 부른 헛 방어 모드 ─────────────────────

@pytest.fixture
def trader():
    t = AutoTrader()
    t.buy_halted = False
    t.buy_halt_reason = ""
    t.buy_halt_date = None
    t.buy_halt_kind = None
    t.initial_asset = 10_000_000
    return t


def test_a_daily_loss_halt_is_tagged(trader):
    """사유 문구를 되읽지 않고 종류로 판단할 수 있어야 한다."""
    with patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        assert trader.halt_buys("일일 손실 한도 초과", kind='daily_loss') is True
    assert trader.buy_halt_kind == 'daily_loss'


def test_resuming_clears_the_tag(trader):
    with patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        trader.halt_buys("일일 손실 한도 초과", kind='daily_loss')
        trader.resume_buys("테스트")
    assert trader.buy_halted is False and trader.buy_halt_kind is None


def test_a_withdrawal_trips_the_circuit_breaker_before_the_baseline_is_fixed(trader):
    """[전제 확인] 출금 직후 3주기 동안은 손실로 보인다 — 이게 헛 방어 모드의 출발점이다.

    이 동작 자체는 옳다(기준이 아직 옛 값이므로). 문제는 그 뒤에 안 풀리는 것이다.
    """
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch.object(trader, 'log'), patch.object(trader, 'get_error_log_tail', create=True):
        trader.risk_manager.check_loss_limit(7_000_000)      # 300만원 출금
    assert trader.buy_halted is True
    assert trader.buy_halt_kind == 'daily_loss'


def test_the_halt_is_released_once_the_withdrawal_is_applied(trader):
    """[핵심] 300만원 출금 → 헛 방어 모드 → 기준 정정 → 풀린다.

    종전에는 날짜가 바뀔 때까지 풀리지 않아, 정상적인 출금 한 번이 그날 신규 진입을
    통째로 멈췄다.
    """
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch.object(trader, 'log'):
        trader.risk_manager.check_loss_limit(7_000_000)      # 기준 1,000만 → -30%
        assert trader.buy_halted is True

        trader.initial_asset = 7_000_000                     # 입출금 감지가 기준을 옮겼다
        released = trader._reevaluate_buy_halt_after_transfer(7_000_000, "출금")

    assert released is True
    assert trader.buy_halted is False and trader.buy_halt_kind is None
    assert any("방어 모드 해제" in c.args[0] for c in tg.call_args_list), "해제를 알리지 않았다"


def test_a_real_loss_keeps_the_halt_after_the_baseline_moves(trader):
    """[대조군] 정정 뒤에도 한도를 넘으면 방어 모드는 유지된다 — 풀면 안 된다."""
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch.object(trader, 'log'):
        trader.risk_manager.check_loss_limit(6_000_000)
        assert trader.buy_halted is True
        trader.initial_asset = 7_000_000                     # 출금 반영해도 -14.3%
        released = trader._reevaluate_buy_halt_after_transfer(6_000_000, "출금")
    assert released is False and trader.buy_halted is True


def test_only_the_daily_loss_halt_is_reconsidered(trader):
    """수동·장애로 걸린 방어 모드까지 입출금이 풀어 버리면 안 된다."""
    with patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        trader.halt_buys("운용자 수동 중단", kind=None)
        released = trader._reevaluate_buy_halt_after_transfer(20_000_000, "입금")
    assert released is False and trader.buy_halted is True


def test_a_released_halt_can_fire_again_the_same_day(trader):
    """풀고 나서 진짜로 한도를 넘으면 다시 걸려야 한다(당일 재발동 차단에 막히면 안 된다)."""
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch.object(trader, 'log'):
        trader.risk_manager.check_loss_limit(7_000_000)
        trader.initial_asset = 7_000_000
        trader._reevaluate_buy_halt_after_transfer(7_000_000, "출금")
        assert trader.buy_halted is False
        trader.risk_manager.check_loss_limit(6_000_000)      # 새 기준 대비 -14.3%
    assert trader.buy_halted is True, "해제 뒤 진짜 손실이 났는데 차단기가 안 걸렸다"


def test_an_unmeasurable_asset_does_not_release_the_halt(trader):
    """'모름'을 '괜찮음'으로 읽어 방어를 풀면 안 된다."""
    with patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        trader.halt_buys("일일 손실 한도 초과", kind='daily_loss')
        assert trader._reevaluate_buy_halt_after_transfer(0, "출금") is False
        trader.initial_asset = 0
        assert trader._reevaluate_buy_halt_after_transfer(7_000_000, "출금") is False
    assert trader.buy_halted is True


# ─────────────── ③ 기준선은 옮기지 않아도 자동으로 보정된다 ───────────────
#
# [설계] 종전에는 입출금이 감지되면 initial_asset 자체를 옮겼다. 그러려면 3주기 확인·자동
# 조정 상한·파일 저장이 필요했고, 상한을 넘으면 **사람이 손대기 전까지 기준이 틀린 채로
# 하루가 갔다**(출금이면 일일 손실 한도가 종일 헛발동한다).
# 순입출금은 원금 불변량의 변화라 매 주기 정확히 다시 잴 수 있다. 저장하지 않고 판정할
# 때마다 재면 상한도 대기도 필요 없고, 스냅샷이 한 번 튀어도 다음 주기에 저절로 낫는다.

def test_the_baseline_follows_a_withdrawal_without_being_moved(trader):
    """[핵심] 기준 자산을 옮기지 않아도 유효 기준선이 출금을 따라간다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -3_000_000
    assert trader.effective_baseline() == 7_000_000


def test_nothing_is_mutated_to_get_the_right_baseline(trader):
    """[되돌릴 수 있음] 기준선은 파생값이라 시작 자산을 옮기지 않는다.

    옮기는 쪽은 되돌릴 수 없고, 입출금 추정이 틀리면 잘못된 기준이 그대로 굳는다.
    파생값은 다음 주기에 다시 재므로 저절로 낫는다.
    """
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -3_000_000
    assert trader.effective_baseline() == 7_000_000
    assert trader.initial_asset == 10_000_000, "시작 자산이 움직였다"

    trader.net_transfer_today = -1_000_000      # 다음 주기에 다시 쟀다
    assert trader.effective_baseline() == 9_000_000, "옛 값이 굳었다"


def test_a_withdrawal_no_longer_trips_the_daily_loss_limit(trader):
    """[핵심] 1,000만 계좌에서 300만 출금은 -30%가 아니라 0%다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -3_000_000
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        trader.risk_manager.check_loss_limit(7_000_000)
    assert trader.buy_halted is False, "정상 출금이 방어 모드를 걸었다"


def test_a_real_loss_alongside_a_withdrawal_still_halts(trader):
    """[대조군] 출금을 뺀 뒤에도 진짜 손실이면 걸려야 한다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -3_000_000        # 기준선 700만
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        trader.risk_manager.check_loss_limit(6_200_000)   # 700만 대비 -11.4%
    assert trader.buy_halted is True


def test_a_deposit_does_not_hide_a_real_loss(trader):
    """[핵심·반대 방향] 입금으로 자산이 늘어도 손실은 손실이다 — 분모가 커져야 한다.

    보정하지 않으면 입금이 손실을 덮어 차단기가 영영 안 걸린다.
    """
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = +5_000_000        # 기준선 1,500만
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        trader.risk_manager.check_loss_limit(13_000_000)  # 1,500만 대비 -13.3%
    assert trader.buy_halted is True, "입금이 실제 손실을 가렸다"


def test_the_halt_heals_itself_on_the_next_cycle(trader):
    """[자동화] 출금 직후 한 주기 헛발동해도, 순입출금이 잡히면 스스로 풀린다.

    사람이 메뉴를 열 필요가 없다 — 이것이 '자동으로 처리'의 실체다.
    """
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = 0                  # 아직 못 쟀다
    with patch.object(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), \
         patch('modules.auto_trade.api.send_telegram_message'), patch.object(trader, 'log'):
        trader.risk_manager.check_loss_limit(7_000_000)
        assert trader.buy_halted is True            # 1주기: 헛발동

        trader.net_transfer_today = -3_000_000      # 2주기: 출금이 잡혔다
        trader.risk_manager.check_loss_limit(7_000_000)
    assert trader.buy_halted is False, "다음 주기에 스스로 풀리지 않았다"


def test_sizing_uses_the_same_baseline_as_the_circuit_breaker(trader):
    """두 장치가 다른 자본을 보면 종목당 한도는 걸리는데 합산 한도는 안 걸린다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -5_000_000
    trader.risk_scale = 1.0
    trader.risk_scale_by_market = {}
    with patch.object(config, 'USE_VOLATILITY_TARGETING', False), \
         patch.object(config, 'SYSTEM_RISK_PER_TRADE', 0):
        amt = trader.risk_manager.allocate_budget(99_000_000, 0.25)
    assert amt == int(5_000_000 * 0.25), f"사이징이 옛 기준(1,000만)을 봤다: {amt:,}"


def test_an_unmeasurable_transfer_falls_back_to_the_start_asset(trader):
    """못 쟀으면(조회 실패) 보정하지 않는다 — 추측한 값으로 기준을 흔들지 않는다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = 0
    assert trader.effective_baseline() == 10_000_000
    trader.initial_asset = 0
    assert trader.effective_baseline() == 0.0
