"""주기 설정 스냅샷 — 한 매매 주기는 시작 시점의 설정 한 벌로 끝까지 판단한다.

메뉴·텔레그램이 주기 도중에 설정을 바꾸면 종전에는 같은 주기의 앞 종목과 뒤 종목이 다른
값으로 판단될 수 있었다. 바뀐 값은 다음 주기에 반영되어야 하고, 설정 화면(다른 스레드)은
고친 값을 바로 봐야 한다.
"""
import concurrent.futures
import inspect
import os
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import utils  # noqa: E402


def test_snapshot_freezes_reads_in_this_thread_only():
    live_before = config.SELL_STRATEGY["SELL_SCORE"]
    seen_other = {}
    try:
        with config.cycle_snapshot():
            # 주기 도중 다른 스레드(메뉴)가 설정을 바꾼다
            t = threading.Thread(target=lambda: config.SELL_STRATEGY.__setitem__("SELL_SCORE", 1.5))
            t.start(); t.join()
            assert config.SELL_STRATEGY["SELL_SCORE"] == live_before      # 이 주기는 그대로
            t2 = threading.Thread(target=lambda: seen_other.setdefault("v", config.SELL_STRATEGY["SELL_SCORE"]))
            t2.start(); t2.join()
            assert seen_other["v"] == 1.5                                  # 메뉴는 바로 본다
        assert config.SELL_STRATEGY["SELL_SCORE"] == 1.5                   # 다음 주기에 반영
        with config.cycle_snapshot():
            assert config.SELL_STRATEGY["SELL_SCORE"] == 1.5
    finally:
        config.SELL_STRATEGY["SELL_SCORE"] = live_before


def test_scalar_fields_and_config_helpers_follow_the_snapshot():
    keep = config.SYSTEM_MAX_HOLDINGS
    keep_ratio = config.SYSTEM_INVEST_PER_STOCK
    try:
        config.settings.SYSTEM_INVEST_PER_STOCK = 0.0                      # 0 = 슬롯 균등 분할
        with config.cycle_snapshot():
            config.settings.SYSTEM_MAX_HOLDINGS = keep + 3                  # 다른 경로로 바뀌어도
            assert config.SYSTEM_MAX_HOLDINGS == keep
            assert config.resolve_invest_ratio() == 1.0 / keep              # config 내부 함수도
        assert config.SYSTEM_MAX_HOLDINGS == keep + 3
        assert config.resolve_invest_ratio() == 1.0 / (keep + 3)
    finally:
        config.settings.SYSTEM_MAX_HOLDINGS = keep
        config.settings.SYSTEM_INVEST_PER_STOCK = keep_ratio


def test_nested_snapshots_restore_outer():
    with config.cycle_snapshot() as outer:
        with config.cycle_snapshot({"SYSTEM_MAX_HOLDINGS": 99}):
            assert config.SYSTEM_MAX_HOLDINGS == 99
        assert config.current_cycle_snapshot() is outer
    assert config.current_cycle_snapshot() is None


def test_worker_pools_inherit_the_cycle_snapshot():
    with config.cycle_snapshot({**config.capture_cycle_snapshot(), "SYSTEM_MAX_HOLDINGS": 77}):
        task = utils.inherit_account_context(lambda: config.SYSTEM_MAX_HOLDINGS)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            assert ex.submit(task).result() == 77
        # 감싸지 않은 작업은 물려받지 않는다 — 그래서 매매 풀은 모두 감싸야 한다
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            assert ex.submit(lambda: config.SYSTEM_MAX_HOLDINGS).result() != 77
    # 주기 밖에서 감싼 작업은 스냅샷 없이(실시간) 돈다
    task = utils.inherit_account_context(lambda: config.current_cycle_snapshot())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(task).result() is None


def test_trading_loop_judges_inside_a_snapshot():
    """[구조 가드] 매매 루프의 판단 3단계(매도·매수·미체결 관리)가 스냅샷 안에서 돈다."""
    from modules.auto_trade.trader import AutoTrader
    src = inspect.getsource(AutoTrader._run_loop)
    i = src.index("with config.cycle_snapshot():")
    block = src[i:i + 1200]
    for call in ("self._check_sell_conditions(", "self._check_buy_conditions(",
                 "self.order_manager.manage_unfilled_orders()"):
        assert call in block, call


def test_decision_paths_do_not_bypass_the_snapshot():
    """settings.<필드> 를 직접 읽으면 스냅샷을 우회한다 — 매매 패키지에서는 config.<필드> 로."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = os.path.join(root, "modules", "auto_trade")
    offenders = []
    for f in os.listdir(pkg):
        if f.endswith(".py"):
            for i, line in enumerate(open(os.path.join(pkg, f), encoding="utf-8"), 1):
                if "config.settings." in line.split("#", 1)[0]:
                    offenders.append(f"{f}:{i}: {line.strip()}")
    assert not offenders, offenders
