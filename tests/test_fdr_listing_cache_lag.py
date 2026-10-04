"""FDR 상장목록이 **매 거래일 몇 시간씩** 죽던 것을 견디게 한다.

[무엇이 문제였나 · 2026-09-08 진단]
`fdr.StockListing` 은 KRX 에서 목록을 받지 않는다. ① KRX 에 '최종 영업일(max_work_dt)'을
묻고 ② **그 날짜의 CSV** 를 제3자 GitHub 저장소에서 내려받는다. ②는 장 마감 뒤 올라오는
파일이라, KRX 가 오늘을 영업일이라고 답한 순간부터 그 파일이 올라오기 전까지 매 거래일
404 가 난다(실측: 2026-09-08 오늘 404 · 어제 이전은 전부 200).

그동안 탐색 메뉴(modules/manage/discover)가 통째로 죽었고, 씨드 감사도 표본을 만들지
못했다. 죽은 엔드포인트가 아니라 **날마다 되풀이되는 시차**이므로 '있는 것 중 가장 최근
것'을 쓴다 — 상장목록은 하루 사이 거의 변하지 않는다.

[2026-10-04] fdr.StockListing 을 더는 부르지 않는다 — 날짜를 알아내려고 매번 data.krx.co.kr(약관 위반으로
IP 를 차단당한 도메인)에 타임아웃 없이 묻고, 실패하면 응답 HTML 을 print 했다. 같은 CSV 를 저장소에서
오늘부터 거슬러 직접 받는다(타임아웃 있음).

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
def cache_days(monkeypatch):
    """캐시 저장소가 가진 날짜 집합을 흉내 낸다. 반환된 set 을 테스트가 채운다. 요청 URL 은 .asked 에 남는다."""
    class _Days(set):
        asked = []

    available = _Days()
    available.asked = []

    def _get(url):
        available.asked.append(url)
        day = str(url).rsplit("/", 1)[-1].replace(".csv", "")
        if day not in available:
            raise OSError(f"HTTP 404: {url}")
        return "idx,Code,Name,ListingDate\n0,000660,SK하이닉스,1996-12-26\n"

    monkeypatch.setattr(krx_daily, "_http_get_text", _get)
    return available


def _day(n):
    import datetime as dt
    return (dt.date.today() - dt.timedelta(days=n)).strftime("%Y-%m-%d")


def test_todays_file_is_used_without_asking_data_krx(cache_days):
    cache_days.add(_day(0))
    df = krx_daily.fdr_listing("KRX")
    assert list(df["Code"]) == ["000660"]
    assert krx_daily.last_listing_date("KRX") == _day(0)
    assert all(u.startswith(krx_daily._FDR_CACHE_BASE) for u in cache_days.asked), \
        "data.krx.co.kr 등 저장소 밖 주소에 묻지 않는다"


def test_todays_missing_file_falls_back_to_yesterday(cache_days):
    """오늘 파일이 없으면 어제 것으로 — 이것이 매 거래일 반복되는 그 창이다."""
    cache_days.add(_day(1))
    df = krx_daily.fdr_listing("KRX")
    assert df is not None and len(df) == 1
    assert list(df["Code"]) == ["000660"]


def test_walks_back_over_a_weekend(cache_days):
    """주말·연휴로 며칠 비어도 가장 최근 것을 찾아낸다."""
    cache_days.add(_day(4))
    assert krx_daily.fdr_listing("KRX") is not None


def test_gives_up_after_lookback_and_says_so(cache_days, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger=krx_daily.logger.name):
        assert krx_daily.fdr_listing("KRX", lookback=3) is None
    assert any("캐시" in r.message for r in caplog.records)
    assert krx_daily.last_listing_date("KRX") is None


def test_unknown_kind_is_not_guessed(cache_days):
    assert krx_daily.fdr_listing("NASDAQ") is None, \
        "이 캐시 경로를 쓰지 않는 목록까지 임의로 받아 오면 안 된다"
    assert not cache_days.asked


def test_listing_date_is_parsed_like_fdr_does(cache_days):
    """스키마를 FDR StockListing 과 맞춘다(KRX-DESC 는 FDR 이 ListingDate 를 파싱한다)."""
    cache_days.add(_day(1))
    df = krx_daily.fdr_listing("KRX-DESC")
    assert pd.api.types.is_datetime64_any_dtype(df["ListingDate"])


def test_app_never_imports_financedatareader():
    """실행 경로는 FDR 라이브러리를 적재하지 않는다(감사 도구만 쓴다)."""
    import ast, inspect
    tree = ast.parse(inspect.getsource(krx_daily))
    names = [a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names] + [getattr(n, "module", "") or "" for n in ast.walk(tree)
                                   if isinstance(n, ast.ImportFrom)]
    assert not any("FinanceDataReader" in x for x in names)
