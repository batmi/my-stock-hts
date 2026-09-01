"""프로그램이 꺼져 있던 사이의 입출금을 스스로 되찾는가.

[이 파일도 실사고에서 나왔다] 2026-08-31, 실계좌 44048158-01.
프로그램이 꺼져 있던 7/28~8/4 사이에 약 1만원이 빠졌다. 장중 감지(_monitor_account_status)는
'그날 기준 원금이 잡힌 뒤'의 변화만 보므로 이 출금은 잴 주체가 없었고, net_transfer 가
비어 있으니 get_max_daily_asset 의 환산도 일어나지 않았다. 07-27의 10,027원이 90일 룩백의
고점으로 남아

    드로다운 = (10,027 − 27) / 10,027 = 99.73%

가 매일 계산됐다(현재 자산 27원). DD_LEVEL_2를 넘으니 리스크 한도가 x0.8로 묶였고,
운용자가 DB를 직접 고치기 전에는 풀리지 않았다 — 운용자 개입이 필요한 상태 자체가 결함이다.

[여기서 고정하는 것]
  ① 원금 불변식으로 정지 구간의 입출금을 되찾아 **자동으로** 반영한다.
  ② 그 값은 '마지막 대조점 그날의 행'에 실린다 — 오늘 행에 실으면 이중 계산이다.
  ③ 모르면 안 건드린다. 실현손익을 못 쟀거나 대조점이 보존 기간 밖이면 아무것도 하지 않는다
     (잘린 실현손익은 가짜 '출금'이 되고, 그 방향의 오탐은 고점을 낮춰 한도를 연다).
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import config
from modules.auto_trade import AutoTrader
from modules.db_manager import DBManager

ACC = "44048158-01"


def _d(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture
def db(tmp_path):
    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(tmp_path / "asset.sqlite")
    m = DBManager()
    with patch("modules.auto_trade.db_manager.db", m), \
         patch("modules.db_manager.db", m):
        yield m
    if getattr(getattr(m, "local", None), "conn", None):
        m.local.conn.close()
    config.DB_FILE_PATH = original


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.initial_asset = 0
    yield t
    AutoTrader._instance = None


def _reconcile(trader, principal, realized_ok=True):
    with patch("modules.auto_trade.api.send_telegram_message") as tg:
        amount = trader._reconcile_offline_transfer(ACC, principal, realized_ok)
    return amount, tg


def _sell(db, date_str, profit, odno="A1", account=ACC):
    """매도 기록 한 건(실현손익 profit)을 남긴다."""
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO trades (time, type, code, name, qty, price, odno, account, "
        "is_sim, profit_amt, order_status) VALUES (?,?,?,?,?,?,?,?,0,?,'체결')",
        (f"{date_str} 10:00:00", "매도(AUTO)", "005930", "삼성전자", "1", "1000",
         odno, account, profit))
    conn.commit()


#  실사고 그대로: 90일 룩백 안에 10,027원 행이 여럿, 그 뒤 27원. 마지막 대조점은 07-27.
def _fill_incident(db):
    for ago in (73, 70, 66, 60, 55, 35):
        db.save_daily_asset(_d(ago), ACC, 10_027.0)
    db.save_daily_asset(_d(35), ACC, 10_027.0, principal=10_027.0)   # 마지막 가동일
    for ago in range(26, 0, -1):
        db.save_daily_asset(_d(ago), ACC, 27.0)
    return _d(35)


# ───────────────────────── ① 스스로 낫는다 ─────────────────────────

def test_the_real_incident_heals_itself(db, trader):
    """[핵심] 정지 중 1만원 출금이 자동으로 반영되어 가짜 드로다운이 사라진다."""
    last_date = _fill_incident(db)

    # 오늘 기동: 원금 27원(현금 27 + 매입원가 0 - 실현손익 0)
    amount, tg = _reconcile(trader, 27)
    assert amount == -10_000, f"정지 중 출금을 되찾지 못했다: {amount}"

    rows = dict((d, n) for d, n in db._get_conn().execute(
        "SELECT date, net_transfer FROM daily_asset_history WHERE account = ?", (ACC,)))
    assert rows[last_date] == -10_000.0
    assert rows[_d(1)] == 0.0, "오늘 행에 실으면 이미 반영된 자산에 이중 계산된다"

    hwm = db.get_max_daily_asset(_d(90), ACC)
    dd = max(0.0, (hwm - 27) / hwm * 100.0)
    assert dd < 1.0, f"가짜 드로다운이 남아 있다: {dd:.1f}% (HWM {hwm:,.0f}원)"


def test_the_operator_is_told_but_asked_to_do_nothing(db, trader):
    """알리되 조치를 요구하면 안 된다 — 자동 반영이 이 수정의 목적이다."""
    _fill_incident(db)
    _, tg = _reconcile(trader, 27)
    msg = str(tg.call_args)
    assert "출금" in msg and "조치할 것은 없습니다" in msg


def test_the_drawdown_cache_is_invalidated(db, trader):
    """환산이 바뀌었으면 드로다운을 다시 재야 한다 — 캐시가 남으면 그날 하루가 그대로 간다."""
    _fill_incident(db)
    trader._hwm_cache_date = datetime.now().strftime("%Y-%m-%d")
    _reconcile(trader, 27)
    assert trader._hwm_cache_date is None


def test_a_deposit_is_reflected_too(db, trader):
    """입금 방향도 같다 — 옛 자산이 올라가 드로다운을 **과소**평가하지 않게 한다."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 15_000_000.0)
    amount, _ = _reconcile(trader, 15_000_000)
    assert amount == 5_000_000


