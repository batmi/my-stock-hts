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


# ───────── ⑤ 우리가 모르는 매매가 있으면 보정하지 않는다 ─────────

def test_an_external_sell_blocks_the_gap_sum(db, trader):
    """[핵심] 실현손익을 모르는 외부 매도가 구간에 있으면 그 금액이 가짜 입출금이 된다."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 10_500_000.0)
    _sell(db, _d(5), 0, odno="EXT")                 # 외부 매도, 손익 미상
    db._get_conn().execute("UPDATE trades SET type = '매도(외부)' WHERE odno = 'EXT'")
    db._get_conn().commit()

    total, ok = db.get_realized_profit_between(_d(10), _d(1), ACC)
    assert (total, ok) == (0, False)
    assert _reconcile(trader, 10_500_000)[0] == 0, "모르는 매도를 입출금으로 적었다"


def test_a_backfilled_external_sell_is_trusted(db, trader):
    """[반대 방향] 기동 동기화가 손익을 채운 외부 매도는 정상이다 — 게이트가 과하면 안 된다.

    holdings_backfill.build_records 는 평단을 전진 재생해 매도의 실현손익까지 채운다.
    채워진 값까지 '모른다'로 보면 수동 매수분을 들고 있는 계좌는 영영 보정되지 않는다.
    """
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 10_500_000.0)
    _sell(db, _d(5), 500_000, odno="EXT")
    db._get_conn().execute("UPDATE trades SET type = '매도(외부)' WHERE odno = 'EXT'")
    db._get_conn().commit()

    total, ok = db.get_realized_profit_between(_d(10), _d(1), ACC)
    assert (total, ok) == (500_000, True)
    assert _reconcile(trader, 10_500_000)[0] == 0     # 실현손익으로 전부 설명된다


# ───────── ⑥ 증권사 장부와 대조한다 (정지 중 왕복매매) ─────────

def _broker(trader, book):
    return patch('modules.auto_trade.account.fetch_period_realized', return_value=book)


def test_a_roundtrip_while_off_is_caught_by_the_broker_book(db, trader):
    """[핵심] 꺼진 사이에 사서 그 사이에 판 매매는 우리 DB에 **행조차 없다**.

    holdings_backfill 은 지금 보유 중인 종목을 역산해 채우므로 왕복매매는 복원되지 않는다
    (그 모듈의 [한계] 주석). 그러면 그 실현손익이 통째로 가짜 입출금이 된다 — 손실 쪽이면
    자산 고점을 낮춰 리스크 한도가 조용히 열린다. 증권사 장부만이 답을 안다.
    """
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 9_000_000.0)
    with _broker(trader, {'realized': -1_000_000, 'cost': 0}):   # 왕복매매로 100만 손실
        assert _reconcile(trader, 9_000_000)[0] == 0, "모르는 매매를 출금으로 적었다"


def test_the_broker_book_agreeing_lets_the_correction_through(db, trader):
    """장부가 우리 기록과 맞으면 그대로 보정한다 — 대조가 기능을 막으면 안 된다."""
    _fill_incident(db)
    with _broker(trader, {'realized': 0, 'cost': 0}):
        assert _reconcile(trader, 27)[0] == -10_000


def test_the_cost_convention_is_tolerated(trader):
    """rlzt_pfls 가 제비용을 포함하는지가 응답마다 달라 보인다 — 둘 중 하나만 맞으면 된다."""
    with _broker(trader, {'realized': 530_000, 'cost': 30_000}):
        assert trader._realized_matches_broker("2026-08-01", "2026-08-10", 500_000, 50_000)


def test_no_broker_book_does_not_block(trader):
    """모의·토스·관찰 모드는 이 조회를 못 한다 — 대조 불가가 기능 정지가 되면 안 된다."""
    with _broker(trader, None):
        assert trader._realized_matches_broker("2026-08-01", "2026-08-10", 500_000, 50_000)


def test_a_blocked_reconcile_is_retried_next_cycle(db, trader):
    """보류는 '오늘 몫을 썼다'가 아니다 — 장부가 늦게 맞아떨어지면 그때 보정해야 한다."""
    _fill_incident(db)
    with _broker(trader, {'realized': 900_000, 'cost': 0}):
        assert _reconcile(trader, 27)[0] == 0
    with _broker(trader, {'realized': 0, 'cost': 0}):
        assert _reconcile(trader, 27)[0] == -10_000


# ───────── ⑦ 장중에 이미 적은 입출금을 다음 날 다시 더하지 않는다 (2026-11-08) ─────────

def _net(db, date_str):
    row = db._get_conn().execute(
        "SELECT net_transfer FROM daily_asset_history WHERE date = ? AND account = ?",
        (date_str, ACC)).fetchone()
    return row[0] if row else None


def test_an_intraday_transfer_is_not_added_again_next_morning(db, trader):
    """[실재현] 잔차는 대조점 이후의 입출금 **전부**다 — 장중 감지가 이미 그날 행에 적은 몫이 있다.

    종전에는 그것을 빼지 않아 장중 +100만 입금이 다음 날 아침 기준선을 잡을 때 대조점 행에
    한 번 더 더해졌다(net_transfer 200만). 출금이면 고점이 그만큼 더 낮아져 한도가 열린다.
    """
    db.save_daily_asset(_d(1), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 10_000_000.0, net_transfer=1_000_000)   # 장중 감지가 남긴 몫
    amount, tg = _reconcile(trader, 11_000_000)
    assert amount == 0, f"장중에 적은 입금을 또 더했다: {amount}"
    assert _net(db, _d(1)) == 1_000_000.0
    tg.assert_not_called()


def test_only_the_unrecorded_part_is_added(db, trader):
    """장중에 적은 뒤 프로그램이 꺼진 사이 또 입출금이 있었다면 그 나머지만 적는다."""
    db.save_daily_asset(_d(3), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(3), ACC, 10_000_000.0, net_transfer=-2_000_000)  # 그날 장중 출금
    db.save_daily_asset(_d(2), ACC, 8_000_000.0)                            # 대조점 없는 날
    # 꺼져 있던 사이 50만 더 출금 → 오늘 원금 750만
    amount, _ = _reconcile(trader, 7_500_000)
    assert amount == -500_000
    assert _net(db, _d(3)) == -2_500_000.0


def test_recorded_transfers_unreadable_means_no_correction(db, trader):
    """이미 적힌 몫을 못 읽으면 같은 돈을 두 번 적을 수 있다 — 모르면 안 건드린다."""
    _fill_incident(db)
    with patch.object(db, "get_net_transfer_between", return_value=(0.0, False)):
        assert _reconcile(trader, 27)[0] == 0
    assert _reconcile(trader, 27)[0] == -10_000, "보류는 '오늘 몫을 썼다'가 아니다"


def test_net_transfer_sum_counts_only_rows_the_drawdown_uses(db):
    """get_max_daily_asset 의 환산(asset > 0)에 실린 것만 '이미 반영됐다'고 할 수 있다."""
    db.save_daily_asset(_d(3), ACC, 1_000.0, net_transfer=-300)
    db.save_daily_asset(_d(2), ACC, 1_000.0, net_transfer=100)
    db.save_daily_asset(_d(1), ACC, 0.0, net_transfer=999)
    assert db.get_net_transfer_between(_d(3), _d(1), ACC) == (-200.0, True)
    assert db.get_net_transfer_between(_d(2), _d(2), ACC) == (100.0, True)
    assert db.get_net_transfer_between(_d(9), _d(8), ACC) == (0.0, True)


# ───────── ⑧ 토스 거래내역(1.2.28)과 대조한다 ─────────

def _ledger(book):
    return patch('modules.auto_trade.account.fetch_period_cash_flows', return_value=book)


def _book(transfer=0.0, income=0.0, securities_moves=0, unknown=0):
    return {'transfer': transfer, 'income': income, 'securities_moves': securities_moves,
            'unknown': unknown, 'rows': 1}


def test_a_roundtrip_while_off_is_caught_by_the_toss_ledger(db, trader):
    """[핵심] 토스에는 기간 실현손익 장부가 없어 이 대조가 늘 통과였다.

    거래내역이 '입출금은 없었다'고 말하는데 원금이 100만 줄었으면, 그 100만은 우리가 모르는
    매매의 손실이다. 출금으로 적으면 고점이 낮아져 리스크 한도가 조용히 열린다.
    """
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 9_000_000.0)
    with _ledger(_book(transfer=0.0)):
        assert _reconcile(trader, 9_000_000)[0] == 0, "모르는 매매를 출금으로 적었다"


def test_the_toss_ledger_agreeing_lets_the_correction_through(db, trader):
    _fill_incident(db)
    with _ledger(_book(transfer=-10_000.0)):
        assert _reconcile(trader, 27)[0] == -10_000


def test_dividends_are_not_recorded_as_a_deposit(db, trader):
    """배당은 돈이 들어왔어도 입출금이 아니다 — 문턱(최대 5만원)을 넘으면 '입금'으로 적히던 것."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 10_080_000.0)
    with _ledger(_book(income=80_000.0)):
        assert _reconcile(trader, 10_080_000)[0] == 0


