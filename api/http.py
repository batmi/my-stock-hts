"""HTTP 전송 계층 — TPS 게이트·재시도·커넥션 풀.

KIS 의 초당 거래건수 제한(EGW00201)은 앱키 단위라, 요청을 게이트 하나로 모아
흘려보내지 않으면 여러 스레드가 각자 한도까지 밀어 서로를 막는다. 어댑터(urllib3)
레벨 재시도는 게이트 아래에서 일어나 계측을 속이므로 전면 봉인하고(GatedRetry),
재시도는 게이트를 다시 지나는 앱 레벨에서만 한다.
"""
import json
import logging
import random
import ssl
import threading
import time
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from urllib3.util.retry import Retry
from collections import Counter, deque
import config
import context
from modules import instance_lock

#  로거 이름은 분해 전(api.py)과 같은 'api' 로 둔다 — 로그 필터·레벨 설정이 이름을 보므로
#  서브모듈마다 다른 이름을 쓰면 기존 설정이 조용히 빗나간다.
logger = logging.getLogger("api")

def _api():
    """패키지 네임스페이스(api)를 돌려준다 — 다른 계층의 이름은 반드시 이걸 통해 부른다.

    분해 전에는 전부 한 모듈이었으므로 테스트의 patch.object(api, 'X') 가 모든 호출부에
    걸렸다. 서브모듈이 상대 모듈을 직접 import 하면 그 patch 가 닿지 않는다 —
    같은 규약을 쓰는 modules/auto_trade 의 _pkg() 와 같은 이유다.
    """
    import api
    return api

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        # urllib3 메이저 버전 확인을 통한 명시적 분기 (지연 평가로 인한 에러 방지)
        urllib3_version = int(urllib3.__version__.split('.')[0])
        
        if urllib3_version >= 2:
            # urllib3 v2.x
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_minimum_version=ssl.TLSVersion.TLSv1_2)
        else:
            # urllib3 v1.x
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_version=ssl.PROTOCOL_TLSv1_2)

# Rate Limit(초당 한도 초과) 재시도 대기. 서버 장애용 지수 백오프와 분리한다 —
#  한도 초과는 다음 TPS 창만 비면 풀리므로, 재시도까지 몇 초씩 잠들 이유가 없다.
#  (전송 직전 스로틀이 TPS 창을 다시 지키므로 짧게 깨어나도 한도를 넘지 않는다)
# [속도 2026-08-09] 레이트리밋 재시도는 **싸야 한다.**
#  명목 한도(20 TPS)에 붙여 운행하면 실측상 20% 안팎이 거부되는데, 그 20%가 매번 0.2~1.5초를
#  자면 체감 속도가 그대로 무너진다. 거부는 장애가 아니라 '다음 창을 기다리라'는 신호일 뿐이고,
#  재전송은 어차피 TPS 게이트를 다시 지나므로 일찍 깨어나도 한도를 넘지 않는다.
#  게이트의 최소 대기(1/20 = 50ms)가 실질 하한 역할을 한다.
RATE_LIMIT_RETRY_WAIT = 0.05     # 시도 회차마다 선형 증가 (0.05 → 0.10 → 0.15 …)
RATE_LIMIT_RETRY_WAIT_MAX = 0.3  # 지터 제외 상한

# 서버가 유휴 keep-alive 소켓을 닫아 생기는 '연결 끊김'의 예외 문구들.
#  진짜 장애(게이트웨이 오류·타임아웃)와 달리 즉시 재전송하면 그대로 성공한다.
_CONNECTION_DROP_MARKS = ("RemoteDisconnected", "Connection aborted",
                          "Connection reset", "ProtocolError", "ConnectionResetError")


def _retry_wait_seconds(attempt, reason):
    """재시도 전 대기 시간(초).

    Rate Limit(EGW00201/EGW00215)은 서버 장애가 아니라 '초당 한도 초과'다. 다음 TPS 창
    (<1초)만 비면 곧바로 성공하는데도 장애용 지수 백오프(1→2→4→8→16초)를 그대로 태우면
    한 번 걸릴 때마다 최대 31초를 잠든다. 모의투자(2 TPS)에서 콜드 캐시로 대량 일봉을 받는
    '데이터 수신' 단계가 특정 종목에서 오래 멈추던 주된 원인이라, 짧은 선형 대기로 분리한다.
    (전송 직전 스로틀이 TPS 창을 다시 지키므로 일찍 깨어나도 한도를 넘지 않는다)

    그 외(연결 끊김·게이트웨이 오류 등 진짜 장애)는 종전 지수 백오프를 유지한다.
    """
    r = reason or ""
    if "Rate Limit" in r:
        # 지터도 대기와 같은 눈금으로 준다. 종전에는 0.1~0.5초를 더해, 0.05초 대기에
        #  0.3초 지터가 붙는 배보다 배꼽이 큰 상황이 됐다.
        return (min(RATE_LIMIT_RETRY_WAIT * (attempt + 1), RATE_LIMIT_RETRY_WAIT_MAX)
                + random.uniform(0.01, 0.05))
    jitter = random.uniform(0.1, 0.5)   # 동시 재시도 스레드가 한꺼번에 깨어나는 것 방지
    # [추가 2026-08-09] 끊긴 keep-alive 연결도 짧은 대기로 분리한다.
    #  어댑터 레벨 재시도를 봉인(total=0)하면서 이 흔한 경우가 앱 레벨로 올라왔는데,
    #  장애용 지수 백오프(1→2→4초)를 태우면 서버가 유휴 소켓을 닫을 때마다 초 단위로
    #  멈춘다. 재전송은 어차피 TPS 게이트를 다시 지나므로 일찍 깨어나도 한도를 넘지 않는다.
    if any(k in r for k in _CONNECTION_DROP_MARKS):
        return min(RATE_LIMIT_RETRY_WAIT * (attempt + 1), RATE_LIMIT_RETRY_WAIT_MAX) + jitter
    base_delay = getattr(config, 'RETRY_DELAY_SERVER', 1.0)
    return (base_delay * (2 ** attempt)) + jitter


