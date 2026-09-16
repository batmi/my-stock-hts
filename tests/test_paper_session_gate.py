"""가상투자 체결은 실계좌가 거부할 시간·종목을 체결하지 않는다(2026-09-16, 애프터마켓 정합)."""
from unittest.mock import patch

import pytest

from modules import paper_broker as pb


@pytest.mark.paper_session_gate
def test_no_fill_when_no_market_is_open():
    with patch.object(pb, "_market_open", lambda: False), patch.object(pb, "_etf_untraded", lambda c, n: False):
        res = pb.place_order("buy", "005930", 1, 70000, name="삼성전자")
    assert res["rt_cd"] == "1" and res["msg_cd"] == "PAPER_REJECT" and "열린 시장" in res["msg1"]


@pytest.mark.paper_session_gate
def test_no_fill_for_etf_in_after_market():
    with patch.object(pb, "_market_open", lambda: True), patch.object(pb, "_etf_untraded", lambda c, n: True):
        res = pb.place_order("buy", "069500", 1, 40000, name="KODEX 200")
    assert res["rt_cd"] == "1" and "ETF/ETN" in res["msg1"]


@pytest.mark.paper_session_gate
def test_gate_reads_session_helpers():
    with patch("api.domestic_trading_session_open", lambda: False):
        assert pb._market_open() is False
    with patch("api.domestic_etf_untraded_window", lambda: True), patch("api.is_domestic_etf_etn", lambda c, n: True):
        assert pb._etf_untraded("069500", "KODEX 200") is True
    with patch("api.domestic_etf_untraded_window", lambda: False):
        assert pb._etf_untraded("069500", "KODEX 200") is False
