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

import config
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


# ═══════════════════════════════════════════════════════════════════
# 3. 일봉 조회창(250봉 ≈ 14개월) 밖의 보유 구간
# ═══════════════════════════════════════════════════════════════════
#  [왜] 일봉 경로는 CHART_LOOKBACK_DAYS(730일)로 요청해 놓고 마지막에 `.tail(250)`으로
#  자른다(api/charts.py). 그보다 오래 들고 있는 포지션은 **진입 직후 구간이 df에 없어**
#  봉에서 되짚어도 그 시기 고점을 못 찾는다. 봇이 감시하는 종목은 첫날부터 앵커를 쌓아
#  DB가 기억하지만, 봇이 보지 않는 포지션(ETF·수동 매수)에는 그 기억이 없다.

DEEP_PEAK = 52_000.0      # 일봉 창보다 이전(진입 직후)의 고점 — 주봉에만 남아 있다


@pytest.fixture(autouse=True)
def _clear_deep_cache():
    """주봉 보강 메모는 프로세스 수명이라 테스트 간에 새어 나간다."""
    from modules.auto_trade import engine
    engine._DEEP_ANCHOR_CACHE.clear()
    yield
    engine._DEEP_ANCHOR_CACHE.clear()


def _window_bars(bars=250):
    """오늘부터 거슬러 250봉만 있는 일봉 — 실제 조회 결과의 모양."""
    close = np.linspace(BUY_PRICE, NOW_PRICE, bars)
    high = close * 1.001
    return pd.DataFrame({
        "date": pd.date_range(end=pd.Timestamp.today().normalize(), periods=bars),
        "open": close, "high": high, "low": close * 0.99, "close": close,
        "volume": np.full(bars, 500_000.0),
    })


def _weekly_bars(entry, peak=DEEP_PEAK, peak_offset_days=60):
    """진입일 전후를 모두 덮는 주봉. 고점은 진입 이후·일봉 창 이전에 둔다."""
    start = pd.Timestamp(entry) - pd.Timedelta(days=180)
    dates = pd.date_range(start=start, end=pd.Timestamp.today().normalize(), freq="7D")
    close = np.full(len(dates), BUY_PRICE)
    high = close * 1.001
    df = pd.DataFrame({"date": dates, "open": close, "high": high,
                       "low": close * 0.99, "close": close,
                       "volume": np.full(len(dates), 500_000.0)})
    target = pd.Timestamp(entry) + pd.Timedelta(days=peak_offset_days)
    idx = (df["date"] - target).abs().idxmin()
    df.loc[idx, "high"] = peak
    return df


