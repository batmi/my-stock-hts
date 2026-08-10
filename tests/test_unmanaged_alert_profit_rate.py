"""미관리 포지션 손절 경보가 '평가손익률 없음'에 조용해지지 않는가.

[왜 이 테스트인가] 이 경보는 시스템이 손절해 주지 않는 포지션(트레이딩 제한·ETF·미체결
주문 존재 등)의 **마지막 안전망**이다. 그런데 판정이 `float(item.get('evlu_pfls_rt') or 0.0)`
이었다. 필드가 없거나 빈 값이면 0%가 되고, 0%는 손절선(음수)보다 위라 '회복했다'로 읽혀
경보를 건너뛰는 것은 물론 24시간 스로틀까지 풀어 버린다.

증권사 어댑터는 일부 필드를 0/누락으로 준다 — 같은 이유로 pchs_amt 는 이미 수량×평단으로
복원하고 있다. 즉 잔고 데이터가 부실할수록 안전망이 조용해지는 구조였다.
"""
import pytest

from modules.auto_trade import engine as E


def test_uses_reported_rate_when_present():
    assert E.holding_profit_rate({"evlu_pfls_rt": "-8.3"}) == pytest.approx(-8.3)


def test_zero_is_a_real_value_not_missing():
    """실제로 본전인 포지션의 0%까지 '없음'으로 뭉개면 안 된다."""
    assert E.holding_profit_rate({"evlu_pfls_rt": "0"}) == 0.0


@pytest.mark.parametrize("raw", [None, "", "-"])
def test_missing_rate_is_derived_from_average_and_price(raw):
    """필드가 비어도 평단과 현재가가 있으면 직접 구한다 — 0%로 두면 손절 판정이 뒤집힌다."""
    item = {"evlu_pfls_rt": raw, "pchs_avg_pric": "100000", "prpr": "92000"}
    assert E.holding_profit_rate(item) == pytest.approx(-8.0)


@pytest.mark.parametrize("item", [
    {},
    {"pchs_avg_pric": "0", "prpr": "92000"},
    {"pchs_avg_pric": "100000", "prpr": "0"},
    {"pchs_avg_pric": "abc", "prpr": "92000"},
])
def test_unknown_returns_none_not_zero(item):
    """구할 수 없으면 모른다고 답한다 — 0을 돌려주면 손실 중인 포지션이 안전해 보인다."""
    assert E.holding_profit_rate(item) is None


def test_alert_holds_throttle_when_rate_is_unknown():
    """판정 불가일 때 스로틀을 풀면, 다음에 값이 잡혀도 이미 알린 것으로 오해될 수 있다."""
    import inspect
    from modules.auto_trade import AutoTrader
    src = inspect.getsource(AutoTrader._alert_unmanaged_stop)
    assert "holding_profit_rate" in src, "판정이 여전히 or 0.0 폴백을 쓴다"
    head = src.split("holding_profit_rate")[1][:400]
    assert "return" in head and "unmanaged_stop_notified.pop" not in head, \
        "판정 불가 경로에서 스로틀을 건드린다"
