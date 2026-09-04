# toss_api.py
# -----------------------------------------------------------------------------
# 토스증권(Toss Securities) Open API 클라이언트.
#
# - KIS 전용 로직(api.py)과 분리하여, 토스 API가 제공하는 기능만 담당한다.
# - 인증: OAuth 2.0 Client Credentials Grant (POST /oauth2/token, form-urlencoded).
# - 거의 모든 거래/자산 API는 X-Tossinvest-Account 헤더(accountSeq)가 필요하다.
#   accountSeq는 GET /api/v1/accounts 응답에서 TOSS_ACC_NUM(계좌번호)과 매칭하여 구한다.
# - 응답 envelope: 성공은 {"result": ...}, 실패는 {"error": {code,message,...}}.
# - Rate Limit은 그룹별 토큰버킷이나, 여기서는 보수적 단일 최소간격 + 429 백오프로 처리한다.
#
# 본 모듈의 메서드는 "토스 원형에 가까운" 깔끔한 파이썬 값을 반환한다.
# (KIS 화면 코드가 기대하는 형태로의 변환은 api.py 어댑터 계층에서 수행한다.)
# -----------------------------------------------------------------------------
import json
import time
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta

import requests

import config

logger = logging.getLogger("hts")

_BASE = config.TOSS_URL

# 토큰 메모리 캐시 (디스크 IO 절감용; 영속 캐시는 config.session 토큰 캐시 재사용)
_token_lock = threading.Lock()

# -------------------------------------------------------------------------
# 그룹별 Rate Limit 제어
#  - 토스는 Rate Limit을 그룹(AUTH/MARKET_DATA/MARKET_DATA_CHART/...)별로 분리 관리한다.
#  - 그룹마다 슬라이딩 윈도우(1초) 토큰버킷 + 최소 간격으로 제어하고,
#    429 발생 시 해당 그룹에만 쿨다운(Retry-After)을 적용해 다른 그룹을 막지 않는다.
#  - 전 종목 스캔(캔들 다량 호출) 시 MARKET_DATA_CHART 그룹을 별도로 보수적 제한한다.
# -------------------------------------------------------------------------
# 그룹별 한도는 2026-07-19 tools/toss_tps_probe.py 실측값(서버 X-RateLimit-Limit 헤더 확인).
# 종전엔 전 그룹 10으로 가정했으나 실제로는 그룹별로 다르다 — 한도 초과 설정은 429+
# 그룹 쿨다운(1초)을 유발해 오히려 실효 처리량을 떨어뜨린다.
#   MARKET_DATA(/prices)=10, MARKET_DATA_CHART(/candles)=5, STOCK(/stocks)=5,
#   MARKET_INFO(/exchange-rate)=3, RANKING(/rankings)=5
# 주문·계좌 그룹은 안전상 미실측 — 종전 값 유지(429 시 쿨다운 로직이 방어).
_TOSS_MAX_RPS = 10
_GROUP_RPS = {
    "AUTH": 2,
    "MARKET_DATA": 10,        # 실측 10
    "MARKET_DATA_CHART": 5,   # 실측 5 (종전 10 — 초과 설정이었음)
    "STOCK": 5,               # 실측 5 (종전 10 — 초과 설정이었음)
    "MARKET_INFO": 3,         # 실측 3 (종전 10 — 초과 설정이었음)
    "ACCOUNT": _TOSS_MAX_RPS,
    "ASSET": _TOSS_MAX_RPS,
    "ORDER": _TOSS_MAX_RPS,
    "ORDER_HISTORY": _TOSS_MAX_RPS,
    "ORDER_INFO": _TOSS_MAX_RPS,
    "RANKING": 5,             # 실측 5 (종전 10 — 초과 설정이었음)
    # 시장지표(1.2.4 신설) — 한도 미실측. 성격이 비슷한 MARKET_DATA/CHART보다 보수적으로 잡고,
    #  초과 시엔 429 그룹 쿨다운이 방어한다. (지수는 4종 이하 소량 호출이라 낮아도 무해)
    "MARKET_INDICATOR": 5,
    "MARKET_INDICATOR_CHART": 3,
}

