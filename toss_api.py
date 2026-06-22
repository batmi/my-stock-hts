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
from datetime import datetime, timedelta

import requests

import config

logger = logging.getLogger("hts")

_BASE = config.TOSS_URL

# 토큰 메모리 캐시 (디스크 IO 절감용; 영속 캐시는 config.session 토큰 캐시 재사용)
_token_lock = threading.Lock()

# 그룹별 호출 간 최소 간격 제어용
_rate_lock = threading.Lock()
_last_call_ts = {}


class TossApiError(Exception):
    """토스 API 오류(error envelope 또는 HTTP 오류)."""

    def __init__(self, code, message, status=None, request_id=None, data=None):
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id
        self.data = data or {}
        super().__init__(f"[{status}] {code}: {message}")


# =========================================================================
# 인증 (OAuth2 Client Credentials)
# =========================================================================
def get_access_token(force_refresh=False):
    """토스 access token을 발급/캐시한다. 실패 시 None."""
    with _token_lock:
        if not force_refresh:
            cached = config.session.get_valid_token("TOSS")
            if cached:
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
                    logger.info("[Toss] 액세스 토큰 발급 완료")
                    return token
                logger.error(f"[Toss] 토큰 응답에 access_token 없음: {res.text[:200]}")
                return None
            else:
                # OAuth2 표준 에러 포맷 {"error","error_description"}
                try:
                    err = res.json()
                    logger.error(
                        f"[Toss] 토큰 발급 실패 ({res.status_code}): "
                        f"{err.get('error')} - {err.get('error_description')}"
                    )
                except Exception:
                    logger.error(f"[Toss] 토큰 발급 실패 ({res.status_code}): {res.text[:200]}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"[Toss] 토큰 발급 네트워크 오류: {e}")
            return None


# =========================================================================
# 공통 요청 처리
# =========================================================================
def _throttle(group):
    """그룹별 최소 호출 간격을 보장한다."""
    min_interval = 1.0 / max(config.TOSS_TX_PER_SECOND, 1)
    while True:
        with _rate_lock:
            now = time.time()
            last = _last_call_ts.get(group, 0.0)
            wait = min_interval - (now - last)
            if wait <= 0:
                _last_call_ts[group] = now
                return
        time.sleep(wait)


def _request(method, path, group, params=None, json_body=None, account=True, retries=2):
    """토스 API 호출 공통 처리. 성공 시 result 페이로드를 반환한다.

    account=True 이면 X-Tossinvest-Account 헤더(accountSeq)를 부착한다.
    오류 시 TossApiError를 발생시킨다.
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
    for attempt in range(retries + 1):
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
            last_exc = TossApiError("network-error", str(e), status=None)
            if attempt < retries:
                time.sleep(config.RETRY_DELAY_SERVER * (attempt + 1))
                continue
            raise last_exc

        # Rate limit
        if res.status_code == 429:
            retry_after = res.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after and retry_after.isdigit()) else 1.0
            if attempt < retries:
                time.sleep(wait)
                continue
            raise TossApiError("rate-limit-exceeded", "요청 한도를 초과했습니다.", status=429)

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
        except Exception:
            pass

        # 5xx는 재시도
        if res.status_code >= 500 and attempt < retries:
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
# 종목 정보 (Stock Info)
# =========================================================================
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
    return _request("GET", "/api/v1/commissions", group="ORDER_INFO") or []


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
    return _request("POST", "/api/v1/orders", group="ORDER", json_body=body)


def modify_order(order_id, order_type="LIMIT", quantity=None, price=None, confirm_high_value=False):
    """주문 정정. KR은 quantity 허용, US는 price만. 반환: {orderId}(신규 주문ID)"""
    body = {"orderType": order_type}
    if quantity is not None:
        body["quantity"] = str(quantity)
    if price is not None:
        body["price"] = str(price)
    if confirm_high_value:
        body["confirmHighValueOrder"] = True
    return _request("POST", f"/api/v1/orders/{order_id}/modify", group="ORDER", json_body=body)


def cancel_order(order_id):
    """주문 취소. 반환: {orderId}(신규 주문ID)"""
    return _request("POST", f"/api/v1/orders/{order_id}/cancel", group="ORDER", json_body={})


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