# [TPS 우선순위] 시스템 트레이딩이 쓰는 스레드 이름. 접두어로 판정한다.
#  매매 판단·주문은 시각(時刻)이 곧 가격이라 미룰 수 없는 반면, 조회 메뉴는 몇 초 늦어도
#  사용자가 기다리면 그만이다. 그런데 종전에는 모든 스레드가 같은 토큰 버킷을 동등하게
#  다퉈, 메뉴 1·2를 여는 동안 정작 후보 분석(cand_io_*)이 EGW00201로 최종 실패했다
#  (2026-08-05 관측). 실패한 조회는 그 종목의 판정을 통째로 건너뛰게 만든다.
_SYSTEM_THREAD_PREFIXES = (
    "AutoTrader",          # 매매 메인 루프
    "ConclusionMonitor",   # 체결 감시
    "ReservedOrderMonitor",  # 예약 주문 감시(발주 경로)
    "cand_io",             # 후보 분석 I/O 풀
    "at_",                 # 자동매매가 띄우는 작업 풀(at_cand·at_sell·at_engine·at_init…)
)


def _is_system_priority():
    """현재 스레드가 시스템 트레이딩 경로인가. 아니면 조회성 호출로 보고 양보시킨다."""
    try:
        name = threading.current_thread().name or ""
    except Exception:
        return True   # 알 수 없으면 양보시키지 않는다(매매를 늦추는 쪽이 더 위험하다)
    return name.startswith(_SYSTEM_THREAD_PREFIXES)


class _RealTpsBucket:
    """실전 서버 TPS 상태 한 벌.

    [왜 나누나] KIS의 유량 한도(REAL_TX_PER_SECOND=20)는 **앱키 단위**다. 수동 계좌와
    자동매매 계좌에 서로 다른 앱키를 쓰면 각각 20 TPS를 따로 받는데, 한 카운터로 묶어
    세면 둘이 합쳐 20으로 눌려 실제 가용 용량의 절반을 스스로 버린다. 특히 운용자가
    메뉴에서 조회를 돌리는 동안 시스템 트레이딩의 주문·판정이 그 조회와 같은 예산을
    다투게 되는데, 이건 시각이 곧 가격인 쪽이 손해를 보는 구조다.

    반대로 앱키가 실제로는 같은데 나눠 세면 합계 40 TPS가 되어 EGW00201을 자초한다.
    그래서 버킷 배정은 '앱키가 실제로 다를 때만' 갈라진다(_real_bucket_key 참조).

    AIMD 상태(적응 한도·백오프 시각)도 버킷마다 독립이어야 한다. 거부는 키 단위로
    오므로, 한쪽 키가 물러난 것을 근거로 다른 키까지 낮추면 멀쩡한 예산을 버린다.
    """
    __slots__ = ("history", "adaptive_limit", "last_raise", "last_drop",
                 "last_priority_grant", "grants",
                 "rl_count", "rl_window_start", "rl_limit_from", "rl_grants_from",
                 "rl_trs", "rl_threads", "rl_last_emit")

    def __init__(self):
        self.history = deque()
        # [#7] 실전 실효 TPS를 적응형으로 운행한다(AIMD).
        #  - 시작값: 명목 한도 × REAL_TPS_SAFETY(=0.9 마진). 성공이 누적되면 마진을 조금씩 줄여(가산 증가)
        #    실효 TPS를 점진 상향하고, EGW00201(초당 거래건수 초과)이 나면 곱셈 감소로 즉시 물러난다.
        #  - [REAL_TPS_SAFETY_MIN, REAL_TPS_SAFETY_MAX] 범위 내에서 적정 TPS로 자가 수렴한다.
        self.adaptive_limit = None
        # [Fix] 가산 증가를 마지막으로 적용한 시각. AIMD의 '증가'는 요청당이 아니라
        #  윈도우(1초)당 한 번이어야 한다 — 아래 _tps_on_success_real 주석 참조.
        self.last_raise = 0.0
        # 곱셈 감소도 윈도우당 한 번이다(config.TPS_BACKOFF_WINDOW_SEC 주석 참조).
        self.last_drop = 0.0
        # 우선순위(매매) 스레드가 마지막으로 전송을 얻은 시각. 매매가 놀 때는 조회에게
        #  예약분을 돌려주기 위한 값이다.
        self.last_priority_grant = 0.0
        self.grants = 0                # 게이트를 통과한 누적 전송 건수(실전)
        # [로그 집계] 거부는 초당 수 건까지 나므로 건건이 WARNING을 남기면 로그가 그것만으로
        #  찬다. TPS_LOG_INTERVAL_SEC 마다 한 줄로 묶되(첫 거부는 즉시), 사후에 상황을
        #  재구성할 값은 그 한 줄에 모두 담는다.
        self.rl_count = 0
        self.rl_window_start = 0.0
        self.rl_limit_from = 0.0
        self.rl_grants_from = 0
        self.rl_trs = Counter()
        self.rl_threads = Counter()
        self.rl_last_emit = 0.0


