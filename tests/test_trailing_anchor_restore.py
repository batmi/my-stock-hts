"""트레일링 앵커(최고가)는 **진입일 이후 봉 고가**여야 한다.

[사고 · 2026-08-24] 102780(KODEX 삼성그룹)을 2026-04-10에 진입해 136일을 들고 있었다.
 그 사이 실제 고가는 36,360원(6/19)이다. 그런데 이날 메뉴에서 **1주를 추가 매수**하자
 9-2 잔고의 최고가가 그 체결가인 25,500원으로 내려앉았다. 보유일은 136일 그대로였다.

 원인은 두 겹이다.
   ① 매수 주문 경로(trading.py · engine.OrderManager)가 체결 직후
      `update_highest_price(code, 체결가)`로 앵커를 심는다. 신규 진입에는 맞지만,
      **이미 몇 달 들고 있던 포지션**에도 똑같이 심는다.
   ② 그 앵커를 받는 쪽이 '기록이 **없을 때만**' 봉에서 유도했다
      (`elif highest_price <= 0 and entry_date`). ①이 기록을 만들어 버리므로
      유도 분기는 그 뒤로 영영 열리지 않는다.

 결과는 표시 문제로 끝나지 않는다. MFE가 +78% → +24.9%로 줄고, 고점 대비 30% 반납이
 TS 판정에서 통째로 사라진다 — **청산 신호가 조용히 지워진다.**

[정의는 백테스트가 이미 갖고 있었다] portfolio_backtest는 진입 봉의 고가에서 시작해
 봉 고가의 러닝맥스를 쓴다(`pos["high"] = max(pos["high"], row["high"])`).
 실매매만 '봇이 보고 있는 동안의 현재가'를 쌓고 있었다. 그래서 앵커는
 재기동 구간·HTS 직접 매수 구간·추가 매수 시점에서 실제 고점보다 낮게 남았다.
 이 파일은 실매매를 그 정의로 되돌린 것을 고정한다.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from modules import db_manager
from modules.auto_trade import AutoTrader

CODE, NAME = "102780", "KODEX 삼성그룹"
#  2절(실매매 배선)은 다른 종목으로 잰다 — 사고 종목은 ETF라 자동매매가 아예 건너뛴다
#  ("[매도스킵] ETF 제외 설정"). 앵커 복원은 종목 종류와 무관한 문제이므로 감시 대상
#  종목에서 확인해야 배선이 실제로 도는지 알 수 있다.
LIVE_CODE, LIVE_NAME = "005930", "삼성전자"

BUY_PRICE = 20_424.0      # 실제 사고의 평단
NOW_PRICE = 25_465.0
PEAK_HIGH = 36_360.0      # 진입 후 실제 고가 (6/19)
PLANTED = 25_500.0        # 추가 매수 체결가 — 이것이 앵커로 심겼다
HOLDING_DAYS = 136


def _bars(peak=PEAK_HIGH, days=HOLDING_DAYS + 30, current=NOW_PRICE):
    """진입 훨씬 전부터 오늘까지의 일봉. 보유 구간 한가운데에 고점을 둔다."""
    close = np.linspace(BUY_PRICE, current, days)
    high = close * 1.001
    high[days - HOLDING_DAYS // 2] = peak          # 보유 구간 안의 고점
    return pd.DataFrame({
        "date": pd.date_range(end=pd.Timestamp.today().normalize(), periods=days),
        "open": close, "high": high, "low": close * 0.99, "close": close,
        "volume": np.full(days, 500_000.0),
    })


def _entry_date():
    return (datetime.now() - timedelta(days=HOLDING_DAYS)).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════
# 1. 잔고 화면(메뉴 9-2) — analyze_holdings
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def _no_db():
    """보유 분석이 읽는 DB 배치 조회를 빈 값으로 고정한다."""
    targets = {
        "get_all_stock_strategies": [],
        "get_latest_buy_trades": {},
        "get_buy_trades_for_current_holdings": {},
        "get_all_trailing_stops": {},
        "get_all_half_tp": set(),
    }
    patchers = [patch(f"modules.db_manager.db.{k}", return_value=v) for k, v in targets.items()]
    patchers.append(patch("api.get_period_entry_dates", return_value={}))
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()


def _analyze(anchor, entry_date, df=None):
    from modules import auto_trade

    entries = [{"code": CODE, "name": NAME, "buy_price": BUY_PRICE,
                "current_price": NOW_PRICE, "profit_rate": 24.7, "is_overseas": False}]
    broker = {CODE: entry_date.replace("-", "")} if entry_date else {}

    with patch("api.get_period_entry_dates", return_value=broker), \
         patch("modules.db_manager.db.get_all_trailing_stops",
               return_value=({CODE: anchor} if anchor else {})), \
         patch("api.get_chart_data", return_value=(df if df is not None else _bars())), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)):
        return auto_trade.analyze_holdings(entries)[CODE]


def test_추가매수가_심은_앵커가_진입후_고가를_덮지_않는다(_no_db):
    """사고 그대로의 배치 — 기록된 앵커(체결가)보다 봉 고가가 높으면 봉 고가를 쓴다."""
    res = _analyze(anchor=PLANTED, entry_date=_entry_date())

    assert res["highest_price"] == pytest.approx(PEAK_HIGH), (
        f"최고가가 {res['highest_price']:,.0f}원 — 추가 매수 체결가({PLANTED:,.0f})가 "
        "진입 후 실제 고가를 덮었다. TS의 반납폭이 통째로 사라진다")
    assert res["max_profit_rate"] > 70, "MFE가 앵커와 함께 무너졌다"


def test_보유일수는_추가매수로_리셋되지_않는다(_no_db):
    """대조 — 진입일(수량 0→1 시점) 기준이라 1주 더 담아도 그대로다."""
    assert _analyze(anchor=PLANTED, entry_date=_entry_date())["holding_days"] == HOLDING_DAYS


def test_기록된_앵커가_더_높으면_그것을_쓴다(_no_db):
    """장중 틱 고점은 일봉 고가보다 높을 수 있다. 유도는 **덮어쓰기가 아니라 최댓값**이다."""
    higher = PEAK_HIGH + 1_000
    assert _analyze(anchor=higher, entry_date=_entry_date())["highest_price"] \
        == pytest.approx(higher)


def test_진입일을_모르면_기록된_앵커를_그대로_쓴다(_no_db):
    """대조 — 유도할 기준일이 없으면 종전대로 기록만 본다(없는 근거를 지어내지 않는다)."""
    assert _analyze(anchor=PLANTED, entry_date=None)["highest_price"] == pytest.approx(PLANTED)


# ═══════════════════════════════════════════════════════════════════
# 2. 실매매 — 판정에 넘어가는 값과 DB에 남는 값
# ═══════════════════════════════════════════════════════════════════
#  [왜 배선까지 보는가] 위 1절은 표시 경로다. 그것만 고치면 화면과 실제 청산 판정이
#  갈린다 — 사람이 보는 최고가와 봇이 쓰는 앵커가 다른, 더 나쁜 상태가 된다.

@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    db_manager.db.delete_trailing_stop(LIVE_CODE)
    yield t
    db_manager.db.delete_trailing_stop(LIVE_CODE)


def _run_sell_cycle(trader, df):
    holdings = [{'pdno': LIVE_CODE, 'prdt_name': LIVE_NAME, 'hldg_qty': '229', 'ord_psbl_qty': '229',
                 'pchs_avg_pric': str(BUY_PRICE), 'pchs_amt': str(int(BUY_PRICE * 229)),
                 'prpr': str(NOW_PRICE), 'evlu_amt': str(int(NOW_PRICE * 229)),
                 'evlu_pfls_amt': '1154285', 'evlu_pfls_rt': '24.67'}]
    trader.is_running = True
    trader.market_index_status = {}
    trader.market_status_notified = {}

    with patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.load_restricted_stocks', return_value={}), \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=229), \
         patch('modules.auto_trade.api.get_chart_data', return_value=df), \
         patch('modules.db_manager.db.get_position_entry_dates',
               return_value={LIVE_CODE: _entry_date()}), \
         patch('modules.auto_trade.DefaultStrategy.analyze_sell') as mock_analyze, \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value='1'):
        mock_analyze.return_value = {'action': 'hold', 'reason': '', 'score': 5.0,
                                     'state': '보유', 'ind': {'rsi': 50, 'adx': 20, 'cci': 0}}
        trader._check_sell_conditions(holdings, is_market_open=True)

    assert mock_analyze.called, "매도 판정 자체가 돌지 않았다 — 하네스 전제가 깨졌다"
    return mock_analyze.call_args.kwargs.get('highest_price')


def test_매도_판정이_받는_앵커는_진입후_고가다(trader):
    db_manager.db.update_highest_price(LIVE_CODE, PLANTED)      # 추가 매수가 심은 앵커

    passed = _run_sell_cycle(trader, _bars())

    assert passed == pytest.approx(PEAK_HIGH), (
        f"판정에 {passed}가 넘어갔다 — 화면만 고치고 실제 청산 판정은 옛 앵커를 쓴다")


def test_복원된_앵커는_DB에도_남는다(trader):
    """다음 주기·재기동·오픈리스크 산출이 같은 값을 보게 해야 한다."""
    db_manager.db.update_highest_price(LIVE_CODE, PLANTED)

    _run_sell_cycle(trader, _bars())

    assert db_manager.db.get_highest_price(LIVE_CODE) == pytest.approx(PEAK_HIGH)


def test_봉_고가가_낮으면_기록을_내리지_않는다(trader):
    """대조 — 앵커는 단조 증가다. 유도값이 낮다고 앵커가 내려가면 청산선이 헐거워진다."""
    db_manager.db.update_highest_price(LIVE_CODE, 40_000)

    passed = _run_sell_cycle(trader, _bars())

    assert passed == pytest.approx(40_000)
    assert db_manager.db.get_highest_price(LIVE_CODE) == pytest.approx(40_000)
