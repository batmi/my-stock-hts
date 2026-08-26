import unittest
from unittest.mock import MagicMock, patch
import threading
import time
import sys
import os

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 import 가능하게 함
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import ThrottledSession
import config

class TestThrottledSession(unittest.TestCase):
    def setUp(self):
        """테스트 환경 설정"""
        self.session = ThrottledSession()
        
        # 테스트용 TPS 설정 (계산 검증을 위해 명확한 값 사용)
        self.original_real_tps = config.REAL_TX_PER_SECOND
        self.original_real_safety = getattr(config, 'REAL_TPS_SAFETY', 0.9)

        config.REAL_TX_PER_SECOND = 10.0 # 실효 한도 = 10 * REAL_TPS_SAFETY(0.9) = 9 TPS -> 1/9초 간격
        config.REAL_TPS_SAFETY = 0.9     # 실전 내부 안전계수 명시 고정
        # [#7] 적응형 TPS를 비활성(step=0)으로 고정해 게이트 간격 검증을 결정적으로 만든다.
        # (성공 시 실효 TPS가 미세 상향되면 두 번째 요청 간격이 1/9에서 어긋나 검증이 흔들린다)
        self.original_tps_adapt_step = getattr(config, 'TPS_ADAPT_STEP', 0.05)
        config.TPS_ADAPT_STEP = 0.0
        # [수정 2026-08-09] 이 테스트는 '기본 게이트 간격'을 검증한다. 그런데 나중에 들어온
        #  우선순위 예약분이 조회성 스레드의 간격을 벌리고, 테스트는 MainThread(=조회성)에서
        #  돈다 — 그래서 기대값이 전부 어긋나 세 케이스가 계속 실패하고 있었다.
        #  여기서는 예약분을 끄고 기본 간격만 본다.
        #  (예약분 자체는 아래 test_low_priority_thread_yields_the_reserve 가 따로 검증한다)
        self.original_reserve = getattr(config, 'PRIORITY_RESERVE_TPS', 2.0)
        config.PRIORITY_RESERVE_TPS = 0.0
        # 간격 검증 케이스들은 균등 전송을 전제한다. 운용 기본값은 버스트 허용(속도 우선)이라
        #  여기서만 켠다 — 버스트 자체는 test_burst_respects_the_window_cap 이 따로 본다.
        self.original_even = getattr(config, 'TPS_EVEN_PACING', True)
        config.TPS_EVEN_PACING = True
        
        # requests.Session.request 모킹 (실제 네트워크 요청 방지)
        self.patcher_request = patch('requests.Session.request')
        self.mock_request = self.patcher_request.start()
        # 가짜 응답 객체 설정
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'rt_cd': '0', 'msg_cd': 'MCA00000'}
        self.mock_request.return_value = mock_response
        
        # time 제어용 변수 (time.time과 time.sleep 연동을 위해 Custom Mock 적용)
        self.current_time = 1000.0
        
        def mock_time():
            return self.current_time
            
        class SleepTracker:
            def __init__(self, ctx):
                self.ctx = ctx
                self.called = False
                self.call_args = None
                self.max_wait = 0.0  # [추가] 호출된 대기시간 중 최대값 (부동소수 잔여 sleep에 견고)

            def __call__(self, seconds):
                self.called = True
                self.call_args = ((seconds,), {})
                self.max_wait = max(self.max_wait, seconds)
                # 무한 루프 방지: sleep 시간이 0이어도 최소 0.001초는 흐르도록 강제 전진
                self.ctx.current_time += max(seconds, 0.001)
            
        # side_effect 대신 new를 사용하여 MagicMock 호출 기록 누적에 의한 메모리 누수 방지
        self.patcher_time = patch('time.time', new=mock_time)
        self.patcher_time.start()
        
        self.mock_sleep = SleepTracker(self)
        self.patcher_sleep = patch('time.sleep', new=self.mock_sleep)
        self.patcher_sleep.start()

    def tearDown(self):
        """테스트 환경 복구"""
        config.REAL_TX_PER_SECOND = self.original_real_tps
        config.REAL_TPS_SAFETY = self.original_real_safety
        config.TPS_ADAPT_STEP = self.original_tps_adapt_step
        config.PRIORITY_RESERVE_TPS = self.original_reserve
        config.TPS_EVEN_PACING = self.original_even
        
        for patcher in [self.patcher_request, self.patcher_time, self.patcher_sleep]:
            try:
                patcher.stop()
            except RuntimeError:
                pass # 테스트 내에서 명시적으로 이미 stop()된 경우 무시

    def test_real_server_throttling(self):
        """실전투자 URL 요청 시 스로틀링 적용 확인"""
        url = "https://openapi.koreainvestment.com/uapi/test" # 실전투자 도메인
        
        self.current_time = 2000.0
        self.session.request('GET', url)
        self.session.request('GET', url)

        # 실전투자 예상 대기 시간: 실효 한도 = REAL_TX_PER_SECOND * REAL_TPS_SAFETY = 9 TPS
        #   -> min_interval = 1.0 / 9 ≈ 0.1111초
        # (1/9는 무한소수이므로 부동소수 누적으로 마지막 sleep이 미세값(~0)이 될 수 있어
        #  '마지막 호출'이 아닌 '최대 대기값'으로 검증한다.)
        effective_limit = config.REAL_TX_PER_SECOND * config.REAL_TPS_SAFETY
        expected_wait = 1.0 / effective_limit
        self.assertTrue(self.mock_sleep.called)
        self.assertAlmostEqual(self.mock_sleep.max_wait, expected_wait, places=4)

        # 실전투자 상태 변수가 두 번째 요청 통과 시점(≈ 2000 + min_interval)으로 갱신되었는지 확인
        # (무한루프 방지용 sleep 최소 전진(0.001)으로 한 틱 밀릴 수 있어 places=2로 검증)
        self.assertAlmostEqual(self.session.request_history_real[-1], 2000.0 + expected_wait, places=2)

    def test_low_priority_thread_yields_the_reserve(self):
        """[핵심] 조회성 스레드는 매매용 예약분을 뺀 나머지만 쓴다.

        종전에는 비율(×0.5)로 쪼갰다. 실효 한도가 AIMD로 움직이는 지금은 비율이 틀린
        도구다 — 한도 20에서 조회 10은 넉넉하지만 한도 6에서 조회 3은 메뉴가 못 쓸 만큼
        느리면서 매매에 남는 몫도 3뿐이다. 절대량 예약은 한도가 어디로 가든 매매 헤드룸을
        그대로 지킨다(2026-08-09 실측 무릎 6 TPS 반영).
        """
        config.PRIORITY_RESERVE_TPS = 2.0
        url = "https://openapi.koreainvestment.com/uapi/test"

        self.current_time = 3000.0
        # 매매가 방금 돌았다고 표시한다 — 예약은 '매매가 실제로 돌 때'만 걸린다.
        self.session._last_priority_grant = self.current_time
        self.session.request('GET', url)   # MainThread = 조회성(비우선순위)
        self.session.request('GET', url)

        effective_limit = config.REAL_TX_PER_SECOND * config.REAL_TPS_SAFETY
        expected = 1.0 / (effective_limit - config.PRIORITY_RESERVE_TPS)
        self.assertAlmostEqual(self.mock_sleep.max_wait, expected, places=4,
                               msg="조회성 스레드가 예약분을 양보하지 않았다")

    def test_reserve_is_released_when_trading_is_idle(self):
        """[핵심] 매매가 놀면 예약분을 조회에게 돌려준다.

        떼어 두기만 하고 아무도 안 쓰면 그냥 버려지는 몫이다. 자동매매를 안 켠 주말에
        메뉴만 여는데도 2 TPS가 통째로 놀았고, 그만큼 조회가 느려졌다. 종전 비율 배분이
        갖고 있던 '유휴 시 손해 없음' 성질을 절대량 예약이 깨뜨린 자리다.
        """
        config.PRIORITY_RESERVE_TPS = 2.0
        url = "https://openapi.koreainvestment.com/uapi/test"

        self.current_time = 4000.0
        # 우선순위 스레드가 최근에 전송한 적이 없다(_last_priority_grant = 0).
        self.session.request('GET', url)
        self.session.request('GET', url)

        effective_limit = config.REAL_TX_PER_SECOND * config.REAL_TPS_SAFETY
        self.assertAlmostEqual(self.mock_sleep.max_wait, 1.0 / effective_limit, places=4,
                               msg="매매가 노는데도 조회가 예약분을 양보했다")

    def test_burst_respects_the_window_cap(self):
        """[속도] 균등 전송을 끄면 창 안에서는 몰아 보내되, 창 상한은 그대로 지킨다.

        1.1초 창 상한이 곧 초당 상한이다 — 1.0초는 1.1초의 부분구간이므로, 창에 20건
        상한을 두면 어떤 1초를 잘라도 20건을 넘지 않는다. 문서 한도(20 TPS)를 지키면서
        간격만 푸는 것이라, 실측상 처리량이 높은 버스트 패턴을 안전하게 쓸 수 있다.
        """
        config.TPS_EVEN_PACING = False
        config.REAL_TX_PER_SECOND = 20.0
        config.REAL_TPS_SAFETY = 1.0
        url = "https://openapi.koreainvestment.com/uapi/test"

        self.current_time = 5000.0
        for _ in range(20):
            self.session.request('GET', url)
        # 창 상한까지는 대기 없이 통과한다
        self.assertEqual(self.mock_sleep.max_wait, 0.0, "버스트 구간인데 대기가 걸렸다")
        self.assertEqual(len(self.session.request_history_real), 20)

        # 21번째는 창이 빌 때까지 기다린다 → 어떤 1초에도 20건을 넘지 않는다
        self.session.request('GET', url)
        self.assertGreater(self.mock_sleep.max_wait, 0.0,
                           "창 상한을 넘겼는데 대기가 없다 — 명목 한도를 초과한다")

if __name__ == '__main__':
    unittest.main()
