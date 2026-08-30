"""자산 평가의 기준 시세가 시간대마다 옳은가 — 정규장=현재가, 마감 후=KRX 확정 종가.

[사고] 2026-08-30 20:00 실측. 같은 계좌인데 두 화면의 총자산이 39,300원 달랐다.
  9-6-5 성과 현황   10,084,924원 (주식 3,703,700 = 마지막 NXT 체결가로 '지금' 재평가)
  9-6-4 자산 곡선   10,124,224원 (주식 3,743,000 = 08-28 15:19:40 장중가 스냅샷)
  실제 KRX 확정 종가 3,744,000원 — 화면 어느 쪽도 종가가 아니었다.

원인은 둘이다.
  ① api.get_current_price 는 ats_prpr(NXT 체결가)이 있으면 무조건 그것을 돌려준다.
     주문가·손절 트리거는 언제나 실시간가여야 하므로 그 함수는 그대로가 맞다 —
     걸러야 할 것은 **평가액**이다. NXT 거래량은 정규장의 수백분의 1이라, 몇 건의
     장외 체결이 계좌 전체의 평가액·자산곡선·MDD를 정하게 된다.
  ② 주기 스냅샷은 RUNNING 분기에서만 돈다. 15:20 단일가 휴게부터 WAITING이라
     그날 마지막 스냅샷은 15:19 장중가로 굳고, 종가 단일가(15:20~15:30)에서 확정된
     종가는 자산곡선에 영영 들어가지 못한다. 곡선이 유일한 소스인 MDD가 종가 기준이
     아니게 된다.

이 파일은 그 둘을 각각 못박는다.
"""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import config
from modules import db_manager, paper_broker

CODE, NAME = "161890", "한국콜마"
AVG, QTY = 142_420, 10
KRX_CLOSE = 161_800     # 2026-08-28 확정 종가
NXT_LAST = 157_700      # 같은 시각의 마지막 NXT 체결가(평가에 섞이면 안 되는 값)
TODAY = "20260828"


