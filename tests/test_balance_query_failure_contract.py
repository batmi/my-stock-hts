"""잔고 '조회 실패'와 '보유 없음'을 가르는가.

[감사 2026-09-06] api.get_domestic_balance 는 실패를 (None, None) 로 정확히 알려 주는데,
그 형제인 api.get_overseas_balance 는 세 거래소가 모두 실패해도 **빈 목록**을 돌려줬다.
받는 쪽은 그것을 '해외를 안 들고 있다'로 읽는다. 두 소비자
(auto_trade.common.current_holding_qty · reserved_order_monitor._holding_exit_result)는
이미 `is None` 팔을 갖고 있었지만, 만들어 주는 쪽이 없어 **죽은 코드**였다.

같은 부류가 위층에도 있었다 — 조회 실패를 운영자에게 '보유 없음'이라고 알리는 자리들:
장 마감 브리핑(scheduler) · 텔레그램 /잔고 · 손으로 손절하는 매도 화면(trading) ·
보유 복원 제안(holdings_backfill). 모르는 것을 없다고 답하지 않는다.
"""
import pytest
from unittest.mock import patch

import api
import config
from api import account as api_account


_FAIL = {'rt_cd': '9999', 'msg_cd': 'NETERR', 'msg1': 'timeout'}
_OK_EMPTY = {'rt_cd': '0', 'output1': [], 'output2': []}


def _ovs_item(code='AAPL', exc='NASD'):
    return {'ovrs_pdno': code, 'ovrs_item_name': code, 'ovrs_cblc_qty': '5',
            'ord_psbl_qty': '5', 'pchs_avg_pric': '200', '_exchange': exc}


@pytest.fixture
def kis_mode(monkeypatch):
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(api_account, '_paper_active', lambda: False)
    yield


# ══════════════════════════════════════════════════════════════════════
# api 층 — 반환 계약
# ══════════════════════════════════════════════════════════════════════

def test_해외잔고_전거래소_실패는_None이다(kis_mode):
    with patch.object(api, 'call_api', return_value=_FAIL):
        assert api_account.get_overseas_balance('11111111', '01') is None


def test_해외잔고_일부_거래소만_실패해도_None이다(kis_mode):
    """반쪽 목록으로는 '없음'을 결론지을 수 없다.

    보유가 실패한 거래소에 있었을 수 있다. 목록의 **완전성**을 주장할 수 없으면
    조회된 몇 건은 버리고 실패로 답한다 — 국내 형제와 같은 계약이다.
    """
    seq = [{'rt_cd': '0', 'output1': [_ovs_item()]}, _FAIL, _OK_EMPTY]
    with patch.object(api, 'call_api', side_effect=seq):
        assert api_account.get_overseas_balance('11111111', '01') is None


def test_해외잔고_전부_성공하면_목록이다(kis_mode):
    with patch.object(api, 'call_api', side_effect=[
            {'rt_cd': '0', 'output1': [_ovs_item()]}, _OK_EMPTY, _OK_EMPTY]):
        res = api_account.get_overseas_balance('11111111', '01')
    assert isinstance(res, list) and len(res) == 1
    assert res[0]['_exchange'] == 'NASD'


def test_해외_보유가_없으면_빈_목록이다_None이_아니다(kis_mode):
    """진짜 '없음'까지 실패로 만들면 반대편 결함이 된다."""
    with patch.object(api, 'call_api', side_effect=[_OK_EMPTY] * 3):
        assert api_account.get_overseas_balance('11111111', '01') == []


def test_토스_해외잔고_실패도_None이다(monkeypatch):
    """토스 국내 형제(_toss_domestic_balance)는 이미 (None, None) 인데 여기만 [] 였다."""
    from api import toss as api_toss

    def _boom():
        raise api_toss.toss_api.TossApiError("NETERR", "네트워크 오류")

    monkeypatch.setattr(api_toss.toss_api, 'get_holdings', _boom)
    assert api_toss._toss_overseas_balance() is None


