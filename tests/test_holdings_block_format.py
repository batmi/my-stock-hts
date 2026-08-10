"""텔레그램 보유 종목 블록의 표기를 고정한다.

format_holdings_block 하나가 /status·/holdings·/position(수동 포지션)·시스템 시작/종료
알림·장 시작/마감 알림을 전부 그린다. 표기가 메시지마다 갈리지 않게 하려고 한 곳으로
모은 함수라, 여기서 형식이 바뀌면 다섯 화면이 동시에 바뀐다. 그런데 형식을 고정하는
테스트가 없어 그동안 무엇이 정답인지 코드 말고는 근거가 없었다.

[2026-08-10] 한 줄에 두 항목을 '|'로 묶던 표기를 한 줄 한 항목으로 바꿨다. 모바일 폭에서
임의로 접히면 묶인 두 값의 경계가 흐려지고, 아래 상태·최고가·ATR·TS 줄과 들여쓰기가
어긋나 세로로 훑어 읽을 수 없기 때문이다.
"""
import pytest

from modules.auto_trade import format_holdings_block


def _holding(**over):
    """실측 예시(신한지주 9주)를 그대로 쓴다 — 숫자가 서로 맞아떨어져야 표기 오류가 보인다."""
    item = {
        'pdno': '055550', 'prdt_name': '신한지주', 'hldg_qty': '9',
        'prpr': '104600', 'pchs_avg_pric': '108600',
        'evlu_amt': '941400', 'evlu_pfls_amt': '-36000', 'evlu_pfls_rt': '-3.68',
    }
    item.update(over)
    return item


def _lines(msg):
    return msg.split("\n")


# ─────────────────────────────────────────────
# 1. 기본 4줄 — 한 줄에 한 항목
# ─────────────────────────────────────────────

def test_each_value_gets_its_own_line():
    msg = format_holdings_block([_holding()])
    lines = _lines(msg)

    assert "• 신한지주 (9주)" in lines
    assert "   현재: 104,600원" in lines
    assert "   평단: 108,600원" in lines
    assert "   평가: 941,400원" in lines
    assert "   손익: -36,000원 (-3.68%)" in lines


def test_no_pipe_separated_pairs():
    """'|'로 두 항목을 묶지 않는다 — 모바일에서 접히면 경계가 사라진다."""
    msg = format_holdings_block([_holding()])
    assert "|" not in msg, f"항목을 '|'로 묶었다:\n{msg}"


def test_value_lines_share_one_indent():
    """종목 아래 값 줄은 들여쓰기가 모두 같아야 세로로 훑어 읽힌다."""
    msg = format_holdings_block([_holding()])
    value_lines = [ln for ln in _lines(msg)
                   if ln.startswith(" ") and any(k in ln for k in ("현재:", "평단:", "평가:", "손익:"))]
    assert len(value_lines) == 4
    assert {len(ln) - len(ln.lstrip()) for ln in value_lines} == {3}


def test_profit_keeps_its_sign_and_rate_together():
    """손익은 금액과 비율이 한 줄에 남는다(같은 사실의 두 표현이라 붙어 있어야 한다)."""
    msg = format_holdings_block([_holding()])
    line = next(ln for ln in _lines(msg) if "손익:" in ln)
    assert "-36,000원" in line and "(-3.68%)" in line


def test_gain_is_rendered_with_a_plus_sign():
    msg = format_holdings_block([_holding(evlu_pfls_amt='36000', evlu_pfls_rt='3.68')])
    assert "   손익: +36,000원 (+3.68%)" in _lines(msg)


# ─────────────────────────────────────────────
# 2. 분석 결과가 붙는 줄 — 순서와 들여쓰기
# ─────────────────────────────────────────────

def _analysis(**over):
    res = {
        'state': '관심', 'score': 5.5, 'unmanaged': False,
        'highest_price': 110200, 'max_profit_rate': 1.5,
        'applied_sl_rate': -9.3, 'is_atr_stop': True,
    }
    res.update(over)
    return {'055550': res}


def test_analysis_lines_follow_in_a_fixed_order():
    """상태 → 최고가 → 손절가 → TS. 화면마다 순서가 달라지면 눈이 위치를 못 잡는다."""
    msg = format_holdings_block([_holding()], analysis_results=_analysis())
    lines = _lines(msg)
    order = [i for i, ln in enumerate(lines)
             if any(k in ln for k in ("상태:", "최고가:", "ATR:"))]
    assert order == sorted(order)
    assert "   상태: 🟢 관심 5.5 자동" in lines
    assert "   최고가: 110,200 (+1.5%)" in lines
    assert "   ATR: 98,500 (-9.3%)" in lines   # 108,600 × (1 - 9.3%)


def test_manual_position_hides_the_auto_tag():
    """/position(수동 포지션)은 자동/수동 꼬리표가 의미 없어 붙이지 않는다."""
    msg = format_holdings_block([_holding()], title="포지션 분석",
                                analysis_results=_analysis(), show_auto_status=False)
    line = next(ln for ln in _lines(msg) if "상태:" in ln)
    assert line.endswith("관심 5.5"), f"자동/수동 꼬리표가 붙었다: {line}"


def test_unmanaged_holding_is_tagged_manual():
    msg = format_holdings_block([_holding()], analysis_results=_analysis(unmanaged=True))
    assert "   상태: 🟢 관심 5.5 수동" in _lines(msg)


def test_ts_target_and_stop_stay_on_separate_lines():
    """TS 발동가와 그때 생길 청산선은 줄을 나눠 들여쓴다(한 줄이면 모바일에서 '→'가 끊긴다)."""
    ts = {'armed': False, 'activation': 20.2, 'callback': 14.0, 'stop_price': 0}
    msg = format_holdings_block([_holding()], analysis_results=_analysis(ts=ts))
    lines = _lines(msg)

    target = next(ln for ln in lines if ln.startswith("   TS:"))
    assert "도달 시" in target
    idx = lines.index(target)
    assert lines[idx + 1].lstrip().startswith("→ 청산선"), (
        f"청산선이 발동가 바로 다음 줄이 아니다:\n{msg}")
    assert lines[idx + 1].startswith("          "), "청산선 줄은 발동가에 딸리도록 더 들여쓴다"


# ─────────────────────────────────────────────
# 3. 헤더 · 다종목
# ─────────────────────────────────────────────

def test_header_carries_the_title_and_count():
    msg = format_holdings_block([_holding(), _holding(pdno='005930', prdt_name='삼성전자')])
    assert msg.startswith("📋 [보유 종목 현황] (2종목)")


def test_holdings_are_separated_by_a_blank_line():
    msg = format_holdings_block([_holding(), _holding(pdno='005930', prdt_name='삼성전자')])
    assert "\n\n• 삼성전자" in msg


def test_empty_list_leaves_only_the_header():
    """보유가 없을 때의 안내 문구는 호출부마다 달라 이 함수가 붙이지 않는다."""
    assert format_holdings_block([]) == "📋 [보유 종목 현황] (0종목)"


def test_name_decorator_marks_restricted_and_ruled_stocks():
    msg = format_holdings_block([_holding()], name_decorator=lambda code, name: name + "-+")
    assert "• 신한지주-+ (9주)" in _lines(msg)
