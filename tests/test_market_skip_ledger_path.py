"""시장 필터가 자른 종목이 원장까지 실제로 닿는가 — 판정 자리에서 확인한다.

tests/test_ledger_market_blind.py 는 원장(DB) 쪽을, 이 파일은 **그 행을 만드는 자리**를
 본다. 결함은 DB 가 아니라 trader._analyze_candidate_worker 의 4번(시장 지수 필터링)이
 반환값에 'ledger' 키를 달지 않은 것이었다.
"""
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


class _FakeOrderManager:
    def is_pending(self, code):
        return False

    def pending_odnos(self, code):
        return []


@pytest.fixture
def trader(monkeypatch):
    from modules.auto_trade import trader as trader_mod
    #  Class.__new__ 는 싱글톤을 돌려준다 — object.__new__ 로 빈 객체를 만든다.
    t = object.__new__(trader_mod.AutoTrader)
    t.is_running = True
    t.order_manager = _FakeOrderManager()
    t.market_index_status = {}
    t.set_stock_state = lambda code, state: None
    t._get_stock_market_type = lambda code: "KOSPI"
    monkeypatch.setattr(trader_mod.api, 'nxt_order_window', lambda *a, **k: False)
    monkeypatch.setattr(config, 'USE_MARKET_FILTER', True, raising=False)
    return t


def _run(t):
    return t._analyze_candidate_worker(
        {'code': '005930', 'name': '삼성전자', 'group': 'stocks_kr'},
        holding_codes=set(), rules_map={}, restricted_stocks=set(),
        market_regime_adj={}, safe_delay=0, reentry_hurdles={},
        holdings_dfs={}, holding_groups_map={})


def test_필터가_막은_종목이_원장_행을_들고_돌아온다(trader):
    """종전에는 'ledger' 키가 아예 없어, 필터를 켠 계좌의 원장이 통째로 비었다."""
    trader.market_index_status = {"KOSPI": {'is_healthy': False, 'current': 3000}}
    res = _run(trader)
    assert res['type'] == 'market_skip'
    assert res.get('ledger'), f"원장 행이 없다 — 필터를 켜면 원장이 다시 빈다: {res}"
    assert res['ledger']['outcome'] == 'market'
    assert res['ledger']['code'] == '005930'


def test_상태_캐시가_아예_없어도_원장에_남는다(trader):
    """첫 주기 전·지수 조회 실패도 fail-closed 로 막는 자리다 — 그것도 기록돼야 한다."""
    trader.market_index_status = {}
    res = _run(trader)
    assert res['type'] == 'market_skip'
    assert res.get('ledger', {}).get('outcome') == 'market'


def test_판정_전에_잘렸으므로_점수도_상태도_적지_않는다(trader):
    """분석을 안 한 주기다. 점수를 0 으로, 상태를 아무 글자로 적으면 없는 사실이 생긴다."""
    trader.market_index_status = {"KOSPI": {'is_healthy': False}}
    row = _run(trader)['ledger']
    assert 'score' not in row and 'state' not in row, f"판정하지 않은 값이 적혔다: {row}"


def test_수집_루프가_사유를_가리지_않고_원장을_모은다():
    """market_skip 은 type 분기에서 로그도 안 찍는 갈래다. 원장 수집이 그 분기 **뒤**에
    있으면 이 갈래만 다시 조용해진다 — 분기보다 앞에 있어야 한다."""
    import inspect
    from modules.auto_trade.trader import AutoTrader

    src = inspect.getsource(AutoTrader._analyze_candidates)
    collect = src.index("ledger_rows.append")
    branch = src.index("if res['type'] == 'candidate'")
    assert collect < branch, "원장 수집이 사유별 분기 안으로 들어갔다 — 갈래마다 빠뜨린다"