_rate_lock = threading.Lock()
_group_hist = defaultdict(deque)       # group -> 최근 1초 요청 타임스탬프
_group_cooldown = defaultdict(float)   # group -> 이 시각까지 추가 대기 (429 백오프)


def _group_rps(group):
    return _GROUP_RPS.get(group, max(config.TOSS_TX_PER_SECOND, 1))


def _note_rate_limited(group, retry_after):
    """429 발생 시 해당 그룹에 쿨다운을 설정한다."""
    with _rate_lock:
        _group_cooldown[group] = time.time() + max(float(retry_after or 0), 1.0)


class TossApiError(Exception):
    """토스 API 오류(error envelope 또는 HTTP 오류)."""

    def __init__(self, code, message, status=None, request_id=None, data=None):
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id
        self.data = data or {}
        super().__init__(f"[{status}] {code}: {message}")


class TossOrderOutcomeUnknown(TossApiError):
    """주문 요청이 토스에 닿았는지 알 수 없는 상태(응답 유실).

    '실패'와 구분해야 한다. 실패는 다시 보내도 되지만 이 상태는 안 된다 — 이미 접수됐을
    수 있고, 그러면 같은 포지션이 두 번 생겨 손절폭·변동성 한도·히트 캡이 한꺼번에
    무의미해진다. 호출부는 재전송 대신 **주문 내역을 조회해** 확인한다(api/orders.py).
    KIS 경로의 OrderOutcomeUnknown 과 같은 역할이며, api/toss.py 가 그것으로 옮겨 던진다.
    """


