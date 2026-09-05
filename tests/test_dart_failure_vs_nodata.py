"""DART: **조회 실패는 '데이터 없음'이 아니다**.

[왜 이 파일이 있나 · 2026-09-05]
`call_dart` 는 종전에 세 가지를 모두 None 으로 돌려줬다 — 한도초과(020), 네트워크 오류,
그리고 진짜 '조회된 데이터 없음'(013). 위의 get_dart_* 들이 그 None 을 `[]` 로 바꿔 내놓기
때문에, DART 일일 한도를 소진한 상태로 메뉴 6-7(수급·물량)을 열면 화면이

    "최근 90일간 수급·물량 관련 보고가 없습니다"

를 찍는다. 아무것도 조회하지 못한 채 무결점 진단서를 내주는 것이다(실측 재현). 이 화면은
관심종목 하나당 여러 건을 부르므로 한도 소진은 드물지 않다.

방향이 나쁘다. **오버행(잠재 매도물량)과 자기주식은 '없다'로 읽는 순간 판단이 반대로 간다.**
같은 파일의 `rem_estimated` 는 이미 '모르면 위험 쪽'을 택하는데, 그보다 앞단인 조회 실패가
통째로 조용했다. 실패를 예외로 올리면 modules/manage/scan.ScanFailures 가 이미 깔아 둔
수집 경로를 타고 화면 맨 위에 밝혀진다. ([[unknown-vs-empty]])

기업코드 맵도 같다 — 맵 하나가 비면 `if not corp: return []` 로 관심종목 **전부**가 조용히
'해당 없음'이 된다. 맵을 못 받은 것과, 맵은 받았는데 그 코드가 비상장인 것은 다르다.
"""
from unittest.mock import patch

import pytest

import config
from modules import dart_api
from modules.dart_api import DartQueryError


class _Res:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(config, "DART_API_KEY", "dummy", raising=False)


@pytest.fixture
def corp_map(monkeypatch):
    import api
    #  getter 들은 _api().get_dart_corp_map() 으로 부른다(패키지 네임스페이스 규약).
    monkeypatch.setattr(api, "get_dart_corp_map", lambda *a, **k: {"005930": "00126380"})


def test_한도초과는_예외다(key):
    with patch.object(dart_api.requests, "get",
                      return_value=_Res({"status": "020", "message": "요청 제한을 초과하였습니다."})):
        with pytest.raises(DartQueryError):
            dart_api.call_dart("list.json", {"corp_code": "x"})


def test_네트워크_오류는_예외다(key):
    with patch.object(dart_api.requests, "get", side_effect=OSError("connection reset")):
        with pytest.raises(DartQueryError):
            dart_api.call_dart("list.json", {"corp_code": "x"})


def test_데이터없음_013만_None(key):
    """'봤는데 없다'는 실패가 아니다 — 여기가 예외가 되면 정상 화면이 경고로 뒤덮인다."""
    with patch.object(dart_api.requests, "get",
                      return_value=_Res({"status": "013", "message": "조회된 데이타가 없습니다."})):
        assert dart_api.call_dart("list.json", {"corp_code": "x"}) is None


def test_정상_응답은_list를_준다(key):
    with patch.object(dart_api.requests, "get",
                      return_value=_Res({"status": "000", "list": [{"rcept_no": "1"}]})):
        assert dart_api.call_dart("list.json", {"corp_code": "x"}) == [{"rcept_no": "1"}]


@pytest.mark.parametrize("getter, args", [
    ("get_dart_bond_issue_detail", ("005930", "20260101", "20260905")),
    ("get_dart_paid_increase_detail", ("005930", "20260101", "20260905")),
    ("get_dart_disclosures", ("005930",)),
    ("get_dart_major_holdings", ("005930",)),
])
def test_한도초과가_빈_결과로_둔갑하지_않는다(key, corp_map, getter, args):
    """[핵심] 오버행·유상증자·공시가 '없다'로 보이면 판단이 반대로 간다."""
    with patch.object(dart_api.requests, "get",
                      return_value=_Res({"status": "020", "message": "한도초과"})):
        with pytest.raises(DartQueryError):
            getattr(dart_api, getter)(*args)


def test_기업코드_맵을_못_받으면_예외다(key, monkeypatch):
    """맵이 비면 관심종목 전부가 조용히 '해당 없음'이 된다."""
    monkeypatch.setattr(dart_api, "_dart_corp_map_cache", None, raising=False)
    with patch.object(dart_api.requests, "get", side_effect=OSError("dns")), \
         patch.object(dart_api.os.path, "exists", return_value=False):
        with pytest.raises(DartQueryError):
            dart_api.get_dart_corp_map(force_refresh=True)


def test_맵에_없는_코드는_실패가_아니다(key, corp_map):
    """비상장·폐지 종목은 조회 실패가 아니라 진짜 '해당 없음'이다."""
    assert dart_api.get_dart_disclosures("999999") == []


def test_키가_없으면_예외다(monkeypatch):
    monkeypatch.setattr(config, "DART_API_KEY", "", raising=False)
    with pytest.raises(DartQueryError):
        dart_api.call_dart("list.json", {"corp_code": "x"})


def test_실패가_수급물량_화면에_밝혀진다(key, monkeypatch, capsys):
    """end-to-end: 한도초과에서 '보고가 없습니다'가 아니라 실패 줄이 먼저 나와야 한다."""
    from rich.console import Console
    from modules.manage import insider

    monkeypatch.setattr(config, "console", Console(width=160), raising=False)
    with patch.object(insider, "_kr_stocks",
                      return_value=[("005930", "삼성전자"), ("000660", "SK하이닉스")]), \
         patch.object(dart_api, "get_dart_corp_map",
                      return_value={"005930": "00126380", "000660": "00164779"}), \
         patch.object(dart_api.requests, "get",
                      return_value=_Res({"status": "020", "message": "한도초과"})), \
         patch("core.utils.clear_screen", lambda: None):
        insider.show_insider_trades(days=90)

    out = capsys.readouterr().out
    assert "조회하지 못했습니다" in out, "한도 소진이 화면에 밝혀지지 않았다"
    assert "'해당 없음'이 아닙니다" in out
    assert "최근 90일간 수급·물량 관련 보고가 없습니다" not in out, \
        "아무것도 못 봤는데 무결점 진단서를 냈다"