# ══════════════════════════════════════════════════════════════════════
# modules.account — 실패를 위로 전한다
# ══════════════════════════════════════════════════════════════════════

def test_fetch_domestic_balance_실패는_None쌍이다(monkeypatch):
    from modules import account as account_mod
    monkeypatch.setattr(account_mod.api, 'get_domestic_balance', lambda *a, **k: (None, None))
    assert account_mod.fetch_domestic_balance('1', '01') == (None, None)


def test_fetch_domestic_balance_깨진_한줄이_목록전체를_지우지_않는다(monkeypatch):
    """종전에는 int(item['hldg_qty']) 가 루프 밖 except 로 튀어 **나머지 종목이 통째로**
    사라졌다. 증권사는 값이 없을 때 키를 주고 빈 문자열을 담는다."""
    from modules import account as account_mod
    rows = [{'pdno': 'A', 'hldg_qty': ''},                 # 읽을 수 없는 줄
            {'pdno': 'B', 'hldg_qty': '10'},
            {'pdno': 'C', 'hldg_qty': '0'}]                # 진짜 0주
    monkeypatch.setattr(account_mod.api, 'get_domestic_balance', lambda *a, **k: (rows, [{}]))
    holdings, _ = account_mod.fetch_domestic_balance('1', '01')
    assert [h['pdno'] for h in holdings] == ['B']


def test_fetch_overseas_balance_는_실패를_그대로_전한다(monkeypatch):
    from modules import account as account_mod
    monkeypatch.setattr(account_mod.api, 'get_overseas_balance', lambda *a, **k: None)
    assert account_mod.fetch_overseas_balance('1', '01') is None


# ══════════════════════════════════════════════════════════════════════
# 운영자에게 '없음'이라고 말하지 않는다
# ══════════════════════════════════════════════════════════════════════

def test_장마감_브리핑은_조회실패를_보유없음으로_알리지_않는다(monkeypatch):
    """텔레그램으로 '보유 종목이 없어…'가 나가면 운영자는 포지션이 정리된 줄 안다.
    마감 후 갭 전에 그 오해가 가장 비싸다."""
    from modules import scheduler
    sent = []
    monkeypatch.setattr(scheduler.api, 'get_domestic_balance', lambda *a, **k: (None, None))
    monkeypatch.setattr(scheduler.api, 'get_deposit_balance', lambda *a, **k: {'d2_deposit': 0})
    monkeypatch.setattr(scheduler.api, 'send_telegram_message', lambda m, **k: sent.append(m))
    monkeypatch.setattr(scheduler.utils, 'system_trading_account', lambda: ('11111111', '01'))

    scheduler.SystemScheduler().execute_daily_closing_report()

    assert sent, "아무 알림도 나가지 않았다"
    assert "보유 종목이 없어" not in sent[0], f"조회 실패를 '보유 없음'으로 알렸다: {sent[0]}"
    assert "조회" in sent[0]


def test_보유복원_제안은_잔고조회_실패를_복원할것없음으로_읽지_않는다(monkeypatch):
    """plan() 은 체결 내역 조회 실패를 RuntimeError 로 올린다(빈 계획 금지). 그런데
    **잔고** 조회 실패는 그 앞에서 `holdings or []` 에 삼켜져 똑같이 빈 계획이 됐다."""
    from modules import holdings_backfill as hb
    monkeypatch.setattr(hb.api, 'get_domestic_balance', lambda *a, **k: (None, None))
    monkeypatch.setattr(hb, 'supports_broker_history', lambda: True)
    summary = hb.sync_account(cano='11111111', acnt_prdt_cd='01')
    assert "복원할 종목이 없다" in (summary['error'] or ""), \
        f"잔고 조회 실패가 '복원할 것 없음'으로 조용히 끝났다: {summary}"
    assert summary['written'] == 0 and summary['partial'] == []
