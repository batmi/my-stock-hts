"""TPS 우선순위: 한도 경쟁에서 시스템 트레이딩이 조회 메뉴를 이긴다.

[왜] 매매 판단·주문은 시각이 곧 가격이라 미룰 수 없지만, 조회 메뉴는 몇 초 늦어도
사용자가 기다리면 그만이다. 그런데 종전에는 모든 스레드가 같은 토큰 버킷을 동등하게
다퉈, 메뉴 1·2를 여는 동안 정작 후보 분석 스레드(cand_io_*)가 EGW00201로 '최종 실패'
했다(2026-08-05 관측). 실패한 조회는 재시도로 끝나는 게 아니라 그 종목의 판정을
그 주기에서 통째로 건너뛰게 만든다 — 조용한 데이터 손실이다.
"""
import threading

import pytest

import api
import config


@pytest.mark.parametrize("name,expected", [
    ("AutoTrader", True),
    ("ConclusionMonitor", True),
    ("ReservedOrderMonitor", True),
    ("cand_io_3", True),
    ("at_cand_0", True),
    ("at_sell_2", True),
    ("MainThread", False),        # 메뉴 조회
    ("ThreadPoolExecutor-5_4", False),   # 메뉴가 띄운 익명 작업 풀
    ("TelegramBot", False),
    ("GeminiAI_1", False),
])
def test_thread_priority_classification(name, expected):
    """스레드 이름으로 시스템 트레이딩 경로인지 판정한다."""
    result = {}

    def _probe():
        result['v'] = api._is_system_priority()

    t = threading.Thread(target=_probe, name=name)
    t.start()
    t.join()
    assert result['v'] is expected, f"{name} 판정이 틀렸다"


def test_unknown_thread_defaults_to_priority(monkeypatch):
    """스레드 이름을 못 읽으면 양보시키지 않는다 — 매매를 늦추는 쪽이 더 위험하다."""
    def _boom():
        raise RuntimeError("no thread name")
    monkeypatch.setattr(api.threading, 'current_thread', _boom)
    assert api._is_system_priority() is True


def test_low_priority_gate_is_stricter():
    """조회성 호출의 문턱(한도·최소간격)이 매매보다 빡빡하다.

    문턱만 낮추는 방식이라 매매가 한가하면 조회도 한도를 그대로 쓴다(유휴 손실 없음).
    """
    share = float(getattr(config, 'LOW_PRIORITY_TPS_SHARE', 0.5))
    assert 0 < share <= 1.0, "비율은 0~1 사이여야 한다"

    effective_limit, min_interval = 18.0, 1.0 / 18.0

    # 매매(우선) 게이트
    hi_limit, hi_interval = effective_limit, min_interval
    # 조회(양보) 게이트 — api.ThrottledSession.request 와 같은 식
    lo_limit = max(1.0, effective_limit * share)
    lo_interval = min_interval / share

    assert lo_limit < hi_limit, "조회 한도가 매매보다 낮아야 한다"
    assert lo_interval > hi_interval, "조회 최소간격이 매매보다 길어야 한다"


def test_system_thread_prefixes_cover_trading_executors():
    """자동매매가 띄우는 작업 풀 이름이 우선순위 접두어에 실제로 걸린다.

    실행기에 prefix 를 붙여 놓고 접두어 목록에 반영하지 않으면, 그 워커들이 조용히
    '조회'로 분류돼 매매가 스스로 양보하게 된다.
    """
    import re
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prefixes = []
    for rel in ("modules/auto_trade/trader.py", "modules/auto_trade/engine.py",
                "modules/auto_trade/conclusion.py"):
        src = open(os.path.join(root, rel), encoding="utf-8").read()
        prefixes += re.findall(r'thread_name_prefix="([^"]+)"', src)

    assert prefixes, "자동매매 작업 풀에 thread_name_prefix 가 하나도 없다"
    for p in prefixes:
        assert p.startswith(api._SYSTEM_THREAD_PREFIXES), \
            f"작업 풀 접두어 '{p}' 가 우선순위 목록에 없다 — 매매가 조회로 분류된다"
