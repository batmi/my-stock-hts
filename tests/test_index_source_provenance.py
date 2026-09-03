"""시장 필터가 무엇으로 판단했는지 되짚을 수 있어야 한다.

국내 지수는 KRX 확정 봉 위에 KIS·토스·tvDatafeed·yfinance 중 하나를 얹어 만든다.
그 값으로 시장 필터(이평 이탈 → 신규 매수 중단)와 국면 판정이 도는데, 종전에는 출처를
DataFrame.attrs 에 적어 두고 **아무도 읽지 않았고**, 병합 단계에서 그마저 KRX 로 덮였다.
최후 폴백(yfinance)은 최신 거래일 종가를 결측으로 주는 일이 잦아, 매수 중단·재개가
어긋났을 때 먼저 의심해야 할 곳인데 흔적이 남지 않았다.
"""
import pandas as pd
import pytest

from modules import analysis


def _frame(dates, source=None):
    df = pd.DataFrame({"date": dates, "open": 1.0, "high": 1.0, "low": 1.0,
                       "close": 1.0, "volume": 0.0})
    if source:
        df.attrs["source"] = source
    return df


def test_merge_records_both_sources():
    """당일 봉을 얹었으면 뼈대와 당일 값의 출처를 함께 남긴다."""
    hist = _frame(["20260901", "20260902"], "KRX")
    live = _frame(["20260902", "20260903"], "TVDATAFEED")

    out = analysis._merge_index_history(hist, live)

    assert analysis.index_source(out) == "KRX+TVDATAFEED"
    assert list(out["date"]) == ["20260901", "20260902", "20260903"]


def test_merge_keeps_hist_source_when_nothing_was_added():
    """실시간 소스가 더 최신 날짜를 못 주면 뼈대의 출처 그대로다."""
    hist = _frame(["20260901", "20260902"], "KRX")
    live = _frame(["20260902"], "YFINANCE")

    out = analysis._merge_index_history(hist, live)
    assert analysis.index_source(out) == "KRX"


def test_merge_passes_through_single_source():
    live = _frame(["20260902"], "TOSS")
    assert analysis.index_source(analysis._merge_index_history(None, live)) == "TOSS"


def test_index_source_is_safe_on_missing_values():
    assert analysis.index_source(None) is None
    assert analysis.index_source(_frame(["20260902"])) is None


def test_last_resort_fallback_warns_once_per_day(monkeypatch, caplog):
    """yfinance 폴백은 거래일마다 지수당 한 번 경고로 남는다(5분 캐시마다 울리면 소음)."""
    monkeypatch.setattr(analysis, "_INDEX_LAST_RESORT_WARNED", set())
    monkeypatch.setattr(analysis, "_current_market_day", lambda: "20260904")

    with caplog.at_level("WARNING"):
        analysis._warn_index_last_resort("KOSPI", "^KS11")
        analysis._warn_index_last_resort("KOSPI", "^KS11")
        analysis._warn_index_last_resort("KOSDAQ", "^KQ11")

    warned = [r for r in caplog.records if "최후 폴백" in r.getMessage()]
    assert len(warned) == 2, "같은 지수·같은 날은 한 번만 알린다"
    assert any("KOSPI" in r.getMessage() for r in warned)
    assert any("KOSDAQ" in r.getMessage() for r in warned)


@pytest.mark.parametrize("source,flagged", [
    ("KRX+YFINANCE", True),
    ("YFINANCE", True),
    ("KRX+TVDATAFEED", False),
    ("KIS", False),
    (None, False),
])
def test_status_screen_flags_only_the_last_resort_source(source, flagged):
    """상태 화면은 최후 폴백으로 받은 지수만 밝힌다(평상시 출처는 읽는 데 방해)."""
    from modules.auto_trade import trader as at

    note = at.index_source_note({"is_healthy": True, "current": 2500.0, "source": source})
    assert bool(note) is flagged
    if flagged:
        assert "yfinance" in note


def test_status_note_is_safe_on_garbage():
    from modules.auto_trade import trader as at

    assert at.index_source_note(None) == ""
    assert at.index_source_note({}) == ""
    assert at.index_source_note("not a dict") == ""
