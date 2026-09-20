"""해외 일봉 tvDatafeed 폴백 — 같은 티커의 외국 상장(통화 다름)을 후보에서 뺀다(2026-09-20)."""
from unittest.mock import MagicMock, patch

import pandas as pd

from modules import analysis


def _hist_df():
    idx = pd.to_datetime(["2026-09-17 13:30", "2026-09-18 13:30"])
    return pd.DataFrame({"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                         "close": [250.0, 251.0], "volume": [1.0, 1.0]}, index=pd.Index(idx, name="datetime"))


def _run(matches, hist_side_effect):
    tv = MagicMock()
    tv.search_symbol.return_value = matches
    tv.get_hist.side_effect = hist_side_effect
    analysis._TVDATAFEED_EXCHANGE.clear()
    analysis._TVDATAFEED_OVERSEAS_NEG_CACHE.clear()
    with patch.object(analysis, "_get_tvdatafeed", return_value=tv), \
         patch.dict("sys.modules", {"tvDatafeed": MagicMock()}), \
         patch.object(analysis.time, "sleep"):
        out = analysis.fetch_overseas_daily_via_tvdatafeed("TSLA")
    return out, [c.kwargs.get("exchange") for c in tv.get_hist.call_args_list]


def test_외국_거래소_상장은_후보에서_빠진다():
    matches = [{"symbol": "TSLA", "exchange": "NASDAQ"}, {"symbol": "TSLA", "exchange": "NEO"},
               {"symbol": "TSLA", "exchange": "BMV"}]
    # NASDAQ 두 번 다 실패 → 종전엔 NEO(CAD) 로 넘어가 그 가격을 돌려줬다
    out, tried = _run(matches, [None, None, _hist_df()])
    assert out is None and tried == ["NASDAQ", "NASDAQ"]
    assert "TSLA" not in analysis._TVDATAFEED_EXCHANGE


def test_미국_거래소면_종전대로_받는다():
    out, tried = _run([{"symbol": "TSLA", "exchange": "NASDAQ"}], [_hist_df()])
    assert list(out["date"]) == ["20260917", "20260918"] and tried == ["NASDAQ"]
    assert analysis._TVDATAFEED_EXCHANGE["TSLA"] == "NASDAQ"
