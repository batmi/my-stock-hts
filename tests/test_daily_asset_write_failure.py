"""자산 스냅샷 **쓰기** 실패가 드러나고, 다음 주기에 다시 시도되는가.

[배경 · 2026-09-06 감사] 읽기 쪽(get_max_daily_asset)은 하루 전 감사에서 '조회 실패'와
'이력 없음'을 갈라 예외를 올리도록 고쳐졌다. 그런데 같은 값을 **쓰는** save_daily_asset 은
예외를 통째로 삼키고 아무것도 돌려주지 않았다. 파일 쪽 쌍둥이(common.save_daily_initial_asset)
는 처음부터 bool 을 돌려주고 그 사유까지 독스트링에 적어 뒀다 — 둘은 같은 기준선을 담는다.

무엇이 걸려 있나: daily_asset_history 의 net_transfer 는 옛 자산을 오늘의 자본으로 환산하는
값이다. 이 값이 유실되면 출금이 그대로 '손실'로 읽혀 가짜 드로다운이 남고, 그 고점이
DD_LOOKBACK_DAYS(기본 90일) 내내 리스크 한도를 묶는다([[daily-asset-baseline-transfers]]).
실측: 1,000만 계좌에서 300만 출금 기록이 유실되면 드로다운이 0.0% 가 아니라 30.0% 로
계산돼 DD_LEVEL_2(10%)를 넘는다.

게다가 호출부는 쓰기 **전에** 메모리 표식을 찍었다. 한 번 실패하면 다음 주기의 조건이
거짓이 되어 그날 내내 다시 시도하지 않는다 — 실패가 영구가 된다.
"""
import logging
import sqlite3

import pytest

from modules import db_manager


ACC = "TEST-DAILY-ASSET"


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


class _BrokenConn:
    """cursor() 부터 깨진다 — 디스크 IO 오류(라즈베리파이 SD 카드)를 흉내 낸다."""

    def cursor(self):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):  # pragma: no cover - 도달하지 않는다
        pass


@pytest.fixture
def broken_write(monkeypatch):
    """쓰기만 깨뜨린다 — 뒤이은 조회는 정상이어야 '유실된 결과'를 볼 수 있다.

    [주의] 패치는 반드시 **클래스**에 건다. 인스턴스에 걸면 되돌릴 때 바인드 메서드가
     인스턴스 속성으로 남아, 뒤이은 테스트의 클래스 패치를 가린다 — 실제로 그 때문에
     test_db_failure_visibility 가 통째로 통과해 버렸다(전체 실행에서만 드러났다).
    """
    import contextlib

    @contextlib.contextmanager
    def _broken():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _BrokenConn())
            yield

    return _broken


# ─────────────────────────────────────────────
# 1. 실패를 돌려주는가 · 흔적을 남기는가
# ─────────────────────────────────────────────

def test_a_failed_snapshot_write_reports_false(broken_write):
    with broken_write():
        assert _db().save_daily_asset("2026-09-06", ACC, 10_000_000) is False


def test_a_successful_snapshot_write_reports_true():
    assert _db().save_daily_asset("2026-09-06", ACC, 10_000_000) is True


def test_a_failed_snapshot_write_is_logged(broken_write, caplog):
    with caplog.at_level(logging.ERROR, logger="modules.db_manager"), broken_write():
        _db().save_daily_asset("2026-09-06", ACC, 10_000_000)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("자산 스냅샷 저장 실패" in m for m in msgs), msgs


def test_a_locked_db_still_retries_then_reports_false(monkeypatch):
    """'locked' 는 종전대로 5회까지 기다린다 — 재시도를 없애는 수정이 아니다."""
    calls = []

    class _Locked:
        def cursor(self):
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        def commit(self):  # pragma: no cover
            pass

    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Locked())
    monkeypatch.setattr(db_manager.time, "sleep", lambda s: None)
    assert _db().save_daily_asset("2026-09-06", ACC, 10_000_000) is False
    assert len(calls) == 5, f"재시도 횟수가 바뀌었다: {len(calls)}"


# ─────────────────────────────────────────────
# 2. 무엇이 걸려 있나 — 유실된 net_transfer 가 만드는 가짜 드로다운
# ─────────────────────────────────────────────

def test_a_lost_net_transfer_turns_a_withdrawal_into_a_drawdown(broken_write):
    db = _db()
    db.save_daily_asset("2026-09-05", ACC, 10_000_000)

    with broken_write():
        assert db.save_daily_asset("2026-09-06", ACC, 10_000_000,
                                   net_transfer=-3_000_000) is False

    equity = 7_000_000                      # 300만 출금 후의 실제 자산
    hwm = db.get_max_daily_asset("2026-06-08", ACC)
    dd = (hwm - equity) / hwm * 100
    assert dd == pytest.approx(30.0), (
        f"기록이 유실됐는데 드로다운이 {dd:.1f}% 다 — 이 상황을 재현하지 못한다")


