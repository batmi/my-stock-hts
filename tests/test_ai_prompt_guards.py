"""AI 프롬프트 가드 및 종목코드 사후 검증 테스트.

- 매매 자문성 프롬프트에 추세추종/불확실성 가드가 실제로 붙어 있는지 (회귀 방지)
- AI가 출력한 '종목명(6자리코드)' 표기를 KRX 상장목록과 대조하는 로직
"""
import string
from unittest.mock import patch

import pytest

from modules import prompts, theme_analysis


# ==========================================================
# 1. 프롬프트 가드 부착 여부
# ==========================================================

# 매매·자산배분 판단을 요구하므로 추세추종 기조가 반드시 주입돼야 하는 프롬프트
_TREND_GUARDED = [
    "STOCK_ANALYSIS_PROMPT",
    "INDEX_ANALYSIS_PROMPT",
    "CHART_IMAGE_ANALYSIS_PROMPT",
    "DAILY_CLOSING_PROMPT",
    "BACKTEST_MONTE_CARLO_PROMPT",
    "BACKTEST_SINGLE_PROMPT",
    "BACKTEST_WALK_FORWARD_PROMPT",
    "TRADING_AUTOPSY_PROMPT",
]

# 전망·조언을 내놓으므로 확신도·반증 조건을 요구해야 하는 프롬프트
_UNCERTAINTY_GUARDED = [
    "MARKET_TRENDS_PROMPT",
    "STOCK_ANALYSIS_PROMPT",
    "INDEX_ANALYSIS_PROMPT",
    "CHART_IMAGE_ANALYSIS_PROMPT",
    "DAILY_CLOSING_PROMPT",
    "MORNING_BRIEFING_PROMPT",
    "STOCK_CURATION_PROMPT",
    "BACKTEST_MONTE_CARLO_PROMPT",
    "BACKTEST_SINGLE_PROMPT",
    "BACKTEST_WALK_FORWARD_PROMPT",
]

# '종목명(코드)' 추천이 나가는 프롬프트
_TICKER_GUARDED = [
    "MARKET_TRENDS_PROMPT",
    "MORNING_BRIEFING_PROMPT",
    "STOCK_CURATION_PROMPT",
]


@pytest.mark.parametrize("name", _TREND_GUARDED)
def test_trend_following_context_attached(name):
    assert prompts._TREND_FOLLOWING_CONTEXT in getattr(prompts, name)


@pytest.mark.parametrize("name", _UNCERTAINTY_GUARDED)
def test_uncertainty_guard_attached(name):
    assert prompts._UNCERTAINTY_GUARD in getattr(prompts, name)


@pytest.mark.parametrize("name", _TICKER_GUARDED)
def test_ticker_guard_attached(name):
    assert prompts._TICKER_GUARD in getattr(prompts, name)


def test_guard_blocks_have_no_braces():
    """공통 블록은 .format() 템플릿에 이어 붙으므로 중괄호가 있으면 KeyError가 난다."""
    for blk in (prompts._TREND_FOLLOWING_CONTEXT, prompts._UNCERTAINTY_GUARD,
                prompts._TICKER_GUARD, prompts._STYLE_RICH, prompts._STYLE_SHARED):
        assert "{" not in blk and "}" not in blk


def test_all_prompt_placeholders_intact():
    """가드 삽입으로 기존 치환자가 깨지지 않았는지 확인."""
    expected = {
        "STOCK_ANALYSIS_PROMPT": {"now", "name", "code", "tech_info_str"},
        "INDEX_ANALYSIS_PROMPT": {"now", "name", "code", "tech_info_str"},
        "CHART_IMAGE_ANALYSIS_PROMPT": {"now", "name", "code", "period_str"},
        "DAILY_CLOSING_PROMPT": {"macro_context", "portfolio_str", "today_trades_str"},
        "MORNING_BRIEFING_PROMPT": {"now", "market_data_str"},
        "STOCK_CURATION_PROMPT": {"now", "macro_context"},
        "MARKET_TRENDS_PROMPT": {"now", "macro_context"},
        "TRADING_AUTOPSY_PROMPT": {"name", "code", "buy_time", "buy_score",
                                   "holding_days", "profit_rate", "sell_reason"},
    }
    for name, fields in expected.items():
        tmpl = getattr(prompts, name)
        found = {f for _, f, _, _ in string.Formatter().parse(tmpl) if f}
        assert found == fields, f"{name} 치환자 불일치: {found}"


