# realtime.py
# -----------------------------------------------------------------------------
# 실시간 시세 추상화(RealtimeFeed) + KIS WebSocket 피드 구현.
#
# 목적: 보유/관심 종목의 현재가·체결강도·호가를 KIS WebSocket push로 수신해
#       마이크로 캐시에 보관하고, 읽기 경로(get_current_price 등)가 REST 대신
#       이 캐시를 우선 사용하도록 한다. 미구독/끊김/비활성 시 기존 REST로 자동 폴백한다.
#
# 제약(KIS): 단일 연결당 41건(종목×TR) 등록 한도 + approval_key당 동시 연결 1개.
#       → 보유종목 우선 구독 + 관심종목 로테이션(SubscriptionManager)으로 운용한다.
#
# 토스(mode 3): 공식 WS 미지원 → TossPollingFeed(빈 캐시)로 두어 항상 REST 폴백.
#       추후 토스가 WS를 공개하면 TossWsFeed만 추가하면 된다.
# -----------------------------------------------------------------------------
import asyncio
import base64
import json
import logging
import threading
import time

import requests

import config

logger = logging.getLogger("hts")

# ==========================================================
# KIS 실시간 TR 및 필드 인덱스
#  - 인덱스는 KIS apiportal 실시간 응답 스펙(파이프/캐럿 구분) 기준이다.
# ==========================================================
TR_PRICE = "H0STCNT0"   # 국내주식 실시간 체결가(KRX) — 현재가/등락/거래량/체결강도 포함
TR_ASK = "H0STASP0"     # 국내주식 실시간 호가(10단계 잔량)
TR_EXEC_REAL = "H0STCNI0"  # 실전 체결통보(AES256 암호화) — tr_key = HTS ID

# H0STCNT0(체결가) 레코드 필드 인덱스
_P_CODE, _P_PRICE, _P_CHG_RATE, _P_VOLUME, _P_VOL_STRENGTH = 0, 2, 5, 13, 18
# H0STASP0(호가) 레코드 필드 인덱스 (총 매도/매수 호가잔량)
_A_CODE, _A_TOTAL_ASK, _A_TOTAL_BID = 0, 43, 44
# H0STCNI0/9(체결통보) 레코드 필드 인덱스 (KIS 체결통보 스펙)
#  0:고객ID 1:계좌 2:주문번호 3:원주문 4:매도매수구분(01매도/02매수) 5:정정구분 6:주문종류
#  7:주문조건 8:종목코드 9:체결수량 10:체결단가 11:체결시각 12:거부여부(0정상/1거부)
#  13:체결구분(1접수·정정·취소·거부 / 2체결)
_E_CUST, _E_ACNT, _E_ODNO, _E_OODNO, _E_BUYSELL = 0, 1, 2, 3, 4
_E_CODE, _E_QTY, _E_PRICE, _E_TIME, _E_REJECT, _E_FILLED = 8, 9, 10, 11, 12, 13


def _to_float(s):
    try:
        return float(str(s).strip().replace(',', ''))
    except (TypeError, ValueError):
        return 0.0


def _split_records(body, count):
    """캐럿(^)으로 이어진 실시간 body를 count개의 레코드(필드 리스트)로 분리한다."""
    fields = body.split('^')
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 1
    if n <= 1 or not fields:
        return [fields]
    width = len(fields) // n
    if width <= 0:
        return [fields]
    return [fields[i * width:(i + 1) * width] for i in range(n)]


def parse_h0stcnt0(body, count="1"):
    """실시간 체결가(H0STCNT0) body를 파싱해 종목별 dict 리스트를 반환한다."""
    out = []
    for rec in _split_records(body, count):
        if len(rec) <= _P_VOL_STRENGTH:
            continue
        out.append({
            'code': rec[_P_CODE],
            'price': _to_float(rec[_P_PRICE]),
            'change_rate': _to_float(rec[_P_CHG_RATE]),
            'volume': _to_float(rec[_P_VOLUME]),
            'vol_strength': _to_float(rec[_P_VOL_STRENGTH]),
        })
    return out