def _deep_entry():
    """일봉 250봉 창보다 확실히 오래된 진입일(약 2년 전)."""
    return (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")


def _anchor(entry, daily, weekly):
    from modules.auto_trade import engine

    def _chart(code, is_overseas=False, period_type='daily', **kw):
        return weekly if period_type == 'weekly' else daily

    with patch("api.get_chart_data", side_effect=_chart) as m:
        return engine.anchor_high_since(CODE, daily, entry), m


def test_일봉_창_밖의_고점을_주봉으로_되짚는다():
    """사고의 일반형 — 2년 들고 있으면 진입 직후 고점이 일봉 df에 아예 없다."""
    entry = _deep_entry()
    val, _ = _anchor(entry, _window_bars(), _weekly_bars(entry))

    assert val == pytest.approx(DEEP_PEAK), (
        f"앵커가 {val:,.0f} — 일봉 창(250봉) 안만 봐서 진입 직후 고점을 놓쳤다")


def test_일봉_창이_진입일을_덮으면_주봉을_조회하지_않는다():
    """대조 — 절대다수 경로다. 여기서 주봉을 집으면 매 주기 헛호출이 붙는다."""
    entry = _entry_date()                      # 136일 전 — 창 안
    val, mock = _anchor(entry, _bars(), _weekly_bars(entry))

    assert val == pytest.approx(PEAK_HIGH)
    assert not any(c.kwargs.get('period_type') == 'weekly' for c in mock.call_args_list), \
        "일봉으로 충분한데 주봉까지 조회했다"


def test_진입일에_걸친_주는_버린다():
    """주봉 라벨이 주 시작일인지 마감일인지는 소스마다 다르다. 경계 주를 쓰면
    **진입 전 고가**가 앵커로 섞여 들어와 청산선이 위로 올라간다(오청산)."""
    entry = _deep_entry()
    weekly = _weekly_bars(entry, peak=99_000.0, peak_offset_days=0)   # 진입 주에 고점
    val, _ = _anchor(entry, _window_bars(), weekly)

    assert val != pytest.approx(99_000.0), "진입일에 걸친 주의 고가를 앵커로 썼다"


def test_주봉_보강은_종목당_한_번만_조회한다():
    """보유 종목은 매 주기 돈다. 메모가 없으면 창 밖 포지션마다 주봉 호출이 반복된다."""
    entry = _deep_entry()
    daily, weekly = _window_bars(), _weekly_bars(entry)

    def _chart(code, is_overseas=False, period_type='daily', **kw):
        return weekly if period_type == 'weekly' else daily

    from modules.auto_trade import engine
    with patch("api.get_chart_data", side_effect=_chart) as m:
        for _ in range(3):
            engine.anchor_high_since(CODE, daily, entry)
        weekly_calls = [c for c in m.call_args_list if c.kwargs.get('period_type') == 'weekly']

    assert len(weekly_calls) == 1, f"주봉을 {len(weekly_calls)}번 조회했다"


def test_주봉_조회가_실패해도_일봉_답을_돌려준다():
    """fail-safe — 보강에 실패했다고 앵커가 사라지면 TS가 통째로 빠진다."""
    from modules.auto_trade import engine
    entry = _deep_entry()
    daily = _window_bars()

    with patch("api.get_chart_data", side_effect=RuntimeError("weekly down")):
        val = engine.anchor_high_since(CODE, daily, entry)

    assert val == pytest.approx(float(daily["high"].max()))


# ═══════════════════════════════════════════════════════════════════
# 4. 자동 매도에서 제외된 ETF도 앵커는 남는다
# ═══════════════════════════════════════════════════════════════════
#  [왜] ETF는 SYSTEM_INCLUDE_ETF=False면 매도 판정 루프에 닿기 전에 빠져나간다. 그래서
#  102780의 trailing_stops 에는 마지막 매수 체결가(25,500)가 그대로 굳어 있었다. 화면은
#  매번 봉에서 되짚어 맞지만, DB만 보는 도구·감사와 ETF 포함을 켠 순간의 청산선은 틀린다.


class _MidSession(datetime):
    """장중(10:00)으로 고정한다. NXT 운영시간(15:30~20:00·08:00~08:50)에 테스트를 돌리면
    ETF가 그 분기에서 먼저 빠져나가 벽시계에 따라 결과가 갈린다."""
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture
def etf_trader():
    AutoTrader._instance = None
    t = AutoTrader()
    db_manager.db.delete_trailing_stop(CODE)
    yield t
    db_manager.db.delete_trailing_stop(CODE)


def _run_etf_cycle(trader, df, clock=None, nxt=False):
    """ETF 보유 1주기.

    [벽시계를 끊는다 · 2026-09-05] `api.nxt_order_window()` 는 **자기 datetime** 을 쓴다 —
     아래 `_MidSession`/`_NxtSession` 이 갈아끼우는 `trader.datetime` 이 아니다. 그래서
     종전에는 스위트를 15:30~20:00(또는 08:00~08:50)에 돌리면 ETF 테스트가 통째로 NXT
     분기를 탔고, `_NxtSession` 을 쓰는 테스트는 반대로 그 시간대 **밖에서는 NXT 분기를
     한 번도 밟지 않은 채 통과**했다(두 분기 모두 앵커를 복원하므로 단언은 성립한다).
     즉 그 테스트는 낮에는 스스로 무의미해진다. 분기를 인자로 못박는다.
    """
    holdings = [{'pdno': CODE, 'prdt_name': NAME, 'hldg_qty': '229', 'ord_psbl_qty': '229',
                 'pchs_avg_pric': str(BUY_PRICE), 'pchs_amt': str(int(BUY_PRICE * 229)),
                 'prpr': str(NOW_PRICE), 'evlu_amt': str(int(NOW_PRICE * 229)),
                 'evlu_pfls_amt': '1154285', 'evlu_pfls_rt': '24.67'}]
    trader.is_running = True
    trader.market_index_status = {}
    trader.market_status_notified = {}

    with patch('modules.auto_trade.trader.datetime', clock or _MidSession), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.load_restricted_stocks', return_value={}), \
         patch('modules.auto_trade.api.is_domestic_etf_etn', return_value=True), \
         patch('modules.auto_trade.api.nxt_order_window', return_value=nxt), \
         patch.object(config, 'SYSTEM_INCLUDE_ETF', False), \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=229), \
         patch('modules.auto_trade.api.get_chart_data', return_value=df) as chart, \
         patch('modules.db_manager.db.get_position_entry_dates',
               return_value={CODE: _entry_date()}), \
         patch('modules.auto_trade.DefaultStrategy.analyze_sell') as mock_analyze, \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value='1'):
        trader._check_sell_conditions(holdings, is_market_open=True)

    return mock_analyze, chart


