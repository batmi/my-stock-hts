"""시장 필터가 무엇으로 판단했는지 되짚을 수 있어야 한다.

국내 지수는 KRX 확정 봉 위에 KIS·토스·tvDatafeed·yfinance 중 하나를 얹어 만든다.
그 값으로 시장 필터(이평 이탈 → 신규 매수 중단)와 국면 판정이 도는데, 종전에는 출처를
DataFrame.attrs 에 적어 두고 **아무도 읽지 않았고**, 병합 단계에서 그마저 KRX 로 덮였다.
최후 폴백(yfinance)은 최신 거래일 종가를 결측으로 주는 일이 잦아, 매수 중단·재개가
어긋났을 때 먼저 의심해야 할 곳인데 흔적이 남지 않았다.
"""
import pandas as pd

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
    live = _frame(["20260902"], "TVDATAFEED")

    out = analysis._merge_index_history(hist, live)
    assert analysis.index_source(out) == "KRX"


def test_merge_passes_through_single_source():
    live = _frame(["20260902"], "TOSS")
    assert analysis.index_source(analysis._merge_index_history(None, live)) == "TOSS"


def test_index_source_is_safe_on_missing_values():
    assert analysis.index_source(None) is None
    assert analysis.index_source(_frame(["20260902"])) is None
