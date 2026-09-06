"""AI 응답의 종목 표기는 **사람에게 닿기 전에** KRX 상장목록과 대조한다.

[왜 이 파일이 있나 · 2026-09-07]
verify_stock_codes 는 'AI 가 종목코드를 지어내는 것'을 막으려고 만든 유일한 사후
방어선이다(프롬프트 지시만으로는 막히지 않는다는 것이 그 함수의 전제다). 그런데 그
방어선은 **코드를 요구하는 프롬프트 세 갈래에만** 걸려 있었다.

프롬프트가 요구하지 않아도 AI 는 종목을 코드와 함께 적는다. 걸리지 않은 세 갈래가
전부 텔레그램으로 사람에게 그대로 나가고 있었다:

 · /ask 자유 질문 — "추천 종목 알려줘"에 코드가 붙어 돌아온다.
 · 종목 뉴스 검색 — 특정 종목을 물었으니 그 종목 코드를 되받아 적는다.
 · 장 마감 브리핑 — 보유·매매 종목을 열거한다. 스케줄러가 매일 자동 발송한다.

검증을 어느 갈래에 걸지는 '프롬프트가 무엇을 요구했나'가 아니라 '응답에 종목 표기가
있나'로 정해야 한다. 비용은 없다 — 표기가 하나도 없으면 상장목록을 아예 부르지 않는다.
"""
from unittest.mock import patch

import pytest

from modules import theme_analysis


LISTING = {"005930": {"name": "삼성전자", "marcap": 400_000_000_000_000}}
FAKE_CODE = "• 미래바이오(999999) - 추천합니다"


@pytest.fixture
def listing(monkeypatch):
    from modules import krx_daily
    monkeypatch.setattr(krx_daily, "get_listing_map", lambda *a, **k: dict(LISTING))


def _report(text):
    """_run_gemini_report 를 이 텍스트로 고정한다."""
    return patch.object(theme_analysis, "_run_gemini_report", return_value=text)


@pytest.mark.parametrize("call", [
    lambda: theme_analysis.ask_gemini("추천 종목 알려줘"),
    lambda: theme_analysis.generate_daily_closing_report("포트폴리오"),
    lambda: theme_analysis.generate_morning_briefing("지수"),
])
def test_지어낸_코드는_사람에게_그대로_나가지_않는다(listing, call, monkeypatch):
    monkeypatch.setattr(theme_analysis, "_ensure_genai", lambda: object())
    monkeypatch.setattr(theme_analysis.config, "GEMINI_API_KEY", "k", raising=False)
    monkeypatch.setattr(theme_analysis, "_get_macro_context_str", lambda: "")
    monkeypatch.setattr(theme_analysis, "_get_today_trades_str", lambda: "")

    with _report(FAKE_CODE):
        out = call()

    assert "미상장 코드" in out
    assert "존재하지 않는 종목코드" in out


def test_뉴스_검색도_대조한다(listing, monkeypatch):
    monkeypatch.setattr(theme_analysis, "_ensure_genai", lambda: object())
    monkeypatch.setattr(theme_analysis.config, "GEMINI_API_KEY", "k", raising=False)
    monkeypatch.setattr(theme_analysis, "fetch_realtime_news", lambda *a, **k: "뉴스")

    with _report(FAKE_CODE):
        out = theme_analysis.get_latest_news_with_gemini("미래바이오", "999999")

    assert "미상장 코드" in out


def test_실재하는_종목에는_경고를_붙이지_않는다(listing, monkeypatch):
    """정직해지느라 정상 응답을 더럽히면 안 된다."""
    monkeypatch.setattr(theme_analysis, "_ensure_genai", lambda: object())
    monkeypatch.setattr(theme_analysis.config, "GEMINI_API_KEY", "k", raising=False)

    with _report("• 삼성전자(005930) - 반도체"):
        out = theme_analysis.ask_gemini("질문")

    assert "⚠️" not in out


def test_종목_표기가_없으면_상장목록을_부르지도_않는다(monkeypatch):
    """비용이 0 이라는 주장을 실제로 확인한다 — 없으면 네트워크를 건드리지 않는다."""
    from modules import krx_daily

    called = []
    monkeypatch.setattr(krx_daily, "get_listing_map",
                        lambda *a, **k: (called.append(1), dict(LISTING))[1])
    monkeypatch.setattr(theme_analysis, "_ensure_genai", lambda: object())
    monkeypatch.setattr(theme_analysis.config, "GEMINI_API_KEY", "k", raising=False)

    with _report("오늘 시장은 혼조세였습니다."):
        theme_analysis.ask_gemini("질문")

    assert called == []


def test_오류_문자열에는_검증_주석을_붙이지_않는다(monkeypatch):
    """'⚠️ Gemini API가 설정되지 않았습니다'에 검증 안내가 붙으면 원인이 흐려진다."""
    monkeypatch.setattr(theme_analysis, "_ensure_genai", lambda: None)
    out = theme_analysis.generate_daily_closing_report("포트폴리오")
    assert out.startswith("⚠️")
    assert "검증" not in out