def test_autopsy_prompt_rejects_outcome_bias():
    """단일 거래 결과로 실패를 단정하거나 파라미터를 바꾸라는 요구가 없어야 한다."""
    tmpl = prompts.TRADING_AUTOPSY_PROMPT
    assert "규칙 정상 작동" in tmpl
    assert "표본 1건은 통계가 아닙니다" in tmpl
    assert "손실이 났다면 실패 요인" not in tmpl


def test_chart_prompt_blocks_mean_reversion():
    """과매수·과매도만을 근거로 한 매도/저점매수 조언이 금지돼 있어야 한다."""
    tmpl = prompts.CHART_IMAGE_ANALYSIS_PROMPT
    assert "평균회귀 매도 신호" in tmpl
    assert "저점매수·분할매수 의견도 금지" in tmpl


def test_curation_prompt_does_not_ask_ai_for_marcap():
    """AI는 실시간 시총을 모르므로 판정을 맡기지 않는다(시스템이 사후 검증)."""
    tmpl = prompts.STOCK_CURATION_PROMPT
    assert "시총 1천억 이상 우량주 위주" not in tmpl
    assert "시스템이 사후 검증" in tmpl


# ==========================================================
# 2. 종목코드 사후 검증
# ==========================================================

_LISTING = {
    "005930": {"name": "삼성전자", "marcap": 450_000_000_000_000},
    "373220": {"name": "LG에너지솔루션", "marcap": 90_000_000_000_000},
    "246720": {"name": "아스타", "marcap": 99_400_000_000},
}


def _patch_listing(value):
    return patch("modules.krx_daily.get_listing_map", return_value=value)


def test_verify_passes_correct_mentions():
    with _patch_listing(_LISTING):
        text = "• 삼성전자(005930) - 반도체 대장주"
        assert theme_analysis.verify_stock_codes(text) == text


def test_verify_flags_unlisted_code():
    with _patch_listing(_LISTING):
        out = theme_analysis.verify_stock_codes("• 없는종목(999999) - 추천")
    assert "⚠️미상장 코드" in out
    assert "존재하지 않는 종목코드" in out


def test_verify_flags_name_mismatch():
    with _patch_listing(_LISTING):
        out = theme_analysis.verify_stock_codes("• 엘지에너지솔루션(373220) - 2차전지")
    assert "⚠️실제: LG에너지솔루션" in out
    assert "종목명 불일치" in out


def test_verify_flags_small_cap():
    with _patch_listing(_LISTING):
        out = theme_analysis.verify_stock_codes("• 아스타(246720) - 소형주")
    assert "⚠️시총 994억" in out
    assert "시총 1,000억 미만" in out


def test_verify_skips_when_listing_unavailable():
    """조회 실패를 '없는 종목'으로 오판하면 안 된다 — 원문 그대로 반환."""
    with _patch_listing(None):
        text = "• 없는종목(999999) - 추천"
        assert theme_analysis.verify_stock_codes(text) == text


def test_verify_survives_listing_exception():
    with patch("modules.krx_daily.get_listing_map", side_effect=RuntimeError("net")):
        text = "• 삼성전자(005930) - 반도체"
        assert theme_analysis.verify_stock_codes(text) == text


def test_verify_ignores_non_ticker_parentheses():
    with _patch_listing(_LISTING):
        text = "코스피는 (2026년) 기준 상승했고 거래대금 (123,456억) 수준"
        assert theme_analysis.verify_stock_codes(text) == text


def test_verify_handles_bullet_and_table_prefixes():
    """불릿·표 구분자가 이름 앞에 붙어도 오탐하지 않아야 한다."""
    with _patch_listing(_LISTING):
        out = theme_analysis.verify_stock_codes("| 반도체 | 삼성전자(005930) |")
    assert "⚠️" not in out


@pytest.mark.parametrize("value", [None, "", 123])
def test_verify_handles_empty_input(value):
    assert theme_analysis.verify_stock_codes(value) == value
