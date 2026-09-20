"""DART 공시목록(list.json) 페이지 이어받기 — dart_api 감사(2026-09-20).

한 페이지 100건 상한 때문에 730일·400일 창이 최신 100건으로 조용히 잘리던 자리.
"""
from unittest.mock import patch

import api
from modules import dart_api


def _rows(start, n):
    return [{"rcept_no": f"2026{i:010d}", "report_nm": f"공시{i}", "rcept_dt": "20260101"}
            for i in range(start, start + n)]


def test_꽉_찬_페이지_뒤에는_다음_페이지를_이어_받는다():
    pages = {"1": _rows(0, 100), "2": _rows(100, 100), "3": _rows(200, 17)}
    calls = []

    def fake(endpoint, params):
        calls.append(params["page_no"])
        return pages.get(params["page_no"], None)

    with patch.object(api, "get_dart_corp_map", return_value={"005930": "00126380"}), \
         patch.object(api, "call_dart", side_effect=fake):
        out = dart_api.get_dart_disclosures("005930", days=400)
    assert calls == ["1", "2", "3"]
    assert len(out) == 217 and out[0]["rcept_no"] == "20260000000000" and out[-1]["report_nm"] == "공시216"


def test_덜_찬_페이지에서_멈춘다():
    with patch.object(api, "get_dart_corp_map", return_value={"005930": "x"}), \
         patch.object(api, "call_dart", return_value=_rows(0, 7)) as m:
        out = dart_api.get_dart_disclosures("005930", days=30)
    assert len(out) == 7 and m.call_count == 1


def test_같은_페이지가_되풀이되면_멈춘다():
    """가짜·고장 난 서버가 page_no 를 무시해도 무한히 돌지 않는다."""
    with patch.object(api, "get_dart_corp_map", return_value={"005930": "x"}), \
         patch.object(api, "call_dart", return_value=_rows(0, 100)) as m:
        out = dart_api.get_dart_disclosures("005930", days=30)
    assert len(out) == 100 and m.call_count == 2
