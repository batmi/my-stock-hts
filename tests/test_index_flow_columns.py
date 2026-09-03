"""지수 기간별 시세 표의 '공매도'·'수급(개/외/기)' 컬럼.

[왜 종목 표와 나란히 두면 안 되는가] 이름은 같지만 **단위가 다르다.**
  · 지수 수급  = 순매수 **대금(원)**   / 종목 수급  = 순매수 **주식 수**
  · 지수 공매도 = **거래대금** 비중     / 종목 공매도 = **거래량** 비중
지수에는 상장주식수가 없어 애초에 주식 수 기준 집계가 존재하지 않는다. 그래서 헤더에
단위를 적어 두고, 같은 이유로 '외인률'(외국인보유주식수/상장주식수)은 지수에 **없다**.
"""
import io
from unittest.mock import patch

import pandas as pd
import pytest

import config
from modules import analysis


def _price_df():
    dates = pd.date_range("2026-08-01", periods=40).strftime("%Y%m%d")
    base = pd.Series(range(40), dtype=float) + 2500.0
    return pd.DataFrame({'date': dates, 'open': base, 'high': base + 5,
                         'low': base - 5, 'close': base, 'volume': 1_000_000.0})


def _flow_df():
    dates = pd.date_range("2026-08-01", periods=40).strftime("%Y%m%d")
    return pd.DataFrame({
        'date': dates,
        'indi': [-955168464632.0] * 40,
        'frgn': [41438637419.0] * 40,
        'inst': [914345120731.0] * 40,
        'short_ratio': [7.823999] * 40,
    })


def _render(code, flow, width=200):
    """표를 문자열로 받는다."""
    from rich.console import Console
    buf = io.StringIO()
    saved = config.console
    config.console = Console(file=buf, width=width, force_terminal=False, no_color=True)
    try:
        with patch.object(analysis, 'get_domestic_index_data', return_value=_price_df()), \
             patch('modules.krx_data.get_market_flow_daily', return_value=flow):
            analysis._print_period_price_common(code, False, limit=5)
    finally:
        config.console = saved
    return buf.getvalue()


def test_index_table_shows_flow_columns():
    out = _render("KOSPI", _flow_df())
    assert "공매도" in out and "수급(개/외/기)" in out
    assert "7.82%" in out, "공매도 비중이 표에 없다"
    # 억/조 단위가 K·M 으로 뭉개지면 시장 수급을 읽을 수 없다.
    assert "-955.2B" in out and "+914.3B" in out, out


def test_index_table_never_shows_foreign_ownership():
    """지수는 상장주식수가 없어 외인률이 정의되지 않는다 — 있으면 지어낸 값이다."""
    out = _render("KOSPI", _flow_df())
    assert "외인률" not in out


def test_index_table_unchanged_when_source_unavailable():
    """KRX 자격증명이 없으면 컬럼을 아예 붙이지 않는다 — 빈 칸만 늘리지 않는다."""
    out = _render("KOSPI", None)
    assert "공매도" not in out and "수급" not in out
    assert "OBV" in out, "표 자체는 종전대로 나와야 한다"


def test_index_table_width_stays_within_the_stock_table():
    """같은 가족의 표보다 넓어지면 안 된다(종목 기간별 시세 = 158열)."""
    out = _render("KOSPI", _flow_df())
    assert max(len(l.rstrip()) for l in out.splitlines()) <= 158


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