def test_a_deposit_with_a_dividend_records_only_the_deposit(db, trader):
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 11_080_000.0)
    with _ledger(_book(transfer=1_000_000.0, income=80_000.0)):
        assert _reconcile(trader, 11_080_000)[0] == 1_000_000
    assert _net(db, _d(10)) == 1_000_000.0


def test_securities_moves_block_the_correction(db, trader):
    """타사 이관·주식배당 입고는 금액이 없다 — 매입원가가 늘어 원금이 뛰어도 그것이 입금인지 모른다."""
    _fill_incident(db)
    with _ledger(_book(transfer=-10_000.0, securities_moves=1)):
        assert _reconcile(trader, 27)[0] == 0


def test_unreadable_ledger_rows_block_the_correction(db, trader):
    _fill_incident(db)
    with _ledger(_book(transfer=-10_000.0, unknown=2)):
        assert _reconcile(trader, 27)[0] == 0


def test_no_ledger_keeps_the_previous_behaviour(db, trader):
    """가상투자·한투·조회 실패는 장부가 없다 — 대조 불가가 기능 정지가 되면 안 된다."""
    _fill_incident(db)
    with _ledger(None):
        assert _reconcile(trader, 27)[0] == -10_000


def test_the_ledger_window_runs_through_today(db, trader):
    """잔차에는 오늘 기준선을 잡기 전까지의 입출금도 들어 있다 — 실현손익과 달리 오늘 몫이 상쇄되지 않는다."""
    _fill_incident(db)
    with _ledger(_book(transfer=-10_000.0)) as m:
        _reconcile(trader, 27)
    start, end = m.call_args[0][:2]
    assert start == _d(35) and end == _d(0)


