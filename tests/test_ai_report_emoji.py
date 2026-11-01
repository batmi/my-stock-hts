"""AI 리포트 상자는 어느 터미널에서나 오른쪽 테두리가 한 줄로 서야 한다.

이모지 폭은 rich·tmux·터미널이 각자 다른 표로 정한다. rich 는 이모지를 2칸으로 세어
테두리 자리를 잡는데, 1칸으로 그리는 터미널에서는 이모지가 든 줄만 테두리가 한 칸
당겨졌다(2026-11-01 실시간 테마 분석 리포트). 터미널 상자에 그리기 직전에 이모지를
걷어 낸다 — 원문(캐시·텔레그램)은 손대지 않는다.
"""
import io
from unittest.mock import patch

from rich.cells import cell_len
from rich.console import Console

from core import utils


def _has_emoji(s):
    return any(ord(c) >= 0x1F000 or c in "️‍⭐⚠" for c in s)


def test_section_title_emoji_removed_with_its_space():
    assert utils.strip_emoji("[⭐마켓 브리핑]") == "[마켓 브리핑]"
    assert utils.strip_emoji("[📊 오늘의 핵심 테마 TOP 5 요약 표]") == "[오늘의 핵심 테마 TOP 5 요약 표]"
    assert utils.strip_emoji("🤖 AI 종목 심층 진단: 삼성전자(005930)") == "AI 종목 심층 진단: 삼성전자(005930)"
    assert utils.strip_emoji("  - 🔍 들여쓰기") == "  - 들여쓰기"


def test_warning_keeps_its_meaning_as_text_symbol():
    """검증 주석(⚠️시총 704억)은 지우면 경고인 줄 모른다 — 어디서나 1칸인 ※ 로 바꾼다."""
    assert utils.strip_emoji("(000440 ⚠️시총 704억)") == "(000440 ※시총 704억)"
    assert utils.strip_emoji("⚠️ 시스템 검증 (KRX 상장목록 대조)") == "※ 시스템 검증 (KRX 상장목록 대조)"


def test_words_beside_emoji_do_not_merge():
    assert utils.strip_emoji("상승🚀 지속") == "상승 지속"
    assert utils.strip_emoji("상승 🚀 지속") == "상승 지속"
    # 국기·ZWJ 처럼 붙은 묶음도 공백 하나만 남긴다
    assert utils.strip_emoji("🇰🇷 한국 🇺🇸 미국") == "한국 미국"
    assert utils.strip_emoji("👨‍💻 개발") == "개발"


def test_text_symbols_with_stable_width_are_kept():
    """★ ✓ → ▲ 같은 글자 기호는 어디서나 1칸이다 — 강도 표기 같은 뜻을 지우지 않는다."""
    s = "강도 ★★★☆☆ ✓ → ▲ • … ─ ⌘ ⬆"
    assert utils.strip_emoji(s) == s


def test_empty_passthrough():
    assert utils.strip_emoji("") == ""
    assert utils.strip_emoji(None) is None


def test_panel_right_border_is_one_column():
    report = (
        "[⭐마켓 브리핑, 매크로 및 글로벌 정세 요약]\n\n"
        "본문 한 줄입니다.\n\n"
        "[📊오늘의 핵심 테마 TOP 5 요약 표]\n\n"
        "| 순위 | 테마명 | 대장주 |\n|---|---|---|\n"
        "| 1 | 에너지 | 중앙에너비스(000440 ⚠️시총 704억) |\n\n"
        "[🔍테마별 심층 분석 (Deep Dive)]\n"
    )
    console = Console(file=io.StringIO(), width=200, record=True, color_system=None)
    with patch("config.console", console):
        utils.print_ai_report(report, "🤖 실시간 테마 분석 리포트")

    lines = [ln.rstrip() for ln in console.export_text().splitlines() if ln.strip()]
    assert lines, "상자가 출력되지 않았다"
    # rich 자신의 셈으로는 이모지가 있어도 폭이 맞는다 — 터미널과 셈이 갈리는 글자가
    #  하나도 없어야 어느 터미널에서나 맞는다. 이 줄이 결함을 잡는 검사다.
    assert not any(_has_emoji(ln) for ln in lines)
    # 상자 배치는 종전 그대로(좌측 마진 4 + 상자 폭 120)
    assert {cell_len(ln) for ln in lines} == {124}
    assert "실시간 테마 분석 리포트" in lines[0]
    assert any("※시총 704억" in ln for ln in lines)
