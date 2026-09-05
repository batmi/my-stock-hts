"""계좌 라우팅이 워커 스레드를 건너서도 유지되는지 검증한다.

[배경] context.trade_context는 threading.local()이라 스레드 간 상속되지 않는다.
자동매매는 매도 판정을 ThreadPoolExecutor(at_sell)로 병렬 처리하고 그 안에서
손절·트레일링 매도 주문을 낸다. 계좌 컨텍스트를 전파하지 않으면 부모가
AccountContext(자동계좌) 안에 있어도 워커에서는 use_auto_account가 미설정이라
getattr(..., False) 폴백에 걸려 **수동 계좌로 주문이 나간다**.

실전(mode 2)에서 수동/자동 계좌가 분리돼 있으면 결과가 비대칭이라 특히 위험하다.
신규 매수는 AutoTrader 본체 스레드라 자동 계좌로 가는데, 매도만 수동 계좌로 가서
'자동 계좌로 사고 자동 계좌에서 못 파는' 상태가 된다. 손절이 수량 0으로 조용히
취소되거나, 같은 종목의 수동 보유분이 대신 팔린다.
"""
import concurrent.futures
import threading

from unittest.mock import patch

import pytest

import api
import config
from core import context
from core import utils

MAIN_CANO = "11111111"
AUTO_CANO = "22222222"


@pytest.fixture
def separated_accounts(monkeypatch):
    """실전 모드 + 수동/자동 계좌·앱키가 분리된 환경."""
    s = config.session
    for k, v in (
        ('is_toss', False), ('is_paper', False),
        ('cano', MAIN_CANO), ('acnt_prdt_cd', '01'),
        ('auto_cano', AUTO_CANO), ('auto_acnt_prdt_cd', '01'),
        ('auto_app_key', 'AUTO_KEY'), ('auto_app_secret', 'AUTO_SEC'),
        ('real_app_key', 'MAIN_KEY'), ('real_app_secret', 'MAIN_SEC'),
    ):
        monkeypatch.setattr(s, k, v, raising=False)
    monkeypatch.setattr(context.trade_context, 'use_auto_account', False, raising=False)
    yield s
    context.trade_context.use_auto_account = False


def _resolved_cano():
    cano, _ = api._prepare_account_params(None, None)
    return cano


def test_worker_thread_loses_account_context_without_the_wrapper(separated_accounts):
    """전파 래퍼가 없으면 워커는 수동 계좌로 떨어진다 — 이 결함의 존재 근거."""
    got = []
    with utils.AccountContext(AUTO_CANO):
        assert _resolved_cano() == AUTO_CANO, "제출 스레드부터 틀렸다면 이 테스트는 무의미하다"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            got.append(ex.submit(_resolved_cano).result())
    assert got == [MAIN_CANO], (
        "threading.local이 상속되지 않는다는 전제가 깨졌다 — 이 테스트가 지키는 "
        "불변식(래퍼 필요성)이 바뀌었으니 아래 테스트들을 재검토할 것")


def test_inherit_account_context_carries_the_account_into_workers(separated_accounts):
    """래퍼를 씌우면 워커에서도 자동 계좌가 유지된다."""
    with utils.AccountContext(AUTO_CANO):
        task = utils.inherit_account_context(_resolved_cano)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            got = [f.result() for f in [ex.submit(task) for _ in range(6)]]
    assert got == [AUTO_CANO] * 6, f"워커가 수동 계좌로 샜다: {got}"


