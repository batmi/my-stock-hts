"""[3]-7 일괄 차트 생성 — 다중 선택 파서·대상 해석·일괄 실행 계약.

핵심 계약: 일괄 생성은 단건과 **같은 화질**로 그린다(dpi 를 넘기지 않아 CHART_DPI 를 쓴다).
"""
from unittest.mock import patch

import pytest

import config
from core import utils
from modules import chart_batch


# ── parse_multi_selection ───────────────────────────────────────────
@pytest.mark.parametrize("text,n,expected", [
    ("1,3,7", 10, [0, 2, 6]),
    ("5-8", 10, [4, 5, 6, 7]),
    ("1, 4-6 9", 10, [0, 3, 4, 5, 8]),
    ("3,1,3", 5, [2, 0]),            # 입력 순서 유지·중복 제거
    ("8-6", 10, [5, 6, 7]),          # 뒤집힌 범위
    ("all", 3, [0, 1, 2]),
    ("ALL", 2, [0, 1]),
])
def test_parse_multi_selection_numbers(text, n, expected):
    assert utils.parse_multi_selection(text, n) == expected


@pytest.mark.parametrize("text", ["삼성", "NVDA", "", "005930"])
def test_parse_multi_selection_falls_back_to_search(text):
    # 한 토큰짜리 범위 밖 숫자(종목코드)는 번호가 아니라 검색어다
    assert utils.parse_multi_selection(text, 10) is None


@pytest.mark.parametrize("text", ["1,11", "9-12", "0,2"])
def test_parse_multi_selection_out_of_range_raises(text):
    with pytest.raises(ValueError):
        utils.parse_multi_selection(text, 10)


def test_select_multiple_search_then_all():
    items = [{"code": "005930", "name": "삼성전자"}, {"code": "000660", "name": "SK하이닉스"},
             {"code": "028260", "name": "삼성물산"}]
    with patch("core.utils.Prompt.ask", side_effect=["삼성", "all"]):
        got = utils.select_multiple_from_list(items)
    assert [g["code"] for g in got] == ["005930", "028260"]


def test_select_multiple_cancel():
    with patch("core.utils.Prompt.ask", return_value="b"):
        assert utils.select_multiple_from_list([{"code": "1", "name": "a"}]) is None


# ── 대상 해석 ──────────────────────────────────────────────────────
def test_parse_direct_codes():
    assert chart_batch.parse_direct_codes("005930, 0080g0  nvda,AAPL") == [
        ("005930", False), ("0080G0", False), ("NVDA", True), ("AAPL", True)]


def test_add_to_basket_dedup_and_cap(monkeypatch):
    monkeypatch.setattr(config, "BATCH_CHART_MAX", 2)
    basket = [{"code": "005930", "name": "삼성전자", "ovs": False}]
    added, dropped = chart_batch._add_to_basket(basket, [
        {"code": "005930", "name": "삼성전자", "ovs": False},     # 중복
        {"code": "NVDA", "name": "NVIDIA", "ovs": True},
        {"code": "AAPL", "name": "Apple", "ovs": True},          # 상한 초과
    ])
    assert (added, dropped) == (1, 1)
    assert [b["code"] for b in basket] == ["005930", "NVDA"]


# ── 일괄 실행 ──────────────────────────────────────────────────────
T = [{"code": "005930", "name": "삼성전자", "ovs": False},
     {"code": "000660", "name": "SK하이닉스", "ovs": False},
     {"code": "NVDA", "name": "NVIDIA", "ovs": True}]


@pytest.fixture
def no_skip():
    with patch.object(chart_batch, "skip_reason", return_value=None):
        yield


