"""data.krx.co.kr 공식 시세(modules/krx_data.py) 검증.

네트워크는 타지 않는다 — `_post`(원시 bld 호출)와 pykrx 지수 조회만 목으로 갈아끼우고
파싱·세션 분리·폴백 규약을 고정한다. 실제 응답 모양은 붙이기 전에 실측한 것을 그대로 옮겼다.
"""
import os
from unittest.mock import patch

import pandas as pd
import pytest

from modules import krx_data


@pytest.fixture(autouse=True)
def _isolated():
    """자격증명·라이브러리 슬롯을 채워 두고(실제 로그인 없음) 캐시를 비운다."""
    saved = (krx_data._import_done, krx_data._pykrx_webio)
    krx_data._import_done = True
    krx_data._pykrx_webio = object()        # is_available() 통과용 — _post 는 테스트가 patch
    krx_data.clear_cache()
    with patch.dict(os.environ, {"KRX_ID": "x", "KRX_PW": "y"}):
        yield
    krx_data.clear_cache()
    krx_data._import_done, krx_data._pykrx_webio = saved


# ---------------------------------------------------------------------------
# 파싱 단위
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1,099.70", 1099.70), ("203,410", 203410.0), ("58.03", 58.03),
    ("-", None), ("", None), (None, None), ("abc", None),
])
def test_num_parses_krx_notation(raw, expected):
    assert krx_data._num(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026/08/21", "20260821"),
    ("2026/08/21 (주간)", "20260821"),      # 파생은 날짜에 세션이 붙어 온다
    ("2026/08/21 (야간)", "20260821"),
    ("", None), ("이상한값", None),
])
def test_date8(raw, expected):
    assert krx_data._date8(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026/08/21 (주간)", "주간"), ("2026/08/21 (야간)", "야간"), ("2026/08/21", ""),
])
def test_session_tag(raw, expected):
    assert krx_data._session_tag(raw) == expected


def test_clamp_range_caps_at_two_years():
    """기간이 2년을 넘으면 KRX 가 빈 응답을 준다 — 넘기기 전에 잘라야 한다."""
    start, end = krx_data._clamp_range(3650)
    span = (pd.Timestamp(end) - pd.Timestamp(start)).days
    assert span <= krx_data._MAX_RANGE_DAYS


def test_expiry_key():
    assert krx_data._expiry_key("변동성지수 F 202609") == 202609
    assert krx_data._expiry_key("코스피200 F 202612 (주간)") == 202612
    assert krx_data._expiry_key("이름없음") is None


# ---------------------------------------------------------------------------
# 자격증명 게이트 — 없으면 조용히 꺼져야 한다(호출부가 종전 경로로 폴백)
# ---------------------------------------------------------------------------
def test_unavailable_without_credentials():
    with patch.dict(os.environ, {"KRX_ID": "", "KRX_PW": ""}):
        assert krx_data.is_available() is False


def test_index_returns_none_without_credentials():
    with patch.dict(os.environ, {"KRX_ID": "", "KRX_PW": ""}):
        assert krx_data.get_index_daily("KOSPI200") is None


def test_session_none_when_unavailable():
    with patch.dict(os.environ, {"KRX_ID": "", "KRX_PW": ""}):
        assert krx_data._session() is None


# ---------------------------------------------------------------------------
# 지수 티커 — KIS 코드와 숫자가 겹치므로 표가 섞이면 안 된다
# ---------------------------------------------------------------------------
def test_index_tickers_do_not_follow_kis_numbering():
    """KIS '2001'=코스피200 이지만 KRX '2001'=코스닥이다. 이 표는 KRX 체계여야 한다."""
    assert krx_data.INDEX_TICKERS["KOSPI200"] == "1028"
    assert krx_data.INDEX_TICKERS["KOSDAQ"] == "2001"
    assert krx_data.INDEX_TICKERS["KOSPI"] == "1001"
    assert krx_data.INDEX_TICKERS["KOSDAQ150"] == "2203"


def test_index_rejects_unsupported_type():
    assert krx_data.get_index_daily("VKOSPI") is None
    assert krx_data.get_index_daily("K200FUT_F") is None


# ---------------------------------------------------------------------------
# 금현물 — 실측 응답 모양 그대로
# ---------------------------------------------------------------------------
_GOLD_ROWS = [
    {"TRD_DD": "2026/08/21", "TDD_CLSPRC": "203,410", "TDD_OPNPRC": "202,390",
     "TDD_HGPRC": "203,410", "TDD_LWPRC": "201,170", "ACC_TRDVOL": "216,327"},
    {"TRD_DD": "2026/08/20", "TDD_CLSPRC": "201,620", "TDD_OPNPRC": "200,100",
     "TDD_HGPRC": "202,000", "TDD_LWPRC": "199,800", "ACC_TRDVOL": "180,000"},
]


