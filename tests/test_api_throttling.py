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
        
        # time.sleep 모킹 (테스트 속도 향상 및 대기 시간 검증)
        self.patcher_sleep = patch('time.sleep')
        self.mock_sleep = self.patcher_sleep.start()

    def tearDown(self):
        """테스트 환경 복구"""
        config.SIM_TX_PER_SECOND = self.original_sim_tps
        config.REAL_TX_PER_SECOND = self.original_real_tps
        self.patcher_request.stop()
        self.patcher_sleep.stop()

    def test_simulation_throttling(self):
        """모의투자 URL 요청 시 스로틀링(속도 제한) 적용 확인"""
        url = "https://openapivts.koreainvestment.com/uapi/test" # 모의투자 도메인
        
        # 1. 첫 번째 요청 (초기 상태)
        # 현재 시간을 1000.0초로 고정
        with patch('time.time', return_value=1000.0):
            self.session.request('GET', url)
            
        # 첫 요청은 대기 없이 즉시 실행되어야 함 (wait_time <= 0)
        # sleep이 호출되지 않았거나 0으로 호출되었을 수 있음
        
        # 2. 두 번째 요청 (직후)
        # 시간은 여전히 1000.0초라고 가정 (매우 빠른 연속 호출)
        with patch('time.time', return_value=1000.0):
            self.session.request('GET', url)
            
        # 예상 대기 시간 계산: (1.0 / 2.0) * 1.05 = 0.525초
        expected_wait = 0.525
        
        # sleep이 해당 시간만큼 호출되었는지 확인
        self.assertTrue(self.mock_sleep.called)
        args, _ = self.mock_sleep.call_args
        self.assertAlmostEqual(args[0], expected_wait, places=5)
        
        # 다음 예약 시간이 갱신되었는지 확인
        # 첫 요청 예약: 1000.0 (리셋됨) -> next: 1000.0 + 0.525 = 1000.525
        # 두 번째 요청 예약: 1000.525 -> next: 1000.525 + 0.525 = 1001.05
        self.assertAlmostEqual(self.session.next_available_time_sim, 1001.05, places=3)

    def test_real_server_throttling(self):
        """실전투자 URL 요청 시 스로틀링 적용 확인"""
        url = "https://openapi.koreainvestment.com/uapi/test" # 실전투자 도메인
        
        # 1. 첫 번째 요청
        with patch('time.time', return_value=2000.0):
            self.session.request('GET', url)
            
        # 2. 두 번째 요청
        with patch('time.time', return_value=2000.0):
            self.session.request('GET', url)
            
        # 예상 대기 시간 계산: (1.0 / 10.0) * 1.05 = 0.105초
        expected_wait = 0.105
        self.assertTrue(self.mock_sleep.called)
        args, _ = self.mock_sleep.call_args
        self.assertAlmostEqual(args[0], expected_wait, places=5)
        
        # 실전투자 상태 변수가 갱신되었는지 확인
        self.assertAlmostEqual(self.session.next_available_time_real, 2000.0 + expected_wait * 2, places=3)

    def test_reserve_then_wait_concurrency(self):
        """선예약 후대기(Reserve-then-Wait) 로직의 동시성 처리 확인"""
        url = "https://openapivts.koreainvestment.com/uapi/test"
        
        # 3개의 스레드가 동시에 요청을 보낸다고 가정
        # time.time()은 모두 동일한 시점(3000.0)을 반환
        with patch('time.time', return_value=3000.0):
            # Req 1
            self.session.request('GET', url)
            # next_sim -> 3000.525
            
            # Req 2
            self.session.request('GET', url)
            # wait -> 0.525, next_sim -> 3001.05
            
            # Req 3
            self.session.request('GET', url)
            # wait -> 1.05, next_sim -> 3001.575
            
        # sleep 호출 기록 확인
        # args[0]이 대기 시간
        call_args = [args[0] for args, _ in self.mock_sleep.call_args_list]
        
        # 0보다 큰 대기 시간만 필터링 (첫 요청은 0일 수 있음)
        waits = [w for w in call_args if w > 0.0001]
        
        interval = 0.525
        
        # 최소 2번의 대기가 발생해야 함 (2번째, 3번째 요청)
        self.assertTrue(len(waits) >= 2)
        
        # 각 요청이 겹치지 않게 순차적으로 대기 시간을 할당받았는지 확인
        # (병렬로 대기하므로 각 스레드는 0.525초, 1.05초를 각각 기다리게 됨)
        has_interval_1 = any(abs(w - interval) < 1e-5 for w in waits)
        has_interval_2 = any(abs(w - (interval * 2)) < 1e-5 for w in waits)
        
        self.assertTrue(has_interval_1, f"{interval} not found (approx) in {waits}")
        self.assertTrue(has_interval_2, f"{interval * 2} not found (approx) in {waits}")

    def test_thread_safety_with_threads(self):
        """실제 스레드를 생성하여 락(Lock) 동작 검증"""
        url = "https://openapivts.koreainvestment.com/uapi/test"
        num_threads = 10
        threads = []
        
        # 모든 스레드가 4000.0초 시점에 동시에 진입한다고 가정
        with patch('time.time', return_value=4000.0):
            def worker():
                self.session.request('GET', url)
            
            for _ in range(num_threads):
                t = threading.Thread(target=worker)
                threads.append(t)
                t.start()
                
            for t in threads:
                t.join()
                
        # 10개의 요청이 처리되었으므로, next_available_time_sim은
        # 4000.0 + (0.525 * 10) = 4005.25 가 되어야 함
        interval = 0.525
        expected_next_time = 4000.0 + (interval * num_threads)
        
        self.assertAlmostEqual(self.session.next_available_time_sim, expected_next_time, places=3)
        
        # mock_request(실제 API 호출 모킹)가 10번 호출되었는지 확인
        self.assertEqual(self.mock_request.call_count, num_threads)

if __name__ == '__main__':
    unittest.main()