@pytest.fixture
def paper(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "paper_valuation_test.db")
    original_path = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', path, raising=False)
    monkeypatch.setattr(config, 'PAPER_SEED_CAPITAL', 10_000_000, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    db_manager.db.switch_path(path)
    paper_broker.init_tables()
    paper_broker.reset_price_cache()
    db_manager.db.execute_query(
        "INSERT OR REPLACE INTO paper_positions "
        "(code, name, qty, avg_price, first_buy_at, last_buy_at) VALUES (?,?,?,?,?,?)",
        (CODE, NAME, QTY, float(AVG), "2026-08-21 09:00:49", "2026-08-26 09:01:30"))
    yield paper_broker
    paper_broker.reset_price_cache()
    db_manager.db.close_all_connections()
    db_manager.db.switch_path(original_path)


def _bars(last_date=TODAY, close=KRX_CLOSE):
    return pd.DataFrame({"date": ["20260827", last_date],
                         "open": [0.0, 0.0], "high": [0.0, 0.0], "low": [0.0, 0.0],
                         "close": [float(close) - 1000, float(close)],
                         "volume": [0.0, 0.0]})


def _balance(*, regular_hours, settled=TODAY, bars=None, price=NXT_LAST):
    """(요약 dict, 차트조회 mock). regular_hours=True 면 KRX 정규장 중이다."""
    chart = MagicMock(return_value=_bars() if bars is None else bars)
    with patch('api.chart_overlay_enabled', return_value=regular_hours), \
         patch('api.krx_last_settled_day', return_value=settled), \
         patch('api.get_chart_data', chart), \
         patch('api.get_current_price', return_value=price):
        out1, out2 = paper_broker.get_domestic_balance()
    return out1[0], out2[0], chart


# ---------------------------------------------------------------------------
# ① 평가 기준 — 정규장 중에는 현재가, 장 종료 후에는 KRX 확정 종가
# ---------------------------------------------------------------------------
def test_regular_hours_use_the_live_price(paper):
    """정규장 중에는 현재가로 평가한다.

    이 값은 잔고 행의 prpr 로 나가고, 트레이더의 손절·트레일링 판정이 그것을 그대로
    읽는다(_check_sell_conditions: current_price = float(item['prpr'])). 장중에 확정
    종가(=직전 거래일 종가)로 얼려 버리면 손절이 하루 종일 발동하지 않는다.
    """
    row, summary, chart = _balance(regular_hours=True, price=159_000)
    assert int(row['prpr']) == 159_000
    assert int(summary['scts_evlu_amt']) == 159_000 * QTY
    assert not chart.called, "정규장 중에 일봉을 뒤졌다 — 필요 없는 조회다"


def test_after_close_uses_the_krx_settled_close(paper):
    """장 종료 후에는 KRX 확정 종가로 평가한다 — NXT 최종가가 아니다."""
    row, summary, _c = _balance(regular_hours=False)
    assert int(row['prpr']) == KRX_CLOSE
    assert int(summary['scts_evlu_amt']) == KRX_CLOSE * QTY
    assert int(row['prpr']) != NXT_LAST


def test_nxt_sessions_are_also_excluded(paper):
    """NXT 프리(08:00~09:00)·애프터(15:30~20:00)도 '정규장 밖'이다.

    종전 게이트(display_price_krx_fixed)는 20:00 이후·주말에만 걸려, 애프터마켓
    시간대의 평가액은 여전히 NXT 체결가였다. 평가 기준은 설정이 아니라 거래소다.
    """
    for fixed in (True, False):     # USE_KRX_CLOSE_AFTER_HOURS 어느 쪽이든
        with patch('api.display_price_krx_fixed', return_value=fixed):
            row, _s, _c = _balance(regular_hours=False)
        assert int(row['prpr']) == KRX_CLOSE


def test_stale_bar_falls_back_to_the_live_price(paper):
    """당일 봉을 못 받았으면 확정 종가로 쓰지 않는다.

    그대로 쓰면 지난 거래일 종가가 오늘 평가액이 된다. 15:30~15:40(종가 확정 여유)이
    실제로 이 구간이다.
    """
    row, _s, _c = _balance(regular_hours=False, bars=_bars(last_date="20260827"))
    assert int(row['prpr']) == NXT_LAST


def test_settled_close_is_not_stale(paper):
    """확정 종가는 '판정 불가'가 아니다.

    _price_stale 이 서면 트레이더가 그 행을 판정에서 통째로 뺀다 — 마감 후 청산 신호
    스캔이 죽는다. 조회 실패 폴백과 확정 종가는 다른 사건이다.
    """
    row, _s, _c = _balance(regular_hours=False)
    assert row['_price_stale'] is False


def test_settled_close_is_fetched_once_per_session(paper):
    """확정된 봉은 불변이다. 주기마다 일봉을 다시 뒤지면 라즈베리파이에서 그대로 비용이다."""
    chart = MagicMock(return_value=_bars())
    with patch('api.chart_overlay_enabled', return_value=False), \
         patch('api.krx_last_settled_day', return_value=TODAY), \
         patch('api.get_chart_data', chart), \
         patch('api.get_current_price', return_value=NXT_LAST):
        for _ in range(5):
            paper_broker.get_domestic_balance()
    assert chart.call_count == 1


def test_price_lookup_failure_still_falls_back(paper):
    """일봉도 현재가도 없으면 종전 폴백(직전 정상가 → 평단)이 그대로 산다."""
    with patch('api.chart_overlay_enabled', return_value=False), \
         patch('api.krx_last_settled_day', return_value=TODAY), \
         patch('api.get_chart_data', side_effect=RuntimeError("net")), \
         patch('api.get_current_price', return_value=0):
        out1, _out2 = paper_broker.get_domestic_balance()
    assert int(out1[0]['prpr']) == AVG
    assert out1[0]['_price_stale'] is True


# ---------------------------------------------------------------------------
# ② 마감 스냅샷 — 종가가 자산곡선에 들어가는가
# ---------------------------------------------------------------------------
def test_snapshot_reports_whether_it_is_close_based(paper):
    """스냅샷은 '종가로 찍혔는가'를 돌려준다 — 호출부의 재시도 판정 근거다."""
    with patch('api.chart_overlay_enabled', return_value=False), \
         patch('api.krx_last_settled_day', return_value=TODAY), \
         patch('api.get_chart_data', return_value=_bars()), \
         patch('api.get_current_price', return_value=NXT_LAST):
        assert paper_broker.snapshot_equity() is True

    paper_broker.reset_price_cache()
    with patch('api.chart_overlay_enabled', return_value=False), \
         patch('api.krx_last_settled_day', return_value=TODAY), \
         patch('api.get_chart_data', return_value=_bars(last_date="20260827")), \
         patch('api.get_current_price', return_value=NXT_LAST):
        assert paper_broker.snapshot_equity() is False


def test_snapshot_records_the_closing_value(paper):
    """마감 스냅샷이 곡선의 그날 행을 종가로 덮는다."""
    with patch('api.chart_overlay_enabled', return_value=True), \
         patch('api.get_current_price', return_value=159_000):
        paper_broker.snapshot_equity()          # 장중 주기 스냅샷
    with patch('api.chart_overlay_enabled', return_value=False), \
         patch('api.krx_last_settled_day', return_value=TODAY), \
         patch('api.get_chart_data', return_value=_bars()), \
         patch('api.get_current_price', return_value=NXT_LAST):
        paper_broker.snapshot_equity()          # 마감 스냅샷

    curve = paper_broker.get_equity_curve()
    assert len(curve) == 1, "같은 날 두 행이 생겼다 — 곡선의 일수가 부푼다"
    assert curve[-1]['stock_value'] == KRX_CLOSE * QTY


# ---------------------------------------------------------------------------
# ③ MDD — 같은 화면의 총자산은 실시간인데 낙폭만 어제 기준이면 안 된다
# ---------------------------------------------------------------------------
def test_mdd_includes_the_current_value(paper):
    """오늘의 하락은 다음 스냅샷을 기다리지 않고 낙폭에 들어가야 한다."""
    db_manager.db.execute_query(
        "INSERT OR REPLACE INTO paper_equity (date, cash, stock_value, total, seed) "
        "VALUES (?,?,?,?,?)", ("2026-08-28", 10_000_000.0, 2_000_000.0, 12_000_000.0, 1e7))

    with patch('api.chart_overlay_enabled', return_value=False), \
         patch('api.krx_last_settled_day', return_value=TODAY), \
         patch('api.get_chart_data', return_value=_bars(close=120_000)), \
         patch('api.get_current_price', return_value=NXT_LAST):
        perf = paper_broker.get_performance()

    # 곡선 고점 12,000,000 대비 지금(현금 + 120,000×10)까지의 낙폭
    expected = (perf['total'] - 12_000_000.0) / 12_000_000.0 * 100
    assert perf['mdd'] == pytest.approx(expected)
    assert perf['mdd'] < -1.0, "현재값이 낙폭에 반영되지 않았다"


# ---------------------------------------------------------------------------
# ④ 마감 스냅샷 게이트 — 언제 찍고 언제 안 찍는가
# ---------------------------------------------------------------------------
#  [배경] 주기 스냅샷은 RUNNING 분기에만 있다. 15:20 단일가 휴게부터 WAITING이라
#  그날 종가는 곡선에 들어가지 못한다. 이 게이트가 그 공백을 메우는 유일한 경로다.
import modules.auto_trade as at_pkg          # noqa: E402
from modules.auto_trade import AutoTrader    # noqa: E402


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.paper_closing_snapshot_date = None
    return t


def _closing(trader, settled=TODAY, holiday=False, ok=True):
    from datetime import datetime as _dt

    class _Now(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 8, 28, 15, 45)

    with patch.object(at_pkg.trader, 'datetime', _Now), \
         patch('modules.auto_trade.api.is_holiday_today', return_value=holiday), \
         patch('modules.auto_trade.api.krx_last_settled_day', return_value=settled), \
         patch('modules.paper_broker.snapshot_equity', return_value=ok) as snap:
        trader._snapshot_paper_closing_equity()
    return snap


def test_closing_snapshot_waits_for_the_confirmed_close(trader):
    """확정 전(15:30~15:40)에 찍으면 직전 거래일 종가를 오늘 값으로 굳힌다."""
    assert not _closing(trader, settled="20260827").called


def test_closing_snapshot_skips_holidays(trader):
    """휴장일에 행을 만들면 주말이 '변동 없는 거래일'로 곡선에 남는다."""
    assert not _closing(trader, holiday=True).called


def test_closing_snapshot_runs_once_per_trading_day(trader):
    """마감 뒤에도 루프는 계속 돈다. 매 주기 찍으면 잔고·일봉 조회만 낭비된다."""
    assert _closing(trader).called
    assert not _closing(trader).called


def test_closing_snapshot_retries_until_it_is_close_based(trader):
    """한 종목이라도 일봉을 못 받아 실시간가로 폴백했다면 아직 종가 기준이 아니다."""
    assert _closing(trader, ok=False).called          # 찍긴 찍는다(덮어쓰기)
    assert _closing(trader, ok=True).called, "재시도가 막혔다 — 그날은 영영 장중가로 남는다"
    assert not _closing(trader).called                # 성공 후에는 하루 1회