def test_gold_daily_has_real_ohlc_and_volume():
    """네이버 경로의 한계(종가만·거래량 0)가 사라졌는지 — 이 변경의 존재 이유다."""
    with patch.object(krx_data, "_post", return_value=_GOLD_ROWS):
        df = krx_data.get_gold_daily(400)
    assert list(df.columns) == krx_data._COLUMNS
    assert df.attrs["source"] == "KRX"
    assert df["date"].tolist() == ["20260820", "20260821"]        # 오름차순
    last = df.iloc[-1]
    assert last["open"] == 202390 and last["high"] == 203410 and last["low"] == 201170
    assert last["high"] > last["low"]                              # 평탄화가 아니다
    assert (df["volume"] > 0).all()                                # OBV 가 성립한다


def test_gold_daily_none_when_endpoint_fails():
    with patch.object(krx_data, "_post", return_value=None):
        assert krx_data.get_gold_daily(400) is None


# ---------------------------------------------------------------------------
# 코스피200 선물 — 한 응답에 주간·야간이 섞여 온다
# ---------------------------------------------------------------------------
_FUT_ROWS = [
    {"TRD_DD": "2026/08/21 (주간)", "TDD_CLSPRC": "1,099.70", "TDD_OPNPRC": "1,073.05",
     "TDD_HGPRC": "1,105.25", "TDD_LWPRC": "1,061.55", "ACC_TRDVOL": "117,863"},
    {"TRD_DD": "2026/08/21 (야간)", "TDD_CLSPRC": "1,079.45", "TDD_OPNPRC": "1,068.00",
     "TDD_HGPRC": "1,085.00", "TDD_LWPRC": "1,053.05", "ACC_TRDVOL": "32,770"},
    {"TRD_DD": "2026/08/20 (주간)", "TDD_CLSPRC": "1,082.25", "TDD_OPNPRC": "1,050.90",
     "TDD_HGPRC": "1,090.75", "TDD_LWPRC": "1,033.65", "ACC_TRDVOL": "165,042"},
    {"TRD_DD": "2026/08/20 (야간)", "TDD_CLSPRC": "1,040.35", "TDD_OPNPRC": "1,058.80",
     "TDD_HGPRC": "1,064.10", "TDD_LWPRC": "1,027.65", "ACC_TRDVOL": "49,856"},
]


@pytest.mark.parametrize("session,expected_close", [
    ("F", [1082.25, 1099.70]), ("CM", [1040.35, 1079.45]),
])
def test_k200_futures_splits_day_and_night(session, expected_close):
    with patch.object(krx_data, "_front_contract", return_value=("KR4A01690002", "20260821")), \
         patch.object(krx_data, "_contract_series", return_value=_FUT_ROWS):
        df = krx_data.get_k200_futures_daily(session, 400)
    assert df["close"].tolist() == expected_close
    assert len(df) == 2                      # 반대 세션이 섞이지 않았다


def test_k200_futures_night_aliases():
    """'CM'·'야간'·'NIGHT' 는 같은 세션을 가리켜야 한다(호출부 표기가 제각각이다)."""
    for alias in ("CM", "야간", "night"):
        krx_data.clear_cache()
        with patch.object(krx_data, "_front_contract", return_value=("X", "20260821")), \
             patch.object(krx_data, "_contract_series", return_value=_FUT_ROWS):
            df = krx_data.get_k200_futures_daily(alias, 400)
        assert df["close"].tolist() == [1040.35, 1079.45], alias


def test_k200_futures_none_when_no_contract():
    with patch.object(krx_data, "_front_contract", return_value=(None, None)):
        assert krx_data.get_k200_futures_daily("F", 400) is None


def test_front_contract_picks_most_traded():
    """근월물 = 거래량 최다. 만기 문자열 표기가 바뀌어도 흔들리지 않는 기준이다."""
    cons = [("A", "코스피200 F 202612 (주간)", 4758.0),
            ("B", "코스피200 F 202609 (주간)", 117863.0)]
    with patch.object(krx_data, "_live_contracts", return_value=cons):
        isu, _day = krx_data._front_contract(krx_data.PROD_K200_FUTURES)
    assert isu == "B"


# ---------------------------------------------------------------------------
# V코스피200 — 선물 응답의 SPOT_PRC 가 현물이다
# ---------------------------------------------------------------------------
_VK_ROWS = [
    {"TRD_DD": "2026/08/21", "TDD_CLSPRC": "-", "ACC_TRDVOL": "0", "SPOT_PRC": "58.03"},
    {"TRD_DD": "2026/08/20", "TDD_CLSPRC": "55.35", "ACC_TRDVOL": "13", "SPOT_PRC": "57.26"},
]


def test_vkospi_uses_spot_not_futures_close():
    """거래량 0·종가 '-' 인 날에도 현물값은 있다 — 선물 종가를 쓰면 안 된다."""
    with patch.object(krx_data, "_live_contracts", return_value=[("A", "변동성지수 F 202609", 0.0)]), \
         patch.object(krx_data, "_contract_series", return_value=_VK_ROWS):
        df = krx_data.get_vkospi_daily(200)
    assert df["close"].tolist() == [57.26, 58.03]
    assert 55.35 not in df["close"].tolist()          # 선물 종가가 아니다
    assert (df["volume"] == 0).all()                  # 현물 지수라 거래량이 없다


