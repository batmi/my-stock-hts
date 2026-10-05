"""지수 기간별 시세 표의 수급 계열 컬럼.

[2026-10-05] 지수의 '공매도'·'수급(개/외/기)'(시장 단위 순매수 대금·공매도 거래대금 비중)은
data.krx.co.kr 웹 로그인 스크래핑(pykrx)으로만 받았다. 약관 위반으로 2026-09-18 꺼졌고 pykrx 와
함께 컬럼 코드도 지웠다 — KRX Open API 에는 대응 서비스가 없다. 그래서 지수 표에는 이 계열
컬럼이 없어야 하고, '외인률'(외국인보유주식수/상장주식수)은 지수에 원래 정의되지 않는다.
"""
import io
from unittest.mock import patch

import pandas as pd

import config
from modules import analysis


def _price_df():
    dates = pd.date_range("2026-08-01", periods=40).strftime("%Y%m%d")
    base = pd.Series(range(40), dtype=float) + 2500.0
    return pd.DataFrame({'date': dates, 'open': base, 'high': base + 5,
                         'low': base - 5, 'close': base, 'volume': 1_000_000.0})


def _render(code, width=200):
    """표를 문자열로 받는다."""
    from rich.console import Console
    buf = io.StringIO()
    saved = config.console
    config.console = Console(file=buf, width=width, force_terminal=False, no_color=True)
    try:
        with patch.object(analysis, 'get_domestic_index_data', return_value=_price_df()):
            analysis._print_period_price_common(code, False, limit=5)
    finally:
        config.console = saved
    return buf.getvalue()


def test_index_table_has_no_flow_or_ownership_columns():
    """원천이 없는 컬럼을 빈 칸으로 늘리지 않는다 — 지수 표는 OBV 까지다."""
    out = _render("KOSPI")
    assert "공매도" not in out and "수급" not in out and "외인률" not in out
    assert "OBV" in out, "표 자체는 종전대로 나와야 한다"


def test_stock_flow_keeps_billions():
    """종목 수급 포맷의 B 분기가 M 에 덮여 있었다(if 두 번 → elif).

    10억 주 이상은 드물지만 있고, 그때 '1,200.0M' 처럼 자릿수를 잘못 읽게 된다.
    지수 표를 붙이며 같은 함수 계열을 손댔으므로 원래 자리도 못을 박아 둔다.
    """
    big = 1_200_000_000     # 12억 주
    trend = [{'stck_bsop_date': d, 'prsn_ntby_qty': str(big),
              'frgn_ntby_qty': '-100', 'orgn_ntby_qty': '100'}
             for d in _price_df()['date']]

    from rich.console import Console
    buf = io.StringIO()
    saved = config.console
    config.console = Console(file=buf, width=250, force_terminal=False, no_color=True)
    try:
        with patch.object(analysis.api, 'get_chart_data', return_value=_price_df()), \
             patch.object(analysis.api, 'get_investor_trend', return_value=trend), \
             patch.object(analysis.api, 'get_daily_short_selling', return_value=[]), \
             patch.object(analysis.api, 'get_daily_foreign_rate', return_value=[]), \
             patch.object(analysis.api, 'get_current_price_data',
                          return_value={'rt_cd': '1', 'output': {}}):
            analysis._print_period_price_common("005930", False, limit=3)
    finally:
        config.console = saved

    out = buf.getvalue()
    assert "1.2B" in out, f"10억 주 이상이 B 로 표기되지 않았다:\n{out}"
