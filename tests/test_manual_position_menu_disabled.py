"""9-5 포지션 분석 — 종목이 없으면 분석·수정·삭제는 회색·선택 불가 (2026-10-15 메뉴 비활성 통일)."""
from unittest.mock import patch

from modules import account


def _ask_once(positions):
    with patch.object(account, "load_manual_positions", return_value=positions), \
         patch.object(account, "_print_saved_positions"), \
         patch("config.console.print"), \
         patch.object(account.Prompt, "ask", return_value="b") as ask:
        account.manual_holding_analysis()
    return ask.call_args


def test_empty_positions_only_allow_add():
    call = _ask_once([])
    assert call.kwargs["choices"] == ["1", "b", "q"] and call.kwargs["default"] == "1"
    assert "[dim]0: 분석[/dim]" in call.args[0]


def test_with_positions_all_actions_allowed():
    call = _ask_once([{"code": "005930", "name": "삼성전자"}])
    assert call.kwargs["choices"] == ["0", "1", "2", "3", "b", "q"] and call.kwargs["default"] == "0"
    assert "[dim]" not in call.args[0].split(")")[0]