# ───────────────────── ② 오탐하지 않는다 ─────────────────────

def test_realized_profit_is_not_a_withdrawal(db, trader):
    """정지 중 매도로 손실이 났을 뿐인데 출금으로 오인하면 고점이 낮아진다(한도가 열린다)."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 9_000_000.0)
    _sell(db, _d(5), -1_000_000)                    # 정지 중 100만원 실현 손실
    amount, tg = _reconcile(trader, 9_000_000)
    assert amount == 0, f"실현손익을 입출금으로 오인했다: {amount}"
    assert not tg.called


def test_realized_profit_counts_each_order_once(db, trader):
    """한 주문의 접수·체결 두 행이 남아도 실현손익은 한 번만 센다 (중복 계산 = 가짜 입금)."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 10_500_000.0)
    _sell(db, _d(5), 500_000, odno="A1")
    _sell(db, _d(5), 500_000, odno="A1")            # 같은 주문의 두 번째 기록
    amount, _ = _reconcile(trader, 10_500_000)
    assert amount == 0, f"같은 주문을 두 번 세어 가짜 입출금이 났다: {amount}"


def test_noise_below_the_threshold_is_ignored(db, trader):
    """매수 수수료·세금 잔돈은 입출금이 아니다."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 9_997_000.0)
    amount, _ = _reconcile(trader, 9_997_000)       # -3,000원 (문턱 5만원)
    assert amount == 0


def test_the_threshold_scales_down_for_small_accounts(trader):
    """[실사고의 교훈] 5만원 고정이면 소액 계좌에서 전 재산이 빠져나가도 못 잡는다."""
    assert trader._offline_transfer_threshold(10_000_000) == 50_000
    assert trader._offline_transfer_threshold(10_027) < 10_000
    assert trader._offline_transfer_threshold(0) == 100.0   # 바닥은 있다


# ───────────────── ③ 모르면 안 건드린다 ─────────────────

def test_gated_on_realized_ok(db, trader):
    """실현손익을 못 쟀으면 그 금액이 그대로 가짜 입출금이 된다 — 감지 자체를 하지 않는다."""
    _fill_incident(db)
    amount, _ = _reconcile(trader, 27, realized_ok=False)
    assert amount == 0


def test_no_reference_point_no_correction(db, trader):
    """이 계좌의 첫 운용에는 대조할 것이 없다."""
    db.save_daily_asset(_d(1), ACC, 10_000_000.0)   # principal 없음
    amount, _ = _reconcile(trader, 5_000_000)
    assert amount == 0


def test_a_reference_outside_the_retention_window_is_skipped(db, trader):
    """보존 기간 밖이면 그 사이 매도 기록이 지워져 실현손익 합이 잘린다 = 가짜 출금."""
    old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    db.save_daily_asset(old, ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 5_000_000.0)
    amount, _ = _reconcile(trader, 5_000_000)
    assert amount == 0


def test_the_same_transfer_is_not_counted_twice(db, trader):
    """되찾은 뒤 오늘의 대조점을 남기면, 다시 켜도 같은 입출금이 또 잡히면 안 된다."""
    _fill_incident(db)
    assert _reconcile(trader, 27)[0] == -10_000
    # 기동 경로가 하는 일: 오늘 행에 오늘의 원금을 남긴다
    db.save_daily_asset(_d(0), ACC, 27.0, principal=27.0)
    assert _reconcile(trader, 27)[0] == 0


# ───────────────── ④ 기록 자체를 망가뜨리지 않는다 ─────────────────

def test_add_net_transfer_accumulates(db):
    """그날 장중에 이미 감지된 입출금 위에 덮어쓰면 먼저 잰 값이 사라진다."""
    db.save_daily_asset(_d(3), ACC, 1_000.0, net_transfer=-500)
    assert db.add_net_transfer(_d(3), ACC, -200) is True
    got, = db._get_conn().execute(
        "SELECT net_transfer FROM daily_asset_history WHERE date = ? AND account = ?",
        (_d(3), ACC)).fetchone()
    assert got == -700.0


def test_add_net_transfer_never_creates_a_row(db):
    """없는 날을 새로 만들면 자산 없는 행이 생겨 이력이 더 나빠진다."""
    assert db.add_net_transfer(_d(3), ACC, -200) is False
    assert db._get_conn().execute(
        "SELECT COUNT(*) FROM daily_asset_history").fetchone()[0] == 0


def test_saving_an_asset_row_preserves_the_principal(db):
    """자산만 갱신하는 호출이 대조점을 지우면 오프라인 감지가 다시 꺼진다."""
    db.save_daily_asset(_d(3), ACC, 1_000.0, principal=900.0)
    db.save_daily_asset(_d(3), ACC, 1_100.0)
    snap = db.get_last_principal_snapshot(ACC, _d(2))
    assert snap == (_d(3), 900.0)


def test_a_zero_asset_row_is_not_a_reference_point(db):
    """자산 0인 행에 순입출금을 적으면 환산(WHERE asset > 0)에서 통째로 빠진다."""
    db.save_daily_asset(_d(3), ACC, 0.0, principal=900.0)
    assert db.get_last_principal_snapshot(ACC, _d(2)) is None