def test_wrapper_restores_the_previous_value_in_the_worker(separated_accounts):
    """워커 스레드가 재사용돼도 이전 값을 되돌려 다음 작업을 오염시키지 않는다."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with utils.AccountContext(AUTO_CANO):
            pool.submit(utils.inherit_account_context(_resolved_cano)).result()
        # 컨텍스트 밖에서 제출하면 수동 계좌여야 한다(앞 작업의 잔상이 남으면 안 된다)
        assert pool.submit(_resolved_cano).result() == MAIN_CANO
    finally:
        pool.shutdown()


def test_wrapper_captures_at_submit_time_not_at_call_time(separated_accounts):
    """캡처 시점은 '제출 스레드에서 래핑한 순간'이다."""
    with utils.AccountContext(AUTO_CANO):
        task = utils.inherit_account_context(_resolved_cano)
    # 래핑 이후 컨텍스트를 떠났어도, 래핑 당시의 계좌를 그대로 쓴다
    assert task() == AUTO_CANO


def test_system_trading_account_matches_the_scattered_ternary(separated_accounts):
    """utils.system_trading_account()가 trader.py의 기존 계좌 선택과 같은 답을 준다."""
    s = config.session
    assert utils.system_trading_account()[0] == s.auto_cano == AUTO_CANO


def test_system_trading_account_falls_back_when_auto_is_unset(separated_accounts, monkeypatch):
    """AUTO_ACC_NUM 미설정(계좌 미분리) 환경에서는 수동 계좌로 떨어진다."""
    monkeypatch.setattr(config.session, 'auto_cano', "", raising=False)
    monkeypatch.setattr(config.session, 'auto_acnt_prdt_cd', "", raising=False)
    assert utils.system_trading_account() == (MAIN_CANO, '01')


def test_send_order_pins_the_auto_account_from_any_thread(separated_accounts, monkeypatch):
    """OrderManager.send_order는 호출 스레드와 무관하게 자동 계좌로 발주한다.

    시스템 트레이딩의 모든 주문(신규 매수·피라미딩·손절/트레일링 매도)이 이 함수
    하나를 지나므로, 여기가 마지막 방어선이다.
    """
    from modules.auto_trade import engine

    seen = []

    def fake_place_order(market, action, code, qty, price, ord_dvsn, exchange_code=None):
        seen.append((action, _resolved_cano(), utils.get_common_headers("TTTC0802U")["appKey"]))
        return {"rt_cd": "1", "msg1": "probe", "msg_cd": "PROBE", "output": {}}

    monkeypatch.setattr(api, 'place_order', fake_place_order)

    class _Trader:
        trade_history = []
        trailing_stop_cache = {}
        _lock = threading.RLock()

        def log(self, *a, **k):
            pass

    om = engine.OrderManager.__new__(engine.OrderManager)
    om.trader = _Trader()
    om._lock = threading.RLock()
    om.pending_orders = {}
    om.orders_sent_count = 0
    om.order_fail_alerted = {}

    # 계좌 컨텍스트를 전혀 잡지 않은 워커 스레드에서 발주한다(at_sell 워커와 동일 조건)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="at_sell") as ex:
        for side in ("sell", "buy"):
            ex.submit(om.send_order, "005930", 1, side, name="삼성전자", price=70000).result()

    assert seen, "place_order가 호출되지 않았다"
    for action, cano, appkey in seen:
        assert cano == AUTO_CANO, f"{action} 주문이 수동 계좌로 나갔다: {cano}"
        assert appkey == "AUTO_KEY", f"{action} 주문이 수동 앱키를 썼다: {appkey}"


# ==========================================================
# [감사 2026-09-04] AI 매매 복기도 계좌를 타고 가야 한다
# ==========================================================

def test_trading_autopsy_thread_carries_the_trade_account(separated_accounts):
    """복기 스레드가 매도 체결의 계좌 컨텍스트를 물고 가야 한다.

    복기는 db.get_latest_buy_trade 로 매수 시점·점수를 읽는데 그 조회는 계좌로 갈린다.
    맨 스레드로 띄우면 threading.local 이 상속되지 않아 수동 계좌를 뒤지고, 자동매매가
    산 종목의 매수 기록을 못 찾아 리포트가 '알 수 없음'인 채 AI 에게 넘어간다.
    """
    seen = {}

    def _autopsy_body(code, name, record):
        seen['use_auto'] = getattr(context.trade_context, 'use_auto_account', False)

    #  체결 계좌(AUTO_CANO) 안에서 캡처한다 — 감시 루프의 기본값(수동)을 싸면 의미가 없다.
    with utils.AccountContext(AUTO_CANO):
        wrapped = utils.inherit_account_context(_autopsy_body)

    t = threading.Thread(target=wrapped, args=("005930", "삼성전자", {}), daemon=True)
    t.start()
    t.join(timeout=3)

    assert seen.get('use_auto') is True, "복기가 수동 계좌를 뒤진다"


def test_autopsy_call_site_is_wrapped():
    """호출 지점이 실제로 래핑되어 있는지 — 원래 결함이 있던 자리를 못 박는다."""
    import inspect

    from modules.auto_trade import conclusion

    src = inspect.getsource(conclusion.ConclusionMonitor)
    idx = src.find("_send_trading_autopsy, args=")
    assert idx == -1, "복기 스레드에 맨 메서드를 그대로 넘기고 있다(계좌 컨텍스트 유실)"
    assert "utils.inherit_account_context(self._send_trading_autopsy)" in src


# ==========================================================
# [2026-09-05] cano 를 아는 함수는 컨텍스트도 함께 세운다
# ==========================================================
def test_보유수량_조회는_스스로_계좌_컨텍스트를_세운다(separated_accounts):
    """cano 를 TR 파라미터로 넘기는 것만으로는 부족하다.

    **어느 앱키·토큰으로 나가는가**는 threading.local(use_auto_account)이 정한다
    (core.utils.get_common_headers · api.auth.get_current_token · api.http._real_bucket_key).
    current_holding_qty 는 제한 정리 추적처럼 **새로 띄운 데몬 스레드**에서 불리는데,
    그 스레드에서 플래그는 미설정(=수동)이라 자동 계좌 잔고를 수동 앱키로 묻게 된다.
    계좌가 갈린 실전(mode 2)에서 그 조회는 실패하고, 실패는 None 이 되어 호출부가
    '모름 → 제한 유지'로 굳는다 — 그 종목의 손절·트레일링이 영영 멈춘다.
    """
    import concurrent.futures

    from modules import auto_trade

    seen = {}

    def _fake_balance(cano, acnt, *a, **k):
        seen['use_auto'] = getattr(context.trade_context, 'use_auto_account', False)
        seen['cano'] = cano
        return ([{'pdno': '005930', 'hldg_qty': '7'}], [])

    with patch.object(auto_trade.common.api, 'get_domestic_balance', side_effect=_fake_balance):
        # 컨텍스트가 없는 **새 스레드**에서 자동 계좌를 묻는다(실제 호출 조건과 같다).
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            qty = ex.submit(auto_trade.current_holding_qty,
                            '005930', config.session.auto_cano,
                            config.session.auto_acnt_prdt_cd, False).result()

    assert qty == 7
    assert seen['cano'] == config.session.auto_cano
    assert seen['use_auto'] is True, (
        "자동 계좌를 묻는데 수동 앱키 컨텍스트로 나갔다")


def test_보유수량_조회가_끝나면_컨텍스트를_되돌린다(separated_accounts):
    """컨텍스트를 남기면 같은 스레드의 다음 호출이 남의 계좌로 나간다."""
    from modules import auto_trade

    context.trade_context.use_auto_account = False
    with patch.object(auto_trade.common.api, 'get_domestic_balance',
                      return_value=([], [])):
        auto_trade.current_holding_qty('005930', config.session.auto_cano,
                                       config.session.auto_acnt_prdt_cd, False)
    assert getattr(context.trade_context, 'use_auto_account', False) is False


# ---------------------------------------------------------------------------
# 지수 캐시의 백그라운드 재검증 스레드 (2026-09-05)
#
# `analysis._trigger_async_refresh` 는 stale 캐시를 그대로 서빙한 뒤 뒤에서 1스레드로
# 재조회한다. 그 스레드가 부르는 _fetch_domestic_index_data 는 모드 1/2 에서
# **KIS 지수 차트 TR**(api.get_domestic_index_chart)을 탄다 — 계좌번호는 안 쓰지만
# 앱키·토큰·TPS 버킷은 use_auto_account 가 고른다.
#
# 이 경로는 자동매매 루프에서 걸린다:
#     engine/trader → analysis.get_market_regime → get_domestic_index_data
#                   → 캐시 stale → _trigger_async_refresh
# 안 싸면 자동매매가 유발한 조회가 **수동 앱키**로 나가고 수동 버킷의 TPS 를 깎는다.
#
# 이 자리는 AccountContext 블록 안에 있지 않아 test_worker_thread_contract 의 구문
# 검사(AccountContext 안의 spawn)에 잡히지 않는다 — 그래서 여기서 따로 못박는다.
def test_지수_비동기_재검증이_계좌_컨텍스트를_들고_간다(monkeypatch):
    import threading as _th

    from modules import analysis

    monkeypatch.setattr(analysis, "_index_cache_enabled", lambda: True)
    done = _th.Event()
    seen = {}

    def _fake_fetch(market_type):
        seen['use_auto'] = getattr(context.trade_context, 'use_auto_account', False)
        done.set()
        return None

    monkeypatch.setattr(analysis, "_fetch_domestic_index_data", _fake_fetch)
    monkeypatch.setattr(analysis, "_store_index_cache", lambda *a, **k: None)
    analysis._INDEX_REFRESH_INFLIGHT.pop("KOSPI", None)

    prev = getattr(context.trade_context, "use_auto_account", False)
    try:
        context.trade_context.use_auto_account = True
        analysis._trigger_async_refresh("KOSPI")
        assert done.wait(5), "재검증 워커가 돌지 않았다"
    finally:
        context.trade_context.use_auto_account = prev
        analysis._INDEX_REFRESH_INFLIGHT.pop("KOSPI", None)

    assert seen['use_auto'] is True, (
        "지수 재검증이 수동 앱키로 나갔다 — 자동매매가 유발한 조회다")


def test_지수_재검증_래퍼가_소스에_남아_있다():
    """동작 검사만으로는 '되돌림'을 늦게 안다 — 구문으로도 못박는다."""
    import ast

    from modules import analysis

    tree = ast.parse(open(analysis.__file__.replace(".pyc", ".py"), encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_trigger_async_refresh")
    targets = [ast.unparse(k.value)
               for n in ast.walk(fn) if isinstance(n, ast.Call)
               and ast.unparse(n.func).endswith("Thread")
               for k in n.keywords if k.arg == "target"]
    assert targets, "재검증 스레드 생성을 찾지 못했다 — 검사기가 낡았다"
    for t in targets:
        assert "_task" in t or "inherit_account_context" in t, (
            f"감싸지 않은 채 띄운다: {t}")