def test_ETF는_매도판정에서_빠지되_앵커는_복원된다(etf_trader):
    db_manager.db.update_highest_price(CODE, PLANTED)      # 추가 매수가 심은 앵커

    mock_analyze, chart = _run_etf_cycle(etf_trader, _bars())

    assert not mock_analyze.called, "ETF가 자동 매도 판정을 탔다 — 제외 의도가 깨졌다"
    assert chart.called, "차트를 집지 않았다 — ETF 분기에 닿기 전에 빠졌다(하네스 전제 붕괴)"
    assert db_manager.db.get_highest_price(CODE) == pytest.approx(PEAK_HIGH), (
        "ETF의 앵커가 매수 체결가에 굳은 채 남았다")


def test_ETF_앵커_복원은_주기마다_차트를_다시_집지_않는다(etf_trader):
    """라즈베리파이 보호 — 앵커는 일봉 고가라 주기(수십 초)마다 볼 이유가 없다.

    [플래키 진단 · 2026-09-06] 이 테스트는 전체 스위트에서 드물게(실측 약 25회 중 3회)
     실패하는데 단독·반복 실행으로는 재현되지 않았다. 어느 단언이 어떤 상태에서 깨지는지
     기록이 남지 않아 추적이 막혔으므로, 실패 메시지가 스스로 원인을 말하게 한다:
       · 스로틀 dict 가 첫 주기에 찍혔는가(= _restore_trailing_anchor 에 닿았는가)
       · 두 번째 주기의 차트 호출이 **어디서** 났는가(인자로 경로가 갈린다)
     첫 단언이 깨지면 1주기가 앵커 복원 전에 빠져나간 것이고, 둘째가 깨지면 ETF 분기가
     아닌 일반 매도 경로(trader 의 df 조회)로 흘러간 것이다.
    """
    db_manager.db.update_highest_price(CODE, PLANTED)

    _, first = _run_etf_cycle(etf_trader, _bars())
    throttle_after_first = dict(getattr(etf_trader, '_anchor_restore_at', {}))
    _, chart = _run_etf_cycle(etf_trader, _bars())

    assert first.called, (
        "첫 주기부터 차트를 집지 않았다 — 하네스 전제가 깨졌다. "
        f"스로틀={throttle_after_first} (비어 있으면 앵커 복원에 닿기 전에 빠져나갔다)")
    assert not chart.called, (
        "두 번째 주기에도 차트를 조회했다 — 스로틀이 걸리지 않았다. "
        f"스로틀={throttle_after_first} 호출={chart.call_args_list}")



class _NxtSession(datetime):
    """NXT 운영시간(16:00)으로 고정한다."""
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz).replace(hour=16, minute=0, second=0, microsecond=0)


def test_NXT_시간대의_ETF도_앵커는_복원된다(etf_trader):
    """NXT 분기(15:30~20:00·08:00~08:50)는 ETF 제외 분기보다 **먼저** 걸린다. 거기서 그냥
    돌아서면 장 마감 후에만 봇을 켜는 운용에서 앵커가 영영 복원되지 않는다.

    [2026-09-05] `nxt=True` 를 명시한다. 종전에는 `_NxtSession` 만 갈아끼웠는데 그 시계는
     `api.nxt_order_window` 에 닿지 않아, 낮에 돌리면 이 테스트가 NXT 분기를 밟지 못한
     채로 통과했다 — 지키려던 회귀를 지키지 않는 상태로 오래 서 있었다.
    """
    db_manager.db.update_highest_price(CODE, PLANTED)

    mock_analyze, chart = _run_etf_cycle(etf_trader, _bars(), clock=_NxtSession, nxt=True)

    assert not mock_analyze.called, "NXT 시간대에 ETF가 매도 판정을 탔다"
    assert chart.called, "차트를 집지 않았다 — NXT 분기에 닿기 전에 빠졌다(하네스 전제 붕괴)"
    assert db_manager.db.get_highest_price(CODE) == pytest.approx(PEAK_HIGH)


def test_그_분기가_실제로_NXT_분기인지_못박는다():
    """`nxt=True` 하네스가 진짜 NXT 분기를 태우는지 — 안 그러면 위 테스트는 ETF 분기의 사본이다."""
    import inspect
    from modules.auto_trade import trader as _t

    src = inspect.getsource(_t.AutoTrader._check_sell_conditions)
    i_nxt = src.index("is_nxt_market = api.nxt_order_window()")
    i_etf = src.index("if is_domestic_etf and not getattr(config, 'SYSTEM_INCLUDE_ETF'")
    assert i_nxt < i_etf, "NXT 분기가 ETF 제외 분기보다 먼저 걸린다는 전제가 깨졌다"
    assert "api.nxt_order_window()" in src, (
        "분기 조건이 바뀌었다 — 하네스의 nxt 인자가 더는 그 분기를 고르지 못한다")
