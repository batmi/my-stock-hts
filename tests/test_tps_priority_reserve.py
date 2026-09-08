"""매매용 TPS 예약분 — '기다리는 매매'와 '없는 매매'를 구별한다.

[왜 · 2026-09-08] 예약분(PRIORITY_RESERVE_TPS)은 조회가 한도를 다 써도 매매가 나갈
 자리를 남겨 두는 장치다. 그런데 예약을 **풀지 말지**를 '우선순위 스레드가 마지막으로
 전송을 얻은 시각'으로 판정했다. 조회가 창을 가득 채우면 매매 스레드는 전송을 못 얻고,
 못 얻었다는 이유로 예약이 풀려 더 못 얻는다 — 예약이 자기 자신을 끄는 구조다.

 실측(조회 16스레드 포화, 명목 20 TPS, 매매 첫 전송까지):
     예약 살아있음 →     0ms ·     0ms ·     0ms
     예약 풀림     → 6,108ms · 1,700ms · 1,694ms
     수정 후       →   600ms ·   598ms ·   600ms   (창이 비는 시간 ≤1.1초가 상한)

 매매 루프의 주기는 유휴 판정 기준(10초)보다 길므로, **매 주기의 첫 요청**이 반드시 이
 상태를 만난다. 그 요청이 손절 판정을 위한 현재가 조회이거나 주문이다.
"""
import threading
import unittest
from unittest.mock import MagicMock, patch

import config
from api import ThrottledSession
from api import http as H

URL = "https://openapi.koreainvestment.com/uapi/domestic-stock/v1/quotations/inquire-price"


class _Clock:
    """가짜 시계 — sleep 이 시간을 전진시킨다(기존 test_api_throttling 과 같은 방식)."""

    def __init__(self, t0=1000.0):
        self.now = t0
        self.sleeps = []
        self.demand_at_sleep = []
        self.watch = None       # 관찰할 버킷

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.watch is not None:
            self.demand_at_sleep.append(self.watch.last_priority_demand)
        self.now += max(seconds, 0.001)


class ReserveReleaseTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.session = ThrottledSession()
        self.bucket = self.session._real_buckets[self.session.BUCKET_MANUAL]

        self._saved = {k: getattr(config, k, None) for k in
                       ("REAL_TX_PER_SECOND", "REAL_TPS_SAFETY", "TPS_ADAPT_STEP",
                        "PRIORITY_RESERVE_TPS", "PRIORITY_RESERVE_IDLE_SEC",
                        "TPS_EVEN_PACING")}
        config.REAL_TX_PER_SECOND = 10.0
        config.REAL_TPS_SAFETY = 0.9        # 실효 한도 9 TPS
        config.TPS_ADAPT_STEP = 0.0         # 적응 상향을 꺼 간격을 결정적으로 만든다
        config.PRIORITY_RESERVE_TPS = 2.0
        config.PRIORITY_RESERVE_IDLE_SEC = 10.0
        config.TPS_EVEN_PACING = True       # 간격으로 관찰한다

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {'rt_cd': '0', 'msg_cd': 'MCA00000'}
        self._p = [patch('requests.Session.request', return_value=resp),
                   patch('time.time', new=self.clock.time),
                   patch('time.sleep', new=self.clock.sleep)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            try:
                p.stop()
            except RuntimeError:
                pass
        for k, v in self._saved.items():
            if v is not None:
                setattr(config, k, v)

    # -- 관찰 도구 ---------------------------------------------------------
    def _query_gap(self):
        """조회성(비우선순위) 스레드가 연속 두 건을 보낼 때 벌어지는 간격."""
        self.clock.sleeps.clear()
        self.session.request('GET', URL)
        self.session.request('GET', URL)
        return max(self.clock.sleeps) if self.clock.sleeps else 0.0

    @property
    def _gap_with_reserve(self):
        return 1.0 / (config.REAL_TX_PER_SECOND * config.REAL_TPS_SAFETY
                      - config.PRIORITY_RESERVE_TPS)

    @property
    def _gap_without_reserve(self):
        return 1.0 / (config.REAL_TX_PER_SECOND * config.REAL_TPS_SAFETY)

    # -- 해제 조건 ---------------------------------------------------------
    def test_a_waiting_trader_keeps_the_reserve_alive(self):
        """전송은 못 얻었지만 **요청은 한** 매매가 있으면 예약을 유지한다.

        이 한 줄이 결함의 핵심이다 — 종전에는 전송(grant)만 셌다.
        """
        self.bucket.last_priority_demand = self.clock.now
        self.bucket.last_priority_grant = 0.0        # 한 번도 못 나갔다
        self.assertAlmostEqual(self._query_gap(), self._gap_with_reserve, places=4,
                               msg="기다리는 매매가 있는데 조회가 예약분을 다 썼다")

    def test_a_granted_trader_still_keeps_the_reserve_alive(self):
        """종전 신호(마지막 전송)도 그대로 활동의 증거다."""
        self.bucket.last_priority_demand = 0.0
        self.bucket.last_priority_grant = self.clock.now
        self.assertAlmostEqual(self._query_gap(), self._gap_with_reserve, places=4)

    def test_no_trader_at_all_still_releases_the_reserve(self):
        """자동매매를 안 켠 주말에는 종전대로 예약을 돌려준다 — 손실 0."""
        self.bucket.last_priority_demand = 0.0
        self.bucket.last_priority_grant = 0.0
        self.assertAlmostEqual(self._query_gap(), self._gap_without_reserve, places=4,
                               msg="매매가 없는데도 조회가 예약분을 양보했다")

    def test_an_old_demand_expires_like_an_old_grant(self):
        """요청도 오래되면 만료된다(예약을 영구히 붙잡지 않는다)."""
        self.bucket.last_priority_demand = self.clock.now - 30.0
        self.bucket.last_priority_grant = 0.0
        self.assertAlmostEqual(self._query_gap(), self._gap_without_reserve, places=4)

    # -- 요청이 실제로 기록되는가 ------------------------------------------
    def test_the_demand_is_recorded_before_the_send_not_after(self):
        """전송을 못 얻고 자는 그 순간에 이미 요청이 기록돼 있어야 한다.

        기록이 전송 뒤에만 일어나면, 창이 가득 찬 동안 매매의 존재가 보이지 않는다.
        """
        # 창을 상한까지 채워 첫 회차가 반드시 대기하게 만든다
        limit = config.REAL_TX_PER_SECOND * config.REAL_TPS_SAFETY
        for i in range(int(limit) + 1):
            self.bucket.history.append(self.clock.now - 0.01)
        self.clock.watch = self.bucket
        self.clock.demand_at_sleep.clear()

        done = []

        def trader():
            self.session.request('GET', URL)
            done.append(True)

        t = threading.Thread(target=trader, name="AutoTrader")
        t.start()
        t.join(timeout=10)

        self.assertTrue(done, "매매 요청이 끝내 나가지 못했다")
        self.assertTrue(self.clock.demand_at_sleep, "대기가 한 번도 없었다 — 전제가 깨졌다")
        self.assertGreater(self.clock.demand_at_sleep[0], 0.0,
                           "전송을 못 얻고 자는 동안 매매의 요청이 기록되지 않았다")


class PriorityNamingContractTest(unittest.TestCase):
    """우선순위 판정은 스레드 **이름**으로 한다 — 이름이 바뀌면 조용히 양보한다.

    매매 경로가 조회로 분류되면 그 요청은 예약분을 못 쓰고 EGW00201 로 최종 실패할 수
    있다. 실패한 조회는 그 종목의 판정을 통째로 건너뛰게 만든다(2026-08-05 관측).
    이름과 판정이 갈라지는 것을 코드가 스스로 알아채게 한다.
    """

    #  실제로 그 이름을 만드는 자리 → 우선순위여야 하는가
    CASES = [
        ("AutoTrader", True),            # 매매 메인 루프
        ("ConclusionMonitor", True),     # 체결 감시
        ("ReservedOrderMonitor", True),  # 예약 주문(발주 경로)
        ("cand_io_0", True),             # 후보 분석 I/O 풀
        ("at_cand_io_1", True),
        ("at_engine_2", True),
        ("at_sell_0", True),
        ("at_init_0", True),
        ("at_status_0", True),
        ("MainThread", False),           # 사람이 여는 메뉴 — 기다려도 된다
        ("ThemeIO_3", False),
        ("GeminiAI_0", False),
        ("TgSender_0", False),
        ("JournalSync", False),
    ]

    def test_each_thread_name_lands_on_the_intended_side(self):
        for name, expected in self.CASES:
            with self.subTest(name=name):
                t = threading.Thread(target=lambda: None, name=name)
                out = {}

                def probe():
                    out['v'] = H._is_system_priority()

                t = threading.Thread(target=probe, name=name)
                t.start()
                t.join()
                self.assertEqual(out['v'], expected, f"{name} 의 우선순위 판정")

    def test_the_names_the_prefixes_expect_still_exist_in_the_code(self):
        """접두어가 가리키는 이름이 코드에서 사라지면 판정은 영영 참이 되지 않는다."""
        import glob
        src = "\n".join(open(p, encoding='utf-8').read()
                        for p in glob.glob("modules/**/*.py", recursive=True)
                        + glob.glob("api/**/*.py", recursive=True))
        for prefix in ("AutoTrader", "ConclusionMonitor", "ReservedOrderMonitor"):
            self.assertIn(f'name="{prefix}"', src,
                          f"{prefix} 스레드를 만드는 자리가 사라졌다 — 접두어가 죽은 이름을 가리킨다")
        for prefix in ("cand_io", "at_"):
            self.assertIn(f'thread_name_prefix="{prefix}', src,
                          f"{prefix} 로 시작하는 작업 풀이 사라졌다")


class StateChangingUrlCoverageTest(unittest.TestCase):
    """응답 유실 시 재전송을 막는 판정이 **실제 주문 엔드포인트를 전부** 덮는가.

    하나라도 빠지면 그 주문은 타임아웃에 재전송되어 같은 주문이 두 번 나간다.
    포지션이 두 배가 되면 손절폭·변동성 한도·히트 캡이 한꺼번에 무의미해진다.
    """

    ORDER_ENDPOINTS = [
        "https://openapi.koreainvestment.com/uapi/domestic-stock/v1/trading/order-cash",
        "https://openapi.koreainvestment.com/uapi/domestic-stock/v1/trading/order-rvsecncl",
        "https://openapi.koreainvestment.com/uapi/overseas-stock/v1/trading/order",
        "https://openapi.koreainvestment.com/uapi/overseas-stock/v1/trading/order-rvsecncl",
    ]

    QUERY_ENDPOINTS = [
        "https://openapi.koreainvestment.com/uapi/domestic-stock/v1/trading/inquire-balance",
        "https://openapi.koreainvestment.com/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
        "https://openapi.koreainvestment.com/uapi/overseas-stock/v1/trading/inquire-nccs",
        "https://openapi.koreainvestment.com/uapi/domestic-stock/v1/quotations/inquire-price",
    ]

    def test_every_order_endpoint_is_treated_as_state_changing(self):
        for u in self.ORDER_ENDPOINTS:
            with self.subTest(url=u):
                self.assertTrue(H._is_state_changing("POST", u), f"{u} 가 주문으로 안 잡힌다")

    def test_query_endpoints_are_not(self):
        for u in self.QUERY_ENDPOINTS:
            with self.subTest(url=u):
                self.assertFalse(H._is_state_changing("POST", u))

    def test_the_repository_uses_no_order_endpoint_the_check_does_not_know(self):
        """코드가 실제로 부르는 주문 URL 중 판정이 모르는 것이 있으면 잡는다."""
        import glob
        import re
        #  주문 URL 은 core/constants.py 가 들고 있다 — 탐색 범위에서 빠뜨리면 이 검사가
        #   조용히 아무것도 안 본다. 아래 self-test(orders 가 비면 실패)가 그것을 잡는다.
        paths = (glob.glob("api/**/*.py", recursive=True)
                 + glob.glob("modules/**/*.py", recursive=True)
                 + glob.glob("brokers/**/*.py", recursive=True)
                 + glob.glob("core/**/*.py", recursive=True))
        src = "\n".join(open(p, encoding='utf-8').read() for p in paths)
        used = set(re.findall(r"uapi/[a-z0-9/-]*", src))
        orders = [u for u in used if "/order" in u]
        self.assertTrue(orders, "주문 엔드포인트를 하나도 못 찾았다 — 이 검사가 무력하다")
        for u in orders:
            with self.subTest(endpoint=u):
                self.assertTrue(
                    H._is_state_changing("POST", "https://openapi.koreainvestment.com/" + u),
                    f"{u} 를 코드가 부르는데 재전송 금지 판정이 모른다")