def test_vkospi_flattens_ohlc_to_close():
    """현물 OHLC 가 없으므로 평탄화된다 — 이 한계를 테스트로 못박아 둔다."""
    with patch.object(krx_data, "_live_contracts", return_value=[("A", "변동성지수 F 202609", 0.0)]), \
         patch.object(krx_data, "_contract_series", return_value=_VK_ROWS):
        df = krx_data.get_vkospi_daily(200)
    for col in ("open", "high", "low"):
        assert df[col].tolist() == df["close"].tolist()


def test_vkospi_warns_when_contracts_disagree(caplog):
    """계약별 SPOT_PRC 가 어긋나면 '현물값' 전제가 깨진 것이다 — 조용히 넘기면 안 된다."""
    other = [{"TRD_DD": "2026/08/21", "SPOT_PRC": "99.99", "TDD_CLSPRC": "-"}]
    # 스냅샷마다 **다른** 계약이 나와야 두 번째 시계열까지 받는다(같은 계약은 건너뛴다).
    contracts = iter([[("A", "변동성지수 F 202609", 0.0)], [("B", "변동성지수 F 202606", 0.0)]])
    series = iter([_VK_ROWS, other])
    with patch.object(krx_data, "_live_contracts",
                      side_effect=lambda *a, **k: next(contracts, [])), \
         patch.object(krx_data, "_contract_series", side_effect=lambda *a, **k: next(series, [])), \
         patch.object(krx_data, "_VKOSPI_SNAPSHOT_BACK_DAYS", (0, 150)):
        with caplog.at_level("WARNING"):
            krx_data.get_vkospi_daily(200)
    assert any("계약별로 어긋난다" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 공통 스키마
# ---------------------------------------------------------------------------
def test_finish_drops_zero_and_missing_close():
    """거래정지·미체결 봉(종가 0/없음)은 지표를 망가뜨리므로 버린다."""
    rows = [{"date": "20260821", "close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 5},
            {"date": "20260820", "close": 0.0, "open": 0.0, "high": 0.0, "low": 0.0, "volume": 0},
            {"date": "20260819", "close": None, "open": None, "high": None, "low": None, "volume": 0}]
    df = krx_data._finish(rows, "KRX")
    assert df["date"].tolist() == ["20260821"]


def test_finish_returns_none_on_empty():
    assert krx_data._finish([], "KRX") is None
    assert krx_data._finish(None, "KRX") is None


# ---------------------------------------------------------------------------
# sys.stdout 을 건드리지 않는다 — 2026-08-25 화면 정지 장애의 재발 방지
# ---------------------------------------------------------------------------
#  sys.stdout 은 프로세스 전역이고 rich Console 은 **쓰기 시점에** sys.stdout 을 다시 읽는다.
#  워커 스레드가 redirect_stdout 을 잡고 있으면 메인 스레드의 화면 출력이 통째로 StringIO 로
#  들어가 사라진다(실측: 워커의 버퍼에 메인 출력이 그대로 담겼다). KRX 조회는 초 단위라
#  창이 넓어, 모드 2·3 모두 기동 직후 화면이 멎었다.
import ast
import inspect
import sys as _sys

from modules import krx_daily


def _redirect_users(module):
    """모듈 안에서 redirect_stdout/redirect_stderr 를 쓰는 함수 이름 집합."""
    tree = ast.parse(inspect.getsource(module))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in ("redirect_stdout", "redirect_stderr"):
                found.add(node.name)
    return found


@pytest.mark.parametrize("module", [krx_data, krx_daily], ids=["krx_data", "krx_daily"])
def test_redirect는_일회성_import_에서만_쓴다(module):
    """조회 경로에서 stdout 을 가로채면 화면이 멎는다 — 허용 지점은 _lazy_import 하나뿐이다."""
    assert _redirect_users(module) <= {"_lazy_import"}, (
        f"{module.__name__}: 조회 경로가 sys.stdout 을 가로챈다 — "
        f"pykrx 배너는 krx_data.silence_pykrx_banner() 로 지울 것")


def test_조회_중에도_stdout_이_그대로다():
    """실제 조회 경로가 전역 stdout 을 바꾸지 않는지 값으로 확인한다."""
    seen = []

    class _Sess:
        def post(self, *_a, **_k):
            seen.append(_sys.stdout)
            raise RuntimeError("네트워크 없음")     # 조회 실패는 이 테스트의 관심사가 아니다

    original = _sys.stdout
    with patch.object(krx_data, "_session", return_value=_Sess()):
        krx_data.get_gold_daily(400)
    assert seen and all(s is original for s in seen)
    assert _sys.stdout is original


def test_배너_억제는_모듈_print만_바꾼다():
    """전역을 건드리지 않고 pykrx 모듈의 print 이름만 갈아끼워야 한다."""
    auth = pytest.importorskip("pykrx.website.comm.auth")
    original = _sys.stdout
    krx_data.silence_pykrx_banner()
    assert getattr(auth, "_hts_silenced", False) is True
    assert _sys.stdout is original
    # 계정 ID 는 로거로도 흘리지 않는다
    assert auth.print("  로그인 ID: someone") is None
