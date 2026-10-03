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
    """3-7 → 국내 주식 → all → [8] 실행 → 주봉 — 담은 종목이 한 번에 그려진다."""
    stocks = [{"code": "005930", "name": "삼성전자"}, {"code": "000660", "name": "SK하이닉스"}]
    inputs = iter(["1", "all", "8", "1"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **k: next(inputs, "b")), \
         patch.object(config.session, "stock_data", {"stocks_kr": stocks}, create=True), \
         patch("core.utils.pause"), patch("core.utils.clear_screen"), \
         patch.object(chart_batch, "run_batch") as run:
        chart_batch.batch_chart_menu()
    basket, p_type, months = run.call_args.args
    assert [b["code"] for b in basket] == ["005930", "000660"]
    assert p_type == "weekly" and all(b["ovs"] is False for b in basket)
