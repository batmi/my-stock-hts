"""'등락폭 (등락률) [강도]' 컬럼은 값마다 세로로 맞아야 한다.

한 컬럼에 값 세 개를 담으면 rich 는 셀 **전체**만 우측 정렬하므로 오른쪽 끝만 맞고
안쪽 값들은 자릿수에 따라 들쭉날쭉해진다(+2000 과 +14000 이 섞이면 등락률·강도가
세로로 안 읽힌다). 셀 안에서 값마다 폭을 맞춘다.
"""
from rich.markup import render

from core import utils

SEP = utils.CELL_PART_SEP


def _cell(diff, rate, strength=""):
    return f"{diff}{SEP}({rate}){SEP}{strength}"


def _plain(cells):
    return [render(c).plain for c in cells]


def test_parts_line_up_vertically():
    out = _plain(utils.align_cell_parts([
        _cell("+2000", "+0.80%", "[111%]"),
        _cell("+14000", "+0.88%", "[89%]"),
        _cell("+0", "+0.00%", "[0%]"),
    ]))

    # 각 값의 시작·끝 위치가 모든 행에서 같아야 한다
    assert len({len(c) for c in out}) == 1
    assert len({c.index("(") for c in out}) == 1
    #  강도는 숫자라 오른쪽(끝자리)이 맞아야 읽힌다 — 여는 대괄호가 아니라 '%]'가 기준.
    assert len({c.index("%]") for c in out}) == 1
    assert out[0] == " +2000 (+0.80%) [111%]"
    assert out[1] == "+14000 (+0.88%)  [89%]"


def test_column_does_not_get_wider_than_before():
    """정렬 때문에 컬럼이 넓어지면 터미널 폭 상한을 밀어낸다 — 최장 셀 길이는 그대로."""
    raw = [("+16000", "+2.23%", "[125%]"), ("+4700", "+3.18%", "[406%]"),
           ("+0", "+0.00%", "[0%]"), ("-500", "-0.13%", "[43%]")]
    before = max(len(f"{d} ({r}) {s}") for d, r, s in raw)
    after = max(len(c) for c in _plain(utils.align_cell_parts([_cell(*x) for x in raw])))
    assert after == before


def test_missing_strength_keeps_the_other_values_in_place():
    """강도가 없는 행(해외·미표시)도 앞의 두 값은 같은 자리에 선다."""
    out = _plain(utils.align_cell_parts([
        _cell("+2000", "+0.80%", "[111%]"),
        _cell("-500", "-0.13%"),
    ]))
    assert len({c.index("(") for c in out}) == 1


def test_cells_without_parts_are_untouched():
    """'실패'·'-' 같은 칸은 손대지 않는다 — 값 없는 행이 폭을 끌어올리면 안 된다."""
    cells = [_cell("+2000", "+0.80%", "[111%]"), "[dim]-[/dim]", "실패"]
    out = utils.align_cell_parts(cells)
    assert out[1] == "[dim]-[/dim]" and out[2] == "실패"


def test_markup_is_preserved_and_not_counted_in_width():
    """색 태그는 폭에 세지 않는다(세면 색 있는 행만 밀린다)."""
    out = utils.align_cell_parts([
        f"[red]+2000[/]{SEP}[red](+0.80%)[/]{SEP}[white][111%][/]",
        f"[blue]-14000[/]{SEP}[blue](-0.88%)[/]{SEP}[dim][89%][/dim]",
    ])
    assert "[red]" in out[0] and "[blue]" in out[1]
    plain = _plain(out)
    assert len(plain[0]) == len(plain[1])


def test_worker_emits_separated_parts(monkeypatch):
    """표 워커가 실제로 구분자를 넣어 셀을 만든다(정렬의 입력 계약)."""
    import pandas as pd

    from modules import analysis

    n = 250
    df = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n).strftime("%Y%m%d"),
        "open": [70000] * n, "high": [70500] * n, "low": [69500] * n,
        "close": [70000] * n, "volume": [1000000] * n,
    })
    price = {"rt_cd": "0", "output": {"stck_prpr": "72000", "stck_sdpr": "70000",
                                      "ats_prpr": "0", "prdy_ctrt": "2.86",
                                      "prdy_vrss": "2000"}}
    monkeypatch.setattr(analysis.api, "get_chart_data", lambda *a, **k: df)
    monkeypatch.setattr(analysis.api, "get_current_price_data", lambda *a, **k: price)
    monkeypatch.setattr(analysis.api, "get_investor_trend", lambda *a, **k: [])
    monkeypatch.setattr(analysis, "check_smart_money_turnaround", lambda *a, **k: (False, ""))

    row = analysis._print_table_worker(("삼성전자", "005930"), "국내 주식 기술적 분석",
                                       False, True, set(), {}, {}, set(), set())[0]
    assert row[4].count(SEP) == 2, "등락 셀이 값 셋으로 나뉘어 있어야 정렬할 수 있다"