def parse_h0stasp0(body, count="1"):
    """실시간 호가(H0STASP0) body를 파싱해 종목별 총 매도/매수 잔량 dict 리스트를 반환한다."""
    out = []
    for rec in _split_records(body, count):
        if len(rec) <= _A_TOTAL_BID:
            continue
        out.append({
            'code': rec[_A_CODE],
            'total_ask': _to_float(rec[_A_TOTAL_ASK]),
            'total_bid': _to_float(rec[_A_TOTAL_BID]),
        })
    return out


def aes_cbc_decrypt(b64_cipher, key, iv):
    """KIS 체결통보 AES256-CBC(PKCS7) 복호화. 표준 라이브러리 cryptography 사용(추가 의존성 없음)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    raw = base64.b64decode(b64_cipher)
    decryptor = Cipher(algorithms.AES(key.encode('utf-8')), modes.CBC(iv.encode('utf-8'))).decryptor()
    plain = decryptor.update(raw) + decryptor.finalize()
    pad = plain[-1] if plain else 0  # PKCS7 패딩 제거
    if 0 < pad <= 16:
        plain = plain[:-pad]
    return plain.decode('utf-8', errors='ignore')


def parse_h0stcni(plain_body):
    """복호화된 체결통보 body(캐럿 구분)를 파싱해 dict를 반환한다. 부족하면 None."""
    rec = plain_body.split('^')
    if len(rec) <= _E_FILLED:
        return None
    code = rec[_E_CODE].strip()
    if code.upper().startswith("KR") and len(code) >= 9:
        code = code[3:9]   # ISIN 표준코드(KR7+단축6자리+…) → 단축코드 6자리
    elif len(code) > 6:
        code = code[-6:]
    return {
        'cust_id': rec[_E_CUST],
        'acnt': rec[_E_ACNT],
        'odno': rec[_E_ODNO].strip(),
        'orig_odno': rec[_E_OODNO].strip(),
        'buy_sell': rec[_E_BUYSELL].strip(),       # '01'=매도 '02'=매수
        'code': code,
        'qty': _to_float(rec[_E_QTY]),
        'price': _to_float(rec[_E_PRICE]),
        'time': rec[_E_TIME].strip(),
        'rejected': rec[_E_REJECT].strip() == '1',
        'is_fill': rec[_E_FILLED].strip() == '2',  # '2'=실제 체결, '1'=접수/정정/취소/거부
    }


# ==========================================================
# 구독 관리자: 41건 한도 내에서 보유 우선 + 관심 로테이션 계획을 만든다.
# ==========================================================
class SubscriptionManager:
    """41건 한도 내 구독 계획을 만든다. **시스템 트레이딩 종목을 최우선**으로 구독한다.

    우선순위(priority)는 시스템 트레이딩 종목(보유종목 먼저, 그 다음 매수후보) 순서로 전달한다.
      - priority가 용량 이내면: priority는 **전부 항상 구독**(로테이션 없음), 남는 슬롯만 그 외(other) 관심종목을 로테이션.
      - priority만으로 용량을 초과하면: **priority 안에서만 로테이션**(시스템 종목끼리 번갈아 커버), other는 구독하지 않는다.
    """
    def __init__(self, max_regs=41, subscribe_orderbook=True):
        self.max_regs = int(max_regs)
        self.subscribe_orderbook = bool(subscribe_orderbook)
        self._reserved = 0    # 체결통보 등 시세 외 고정 등록 슬롯 수(종목 용량에서 제외).
        self._priority = []   # 시스템 트레이딩 종목(보유→후보 순). 최우선.
        self._other = []      # 그 외 관심종목. 남는 슬롯에 로테이션.
        self._offset = 0
        self._lock = threading.RLock()

    def set_reserved(self, n):
        """체결통보 등 시세 외 고정 등록이 차지하는 슬롯 수를 지정한다."""
        with self._lock:
            self._reserved = max(0, int(n))

    def set_symbols(self, priority, other=None):
        """priority: 시스템 트레이딩 종목(보유 먼저, 후보 다음). other: 그 외 관심종목."""
        with self._lock:
            self._priority = self._dedup(priority)
            pset = set(self._priority)
            self._other = [c for c in self._dedup(other) if c not in pset]

    @staticmethod
    def _dedup(codes):
        seen, out = set(), []
        for c in (codes or []):
            if c and c not in seen:
                seen.add(c); out.append(c)
        return out

    def _regs_per_symbol(self):
        # 종목당 '최소' 등록 수는 1(현재가). 호가는 남는 슬롯에 best-effort로 얹으므로
        # 현재가 커버리지를 절반으로 깎지 않는다.
        return 1

    def capacity_symbols(self):
        """동시에 현재가를 구독 가능한 종목 수(등록 한도 - 예약 슬롯).
        (호가는 별도 예산이 아니라 현재가 등록 후 남는 슬롯에 얹는다.)"""
        return max(0, self.max_regs - self._reserved)

    def advance(self):
        """로테이션 윈도우를 한 칸 전진시킨다(other 또는 초과 priority 순환용)."""
        with self._lock:
            self._offset += 1

    def plan(self):
        """현재 구독해야 할 (tr_id, code) 등록 집합을 한도 내에서 산출한다.

        등록 예산(=한도-예약)을 현재가(H0STCNT0)에 우선 배정해 최대한 많은 종목을 커버하고,
        호가(H0STASP0)는 남는 슬롯에 우선순위(보유→후보) 순으로 best-effort로 얹는다.
        이렇게 하면 호가 구독을 켜도 현재가 커버리지가 절반으로 줄지 않는다.
        """
        with self._lock:
            budget = max(0, self.max_regs - self._reserved)  # 남은 등록 예산
            cap = budget  # 현재가는 종목당 1등록이므로 예산이 곧 종목 수
            pri, oth = self._priority, self._other

            if cap <= 0:
                chosen = []
            elif len(pri) >= cap:
                # 시스템 종목만으로 용량 초과 → 시스템 종목끼리만 로테이션(그 외 구독 안 함)
                off = self._offset % len(pri)
                chosen = [pri[(off + i) % len(pri)] for i in range(cap)]
            else:
                # 시스템 종목 전부 항상 구독 + 남는 슬롯에 그 외 관심종목 로테이션
                chosen = list(pri)
                slots = cap - len(pri)
                if oth and slots > 0:
                    off = self._offset % len(oth)
                    chosen += [oth[(off + i) % len(oth)] for i in range(min(slots, len(oth)))]

            # 1) 현재가 우선 등록
            regs = [(TR_PRICE, code) for code in chosen]
            # 2) 호가는 남는 등록 슬롯에 우선순위(chosen 순서)대로 best-effort 추가
            if self.subscribe_orderbook:
                remaining = budget - len(regs)
                for code in chosen:
                    if remaining <= 0:
                        break
                    regs.append((TR_ASK, code))
                    remaining -= 1
            return regs

    def coverage(self):
        """현재 구독 계획 기준 커버리지 요약을 반환한다.
        반환: dict(priority=시스템종목수, capacity=동시 현재가 용량,
                  price_covered=현재가 커버 종목수, ob_covered=호가 커버 종목수,
                  rest_fallback=현재가를 REST로 폴백해야 하는 시스템 종목수)."""
        with self._lock:
            regs = self.plan()
            price_codes = {c for (t, c) in regs if t == TR_PRICE}
            ob_codes = {c for (t, c) in regs if t == TR_ASK}
            pri_set = set(self._priority)
            return {
                'priority': len(pri_set),
                'capacity': self.capacity_symbols(),
                'price_covered': len(price_codes),
                'ob_covered': len(ob_codes),
                'rest_fallback': len(pri_set - price_codes),
            }


# ==========================================================
# 추상 인터페이스
# ==========================================================
class RealtimeFeed:
    def start(self): pass
    def stop(self): pass
    def set_symbols(self, priority, other=None): pass
    def coverage(self): return None
    def get_price(self, code, max_age=3.0): return None
    def get_vol_strength(self, code, max_age=3.0): return None
    def get_orderbook(self, code, max_age=3.0): return None


class TossPollingFeed(RealtimeFeed):
    """토스(mode 3): 공식 WS 미지원 → 빈 피드(항상 None) → 읽기 경로가 기존 REST 폴링 사용."""
    pass


# ==========================================================
# KIS WebSocket 피드
# ==========================================================
class KisRealtimeFeed(RealtimeFeed):
    def __init__(self):
        self.manager = SubscriptionManager(
            max_regs=getattr(config, 'WS_MAX_REGISTRATIONS', 41),
            subscribe_orderbook=getattr(config, 'WS_SUBSCRIBE_ORDERBOOK', True),
        )
        self._price = {}   # code -> {'price','change_rate','volume','vol_strength','ts'}
        self._ask = {}     # code -> {'total_ask','total_bid','ts'}
        self._cache_lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._loop = None
        self._subscribed = set()  # 현재 연결에 등록된 (tr_id, code)
        self._was_enabled = None  # USE_WEBSOCKET 상태 전환 로깅용
        self._got_data = False    # 연결당 첫 데이터 수신 로깅용
        # ---- 체결통보(H0STCNI0/9) ----
        self._exec_callbacks = []   # 체결통보 도착 시 호출할 콜백(notice dict 인자)
        self._cb_lock = threading.RLock()
        self._aes_key = None        # 구독 응답으로 수신하는 AES256 key/iv
        self._aes_iv = None
        self._exec_subscribed = False  # 연결당 체결통보 구독 여부
        self._got_exec = False         # 연결당 첫 체결통보 로깅용

    # ---- 읽기 API (읽기 경로가 호출) ----
    def _fresh(self, entry, max_age):
        return entry is not None and (time.time() - entry['ts']) <= max_age

    def get_price(self, code, max_age=3.0):
        with self._cache_lock:
            e = self._price.get(code)
            if self._fresh(e, max_age) and e['price'] > 0:
                return e['price']
        return None

    def get_vol_strength(self, code, max_age=3.0):
        with self._cache_lock:
            e = self._price.get(code)
            if self._fresh(e, max_age):
                return e['vol_strength']
        return None

    def get_orderbook(self, code, max_age=3.0):
        with self._cache_lock:
            e = self._ask.get(code)
            if self._fresh(e, max_age):
                return {'total_ask': e['total_ask'], 'total_bid': e['total_bid']}
        return None

    # ---- 라이프사이클 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="KisRealtimeFeed")
        self._thread.start()
        logger.info("[WS] KIS 실시간 피드 시작")

    def stop(self):
        self._stop.set()

    def set_symbols(self, priority, other=None):
        self.manager.set_symbols(priority, other)

    def coverage(self):
        return self.manager.coverage()

    # ---- 체결통보 ----
    def register_exec_callback(self, fn):
        """체결통보 도착 시 호출할 콜백을 등록한다(중복 등록 방지)."""
        with self._cb_lock:
            if fn not in self._exec_callbacks:
                self._exec_callbacks.append(fn)

    def _hts_id(self):
        return (getattr(config.session, 'hts_id', '') or '').strip()

    def _exec_enabled(self):
        """체결통보 WS 사용 조건: USE_WEBSOCKET ON + HTS ID 설정됨."""
        return self._enabled() and bool(self._hts_id())

    def _exec_tr_id(self):
        return TR_EXEC_REAL

    def _invoke_exec_callbacks(self, notice):
        with self._cb_lock:
            callbacks = list(self._exec_callbacks)
        for fn in callbacks:
            try:
                fn(notice)
            except Exception as e:
                logger.debug(f"[WS] 체결통보 콜백 오류: {e}")

    async def _subscribe_exec(self, ws, approval):
        """연결 직후 체결통보(HTS ID 키)를 1회 구독한다. 실패해도 시세/폴백에는 영향 없음."""
        if self._exec_subscribed or not self._exec_enabled():
            return
        try:
            await ws.send(self._sub_msg(approval, self._exec_tr_id(), self._hts_id(), subscribe=True))
            self._exec_subscribed = True
            logger.info(f"[WS] 체결통보 구독 요청(tr={self._exec_tr_id()}, id=…{self._hts_id()[-3:]})")
        except Exception as e:
            logger.info(f"[WS] 체결통보 구독 실패(REST 폴백 유지): {e}")

    # ---- 내부: approval key / URI ----
    def _fetch_approval_key(self):
        try:
            base = config.session.url_base or config.REAL_URL
            appkey = config.session.app_key or config.session.real_app_key
            secret = config.session.app_secret or config.session.real_app_secret
            if not appkey or not secret:
                logger.info("[WS] approval_key 발급 불가: APP_KEY/SECRET 미설정 → REST 폴백")
                return None
            res = requests.post(f"{base}/oauth2/Approval", json={
                "grant_type": "client_credentials", "appkey": appkey, "secretkey": secret
            }, timeout=5)
            if res.status_code == 200:
                key = res.json().get("approval_key")
                if key:
                    logger.info(f"[WS] approval_key 발급 완료 (…{str(key)[-6:]})")
                    return key
            logger.info(f"[WS] approval_key 발급 실패 (status={res.status_code})")
        except Exception as e:
            logger.info(f"[WS] approval_key 발급 오류: {e}")
        return None

    def _ws_uri(self):
        # 실전 21000 / 모의 31000 (ops 도메인)
        port = 21000
        return f"ws://ops.koreainvestment.com:{port}"

    def _sub_msg(self, approval, tr_id, tr_key, subscribe=True):
        return json.dumps({
            "header": {"approval_key": approval, "custtype": "P",
                       "tr_type": "1" if subscribe else "2", "content-type": "utf-8"},
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        })

    # ---- 내부: 스레드/이벤트루프 ----
    def _thread_main(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run())
        except Exception as e:
            logger.debug(f"[WS] 이벤트루프 종료: {e}")

    @staticmethod
    def _enabled():
        return getattr(config, 'USE_WEBSOCKET', True)

    def _log_toggle(self, enabled):
        """USE_WEBSOCKET 상태 전환만 INFO로 1회 기록(매 루프 도배 방지)."""
        if self._was_enabled is None or self._was_enabled != enabled:
            if enabled:
                logger.info("[WS] USE_WEBSOCKET 켜짐 → 실시간 연결 시작")
            else:
                logger.info("[WS] USE_WEBSOCKET 꺼짐 → 연결 안 함(REST 폴백)")
            self._was_enabled = enabled

    async def _run(self):
        import websockets
        backoff = getattr(config, 'WS_RECONNECT_BACKOFF_SEC', 5)
        while not self._stop.is_set():
            # [런타임 토글] USE_WEBSOCKET이 꺼져 있으면 연결하지 않고 유휴 대기한다.
            #  메뉴 0에서 다시 켜면(재시작 없이) 이 루프가 곧바로 연결을 시작한다.
            if not self._enabled():
                self._log_toggle(False)
                await asyncio.sleep(2)
                continue
            self._log_toggle(True)
            approval = await self._loop.run_in_executor(None, self._fetch_approval_key)
            if not approval:
                await asyncio.sleep(backoff)
                continue
            label = "실전"
            try:
                async with websockets.connect(self._ws_uri(), ping_interval=None, max_size=None) as ws:
                    logger.info(f"[WS] 연결 성공 ({self._ws_uri()}, {label})")
                    self._got_data = False
                    self._got_exec = False
                    self._subscribed = set()
                    self._exec_subscribed = False
                    self._aes_key = self._aes_iv = None
                    # 체결통보를 쓰면 등록 슬롯 1개를 예약(시세 종목 용량에서 제외) 후 먼저 구독한다.
                    self.manager.set_reserved(1 if self._exec_enabled() else 0)
                    await self._subscribe_exec(ws, approval)
                    await self._reconcile(ws, approval)
                    reconciler = asyncio.ensure_future(self._reconcile_loop(ws, approval))
                    watcher = asyncio.ensure_future(self._disable_watcher(ws))
                    try:
                        async for msg in ws:
                            self._on_message(msg, ws, approval)
                            if self._stop.is_set() or not self._enabled():
                                break
                    finally:
                        reconciler.cancel()
                        watcher.cancel()
                logger.info("[WS] 연결 종료")
            except Exception as e:
                logger.info(f"[WS] 연결 오류(재연결 {backoff}s 후 시도): {e}")
            if self._stop.is_set():
                break
            await asyncio.sleep(1)

    async def _disable_watcher(self, ws):
        """USE_WEBSOCKET이 꺼지거나 중지되면 소켓을 닫아 수신 루프를 즉시 종료한다(연결 해제)."""
        try:
            while True:
                await asyncio.sleep(1)
                if self._stop.is_set() or not self._enabled():
                    if not self._enabled():
                        logger.info("[WS] 토글 OFF 감지 → 연결 해제(REST 폴백)")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    async def _reconcile_loop(self, ws, approval):
        interval = getattr(config, 'WS_ROTATE_INTERVAL_SEC', 30)
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval)
                self.manager.advance()  # 관심종목 로테이션
                await self._reconcile(ws, approval)
        except asyncio.CancelledError:
            pass

    async def _reconcile(self, ws, approval):
        """원하는 구독 집합(plan)과 현재 구독을 비교해 sub/unsub 메시지를 보낸다."""
        desired = set(self.manager.plan())
        to_add = desired - self._subscribed
        to_remove = self._subscribed - desired
        for tr_id, code in to_remove:
            try:
                await ws.send(self._sub_msg(approval, tr_id, code, subscribe=False))
                await asyncio.sleep(0.02)
            except Exception:
                break
        for tr_id, code in to_add:
            try:
                await ws.send(self._sub_msg(approval, tr_id, code, subscribe=True))
                await asyncio.sleep(0.02)  # 구독 폭주 완화
            except Exception:
                break
        prev_count = len(self._subscribed)
        self._subscribed = desired
        if to_add or to_remove:
            codes = sorted({c for (_t, c) in desired})
            msg = (f"[WS] 구독 갱신: +{len(to_add)} -{len(to_remove)} "
                   f"(등록 {len(desired)}/{self.manager.max_regs}건, 종목 {len(codes)}개)")
            # 종목 수가 바뀐 의미있는 변화(최초 구독·관심종목 증감)만 INFO, 정기 로테이션(개수 동일·교체만)은
            # DEBUG로 낮춰 로그 도배를 막는다. 시스템 트레이딩 우선종목은 항상 구독되어 교체 대상이 아니다.
            if len(desired) != prev_count:
                logger.info(msg)
            else:
                logger.debug(msg)

    def _on_message(self, msg, ws, approval):
        try:
            if not msg:
                return
            if msg[0] in '{[':
                # 제어/PINGPONG (JSON)
                data = json.loads(msg)
                tr_id = data.get("header", {}).get("tr_id")
                if tr_id == "PINGPONG":
                    asyncio.ensure_future(ws.send(msg))  # 핑퐁 에코
                    return
                # 체결통보 구독 응답: AES256 key/iv 수신 → 보관(이후 암호화 프레임 복호화에 사용)
                if tr_id == TR_EXEC_REAL:
                    out = (data.get("body") or {}).get("output") or {}
                    key, iv = out.get("key"), out.get("iv")
                    if key and iv:
                        self._aes_key, self._aes_iv = key, iv
                        logger.info("[WS] 체결통보 암호화 키 수신 완료 → 체결 감지 활성화")
                return
            # 실시간 데이터: {암호화여부}|{tr_id}|{건수}|{body}
            parts = msg.split('|', 3)
            if len(parts) < 4:
                return
            enc, tr_id, count, body = parts
            now = time.time()
            # 체결통보(암호화): 복호화 후 파싱 → 콜백(ConclusionMonitor 즉시 확인 트리거)
            if tr_id == TR_EXEC_REAL:
                self._handle_exec_frame(body)
                return
            if not self._got_data and tr_id in (TR_PRICE, TR_ASK):
                self._got_data = True
                logger.info(f"[WS] 실시간 데이터 수신 시작 (tr={tr_id})")
            if tr_id == TR_PRICE:
                with self._cache_lock:
                    for r in parse_h0stcnt0(body, count):
                        self._price[r['code']] = {
                            'price': r['price'], 'change_rate': r['change_rate'],
                            'volume': r['volume'], 'vol_strength': r['vol_strength'], 'ts': now,
                        }
            elif tr_id == TR_ASK:
                with self._cache_lock:
                    for r in parse_h0stasp0(body, count):
                        self._ask[r['code']] = {
                            'total_ask': r['total_ask'], 'total_bid': r['total_bid'], 'ts': now,
                        }
        except Exception as e:
            logger.debug(f"[WS] 메시지 처리 오류: {e}")

    def _handle_exec_frame(self, body):
        """암호화된 체결통보 프레임을 복호화·파싱해 콜백을 호출한다(실패 시 REST 폴백)."""
        if not (self._aes_key and self._aes_iv):
            # 키 미수신(구독 응답 누락 등) → 복호화 불가. 폴링이 체결을 잡으므로 조용히 무시.
            logger.debug("[WS] 체결통보 수신했으나 암호화 키 미보유 → REST 폴백 처리")
            return
        try:
            plain = aes_cbc_decrypt(body, self._aes_key, self._aes_iv)
            notice = parse_h0stcni(plain)
        except Exception as e:
            logger.info(f"[WS] 체결통보 복호화 실패(REST 폴백): {e}")
            return
        if not notice:
            return
        if not self._got_exec:
            self._got_exec = True
            logger.info("[WS] 체결통보 수신 시작")
        side = '매수' if notice['buy_sell'] == '02' else '매도'
        kind = '체결' if notice['is_fill'] else ('거부' if notice['rejected'] else '접수/정정')
        logger.info(f"[WS] 체결통보 {kind}: {notice['code']} {side} {notice['qty']:.0f}주 "
                    f"@{notice['price']:.0f} (주문 {notice['odno']})")
        self._invoke_exec_callbacks(notice)


# ==========================================================
# 전역 싱글톤 + 라이프사이클 헬퍼
# ==========================================================
_feed = None
_feed_lock = threading.RLock()


def get_feed():
    """현재 모드에 맞는 실시간 피드 싱글톤을 반환한다(자동 시작하지 않음)."""
    global _feed
    with _feed_lock:
        if _feed is None:
            if getattr(config.session, 'is_toss', False):
                _feed = TossPollingFeed()
            else:
                _feed = KisRealtimeFeed()
        return _feed


def start_feed():
    """KIS 모드에서 실시간 피드 스레드를 시작한다.

    USE_WEBSOCKET이 꺼져 있어도 스레드는 떠 있되 연결 없이 유휴 대기한다. 메뉴 0에서 토글을
    켜면 프로그램 재시작 없이 자동으로 연결을 시작하고, 끄면 연결을 해제하고 REST로 폴백한다.
    토스(mode 3)는 공식 WS 미지원이라 시작하지 않는다(항상 REST).
    """
    if getattr(config.session, 'is_toss', False):
        logger.info("[WS] 토스 모드: 공식 WS 미지원 → REST 폴링 유지")
        return None
    feed = get_feed()
    feed.start()
    return feed


def stop_feed():
    global _feed
    with _feed_lock:
        if _feed is not None:
            _feed.stop()


def update_symbols(priority, other=None):
    """구독 종목을 피드에 반영한다(구독 계획 갱신).

    priority: 시스템 트레이딩 종목(보유 먼저, 매수후보 다음) — 최우선 구독.
    other: 그 외 관심종목 — 남는 슬롯에 로테이션.
    """
    try:
        get_feed().set_symbols(priority, other)
    except Exception as e:
        logger.debug(f"[WS] 심볼 갱신 오류: {e}")


def coverage():
    """현재 WS 구독 커버리지 요약(dict) 또는 None(미지원/오류)."""
    try:
        return get_feed().coverage()
    except Exception:
        return None


def register_exec_callback(fn):
    """체결통보(H0STCNI0/9) 도착 시 호출할 콜백을 등록한다(KIS 피드 한정).

    토스/미지원 피드에서는 메서드가 없으므로 조용히 무시 → 기존 REST 폴링이 체결을 처리한다.
    """
    try:
        feed = get_feed()
        if hasattr(feed, 'register_exec_callback'):
            feed.register_exec_callback(fn)
    except Exception as e:
        logger.debug(f"[WS] 체결통보 콜백 등록 오류: {e}")
