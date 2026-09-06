"""지수 시세는 '모르는 전일대비'를 0.00%로 말하지 않는다 (감사 2026-09-06, 배치 56).

market.fetch_index_quote 는 텔레그램 지수 요약이 쓰는 경량 시세다. 두 구멍이 있었다:

 1. **NaN 이 `is not None` 을 통과한다.** 지수 최후 폴백(yfinance)이 최신 종가를 결측으로
    주는 일이 잦다는 사실은 이 저장소가 이미 적어 둔 것이다(시장 필터 주석). 그 NaN 이
    그대로 흘러 'nan (nan%)' 가 찍히거나 등락 계산이 무의미해졌다.
 2. **봉이 하나면 `prev = current`.** 등락률이 0.00% 가 되어 화면에는 '변동 없음'으로
    나온다 — 모르는 것을 아는 것처럼 말하는 자리다([[unknown-vs-empty]]).
"""
import pandas as pd
import pytest

import config
from modules import market


def _df(*closes):
    return pd.DataFrame({'close': list(closes)})


@pytest.fixture
def kospi(monkeypatch):
    """국내 지수 경로 하나만 남긴다."""
    name = next(iter(market.DOMESTIC_INDEX_SOURCE_MAP))
    yield name


def test_봉이_하나면_전일값은_모른다(kospi, monkeypatch):
    """0.00%('변동 없음')로 채우지 않는다."""
    monkeypatch.setattr(market.analysis, 'get_domestic_index_data', lambda src: _df(2500.0))
    _, current, prev = market.fetch_index_quote(kospi, "X")
    assert current == 2500.0
    assert prev is None, "전일값을 모르는데 현재값으로 채웠다 — 등락이 0.00%가 된다"


def test_두_봉이면_전일값을_그대로_쓴다(kospi, monkeypatch):
    monkeypatch.setattr(market.analysis, 'get_domestic_index_data', lambda src: _df(2450.0, 2500.0))
    _, current, prev = market.fetch_index_quote(kospi, "X")
    assert (current, prev) == (2500.0, 2450.0)


def test_마지막_종가가_결측이면_값을_내지_않는다(kospi, monkeypatch):
    monkeypatch.setattr(market.analysis, 'get_domestic_index_data',
                        lambda src: _df(2450.0, float('nan')))
    _, current, prev = market.fetch_index_quote(kospi, "X")
    assert current is None and prev is None


def test_전일_종가만_결측이면_현재값은_살린다(kospi, monkeypatch):
    """현재값은 멀쩡하다 — 등락만 모른다."""
    monkeypatch.setattr(market.analysis, 'get_domestic_index_data',
                        lambda src: _df(float('nan'), 2500.0))
    _, current, prev = market.fetch_index_quote(kospi, "X")
    assert current == 2500.0 and prev is None


@pytest.mark.parametrize("bogus", [None, float('nan'), "", "abc"])
def test_usable_은_쓸_수_없는_값을_거른다(bogus):
    assert market._usable(bogus) is None


def test_텔레그램_요약은_모르는_등락을_0퍼센트로_찍지_않는다(monkeypatch):
    """이 문구는 운영자의 휴대폰으로 나간다 — '변동 없음'과 '모름'은 다르다."""
    from modules import telegram_bot

    bot = telegram_bot.TelegramCommander.__new__(telegram_bot.TelegramCommander)
    monkeypatch.setattr(telegram_bot.market, 'fetch_index_quote',
                        lambda name, code: (name, 2500.0, None))
    monkeypatch.setattr(telegram_bot.market, 'is_market_open_for_index', lambda name: True)
    monkeypatch.setattr(telegram_bot.market, 'blocked_kis_only_indices', lambda: set())

    msg = bot._get_market_status()

    assert "0.00%" not in msg, f"모르는 등락이 '변동 없음'으로 찍혔다:\n{msg}"
    assert "전일대비 모름" in msg


def test_텔레그램_요약은_정상_등락을_그대로_찍는다(monkeypatch):
    from modules import telegram_bot

    bot = telegram_bot.TelegramCommander.__new__(telegram_bot.TelegramCommander)
    monkeypatch.setattr(telegram_bot.market, 'fetch_index_quote',
                        lambda name, code: (name, 2550.0, 2500.0))
    monkeypatch.setattr(telegram_bot.market, 'is_market_open_for_index', lambda name: True)
    monkeypatch.setattr(telegram_bot.market, 'blocked_kis_only_indices', lambda: set())

    msg = bot._get_market_status()
    assert "+2.00%" in msg