def _outcome_unknown(exc):
    """응답을 못 받아 결과를 알 수 없는 예외인가(api/http.py의 같은 판정과 문구를 맞춘다).

    ConnectTimeout 은 연결 자체가 안 된 것이라 요청이 나갔을 수 없다 — 다시 보내도 안전하다.
    ReadTimeout 은 보낸 뒤 응답을 못 받은 것이므로 결과를 모른다.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return False
    return isinstance(exc, (requests.exceptions.ReadTimeout,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.ChunkedEncodingError))


# =========================================================================
# 인증 (OAuth2 Client Credentials)
# =========================================================================
def get_access_token(force_refresh=False, stale_token=None):
    """토스 access token을 발급/캐시한다. 실패 시 None.

    stale_token: 401을 맞은 '죽은 토큰'. force_refresh와 함께 주면, 락을 잡은 시점에
      캐시 토큰이 이미 그것과 달라진 경우(= 다른 스레드가 먼저 재발급 완료) 재발급하지
      않고 그 토큰을 그대로 쓴다. 여러 스레드가 동시에 401을 맞았을 때 저마다 재발급해
      서로의 토큰을 무효화하는 폭주를 막는다.
    """
    with _token_lock:
        if not force_refresh:
            cached = config.session.get_valid_token("TOSS")
            if cached:
                return cached
        elif stale_token:
            cached = config.session.get_valid_token("TOSS")
            if cached and cached != stale_token:
                # 동시에 401을 맞은 스레드들은 모두 아래 warning을 남기므로, 로그만 봐서는
                # '한 번 재발급 + 나머지 재사용'인지 '전부 재발급(폭주)'인지 구분되지 않는다.
                # 재사용을 명시해 다음 발생 시 원인을 바로 가릴 수 있게 한다.
                logger.info("[Toss] 401 복구: 다른 스레드가 발급한 토큰 재사용 (재발급 생략)")
                return cached

        key = config.session.toss_app_key
        secret = config.session.toss_app_secret
        if not key or not secret:
            logger.error("[Toss] TOSS_APP_KEY/SECRET이 설정되지 않았습니다.")
            return None

        url = f"{_BASE}/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": key,
            "client_secret": secret,
        }
        try:
            res = requests.post(
                url,
                data=data,  # application/x-www-form-urlencoded
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if res.status_code == 200:
                body = res.json()
                token = body.get("access_token")
                expires_in = int(body.get("expires_in", 0) or 0)
                if token:
                    # 만료 30초 전을 만료시각으로 저장(여유 버퍼)
                    expired_dt = datetime.now() + timedelta(seconds=max(expires_in - 30, 0))
                    expired = expired_dt.strftime("%Y-%m-%d %H:%M:%S")
                    config.session.set_token("TOSS", token, expired)
                    config.set_last_token_error(None)
                    logger.info("[Toss] 액세스 토큰 발급 완료")
                    return token
                logger.error(f"[Toss] 토큰 응답에 access_token 없음: {res.text[:200]}")
                config.set_last_token_error('AUTH')
                return None
            else:
                # OAuth2 표준 에러 포맷 {"error","error_description"}
                err_code = err_desc = None
                try:
                    err = res.json()
                    err_code, err_desc = err.get('error'), err.get('error_description')
                    logger.error(
                        f"[Toss] 토큰 발급 실패 ({res.status_code}): {err_code} - {err_desc}"
                    )
                except Exception:
                    logger.error(f"[Toss] 토큰 발급 실패 ({res.status_code}): {res.text[:200]}")
                # [추가] 허용 IP(화이트리스트) 미등록 감지: 403 access_denied / "IP ... not allowed"
                blob = f"{err_code or ''} {err_desc or ''} {res.text[:200]}".lower()
                if res.status_code == 403 or 'ip address not allowed' in blob or 'access_denied' in blob:
                    config.set_last_token_error('IP_BLOCKED')
                else:
                    config.set_last_token_error('AUTH')
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"[Toss] 토큰 발급 네트워크 오류: {e}")
            config.set_last_token_error('NETWORK')
            return None


# =========================================================================
# 공통 요청 처리
# =========================================================================
def _throttle(group):
    """그룹별 슬라이딩 윈도우(1초) + 최소 간격 + 429 쿨다운을 적용한다."""
    rps = _group_rps(group)
    window = 1.0
    min_interval = window / rps
    while True:
        with _rate_lock:
            now = time.time()
            hist = _group_hist[group]
            # 윈도우(1초) 밖의 기록 제거
            while hist and hist[0] <= now - window:
                hist.popleft()

            cooldown = _group_cooldown.get(group, 0.0)
            wait_cd = cooldown - now                       # 429 쿨다운 잔여
            last = hist[-1] if hist else 0.0
            wait_int = min_interval - (now - last)         # 최소 간격
            wait_win = (hist[0] + window - now) if len(hist) >= rps else 0.0  # 윈도우 한도

            wait = max(wait_cd, wait_int, wait_win)
            if wait <= 0:
                hist.append(now)
                return
        time.sleep(min(wait, 1.0) if wait > 0 else 0.02)


def _request(method, path, group, params=None, json_body=None, account=True, retries=2,
             idempotent=True):
    """토스 API 호출 공통 처리. 성공 시 result 페이로드를 반환한다.

    account=True 이면 X-Tossinvest-Account 헤더(accountSeq)를 부착한다.
    오류 시 TossApiError를 발생시킨다.

    idempotent=False 는 상태를 바꾸는 요청(주문·정정·취소)이다. 조회는 몇 번 다시 보내도
    무해하지만 이쪽은 한 번 더 나가면 포지션이 하나 더 생긴다. 그래서 **요청이 실제로
    전송된 뒤**의 실패(ReadTimeout·전송 중 끊김·5xx)는 재시도하지 않고
    TossOrderOutcomeUnknown 으로 올린다 — 호출부가 조회로 대사한다.
    (연결조차 못 한 ConnectTimeout, 서버가 실행 없이 거절한 429·401 은 그대로 재시도한다.)
    """
    token = get_access_token()
    if not token:
        raise TossApiError("no-token", "토스 액세스 토큰 발급 실패", status=401)

    headers = {"Authorization": f"Bearer {token}"}
    if account:
        seq = config.session.toss_account_seq
        if seq is None:
            seq = resolve_account_seq()
        if seq is None:
            raise TossApiError("account-header-required", "토스 accountSeq를 확인할 수 없습니다.", status=400)
        headers["X-Tossinvest-Account"] = str(seq)

    url = f"{_BASE}{path}"
    last_exc = None
    token_retried = False   # 401 강제 재발급은 요청당 1회로 제한(무한 재발급 방지)
    for attempt in range(retries + 2):  # +1: 401 재발급 재시도가 일반 재시도 횟수를 잡아먹지 않게
        _throttle(group)
        try:
            if method == "GET":
                res = requests.get(url, headers=headers, params=params, timeout=config.DEFAULT_TIMEOUT)
            else:
                body = json.dumps(json_body) if json_body is not None else None
                h = dict(headers)
                if body is not None:
                    h["Content-Type"] = "application/json"
                res = requests.request(method, url, headers=h, params=params, data=body, timeout=config.DEFAULT_TIMEOUT)
        except requests.exceptions.RequestException as e:
            if not idempotent and _outcome_unknown(e):
                raise TossOrderOutcomeUnknown("order-outcome-unknown", str(e),
                                              status=None) from e
            last_exc = TossApiError("network-error", str(e), status=None)
            if attempt < retries:
                time.sleep(config.RETRY_DELAY_SERVER * (attempt + 1))
                continue
            raise last_exc

        # Rate limit
        if res.status_code == 429:
            retry_after = res.headers.get("Retry-After") or res.headers.get("X-RateLimit-Reset")
            wait = float(retry_after) if (retry_after and str(retry_after).isdigit()) else 1.0
            # 해당 그룹에 쿨다운을 적용해 후속 호출이 자동으로 양보하도록 한다.
            _note_rate_limited(group, wait)
            if attempt < retries:
                time.sleep(wait)
                continue
            raise TossApiError("rate-limit-exceeded", "요청 한도를 초과했습니다.", status=429)

        # [추가] 401 invalid-token: 서버가 캐시 만료시각 전에 토큰을 폐기한 경우다
        #  (다른 기기·세션에서 같은 앱키로 재발급하면 앞선 토큰이 무효화된다).
        #  캐시는 '아직 유효'로 보고 재발급을 안 하므로, 복구하지 않으면 캐시 만료(최대 24h)까지
        #  토스 모드 전체가 죽는다. 요청당 1회만 강제 재발급 후 같은 요청을 재시도한다.
        if res.status_code == 401 and not token_retried:
            token_retried = True
            new_token = get_access_token(force_refresh=True, stale_token=token)
            if new_token and new_token != token:
                token = new_token
                headers["Authorization"] = f"Bearer {token}"
                logger.warning(f"[Toss] 401 invalid-token → 토큰 재발급 후 재시도 ({path})")
                continue

        # 성공
        if 200 <= res.status_code < 300:
            try:
                body = res.json()
            except ValueError:
                return None
            return body.get("result")

        # 오류 envelope
        code = "unknown"
        message = res.text[:200]
        request_id = res.headers.get("X-Request-Id")
        data = {}
        try:
            err = res.json().get("error", {})
            code = err.get("code", code)
            message = err.get("message", message)
            request_id = err.get("requestId", request_id)
            data = err.get("data") or {}
            
            # [추가] 1.2.14 미국장 달러/소수점 주문 마감 안내 메시지 개선
            if code in ["amount-order-outside-regular-hours", "fractional-quantity-outside-regular-hours"]:
                message = "미국 주식 금액/소수점 주문은 정규장 종료 1시간 전까지만 접수 가능합니다."
        except Exception:
            pass

        # 5xx는 재시도. 다만 주문 계열은 서버가 이미 접수한 뒤 죽었을 수 있어 결과를 모른다.
        if res.status_code >= 500:
            if not idempotent:
                raise TossOrderOutcomeUnknown("order-outcome-unknown",
                                              f"HTTP {res.status_code}: {message}",
                                              status=res.status_code, request_id=request_id)
            if attempt < retries:
                time.sleep(config.RETRY_DELAY_SERVER * (attempt + 1))
                continue

        raise TossApiError(code, message, status=res.status_code, request_id=request_id, data=data)

    if last_exc:
        raise last_exc
    raise TossApiError("unknown", "알 수 없는 오류", status=None)


# =========================================================================
# 계좌
# =========================================================================
def get_accounts():
    """사용자 계좌 목록. [{accountNo, accountSeq, accountType}, ...]"""
    return _request("GET", "/api/v1/accounts", group="ACCOUNT", account=False) or []


def resolve_account_seq(force=False):
    """TOSS_ACC_NUM(계좌번호)과 매칭하여 accountSeq를 구하고 세션에 캐시한다.

    - TOSS_ACC_NUM 미설정 시 첫 번째 계좌를 사용한다.
    - 성공 시 accountSeq(int) 반환, 실패 시 None.
    """
    if not force and config.session.toss_account_seq is not None:
        return config.session.toss_account_seq

    try:
        accounts = get_accounts()
    except TossApiError as e:
        logger.error(f"[Toss] 계좌 목록 조회 실패: {e}")
        return None

    if not accounts:
        logger.error("[Toss] 사용 가능한 계좌가 없습니다.")
        return None

    target = (config.session.toss_acc_num or "").replace("-", "").strip()
    chosen = None
    if target:
        for acc in accounts:
            acc_no = str(acc.get("accountNo", "")).replace("-", "")
            if acc_no == target or acc_no.startswith(target) or target.startswith(acc_no):
                chosen = acc
                break
        if chosen is None:
            logger.warning(f"[Toss] TOSS_ACC_NUM({config.session.toss_acc_num})과 일치하는 계좌가 없어 첫 계좌를 사용합니다.")
    if chosen is None:
        chosen = accounts[0]

    seq = chosen.get("accountSeq")
    config.session.toss_account_seq = seq
    if not config.session.cano:
        config.session.cano = str(chosen.get("accountNo", ""))
    logger.info(f"[Toss] 사용 계좌: {chosen.get('accountNo')} (seq={seq}, type={chosen.get('accountType')})")
    return seq


# =========================================================================
# 시세 (Market Data)
# =========================================================================
def get_prices(symbols):
    """현재가 다건 조회. symbols: list[str] (최대 200). [{symbol,lastPrice,currency,timestamp}]"""
    if isinstance(symbols, (list, tuple)):
        symbols = ",".join(symbols)
    return _request("GET", "/api/v1/prices", group="MARKET_DATA",
                    params={"symbols": symbols}, account=False) or []


def get_price(symbol):
    """단일 종목 현재가. 없으면 None."""
    rows = get_prices([symbol])
    return rows[0] if rows else None


def get_orderbook(symbol):
    """호가. {timestamp,currency,asks:[{price,volume}],bids:[...]}"""
    return _request("GET", "/api/v1/orderbook", group="MARKET_DATA",
                    params={"symbol": symbol}, account=False)


def get_trades(symbol, count=50):
    """최근 체결 내역. [{price,volume,timestamp,currency}]"""
    return _request("GET", "/api/v1/trades", group="MARKET_DATA",
                    params={"symbol": symbol, "count": count}, account=False) or []


def get_price_limit(symbol):
    """상/하한가. {timestamp,upperLimitPrice,lowerLimitPrice,currency}"""
    return _request("GET", "/api/v1/price-limits", group="MARKET_DATA",
                    params={"symbol": symbol}, account=False)


def get_candles(symbol, interval="1d", count=100, before=None, adjusted=True):
    """캔들(OHLCV). interval: '1m' | '1d'. {candles:[...], nextBefore}"""
    params = {"symbol": symbol, "interval": interval, "count": count, "adjusted": str(adjusted).lower()}
    if before:
        params["before"] = before
    return _request("GET", "/api/v1/candles", group="MARKET_DATA_CHART",
                    params=params, account=False)


# =========================================================================
# 시장 지표 (Market Indicators) — 국내 지수·국채 [API 1.2.4 신설]
#  개별 종목이 아닌 '지수/금리' 전용 엔드포인트다. 카탈로그 밖 심볼은 400 unsupported-symbol.
#  코스피200·코스닥150은 카탈로그에 없으므로 여기서 조회할 수 없다(tvDatafeed 폴백 유지).
# =========================================================================
MARKET_INDICATOR_SYMBOLS = (
    "KOSPI", "KOSDAQ",
    "KR_BOND_2Y", "KR_BOND_3Y", "KR_BOND_5Y",
    "KR_BOND_10Y", "KR_BOND_20Y", "KR_BOND_30Y",
)


def get_market_indicator_prices(symbols):
    """시장 지표 현재가 다건. symbols: list[str] (카탈로그 심볼만).
    [{symbol, timestamp, lastPrice}] — 지수는 포인트, 국채는 % (수익률)."""
    if isinstance(symbols, (list, tuple)):
        symbols = ",".join(symbols)
    return _request("GET", "/api/v1/market-indicators/prices", group="MARKET_INDICATOR",
                    params={"symbols": symbols}, account=False) or []


def get_market_indicator_price(symbol):
    """시장 지표 단건 현재가. 없으면 None."""
    rows = get_market_indicator_prices([symbol])
    return rows[0] if rows else None


def get_market_indicator_candles(symbol, interval="1d", count=200, before=None):
    """시장 지표 캔들(OHLCV). {candles:[...], nextBefore}

    분봉('1m')은 지수(KOSPI·KOSDAQ)만 지원하고 국채는 일봉('1d')만 지원한다.
    종목 캔들(/api/v1/candles)과 달리 currency 필드가 없다.
    """
    params = {"interval": interval, "count": count}
    if before:
        params["before"] = before
    return _request("GET", f"/api/v1/market-indicators/{symbol}/candles",
                    group="MARKET_INDICATOR_CHART", params=params, account=False)


def get_market_indicator_investor_trading(symbol, interval="1d", count=10, until=None):
    """KRX 투자자별 매매동향(개인·외국인·기관·기타법인). KOSPI/KOSDAQ만 지원.
    {nextUntil, records:[{date, updatedAt, individual, foreigner, institution, otherCorporation}]}"""
    params = {"interval": interval, "count": count}
    if until:
        params["until"] = until
    return _request("GET", f"/api/v1/market-indicators/{symbol}/investor-trading",
                    group="MARKET_INDICATOR", params=params, account=False)


# =========================================================================
# 종목 정보 (Stock Info)
# =========================================================================
def get_rankings(rank_type="MARKET_TRADING_AMOUNT", market_country="KR",
                 duration="realtime", count=100, exclude_investment_caution=False):
    """주식 랭킹. {rankedAt, rankings:[{rank,symbol,currency,price:{lastPrice,basePrice,changeRate},...}]}

    MARKET_*/TOSS_* 타입의 price.basePrice = '전일 기준가'(= 전일 정규장 종가, HTS 등락률 기준가).
    → 랭킹 상위(대형주)의 KRX 기준가를 역산·저장 없이 라이브로 확보하는 용도.
    """
    return _request("GET", "/api/v1/rankings", group="RANKING",
                    params={"type": rank_type, "marketCountry": market_country,
                            "duration": duration, "count": count,
                            "excludeInvestmentCaution": str(exclude_investment_caution).lower()},
                    account=False)


def get_stocks(symbols):
    """종목 기본정보 다건. symbols: list[str] (최대 200)."""
    if isinstance(symbols, (list, tuple)):
        symbols = ",".join(symbols)
    return _request("GET", "/api/v1/stocks", group="STOCK",
                    params={"symbols": symbols}, account=False) or []


def get_stock(symbol):
    rows = get_stocks([symbol])
    return rows[0] if rows else None


def get_warnings(symbol):
    """매매 주의사항/VI. [{warningType,exchange,startDate,endDate}]"""
    return _request("GET", f"/api/v1/stocks/{symbol}/warnings", group="STOCK",
                    account=False) or []


# =========================================================================
# 시장 정보 (Market Info)
# =========================================================================
def get_exchange_rate(base="USD", quote="KRW", date_time=None):
    """환율. {baseCurrency,quoteCurrency,rate,midRate,...}"""
    params = {"baseCurrency": base, "quoteCurrency": quote}
    if date_time:
        params["dateTime"] = date_time
    return _request("GET", "/api/v1/exchange-rate", group="MARKET_INFO",
                    params=params, account=False)


def get_market_calendar(country="KR", date=None):
    """장 운영 캘린더. country: 'KR' | 'US'."""
    params = {}
    if date:
        params["date"] = date
    return _request("GET", f"/api/v1/market-calendar/{country}", group="MARKET_INFO",
                    params=params, account=False)


# =========================================================================
# 자산 (Asset)
# =========================================================================
def get_holdings(symbol=None):
    """보유 주식. {totalPurchaseAmount, marketValue, profitLoss, dailyProfitLoss, items:[...]}"""
    params = {}
    if symbol:
        params["symbol"] = symbol
    return _request("GET", "/api/v1/holdings", group="ASSET", params=params)


# =========================================================================
# 주문 정보 (Order Info)
# =========================================================================
def get_buying_power(currency="KRW"):
    """매수 가능 금액(현금 기준). {currency, cashBuyingPower}"""
    return _request("GET", "/api/v1/buying-power", group="ORDER_INFO",
                    params={"currency": currency})


def get_sellable_quantity(symbol):
    """매도 가능 수량. {sellableQuantity}"""
    return _request("GET", "/api/v1/sellable-quantity", group="ORDER_INFO",
                    params={"symbol": symbol})


def get_commissions():
    """매매 수수료. [{marketCountry,commissionRate,startDate,endDate}]"""
    res = _request("GET", "/api/v1/commissions", group="ORDER_INFO") or []
    for item in res:
        if 'commissionRate' in item and item['commissionRate']:
            try:
                # 1.2.14 변경: 소수비율(0.00015)을 백분율 문자열(0.015)로 변환 (하위호환 유지)
                item['commissionRate'] = str(float(item['commissionRate']) * 100)
            except Exception:
                pass
    return res



def get_short_selling(symbol, count=30):
    """공매도 동향 조회 (국내종목 전용)"""
    res = _request("GET", f"/api/v1/stocks/{symbol}/short-selling", 
                   group="STOCK_TRADING_TREND", params={"count": count})
    if res and "records" in res:
        return res["records"]
    return []

def get_investor_trend(symbol, count=10):
    """투자자별 매매동향 조회 (국내종목 전용)"""
    return _request("GET", f"/api/v1/stocks/{symbol}/investor-trading", 
                    group="STOCK_TRADING_TREND", params={"count": count}) or {}


# =========================================================================
# 주문 (Order)
# =========================================================================
def create_order(symbol, side, order_type="LIMIT", quantity=None, price=None,
                 order_amount=None, time_in_force=None, client_order_id=None,
                 confirm_high_value=False):
    """주문 생성. side: 'BUY'|'SELL', order_type: 'LIMIT'|'MARKET'.

    - quantity(수량 기반) 또는 order_amount(US MARKET 금액 기반) 중 하나.
    - LIMIT은 price 필수. US 종가(LOC)는 order_type='LIMIT', time_in_force='CLS'.
    반환: {orderId, clientOrderId}
    """
    body = {"symbol": symbol, "side": side, "orderType": order_type}
    if quantity is not None:
        body["quantity"] = str(quantity)
    if order_amount is not None:
        body["orderAmount"] = str(order_amount)
    if price is not None:
        body["price"] = str(price)
    if time_in_force:
        body["timeInForce"] = time_in_force
    if client_order_id:
        body["clientOrderId"] = client_order_id
    if confirm_high_value:
        body["confirmHighValueOrder"] = True
    return _request("POST", "/api/v1/orders", group="ORDER", json_body=body,
                    idempotent=False)


def modify_order(order_id, order_type="LIMIT", quantity=None, price=None, confirm_high_value=False):
    """주문 정정. KR은 quantity 허용, US는 price만. 반환: {orderId}(신규 주문ID)"""
    body = {"orderType": order_type}
    if quantity is not None:
        body["quantity"] = str(quantity)
    if price is not None:
        body["price"] = str(price)
    if confirm_high_value:
        body["confirmHighValueOrder"] = True
    return _request("POST", f"/api/v1/orders/{order_id}/modify", group="ORDER", json_body=body,
                    idempotent=False)


def cancel_order(order_id):
    """주문 취소. 반환: {orderId}(신규 주문ID)"""
    return _request("POST", f"/api/v1/orders/{order_id}/cancel", group="ORDER", json_body={},
                    idempotent=False)


# =========================================================================
# 주문 이력 (Order History)
# =========================================================================
def get_orders(status="OPEN", symbol=None, from_date=None, to_date=None, cursor=None, limit=None):
    """주문 목록. status: 'OPEN'(미체결) | 'CLOSED'(종료). {orders:[...], nextCursor, hasNext}"""
    params = {"status": status}
    if symbol:
        params["symbol"] = symbol
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if cursor:
        params["cursor"] = cursor
    if limit:
        params["limit"] = limit
    return _request("GET", "/api/v1/orders", group="ORDER_HISTORY", params=params)


def get_order(order_id):
    """주문 상세."""
    return _request("GET", f"/api/v1/orders/{order_id}", group="ORDER_HISTORY")
