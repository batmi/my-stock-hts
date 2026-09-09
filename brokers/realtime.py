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
# 토스(mode 3): 2026-09-09 공식 WS 공개(AsyncAPI 3.0.0) → TossWsFeed.
#       1단계로 **주문 이벤트(personal:order)만** 구독한다. 시세는 붙이지 않아 읽기 경로가
#       계속 REST 폴링을 쓴다(사유는 TossWsFeed 머리말). 쓸 수 없으면 TossPollingFeed로 퇴화.
#       한도(토스): 구독 100건(채널×종목) + 계정당 동시 연결 2개 — KIS의 41건보다 넉넉하다.
# -----------------------------------------------------------------------------
import asyncio
import base64
import json
import logging
import random
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
            #  [캐시 키] 읽기 경로는 6자리 단축코드로 조회한다. 전문에 공백이 섞이면
            #  캐시 키가 어긋나 WS가 영원히 안 맞고, 조용히 REST로만 돌아간다(무증상 성능 저하).
            'code': rec[_P_CODE].strip(),
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
            'code': rec[_A_CODE].strip(),      # 위와 같은 이유(캐시 키 일치)
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

        [호가가 언제 붙는가 · 실측 2026-09-05] 현재가에 예산을 **전부** 주므로, 남는 슬롯은
         `구독 대상 종목 수 < 용량`일 때만 생긴다. 즉 호가는 **관심종목 총수가 용량(한도-예약,
         체결통보 사용 시 40)보다 적을 때만** 붙는다. 지금 관심종목은 국내 64개(주식 44 +
         ETF 20)라 실제 운용에서는 호가 구독이 **0건**이고, 그만큼 REST 호가 조회가
         그대로 나간다(coverage()['ob_covered'] 로 확인 가능).

         이것을 바꾸려면 현재가 커버리지를 호가와 맞바꿔야 한다(등록 슬롯은 제로섬이다).
         커버리지를 절반으로 깎지 않겠다는 것이 이 함수의 설계 선택이므로, 배분 정책은
         여기서 임의로 바꾸지 않는다 — 운영자가 정할 축이다.
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
    """토스 폴백 피드: 빈 피드(항상 None) → 읽기 경로가 기존 REST 폴링을 그대로 쓴다.

    TossWsFeed 를 쓸 수 없을 때(USE_WEBSOCKET OFF·websockets 미설치 등) 자리를 지킨다.
    """
    pass