def test_ledger_tolerance_absorbs_buy_commissions(trader):
    """매수 수수료는 현금만 깎고 매입원가에 안 들어간다 — 그 잔돈으로 보정을 막으면 안 된다."""
    with _ledger(_book(transfer=-1_000_000.0)):
        ok, income = trader._cash_flows_match_broker("2026-08-01", "2026-08-10", -1_000_700.0, 50_000)
    assert ok and income == 0.0
    with _ledger(_book(transfer=-1_000_000.0)):
        ok, _ = trader._cash_flows_match_broker("2026-08-01", "2026-08-10", -1_200_000.0, 50_000)
    assert not ok


# ───────── ⑨ 대조점 날의 '기준선 이전' 거래를 두 번 세지 않는다 (2026-11-14) ─────────
#  대조점 원금은 그날 기준선을 **잰 시각**의 값이고, 거래내역 조회는 날짜 단위(그날 0시부터)다.
#  '입금하고 프로그램을 켠 날'이 대조점이면 그 입금은 원금에 이미 들어 있는데 거래내역에도 잡혀,
#  둘이 그만큼 어긋났다 — '모르는 매매'로 보류되고 그 사이 정지 중 입출금까지 묻혔다.

def _fake_transactions(rows):
    """결제일(from~to, 포함)로 거르는 가짜 GET /transactions — 한 쪽으로 끝난다."""
    def _get(transaction_types=None, from_date=None, to_date=None, cursor=None, limit=None):
        out = [r for r in rows
               if (not from_date or r['day'] >= from_date) and (not to_date or r['day'] <= to_date)]
        return {'transactions': out, 'hasNext': False, 'nextCursor': None}
    return _get


