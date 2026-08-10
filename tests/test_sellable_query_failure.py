"""매도 가능 수량 '조회 실패'를 '매도 불가'로 읽지 않는가.

[왜 이 테스트인가] fetch_sellable_quantity 는 조회 실패와 진짜 0을 똑같이 0으로 돌려줬고,
자동 청산 경로는 0을 '팔 수 없는 상태'로 읽어 **매도를 중단**했다. 즉 일시적 API 오류가
손절을 거르는 결과로 이어진다.

방향이 거꾸로였다는 점이 핵심이다 — 매수 경로는 같은 상황에서 예수금 폴백으로 주문을 내고
수동 매매 화면도 잔고 수량으로 폴백하는데, 정작 자동 청산만 멈췄다. 추세추종에서 못 파는
비용은 못 사는 비용보다 훨씬 크다.
"""
from unittest.mock import patch

import pytest

import api


def _resp(rt_cd="0", rows=None):
    return {"rt_cd": rt_cd, "output1": rows if rows is not None else []}


@pytest.fixture(autouse=True)
def kis_mode(monkeypatch):
    """관찰모드·토스 분기를 타지 않고 KIS 경로를 검사한다."""
    monkeypatch.setattr(api, "_paper_active", lambda: False, raising=False)
    monkeypatch.setattr(api.config.session, "is_toss", False, raising=False)
    monkeypatch.setattr(api.config.session, "is_simulation", True, raising=False)


def test_api_failure_returns_none_not_zero():
    """실패를 0으로 돌려주면 호출부가 '보유 0'으로 단정한다."""
    with patch.object(api, "call_api", return_value=_resp(rt_cd="1")):
        assert api.fetch_sellable_quantity("005930") is None


def test_genuine_zero_is_still_zero():
    """증권사가 '주문가능 0'이라고 답한 것은 사실이다 — None으로 뭉개면 안 된다."""
    rows = [{"pdno": "005930", "ord_psbl_qty": "0"}]
    with patch.object(api, "call_api", return_value=_resp(rows=rows)):
        assert api.fetch_sellable_quantity("005930") == 0


def test_normal_quantity_passes_through():
    rows = [{"pdno": "005930", "ord_psbl_qty": "37"}]
    with patch.object(api, "call_api", return_value=_resp(rows=rows)):
        assert api.fetch_sellable_quantity("005930") == 37


def test_missing_from_response_is_unknown_not_zero():
    """이 조회는 첫 페이지만 본다. 보유 중인데 응답에 없으면 페이징·스냅샷 불일치다."""
    rows = [{"pdno": "000660", "ord_psbl_qty": "10"}]
    with patch.object(api, "call_api", return_value=_resp(rows=rows)):
        assert api.fetch_sellable_quantity("005930") is None


def test_sell_path_falls_back_to_held_quantity_on_failure():
    """조회 실패 시 자동 청산이 보유 수량으로 진행되는지(회귀 방지).

    실제 주문 흐름 전체를 세우는 대신, 그 분기가 코드에 존재하고 보유 수량을 쓰는지
    확인한다 — 이 경로가 사라지면 손절이 조용히 걸러진다.
    """
    import inspect
    from modules.auto_trade import AutoTrader
    src = inspect.getsource(AutoTrader._check_sell_conditions)
    assert "real_qty is None" in src, "조회 실패 분기가 없다"
    assert "hldg_qty" in src.split("real_qty is None")[1][:400], "보유 수량으로 폴백하지 않는다"


def test_manual_screen_handles_none():
    """수동 매도 화면도 None에서 터지지 않고 잔고로 폴백해야 한다."""
    import inspect
    from modules import trading
    src = inspect.getsource(trading)
    assert "max_qty is None" in src
