"""보류된 정지 중 입출금 대조가 **실제로** 다시 시도되는가 (2026-11-14).

[무엇이 틀려 있었나] _reconcile_offline_transfer 는 못 쟀을 때 0 을 돌려주고 주석에 '다음 주기에 다시 해 본다'고
 적어 두었다. 그런데 호출부(_monitor_account_status)는 같은 주기에 기준 원금을 고정하고 오늘 행에 그 원금을
 새 대조점으로 저장한다. 기준 원금이 잡히면 그 블록은 다시 돌지 않고, 내일의 대조는 오늘부터 잰다 —
 그 구간의 입출금은 영영 빠졌다(재현: 첫 주기에 기록분 읽기가 한 번 실패 → 정지 중 −30만 누락).
[고정하는 것]
  ① 다시 해 보면 될 수 있는 보류(DB 읽기 실패·예외)는 오늘 원금을 대조점으로 남기지 않는다 → 다음 기준선이
     같은 대조점으로 다시 재서 되찾는다.
  ② 다시 해도 같은 보류(증권사 장부 불일치·손익을 모르는 외부 매도)는 종전처럼 전진한다. 장부 불일치는
     운용자에게 알린다 — 그 구간은 다시 대조하지 않으므로.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import config
from modules.db_manager import DBManager
from tests.test_first_cycle_baseline_degraded import (CASH, DEPOSIT, HOLDINGS, SUMMARY,  # noqa: F401
                                                      _asset, trader)

PRINCIPAL = CASH + 1_000_000          # 현금 + 매입원가(10주 × 10만원)


def _d(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _acc():
    return f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"


@pytest.fixture
def db(tmp_path):
    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(tmp_path / "asset.sqlite")
    m = DBManager()
    with patch("modules.auto_trade.db_manager.db", m), patch("modules.db_manager.db", m):
        #  3일 전 대조점. 그 뒤 프로그램이 꺼진 사이 30만원이 빠졌다.
        m.save_daily_asset(_d(3), _acc(), 12_000_000.0, principal=PRINCIPAL + 300_000)
        yield m
    if getattr(getattr(m, "local", None), "conn", None):
        m.local.conn.close()
    config.DB_FILE_PATH = original


def _cycle(trader):  # noqa: F811
    logs, tg = [], []
    with patch('modules.auto_trade.account.get_asset_status_data', return_value=_asset()), \
         patch('modules.auto_trade.account_sync.load_daily_initial_asset', return_value=0), \
         patch('modules.auto_trade.account_sync.is_plausible_baseline', return_value=True), \
         patch('modules.auto_trade.account_sync.save_daily_initial_asset', return_value=True), \
         patch('modules.auto_trade.alert_delivered', return_value=True), \
         patch('modules.auto_trade.api.send_telegram_message', side_effect=lambda m, *a, **k: tg.append(str(m))), \
         patch.object(trader, '_run_account_circuit_breaker'), \
         patch.object(trader, 'log', side_effect=lambda m, *a, **k: logs.append(str(m))):
        trader._monitor_account_status(HOLDINGS, SUMMARY, DEPOSIT)
    return logs, tg


def _net(db, day):
    row = db._get_conn().execute("SELECT net_transfer FROM daily_asset_history WHERE date=? AND account=?",
                                 (day, _acc())).fetchone()
    return row[0] if row else None


def _next_baseline(trader):  # noqa: F811
    """다음 기준선(내일 첫 주기·다음 기동)을 흉내 낸다 — 기준 원금이 비어 그 블록이 다시 돈다."""
    trader.baseline_principal = 0
    trader._offline_reconcile_date = None
    return _cycle(trader)


def test_정상이면_정지_중_출금을_적고_오늘을_새_대조점으로_남긴다(db, trader):  # noqa: F811
    _cycle(trader)
    assert _net(db, _d(3)) == -300_000.0
    assert db.get_last_principal_snapshot(_acc(), _d(0)) == (_d(0), float(PRINCIPAL))


@pytest.mark.parametrize("broken", ["get_net_transfer_between", "get_realized_profit_between"])
def test_읽기_실패로_못_쟀으면_대조점을_전진시키지_않고_다음_기준선에서_되찾는다(db, trader, broken):  # noqa: F811
    """[실재현] 첫 주기에 DB 읽기가 한 번 실패한다 — 종전에는 −30만이 영영 빠졌다."""
    fail = ((0.0, False) if broken == "get_net_transfer_between" else (0, False, 'read'))
    with patch.object(db, broken, return_value=fail):
        logs, _ = _cycle(trader)
    assert _net(db, _d(3)) == 0.0
    assert db.get_last_principal_snapshot(_acc(), _d(0))[0] == _d(3), "못 잰 날의 원금이 새 대조점이 됐다"
    assert any("대조점을 새로 남기지 않습니다" in m for m in logs), logs
    assert trader.baseline_principal == PRINCIPAL, "오늘의 장중 감지가 쓸 기준 원금은 잡혀 있어야 한다"

    for _ in range(3):                      # 같은 날 이어지는 주기 — 기준 원금이 잡혀 있어 다시 재지 않는다
        _cycle(trader)
    assert _net(db, _d(3)) == 0.0

    _next_baseline(trader)                  # 다음 기준선에서 같은 대조점으로 다시 잰다
    assert _net(db, _d(3)) == -300_000.0, "보류한 구간을 다시 재지 못했다"
    assert db.get_last_principal_snapshot(_acc(), _d(0)) == (_d(0), float(PRINCIPAL))


def test_예외로_못_쟀어도_같다(db, trader):  # noqa: F811
    with patch.object(db, "get_net_transfer_between", side_effect=RuntimeError("boom")):
        _cycle(trader)
    assert db.get_last_principal_snapshot(_acc(), _d(0))[0] == _d(3)
    _next_baseline(trader)
    assert _net(db, _d(3)) == -300_000.0


def test_손익을_모르는_외부_매도는_다시_재도_같으므로_전진한다(db, trader):  # noqa: F811
    """그 행이 채워지기 전에는 내일도 같은 답이다 — 대조점을 묶어 두면 그 뒤의 입출금까지 못 적는다."""
    with patch.object(db, "get_realized_profit_between", return_value=(0, False, 'external_sell')):
        _cycle(trader)
    assert trader._offline_reconcile_deferred is None
    assert db.get_last_principal_snapshot(_acc(), _d(0)) == (_d(0), float(PRINCIPAL))


def test_장부_불일치는_전진하고_운용자에게_알린다(db, trader):  # noqa: F811
    """증권사 장부가 '그 사이 실현손익이 90만원 더 있다'고 말한다 — 우리가 모르는 매매다.

    다시 대조해도 같으므로 전진한다. 종전에는 화면 로그 한 줄뿐이라, 그 구간의 입출금이 드로다운 기준에서
    빠진 채 룩백 내내 가는 것을 운용자가 알 길이 없었다.
    """
    with patch('modules.auto_trade.account.fetch_period_realized',
               return_value={'realized': 900_000, 'cost': 0}):
        logs, tg = _cycle(trader)
    assert _net(db, _d(3)) == 0.0
    assert db.get_last_principal_snapshot(_acc(), _d(0)) == (_d(0), float(PRINCIPAL))
    alert = [m for m in tg if "정지 중 입출금 보정 보류" in m]
    assert len(alert) == 1, tg
    assert "-300,000원" in alert[0] and "다시 대조하지 않습니다" in alert[0]
    assert any("어긋납니다" in m for m in logs)


def test_구간_실현손익_조회는_실패_사유를_가른다(db):
    acc = _acc()
    assert db.get_realized_profit_between(_d(3), _d(1), acc) == (0, True)
    assert db.get_realized_profit_between(_d(3), _d(1), acc, detail=True) == (0, True, '')
    conn = db._get_conn()
    conn.execute("INSERT INTO trades (time, type, code, name, qty, price, odno, account, is_sim, profit_amt, "
                 "order_status) VALUES (?,?,?,?,?,?,?,?,0,?,'체결')",
                 (f"{_d(2)} 10:00:00", "매도(외부)", "005930", "삼성전자", "1", "1000", "X1", acc, 0))
    conn.commit()
    assert db.get_realized_profit_between(_d(3), _d(1), acc) == (0, False)
    assert db.get_realized_profit_between(_d(3), _d(1), acc, detail=True) == (0, False, 'external_sell')
    with patch.object(db, "_get_conn", side_effect=RuntimeError("locked")):
        assert db.get_realized_profit_between(_d(3), _d(1), acc, detail=True) == (0, False, 'read')
