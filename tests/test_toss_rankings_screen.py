"""[6-9] 투자자 순매수 · 섹터 순위 화면(토스 1.2.28) — 무엇을 보여 주고 무엇을 '모른다'고 말하는가.

[여기서 고정하는 것]
  ① 투자자마다 기준 시각(rankedAt)이 다르다 — 표마다 적는다. 개인은 장 마감 전 직전 영업일 값이다.
  ② 응답을 해석하지 못하면 '순위 없음'이 아니라 '받지 못했다'고 말한다([[unknown-vs-empty]]).
  ③ 표 폭은 2-1 종목 시세 표(135열)보다 좁다([[terminal-table-width]]) — 금액이 '…'로 잘리면 표가 쓸모없다.
  ④ 토스 모드가 아니면 메뉴는 회색(선택 불가)이다([[menu-disabled-greyed]]).
"""
import os
import re
import sys
from unittest.mock import patch

from rich.console import Console

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from brokers import toss_api  # noqa: E402
from modules.manage import toss_rankings as TR  # noqa: E402


def _ranking_payload():
    def blk(ts, buys, sells):
        return {"rankedAt": ts,
                "netBuyRankings": [{"rank": i + 1, "symbol": s, "currency": "KRW", "netBuyAmount": a}
                                   for i, (s, a) in enumerate(buys)],
                "netSellRankings": [{"rank": i + 1, "symbol": s, "currency": "KRW", "netSellAmount": a}
                                    for i, (s, a) in enumerate(sells)]}
    return {
        "foreigner": blk("2026-10-20T15:41:19+09:00",
                         [("005930", "386487682000"), ("402340", "43100000000")],
                         [("000660", "391201805000")]),
        "institution": {"rankedAt": None, "netBuyRankings": [], "netSellRankings": []},
        "individual": blk("2026-10-19T20:17:53+09:00", [("122630", "86500000000")],
                          [("005930", "120400000000")]),
    }


def _render(fn, *a, **k):
    con = Console(record=True, width=135, force_terminal=False, color_system=None)
    with patch.object(config, "console", con), \
         patch.object(TR, "_names", lambda syms: {"005930": "삼성전자", "000660": "SK하이닉스"}), \
         patch.object(TR, "_my_codes", lambda: ({"000660"}, {"005930"})):
        ok = fn(*a, **k)
    return ok, con.export_text()


def test_투자자마다_기준_시각을_적는다():
    with patch.object(toss_api, "get_investor_trading_rankings", return_value=_ranking_payload()):
        ok, text = _render(TR.show_investor_rankings)
    assert ok is True
    assert "외국인 · 기준 10-20 15:41" in text
    assert "개인 · 기준 10-19 20:17" in text, "개인은 장 마감 전이면 직전 영업일 값이다 — 날짜가 보여야 한다"
    assert "3,864.9억" in text and "삼성전자(005930)" in text
    assert "402340(402340)" not in text and "402340" in text, "이름을 모르면 코드만 찍는다"


def test_데이터를_못_주는_투자자는_없다고_말한다():
    with patch.object(toss_api, "get_investor_trading_rankings", return_value=_ranking_payload()):
        _, text = _render(TR.show_investor_rankings)
    assert "기관: 지금은 토스가 이 투자자 순위를 제공하지 않습니다" in text


def test_보유와_관심을_표시한다():
    with patch.object(toss_api, "get_investor_trading_rankings", return_value=_ranking_payload()):
        _, text = _render(TR.show_investor_rankings)
    line = next(ln for ln in text.splitlines() if "삼성전자(005930)" in ln and "3,864.9억" in ln)
    assert "보유" in line
    assert any("SK하이닉스(000660)" in ln and "관심" in ln for ln in text.splitlines())


def test_해석_못_한_응답은_순위_없음이_아니다():
    with patch.object(toss_api, "get_investor_trading_rankings", return_value=None):
        ok, text = _render(TR.show_investor_rankings)
    assert ok is False and "해석하지 못했습니다" in text
    with patch.object(toss_api, "get_investor_trading_rankings",
                      side_effect=toss_api.TossApiError("rate-limit-exceeded", "한도", 429)):
        ok, text = _render(TR.show_investor_rankings)
    assert ok is False and "받지 못했습니다" in text


