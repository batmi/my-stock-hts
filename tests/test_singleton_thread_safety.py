"""싱글톤 생성이 스레드에 안전한가 — **반쯤 만들어진 객체를 넘기지 않는다.**

[왜 이 파일이 있나 · 2026-09-05]
이 시스템은 스레드가 많다 — 매매 루프, 스케줄러, 체결 감시, 텔레그램 봇, 예약주문 감시,
WebSocket 피드, 그리고 워커 풀 여럿. 그 스레드들이 같은 싱글톤을 각자 부른다. 그런데
일곱 개 클래스가 전부 이렇게 생겼었다:

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)     # ← 먼저 공표하고
            cls._instance.is_running = False         # ← 속성은 그 뒤에 수십 개
            ...
            cls._instance.file_logger = config.get_autotrade_logger()   # ← 파일 I/O
            cls._instance.order_manager = OrderManager(cls._instance)

검사와 대입 사이가 열려 있는 것보다 **인스턴스를 먼저 대입하고 속성을 뒤에 채우는 것**이
더 나쁘다. 두 번째 스레드는 그 창에 들어와 '있다'고 보고 미완성 객체를 그대로 가져간다.
초기화 도중 파일 I/O 와 DB 접근이 있어 GIL 이 실제로 놓이므로 이론상의 경합이 아니다 —
**실측: 8스레드 중 7개가 미완성 객체를 받았다.**

기동 순서상 창이 열려 있다. main 은 텔레그램 봇 스레드를 먼저 띄우고
(`telegram_cmd.start()`), SystemScheduler·AutoTrader 는 그 **뒤에** 처음 만든다.
봇 스레드의 명령 처리는 `SystemScheduler().start()` · `AutoTrader()` 를 부른다.

미완성 AutoTrader 를 받으면 `order_manager` · `strategy` 같은 늦게 채워지는 속성에서
AttributeError 가 나고, 그 예외는 대개 워커의 try 에 삼켜져 **조용히 한 주기가 빈다**.
"""
import threading

import pytest

from modules.auto_trade.conclusion import ConclusionMonitor
from modules.auto_trade.trader import AutoTrader
from modules.journal_sync import JournalSyncWorker
from modules.market_halt import MarketHaltMonitor
from modules.reserved_order_monitor import ReservedOrderMonitor
from modules.scheduler import SystemScheduler
from modules.telegram_bot import TelegramCommander

SINGLETONS = [AutoTrader, ConclusionMonitor, SystemScheduler, TelegramCommander,
              JournalSyncWorker, ReservedOrderMonitor, MarketHaltMonitor]

#  '늦게 채워지는' 속성 — 초기화가 끝났는지 이걸로 본다(초반 속성은 창이 좁아 못 잡는다).
LATE_ATTR = {
    AutoTrader: "buy_halt_reason",
    ConclusionMonitor: "consecutive_errors",
    SystemScheduler: "trader",
    ReservedOrderMonitor: "chart_cache",
    MarketHaltMonitor: "vi_active",
}


@pytest.mark.parametrize("cls", SINGLETONS, ids=lambda c: c.__name__)
def test_생성자에_락이_있다(cls):
    """구조로 고정한다 — 경합은 재현이 확률적이라 테스트가 조용히 늙는다."""
    assert isinstance(getattr(cls, "_instance_lock", None),
                      type(threading.RLock())), \
        f"{cls.__name__} 에 _instance_lock 이 없다 — 싱글톤 생성이 경합에 열려 있다"


@pytest.mark.parametrize("cls", list(LATE_ATTR), ids=lambda c: c.__name__)
def test_동시_생성이_같은_완성된_객체를_준다(cls, monkeypatch):
    """[핵심] 스레드 전환을 촘촘히 두고 여러 스레드가 동시에 부른다.

    switchinterval 을 줄이는 것은 재현을 **빠르게** 하려는 것이지 없는 문제를 만드는 것이
    아니다 — 실제 경로에는 초기화 안에 파일 I/O 가 있어 기본 설정에서도 GIL 이 놓인다.
    """
    import sys
    saved_interval = sys.getswitchinterval()
    saved_instance = cls._instance
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(20):
            cls._instance = None
            n = 8
            barrier = threading.Barrier(n)
            got, broken = [], []

            def worker():
                barrier.wait()
                obj = cls()
                got.append(obj)
                if not hasattr(obj, LATE_ATTR[cls]):
                    broken.append(obj)

            threads = [threading.Thread(target=worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not broken, \
                f"{cls.__name__}: {len(broken)}개 스레드가 미완성 객체를 받았다"
            assert len({id(o) for o in got}) == 1, \
                f"{cls.__name__}: 서로 다른 인스턴스가 {len({id(o) for o in got})}개 생겼다"
    finally:
        sys.setswitchinterval(saved_interval)
        cls._instance = saved_instance


@pytest.mark.parametrize("cls", [SystemScheduler, TelegramCommander, JournalSyncWorker],
                         ids=lambda c: c.__name__)
def test_초기화가_두_번_돌지_않는다(cls):
    """`if self._initialized: return` 도 검사와 대입 사이가 열려 있었다.

    SystemScheduler.__init__ 은 self.trader = AutoTrader() 를 하고 하트비트 시각을
    다시 찍는다 — 두 번 돌면 그만큼 감시 기준 시각이 되감긴다.
    """
    import sys
    saved_interval = sys.getswitchinterval()
    saved_instance = cls._instance
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(20):
            cls._instance = None
            n = 8
            barrier = threading.Barrier(n)
            runs = []
            original = cls.__init__

            def counting_init(self, *a, **k):
                #  관찰도 **같은 락 안에서** 해야 한다. 락 밖에서 _initialized 를 읽으면
                #  '아직'이라고 본 스레드를 전부 세게 되는데, 그건 초기화가 여러 번 돈 것이
                #  아니라 아직 순서를 기다리는 것이다(이 테스트가 처음에 그렇게 틀렸다).
                with cls._instance_lock:
                    was = getattr(self, "_initialized", False)
                    original(self, *a, **k)
                    if not was:
                        runs.append(1)

            cls.__init__ = counting_init
            try:
                threads = [threading.Thread(target=cls) for _ in range(n)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            finally:
                cls.__init__ = original
            assert len(runs) == 1, f"{cls.__name__} 초기화가 {len(runs)}번 돌았다"
    finally:
        sys.setswitchinterval(saved_interval)
        cls._instance = saved_instance
