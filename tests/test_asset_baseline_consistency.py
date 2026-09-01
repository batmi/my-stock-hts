"""자산 기준을 보는 장치들이 **같은 보정**을 보는가.

입출금은 손익이 아닌데 자산을 움직인다. 그래서 이 시스템은 기준을 옮기지 않고
'시작 자산 + 오늘 순입출금'으로 **잴 때마다 환산**한다(engine._equity_baseline).
문제는 그 환산을 보는 곳과 못 보는 곳이 섞이면, 한 장치는 맞고 다른 장치는 틀린 자본을
보게 된다는 것이다. 이 파일은 남아 있던 구멍들을 고정한다(2026-09-01 감사).

  ① 계좌 차단기의 '비정상 급감' 필터가 원본 시작 자산과 대고 있었다
     → 자산의 절반이 넘는 출금이 current_total_asset 을 출금 전 값에 하루 종일 얼렸다.
       그 값은 히트 캡의 분모이자 드로다운의 현재 자산이다.
  ② 드로다운 HWM 의 바닥값이 원본 시작 자산이었다
     → 정상적인 출금 한 번이 그날 내내 가짜 드로다운을 만들고 경보까지 울렸다.
  ③ 모니터 루프의 기준선 설치에 그럴듯함 검사가 없었다
     → 기동 경로가 거부한 값(시세 결손 의심)을 한 주기 뒤 그대로 박아, 안전장치가 무력.

  ④ 표시용 일일 손익만 원본 시작 자산으로 나누고 있었다
     → 판정은 맞는데 화면·텔레그램만 -30%를 띄운다. 없는 손실을 보고 개입하게 된다.

모두 '운용자가 알아채고 손대야 낫는' 상태를 만든다는 공통점이 있다.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from modules.auto_trade import AutoTrader


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.initial_asset = 0
    t.net_transfer_today = 0
    t.current_total_asset = 0
    yield t
    AutoTrader._instance = None


# ─────────── ① 차단기의 급감 필터는 보정된 기준선과 대야 한다 ───────────

def _breaker(trader, current_total):
    with patch.object(trader.risk_manager, 'check_loss_limit'):
        trader._run_account_circuit_breaker(current_total)
    return trader.current_total_asset


def test_a_large_withdrawal_does_not_freeze_the_equity(trader):
    """[핵심] 자산의 60%를 빼는 것은 정상 거래다 — 급감으로 읽어 자산을 얼리면 안 된다.

    얼면 히트 캡이 **없는 돈**을 분모로 쓰고(한도가 넓어진다), 드로다운은 현재 자산을
    실제보다 크게 봐서 과소평가된다 — 둘 다 한도가 조용히 열리는 방향이다.
    """
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -6_000_000      # 600만원 출금
    trader.current_total_asset = 10_000_000
    assert _breaker(trader, 4_000_000) == 4_000_000, "정상 출금을 급감으로 읽어 자산이 얼었다"


def test_a_real_crash_still_freezes_the_equity(trader):
    """[반대 방향] 입출금 없이 반토막이면 시세 결손(주식 평가액 0 수신) 의심이 맞다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = 0
    trader.current_total_asset = 10_000_000
    assert _breaker(trader, 4_000_000) == 10_000_000, "API 결손 의심 값이 기준자산에 박혔다"


def test_without_a_baseline_any_value_is_accepted(trader):
    """기준이 없으면 대조할 것도 없다 — 종전 동작을 그대로 유지한다."""
    trader.initial_asset = 0
    assert _breaker(trader, 4_000_000) == 4_000_000


# ─────────── ② 드로다운 고점의 바닥값도 보정을 봐야 한다 ───────────

def _drawdown(trader):
    trader._hwm_cache = 0.0
    trader._hwm_cache_date = datetime.now().strftime("%Y-%m-%d")   # DB 조회 생략
    return trader._get_account_drawdown_pct({})


def test_todays_withdrawal_is_not_a_drawdown(trader):
    """[핵심] 1,000만 계좌에서 300만을 빼도 드로다운은 0이어야 한다.

    종전에는 원본 시작 자산이 고점의 바닥값이라 30%가 찍혔다. DD_LEVEL_2(10%)를 넘으니
    리스크 한도가 줄고 '계좌 드로다운 30%' 경보까지 나간다 — 정상적인 출금 한 번에.
    """
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -3_000_000
    trader.current_total_asset = 7_000_000
    assert _drawdown(trader) == pytest.approx(0.0)


