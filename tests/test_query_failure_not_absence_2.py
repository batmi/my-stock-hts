"""'조회 실패'를 '없음'으로 접는 나머지 자리들 (감사 2026-09-06, 배치 52).

api 계층 21개 공개 함수가 실패를 None 으로 돌려준다. 그 호출부를 AST 로 훑어
None 검사가 없는 곳을 찾아낸 결과 두 곳이 실제 결함이었다. 둘 다 **의도는 이미
코드에 적혀 있었고**, `or []` 한 조각이 그 의도를 무력화하고 있었다.
"""
import time
from unittest.mock import patch

import pytest

import api
import config


# ══════════════════════════════════════════════════════════════════════
# 1. 토스 자산 보정 — 미체결 조회 실패가 '가짜 입출금'이 된다
# ══════════════════════════════════════════════════════════════════════

def _trader():
    from modules.auto_trade import trader as trader_mod
    t = trader_mod.AutoTrader.__new__(trader_mod.AutoTrader)
    return t


def test_미체결_조회_실패는_예약현금_0원이_아니다(monkeypatch):
    """`or []` 가 조회 실패를 '미체결 없음'으로 만들면 보정액이 0이 된다.

    [무엇이 걸려 있는가] 이 함수의 독스트링이 직접 적어 뒀다 — 보정하지 않으면
     "자산/원금 계산이 흔들려 입금 자동 감지가 오작동(가짜 입금)하고 손실률이 왜곡된다".
     호출부에는 예외를 받아 `toss_cash_reliable = False` 로 두는 팔이 이미 있는데,
     get_domestic_open_orders 는 실패를 **예외가 아니라 None** 으로 알린다. 그 None 이
     `or []` 에 삼켜져 호출부는 0원을 정상 보정값으로 믿었다.
     그 왜곡은 daily_asset_history 의 net_transfer 로 굳어 며칠을 간다.
    """
    from modules.auto_trade import trader as trader_mod
    monkeypatch.setattr(trader_mod.api, 'get_domestic_open_orders', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="미체결 없음"):
        _trader()._get_toss_open_buy_reserved('11111111', '01')


def test_미체결이_진짜_없으면_예약현금은_0원이다(monkeypatch):
    """진짜 '없음'까지 실패로 만들면 반대편 결함이 된다."""
    from modules.auto_trade import trader as trader_mod
    monkeypatch.setattr(trader_mod.api, 'get_domestic_open_orders', lambda *a, **k: [])
    assert _trader()._get_toss_open_buy_reserved('11111111', '01') == 0


def test_미체결_매수만_예약현금으로_센다(monkeypatch):
    from modules.auto_trade import trader as trader_mod
    rows = [
        {'sll_buy_dvsn_cd': '02', 'rmn_qty': '10', 'ord_unpr': '70000'},   # 매수
        {'sll_buy_dvsn_cd': '01', 'rmn_qty': '10', 'ord_unpr': '70000'},   # 매도 — 제외
    ]
    monkeypatch.setattr(trader_mod.api, 'get_domestic_open_orders', lambda *a, **k: rows)
    assert _trader()._get_toss_open_buy_reserved('11111111', '01') == 700_000


# ══════════════════════════════════════════════════════════════════════
# 2. 수급 프로브 — 조회 실패를 '수급 없음'으로 5분간 굳힌다
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def clean_probe():
    from modules import analysis
    analysis._INV_PROBE_CACHE.clear()
    yield analysis
    analysis._INV_PROBE_CACHE.clear()


_LIST = [('삼성전자', '005930'), ('SK하이닉스', '000660'), ('NAVER', '035420')]


def _inv_row(frgn=1000):
    return [{'stck_bsop_date': '20260905', 'prsn_ntby_qty': '0',
             'frgn_ntby_qty': str(frgn), 'orgn_ntby_qty': '0'}]


def test_수급_조회_실패는_판정을_캐시하지_않는다(clean_probe, monkeypatch):
    """실패를 '수급 없음'으로 세면 5분 캐시에 굳어, API 가 회복돼도 그동안 표가
    OBV 로 폴백한다 — 실패를 캐시에 굳히는 전형이다.

    get_investor_trend 의 계약은 실패=None / 없음=[] 로 이미 갈라져 있다(2026-09-05).
    """
    analysis = clean_probe
    monkeypatch.setattr(analysis.api, 'get_investor_trend', lambda code: None)
    assert analysis._probe_investor_data(_LIST) is False
    assert not analysis._INV_PROBE_CACHE, "조회 실패 판정이 캐시에 굳었다"

    # 회복되면 곧바로 반영된다(캐시가 막지 않는다).
    monkeypatch.setattr(analysis.api, 'get_investor_trend', lambda code: _inv_row())
    assert analysis._probe_investor_data(_LIST) is True


def test_수급이_진짜_비어_있으면_그_판정은_캐시한다(clean_probe, monkeypatch):
    """'없음'은 결론이다 — 매번 다시 묻지 않는다(반복 조회 방지가 이 캐시의 목적)."""
    analysis = clean_probe
    calls = []
    monkeypatch.setattr(analysis.api, 'get_investor_trend',
                        lambda code: calls.append(code) or [])
    assert analysis._probe_investor_data(_LIST) is False
    n = len(calls)
    assert analysis._probe_investor_data(_LIST) is False
    assert len(calls) == n, "'없음' 판정을 캐시하지 않아 매번 다시 물었다"


def test_한_종목이라도_답하면_그_답으로_판정한다(clean_probe, monkeypatch):
    """앞 두 종목이 실패해도 세 번째가 답하면 그것이 결론이다."""
    analysis = clean_probe
    seq = {'005930': None, '000660': None, '035420': _inv_row()}
    monkeypatch.setattr(analysis.api, 'get_investor_trend', lambda code: seq[code])
    assert analysis._probe_investor_data(_LIST) is True
    assert analysis._INV_PROBE_CACHE