def test_표는_135열을_넘지_않고_금액이_잘리지_않는다():
    with patch.object(toss_api, "get_investor_trading_rankings", return_value=_ranking_payload()):
        _, text = _render(TR.show_investor_rankings)
    assert max(len(ln) for ln in text.splitlines()) <= 135
    assert "…" not in text


def _sector_payload():
    return {"rankings": [
        {"rank": 1, "sectorId": "100", "name": "반도체", "changeRate": "0.0179",
         "tradingAmount": "2411000000000", "marketCapitalization": "612000000000000", "currency": "KRW"},
        {"rank": 2, "sectorId": "42", "name": "스마트폰 부품", "changeRate": "-0.0123",
         "tradingAmount": None, "marketCapitalization": "9800000000000", "currency": "KRW"},
    ]}


def _sectors():
    return {"sectors": [{"sectorId": "10", "name": "IT 하드웨어", "depth": 0, "parentSectorId": None},
                        {"sectorId": "100", "name": "반도체", "depth": 1, "parentSectorId": "10"},
                        {"sectorId": "42", "name": "스마트폰 부품", "depth": 1, "parentSectorId": "10"}]}


def test_섹터_순위는_분류와_모르는_거래대금을_구분해_보인다():
    with patch.object(toss_api, "get_sector_rankings", return_value=_sector_payload()) as m, \
         patch.object(toss_api, "get_sectors", return_value=_sectors()), \
         patch("modules.manage.toss_rankings.Prompt.ask", side_effect=["2", ""]):
        ok, text = _render(TR.show_sector_rankings)
    assert ok is True
    assert m.call_args[0] == ("TOP_GAINERS", "KR", "1w")
    assert "중 · IT 하드웨어" in text
    assert "+1.79%" in text and "-1.23%" in text
    row2 = next(ln for ln in text.splitlines() if "스마트폰 부품" in ln and "%" in ln)
    assert re.search(r"-1\.23%\s+-\s+9\.80조", row2), f"모르는 거래대금을 0 으로 찍었다: {row2}"
    assert "2.41조" in text, "1조가 넘는 금액은 조 단위로 보인다"
    assert max(len(ln) for ln in text.splitlines()) <= 135


def test_섹터를_고르면_구성_종목을_시총순으로_보인다():
    stocks = {"stocks": [{"symbol": "005930", "name": "삼성전자", "volume": "18432100",
                          "tradingAmount": "1041436650000", "marketCapitalization": "388035900000000",
                          "currency": "KRW"}]}
    with patch.object(toss_api, "get_sector_rankings", return_value=_sector_payload()), \
         patch.object(toss_api, "get_sectors", return_value=_sectors()), \
         patch.object(toss_api, "get_sector_stocks", return_value=stocks) as m, \
         patch("modules.manage.toss_rankings.Prompt.ask", side_effect=["1", "9", "1", ""]):
        _, text = _render(TR.show_sector_rankings)
    assert m.call_args[0] == ("100", "KR")
    assert "표의 번호를 입력하세요" in text
    assert "반도체 구성 종목" in text and "388.04조" in text and "1.04조" in text


def test_토스_모드가_아니면_메뉴_9번은_회색이다():
    from modules import theme_analysis
    seen = {}

    def fake_menu(title, items, default_choice="1", disabled=None, **k):
        seen['disabled'] = set(disabled or ())
        seen['keys'] = [i[0] for i in items]
        return "b"

    assert config.session.is_toss is False
    with patch.object(theme_analysis.utils, "show_menu", fake_menu), \
         patch.object(theme_analysis.utils, "clear_screen", lambda: None):
        theme_analysis.run_theme_analysis()
    assert "9" in seen['keys'] and seen['disabled'] == {"9"}


def test_토스_모드에서는_메뉴_9번이_열린다(monkeypatch):
    from modules import theme_analysis
    seen = {}

    def fake_menu(title, items, default_choice="1", disabled=None, **k):
        seen['disabled'] = set(disabled or ())
        return "b"

    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    with patch.object(theme_analysis.utils, "show_menu", fake_menu), \
         patch.object(theme_analysis.utils, "clear_screen", lambda: None):
        theme_analysis.run_theme_analysis()
    assert seen['disabled'] == set()
