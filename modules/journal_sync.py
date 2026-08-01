"""매매일지 웹서버 연동 — Universal Trading History API v2 클라이언트.

Outbox 패턴
-----------
체결 처리 경로에서는 **절대 네트워크를 타지 않는다.**
`db_manager.insert_trade()` 가 거래 기록과 같은 트랜잭션으로 `journal_outbox` 에
적재하고, 이 모듈의 백그라운드 워커가 배치로 전송한다.

  - 체결 확인 루프가 네트워크 지연(수 초)에 묶이지 않는다
  - 라즈베리파이가 단절·재부팅돼도 큐가 DB에 남아 자동으로 복구된다
  - 서버가 brokerExecutionId 로 멱등 처리하므로 재전송이 언제나 안전하다

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
_MAX_ATTEMPTS = 12            # 이후로는 백오프 상한(1시간)으로만 재시도

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

def enqueue(cursor, trade):
    """전송 대기열에 적재한다. 호출자의 트랜잭션·커서를 그대로 쓴다.

    전송 대상이 아니면 조용히 무시한다. **여기서 예외를 올리면 거래 기록 저장이
    함께 롤백되므로**, 판단이 애매하면 적재하지 않는 쪽을 택한다.
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


def _fetch_pending(limit=_BATCH_SIZE):
    from modules import db_manager
    now = datetime.now(KST)
    rows = []
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, exec_id, payload, attempts, last_attempt_at FROM journal_outbox "
            "WHERE synced_at IS NULL ORDER BY id LIMIT ?", (limit * 3,))
        for row in cursor.fetchall():
            attempts = row['attempts'] or 0
            last = row['last_attempt_at']
            if last:
                try:
                    elapsed = (now - datetime.strptime(last, '%Y-%m-%d %H:%M:%S').replace(
                        tzinfo=KST)).total_seconds()
                    if elapsed < _backoff_seconds(attempts):
                        continue  # 아직 백오프 대기 중
                except ValueError:
                    pass
            rows.append(row)
            if len(rows) >= limit:
                break
    except Exception as e:
        logger.warning(f"[Journal] 대기열 조회 실패: {e}")
    return rows


def _mark_result(results_by_id, now_str):
    """전송 결과를 대기열에 반영한다. {outbox_id: (synced, remote_id, error)}"""
    from modules import db_manager
    if not results_by_id:
        return
    with db_manager.db.lock:
        try:
            conn = db_manager.db._get_conn()
            cursor = conn.cursor()
            for outbox_id, (synced, remote_id, error) in results_by_id.items():
                if synced:
                    cursor.execute(
                        "UPDATE journal_outbox SET synced_at = ?, remote_id = ?, "
                        "last_attempt_at = ?, last_error = NULL WHERE id = ?",
                        (now_str, remote_id, now_str, outbox_id))
                else:
                    cursor.execute(
                        "UPDATE journal_outbox SET attempts = attempts + 1, "
                        "last_attempt_at = ?, last_error = ? WHERE id = ?",
                        (now_str, (error or '')[:500], outbox_id))
            conn.commit()
        except Exception as e:
            logger.warning(f"[Journal] 대기열 갱신 실패: {e}")


def flush_once():
    """대기열을 한 번 비운다. (전송 성공 건수, 실패 건수) 반환."""
    if not is_enabled():
        return 0, 0

    rows = _fetch_pending()
    if not rows:
        return 0, 0

    payloads = []
    outbox_ids = []
    for row in rows:
        try:
            payloads.append(json.loads(row['payload']))
            outbox_ids.append(row['id'])
        except Exception:
            # 손상된 페이로드는 재시도해도 소용없다 — 실패로 표시해 백오프에 맡긴다.
            _mark_result({row['id']: (False, None, '페이로드 파싱 불가')},
                         datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'))

    if not payloads:
        return 0, 0

    res = _request('POST', '/api/v1/trades/batch',
                   json_body={'source': _source(), 'trades': payloads})
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')

    if res is None:
        _mark_result({oid: (False, None, '서버 응답 없음') for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    if res.status_code == 429:
        retry_after = res.headers.get('Retry-After', '?')
        logger.info(f"[Journal] 레이트 리밋 — {retry_after}초 후 재시도")
        _mark_result({oid: (False, None, f'429 (Retry-After={retry_after})')
                      for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    if res.status_code not in (200, 201):
        message = f'{res.status_code}: {res.text[:200]}'
        logger.warning(f"[Journal] 배치 전송 실패 — {message}")
        _mark_result({oid: (False, None, message) for oid in outbox_ids}, now_str)
        return 0, len(outbox_ids)

    try:
        body = res.json()
        results = body.get('results') or []
    except Exception:
        results = []

    marks = {}
    ok = fail = 0
    for i, oid in enumerate(outbox_ids):
        item = results[i] if i < len(results) else None
        if item is None:
            # 응답에 결과가 없으면 성공 여부를 알 수 없다 — 재전송해도 서버가
            # 멱등 처리하므로 실패로 두고 다시 보내는 편이 안전하다.
            marks[oid] = (False, None, '응답에 결과 항목 없음')
            fail += 1
        elif item.get('status') in ('created', 'duplicate'):
            marks[oid] = (True, item.get('id'), None)
            ok += 1
            if item.get('warnings'):
                logger.info(f"[Journal] 서버 경고({item.get('brokerExecutionId')}): "
                            f"{'; '.join(item['warnings'])}")
        else:
            marks[oid] = (False, None, f"{item.get('errorCode')}: {item.get('error')}")
            fail += 1

    _mark_result(marks, now_str)
    if ok or fail:
        remaining = pending_count()
        logger.info(f"[Journal] 전송 완료 {ok}건 / 실패 {fail}건 (대기 잔량 {remaining}건)")
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
    """미전송 건수 (메뉴/상태 표시용)."""
    from modules import db_manager
    try:
        conn = db_manager.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM journal_outbox WHERE synced_at IS NULL")
        return cursor.fetchone()[0]
    except Exception:
        return 0


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
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="JournalSync")
        self.thread.start()
        logger.info(f"[Journal] 매매일지 연동 시작 — {_base_url()} "
                    f"(source={_source()}, 대기 잔량 {pending_count()}건)")

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