# ==========================================================
# 토스 WebSocket 피드 (mode 3)
# ==========================================================
#  [무엇을 구독하는가 · 1단계] 주문 이벤트(`personal:order`) **하나뿐**이다.
#   시세(`trade:kr`·`orderbook:kr`)는 스펙상 가능하지만 아직 붙이지 않는다:
#     · `trade:kr` 은 "KRX 정규장 + NXT 프리·정규·애프터마켓 **합산**"이다. 이 프로젝트의
#       경계는 '판단(지표)=KRX 확정 봉, 트리거·주문가=실시간가'이므로(krx-nxt-data-boundary),
#       지표 경로에 닿으면 ATR 이 부풀어 오른다(krx-daily-source 가 기록한 6~15%).
#     · 프레임에 누적 거래량·매수/매도 구분이 없고 유실(LOSSY) 가능성이 있어, 스펙이
#       **수신 합산으로 누적 거래량을 재구성하지 말라고 명시**한다. KIS 캐시는 volume 을
#       담는 자리가 있어 0 으로 채우기 쉬운데, 그러면 '못 잰 값'이 '0 거래'로 둔갑한다.
#     · 체결강도는 토스가 REST 에서도 제공하지 않는다(api/quotes/price.py 가 None 반환).
#       웹소켓도 이 값을 주지 않으므로 붙여도 게이트가 얻는 것이 없다.
#   그래서 시세 읽기 메서드는 RealtimeFeed 기본값(None)을 그대로 둔다 = REST 폴백.
#
#  [KIS 와 다른 점 — 코드를 옮겨 오면 깨지는 자리]
#    · 구독이 **선언형 full-replace** 다. sub/unsub 액션이 없고, 보낸 배열 하나가 곧 현재
#      구독 전체다(빠진 항목 자동 해제, `[]` 는 전체 해제). KisRealtimeFeed._reconcile 의
#      집합 diff 방식을 그대로 쓸 수 없다.
#    · keepalive 는 JSON 이 아니라 **순수 텍스트 `PING`**(대문자 4글자)이다. 그리고 서버가
#      보내주는 데이터는 idle 타이머를 리셋하지 않는다 — 데이터를 받는 중에도 계속 보내야 한다.
#    · 인증은 handshake 1회뿐이라 연결 유지 중 토큰이 만료돼도 끊기지 않는다. 토큰은
#      **재연결할 때만** 필요하다.
#    · 동시 연결이 계정당 2개이고, 초과하면 **가장 오래된 연결이 close code 없이** 종료된다.
#      같은 앱키를 다른 기기에서 쓰면 서로 밀어내는데 로그에는 그냥 '끊김'으로 보인다.
#      그래서 재연결은 반드시 지수 백오프(jitter 포함)로 한다 — 즉시 재연결하면 두 기기가
#      서로를 무한히 밀어낸다.
class TossWsFeed(RealtimeFeed):
    """토스 주문 이벤트 웹소켓 피드.

    [설계 원칙] 프레임 내용을 **믿지 않는다.** 주문 이벤트가 오면 ConclusionMonitor 를
     깨우기만 하고, 체결 확정은 검증된 REST 경로가 한다(KIS 체결통보와 같은 규약 —
     modules/auto_trade/conclusion.py 의 _on_ws_exec_notice 참고). 웹소켓은 **지연을
     줄일 뿐**이고, 한 프레임도 오지 않아도 주기 폴링이 같은 체결을 잡는다.

     이 원칙은 토스에서 더 강하게 지켜야 한다. 스펙이 이렇게 적고 있다:
     "클라이언트 수신이 2초 이상 계속 막히면(backpressure) 서버가 연결을 끊습니다."
     콜백에서 DB 를 쓰거나 REST 를 부르면 그 순간 연결이 끊긴다.

    [무손실의 범위] 주문 채널은 LOSSLESS 지만 **연결 세션 안에서만** 그렇다. 끊긴 구간의
     이벤트는 재전송되지 않으므로, 재연결하면 구독을 다시 선언하고 REST 로 주문 상태를
     재동기화해야 한다. 여기서는 재연결마다 콜백을 한 번 깨워(`reconnect` 통보) 그 일을
     ConclusionMonitor 에 맡긴다 — 이 피드가 직접 주문을 조회하지 않는다.
    """

    def __init__(self):
        #  구독 계획은 KIS 와 **같은 정책**을 쓴다(보유→후보 우선, 남는 슬롯에 관심종목
        #  로테이션, 현재가 먼저·호가는 잔여 슬롯). 한도만 100 으로 다르다. 토스도 구독 수를
        #  '채널×종목'으로 세므로 KIS 의 'TR×종목'과 셈법이 같아 그대로 재사용된다.
        self.manager = SubscriptionManager(
            max_regs=getattr(config, 'TOSS_WS_MAX_SUBSCRIPTIONS', 100),
            subscribe_orderbook=True,
        )
        self.manager.set_reserved(1)   # personal:order 몫 한 자리
        self._price = {}   # code -> {'price', 'ts'}   ※ volume·vol_strength 는 담지 않는다
        self._ask = {}     # code -> {'total_ask', 'total_bid', 'ts'}
        self._cache_lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._loop = None
        self._exec_callbacks = []
        self._cb_lock = threading.RLock()
        self._was_enabled = None    # USE_WEBSOCKET 토글 전환 로깅용
        self._got_event = False     # 연결당 첫 주문 이벤트 로깅용
        self._got_quote = False     # 연결당 첫 시세 프레임 로깅용
        self._account_seq = None    # 구독 중인 accountSeq(문자열)
        self._subscribed = False    # 주문 채널이 ack 로 확정됐는가
        self._declared = ()         # 마지막으로 선언한 구독(재선언 여부 판단용)
        self._quote_codes = frozenset()   # ack 로 확정된 시세 종목

    # ---- 라이프사이클 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="TossWsFeed")
        self._thread.start()
        logger.info("[TossWS] 주문 이벤트 피드 시작")

    def stop(self):
        self._stop.set()

    # ---- 읽기 API (읽기 경로가 호출) ----
    def _fresh(self, entry, max_age):
        return entry is not None and (time.time() - entry['ts']) <= max_age

    def get_price(self, code, max_age=3.0):
        with self._cache_lock:
            e = self._price.get(code)
            if self._fresh(e, max_age) and e['price'] > 0:
                return e['price']
        return None

    def get_orderbook(self, code, max_age=3.0):
        with self._cache_lock:
            e = self._ask.get(code)
            if self._fresh(e, max_age):
                return {'total_ask': e['total_ask'], 'total_bid': e['total_bid']}
        return None

    #  get_vol_strength 는 정의하지 않는다(기본 None). 토스는 체결강도를 REST 에서도
    #  웹소켓에서도 주지 않는다 — 여기서 무언가 돌려주면 그것은 지어낸 값이다.

    def set_symbols(self, priority, other=None):
        self.manager.set_symbols(priority, other)

    def coverage(self):
        """커버리지 요약. 시세 구독이 꺼져 있으면 그 항목은 0 이 아니라 None 이다.

        '0종목 커버'라고 적으면 KIS 와 같은 표에서 '피드가 죽었다'로 읽힌다. 재는 축이
        아닐 때와 재 봤더니 0 일 때는 다른 사실이다(unknown-vs-empty).
        """
        base = {
            'order_subscribed': self._subscribed,
            'account_seq': self._account_seq,
        }
        if not self._quotes_enabled():
            base.update(priority=None, capacity=None, price_covered=None,
                        ob_covered=None, rest_fallback=None)
            return base
        cov = self.manager.coverage()
        cov.update(base)
        return cov

    # ---- 주문 이벤트 콜백 (KIS 체결통보와 같은 등록 규약) ----
    def register_exec_callback(self, fn):
        with self._cb_lock:
            if fn not in self._exec_callbacks:
                self._exec_callbacks.append(fn)

    def _invoke_exec_callbacks(self, notice):
        with self._cb_lock:
            callbacks = list(self._exec_callbacks)
        for fn in callbacks:
            try:
                fn(notice)
            except Exception as e:      # noqa: BLE001 - 콜백 하나가 피드를 죽이지 않는다
                logger.debug(f"[TossWS] 주문 이벤트 콜백 오류: {e}")

    @staticmethod
    def _enabled():
        return getattr(config, 'USE_WEBSOCKET', True)

    def _log_toggle(self, enabled):
        if self._was_enabled is None or self._was_enabled != enabled:
            logger.info("[TossWS] USE_WEBSOCKET 켜짐 → 연결 시작" if enabled
                        else "[TossWS] USE_WEBSOCKET 꺼짐 → 연결 안 함(REST 폴백)")
            self._was_enabled = enabled

    # ---- 내부: 스레드/이벤트루프 ----
    def _thread_main(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run())
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[TossWS] 이벤트루프 종료: {e}")

    def _resolve_account_seq(self):
        """구독 대상 accountSeq 를 구한다(실패 시 None).

        종목 코드가 아니라 계좌 순번이고, 값은 숫자지만 codes 에는 **문자열**로 넣는다.
        """
        try:
            from brokers import toss_api
            seq = toss_api.resolve_account_seq()
            return str(seq) if seq is not None else None
        except Exception as e:      # noqa: BLE001
            logger.info(f"[TossWS] accountSeq 확인 실패(REST 폴링 유지): {e}")
            return None

    def _fetch_token(self):
        try:
            from brokers import toss_api
            return toss_api.get_access_token()
        except Exception as e:      # noqa: BLE001
            logger.info(f"[TossWS] 토큰 발급 실패(REST 폴링 유지): {e}")
            return None

    async def _run(self):
        try:
            import websockets
        except ImportError:
            logger.info("[TossWS] websockets 미설치 → 주문 이벤트 구독 없음(REST 폴링 유지)")
            return
        attempt = 0
        while not self._stop.is_set():
            if not self._enabled():
                self._log_toggle(False)
                await asyncio.sleep(2)
                continue
            self._log_toggle(True)
            connected = False
            try:
                token = await self._loop.run_in_executor(None, self._fetch_token)
                seq = await self._loop.run_in_executor(None, self._resolve_account_seq)
                if not token or not seq:
                    raise RuntimeError("토큰 또는 accountSeq 없음")
                uri = getattr(config, 'TOSS_WS_URI', 'wss://openapi-ws.tossinvest.com/ws/v1')
                #  websockets 14 에서 extra_headers → additional_headers 로 이름이 바뀌었다.
                #  requirements 는 >=12 를 허용하므로 둘 다 시도한다.
                try:
                    conn = websockets.connect(
                        uri, additional_headers={"Authorization": f"Bearer {token}"},
                        ping_interval=None, max_size=None)
                except TypeError:
                    conn = websockets.connect(
                        uri, extra_headers={"Authorization": f"Bearer {token}"},
                        ping_interval=None, max_size=None)
                async with conn as ws:
                    connected = True
                    attempt = 0        # 성공했으니 백오프를 처음으로 되돌린다
                    self._got_event = False
                    self._got_quote = False
                    self._subscribed = False
                    self._quote_codes = frozenset()
                    #  새 연결에는 아무 구독도 없다. 장부를 비우지 않으면 '이미 선언했다'고
                    #  판단해 한 건도 보내지 않은 채 조용히 아무것도 안 받는다.
                    self._declared = ()
                    self._account_seq = seq
                    logger.info(f"[TossWS] 연결 성공 (accountSeq={seq})")
                    await self._declare(ws, seq, force=True)
                    #  [재동기화] 무손실 보장은 연결 세션 안에서만이다. 끊겨 있던 동안의
                    #   이벤트는 다시 오지 않으므로, 붙자마자 한 번 깨워 REST 로 주문 상태를
                    #   맞추게 한다(이 피드가 직접 조회하지 않는다).
                    self._invoke_exec_callbacks(self._resync_notice())
                    pinger = asyncio.ensure_future(self._ping_loop(ws))
                    watcher = asyncio.ensure_future(self._disable_watcher(ws))
                    rotator = asyncio.ensure_future(self._rotate_loop(ws, seq))
                    try:
                        async for msg in ws:
                            self._on_message(msg)
                            if self._stop.is_set() or not self._enabled():
                                break
                    finally:
                        pinger.cancel()
                        watcher.cancel()
                        rotator.cancel()
                logger.info("[TossWS] 연결 종료")
            except Exception as e:      # noqa: BLE001
                logger.info(f"[TossWS] 연결 오류: {e}")
            finally:
                #  끊긴 뒤에도 캐시가 남아 있으면 읽기 경로가 '신선하다'고 믿고 낡은 값을
                #  쓴다(TTL 3초 안이면 통과한다). 연결이 없으면 REST 로 가야 한다.
                self._subscribed = False
                self._declared = ()
                self._quote_codes = frozenset()
                with self._cache_lock:
                    self._price.clear()
                    self._ask.clear()
            if self._stop.is_set():
                break
            #  [백오프] 스펙이 정한 1s → 2s → 4s … + jitter. 즉시 재연결하면, 같은 앱키를
            #   쓰는 다른 기기와 서로를 밀어내는 무한 루프가 된다(동시 연결 계정당 2개).
            attempt = 0 if connected else attempt + 1
            cap = getattr(config, 'TOSS_WS_MAX_BACKOFF_SEC', 60)
            delay = min(cap, 2 ** min(attempt, 10)) * (0.5 + random.random() * 0.5)
            await asyncio.sleep(max(1.0, delay))

    @staticmethod
    def _resync_notice():
        """재연결 직후 한 번 보내는 '재동기화하라' 통보.

        rejected=False 여야 한다 — 콜백은 거부 통보를 무시하도록 되어 있어서, True 면
        이 깨우기가 통째로 사라진다(그리고 아무도 모른다).
        """
        return {'source': 'toss-ws', 'event': 'RECONNECT', 'resync': True,
                'rejected': False, 'is_fill': False}

    @staticmethod
    def _quotes_enabled():
        return bool(getattr(config, 'TOSS_WS_SUBSCRIBE_QUOTES', True))

    def _plan(self, seq):
        """지금 선언해야 할 구독 목록을 만든다.

        **full-replace 라 '전체'를 만들어야 한다.** KIS 처럼 '추가할 것'만 담으면 담지 않은
        구독이 해제된다 — 주문 채널까지 함께 날아간다.
        """
        entries = [("personal:order", (seq,))]
        if self._quotes_enabled():
            trades, books = [], []
            for tr_id, code in self.manager.plan():
                (trades if tr_id == TR_PRICE else books).append(code)
            if trades:
                entries.append(("trade:kr", tuple(trades)))
            if books:
                entries.append(("orderbook:kr", tuple(books)))
        return tuple(entries)

    async def _declare(self, ws, seq, force=False):
        """구독을 선언한다. **full-replace** 라 배열 하나가 곧 구독 전체다.

        선언 빈도 한도가 5회/초라 내용이 그대로면 보내지 않는다(로테이션 주기가 30초라
        평상시엔 문제가 없지만, 한도를 넘기면 rate-limit-exceeded 로 선언 자체가 실패한다).
        """
        entries = self._plan(seq)
        if not force and entries == self._declared:
            return False
        payload = [{"id": f"sub-{int(time.time() * 1000)}"}]
        payload += [{"type": t, "codes": list(codes)} for t, codes in entries]
        await ws.send(json.dumps(payload))
        self._declared = entries
        counts = " · ".join(f"{t} {len(c)}건" for t, c in entries)
        logger.info(f"[TossWS] 구독 선언: {counts}")
        return True

    async def _rotate_loop(self, ws, seq):
        """관심종목 로테이션 주기마다 구독을 다시 선언한다(내용이 바뀐 때만)."""
        interval = getattr(config, 'WS_ROTATE_INTERVAL_SEC', 30)
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval)
                if not self._quotes_enabled():
                    continue
                self.manager.advance()
                await self._declare(ws, seq)
        except asyncio.CancelledError:
            pass

    async def _ping_loop(self, ws):
        """텍스트 `PING` 을 주기적으로 보낸다.

        서버는 **클라이언트로부터의 수신**이 180초 없으면 끊는다. 서버가 보내는 데이터는
        이 타이머를 리셋하지 않으므로, 이벤트를 받는 중에도 계속 보내야 한다.
        JSON 으로 감싸면 안 된다 — 따옴표 없는 대문자 4글자 그대로다.
        """
        interval = getattr(config, 'TOSS_WS_PING_INTERVAL_SEC', 60)
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[TossWS] PING 실패(연결이 곧 끊긴다): {e}")

    async def _disable_watcher(self, ws):
        """USE_WEBSOCKET 이 꺼지거나 중지되면 소켓을 닫아 수신 루프를 즉시 끝낸다."""
        try:
            while True:
                await asyncio.sleep(1)
                if self._stop.is_set() or not self._enabled():
                    if not self._enabled():
                        logger.info("[TossWS] 토글 OFF 감지 → 연결 해제(REST 폴백)")
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    def _on_message(self, msg):
        """수신 프레임을 top-level `type` 으로 갈라 처리한다.

        한 연결로 ack·데이터·에러·pong 이 섞여 온다. 모르는 type 은 조용히 버리되,
        **구독 거부(rejected)만은 반드시 남긴다** — 삼키면 그 계좌의 주문 이벤트가
        영영 오지 않는데 로그에는 아무 흔적이 없다(무증상 폴백).
        """
        try:
            if not msg:
                return
            if isinstance(msg, bytes):
                msg = msg.decode('utf-8', 'ignore')
            text = msg.strip()
            if not text or text[0] not in '{[':
                return          # 텍스트 프레임(서버가 보내는 것은 없다)
            data = json.loads(text)
            kind = data.get('type')
            if kind == 'pong':
                return
            if kind == 'subscriptions':
                self._on_ack(data)
                return
            if kind == 'error':
                self._on_error(data)
                return
            if kind == 'message':
                self._on_data_frame(data)
                return
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[TossWS] 메시지 처리 오류: {e}")

    def _on_ack(self, data):
        subscribed = [str(x) for x in (data.get('subscribed') or [])]
        rejected = data.get('rejected') or []
        #  주문 채널과 시세 채널을 갈라서 센다. 하나로 뭉쳐 세면 시세가 다 붙었다는
        #  이유로 '구독 정상'이 되고, 정작 주문 이벤트가 거부된 사실이 묻힌다.
        self._subscribed = any(t.startswith('personal:order') for t in subscribed)
        self._quote_codes = frozenset(
            t.rsplit(':', 1)[-1] for t in subscribed
            if t.startswith('trade:') or t.startswith('orderbook:'))
        if subscribed:
            logger.info(f"[TossWS] 구독 확정 {len(subscribed)}건 "
                        f"(주문 {'O' if self._subscribed else 'X'} · 시세 {len(self._quote_codes)}종목)")
        for r in rejected:
            #  스펙: 원인을 고치기 전에는 재선언해도 같은 이유로 다시 거부된다.
            #  그러니 재시도 루프를 돌리지 않고 사실만 남긴다(REST 폴링이 계속 잡는다).
            #  [흔한 원인] trade:kr 의 codes 는 스펙상 '6자리 숫자'다. 0080G0 같은 신형
            #   우선주는 stock-not-found 로 거부될 수 있다 — 그 종목만 REST 로 돈다.
            logger.warning(f"[TossWS] 구독 거부: {r.get('target')} — "
                           f"{r.get('code')} {r.get('message')} (REST 폴링 유지)")
        if not self._subscribed:
            logger.warning("[TossWS] 주문 이벤트 구독이 확정되지 않았습니다 — "
                           "체결 인지는 REST 폴링에만 의존합니다.")

    def _on_error(self, data):
        err = data.get('error') or {}
        code = err.get('code')
        msg = err.get('message')
        if code == 'server-shutdown':
            #  프레임 직후 연결이 끊긴다. 백오프 루프가 재연결하고 다시 선언한다.
            logger.info(f"[TossWS] 서버 배포 알림({code}) → 재연결 대기: {msg}")
            return
        logger.warning(f"[TossWS] 에러 프레임: {code} — {msg} (기존 구독은 유지된다)")

    #  토스 수치는 전부 문자열(decimal)이다. 못 읽으면 0 이 아니라 None 이어야 한다 —
    #  0 은 '값이 0'이라는 사실이고, 못 읽은 것은 사실이 없는 것이다.
    @staticmethod
    def _num(v):
        try:
            return float(str(v).strip().replace(',', ''))
        except (TypeError, ValueError, AttributeError):
            return None

    def _on_trade_frame(self, topic, payload):
        """실시간 체결 → 현재가 캐시.

        **체결가만 담는다.** 누적 거래량은 프레임에 없고, 스펙이 수신 합산으로 재구성하지
        말라고 명시한다(LOSSY · sequence 없음). 매수/매도 구분과 체결강도도 없다.
        KIS 캐시에는 그 자리가 있어 0 으로 채우기 쉬운데, 그러면 '못 잰 값'이 '0 거래'가 된다.
        """
        code = topic.rsplit(':', 1)[-1]
        price = self._num(payload.get('price'))
        if price is None or price <= 0:
            return
        with self._cache_lock:
            self._price[code] = {'price': price, 'ts': time.time()}
        if not self._got_quote:
            self._got_quote = True
            logger.info("[TossWS] 시세 수신 시작")

    def _on_orderbook_frame(self, topic, payload):
        """실시간 호가 → 총잔량 캐시.

        [REST 와 같은 셈] api/toss.py 의 _toss_order_book 은 **상위 10호가만** 더해
        total_askp_rsqn/total_bidp_rsqn 를 만든다. 여기서 전 호가를 더하면 같은 순간에도
        WS 경로와 REST 경로가 다른 비율을 내놓는다 — 매수 게이트가 '어느 경로가 먼저
        답했는가'에 따라 갈리게 된다. 그래서 10단으로 맞춘다.
        """
        code = topic.rsplit(':', 1)[-1]

        def _total(rows):
            tot = 0
            for row in (rows or [])[:10]:
                v = self._num((row or {}).get('volume'))
                if v is not None:
                    tot += v
            return tot

        with self._cache_lock:
            self._ask[code] = {'total_ask': _total(payload.get('asks')),
                               'total_bid': _total(payload.get('bids')),
                               'ts': time.time()}
        if not self._got_quote:
            self._got_quote = True
            logger.info("[TossWS] 시세 수신 시작")

    def _on_data_frame(self, data):
        """`type: message` 프레임을 topic 접두어로 갈라 보낸다."""
        topic = str(data.get('topic') or '')
        payload = data.get('data') or {}
        if topic.startswith('personal:order'):
            self._on_order_frame(topic, payload)
        elif topic.startswith('trade:'):
            self._on_trade_frame(topic, payload)
        elif topic.startswith('orderbook:'):
            self._on_orderbook_frame(topic, payload)

    def _on_order_frame(self, topic, payload):
        """주문 이벤트를 통보 dict 로 바꿔 콜백에 넘긴다(내용은 신뢰하지 않는다)."""
        order = payload.get('order') or {}
        event = payload.get('event')
        if not self._got_event:
            self._got_event = True
            logger.info("[TossWS] 주문 이벤트 수신 시작")
        #  [unknown enum] 스펙이 "클라이언트는 unknown code 를 허용하도록 구현" 하라고
        #   적는다. 모르는 event 도 버리지 않고 깨운다 — 모르는 상태 변화일수록 REST 로
        #   확인해야 한다. 다만 '거부'만은 KIS 규약대로 표시해 폴링의 미체결 정리에 맡긴다.
        rejected = event in ('REJECTED', 'CANCEL_REJECTED', 'REPLACE_REJECTED')
        notice = {
            'source': 'toss-ws',
            'event': event,
            'acnt_seq': payload.get('accountSeq'),
            'odno': order.get('orderId'),
            'code': order.get('symbol'),
            #  KIS 의 '01'=매도 / '02'=매수 표기에 맞춘다(콜백이 같은 키를 읽는다).
            'buy_sell': {'SELL': '01', 'BUY': '02'}.get(order.get('side')),
            'rejected': rejected,
            'is_fill': event in ('FILL', 'PARTIAL_FILL'),
        }
        logger.info(f"[TossWS] 주문 이벤트 {event}: {notice['code']} (주문 {notice['odno']})")
        self._invoke_exec_callbacks(notice)


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
        """체결통보(HTS ID 키)를 구독한다. 이미 됐으면 아무것도 하지 않는다(재호출 안전).

        실패해도 체결 확정 자체는 멀쩡하다 — 이 통보는 ConclusionMonitor 를 **깨우는**
        용도이고, 못 깨워도 그쪽 주기 폴링이 같은 체결을 잡는다(완전 폴백).

        [Fix 2026-09-08] 다만 **등록 슬롯은 돌려받아야 한다.** 호출부가 구독을 시도하기
         전에 `set_reserved(1)` 로 41건 중 한 자리를 체결통보 몫으로 떼어 두는데, 종전에는
         send 가 실패해도 그 예약이 그대로 남았다. 구독은 안 됐는데 자리만 비워 둔 꼴이라
         시세 커버리지가 한 종목 줄어든 채 **그 연결이 끊길 때까지** 회복되지 않았다
         (재구독을 시도하는 곳도 연결 직후 한 번뿐이었다).
         실패하면 자리를 반납하고, 다음 재조정 주기가 다시 시도한다.
        """
        if not self._exec_enabled():
            self.manager.set_reserved(0)
            return
        if self._exec_subscribed:
            return
        try:
            await ws.send(self._sub_msg(approval, self._exec_tr_id(), self._hts_id(), subscribe=True))
            self._exec_subscribed = True
            self.manager.set_reserved(1)
            logger.info(f"[WS] 체결통보 구독 요청(tr={self._exec_tr_id()}, id=…{self._hts_id()[-3:]})")
        except Exception as e:
            self.manager.set_reserved(0)    # 못 쓴 자리는 시세에 돌려준다
            logger.info(f"[WS] 체결통보 구독 실패(REST 폴백 유지, 등록 슬롯 반납 — "
                        f"다음 주기에 재시도): {e}")

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
                    #  체결통보를 먼저 구독한다. 등록 슬롯 예약(시세 종목 용량에서 1건 제외)은
                    #   _subscribe_exec 가 **성공 여부에 맞춰** 잡고 푼다 — 예약을 여기서
                    #   따로 걸면 구독이 실패했을 때 자리만 비워 둔 채로 남는다(SSOT).
                    await self._subscribe_exec(ws, approval)
                    self._warn_if_orderbook_inert()   # 예약이 확정된 뒤의 용량으로 센다
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

    def _warn_if_orderbook_inert(self):
        """호가 구독을 켜 뒀는데 실제로 0건이면 한 번 알린다.

        [왜 · 2026-09-05] 등록 예산을 현재가에 전부 주므로 호가는 '구독 대상 < 용량'일
         때만 붙는다. 관심종목이 용량을 넘는 평상시에는 **한 건도 안 붙는데**, 설정
         주석은 "종목당 호가 REST 1콜을 절감한다"고 약속한다. 켜 뒀으니 되고 있다고
         읽히는 자리라, 실제로 0건이면 그 사실을 남긴다(연결당 한 번).
        """
        try:
            if not self.manager.subscribe_orderbook:
                return
            cov = self.manager.coverage()
            if cov.get('ob_covered', 0) > 0:
                return
            total = cov.get('priority', 0) + len(self.manager._other)
            logger.info(
                f"[WS] 호가 구독 0건 — 구독 대상 {total}종목이 용량 {cov.get('capacity', 0)}종목을 "
                f"넘어 현재가에 슬롯을 모두 씁니다. WS_SUBSCRIBE_ORDERBOOK 의 REST 절감 효과는 "
                f"이 상태에서 없습니다(현재가 커버리지 우선).")
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[WS] 호가 커버리지 점검 실패: {e}")

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
                #  체결통보 구독이 연결 직후 한 번 실패하면 종전에는 재연결까지 그대로였다.
                #  이미 됐으면 즉시 돌아오므로(_exec_subscribed) 매 주기 물어도 싸다.
                await self._subscribe_exec(ws, approval)
                self.manager.advance()  # 관심종목 로테이션
                await self._reconcile(ws, approval)
        except asyncio.CancelledError:
            pass

    async def _reconcile(self, ws, approval):
        """원하는 구독 집합(plan)과 현재 구독을 비교해 sub/unsub 메시지를 보낸다."""
        desired = set(self.manager.plan())
        to_add = desired - self._subscribed
        to_remove = self._subscribed - desired
        #  [Fix 2026-09-05] 종전에는 send 가 중간에 실패해도 `self._subscribed = desired` 로
        #   **전부 됐다고 기록**했다. 그러면 다음 주기의 to_add 가 비어 재시도가 없다 —
        #   못 보낸 종목은 그 연결이 끊길 때까지 영영 구독되지 않는데 장부는 구독 중이라고
        #   말한다. 실제로 보낸 것만 적는다(다음 주기가 차이를 다시 메운다).
        applied = set(self._subscribed)
        for tr_id, code in to_remove:
            try:
                await ws.send(self._sub_msg(approval, tr_id, code, subscribe=False))
                await asyncio.sleep(0.02)
            except Exception:
                break
            applied.discard((tr_id, code))
        for tr_id, code in to_add:
            try:
                await ws.send(self._sub_msg(approval, tr_id, code, subscribe=True))
                await asyncio.sleep(0.02)  # 구독 폭주 완화
            except Exception:
                break
            applied.add((tr_id, code))
        prev_count = len(self._subscribed)
        self._subscribed = applied
        desired = applied          # 아래 로그가 '보낸 것'을 세도록
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
                _feed = TossWsFeed()
            else:
                _feed = KisRealtimeFeed()
        return _feed


def start_feed():
    """현재 모드의 실시간 피드 스레드를 시작한다.

    USE_WEBSOCKET이 꺼져 있어도 스레드는 떠 있되 연결 없이 유휴 대기한다. 메뉴 0에서 토글을
    켜면 프로그램 재시작 없이 자동으로 연결을 시작하고, 끄면 연결을 해제하고 REST로 폴백한다.

    [2026-09-09] 토스(mode 3)도 이제 시작한다 — 주문 이벤트 웹소켓이 열렸다. 시세는
     구독하지 않으므로 현재가·호가 읽기 경로는 종전 그대로 REST 폴링이다.
    """
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
    """주문 체결 통보 도착 시 호출할 콜백을 등록한다.

    KIS는 체결통보(H0STCNI0/9), 토스는 주문 이벤트(personal:order)가 같은 자리에 붙는다.
    메서드가 없는 피드(TossPollingFeed 등)에서는 조용히 무시 → REST 폴링이 체결을 처리한다.
    """
    try:
        feed = get_feed()
        if hasattr(feed, 'register_exec_callback'):
            feed.register_exec_callback(fn)
    except Exception as e:
        logger.debug(f"[WS] 체결통보 콜백 등록 오류: {e}")
