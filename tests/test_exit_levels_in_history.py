"""매매 기록에 '어디서 끊길 예정이었나'(청산선)를 남기는가.

[왜 필요한가] 히스토리에는 '왜 샀나/왜 팔았나'만 있고 청산선이 없었다. 그런데 이 시스템의
성패는 진입이 아니라 청산선이 정한다(추세추종). 복기할 때 당시 청산선을 모르면 '그 손절이
적절했나'를 판단할 수 없고, **화면은 현재 상태만 보여주므로 청산이 끝난 종목의 값은
어디서도 복원되지 않는다.**

[%에 가격을 붙이는 이유] -7% 만으로는 그 선이 어디였는지 매번 역산해야 한다. 기록은 나중에
읽는 것이라 그 자리에서 계산할 수 없다.
"""
import re

import pytest

import config
from modules.auto_trade import engine


BUY_PRICE = 114000.0
ATR = 4131.0


def _nums(text):
    return re.findall(r'\(([\d,]+)\)', text)


def test_entry_line_has_both_stop_and_trailing():
    out = engine.format_exit_levels(BUY_PRICE, sl_rate=-7.3, label="ATR", atr=ATR)
    assert "ATR:-7.3%" in out and "TS:+" in out, out
    # 세 가격: 손절가 · TS 발동가 · 발동 시 청산선
    assert len(_nums(out)) == 3, out


def test_every_percent_carries_its_price():
    """%만 남기면 나중에 역산해야 한다 — 짝이 없는 %가 있으면 안 된다."""
    out = engine.format_exit_levels(BUY_PRICE, sl_rate=-7.3, label="ATR", atr=ATR)
    # '숫자%' 뒤에는 반드시 '(가격)' 이 온다
    for m in re.finditer(r'[+-][\d.]+%', out):
        tail = out[m.end():m.end() + 1]
        assert tail == '(', f"{m.group()} 뒤에 가격이 없다: {out}"


def test_stop_price_matches_the_rate():
    out = engine.format_exit_levels(BUY_PRICE, sl_rate=-7.3, label="ATR", atr=ATR)
    assert f"{round(BUY_PRICE * (1 - 7.3 / 100)):,}" in out, out


def test_armed_trailing_shows_the_live_line_only():
    """무장 후에는 발동가가 의미 없다 — 실제 청산선 하나만 남는다."""
    ts = engine.compute_trailing_stop(140000, BUY_PRICE, 130000, ind={'atr': ATR})
    assert ts['armed']
    out = engine.format_exit_levels(BUY_PRICE, sl_rate=-7.3, label="ATR",
                                    ts=ts, highest_price=140000)
    assert "TS:-" in out and "TS:+" not in out, out
    assert f"{round(ts['stop_price']):,}" in out, out


def test_matches_the_balance_screen(monkeypatch):
    """같은 값이 두 화면에서 갈리면 어느 쪽을 믿어야 할지 알 수 없다.

    잔고 화면(9-2)의 '청산선' 열과 **같은 숫자**여야 한다. 표시 마크업은 각자 다르지만
    가격은 하나다.
    """
    from modules import account

    ts = engine.compute_trailing_stop(115900, BUY_PRICE, 114500, ind={'atr': ATR})
    res = {'ts': ts, 'applied_sl_rate': -7.3, 'is_atr_stop': True,
           'highest_price': 115900}

    cell = account._fmt_stop_cell(res, BUY_PRICE)
    hist = engine.format_exit_levels(BUY_PRICE, sl_rate=-7.3, label="ATR",
                                     ts=ts, highest_price=115900)

    cell_nums = set(re.findall(r'[\d,]{5,}', cell))
    hist_nums = set(re.findall(r'[\d,]{5,}', hist))
    assert hist_nums and hist_nums <= cell_nums, f"화면과 기록이 갈린다\n{cell}\n{hist}"


def test_overseas_prices_are_dollars():
    out = engine.format_exit_levels(250.5, sl_rate=-8.0, label="ATR", atr=6.0,
                                    is_overseas=True)
    assert "$" in out, out


def test_no_levels_without_a_price():
    assert engine.format_exit_levels(0, sl_rate=-7.0, atr=ATR) == ""


def test_sell_reason_carries_the_levels():
    """모든 매도 사유에 붙어야 한다 — 사유마다 갈리면 복기 기준이 달라진다."""
    import pandas as pd
    strat = engine.DefaultStrategy()
    dates = pd.date_range("2026-07-01", periods=140)
    close = pd.Series(range(140), dtype=float) + 50000.0
    df = pd.DataFrame({'date': dates, 'open': close, 'high': close * 1.01,
                       'low': close * 0.99, 'close': close,
                       'volume': [1_000_000.0] * 140})
    res = strat.analyze_sell("005930", "삼성전자", df, 40000.0, 100000.0, -60.0,
                             highest_price=100000.0)
    assert res['action'] == 'sell', res
    assert "[청산선" in res['reason'], res['reason']