def _tx(kind, amount, day):
    return {'transactionType': kind, 'currency': 'KRW', 'assetClass': None, 'isCancellation': False,
            'netAmount': str(amount), 'marketCountry': 'KR', 'day': day}


def _toss(rows):
    """토스 모드로 두고 거래내역 API 만 가짜로 — 분류(api.toss)와 계좌 래퍼는 실제 코드가 돈다."""
    from contextlib import ExitStack
    from brokers import toss_api
    stack = ExitStack()
    stack.enter_context(patch.object(config.session, 'is_toss', True))
    stack.enter_context(patch('api.account._paper_active', return_value=False))
    stack.enter_context(patch.object(toss_api, 'get_transactions', _fake_transactions(rows)))
    return stack


def test_a_deposit_before_the_baseline_does_not_block_the_next_reconcile(db, trader):
    """[실재현] D 07시 입금 +100만 → 09시 기동(대조점) → 저녁 정지 뒤 −30만 출금 → 다음 날 재기동.

    수정 전: 거래내역 +70만 vs 원금 변화 −30만 = 100만 어긋남 → 보류, −30만이 적히지 않았다.
    """
    D = _d(1)
    rows = [_tx('DEPOSIT', 1_000_000, D)]                       # D 07:00 — 기동 전
    db.save_daily_asset(_d(3), ACC, 10_000_000.0, principal=10_000_000.0)
    with _toss(rows):
        asof = trader._book_flows_asof_now(D)                   # D 09:00 기준선을 잡는 순간
        assert asof == (1_000_000.0, 0.0)
        db.save_daily_asset(D, ACC, 11_000_000.0, principal=11_000_000.0, book_asof=asof)
        rows.append(_tx('WITHDRAWAL', 300_000, D))              # D 21:00 — 프로그램이 꺼진 뒤
        amount, _ = _reconcile(trader, 10_700_000)
    assert amount == -300_000, f"정지 중 30만 출금을 적지 못했다(기록 {amount:+,})"
    assert _net(db, D) == -300_000.0


def test_a_reference_row_without_the_asof_keeps_the_old_comparison(db, trader):
    """남긴 몫이 없는 대조점(옛 행·그때 조회 실패)은 빼지 않고 대조한다 — 어긋나면 보류하고 원인 후보를 적는다."""
    D = _d(1)
    rows = [_tx('DEPOSIT', 1_000_000, D), _tx('WITHDRAWAL', 300_000, D)]
    db.save_daily_asset(D, ACC, 11_000_000.0, principal=11_000_000.0)       # book_asof 없음
    assert db.get_book_asof(D, ACC) is None
    logs = []
    trader.log = logs.append
    with _toss(rows):
        assert _reconcile(trader, 10_700_000)[0] == 0
    assert any("기동 전의 입출금" in m for m in logs), logs


def test_income_before_the_baseline_is_not_subtracted_again(db, trader):
    """기준선 이전 배당(이미 대조점 원금에 든 돈)을 수익분으로 또 빼면 정지 중 입금이 그만큼 작게 적힌다."""
    D = _d(1)
    rows = [_tx('DIVIDEND', 80_000, D)]
    rows[0]['assetClass'] = 'STOCK'
    with _toss(rows):
        db.save_daily_asset(D, ACC, 10_080_000.0, principal=10_080_000.0,
                            book_asof=trader._book_flows_asof_now(D))
        assert db.get_book_asof(D, ACC) == (0.0, 80_000.0)
        rows.append(_tx('DEPOSIT', 1_000_000, D))                # 정지 뒤 입금
        amount, _ = _reconcile(trader, 11_080_000)
    assert amount == 1_000_000


def test_unreadable_rows_at_the_baseline_mean_unknown(trader):
    """해석 못 한 거래가 섞인 합계는 '기준선 이전 몫'으로 남기지 않는다. 장부가 없는 모드도 모른다."""
    D = _d(0)
    with _toss([_tx('DEPOSIT', 1_000_000, D), _tx('SOMETHING_NEW', 5, D)]):
        assert trader._book_flows_asof_now(D) is None
    assert trader._book_flows_asof_now(D) is None                # 가상투자·한투 — 조회 자체가 없다


