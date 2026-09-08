"""FDR 상장목록이 **매 거래일 몇 시간씩** 죽던 것을 견디게 한다.

[무엇이 문제였나 · 2026-09-08 진단]
`fdr.StockListing` 은 KRX 에서 목록을 받지 않는다. ① KRX 에 '최종 영업일(max_work_dt)'을
묻고 ② **그 날짜의 CSV** 를 제3자 GitHub 저장소에서 내려받는다. ②는 장 마감 뒤 올라오는
파일이라, KRX 가 오늘을 영업일이라고 답한 순간부터 그 파일이 올라오기 전까지 매 거래일
404 가 난다(실측: 2026-09-08 오늘 404 · 어제 이전은 전부 200).

그동안 탐색 메뉴(modules/manage/discover)가 통째로 죽었고, 씨드 감사도 표본을 만들지
못했다. 죽은 엔드포인트가 아니라 **날마다 되풀이되는 시차**이므로 '있는 것 중 가장 최근
것'을 쓴다 — 상장목록은 하루 사이 거의 변하지 않는다.

[왜 대체하지 않는가] pykrx 도 업종을 주지만 **어휘가 다르다.** 배제 규칙 키워드는 KRX
표준산업분류 세분류에 맞춰 쓰여 있어서(예: "전기 통신업"·"종합 소매업"·"기타 금융업"),
pykrx 대분류(예: "전기·가스"·"유통"·"기타금융")로 바꾸면 실측상 매칭이 **102/106 → 0/0**
이 된다. 즉 방어주·지주회사 배제가 조용히 꺼진다([[defensive-sector-exclusion]] ·
[[discover-menu-rules-audited]]). 상장폐지 목록은 pykrx 에 아예 없다.
"""
import pandas as pd
import pytest

from modules import krx_daily


@pytest.fixture
def fdr_stub(monkeypatch):
    """_lazy_import 를 건너뛰고 가짜 FDR 을 심는다."""
    class _FDR:
        def __init__(self):
            self.fail = False
            self.empty = False

        def StockListing(self, kind):
            if self.fail:
                raise RuntimeError("HTTP Error 404: Not Found")
            if self.empty:
                return pd.DataFrame()
            return pd.DataFrame({"Code": ["005930"], "Name": ["삼성전자"]})

    stub = _FDR()
    monkeypatch.setattr(krx_daily, "_lazy_import", lambda: None)
    monkeypatch.setattr(krx_daily, "_fdr", stub)
    return stub


@pytest.fixture
def cache_days(monkeypatch):
    """캐시 저장소가 가진 날짜 집합을 흉내 낸다. 반환된 set 을 테스트가 채운다."""
    available = set()

    def _read_csv(url, **kw):
        day = str(url).rsplit("/", 1)[-1].replace(".csv", "")
        if day not in available:
            raise OSError(f"404: {url}")
        return pd.DataFrame({"Code": ["000660"], "Name": ["SK하이닉스"],
                             "ListingDate": ["1996-12-26"]})

    monkeypatch.setattr(krx_daily.pd, "read_csv", _read_csv)
    return available


def test_normal_path_is_used_when_fdr_works(fdr_stub, cache_days):
    df = krx_daily.fdr_listing("KRX")
    assert list(df["Code"]) == ["005930"], "정상 경로를 두고 캐시로 내려가면 안 된다"


def test_todays_missing_file_falls_back_to_yesterday(fdr_stub, cache_days):
    """오늘 파일이 없으면 어제 것으로 — 이것이 매 거래일 반복되는 그 창이다."""
    import datetime as dt
    fdr_stub.fail = True
    cache_days.add((dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d"))

    df = krx_daily.fdr_listing("KRX")
    assert df is not None and len(df) == 1
    assert list(df["Code"]) == ["000660"]


def test_walks_back_over_a_weekend(fdr_stub, cache_days):
    """주말·연휴로 며칠 비어도 가장 최근 것을 찾아낸다."""
    import datetime as dt
    fdr_stub.fail = True
    cache_days.add((dt.date.today() - dt.timedelta(days=4)).strftime("%Y-%m-%d"))

    assert krx_daily.fdr_listing("KRX") is not None


def test_empty_listing_is_treated_as_failure(fdr_stub, cache_days):
    """빈 목록은 '종목이 없다'가 아니다 — KRX-DELISTING 은 FDR 이 404 를 삼켜 빈 값을 준다."""
    import datetime as dt
    fdr_stub.empty = True
    cache_days.add((dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d"))

    df = krx_daily.fdr_listing("KRX-DELISTING")
    assert df is not None and len(df) == 1, "빈 프레임을 그대로 돌려주면 '폐지 0건'이 된다"


def test_gives_up_after_lookback_and_says_so(fdr_stub, cache_days, caplog):
    import logging
    fdr_stub.fail = True
    with caplog.at_level(logging.WARNING, logger=krx_daily.logger.name):
        assert krx_daily.fdr_listing("KRX", lookback=3) is None
    assert any("캐시" in r.message for r in caplog.records)


def test_unknown_kind_is_not_guessed(fdr_stub, cache_days):
    fdr_stub.fail = True
    assert krx_daily.fdr_listing("NASDAQ") is None, \
        "이 캐시 경로를 쓰지 않는 목록까지 임의로 받아 오면 안 된다"


def test_listing_date_is_parsed_like_fdr_does(fdr_stub, cache_days):
    """폴백 스키마를 정상 경로와 맞춘다(KRX-DESC 는 FDR 이 ListingDate 를 파싱한다)."""
    import datetime as dt
    fdr_stub.fail = True
    cache_days.add((dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d"))

    df = krx_daily.fdr_listing("KRX-DESC")
    assert pd.api.types.is_datetime64_any_dtype(df["ListingDate"])


def test_no_fdr_installed_returns_none(monkeypatch):
    monkeypatch.setattr(krx_daily, "_lazy_import", lambda: None)
    monkeypatch.setattr(krx_daily, "_fdr", None)
    assert krx_daily.fdr_listing("KRX") is None
