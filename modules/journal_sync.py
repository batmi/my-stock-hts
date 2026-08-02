"""매매일지 웹서버 연동 — Universal Trading History API v2 클라이언트.

Outbox 패턴
-----------
체결 처리 경로에서는 **절대 네트워크를 타지 않는다.**
`db_manager.insert_trade()` 가 거래 기록과 같은 트랜잭션으로 `journal_outbox` 에
적재하고, 이 모듈의 백그라운드 워커가 배치로 전송한다.

  - 체결 확인 루프가 네트워크 지연(수 초)에 묶이지 않는다
  - 라즈베리파이가 단절·재부팅돼도 큐가 DB에 남아 자동으로 복구된다
  - 서버가 brokerExecutionId 로 멱등 처리하므로 재전송이 언제나 안전하다

2단 방어
--------
1단(큐)이 막지 못하는 구멍이 하나 있다. `enqueue()` 는 `is_enabled()` 뒤에 있어서
**연동이 꺼져 있던 동안의 체결은 큐에 들어가지도 않는다.** 메뉴 토글 OFF, 환경변수
누락으로 재시작된 구간이 여기 해당하고, 이건 재시도로는 영영 복구되지 않는다.

  1단 큐   : 서버가 죽어 있는 동안의 체결 — 큐에 남았다가 복구 시 자동 전송
  2단 백필 : 연동이 꺼져 있던 동안의 체결 — `backfill_once()` 가 로컬 `trades` 를
             서버의 마지막 동기화 지점과 대조해 큐에 없는 건을 주워 담는다

전송을 포기하는 기준
--------------------
서버가 **이 건을 명시적으로 거절한** 횟수(`reject_count`)만 세어 `_MAX_REJECTS` 에
닿으면 dead-letter 로 뺀다. 통신 실패는 세지 않는다 — 세면 웹서버가 오래 죽어 있을 때
멀쩡한 대기열이 통째로 폐기된다. 반대로 아예 빼지 않으면 서버가 영구 거절하는 행
하나가 배치 앞자리를 잡고 뒤의 정상 건까지 막는다.

멱등키
------
`{env}:{계좌}:{체결일}:{주문번호}:{상태}` 형태로 만든다. 증권사 주문번호(odno)는
영업일마다 재사용되므로 주문번호만 쓰면 다른 날의 다른 체결이 중복으로 오인되어
서버에서 조용히 버려진다. 계좌·일자를 반드시 포함해야 한다.

설정 (~/.htsrc 에 export 후 재시작)
-----------------------------------
  export JOURNAL_API_URL="https://your-host"     # 필수 (미설정 시 연동 전체 비활성)
  export JOURNAL_API_KEY="skm_..."               # 필수 (웹 설정에서 발급)
  export JOURNAL_SOURCE="my-stock-hts"           # 선택 (last-sync 스코프 기준)
  export JOURNAL_SYNC_SIMULATION="0"             # 선택, 1이면 모의투자 체결도 전송
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# 전송 대상 주문 상태. '접수'는 아직 체결이 아니고, 취소는 체결 기록이 아니므로 보내지 않는다.
#  '체결(추정)'은 잔고 대조로 추정한 건이라 confidence=ESTIMATED 로 표시해 전송한다.
_SYNCABLE_STATUS = {
    '체결': 'CONFIRMED',
    '체결(추정)': 'ESTIMATED',
}

_FLUSH_INTERVAL_SEC = 30      # 대기열 전송 주기
_BATCH_SIZE = 100             # 한 번에 보낼 최대 건수 (라즈베리파이 메모리 여유 고려)
_HTTP_TIMEOUT = 8             # 초 — 짧게 잡아 워커가 오래 물리지 않게 한다
# 서버가 '이 건'을 명시적으로 거절한 횟수가 이 값에 닿으면 대기열에서 뺀다(dead-letter).
#  통신 실패(서버 다운·타임아웃)는 여기에 세지 않는다 — 세면 웹서버가 반나절만 죽어 있어도
#  대기열 전체가 폐기된다. 세는 것은 서버가 응답으로 거절 사유를 준 경우뿐이라,
#  여기까지 온 행은 재시도해도 결과가 달라지지 않는 페이로드다.
_MAX_REJECTS = 5

# 전송 완료 행 보존 기간. 라즈베리파이 SD카드에 payload JSON 이 무한 누적되는 걸 막는다.
# 백필 스캔 범위(_BACKFILL_LOOKBACK_DAYS)보다 반드시 길어야 한다 — 짧으면 이미 보낸 건이
# 큐에서 사라진 뒤 백필이 다시 주워 담아 매번 재전송한다.
_RETENTION_DAYS = 90
_PURGE_INTERVAL_SEC = 24 * 3600

# 백필(누락 회수) — 큐에 아예 들어가지 못한 체결을 로컬 trades 에서 찾아 회수한다.
#  연동 토글이 꺼져 있었거나 환경변수가 빠진 채 돌던 구간의 체결이 여기에 해당한다.
#  그 구간엔 enqueue() 가 통째로 건너뛰므로 큐 재시도로는 영원히 복구되지 않는다.
_BACKFILL_INTERVAL_SEC = 6 * 3600
_BACKFILL_STARTUP_DELAY_SEC = 60   # 기동 직후는 로그인·유니버스 적재가 끝나길 기다린다
_BACKFILL_LOOKBACK_DAYS = 30       # 서버에 기록이 하나도 없을 때 거슬러 올라갈 기본 범위
_BACKFILL_OVERLAP_MIN = 10         # 마지막 동기화 지점 앞뒤 경계에서 빠지는 건이 없도록
_BACKFILL_MAX_ROWS = 500           # 한 번에 스캔할 로컬 행 상한 (라파 메모리 보호)

# 봇 상태 Ping 주기. 웹 대시보드는 3회 연속 누락(+여유)되면 '통신단절'로 표시하므로,
# 이 값을 늘리면 장애 감지가 그만큼 늦어진다. 서버 상수와 짝을 이룬다.
_PING_INTERVAL_SEC = 10
_PING_TIMEOUT = 4             # 초 — 10초마다 도는 하트비트가 통신 지연에 오래 물리면 안 된다
_TICK_INTERVAL_SEC = _PING_INTERVAL_SEC   # 워커 순회 주기(Ping 주기에 맞춘다)
# 10초 간격이라 실패 로그를 매번 남기면 로그가 이것만으로 찬다.
# 첫 실패(= 감지 시점)와 이후 5분마다 한 번씩만 WARNING 으로 남긴다.
_PING_WARN_EVERY = 30
_SHUTDOWN_PING_TIMEOUT = 3    # 초 — 종료 통지가 프로그램 종료를 붙잡지 않도록 짧게


# ══════════════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════════════

def _cfg(name, default=''):
    return getattr(config, name, default) or default


def _has_credentials():
    return bool(_cfg('JOURNAL_API_URL') and _cfg('JOURNAL_API_KEY'))


def is_enabled():
    """메뉴 0 토글이 켜져 있고, URL·API 키가 모두 설정돼야 연동이 동작한다.

    설정(JOURNAL_SYNC_USE)과 자격증명(환경변수)을 분리한 이유:
      - 자격증명은 소스·설정파일에 남기면 안 되므로 환경변수로만 받는다
      - 사용 여부는 재시작 없이 껐다 켤 수 있어야 하므로 dynamic_config 에 둔다
    """
    if not getattr(config.settings, 'JOURNAL_SYNC_USE', False):
        return False
    return _has_credentials()


def _base_url():
    return _cfg('JOURNAL_API_URL').rstrip('/')


def _source():
    return _cfg('JOURNAL_SOURCE', 'my-stock-hts')


def _sync_simulation():
    return bool(getattr(config, 'JOURNAL_SYNC_SIMULATION', False))


# ══════════════════════════════════════════════════════════════════════
# 페이로드 변환
# ══════════════════════════════════════════════════════════════════════

def _is_overseas(code):
    """종목코드 형태로 해외 여부를 판단한다 (국내는 6자리 숫자)."""
    code = (code or '').strip()
    return not (len(code) == 6 and code.isdigit())


def _exchange_for(code, overseas):
    """거래소 코드. 서버가 이 값으로 **현지 거래일**을 계산하므로 해외는 특히 중요하다.

    미국 애프터마켓 체결(한국시간 새벽)을 거래소 정보 없이 보내면 서버가 KST 날짜로
    귀속시켜 거래일이 하루 밀린다. 매매 유니버스(stock.json)에 등록된 종목은
    거래소가 함께 들어 있으므로 그대로 쓴다.
    """
    if not overseas:
        return 'KRX'
    try:
        stock_data = getattr(config.session, 'stock_data', None) or {}
        for key in ('stocks_us', 'etfs_us'):
            for item in stock_data.get(key, []):
                if (item.get('code') or '').upper() == code.upper():
                    return item.get('exchange') or ''
    except Exception:
        pass
    # 유니버스 밖(수동·외부 주문)이면 추측하지 않고 비워 둔다. 서버는 요청에 실린
    # 오프셋 기준 날짜로 폴백하므로 최소한 잘못된 거래소명이 기록되진 않는다.
    return ''


def _order_origin(type_str):
    """`매수(AUTO)` 같은 로컬 타입 문자열에서 주문 출처를 뽑아낸다."""
    t = type_str or ''
    if '(AUTO)' in t:
        return 'AUTO'
    if '(예약)' in t:
        return 'RESERVED'
    if '(외부)' in t:
        return 'EXTERNAL'
    if '(수동)' in t:
        return 'MANUAL'
    return ''


def _side(type_str):
    t = type_str or ''
    if '매수' in t or 'buy' in t.lower():
        return 'BUY'
    if '매도' in t or 'sell' in t.lower():
        return 'SELL'
    return ''


def _executed_at(time_str, overseas):
    """로컬 체결시각(KST 문자열)을 오프셋 포함 RFC3339 로 바꾼다.

    로컬 DB의 `time` 은 한국 시간이므로 항상 +09:00 을 붙인다. 서버는 거래소
    코드를 함께 보고 현지 거래일을 계산하므로, 해외 체결도 이대로 보내면 된다.
    """
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            dt = datetime.strptime(str(time_str), fmt).replace(tzinfo=KST)
            return dt.strftime('%Y-%m-%dT%H:%M:%S%z')
        except (TypeError, ValueError):
            continue
    return datetime.now(KST).strftime('%Y-%m-%dT%H:%M:%S%z')


def _exec_id(trade):
    """멱등키 — {env}:{계좌}:{체결일}:{주문번호}:{상태}

    주문번호(odno)는 영업일마다 재사용되므로 계좌·일자 없이는 다른 날의 다른
    체결이 중복으로 오인되어 서버에서 조용히 버려진다.
    '체결(추정)'과 '체결'을 상태까지 키에 넣어 구분하면, 추정 기록이 먼저 올라간 뒤
    확정 기록이 별건으로 들어오는 대신 각각 남으므로 나중에 정정할 수 있다.
    """
    env = 'SIM' if trade.get('is_sim') else 'REAL'
    account = (trade.get('account') or '').replace('-', '')
    day = str(trade.get('time') or '')[:10].replace('-', '')
    odno = (trade.get('odno') or '').strip() or 'NOODNO'
    status = 'E' if trade.get('order_status') == '체결(추정)' else 'F'
    return f'{env}:{account}:{day}:{odno}:{status}'


def build_payload(trade):
    """로컬 trades 행(dict)을 API TradeRecordInput 으로 변환한다."""
    code = (trade.get('code') or '').strip()
    overseas = _is_overseas(code)
    status = trade.get('order_status')
    confidence = _SYNCABLE_STATUS.get(status, 'CONFIRMED')

    try:
        qty = float(trade.get('qty') or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        price = float(trade.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0

    payload = {
        'symbol': code,
        'name': trade.get('name') or '',
        'side': _side(trade.get('type')),
        'price': price,
        'volume': qty,
        'executedAt': _executed_at(trade.get('time'), overseas),
        'brokerExecutionId': _exec_id(trade),
        'isSimulated': bool(trade.get('is_sim')),
        'status': 'FILLED',
        'confidence': confidence,
        'source': _source(),
        'tradeClass': '시스템',
        'currency': 'USD' if overseas else 'KRW',
        'exchange': _exchange_for(code, overseas),
        'assetType': 'STOCK',
        'subAccount': (trade.get('account') or '').replace('-', ''),
        'orderId': trade.get('odno') or '',
        'memo': trade.get('reason') or '',
    }

    origin = _order_origin(trade.get('type'))
    if origin:
        payload['orderOrigin'] = origin
    if trade.get('org_odno'):
        payload['originalOrderId'] = trade['org_odno']

    # 매도 실현손익 — 봇이 이미 계산해 둔 값을 넘겨야 서버 통계가 정확해진다.
    if payload['side'] == 'SELL':
        try:
            if trade.get('profit_amt'):
                payload['realizedPnl'] = float(trade['profit_amt'])
        except (TypeError, ValueError):
            pass
        try:
            if trade.get('profit_rate'):
                payload['realizedPnlRate'] = float(trade['profit_rate'])
        except (TypeError, ValueError):
            pass

    for src, dst in (('strategy_score', 'strategyScore'), ('stop_loss_rate', 'stopLossRate')):
        try:
            value = float(trade.get(src) or 0)
            if value:
                payload[dst] = value
        except (TypeError, ValueError):
            pass

    return payload


# ══════════════════════════════════════════════════════════════════════
# 큐 적재 (db_manager 의 트랜잭션 안에서 호출)
# ══════════════════════════════════════════════════════════════════════

def enqueue(cursor, trade, quiet=False):
    """전송 대기열에 적재한다. 호출자의 트랜잭션·커서를 그대로 쓴다.

    전송 대상이 아니면 조용히 무시한다. **여기서 예외를 올리면 거래 기록 저장이
    함께 롤백되므로**, 판단이 애매하면 적재하지 않는 쪽을 택한다.

    quiet=True 는 백필처럼 수백 건을 한 번에 훑는 경로용이다. 건별 로그를 남기면
    로그가 그것만으로 차므로 호출부가 요약 한 줄만 남긴다.

    반환값이 True 면 INSERT 문을 실제로 실행했다는 뜻이다(중복이라 무시됐을 수도
    있다). 신규 적재 여부까지 알아야 하면 호출 직후 `cursor.rowcount` 를 본다.
    """
    if not is_enabled():
        return False
    if trade.get('order_status') not in _SYNCABLE_STATUS:
        return False
    if trade.get('is_sim') and not _sync_simulation():
        return False
    if not _side(trade.get('type')):
        return False  # 매수/매도로 해석되지 않는 기록(확인요망 등)은 보내지 않는다

    payload = build_payload(trade)
    if not payload['symbol'] or payload['volume'] <= 0:
        return False

    cursor.execute(
        "INSERT OR IGNORE INTO journal_outbox (exec_id, payload, created_at) VALUES (?, ?, ?)",
        (payload['brokerExecutionId'], json.dumps(payload, ensure_ascii=False),
         datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')))

    if quiet:
        return True
    if cursor.rowcount:
        logger.info(
            f"[Journal] 대기열 적재: {payload['side']} {payload['name']}({payload['symbol']}) "
            f"{payload['volume']:g}주 @{payload['price']:,g} "
            f"[{payload['brokerExecutionId']}]")
    else:
        # 같은 체결이 다시 기록된 경우(재확인·재시작 등). 중복 전송이 아니라 정상 동작이다.
        logger.info(f"[Journal] 대기열 중복 스킵: {payload['brokerExecutionId']}")
    return True


def enqueue_standalone(trade):
    """자체 커넥션으로 적재 (백필·수동 재전송 등 트랜잭션 밖에서 쓰는 경로)."""
    from modules import db_manager
    with db_manager.db.lock:
        conn = db_manager.db._get_conn()
        try:
            queued = enqueue(conn.cursor(), trade)
            if queued:
                conn.commit()
            return queued
        except Exception as e:
            logger.warning(f"[Journal] 큐 적재 실패: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════
# HTTP 클라이언트
# ══════════════════════════════════════════════════════════════════════

class _TokenCache:
    """Access Token 캐시. 만료 전 재사용하고 401 을 받으면 즉시 폐기한다."""

    def __init__(self):
        self._token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def invalidate(self):
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def get(self, force=False):
        with self._lock:
            if not force and self._token and time.time() < self._expires_at:
                return self._token

            import requests
            try:
                res = requests.post(
                    f'{_base_url()}/api/v1/auth/token',
                    headers={'X-API-KEY': _cfg('JOURNAL_API_KEY')},
                    timeout=_HTTP_TIMEOUT)
            except Exception as e:
                logger.warning(f"[Journal] 토큰 발급 요청 실패: {e}")
                return None

            if res.status_code != 200:
                logger.warning(f"[Journal] 토큰 발급 거부 ({res.status_code}): {res.text[:200]}")
                return None

            data = res.json()
            self._token = data.get('access_token')
            # 만료 5분 전에 미리 갱신해 경계에서 401 을 맞지 않게 한다.
            self._expires_at = time.time() + max(int(data.get('expires_in', 86400)) - 300, 60)
            logger.info(f"[Journal] 접속 토큰 발급 완료 "
                        f"(유효 {int(data.get('expires_in', 86400)) // 3600}시간, "
                        f"권한: {' '.join(data.get('scopes') or []) or '미표기'})")
            return self._token


_tokens = _TokenCache()


def _request(method, path, *, json_body=None, params=None, retry_on_401=True,
             timeout=None, quiet=False):
    """인증 헤더를 붙여 요청한다. (response 또는 None)

    quiet=True 는 호출부가 실패 로그를 직접 관리할 때 쓴다(하트비트 등).
    """
    import requests

    token = _tokens.get()
    if not token:
        return None

    try:
        res = requests.request(
            method, f'{_base_url()}{path}',
            headers={'Authorization': f'Bearer {token}'},
            json=json_body, params=params, timeout=timeout or _HTTP_TIMEOUT)
    except Exception as e:
        if not quiet:
            logger.warning(f"[Journal] {method} {path} 요청 실패: {e}")
        return None

    if res.status_code == 401 and retry_on_401:
        # 토큰 만료·키 폐기 — 한 번만 새로 받아 재시도한다.
        _tokens.invalidate()
        return _request(method, path, json_body=json_body, params=params,
                        retry_on_401=False, timeout=timeout, quiet=quiet)
    return res


# ══════════════════════════════════════════════════════════════════════
# 워커
# ══════════════════════════════════════════════════════════════════════

def _backoff_seconds(attempts):
    """지수 백오프 (상한 1시간). 서버가 죽어 있을 때 헛된 재시도를 줄인다."""
    return min(60 * (2 ** min(attempts, 6)), 3600)


# 백오프 판정을 SQL 로 내린 표현식. `_backoff_seconds` 와 같은 식이어야 한다.
#  (SQLite: `<<` 는 시프트, 인자 2개짜리 min() 은 스칼라 최솟값)
#  파이썬에서 걸러내면 '아직 대기 중인 행'도 스캔 한도를 차지해, 적체가 한도를 넘는
#  순간 뒤쪽 행이 조회조차 되지 않는다(전송 가능한데 영영 안 나감).
_BACKOFF_SQL = "min(60 * (1 << min(attempts, 6)), 3600)"


def _fetch_pending(limit=_BATCH_SIZE):
    """지금 보낼 수 있는 대기 행. 백오프 중이거나 dead-letter 된 행은 제외된다."""
    from modules import db_manager
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        # last_attempt_at 과 now_str 은 둘 다 KST 무오프셋 문자열이라 strftime('%s')
        # 이 양쪽을 똑같이 UTC 로 간주한다 — 차이값은 그대로 정확하다.
        # CAST 는 생략하면 안 된다. strftime 은 TEXT 를 돌려주는데 SQLite 는 숫자를
        # 언제나 텍스트보다 작다고 보므로, 캐스팅 없이 비교하면 백오프가 통째로
        # 무력화되어 죽은 서버에 매 순회 재요청을 때린다.
        cursor.execute(
            "SELECT id, exec_id, payload, attempts, last_attempt_at FROM journal_outbox "
            "WHERE synced_at IS NULL AND dead_at IS NULL "
            "  AND (last_attempt_at IS NULL "
            f"      OR CAST(strftime('%s', last_attempt_at) AS INTEGER) + {_BACKOFF_SQL} "
            "          <= CAST(strftime('%s', ?) AS INTEGER)) "
            "ORDER BY id LIMIT ?", (now_str, limit))
        return cursor.fetchall()
    except Exception as e:
        logger.warning(f"[Journal] 대기열 조회 실패: {e}")
        return []


def _mark_result(results_by_id, now_str):
    """전송 결과를 대기열에 반영한다. {outbox_id: (synced, remote_id, error, rejected)}

    `rejected` 는 **서버가 이 건을 명시적으로 거절했는지**다. 통신 실패와 구분해야
    한다 — 서버가 거절한 건만 세어 `_MAX_REJECTS` 에 닿으면 dead-letter 로 빼낸다.
    그대로 두면 서버가 영구 거절하는 행 하나가 매 배치 앞자리를 차지해 **뒤에 쌓인
    정상 건까지 영영 나가지 못한다**(head-of-line blocking). 반대로 통신 실패까지
    세면 웹서버가 오래 죽어 있을 때 멀쩡한 대기열이 통째로 폐기된다.
    """
    from modules import db_manager
    if not results_by_id:
        return
    buried = []
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            for outbox_id, mark in results_by_id.items():
                synced, remote_id, error, rejected = mark
                if synced:
                    cursor.execute(
                        "UPDATE journal_outbox SET synced_at = ?, remote_id = ?, "
                        "last_attempt_at = ?, last_error = NULL WHERE id = ?",
                        (now_str, remote_id, now_str, outbox_id))
                    continue

                cursor.execute(
                    "UPDATE journal_outbox SET attempts = attempts + 1, "
                    "reject_count = COALESCE(reject_count, 0) + ?, "
                    "last_attempt_at = ?, last_error = ? WHERE id = ?",
                    (1 if rejected else 0, now_str, (error or '')[:500], outbox_id))
                if not rejected:
                    continue
                cursor.execute(
                    "UPDATE journal_outbox SET dead_at = ? "
                    "WHERE id = ? AND dead_at IS NULL AND COALESCE(reject_count, 0) >= ?",
                    (now_str, outbox_id, _MAX_REJECTS))
                if cursor.rowcount:
                    buried.append((outbox_id, error))
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 대기열 갱신 실패: {e}")

    for outbox_id, error in buried:
        # 운용자가 알아채야 하는 상황이다 — 이 체결은 자동으로는 더 이상 나가지 않는다.
        logger.warning(
            f"[Journal] 전송 포기(dead-letter): outbox#{outbox_id} "
            f"— 서버가 {_MAX_REJECTS}회 거절, 마지막 사유: {error or '미상'}")


def _match_results(outbox_ids, payloads, results):
    """응답 항목을 대기열 행에 짝지어 [(outbox_id, item|None)] 을 만든다.

    계약상 `results` 는 요청과 같은 순서·같은 길이지만, 그 가정 하나가 어긋나면
    **엉뚱한 행이 전송 완료로 표시되어 체결이 영구 유실된다.** 응답에 이미 들어 있는
    `brokerExecutionId` 로 맞춰 보고, 그게 없을 때만 `index` → 위치 순으로 물러선다.
    """
    by_exec = {}
    for item in results:
        if isinstance(item, dict) and item.get('brokerExecutionId'):
            by_exec[item['brokerExecutionId']] = item

    matched = []
    for i, oid in enumerate(outbox_ids):
        exec_id = (payloads[i] or {}).get('brokerExecutionId')
        item = by_exec.get(exec_id) if exec_id else None
        if item is None:
            # 멱등키로 못 찾으면 서버가 준 index 를, 그것도 없으면 위치를 쓴다.
            candidate = results[i] if i < len(results) else None
            if isinstance(candidate, dict):
                idx = candidate.get('index')
                # index 가 자기 위치와 다르면 순서가 어긋난 응답이다 — 짝짓지 않는다.
                item = candidate if idx in (None, i) else None
        matched.append((oid, item))
    return matched


def _send_batch(payloads, outbox_ids, exec_ids):
    """한 묶음을 전송하고 결과를 대기열에 반영한다. (성공, 실패) 반환.

    서버가 묶음 **전체**를 거절(4xx)하면 절반으로 쪼개 다시 보낸다. 1건까지 좁히면
    진범이 특정되므로 그 행만 거절로 세고 나머지는 정상 전송된다. 쪼개지 않으면
    페이로드 한 건 때문에 묶음 전체가, 나아가 대기열 전체가 멈춘다.
    """
    res = _request('POST', '/api/v1/trades/batch',
                   json_body={'source': _source(), 'trades': payloads})
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')

    # ── 서버에 닿지 못했거나 서버가 넘어진 경우: 거절이 아니므로 세지 않고 그대로 재시도
    if res is None:
        _mark_result({oid: (False, None, '서버 응답 없음', False) for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    if res.status_code == 429:
        retry_after = res.headers.get('Retry-After', '?')
        logger.info(f"[Journal] 레이트 리밋 — {retry_after}초 후 재시도")
        _mark_result({oid: (False, None, f'429 (Retry-After={retry_after})', False)
                      for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    if res.status_code >= 500:
        message = f'{res.status_code}: {res.text[:200]}'
        logger.warning(f"[Journal] 서버 오류 — {message}")
        _mark_result({oid: (False, None, message, False) for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    # ── 묶음 전체 거절(400/413/422 등): 반씩 쪼개 진범을 좁힌다
    if res.status_code not in (200, 201):
        message = f'{res.status_code}: {res.text[:200]}'
        if len(payloads) > 1:
            logger.warning(f"[Journal] 배치 거절 — {message} / {len(payloads)}건을 분할 재시도")
            half = len(payloads) // 2
            ok_a, fail_a = _send_batch(payloads[:half], outbox_ids[:half], exec_ids[:half])
            ok_b, fail_b = _send_batch(payloads[half:], outbox_ids[half:], exec_ids[half:])
            return ok_a + ok_b, fail_a + fail_b
        logger.warning(f"[Journal] 단건 거절 — {message} [{exec_ids[0]}]")
        _mark_result({outbox_ids[0]: (False, None, message, True)}, now_str)
        return 0, 1

    try:
        results = (res.json() or {}).get('results') or []
    except Exception:
        results = []

    marks = {}
    ok = fail = 0
    for oid, item in _match_results(outbox_ids, payloads, results):
        if item is None:
            # 성공 여부를 알 수 없다. 재전송해도 서버가 멱등 처리하므로 다시 보내는
            # 편이 안전하다. 서버 잘못이라 단정할 수 없으니 거절로는 세지 않는다.
            marks[oid] = (False, None, '응답에 결과 항목 없음', False)
            fail += 1
        elif item.get('status') in ('created', 'duplicate'):
            marks[oid] = (True, item.get('id'), None, False)   # 성공 행에서 거절 플래그는 무의미
            ok += 1
            if item.get('warnings'):
                logger.info(f"[Journal] 서버 경고({item.get('brokerExecutionId')}): "
                            f"{'; '.join(item['warnings'])}")
        else:
            # 서버가 사유를 붙여 거절한 건 — 재시도해도 결과가 같다. 거절로 센다.
            marks[oid] = (False, None, f"{item.get('errorCode')}: {item.get('error')}", True)
            fail += 1

    _mark_result(marks, now_str)
    return ok, fail


def flush_once():
    """대기열을 한 번 비운다. (전송 성공 건수, 실패 건수) 반환."""
    if not is_enabled():
        return 0, 0

    rows = _fetch_pending()
    if not rows:
        return 0, 0

    payloads, outbox_ids, exec_ids = [], [], []
    for row in rows:
        try:
            payloads.append(json.loads(row['payload']))
            outbox_ids.append(row['id'])
            exec_ids.append(row['exec_id'])
        except Exception:
            # 손상된 페이로드는 재시도해도 소용없다 — 거절로 세어 결국 dead-letter 시킨다.
            _mark_result({row['id']: (False, None, '페이로드 파싱 불가', True)},
                         datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'))

    if not payloads:
        return 0, 0

    ok, fail = _send_batch(payloads, outbox_ids, exec_ids)
    if ok or fail:
        logger.info(f"[Journal] 전송 완료 {ok}건 / 실패 {fail}건 "
                    f"(대기 잔량 {pending_count()}건)")
    return ok, fail


_ping_fail_streak = 0


def ping(status='running', message=None, timeout=None, force=False):
    """봇 상태 Ping. 웹 대시보드의 가동 표시등을 켠다.

    status: running(가동) / stopped(정상 종료) / error(오류)
    force:  메뉴 토글(JOURNAL_SYNC_USE)을 무시하고 자격증명만으로 보낸다.
            연동을 '끄는' 순간의 종료 통지가 여기에 해당한다 — 토글이 이미
            False 로 바뀐 뒤에 stop() 이 불리므로, 검사하면 통지가 통째로
            누락되어 웹 표시등이 계속 '정상 가동중'으로 남는다.
    """
    global _ping_fail_streak

    if not (_has_credentials() if force else is_enabled()):
        return False
    body = {'status': status, 'isSimulated': bool(getattr(config.session, 'is_simulation', False))}
    if message:
        body['message'] = message[:500]

    res = _request('POST', '/api/v1/bot/status', json_body=body,
                   timeout=timeout or _PING_TIMEOUT, quiet=True)
    ok = bool(res is not None and res.status_code == 200)

    if ok:
        # 끊겼다가 살아난 것은 운용자가 알아야 하므로 회복만 INFO 로 남긴다.
        if _ping_fail_streak:
            logger.info(f"[Journal] 봇 상태 Ping 회복 (연속 실패 {_ping_fail_streak}회 후)")
        _ping_fail_streak = 0
        logger.debug(f"[Journal] 봇 상태 Ping 전송 (status={status})")
    else:
        _ping_fail_streak += 1
        reason = res.status_code if res is not None else '응답 없음'
        # 10초 간격이라 매번 남기면 로그가 이것만으로 찬다 — 첫 실패와 이후 5분마다만.
        if _ping_fail_streak == 1 or _ping_fail_streak % _PING_WARN_EVERY == 0:
            logger.warning(f"[Journal] 봇 상태 Ping 실패 ({reason}) "
                           f"— 연속 {_ping_fail_streak}회")
    return ok


def _notify_shutdown(status='stopped', message=None):
    """종료 사실을 웹서버에 한 번 알린다.

    종료 경로에서 호출되므로 무슨 일이 있어도 예외를 올리지 않고, 응답이 늦어도
    프로그램 종료를 붙잡지 않도록 타임아웃을 짧게 둔다.
    """
    try:
        ok = ping(status, message=message, timeout=_SHUTDOWN_PING_TIMEOUT, force=True)
        if ok:
            logger.info(f"[Journal] 종료 상태 통지 완료 (status={status})")
        else:
            # 못 보내도 서버는 Ping 누락으로 곧 '통신단절'을 표시하므로 치명적이지 않다.
            logger.warning("[Journal] 종료 상태 통지 실패 — 웹 표시등은 Ping 누락으로 전환됩니다.")
        return ok
    except Exception as e:
        logger.debug(f"[Journal] 종료 상태 통지 중 오류(무시): {e}")
        return False


def notify_shutdown(status='stopped', message=None):
    """프로그램 종료 시 호출 — 웹 대시보드 표시등을 즉시 '정지됨'으로 바꾼다.

    이 통지가 없으면 Ping 이 3회 누락될 때까지 '정상 가동중'으로 남는다.
    """
    if not _has_credentials():
        return False
    return _notify_shutdown(status, message)


def pending_count():
    """미전송 건수 (메뉴/상태 표시용). dead-letter 된 행은 제외한다."""
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM journal_outbox "
                       "WHERE synced_at IS NULL AND dead_at IS NULL")
        return cursor.fetchone()[0]
    except Exception:
        return 0


def dead_count():
    """전송을 포기한 건수. 0이 아니면 운용자가 원인을 봐야 한다."""
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM journal_outbox WHERE dead_at IS NOT NULL")
        return cursor.fetchone()[0]
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════
# 대기열 정리 (retention)
# ══════════════════════════════════════════════════════════════════════

def purge_synced(days=_RETENTION_DAYS):
    """전송이 끝난 지 `days` 지난 행을 지운다. 삭제 건수 반환.

    라즈베리파이 SD카드에 payload JSON 이 체결마다 무한 누적되는 걸 막는다.
    미전송·dead-letter 행은 건드리지 않는다 — 전자는 아직 보내야 하고, 후자는
    운용자가 원인을 봐야 한다.

    VACUUM 은 하지 않는다. 라파에서 수십 MB DB 를 VACUUM 하면 그동안 쓰기가
    통째로 막히고, 어차피 SQLite 가 빈 페이지를 재사용하므로 실익이 없다.
    """
    from modules import db_manager
    cutoff = (datetime.now(KST) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM journal_outbox "
                "WHERE synced_at IS NOT NULL AND synced_at < ?", (cutoff,))
            removed = cursor.rowcount or 0
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 대기열 정리 실패: {e}")
            return 0
    if removed:
        logger.info(f"[Journal] 전송 완료 {removed}건 정리 ({days}일 경과분)")
    return removed


# ══════════════════════════════════════════════════════════════════════
# 백필 — 큐에 들어가지 못한 체결 회수
# ══════════════════════════════════════════════════════════════════════

def _fetch_last_sync():
    """서버가 마지막으로 저장한 체결 지점을 조회한다. (dict 또는 None)

    `source` 를 반드시 실어야 한다. 빼면 웹 UI 에서 손으로 입력한 기록까지 섞여,
    미래 날짜 기록 하나만 있어도 백필 구간이 통째로 건너뛰어진다.
    계좌로는 좁히지 않는다 — 계좌가 여럿일 때 한쪽만 앞서 있으면 뒤처진 계좌의
    누락 구간이 스캔에서 빠진다.
    """
    res = _request('GET', '/api/v1/trades/last-sync',
                   params={'source': _source(),
                           'isSimulated': 'true' if _sync_simulation() else 'false'})
    if res is None:
        return None
    if res.status_code == 403:
        # trades:read 없이 발급된 키. 전송은 되지만 누락 회수는 못 한다.
        logger.warning("[Journal] 백필 불가 — API 키에 trades:read 권한이 없습니다. "
                       "웹 설정에서 키를 다시 발급하세요.")
        return None
    if res.status_code != 200:
        logger.warning(f"[Journal] 마지막 동기화 지점 조회 실패 "
                       f"({res.status_code}): {res.text[:200]}")
        return None
    try:
        return res.json() or {}
    except Exception:
        return None


def _backfill_since(last_sync):
    """스캔 시작 시각(로컬 KST 문자열)을 정한다.

    서버가 주는 `lastExecutedAt` 은 UTC 인데 로컬 `trades.time` 은 KST 다.
    변환하지 않고 비교하면 9시간이 통째로 어긋난다.
    """
    default = datetime.now(KST) - timedelta(days=_BACKFILL_LOOKBACK_DAYS)
    raw = (last_sync or {}).get('lastExecutedAt')
    if not raw:
        return default.strftime('%Y-%m-%d %H:%M:%S')
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return default.strftime('%Y-%m-%d %H:%M:%S')
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # 경계에서 한 건도 빠지지 않도록 조금 앞에서부터 다시 훑는다. 중복은
    # exec_id UNIQUE 와 서버 멱등 처리가 걸러 주므로 겹치는 쪽이 안전하다.
    since = parsed.astimezone(KST) - timedelta(minutes=_BACKFILL_OVERLAP_MIN)
    return max(since, default).strftime('%Y-%m-%d %H:%M:%S')


def _local_fills_since(since_str, limit=_BACKFILL_MAX_ROWS):
    """`since_str` 이후의 로컬 체결 기록. 오래된 것부터."""
    from modules import db_manager
    statuses = tuple(_SYNCABLE_STATUS)
    placeholders = ','.join('?' * len(statuses))
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT time, type, code, name, qty, price, odno, org_odno, account, "
            f"       is_sim, profit_amt, profit_rate, reason, strategy_score, "
            f"       order_status, stop_loss_rate "
            f"FROM trades WHERE order_status IN ({placeholders}) AND time >= ? "
            f"ORDER BY time LIMIT ?", statuses + (since_str, limit))
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"[Journal] 로컬 체결 조회 실패: {e}")
        return []


def backfill_once():
    """큐에 없는 로컬 체결을 찾아 대기열에 넣는다. (회수 건수, 스캔 건수) 반환.

    **이 함수가 막는 것**: 연동 토글이 꺼져 있었거나 환경변수가 빠진 채 돌던 구간의
    체결이다. 그 구간엔 `enqueue()` 가 통째로 건너뛰므로 큐에 아무것도 남지 않아,
    나중에 연동을 켜도 재시도 로직으로는 영원히 복구되지 않는다. 서버가 죽어 있던
    구간은 큐에 남아 있으므로 여기서 할 일이 없다.

    적재만 하고 전송은 하지 않는다 — 기존 워커가 다음 주기에 알아서 보낸다.
    """
    if not is_enabled():
        return 0, 0

    since = _backfill_since(_fetch_last_sync())
    rows = _local_fills_since(since)
    if not rows:
        return 0, 0

    from modules import db_manager
    queued = 0
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            for trade in rows:
                # quiet=True: 수백 건을 훑으므로 건별 로그는 남기지 않는다.
                #  enqueue 가 True 를 돌려준 직후의 rowcount 는 방금 실행한
                #  INSERT OR IGNORE 의 결과라, 1이면 큐에 없던 신규 건이다.
                if enqueue(cursor, trade, quiet=True) and cursor.rowcount:
                    queued += 1
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 백필 적재 실패: {e}")
            return 0, len(rows)

    if queued:
        # 정상 경로가 놓친 체결이 있었다는 뜻이다 — 조용히 넘기면 안 된다.
        logger.warning(f"[Journal] 백필 — 대기열에 없던 체결 {queued}건 회수 "
                       f"(스캔 {len(rows)}건, {since} 이후)")
    else:
        logger.debug(f"[Journal] 백필 — 누락 없음 (스캔 {len(rows)}건, {since} 이후)")
    return queued, len(rows)


class JournalSyncWorker:
    """대기열을 주기적으로 비우고 봇 상태를 보고하는 단일 백그라운드 스레드."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.is_running = False
        self.thread = None
        self._wake = threading.Event()
        self._last_ping = 0.0
        self._last_flush = 0.0
        self._force_flush = False
        # 첫 백필은 기동 직후가 아니라 조금 뒤에 돈다 — 로그인·유니버스(stock.json)
        # 적재가 끝나야 해외 종목의 거래소를 제대로 붙일 수 있다.
        self._next_backfill = 0.0
        self._next_purge = 0.0

    def start(self):
        if self.is_running:
            return
        if not is_enabled():
            # 왜 안 도는지가 로그만 봐도 판별되어야 한다 — 설정(메뉴)과 자격증명(환경변수)을 구분해 남긴다.
            if not getattr(config.settings, 'JOURNAL_SYNC_USE', False):
                logger.info("[Journal] 매매일지 연동 비활성 (메뉴 0 → 5-3 스위치 OFF)")
            else:
                missing = [n for n in ('JOURNAL_API_URL', 'JOURNAL_API_KEY') if not _cfg(n)]
                logger.info(f"[Journal] 매매일지 연동 비활성 (환경변수 미설정: {', '.join(missing)})")
            return
        self.is_running = True
        self._next_backfill = time.time() + _BACKFILL_STARTUP_DELAY_SEC
        self._next_purge = time.time() + _BACKFILL_STARTUP_DELAY_SEC
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="JournalSync")
        self.thread.start()
        buried = dead_count()
        logger.info(f"[Journal] 매매일지 연동 시작 — {_base_url()} "
                    f"(source={_source()}, 대기 잔량 {pending_count()}건"
                    f"{f', 전송포기 {buried}건' if buried else ''})")

    def stop(self, notify='stopped'):
        """워커를 멈춘다.

        notify 에 상태를 주면 종료 사실을 웹서버에 즉시 알린다. 이 신호가 없으면
        웹 대시보드는 Ping 이 3회 누락될 때까지 '정상 가동중'으로 남아 있게 된다.
        """
        if not self.is_running:
            return
        self.is_running = False
        self._wake.set()
        if self.thread:
            self.thread.join(timeout=3)

        if notify:
            # 워커 스레드가 멈춘 뒤에 보낸다 — 루프의 running Ping 이 이 값을 덮어쓰지 않도록.
            _notify_shutdown(notify)
        logger.info(f"[Journal] 매매일지 연동 중지 (미전송 {pending_count()}건은 큐에 보존)")

    def trigger(self):
        """즉시 1회 순회를 깨운다 (체결 직후 지연 없이 반영하고 싶을 때)."""
        self._force_flush = True
        self._wake.set()

    def _run_loop(self):
        # 순회는 Ping 주기(10초)에 맞춰 돌되, 대기열 전송은 종전대로 30초마다만 한다.
        # (하트비트를 빠르게 하려고 전송까지 10초마다 돌리면 서버 부하만 3배가 된다)
        while self.is_running:
            try:
                now = time.time()
                if self._force_flush or (now - self._last_flush) >= _FLUSH_INTERVAL_SEC:
                    self._force_flush = False
                    flush_once()
                    self._last_flush = time.time()

                if time.time() - self._last_ping >= _PING_INTERVAL_SEC:
                    # 실패해도 _last_ping 을 갱신한다. 갱신하지 않으면 서버가 죽어 있는 동안
                    # 매 순회마다 재시도해 타임아웃으로 루프가 계속 물린다.
                    self._last_ping = time.time()
                    ping('running')

                # 백필·정리는 저빈도라 flush 뒤에 붙여 둔다. 실패해도 다음 주기에
                # 다시 시도하면 되므로 여기서 예외를 따로 잡지 않는다(루프가 삼킨다).
                if time.time() >= self._next_backfill:
                    self._next_backfill = time.time() + _BACKFILL_INTERVAL_SEC
                    queued, _ = backfill_once()
                    if queued:
                        self._force_flush = True   # 회수분은 다음 순회까지 기다리지 않는다

                if time.time() >= self._next_purge:
                    self._next_purge = time.time() + _PURGE_INTERVAL_SEC
                    purge_synced()
            except Exception as e:
                # 워커가 죽으면 큐가 영영 쌓이기만 한다 — 어떤 예외도 루프를 끊지 못하게 한다.
                logger.error(f"[Journal] 동기화 루프 오류(계속 진행): {e}")

            self._wake.wait(timeout=_TICK_INTERVAL_SEC)
            self._wake.clear()


def start():
    """앱 기동 시 호출 — 연동이 꺼져 있으면 아무 일도 하지 않는다."""
    JournalSyncWorker().start()


def stop(notify='stopped'):
    """메뉴에서 연동을 끌 때 / 프로그램 종료 시 호출 — 워커 스레드를 정리한다.

    대기열에 쌓인 미전송 건은 지우지 않는다. 다시 켜면 그대로 이어서 전송된다.
    종료 사실은 웹서버에 알려 대시보드 표시등을 즉시 '정지됨'으로 바꾼다.
    """
    JournalSyncWorker().stop(notify=notify)


def trigger():
    """체결 직후 즉시 전송을 깨운다."""
    if is_enabled():
        JournalSyncWorker().trigger()