def test_the_asof_lives_and_dies_with_the_principal(db):
    """원금을 새로 적으면 그 옆의 몫도 함께 바뀐다(없으면 비운다). 자산·순입출금만 고치는 호출은 보존한다."""
    D = _d(1)
    db.save_daily_asset(D, ACC, 100.0, principal=100.0, book_asof=(30.0, 2.0))
    db.save_daily_asset(D, ACC, 110.0)                           # 자산만
    db.save_daily_asset(D, ACC, 110.0, net_transfer=-5)          # 순입출금만
    assert db.get_book_asof(D, ACC) == (30.0, 2.0)
    assert db.get_last_principal_snapshot(ACC, D) == (D, 100.0) and _net(db, D) == -5.0
    db.save_daily_asset(D, ACC, 110.0, principal=120.0)          # 원금을 다시 쟀는데 몫은 모른다
    assert db.get_book_asof(D, ACC) is None, "옛 시각의 몫이 새 원금 옆에 남았다"
    assert db.get_book_asof(_d(9), ACC) is None                  # 없는 행


def test_the_baseline_snapshot_carries_the_asof():
    """기준 원금을 대조점으로 남기는 자리가 그 시각의 장부 몫을 함께 넘긴다(소스 가드)."""
    import ast
    import inspect
    from modules.auto_trade import account_sync
    tree = ast.parse(inspect.getsource(account_sync))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, 'attr', '') == 'save_daily_asset'
             and any(k.arg == 'principal' for k in n.keywords)]
    assert calls, "기준 원금 스냅샷 저장 자리를 찾지 못했다"
    for c in calls:
        assert any(k.arg == 'book_asof' for k in c.keywords), \
            f"{c.lineno}행: 원금만 남기고 그 시각의 장부 몫을 안 남긴다"


# ───────── ⑩ 장중 기록을 되돌리는 몫을 '정지 중 입출금'이라 알리지 않는다 (2026-11-14) ─────────

def test_reversing_an_intraday_dividend_is_not_called_a_withdrawal(db, trader):
    """[실재현] 장중 배당 +8만이 '입금'으로 적힌 다음 날 — 대조가 −8만으로 되돌린다(숫자는 맞다).

    수정 전에는 그것이 '정지 중 출금 자동 반영 −80,000원'으로 알려졌다. 출금은 없었다.
    """
    db.save_daily_asset(_d(1), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 10_000_000.0, net_transfer=80_000)     # 장중 감지가 남긴 '입금'
    logs = []
    trader.log = logs.append
    with _ledger(_book(income=80_000.0)):
        amount, tg = _reconcile(trader, 10_080_000)
    assert amount == -80_000 and _net(db, _d(1)) == 0.0
    msg = str(tg.call_args)
    assert "입출금 기록 정정" in msg and "정지 중" not in msg
    assert "조치할 것은 없습니다" in msg
    assert any("입출금 기록 정정" in m for m in logs) and not any("정지 중" in m for m in logs)


def test_a_plain_offline_transfer_keeps_its_wording(db, trader):
    """수익분만 있거나(정지 중 배당+입금) 장중 기록만 있으면 종전 문구 그대로다."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 11_080_000.0)
    with _ledger(_book(transfer=1_000_000.0, income=80_000.0)):
        _, tg = _reconcile(trader, 11_080_000)
    assert "정지 중 입금 자동 반영" in str(tg.call_args)


def test_a_ledger_mismatch_tells_the_operator_once(db, trader):
    """장부 불일치 보류는 다시 대조하지 않는다(대조점이 전진한다) — 화면 로그만으로는 운용자가 모른다."""
    db.save_daily_asset(_d(10), ACC, 10_000_000.0, principal=10_000_000.0)
    db.save_daily_asset(_d(1), ACC, 9_000_000.0)
    with _ledger(_book(transfer=0.0)):
        amount, tg = _reconcile(trader, 9_000_000)
    assert amount == 0 and trader._offline_reconcile_deferred is None
    assert tg.call_count == 1 and "정지 중 입출금 보정 보류" in str(tg.call_args)
    assert "-1,000,000원" in str(tg.call_args)
