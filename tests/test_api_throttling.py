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
        self.original_sim_tps = config.SIM_TX_PER_SECOND
        self.original_real_tps = config.REAL_TX_PER_SECOND
        
        config.SIM_TX_PER_SECOND = 2.0   # 0.5초 간격 (실제로는 1.05배 안전계수 적용 -> 0.525초)
        config.REAL_TX_PER_SECOND = 10.0 # 0.1초 간격 (0.105초)
        
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
            
            def __call__(self, seconds):
                self.called = True
                self.call_args = ((seconds,), {})
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
        config.SIM_TX_PER_SECOND = self.original_sim_tps
        config.REAL_TX_PER_SECOND = self.original_real_tps
        
        for patcher in [self.patcher_request, self.patcher_time, self.patcher_sleep]:
            try:
                patcher.stop()
            except RuntimeError:
                pass # 테스트 내에서 명시적으로 이미 stop()된 경우 무시

    def test_simulation_throttling(self):
        """모의투자 URL 요청 시 스로틀링(속도 제한) 적용 확인"""
        url = "https://openapivts.koreainvestment.com/uapi/test" # 모의투자 도메인
        
        self.current_time = 1000.0
        self.session.request('GET', url)
        
        self.session.request('GET', url)
            
        # 모의투자 예상 대기 시간 계산: (1.0 / 2.0) * 1.2 = 0.6초
        expected_wait = 0.6
        
        # sleep이 해당 시간만큼 호출되었는지 확인
        self.assertTrue(self.mock_sleep.called)
        args, _ = self.mock_sleep.call_args
        self.assertAlmostEqual(args[0], expected_wait, places=5)
        
        # 마지막 요청 시간이 1000.6으로 갱신되었는지 확인
        self.assertAlmostEqual(self.session.request_history_sim[-1], 1000.6, places=3)

    def test_real_server_throttling(self):
        """실전투자 URL 요청 시 스로틀링 적용 확인"""
        url = "https://openapi.koreainvestment.com/uapi/test" # 실전투자 도메인
        
        self.current_time = 2000.0
        self.session.request('GET', url)
        self.session.request('GET', url)
            
        # 실전투자 예상 대기 시간 계산: (1.0 / 10.0) * 1.05 = 0.105초
        expected_wait = 0.105
        self.assertTrue(self.mock_sleep.called)
        args, _ = self.mock_sleep.call_args
        self.assertAlmostEqual(args[0], expected_wait, places=5)
        
        # 실전투자 상태 변수가 갱신되었는지 확인
        self.assertAlmostEqual(self.session.request_history_real[-1], 2000.105, places=3)

    def test_reserve_then_wait_concurrency(self):
        """선예약 후대기(Reserve-then-Wait) 로직의 동시성 처리 확인"""
        url = "https://openapivts.koreainvestment.com/uapi/test"
        
        # 모의 시간 환경을 멀티스레드용으로 재정의
        def mock_time_multithread():
            return self.current_time
            
        class SleepTrackerMT:
            def __init__(self, ctx):
                self.ctx = ctx
            
            def __call__(self, seconds):
                self.ctx.current_time += max(seconds, 0.001)
            
        self.current_time = 3000.0
        self.patcher_time.stop()
        self.patcher_sleep.stop()
        
        with patch('time.time', new=mock_time_multithread), \
             patch('time.sleep', new=SleepTrackerMT(self)):
             
             self.session.request('GET', url)
             self.session.request('GET', url)
             self.session.request('GET', url)
            
        # sleep 호출 기록 확인
        # args[0]이 대기 시간
        # 이 테스트는 실제 멀티스레딩이 아니라 모킹된 순차적 흐름이므로
        # 1. 3000.0 (즉시 전송)
        # 2. 3000.6 (최소 간격 0.6초 대기)
        # 3. 3001.5 (윈도우 1.5초 대기: 3000.0 + 1.5초)
        self.assertEqual(self.mock_request.call_count, 3)
        self.assertAlmostEqual(self.session.request_history_sim[-1], 3001.5, places=3)

    def test_thread_safety_with_threads(self):
        """실제 스레드를 생성하여 락(Lock) 동작 검증"""
        url = "https://openapivts.koreainvestment.com/uapi/test"
        num_threads = 10
        threads = []
        
        # 실제 스레드 환경에서는 모킹된 시간(1000.0 고정)을 사용하면 무한루프에 빠지므로,
        # 모킹을 일시 중단하고 실제 time.time과 time.sleep을 사용 (단축된 대기 시간 사용)
        self.patcher_time.stop()
        self.patcher_sleep.stop()
        
        config.SIM_TX_PER_SECOND = 100.0 # 빠른 테스트를 위해 한도 상향 (0.012초 간격)
        
        def worker():
            self.session.request('GET', url)
        
        start = time.time()
        for _ in range(num_threads):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()
            
        for t in threads:
            t.join()
            
        elapsed = time.time() - start
        
        self.assertEqual(self.mock_request.call_count, num_threads)
        
        # 최소 0.012 * (num_threads - 1) 시간이 소요되어야 정상적으로 큐잉된 것
        expected_min_elapsed = (1.0 / config.SIM_TX_PER_SECOND * 1.2) * (num_threads - 1)
        self.assertTrue(elapsed >= expected_min_elapsed)

if __name__ == '__main__':
    unittest.main()