def test_the_same_write_when_it_lands_leaves_no_drawdown():
    db = _db()
    db.save_daily_asset("2026-09-05", ACC, 10_000_000)
    assert db.save_daily_asset("2026-09-06", ACC, 10_000_000,
                               net_transfer=-3_000_000) is True

    hwm = db.get_max_daily_asset("2026-06-08", ACC)
    assert hwm == pytest.approx(7_000_000), hwm


# ─────────────────────────────────────────────
# 3. 호출부 — 한 번 실패하면 다시 시도하는가
# ─────────────────────────────────────────────

from unittest.mock import patch          # noqa: E402

import config                            # noqa: E402
from modules.auto_trade import AutoTrader  # noqa: E402

CODE, NAME = "005930", "삼성전자"


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.initial_asset = 11_000_000
    t.baseline_principal = 0
    t.buy_halted = False
    yield t
    t.is_running = False


def _holdings(qty=10, buy=100_000, price=100_000):
    pchs, evlu = buy * qty, price * qty
    return [{'pdno': CODE, 'prdt_name': NAME, 'hldg_qty': str(qty),
             'ord_psbl_qty': str(qty), 'pchs_avg_pric': str(buy), 'prpr': str(price),
             'pchs_amt': str(pchs), 'evlu_amt': str(evlu),
             'evlu_pfls_amt': str(evlu - pchs),
             'evlu_pfls_rt': f"{(evlu - pchs) / pchs * 100:.2f}"}]


def _cycle(trader, cash, holdings, save_result=True):
    """한 주기 실행. save_result 로 자산 이력 쓰기의 성패를 정한다."""
    evlu = sum(int(h['evlu_amt']) for h in holdings)
    asset = {'tot_asset': cash + evlu, 'sec_eval': evlu, 'order_possible': cash}
    deposit = {'deposit': cash, 'd2_deposit': cash, 'd2_real': cash,
               'foreign_deposit': 0, 'order_possible': cash}
    summary = [{'dnca_tot_amt': str(cash), 'prvs_rcdl_excc_amt': str(cash),
                'scts_evlu_amt': str(evlu), 'tot_evlu_amt': str(cash + evlu)}]

    with patch('modules.auto_trade.account.get_asset_status_data', return_value=asset), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.save_daily_asset',
               return_value=save_result) as save_asset, \
         patch('modules.auto_trade.db_manager.db.shift_daily_assets'), \
         patch('modules.auto_trade.save_daily_initial_asset'), \
         patch('modules.auto_trade.load_daily_initial_asset', return_value=0), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch.object(trader, '_refine_trade_records', side_effect=lambda x: x):
        trader._monitor_account_status(holdings, summary, deposit)
    return [c for c in save_asset.call_args_list
            if c.kwargs.get('net_transfer') is not None]


def test_a_failed_net_transfer_write_is_retried_next_cycle(trader):
    """[핵심] 쓰기가 실패한 주기 다음에도 같은 값을 다시 쓰려 해야 한다.

    종전에는 메모리 표식(net_transfer_today)을 쓰기 **전에** 찍어서, 실패한 뒤에는
    조건이 거짓이 되어 그날 내내 재시도가 없었다.
    """
    cash = 10_000_000
    _cycle(trader, cash, _holdings())          # 기준 확립
    _cycle(trader, cash, _holdings())

    out = _cycle(trader, cash - 3_000_000, _holdings(), save_result=False)
    assert out, "출금이 순입출금 기록으로 이어지지 않았다 — 시나리오가 재현되지 않는다"
    first = out[-1].kwargs['net_transfer']

    again = _cycle(trader, cash - 3_000_000, _holdings(), save_result=False)
    assert again, "실패한 뒤 다음 주기에 다시 시도하지 않았다"
    assert again[-1].kwargs['net_transfer'] == first


def test_a_successful_write_is_not_repeated_every_cycle(trader):
    """성공했으면 매 주기 다시 쓰지 않는다 — 파이3의 IO 를 아끼는 종전 동작."""
    cash = 10_000_000
    _cycle(trader, cash, _holdings())
    _cycle(trader, cash, _holdings())

    assert _cycle(trader, cash - 3_000_000, _holdings(), save_result=True)
    assert not _cycle(trader, cash - 3_000_000, _holdings(), save_result=True)


def test_the_in_memory_correction_survives_a_failed_write(trader):
    """DB 쓰기가 실패해도 **오늘의** 보정(차단기·사이징)은 유지돼야 한다.

    net_transfer_today 는 effective_baseline 이 즉시 쓰는 값이다. 재시도를 위해
    이것까지 비워 두면, 이번엔 오늘의 차단기가 출금을 손실로 읽는다.
    """
    cash = 10_000_000
    _cycle(trader, cash, _holdings())
    _cycle(trader, cash, _holdings())
    _cycle(trader, cash - 3_000_000, _holdings(), save_result=False)
    assert trader.net_transfer_today != 0, "실패했다고 당일 보정까지 사라졌다"