def test_batch_uses_same_quality_as_single(no_skip, tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    with patch("modules.chart.generate_visual_chart", return_value=str(png)) as gen:
        res = chart_batch.generate_batch_charts(T[:1], "weekly", 6)
    assert res[0]["status"] == chart_batch.STATUS_OK
    kwargs = gen.call_args.kwargs
    assert "dpi" not in kwargs           # 화질 다이얼을 건드리지 않는다 → CHART_DPI
    assert kwargs["open_file"] is False and kwargs["period_type"] == "weekly"


def test_batch_isolates_failures(no_skip, tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    side = [RuntimeError("boom"), None, str(png)]
    with patch("modules.chart.generate_visual_chart", side_effect=side) as gen:
        res = chart_batch.generate_batch_charts(T, "daily", 12)
    assert gen.call_count == 3
    assert [r["status"] for r in res] == ["fail", "fail", "ok"]
    assert "boom" in res[0]["reason"] and res[1]["reason"] == "데이터 없음"


def test_batch_keyboard_interrupt_keeps_done(no_skip, tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    with patch("modules.chart.generate_visual_chart", side_effect=[str(png), KeyboardInterrupt]):
        res = chart_batch.generate_batch_charts(T, "daily", 6)
    assert [r["status"] for r in res] == ["ok", "cancel", "cancel"]


def test_batch_skips_blocked_combos_without_rendering():
    with patch.object(config.session, "is_toss", True, create=True), \
         patch("api.index_source_kind", return_value=None), \
         patch("modules.chart.generate_visual_chart") as gen:
        res = chart_batch.generate_batch_charts(T[:1], "hourly", 6)
    gen.assert_not_called()
    assert res[0]["status"] == "skip"


def test_skip_reason_index_intraday():
    with patch("api.index_source_kind", return_value="kr_index"):
        assert chart_batch.skip_reason("KOSPI", "intraday")
        assert chart_batch.skip_reason("KOSPI", "weekly") is None


def test_menu_flow_watchlist_all_then_run():
    """3-7 → 국내 주식 → all → [9] 실행 → 주봉 — 담은 종목이 한 번에 그려진다."""
    stocks = [{"code": "005930", "name": "삼성전자"}, {"code": "000660", "name": "SK하이닉스"}]
    inputs = iter(["1", "all", "9", "1"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **k: next(inputs, "b")), \
         patch.object(config.session, "stock_data", {"stocks_kr": stocks}, create=True), \
         patch("core.utils.pause"), patch("core.utils.clear_screen"), \
         patch.object(chart_batch, "run_batch") as run:
        chart_batch.batch_chart_menu()
    basket, p_type, months = run.call_args.args
    assert [b["code"] for b in basket] == ["005930", "000660"]
    assert p_type == "weekly" and all(b["ovs"] is False for b in basket)


# ── [8] 선택 삭제 (2026-11-09 · 11-16 번호 7→8) ─────────────────────────────────────
def _basket():
    return [dict(t) for t in T] + [{"code": "028260", "name": "삼성물산", "ovs": False}]


def _remove(inputs, basket=None):
    basket = _basket() if basket is None else basket
    with patch("rich.prompt.Prompt.ask", side_effect=list(inputs)):
        removed = chart_batch._remove_from_basket(basket, chart_batch._pick_to_remove(basket))
    return removed, [b["code"] for b in basket]


def test_remove_picks_numbers_and_ranges():
    assert _remove(["1,3"]) == (2, ["000660", "028260"])
    assert _remove(["2-3"]) == (2, ["005930", "028260"])


def test_remove_zero_empties_the_basket():
    assert _remove(["0"]) == (4, [])
    assert _remove(["all"]) == (4, [])        # 담을 때의 문법도 받는다


def test_remove_zero_mixed_with_numbers_is_asked_again():
    """'0,2' 는 모두인지 2번인지 모른다 — 빼지 않고 다시 묻는다."""
    assert _remove(["0,2", "2"]) == (1, ["005930", "NVDA", "028260"])


@pytest.mark.parametrize("cancel", ["", "b", "q"])
def test_remove_cancel_keeps_everything(cancel):
    """빈 Enter 는 취소다 — 공용 다중 선택에서는 '빈 검색어'라 한 종목짜리 바구니가 Enter 한 번에 비었다."""
    assert _remove([cancel]) == (0, ["005930", "000660", "NVDA", "028260"])
    one = [{"code": "005930", "name": "삼성전자", "ovs": False}]
    assert _remove([cancel], one) == (0, ["005930"])


def test_remove_by_search_needs_a_single_match():
    """검색이 여러 종목에 맞으면 빼지 않고 번호를 알려 준 뒤 다시 묻는다."""
    assert _remove(["삼성", "4"]) == (1, ["005930", "000660", "NVDA"])
    assert _remove(["nvda"]) == (1, ["005930", "000660", "028260"])


def test_remove_out_of_range_number_is_not_a_code_search():
    """'9' 가 범위 밖이라고 9 가 든 005930 을 빼면 안 된다. 코드와 정확히 같을 때만 그 종목이다."""
    assert _remove(["9", ""]) == (0, ["005930", "000660", "NVDA", "028260"])
    assert _remove(["005930"]) == (1, ["000660", "NVDA", "028260"])
    assert _remove(["1,9", "2"]) == (1, ["005930", "NVDA", "028260"])   # 여러 토큰의 범위 밖은 다시 묻는다


def test_menu_flow_remove_one_then_run():
    """3-7 → 국내 주식 → all → [8] 2번 빼기 → [9] 실행 — 남은 종목만 그린다."""
    stocks = [{"code": "005930", "name": "삼성전자"}, {"code": "000660", "name": "SK하이닉스"},
              {"code": "028260", "name": "삼성물산"}]
    inputs = iter(["1", "all", "8", "2", "9", "1"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **k: next(inputs, "b")), \
         patch.object(config.session, "stock_data", {"stocks_kr": stocks}, create=True), \
         patch("core.utils.pause"), patch("core.utils.clear_screen"), \
         patch.object(chart_batch, "run_batch") as run:
        chart_batch.batch_chart_menu()
    assert [b["code"] for b in run.call_args.args[0]] == ["005930", "028260"]


def test_menu_default_is_last_choice():
    """[2026-10-15] 다른 메뉴처럼 마지막 선택이 기본값 — 바구니가 차도 실행(9번)에 고정되지 않는다.
    비활성(바구니가 빈 8·9)이 마지막 선택이면 1번으로."""
    stocks = [{"code": "005930", "name": "삼성전자"}]
    choices = iter(["1", "3", "8", "b"])
    defaults = []

    def fake_menu(title, items, default_choice="1", **k):
        defaults.append(default_choice)
        return next(choices)

    with patch("core.utils.show_menu", side_effect=fake_menu), \
         patch.object(config.session, "stock_data", {"stocks_kr": stocks, "stocks_us": []}, create=True), \
         patch.object(chart_batch, "_pick_from_watchlist", return_value=[]), \
         patch("core.utils.pause"), patch("core.utils.clear_screen"):
        chart_batch.batch_chart_menu()
    assert defaults == ["1", "1", "3", "1"]


# ── [7] 그룹 A~Z (2026-11-16) ──────────────────────────────────────
import json  # noqa: E402


def _groups_file():
    return config.CHART_GROUPS_FILE


def _write_raw(data):
    with open(_groups_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _run_menu(inputs, stocks=None):
    """3-7 메뉴를 inputs 로 돌린다. run_batch 목(호출되지 않았으면 call_args 가 None)."""
    it = iter(inputs)
    stocks = stocks if stocks is not None else [{"code": "005930", "name": "삼성전자"},
                                                {"code": "000660", "name": "SK하이닉스"}]
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **k: next(it, "b")), \
         patch.object(config.session, "stock_data", {"stocks_kr": stocks}, create=True), \
         patch("core.utils.pause"), patch("core.utils.clear_screen"), \
         patch.object(chart_batch, "run_batch") as run:
        chart_batch.batch_chart_menu()
    return run


def test_groups_missing_file_is_empty_and_round_trip_keeps_other_slots():
    assert chart_batch.load_groups() == {}
    assert chart_batch.write_group("C", T[:2]) is not None
    assert chart_batch.write_group("A", T[2:]) is not None
    got = chart_batch.load_groups()
    assert list(got) == ["A", "C"]                       # 슬롯 순서로 저장
    assert got["C"] == T[:2] and got["A"] == T[2:]
    assert chart_batch.write_group("C", None) is not None   # 지우기는 그 슬롯만
    assert list(chart_batch.load_groups()) == ["A"]


def test_groups_unreadable_is_none_and_never_overwritten():
    """읽지 못한 그룹 파일은 '그룹 없음'이 아니다 — 저장이 다른 슬롯을 지우면 안 된다."""
    _write_raw({"something": "else"})
    assert chart_batch.load_groups() is None
    assert chart_batch.write_group("A", T) is None
    with open(_groups_file(), encoding="utf-8") as f:
        assert json.load(f) == {"something": "else"}


def test_groups_skip_malformed_slots_and_items():
    _write_raw({"version": 1, "groups": {
        "A": [T[0], {"code": "", "ovs": False}, {"code": "X", "name": "x", "ovs": "no"}, "junk"],
        "AA": [T[1]], "b": [T[1]], "C": "notalist",
        "D": [{"code": "NVDA", "ovs": True}]}})       # 이름이 없으면 코드로
    assert chart_batch.load_groups() == {"A": [T[0]], "D": [{"code": "NVDA", "name": "NVDA", "ovs": True}]}


def test_groups_save_failure_is_reported_not_claimed():
    chart_batch.write_group("A", T[:1])
    with patch("core.jsonio.save_json", return_value=False):
        assert chart_batch.write_group("A", T) is None
        with patch.object(config.console, "print") as out, patch("core.utils.pause"), \
             patch("rich.prompt.Prompt.ask", side_effect=["A", "y"]):
            groups, slot = chart_batch._save_group(list(T), chart_batch.load_groups())
    assert slot is None and groups["A"] == T[:1]
    assert any("저장 실패" in str(c.args[0]) for c in out.call_args_list if c.args)
    assert chart_batch.load_groups()["A"] == T[:1]


def test_menu_default_is_group_when_groups_exist():
    """저장된 그룹이 있으면 들어올 때 기본값이 7번(그룹) — 그 뒤로는 마지막 선택."""
    chart_batch.write_group("A", T[:1])
    choices = iter(["7", "b", "b"])
    defaults = []

    def fake_menu(title, items, default_choice="1", **k):
        defaults.append((title, default_choice, k.get("disabled")))
        return next(choices)

    with patch("core.utils.show_menu", side_effect=fake_menu), patch("core.utils.clear_screen"):
        chart_batch.batch_chart_menu()
    assert defaults[0][1] == "7"
    assert defaults[1][0].startswith("그룹") and defaults[1][1] == "1"     # 하위 메뉴는 불러오기
    assert defaults[1][2] == {"2"}                                         # 바구니가 비어 저장만 회색
    assert defaults[2][1] == "7"


def test_menu_group_disabled_without_basket_and_groups():
    seen = []

    def fake_menu(title, items, default_choice="1", **k):
        seen.append(k.get("disabled"))
        return "b"

    with patch("core.utils.show_menu", side_effect=fake_menu), patch("core.utils.clear_screen"):
        chart_batch.batch_chart_menu()
    assert seen == [{"7", "8", "9"}]   # 그룹·선택 삭제·실행 모두 회색


def test_menu_flow_load_group_then_run():
    """3-7 → [7] → [1] 불러오기 → a → [9] 실행 — 그룹만 그린다(소문자 슬롯도 받는다)."""
    chart_batch.write_group("A", [T[2], T[0]])
    run = _run_menu(["7", "1", "a", "9", "1"])
    assert [b["code"] for b in run.call_args.args[0]] == ["NVDA", "005930"]


@pytest.mark.parametrize("mode,expected", [
    ("1", ["NVDA"]),                       # 기본: 교체
    ("2", ["005930", "000660", "NVDA"]),   # 뒤에 더하기(중복 제외)
])
def test_menu_flow_load_into_filled_basket_asks_replace_or_append(mode, expected):
    chart_batch.write_group("A", [T[2], T[0]] if mode == "2" else [T[2]])
    run = _run_menu(["1", "all", "7", "1", "A", mode, "9", "1"])
    assert [b["code"] for b in run.call_args.args[0]] == expected


def test_menu_flow_load_into_filled_basket_cancel_keeps_basket():
    chart_batch.write_group("A", [T[2]])
    run = _run_menu(["1", "all", "7", "1", "A", "b", "9", "1"])
    assert [b["code"] for b in run.call_args.args[0]] == ["005930", "000660"]


def test_menu_flow_save_basket_to_slot():
    _run_menu(["1", "all", "7", "2", "k"])
    assert [b["code"] for b in chart_batch.load_groups()["K"]] == ["005930", "000660"]


@pytest.mark.parametrize("answer,expected", [("n", ["NVDA"]), ("y", ["005930", "000660"])])
def test_menu_flow_save_over_existing_slot_asks(answer, expected):
    chart_batch.write_group("A", [T[2]])
    _run_menu(["1", "all", "7", "2", "A", answer])
    assert [b["code"] for b in chart_batch.load_groups()["A"]] == expected


def test_menu_flow_delete_group_asks():
    chart_batch.write_group("A", [T[2]])
    chart_batch.write_group("B", [T[0]])
    _run_menu(["7", "3", "A", "n"])
    assert set(chart_batch.load_groups()) == {"A", "B"}
    _run_menu(["7", "3", "A", "y"])
    assert set(chart_batch.load_groups()) == {"B"}


def test_ask_slot_b_is_a_slot_and_enter_cancels():
    """슬롯을 묻는 자리에서 b·q 는 B·Q 슬롯이다. 취소는 빈 Enter."""
    with patch("rich.prompt.Prompt.ask", side_effect=["b"]):
        assert chart_batch._ask_slot({}, "q", must_exist=False) == "B"
    with patch("rich.prompt.Prompt.ask", side_effect=["b", "zz", "1", ""]):      # 빈 B·잘못된 입력은 다시 묻는다
        assert chart_batch._ask_slot({"A": [T[0]]}, "q", must_exist=True) is None
    with patch("rich.prompt.Prompt.ask", side_effect=["b", "a"]):
        assert chart_batch._ask_slot({"A": [T[0]]}, "q", must_exist=True) == "A"


def test_basket_text_marks_group_and_unsaved_change():
    groups = {"A": [T[0], T[1]]}
    assert "(그룹 A)" in chart_batch._basket_text([T[0], T[1]], "A", groups)
    assert "(그룹 A · 수정됨)" in chart_batch._basket_text([T[0]], "A", groups)
    assert "그룹" not in chart_batch._basket_text([T[0]], None, groups)
    assert "그룹" not in chart_batch._basket_text([T[0]], "A", {})          # 지워진 그룹은 표시하지 않는다


def test_load_group_respects_cap(monkeypatch):
    monkeypatch.setattr(config, "BATCH_CHART_MAX", 2)
    basket = []
    with patch("rich.prompt.Prompt.ask", side_effect=["A"]), patch("core.utils.pause"):
        assert chart_batch._load_group(basket, {"A": list(T)}) == "A"
    assert [b["code"] for b in basket] == ["005930", "000660"]


def test_load_same_group_does_not_ask_replace_or_append():
    """바구니가 이미 그 그룹과 같으면 교체·추가를 묻지 않는다 — 어느 쪽이든 결과가 같다."""
    chart_batch.write_group("A", [T[0], T[1]])
    #  1·all 로 그룹 A 와 같은 두 종목을 담고 → [7] 불러오기 A → (질문 없이) [9] 실행.
    #  질문을 했다면 '9' 를 답으로 먹어 실행 입력이 어긋난다.
    run = _run_menu(["1", "all", "7", "1", "A", "9", "1"])
    assert [b["code"] for b in run.call_args.args[0]] == ["005930", "000660"]
