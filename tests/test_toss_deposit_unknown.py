"""토스 예수금을 **못 읽은 것**이 '0원'으로 둔갑하지 못하게 한다.

[왜 · 2026-09-08 감사] `api/toss._toss_krw_deposit` 은 조회 실패도 응답 누락도 `0` 을
돌려줬다. 그 0 은 '현금이 없다'와 글자가 같아서 잔고 요약의
`dnca_tot_amt`·`nxdy_excc_amt`·`prvs_rcdl_excc_amt`·`tot_evlu_amt` 로 그대로 흘렀고,
그 총자산이 그날의 기준 자산이 되면 **일일 손실 한도의 분모**와 사이징 기준이 현금만큼
축소된다. 매수여력 0 으로 신규 매수도 조용히 멈춘다.

기존 방어는 못 잡는다. `is_plausible_baseline` 은 '직전 대비 반토막'을 보는데, 현금
비중이 절반 미만이면 통과한다 — 대부분 투자된 계좌가 정확히 그 상태다.

형제 함수(`_toss_domestic_balance`·`_toss_overseas_balance`)는 이미 실패를 None 으로
구분하고 있었다. 예수금만 구멍이었다([[unknown-vs-empty]] · [[toss-balance-sell-gate]]).

지키는 것 넷:
  ① 조회 실패·필드 누락은 None (0 이 아니다)
  ② 잔고 요약은 예수금 칸을 **비우고** 표식을 남긴다 — 보유분은 그대로 준다
     (보유를 버리면 손절·트레일링이 멈춘다)
  ③ `get_deposit_balance` 의 토스 분기도 KIS 와 같은 계약(실패=None)을 말한다
  ④ 상위 집계가 그 표식을 degraded 로 올려 기준선 결정을 미룬다
"""
import pytest

import api
import config
from api import toss as toss_mod
from brokers import toss_api


@pytest.fixture
def toss_mode(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    yield


# ── ① 실패·누락은 None ──────────────────────────────────────────────
def test_deposit_query_failure_is_none_not_zero(monkeypatch):
    def _boom(cur):
        raise toss_api.TossApiError("NETWORK", "네트워크 순단", status=502)

    monkeypatch.setattr(toss_mod.toss_api, "get_buying_power", _boom)
    assert toss_mod._toss_krw_deposit() is None


def test_missing_cash_field_is_none_not_zero(monkeypatch):
    monkeypatch.setattr(toss_mod.toss_api, "get_buying_power", lambda cur: {})
    assert toss_mod._toss_krw_deposit() is None


def test_genuine_zero_cash_is_still_zero(monkeypatch):
    """진짜 0원은 0으로 남아야 한다 — '모름'과 '없음'을 뒤집지 않는다."""
    monkeypatch.setattr(toss_mod.toss_api, "get_buying_power",
                        lambda cur: {"cashBuyingPower": "0"})
    assert toss_mod._toss_krw_deposit() == 0


# ── ② 잔고 요약: 보유는 살리고 예수금 칸만 비운다 ──────────────────
def _holdings_payload():
    return {"items": [{
        "marketCountry": "KR", "symbol": "005930", "name": "삼성전자",
        "quantity": 10, "averagePrice": 70000, "lastPrice": 80000,
        "marketValue": {"amount": 800000}, "profitLoss": {"amount": 100000, "rate": 0.1428},
    }]}


def test_balance_keeps_holdings_but_blanks_deposit_when_unknown(monkeypatch):
    monkeypatch.setattr(toss_mod.toss_api, "get_holdings", lambda: _holdings_payload())
    monkeypatch.setattr(toss_mod, "_toss_krw_deposit", lambda: None)

    output1, output2 = toss_mod._toss_domestic_balance()
    assert output1, "예수금을 못 읽었다고 보유분을 버리면 손절·트레일링이 멈춘다"
    summary = output2[0]
    assert summary.get("_deposit_unknown"), "모른다는 표식이 없다"
    for k in ("dnca_tot_amt", "nxdy_excc_amt", "prvs_rcdl_excc_amt", "tot_evlu_amt"):
        assert k not in summary, f"{k} 에 0원이 채워졌다 — '현금 없음'으로 읽힌다"
    assert summary["scts_evlu_amt"] == "800000", "아는 값(주식 평가액)은 그대로 줘야 한다"


def test_balance_fills_deposit_when_known(monkeypatch):
    monkeypatch.setattr(toss_mod.toss_api, "get_holdings", lambda: _holdings_payload())
    monkeypatch.setattr(toss_mod, "_toss_krw_deposit", lambda: 1_500_000)

    _o1, output2 = toss_mod._toss_domestic_balance()
    summary = output2[0]
    assert "_deposit_unknown" not in summary
    assert summary["dnca_tot_amt"] == "1500000"
    assert summary["tot_evlu_amt"] == str(800000 + 1_500_000)


# ── ③ get_deposit_balance 의 계약이 모드에 따라 갈리지 않는다 ──────
def test_get_deposit_balance_returns_none_on_toss_failure(monkeypatch, toss_mode):
    monkeypatch.setattr(api, "_toss_krw_deposit", lambda: None, raising=False)
    monkeypatch.setattr(api, "_paper_active", lambda: False, raising=False)
    assert api.get_deposit_balance() is None, \
        "KIS 경로는 실패를 None 으로 답한다 — 토스만 0원을 돌려주면 계약이 갈라진다"


def test_get_deposit_balance_returns_value_on_toss_success(monkeypatch, toss_mode):
    monkeypatch.setattr(api, "_toss_krw_deposit", lambda: 2_000_000, raising=False)
    monkeypatch.setattr(api, "_paper_active", lambda: False, raising=False)
    res = api.get_deposit_balance()
    assert res["deposit"] == 2_000_000 and res["order_possible"] == 2_000_000
