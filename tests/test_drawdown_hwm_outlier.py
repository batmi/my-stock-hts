"""자산 고점(HWM)이 오염되면 리스크 한도가 석 달간 묶인다.

[이 파일은 실사고에서 나왔다] 2026-08-23(일) 가상투자 계좌의 daily_asset_history에
20,028,670원 한 행이 박혔다 — 직전 값 10,028,670원에 정확히 1,000만원이 얹힌 값이다.
자산은 내내 1,000만원 그대로였는데, 그 한 행이 90일 룩백의 고점이 되어

    드로다운 = (20,028,670 − 9,998,824) / 20,028,670 = 50.1%

가 상시 계산됐다. DD_LEVEL_2(10%)를 넘으니 DD_SCALE_2(x0.8)가 걸리고, 히트 캡이
8.5% → 6.8%로 조여져 **한국콜마 증액이 206주기 연속 차단**됐다
(증액 리스크 89,000원 > 남은 예산 35,000원). 08-27·28 로그의 드로다운 세 값
(50.0 / 50.1 / 50.3%)이 이 한 행으로 소수점까지 재현된다.

[여기서 고정하는 것]
  ① 혼자만 튄 행은 고점이 아니다 — 그 한 행이 룩백 내내 한도를 묶는다.
  ② 그러나 고점을 깎는 것은 드로다운을 **과소**평가(한도가 열리는) 방향이라 위험하다.
     실제로 자산이 늘어 여러 날 그 수준을 유지하면 절대 깎지 않는다.
  ③ 깊은 드로다운은 조용히 지나가지 않는다 — 가짜면 석 달을 묶고, 진짜면 재앙이다.
"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import config
from modules.db_manager import DBManager

ACC = "PAPER-"


@pytest.fixture
def db(tmp_path):
    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(tmp_path / "asset.sqlite")
    m = DBManager()
    yield m
    if getattr(getattr(m, "local", None), "conn", None):
        m.local.conn.close()
    config.DB_FILE_PATH = original


def _fill(db, series, account=ACC):
    for date, asset in series:
        db.save_daily_asset(date, account, asset)


#  실사고 그대로의 계열(2026-08-21 ~ 08-29, 08-23이 오염된 행)
REAL_INCIDENT = [
    ("2026-08-21", 10_000_000.0), ("2026-08-22", 10_028_670.0),
    ("2026-08-23", 20_028_670.0),                                  # ← 유령 행
    ("2026-08-24", 10_028_670.0), ("2026-08-25", 10_016_860.0),
    ("2026-08-26", 10_033_360.0), ("2026-08-27",  9_959_424.0),
    ("2026-08-28",  9_998_824.0), ("2026-08-29", 10_084_924.0),
]


def test_the_real_incident_row_is_not_treated_as_a_peak(db):
    """[핵심] 2026-08-23의 20,028,670원 한 행이 고점이 되면 안 된다."""
    _fill(db, REAL_INCIDENT)
    hwm = db.get_max_daily_asset("2026-05-30", ACC)
    assert hwm == 10_084_924.0, f"유령 행이 고점으로 남았다: {hwm:,.0f}"


def test_the_real_incident_no_longer_fakes_a_50_percent_drawdown(db):
    """그 행을 빼면 드로다운이 0이 된다 — 자산은 내내 1,000만원이었다."""
    _fill(db, REAL_INCIDENT)
    hwm = db.get_max_daily_asset("2026-05-30", ACC)
    equity = 9_998_824.0                       # 2026-08-28 실제 자산
    dd = max(0.0, (hwm - equity) / hwm * 100.0)
    assert dd < 5.0, f"가짜 드로다운이 남아 있다: {dd:.1f}%"

    # 대조: 종전(단순 MAX)이었다면 50.1%였다
    old = max(a for _d, a in REAL_INCIDENT)
    assert round((old - equity) / old * 100.0, 1) == 50.1


# ───────────────────── 깎으면 안 되는 경우 ─────────────────────

def test_real_growth_is_never_trimmed(db):
    """[핵심·반대 방향] 실제로 자산이 늘면 고점을 깎으면 안 된다.

    고점을 깎는 것은 드로다운을 과소평가해 **한도가 조용히 열리는** 방향이다.
    그 수준의 행이 여러 날 남아 있으면 실제 자산 수준으로 본다.
    """
    _fill(db, [(f"2026-08-{d:02d}", 10_000_000.0) for d in range(1, 11)]
             + [(f"2026-08-{d:02d}", 25_000_000.0) for d in range(11, 21)])
    assert db.get_max_daily_asset("2026-05-30", ACC) == 25_000_000.0


def test_a_recent_jump_with_company_is_kept(db):
    """[핵심·고립 조건] 중앙값의 2배라도 그 수준의 날이 여럿이면 실제 자산이다.

    룩백 막바지에 자산이 크게 늘면 중앙값 대비 배수는 커지지만 고점은 진짜다. 이걸 깎으면
    드로다운을 과소평가해 **한도가 조용히 열린다** — 오염 행을 남기는 것보다 위험한 방향이다.
    (배수만으로 판정하면 이 경우가 잘려 나가므로 '고립'을 함께 요구한다.)
    """
    _fill(db, [(f"2026-08-{d:02d}", 10_000_000.0) for d in range(1, 16)]
             + [(f"2026-08-{d:02d}", 20_000_000.0) for d in range(16, 21)])
    hwm = db.get_max_daily_asset("2026-05-30", ACC)
    assert hwm == 20_000_000.0, f"진짜 고점을 깎았다: {hwm:,.0f}"


def test_a_deposit_that_shifted_the_whole_series_is_kept(db):
    """정당한 입금은 shift_daily_assets가 과거를 함께 올린다 — 중앙값도 따라 오른다."""
    _fill(db, [(f"2026-08-{d:02d}", 10_000_000.0) for d in range(1, 11)])
    db.shift_daily_assets(ACC, 10_000_000)
    db.save_daily_asset("2026-08-11", ACC, 20_000_000.0)
    assert db.get_max_daily_asset("2026-05-30", ACC) == 20_000_000.0


def test_a_gentle_climb_is_kept(db):
    """서서히 오른 계열의 꼭대기는 오염이 아니다."""
    _fill(db, [(f"2026-08-{d:02d}", 10_000_000.0 + d * 200_000) for d in range(1, 21)])
    assert db.get_max_daily_asset("2026-05-30", ACC) == 10_000_000.0 + 20 * 200_000


def test_a_flat_series_is_untouched(db):
    _fill(db, [(f"2026-08-{d:02d}", 10_000_000.0) for d in range(1, 6)])
    assert db.get_max_daily_asset("2026-05-30", ACC) == 10_000_000.0


def test_a_single_row_account_is_untouched(db):
    _fill(db, [("2026-08-21", 10_000_000.0)])
    assert db.get_max_daily_asset("2026-05-30", ACC) == 10_000_000.0


# ───────────────────── 경계·잡동사니 ─────────────────────

def test_two_isolated_spikes_are_both_removed(db):
    _fill(db, [(f"2026-08-{d:02d}", 10_000_000.0) for d in range(1, 11)]
             + [("2026-08-11", 30_000_000.0), ("2026-08-12", 50_000_000.0)])
    assert db.get_max_daily_asset("2026-05-30", ACC) == 10_000_000.0


def test_other_accounts_do_not_contaminate(db):
    _fill(db, [(f"2026-08-{d:02d}", 10_000_000.0) for d in range(1, 6)])
    _fill(db, [("2026-08-03", 90_000_000.0)], account="OTHER-")
    assert db.get_max_daily_asset("2026-05-30", ACC) == 10_000_000.0


def test_rows_before_the_lookback_are_ignored(db):
    """룩백 제한이 살아 있어야 옛 고점이 영원히 따라다니지 않는다."""
    _fill(db, [("2026-01-02", 50_000_000.0)]
             + [(f"2026-08-{d:02d}", 10_000_000.0) for d in range(1, 6)])
    assert db.get_max_daily_asset("2026-08-01", ACC) == 10_000_000.0


def test_an_empty_account_reports_nothing_rather_than_zero(db):
    """'못 잼'과 '0원'은 다르다 — 0을 돌려주면 드로다운이 0으로 읽힌다."""
    assert db.get_max_daily_asset("2026-05-30", "NOBODY-") is None


# ───────────────────── 깊은 드로다운 경보 ─────────────────────

def test_a_deep_drawdown_alerts_once_a_day():
    """[핵심] 50% 드로다운이 로그 한 줄에만 묻히면 석 달을 묶고도 아무도 모른다."""
    from modules.auto_trade import AutoTrader

    t = AutoTrader()
    t.initial_asset = 10_000_000.0
    t.current_total_asset = 5_000_000.0
    t._dd_alert_date = None
    t.market_index_status = {}
    t.risk_scale = 1.0

    params = dict(config.RISK_SCALING_PARAMS)
    params.update({"USE_REGIME_RISK_SCALING": False, "USE_WHIPSAW_RISK_SCALING": False,
                   "USE_DRAWDOWN_RISK_SCALING": True})
    with patch.object(config, 'RISK_SCALING_PARAMS', params), \
         patch.object(t, '_get_account_drawdown_pct', return_value=50.1), \
         patch.object(t, 'log'), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        t._update_risk_scale()
        t._update_risk_scale()                # 같은 날 두 번째 — 도배하지 않는다

    assert tg.call_count == 1, f"경보가 {tg.call_count}번 나갔다"
    body = tg.call_args[0][0]
    assert "50.1%" in body and "daily_asset_history" in body, (
        "경보에 원인을 찾아갈 실마리가 없다")
    assert t.risk_scale == pytest.approx(params["DD_SCALE_2"])


def test_a_shallow_drawdown_does_not_alert():
    """대조군 — 얕은 드로다운까지 알리면 경보가 소음이 된다."""
    from modules.auto_trade import AutoTrader

    t = AutoTrader()
    t.initial_asset = 10_000_000.0
    t.current_total_asset = 9_400_000.0
    t._dd_alert_date = None
    t.market_index_status = {}
    params = dict(config.RISK_SCALING_PARAMS)
    params.update({"USE_REGIME_RISK_SCALING": False, "USE_WHIPSAW_RISK_SCALING": False})
    with patch.object(config, 'RISK_SCALING_PARAMS', params), \
         patch.object(t, '_get_account_drawdown_pct', return_value=6.0), \
         patch.object(t, 'log'), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        t._update_risk_scale()
    tg.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# 입출금 보정 — 이력을 옮기지 않고 환산한다
#
# 출금하면 그 전날들의 자산은 '더 이상 내 것이 아닌 돈'을 포함한다. 그대로 두면 그 값이
# 고점이 되어 가짜 드로다운이 룩백 내내 리스크 한도를 묶는다(2026-08-23 실사고).
#
# [왜 옮기지 않는가] 종전에는 daily_asset_history 를 통째로 평행이동했다. 되돌릴 수 없고,
# 입출금 추정이 틀리면 고점이 낮아져 드로다운을 **과소**평가한다 = 한도가 조용히 열린다.
# 그래서 30% 상한을 두고 초과분은 사람에게 넘겼는데, 그 미반영분이 다시 90일짜리 가짜
# 드로다운이 됐다. 원본을 두고 환산만 하면 상한도 사람도 필요 없다.
# ──────────────────────────────────────────────────────────────────────────

def test_a_withdrawal_no_longer_leaves_a_phantom_peak(db):
    """[핵심] 300만원 출금 뒤 700만원 계좌의 드로다운은 30%가 아니라 0%다."""
    for d in range(1, 6):
        db.save_daily_asset(f"2026-08-{d:02d}", ACC, 10_000_000.0)
    db.save_daily_asset("2026-08-05", ACC, 10_000_000.0, net_transfer=-3_000_000)
    db.save_daily_asset("2026-08-06", ACC, 7_000_000.0, net_transfer=0)

    hwm = db.get_max_daily_asset("2026-05-30", ACC)
    assert hwm == 7_000_000.0, f"출금 전 자산이 고점으로 남았다: {hwm:,.0f}"
    assert (hwm - 7_000_000) / hwm * 100 == 0.0


def test_a_deposit_raises_the_old_peaks_to_be_comparable(db):
    """입금은 반대 방향 — 옛 자산을 올려야 오늘과 비교된다(안 올리면 드로다운 과소평가)."""
    for d in range(1, 5):
        db.save_daily_asset(f"2026-08-{d:02d}", ACC, 10_000_000.0)
    db.save_daily_asset("2026-08-05", ACC, 10_000_000.0, net_transfer=+5_000_000)
    db.save_daily_asset("2026-08-06", ACC, 15_000_000.0, net_transfer=0)

    assert db.get_max_daily_asset("2026-05-30", ACC) == 15_000_000.0


def test_the_transfer_day_counts_its_own_transfer(db):
    """자산 행은 그날 '시작' 스냅샷이고 입출금은 장중에 난다 — 자기 날 것도 빼야 한다."""
    db.save_daily_asset("2026-08-05", ACC, 10_000_000.0, net_transfer=-3_000_000)
    assert db.get_max_daily_asset("2026-05-30", ACC) == 7_000_000.0


def test_a_real_loss_after_a_withdrawal_is_still_a_drawdown(db):
    """[대조군] 출금을 뺀 뒤에도 진짜로 줄었으면 드로다운이다 — 보정이 손실을 가리면 안 된다."""
    for d in range(1, 6):
        db.save_daily_asset(f"2026-08-{d:02d}", ACC, 10_000_000.0)
    db.save_daily_asset("2026-08-05", ACC, 10_000_000.0, net_transfer=-3_000_000)
    db.save_daily_asset("2026-08-06", ACC, 6_000_000.0, net_transfer=0)   # 100만 실손실

    hwm = db.get_max_daily_asset("2026-05-30", ACC)
    assert hwm == 7_000_000.0
    assert round((hwm - 6_000_000) / hwm * 100, 1) == 14.3


def test_the_raw_history_is_left_intact(db):
    """[되돌릴 수 있음] 환산은 읽을 때만 한다 — 원본이 남아야 잘못된 값이 굳지 않는다."""
    db.save_daily_asset("2026-08-05", ACC, 10_000_000.0, net_transfer=-3_000_000)
    row = db.execute_query("SELECT asset, net_transfer FROM daily_asset_history "
                           "WHERE date='2026-08-05'", fetch='one')
    assert row['asset'] == 10_000_000.0, "원본 자산이 덮어써졌다"
    assert row['net_transfer'] == -3_000_000


def test_updating_the_asset_alone_keeps_the_transfer(db):
    """자산만 갱신하는 호출이 입출금 기록을 지우면 드로다운 기준이 다시 어긋난다."""
    db.save_daily_asset("2026-08-05", ACC, 10_000_000.0, net_transfer=-3_000_000)
    db.save_daily_asset("2026-08-05", ACC, 10_050_000.0)          # net_transfer 미지정
    row = db.execute_query("SELECT asset, net_transfer FROM daily_asset_history "
                           "WHERE date='2026-08-05'", fetch='one')
    assert (row['asset'], row['net_transfer']) == (10_050_000.0, -3_000_000)


def test_rows_without_a_transfer_column_still_work(db):
    """[하위 호환] 옛 행(net_transfer 없음)은 0으로 읽어 종전과 같은 결과를 낸다."""
    for d in range(1, 6):
        db.save_daily_asset(f"2026-08-{d:02d}", ACC, 10_000_000.0)
    db.execute_query("UPDATE daily_asset_history SET net_transfer = NULL")
    assert db.get_max_daily_asset("2026-05-30", ACC) == 10_000_000.0


def test_the_outlier_guard_still_applies_after_adjustment(db):
    """환산 뒤에도 고립 이상치 방어가 살아 있어야 한다(두 방어가 서로를 무력화하면 안 된다)."""
    _fill(db, REAL_INCIDENT)
    assert db.get_max_daily_asset("2026-05-30", ACC) == 10_084_924.0