class OrderOutcomeUnknown(Exception):
    """주문 요청이 거래소에 닿았는지 알 수 없는 상태(응답 유실).

    '실패'와 구분해야 한다. 실패는 재전송해도 되지만 이 상태는 안 된다 —
    이미 체결됐을 수 있기 때문이다. 호출부는 재전송 대신 **주문 내역을 조회해**
    실제로 들어갔는지 확인해야 한다(place_order 참조).
    """


# 상태를 바꾸는 엔드포인트(주문·정정·취소). 조회는 몇 번 다시 보내도 무해하지만
#  이쪽은 한 번 더 나가면 포지션이 하나 더 생긴다.
_STATE_CHANGING_URL_HINTS = ("order-cash", "order-credit", "order-rvsecncl",
                             "order-resv", "trading/order")


def _is_state_changing(method, url):
    if str(method).upper() == "GET":
        return False
    u = str(url or "")
    return any(h in u for h in _STATE_CHANGING_URL_HINTS)


def _is_response_unknown(exc):
    """응답을 받지 못해 결과를 알 수 없는 예외인가.

    ConnectTimeout 은 연결 자체가 안 된 것이라 주문이 나갔을 수 없다 — 재전송해도 안전하다.
    ReadTimeout 은 요청을 보낸 뒤 응답을 못 받은 것이므로 결과를 모른다.
    그 외 ConnectionError(전송 중 끊김)도 마찬가지로 모른다.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return False
    return isinstance(exc, (requests.exceptions.ReadTimeout,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.ChunkedEncodingError))


class ThrottledSession(requests.Session):
    #  '수동(운용자)' 버킷은 기존 동작과 이름을 그대로 유지한다 — 아래 프로퍼티 참조.
    BUCKET_MANUAL = "manual"
    BUCKET_AUTO = "auto"

    def __init__(self):
        super().__init__()
        self.request_history_sim = deque()
        self.lock = threading.Lock() # [추가] Rate Limit 계산 동기화를 위한 락
        self._real_buckets = {self.BUCKET_MANUAL: _RealTpsBucket(),
                              self.BUCKET_AUTO: _RealTpsBucket()}

    # ---- 앱키별 버킷 배정 -------------------------------------------------
    def _real_bucket_key(self):
        """이 스레드의 요청이 어느 앱키로 나가는지 판정한다.

        판정 근거는 실제로 헤더에 실리는 키와 같아야 한다(utils.get_common_headers,
        api.call_api의 키 선택 분기). 그쪽은 auto_app_key가 비면 real_app_key로
        폴백하므로, 여기서도 '자동 키가 있고 수동 키와 다를 때'만 auto로 가른다.
        """
        s = config.session
        if s.is_simulation:
            return self.BUCKET_MANUAL
        if getattr(context.trade_context, 'use_auto_account', False):
            auto = getattr(s, 'auto_app_key', '')
            if auto and auto != s.real_app_key:
                return self.BUCKET_AUTO
        return self.BUCKET_MANUAL

    def _real_bucket(self):
        return self._real_buckets[self._real_bucket_key()]

    # ---- 하위 호환 별칭 ---------------------------------------------------
    #  기존 코드·테스트가 쓰던 평면 속성은 '수동' 버킷을 가리킨다. 계좌를 분리하지
    #  않은 환경에서는 버킷이 하나뿐이라 종전과 완전히 동일하게 동작한다.
    @property
    def request_history_real(self):
        return self._real_buckets[self.BUCKET_MANUAL].history

    @property
    def adaptive_limit_real(self):
        return self._real_buckets[self.BUCKET_MANUAL].adaptive_limit

    @adaptive_limit_real.setter
    def adaptive_limit_real(self, v):
        self._real_buckets[self.BUCKET_MANUAL].adaptive_limit = v

    @property
    def _last_tps_raise(self):
        return self._real_buckets[self.BUCKET_MANUAL].last_raise

    @_last_tps_raise.setter
    def _last_tps_raise(self, v):
        self._real_buckets[self.BUCKET_MANUAL].last_raise = v

    @property
    def _last_tps_drop(self):
        return self._real_buckets[self.BUCKET_MANUAL].last_drop

    @_last_tps_drop.setter
    def _last_tps_drop(self, v):
        self._real_buckets[self.BUCKET_MANUAL].last_drop = v

    @property
    def _last_priority_grant(self):
        return self._real_buckets[self.BUCKET_MANUAL].last_priority_grant

    @_last_priority_grant.setter
    def _last_priority_grant(self, v):
        self._real_buckets[self.BUCKET_MANUAL].last_priority_grant = v

    @property
    def gate_grants_real(self):
        return self._real_buckets[self.BUCKET_MANUAL].grants

    @gate_grants_real.setter
    def gate_grants_real(self, v):
        self._real_buckets[self.BUCKET_MANUAL].grants = v

    def _real_tps_bounds(self):
        nominal = config.REAL_TX_PER_SECOND
        lo = nominal * getattr(config, 'REAL_TPS_SAFETY_MIN', 0.85)
        hi = nominal * getattr(config, 'REAL_TPS_SAFETY_MAX', 0.98)
        start = nominal * getattr(config, 'REAL_TPS_SAFETY', 0.9)
        return lo, hi, start

    def _tps_on_success_real(self):
        """실전 성공(레이트리밋 아님) 시 실효 TPS를 가산 증가(마진 축소)시킨다.

        [Fix 2026-08-05] 증가는 **윈도우(1초)당 한 번**이다. 종전에는 성공 1건마다
        올렸는데, 그러면 실효 상승률이 윈도우 크기(초당 ~18건)배로 뻥튀기된다.
        바닥(17)에서 천장(19.6)까지 2.6 TPS = 성공 52건 = 약 3초. 즉 EGW00201로
        물러나도 3초면 천장에 다시 붙고 또 걸린다 — **평형점이 천장이고 서버 한도는
        20이라, 레이트리밋이 상시 발생하는 정상 상태가 된다**(실측: 30분간 100건 이상).
        AIMD의 증가는 원래 RTT(윈도우)당 1단위이지 패킷당이 아니다. 주기를 바로잡아야
        컨트롤러가 천장이 아니라 실제 한도 아래에서 수렴한다.
        """
        with self.lock:
            b = self._real_bucket()          # 앱키별로 따로 수렴시킨다
            now = time.time()
            if now - b.last_raise < 1.0:
                return
            b.last_raise = now
            lo, hi, start = self._real_tps_bounds()
            cur = b.adaptive_limit if b.adaptive_limit is not None else start
            b.adaptive_limit = min(hi, cur + getattr(config, 'TPS_ADAPT_STEP', 0.05))

    def _tps_on_rate_limit_real(self, url=None, tr_id=None):
        """실전 EGW00201(초당 거래건수 초과) 시 실효 TPS를 곱셈 감소시키고, 거부를 집계한다.

        [설계] 실효 한도를 설정으로 못 박지 않는다. 명목(20)에서 출발해 거부가 나면 물러나고
        멎으면 되올린다 — 그날의 무릎을 서버가 알려주게 한다. 이게 성립하려면 밴드가 실제
        한도를 걸쳐야 하는데, 종전 밴드 [17, 19.6]은 실측 무릎(6)보다 통째로 위에 있어
        컨트롤러가 하한에 눌린 채 4일을 보냈다(운영 로그 495건 중 100%가 '하한 도달').

        [로그] 건건이 남기지 않는다. 거부는 초당 수 건까지 나므로 그것만으로 로그가 찬다.
        TPS_LOG_INTERVAL_SEC 마다 한 줄로 묶되 첫 거부는 즉시 남기고, 사후에 상황을
        재구성할 값은 그 한 줄에 모두 담는다 — 전송률·한도 궤적·최다 TR·스레드·게이트
        미경유 재전송·중복 프로세스. 원인 후보를 가르는 값들이라 하나도 빼지 않는다.
        """
        try:
            th = threading.current_thread().name
        except Exception:
            th = "?"

        emit = None
        with self.lock:
            # 거부는 앱키 단위로 온다. 한쪽 키가 물러난 것을 근거로 다른 키까지 낮추면
            #  멀쩡한 예산을 버린다 — 백오프도 버킷별로 적용한다.
            b = self._real_bucket()
            bucket_key = self._real_bucket_key()
            lo, hi, start = self._real_tps_bounds()
            cur = b.adaptive_limit if b.adaptive_limit is not None else start
            now = time.time()

            # [1] 곱셈 감소는 윈도우당 한 번. 한 번의 초과에는 여러 스레드가 동시에 거부되는데
            #  건건이 곱하면 한 혼잡에 ×0.9가 수십 번 걸려 한도가 바닥까지 무너진다.
            # [2] 기준은 설정 한도가 아니라 **직전 1초에 실제로 보낸 건수**다. 한도가 20인데
            #  실제로는 8/s를 보내는 중이면 20×0.9=18은 아무것도 바꾸지 못하는 헛걸음이고,
            #  그 헛걸음이 쌓여 [1]의 붕괴를 만든다. 실측 전송률에서 물러나야 한 번에 맞는다.
            if now - b.last_drop >= float(getattr(config, 'TPS_BACKOFF_WINDOW_SEC', 1.0) or 0):
                sent_1s = sum(1 for t in b.history if t > now - 1.0)
                ref = min(cur, float(sent_1s)) if sent_1s > 0 else cur
                b.adaptive_limit = max(lo, ref * getattr(config, 'TPS_ADAPT_BACKOFF', 0.9))
                b.last_drop = now
                # 물러난 직후 곧바로 올리지 않는다(한 윈도우는 낮춘 값으로 관찰한다).
                b.last_raise = now
            elif b.adaptive_limit is None:
                b.adaptive_limit = cur   # 창 안이라 안 내리더라도 값은 확정해 둔다

            if b.rl_count == 0:                 # 집계 창 시작
                b.rl_window_start = now
                b.rl_limit_from = cur
                b.rl_grants_from = b.grants
            b.rl_count += 1
            b.rl_trs[tr_id or (str(url or '-').split('?')[0].rstrip('/').split('/')[-1] or '-')] += 1
            b.rl_threads[th] += 1

            interval = float(getattr(config, 'TPS_LOG_INTERVAL_SEC', 60) or 0)
            if now - b.rl_last_emit >= interval:
                span = max(1e-3, now - b.rl_window_start)
                emit = {
                    'bucket': bucket_key,
                    'n': b.rl_count,
                    'span': span,
                    'rate': (b.grants - b.rl_grants_from) / span,
                    'from': b.rl_limit_from,
                    'to': b.adaptive_limit,
                    'floor': b.adaptive_limit <= lo + 1e-9,
                    'trs': b.rl_trs.most_common(2),
                    'tr_kinds': len(b.rl_trs),
                    'threads': b.rl_threads.most_common(1),
                    'th_kinds': len(b.rl_threads),
                }
                b.rl_last_emit = now
                b.rl_count = 0
                b.rl_trs = Counter()
                b.rl_threads = Counter()

        if not emit:
            return

        def _top(pairs, kinds):
            body = "·".join(f"{k}×{v}" for k, v in pairs)
            return f"{body} 외 {kinds - len(pairs)}종" if kinds > len(pairs) else body

        head = (f"{emit['n']}건(첫 거부)" if emit['span'] < 1.0 and emit['n'] == 1
                else f"{emit['n']}건/{emit['span']:.0f}s")
        bucket_tag = "자동매매키" if emit['bucket'] == self.BUCKET_AUTO else "수동키"
        logger.warning(
            f"[TPS/{bucket_tag}] EGW00201 {head} — 전송 {emit['rate']:.1f}/s, "
            f"실효 한도 {emit['from']:.2f}→{emit['to']:.2f} TPS{' (하한)' if emit['floor'] else ''}, "
            f"TR {_top(emit['trs'], emit['tr_kinds'])}, "
            f"스레드 {_top(emit['threads'], emit['th_kinds'])}, "
            f"미경유 재전송 {GatedRetry.ungated_resends}, "
            f"{instance_lock.appkey_duplicate_note()}")

    def request(self, method, url, *args, **kwargs):
        is_real_server = "openapi.koreainvestment.com" in url and "openapivts" not in url
        is_sim_server = "openapivts.koreainvestment.com" in url
        
        # [수정] 재시도 횟수 설정 (kwargs에서 전달받거나 config 기본값 사용)
        max_retries = kwargs.pop('retries', config.MAX_RETRIES)
        if max_retries is None: max_retries = config.MAX_RETRIES
        
        # [추가] 모의투자의 엄격한 TPS 제어로 인한 빈번한 차단을 방지하기 위해 기본 재시도 횟수 +1 추가
        if is_sim_server and max_retries == config.MAX_RETRIES:
            max_retries += 1
        
        response = None
        # 거부 진단용. 어느 TR이 거부되는지가 '엔드포인트 하위 한도 vs 앱키 단위 한도'를 가른다.
        try:
            req_tr_id = (kwargs.get('headers') or {}).get('tr_id')
        except Exception:
            req_tr_id = None
        # [TPS 우선순위] 스레드 단위로 한 번만 판정한다(재시도 중에 바뀌지 않는다).
        is_priority = _is_system_priority()

        for attempt in range(max_retries + 1):
            target_limit = 0
            server_type = "EXTERNAL"
            wait_time = 0
            current_tps = 0
            
            # [Fix] 스케줄링 지연으로 인해 스레드들이 동시에 깨어나 융단 폭격을 하는 현상을 
            # 원천 차단하기 위해, 예약 방식에서 폴링/토큰 획득 방식으로 TPS 제어 재설계
            if is_real_server or is_sim_server:
                while True:
                    wait_time = 0
                    with self.lock:
                        now = time.time()
                        target_limit = config.REAL_TX_PER_SECOND if is_real_server else config.SIM_TX_PER_SECOND
                        # [앱키별 예산] KIS 유량 한도는 앱키 단위다. 수동 계좌와 자동매매
                        #  계좌의 앱키가 다르면 각자 20 TPS를 따로 받으므로 카운터도 갈라야
                        #  한다. 같은 키면 같은 버킷으로 모여 종전과 동일하게 동작한다.
                        #  모의 서버도 우선순위 예약분 해제 판정에 last_priority_grant를
                        #  읽으므로 버킷 자체는 항상 잡는다(모의는 항상 manual 버킷).
                        bucket = self._real_bucket()
                        history = bucket.history if is_real_server else self.request_history_sim
                        server_type = "REAL" if is_real_server else "SIMULATION"
                        
                        if target_limit > 0:
                            # [수정] 명목 한도(target_limit)에 내부 안전계수를 곱해 '실효 한도'로 운행한다.
                            #  - 명목 한도에 정확히 붙이면 클라이언트 윈도우와 KIS 서버 1초 카운터의
                            #    경계가 충돌해 EGW00201이 상시 발생하므로, 약간의 마진을 둔다.
                            #  - config 설정값(REAL_TX_PER_SECOND 등)은 그대로 두고 로직 내부에서만 보정.
                            if is_sim_server:
                                effective_limit = target_limit  # 모의(2 TPS)는 기존 동작 유지
                                min_interval = (1.0 / target_limit) * 1.2
                            else:
                                # [#7] 적응형 실효 한도(AIMD). 미초기화 시 시작 마진(REAL_TPS_SAFETY)으로 출발.
                                if bucket.adaptive_limit is None:
                                    bucket.adaptive_limit = target_limit * getattr(config, 'REAL_TPS_SAFETY', 0.9)
                                effective_limit = max(1.0, bucket.adaptive_limit)
                                min_interval = 1.0 / effective_limit

                            # 1. 윈도우 기반 한도 체크 (Burst 방어)
                            window_size = 1.5 if is_sim_server else 1.1

                            while history and history[0] <= now - window_size:
                                history.popleft()

                            # 2. 최소 간격 체크 (고르게 분산)
                            time_since_last = now - history[-1] if history else float('inf')

                            # [TPS 우선순위] 조회성 호출은 매매용 예약분을 뺀 나머지만 쓴다.
                            #  매매 판단·주문은 시각이 곧 가격이라 미룰 수 없고, 조회 메뉴는 몇 초
                            #  늦어도 무방하다. 종전에는 비율(×0.5)로 쪼갰는데, 실효 한도가 AIMD로
                            #  움직이면 비율은 틀린 도구다 — 한도 20에서 조회 10은 넉넉하지만 한도
                            #  6에서 조회 3은 메뉴가 못 쓸 만큼 느리면서 매매 몫도 3뿐이다.
                            #  절대량 예약은 한도가 어디로 가든 매매 헤드룸을 그대로 지킨다.
                            gate_limit = effective_limit
                            gate_interval = min_interval
                            # [속도] 균등 전송을 끄면 창 한도만 지키고 그 안에서는 몰아 보낸다.
                            #  창(1.1초) 상한이 곧 초당 상한이므로 명목 20 TPS를 넘지 않는다 —
                            #  1.0초는 1.1초의 부분구간이라 어떤 1초를 잘라도 20건 이하다.
                            #  실측상 몰아 보내는 쪽이 처리량이 높다(30연결 폭주 측정).
                            #  모의투자(2 TPS)는 여유가 없어 균등 전송을 유지한다.
                            if is_real_server and not getattr(config, 'TPS_EVEN_PACING', True):
                                gate_interval = 0.0

                            if not is_priority:
                                reserve = float(getattr(config, 'PRIORITY_RESERVE_TPS', 2.0) or 0.0)
                                # 매매가 지금 돌고 있지 않으면 예약분을 풀어 준다. 떼어 두기만
                                #  하고 아무도 안 쓰면 그냥 버려지는 몫이다(주말·자동매매 미가동).
                                idle = float(getattr(config, 'PRIORITY_RESERVE_IDLE_SEC', 10.0) or 0.0)
                                if idle > 0 and (now - bucket.last_priority_grant) > idle:
                                    reserve = 0.0
                                if reserve > 0:
                                    gate_limit = max(1.0, effective_limit - reserve)
                                    if gate_interval > 0:
                                        # 창 한도만 낮추고 간격을 그대로 두면 순간 버스트가 남는다.
                                        gate_interval = max(min_interval, 1.0 / gate_limit)

                            if len(history) < gate_limit and time_since_last >= gate_interval:
                                history.append(now)
                                current_tps = len(history)
                                if is_real_server:
                                    bucket.grants += 1   # [진단] 집계 로그의 전송률 산출용
                                if is_priority:
                                    bucket.last_priority_grant = now
                                break # 락 해제 후 전송 진행
                            else:
                                wait_from_window = (history[0] + window_size) - now if len(history) >= gate_limit else 0
                                wait_from_interval = gate_interval - time_since_last
                                wait_time = max(wait_from_window, wait_from_interval)
                                if wait_time <= 0: wait_time = 0.05
                        else:
                            break # 한도 미설정 시 즉시 통과
                            
                    # 전송 권한을 얻지 못한 경우 락을 반환하고 대기한 후 다시 락을 잡아 권한 획득 시도
                    time.sleep(wait_time)

            if _api()._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
                # [보안] 화면 출력도 마스킹한다. 파일 로그만 가리면 SSH·화면 공유로 샌다.
                config.console.print(f"[dim cyan][TRACE] REQ ({server_type}) TPS:{current_tps:.1f} | {method} {config.mask_sensitive(url)}[/dim cyan]")
                if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                    if kwargs.get('params'): config.console.print(f"[dim cyan]  > Params: {kwargs['params']}[/dim cyan]")
                    if kwargs.get('data'): config.console.print(f"[dim cyan]  > Body Data: {kwargs['data']}[/dim cyan]")
                    if kwargs.get('json'): config.console.print(f"[dim cyan]  > JSON Data: {kwargs['json']}[/dim cyan]")

            try:
                if 'timeout' not in kwargs:
                    kwargs['timeout'] = config.DEFAULT_TIMEOUT

                response = super().request(method, url, *args, **kwargs)

                if _api()._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
                    rt_cd = "-"
                    msg_cd = "-"
                    desc = "정상"
                    res_data = None
                    try:
                        res_data = response.json()
                        rt_cd = res_data.get('rt_cd') or "-"
                        msg_cd = res_data.get('msg_cd') or "-"
                        if msg_cd == 'OPSQ2000': desc = "서버 지연"
                        elif msg_cd in ['EGW00123', 'EGW00121']: desc = "토큰 만료"
                        elif rt_cd != '0' and rt_cd != '-': desc = "오류 발생"
                    except Exception as e:
                        logger.debug(f"API response logging json parse error: {e}")
                    
                    url_tail = url.split('/')[-1].split('?')[0]
                    config.console.print(f"[dim magenta][TRACE] RES ({server_type}) Status:{response.status_code} RT_CD:{rt_cd} MSG_CD:{msg_cd} ({desc}) | {config.mask_sensitive(url_tail)}[/dim magenta]")
                    
                    if config.SCREEN_DEBUG_LEVEL == "DEBUG" and res_data:
                        config.console.print(f"[dim magenta]  > Response Data: {json.dumps(res_data, ensure_ascii=False, indent=2)}[/dim magenta]")

                # [수정] 통합 재시도 로직 (모든 에러 상황 처리)
                should_retry = False
                # 이 응답에 대해 레이트리밋 백오프를 이미 적용했는가(중복 적용 방지).
                rate_limited_handled = False
                retry_reason = ""

                # 1. HTTP Status 확인
                if response.status_code != 200:
                    # [수정] HTTP 에러는 재시도하지 않고 로그만 기록 (연결 끊김은 except 블록에서 처리됨)
                    try:
                        body_preview = response.text[:500]
                        # [수정] EGW00201/EGW00215(초당 거래건수 초과)는 스로틀 백오프로
                        #        재시도되어 정상 복구되는 흐름이므로 ERROR가 아닌 DEBUG로 강등.
                        #        (Status 500으로 내려오지만 실제 장애가 아니라 Rate Limit임)
                        if 'EGW00201' in body_preview or 'EGW00215' in body_preview:
                            logger.debug(f"[Rate Limit] TPS 초과 응답 → 스로틀 백오프 후 재시도. URL: {url}")
                            if is_real_server:
                                # [#7] 실효 TPS 곱셈 감소 (+ 거부된 요청의 TR·URL·전송시각 기록)
                                self._tps_on_rate_limit_real(url=url, tr_id=req_tr_id)
                                # [Fix] 아래 msg_cd 분기가 같은 응답을 한 번 더 처리한다.
                                #  한 번의 거부에 백오프가 두 번 걸려 실효 한도가 실제보다
                                #  두 배 빠르게 내려가고, 진단 로그도 매번 두 줄씩 남았다.
                                rate_limited_handled = True
                        else:
                            logger.error(f"⚠️ [HTTP Error] URL: {url} | Status: {response.status_code} | Body: {body_preview}")
                    except Exception: pass
                
                # 2. API 응답 코드 확인
                if not should_retry:
                    # [수정] OAuth 토큰 발급 요청 등은 rt_cd 구조가 다르므로 검사 제외
                    if "oauth2" in url:
                        pass
                    elif is_sim_server or is_real_server:
                        try:
                            res_json = response.json()
                            rt_cd = res_json.get('rt_cd')
                            msg_cd = res_json.get('msg_cd')
                            msg1 = res_json.get('msg1', '')

                            # [#7] 실전 정상 응답이면 실효 TPS를 가산 증가(마진 축소)시킨다.
                            if is_real_server and rt_cd == '0':
                                self._tps_on_success_real()

                            # [추가] 지수 조회 API(실전)의 빈 응답 이슈 예외 처리
                            # 실전투자 서버에서 지수 조회 시 rt_cd가 없거나 빈 값으로 오는 경우가 있음 -> 에러 로그 제외하고 Fallback 유도
                            if "inquire-daily-indexchartprice" in url and (not rt_cd or rt_cd != '0'):
                                return response

                            # 토큰 만료 처리 (특수 케이스: 갱신 후 재시도)
                            if msg_cd in ['EGW00123', 'EGW00121']:
                                # [수정] 자동 갱신 로직 삭제. 만료 플래그만 설정하고 예외 발생시킴.
                                logger.error(f"토큰 만료 감지(Code: {msg_cd}). 메인 스레드에 갱신을 요청합니다.")
                                context.TOKEN_EXPIRED = True
                                raise Exception(f"Token Expired ({msg_cd})")
                            
                            # 그 외 모든 API 에러 (성공이 아닌 경우)
                            elif rt_cd is not None and rt_cd != '0':
                                rt_disp = rt_cd if rt_cd else "(Empty)"
                                msg_disp = msg_cd if msg_cd else "(Empty)"
                                msg1_disp = msg1 if msg1 else "(Empty)"
                                
                                # EGW00201: 전체 API 초당 거래건수 초과
                                # EGW00215: 원장(계좌/주문) API 초당 거래건수 초과
                                if msg_cd == 'EGW00201' or (msg_cd == 'EGW00215' and 'inquire' in url):
                                    should_retry = True
                                    retry_reason = f"Rate Limit Exceeded ({msg_cd}): {msg1_disp}"
                                    if is_real_server and not rate_limited_handled:
                                        self._tps_on_rate_limit_real(url=url, tr_id=req_tr_id)
                                elif msg_cd == 'EGW00215' and 'inquire' not in url:
                                    # 주문과 같이 상태 변화가 있는 API는 중복 방지를 위해 재시도하지 않음
                                    req_body = kwargs.get('data', '')
                                    logger.error(f"⚠️ [ORDER_FAIL] [API Error] URL: {url} | RT_CD: {rt_disp} | MSG_CD: {msg_disp} | MSG: {msg1_disp} | REQ: {req_body}")
                                elif msg_cd == 'MCA00124' and 'chk-holiday' in url and is_sim_server:
                                    # 모의투자 서버에서 휴장일 조회 미지원 에러 로그 무시 (모의투자 모드에서만 동작하도록 명확화)
                                    pass
                                else:
                                    # 단순 조회(GET) 요청이면서 일시적인 API 서버/MCI 오류인 경우 안전하게 재시도 처리
                                    if method == 'GET' and ('MCI' in msg1_disp or '게이트웨이' in msg1_disp):
                                        should_retry = True
                                        retry_reason = f"KIS Server Intermittent Error ({msg_cd}): {msg1_disp}"
                                    else:
                                        req_body = kwargs.get('data', '')
                                        # 조회 API와 주문 API를 구분하여 에러 로그 출력
                                        if method == 'GET' or 'inquire' in url:
                                            logger.error(f"⚠️ [API Error] URL: {url} | RT_CD: {rt_disp} | MSG_CD: {msg_disp} | MSG: {msg1_disp}")
                                        else:
                                            logger.error(f"⚠️ [ORDER_FAIL] [API Error] URL: {url} | RT_CD: {rt_disp} | MSG_CD: {msg_disp} | MSG: {msg1_disp} | REQ: {req_body}")

                        except Exception as e:
                            # JSON 파싱 실패 등
                            if not str(e).startswith("Token Expired"):
                                # [수정] 파싱 에러는 재시도하지 않음
                                # should_retry = True
                                # retry_reason = f"Response Parsing Error: {e}"
                                # [추가] 파싱 에러 상세 로깅
                                logger.error(f"⚠️ [Parsing Error] URL: {url} | Error: {e} | Body: {response.text[:500]}")
                            else:
                                raise e # 토큰 만료 예외는 그대로 전달
                
                if not should_retry:
                    return response
                
                # 재시도 대상이면 예외를 발생시켜 아래 except 블록에서 처리
                raise Exception(retry_reason)

            except Exception as e:
                # [Fix] 토큰 만료 예외는 내부 재시도(ThrottledSession)를 하지 않고 즉시 상위(call_api)로 전파
                if "Token Expired" in str(e):
                    raise e

                # [Fix 2026-08-10] 응답을 못 받은 주문은 재전송하지 않는다.
                #  타임아웃은 '실패'가 아니라 '모름'이다. 주문이 거래소에 닿아 체결된 뒤
                #  응답만 유실됐을 수 있고, 그때 재전송하면 같은 주문이 두 번 나간다.
                #  포지션이 두 배가 되면 손절폭·변동성 한도·포트폴리오 히트 캡이 한꺼번에
                #  무의미해진다 — 이 시스템의 1차 통제가 수량 산정 하나에 실려 있기 때문이다.
                #  응답을 받은 거부(EGW00201 등)는 주문이 안 들어간 것이 확정이므로 종전대로
                #  재시도한다. 여기서 갈리는 기준은 '응답을 받았는가' 하나다.
                if _is_state_changing(method, url) and _is_response_unknown(e):
                    logger.error(f"⚠️ [ORDER_UNKNOWN] 주문 응답 없음 — 재전송하지 않습니다. "
                                 f"URL: {url} | 사유: {e}")
                    raise OrderOutcomeUnknown(str(e)) from e

                # [수정] 모든 예외(연결 끊김, API 에러 등)에 대해 백오프 후 재시도
                if attempt < max_retries:
                    # [수정] 대기 시간 계산은 _retry_wait_seconds로 분리 — Rate Limit은 짧은
                    #  선형 대기, 진짜 장애는 종전 지수 백오프. (근거는 해당 함수 주석 참조)
                    wait_time = _retry_wait_seconds(attempt, str(e))

                    msg = f"⚠️ API 요청 실패. {wait_time:.1f}초 후 재시도합니다. 사유: {str(e)}"
                    # [수정] Rate Limit(EGW00201/EGW00215)은 정상적인 백오프 재시도 흐름이므로
                    #        DEBUG로 강등하여 로그 노이즈를 줄인다. (진짜 오류만 WARNING 유지)
                    if 'EGW00201' in str(e) or 'EGW00215' in str(e):
                        logger.debug(msg)
                    else:
                        logger.warning(msg)
                    
                    if _api()._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim yellow][TRACE] {msg}[/dim yellow]")
                    
                    time.sleep(wait_time)
                    continue
                
                # [수정] 최종 실패 시 로그 출력 및 마지막 응답/예외 반환
                logger.error(f"⚠️ API 요청 최종 실패. 사유: {str(e)}")
                if response is not None:
                    return response
                raise e
        
        return response

session = ThrottledSession()

class GatedRetry(Retry):
    """어댑터(urllib3) 레벨 재시도 횟수를 세는 Retry.

    [왜 세는가] 어댑터 재시도는 super().request() 내부(=TPS 게이트 아래)에서 일어나므로
    게이트의 전송 히스토리에 잡히지 않는다. 즉 한 논리 요청이 소켓에는 여러 번 나가도
    로그에는 1건으로 보인다 — '전송률은 낮은데 서버가 EGW00201로 거부한다'는 관측과
    정확히 같은 모양이라, 재시도가 봉인돼 있는지 로그로 확인할 수 있어야 한다.
    이 값이 0이 아니면 원인은 게이트 밖(다른 프로세스)이 아니라 게이트 아래다.

    Retry.new() 가 type(self) 로 복제하므로 재시도 사슬 전체가 이 클래스를 유지한다.
    """

    ungated_resends = 0
    _count_lock = threading.Lock()

    def increment(self, *args, **kwargs):
        # 예산이 소진되면 super() 가 MaxRetryError 를 던진다 — 그때는 재전송이 없으므로
        #  세지 않는다(예외가 이 줄을 건너뛴다).
        new_retry = super().increment(*args, **kwargs)
        with GatedRetry._count_lock:
            GatedRetry.ungated_resends += 1
        return new_retry


# [수정] 어댑터 레벨 재시도를 전면 봉인한다(total=0).
# [중요] HTTP 5xx(특히 EGW00201/EGW00215는 Status 500으로 내려옴)는 원래 제외돼 있었지만,
#  연결 끊김(RemoteDisconnected)에 대한 재시도 2회가 남아 있었다. 그 재전송은 TPS 게이트를
#  거치지 않으므로 한 논리 요청이 소켓에는 최대 3번 나간다 — 게이트가 8.5 TPS로 세는 동안
#  실제로는 최대 25 TPS가 나갈 수 있고, 그러면 서버는 거부하는데 우리 로그는 여유롭게
#  보인다(2026-08-09 EGW00201 진단).
#  기능 손실은 없다. 연결 끊김은 앱 레벨(ThrottledSession.request)의 except 가 그대로
#  재시도하며, 그 경로는 게이트를 다시 지난다. 대기 시간도 _retry_wait_seconds 가
#  연결 끊김을 짧은 선형 대기로 분리해 종전 체감 지연을 유지한다.
retry_strategy = GatedRetry(
    total=0,
    backoff_factor=0.5,
    status_forcelist=[],
    allowed_methods=["GET", "POST"],
    raise_on_status=False
)

# [수정] 실전투자(20 TPS) 병렬 처리를 위해 커넥션 풀 크기 확장 (기본 10 -> 30)
# 동시에 많은 네트워크 요청이 발생해도 커넥션 대기 없이 즉시 처리 가능
session.mount('https://', TLSAdapter(max_retries=retry_strategy, pool_connections=30, pool_maxsize=30))