def test_a_real_loss_is_still_a_drawdown(trader):
    """[반대 방향] 입출금이 없으면 그대로 손실이다 — 보정이 손실을 지우면 안 된다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = 0
    trader.current_total_asset = 7_000_000
    assert _drawdown(trader) == pytest.approx(30.0)


def test_a_deposit_does_not_manufacture_a_drawdown(trader):
    """입금은 고점을 그만큼 올린다 — 넣자마자 '고점 대비 하락'이 되면 안 된다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = 5_000_000
    trader.current_total_asset = 15_000_000
    assert _drawdown(trader) == pytest.approx(0.0)


# ─────────── ③ 오프라인 보정은 하루 한 번만 잰다 ───────────

def test_offline_reconcile_runs_once_a_day(trader):
    """기준 자산이 끝내 안 잡히는 날에는 이 경로가 매 주기 다시 돈다 — 같은 입출금을
    주기마다 가산하면 자산 이력이 걷잡을 수 없이 망가진다."""
    db = MagicMock()
    db.get_last_principal_snapshot.return_value = ("2026-08-20", 10_000_000.0)
    db.get_realized_profit_between.return_value = (0, True)
    db.add_net_transfer.return_value = True
    with patch('modules.auto_trade.db_manager.db', db), \
         patch('modules.auto_trade.api.send_telegram_message'):
        first = trader._reconcile_offline_transfer("A-1", 9_000_000, True)
        second = trader._reconcile_offline_transfer("A-1", 9_000_000, True)
    assert (first, second) == (-1_000_000, 0)
    assert db.add_net_transfer.call_count == 1


def test_a_failed_measurement_is_retried(trader):
    """못 쟀으면 오늘 몫을 쓴 것이 아니다 — 다음 주기에 다시 해 봐야 한다."""
    db = MagicMock()
    db.get_last_principal_snapshot.return_value = ("2026-08-20", 10_000_000.0)
    db.get_realized_profit_between.side_effect = [(0, False), (0, True)]
    db.add_net_transfer.return_value = True
    with patch('modules.auto_trade.db_manager.db', db), \
         patch('modules.auto_trade.api.send_telegram_message'):
        assert trader._reconcile_offline_transfer("A-1", 9_000_000, True) == 0
        assert trader._reconcile_offline_transfer("A-1", 9_000_000, True) == -1_000_000


# ─────────── ④ 실패한 기준선이 루프에서 되살아나지 않는다 ───────────

def test_the_monitor_loop_applies_the_same_sanity_check():
    """[핵심] 기동 경로가 거부한 값을 모니터 루프가 한 주기 뒤 박으면 안전장치가 무력하다.

    기준선이 실제보다 작게 박히면 손실률이 늘 큰 양수로 계산돼 **차단기가 종일 발동하지
    않는다**. 아무도 모르는 채로 보호 장치만 사라지는 것이 가장 나쁜 상태다.
    (소스 수준으로 고정한다 — 이 블록은 잔고·예수금·시세를 모두 갖춘 주기 안에 있어
     행위 재현 비용이 크고, 회귀는 '검사 한 줄이 빠진다'는 형태로만 온다.)
    """
    import inspect
    src = inspect.getsource(AutoTrader._monitor_account_status)
    head = src[:src.index("[안전장치] 계좌 차단기")]
    assert "is_plausible_baseline(acc_str, current_total)" in head, \
        "모니터 루프가 그럴듯함 검사 없이 기준선을 설치한다"
    assert head.count("save_daily_initial_asset(acc_str") == 1


# ─────────── ⑤ 표시도 판정과 같은 기준선을 봐야 한다 ───────────

def test_display_pnl_excludes_transfers(trader):
    """출금을 손실로 띄우면 운용자가 없는 손실을 보고 개입하게 된다.

    판정(차단기·사이징)은 이미 보정된 기준선을 보는데 화면·텔레그램만 원본을 보면
    1,000만 계좌에서 300만을 뺀 순간 -30%가 찍힌다.
    """
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = -3_000_000
    assert trader.daily_pnl_base() == 7_000_000
    assert "출금 3,000,000원 제외" in trader.transfer_note()


def test_display_pnl_falls_back_to_the_start_asset(trader):
    """입출금이 없으면 종전과 같은 값이다 — 보정이 평상시 숫자를 바꾸면 안 된다."""
    trader.initial_asset = 10_000_000
    trader.net_transfer_today = 0
    assert trader.daily_pnl_base() == 10_000_000
    assert trader.transfer_note() == ""
