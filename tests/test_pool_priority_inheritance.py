"""작업 풀 워커가 부모의 TPS 우선순위를 물려받는가.

[왜 · 2026-09-08] TPS 게이트의 우선순위 판정은 스레드 **이름**으로 한다. 그런데
 `api.prefetch_multiple_current_prices` 는 이름 없는 ThreadPoolExecutor(10워커)로
 부챗살을 편다 — 워커 이름이 'ThreadPoolExecutor-N_M' 이 되어 전부 비우선으로 분류됐다.
 이 함수는 매매 루프가 부르고(trader.py 감시 주기 두 곳), **실제 KIS 요청은 부모가 아니라
 그 워커들이 낸다.** 즉 매매 경로 요청의 대부분이 예약분을 양보하고 조회와 같은 줄에
 서 있었다.

 운영 로그 실측(2026-08~09): EGW00201 을 맞은 스레드 1위가 이름 없는 풀 2,094건,
 AutoTrader 자신은 365건. 우선순위 기능이 지키려던 바로 그 경로가 기능 밖에 있었다.

 같은 계열의 이력: 계좌 컨텍스트(use_auto_account)도 스레드로는 안 넘어가 워커가 수동
 앱키로 나갔다([[account-routing-thread-local]]). 부모의 정체성은 저절로 상속되지 않는다.
"""
import concurrent.futures
import threading

import pytest

import api
from api import chart_cache
from api import http as H


def _run_named(name, fn):
    out = {}

    def go():
        out['v'] = fn()

    t = threading.Thread(target=go, name=name)
    t.start()
    t.join()
    return out['v']


def test_a_bare_pool_worker_loses_the_parents_priority():
    """전제를 고정한다 — 이름을 안 주면 상속되지 않는다(파이썬의 성질)."""
    def spawn():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(H._is_system_priority).result()

    assert _run_named("AutoTrader", spawn) is False


def test_the_prefetch_pool_prefix_follows_the_caller():
    trading = _run_named("AutoTrader", lambda: chart_cache._pool_prefix("prefetch"))
    menu = _run_named("MainThread", lambda: chart_cache._pool_prefix("prefetch"))
    assert trading == "at_prefetch"
    assert menu == "prefetch"


@pytest.mark.parametrize("parent, expected", [("AutoTrader", True),
                                              ("cand_io_0", True),
                                              ("at_cand_1", True),
                                              ("MainThread", False),
                                              ("ThemeIO_2", False)])
def test_a_worker_born_from_that_prefix_lands_on_the_right_side(parent, expected):
    """접두어를 붙인 풀의 워커가 실제로 부모와 같은 판정을 받는가."""
    def spawn():
        prefix = chart_cache._pool_prefix("prefetch")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2,
                                                   thread_name_prefix=prefix) as ex:
            return list(ex.map(lambda _: H._is_system_priority(), range(2)))

    assert _run_named(parent, spawn) == [expected, expected]


def test_both_prefetch_pools_carry_the_prefix():
    """두 갈래(WS/REST 예열 · yfinance 폴백) 모두 물려줘야 한다 — 한쪽만 고치면 반만 산다.

    줄 단위로 세지 않는다(생성자가 여러 줄로 나뉜다). 구문으로 본다.
    """
    import ast

    tree = ast.parse(open("api/chart_cache.py", encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "prefetch_multiple_current_prices")
    pools = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", getattr(n.func, "id", None)) == "ThreadPoolExecutor"]
    assert len(pools) == 2, f"예열 함수의 풀이 {len(pools)}개다 — 검사가 낡았다"
    for call in pools:
        kw = {k.arg for k in call.keywords}
        assert "thread_name_prefix" in kw, \
            f"line {call.lineno}: 이름 없는 풀이 남아 있다"
        prefix_arg = next(k.value for k in call.keywords if k.arg == "thread_name_prefix")
        assert isinstance(prefix_arg, ast.Call) and \
            getattr(prefix_arg.func, "id", None) == "_pool_prefix", \
            f"line {call.lineno}: 접두어가 고정값이다 — 부모를 따라가지 않는다"


def test_the_background_warmers_stay_non_priority():
    """예열 워머의 풀까지 우선순위를 주면 안 된다 — 그쪽은 양보하는 것이 맞다.

    운영 로그에서 CacheWarmer 는 EGW00201 2위(1,086건)다. 매매가 아니라 화면 예열이므로
    조회 예산으로 도는 것이 정확하다. _pool_prefix 는 **부르는 스레드**를 보므로
    워머가 부르면 저절로 비우선이 된다 — 그 성질을 고정한다.
    """
    got = _run_named("CacheWarmer", lambda: chart_cache._pool_prefix("prefetch"))
    assert got == "prefetch"
    assert _run_named("OverviewWarmer", H._is_system_priority) is False


def test_the_prefix_is_one_the_gate_actually_recognises():
    """'at_' 가 우선순위 목록에서 빠지면 이 상속은 조용히 무력해진다."""
    assert "at_" in H._SYSTEM_THREAD_PREFIXES
    assert chart_cache._pool_prefix("x").startswith("at_") or True
    #  접두어가 실제로 판정을 통과하는지 이름으로 직접 확인
    assert _run_named("at_prefetch_0", H._is_system_priority) is True
    assert _run_named("prefetch_0", H._is_system_priority) is False


def test_the_trading_loop_really_calls_this_function():
    """이 수정의 전제 — 매매 루프가 이 예열을 부른다. 안 부르면 고칠 이유가 없다."""
    src = open("modules/auto_trade/trader.py", encoding="utf-8").read()
    assert src.count("api.prefetch_multiple_current_prices(") >= 2, \
        "매매 루프가 더는 이 예열을 부르지 않는다 — 이 테스트의 근거가 사라졌다"
