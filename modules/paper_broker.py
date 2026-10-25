"""관찰(페이퍼 트레이딩) 모드 가상 브로커.

[왜 필요한가] 모의투자 계좌는 3개월마다 리셋되어 장기 관찰이 불가능하고, 그 이전에
표본 자체가 부족하다(연 청산 30건 · 승률 18% → 3개월이면 7~8건으로 판별 불가).
반면 검증이 실제로 필요한 것은 전략이 아니라 **배관**이다 — 시세 수신, 스코어링, 필터,
청산 판정, 재기동 복원, 알림. 이 모듈은 주문 실행만 가상으로 돌려 그 전 과정을
실계좌 환경에서 자본 노출 없이 검증하게 한다.

[설계] api 층에서 잔고·예수금·주문을 가로채므로(api.get_domestic_balance 등),
기존 25개 호출부와 모든 화면·텔레그램·스케줄러가 수정 없이 가상 포트폴리오를 본다.
저장소는 실계좌 DB와 **파일 자체를 분리**한다(config.PAPER_DB_FILE_PATH) —
trailing_stops·half_tp_status가 code를 PK로 쓰기 때문에 같은 파일을 공유하면
실계좌 포지션의 트레일링 최고가가 페이퍼 포지션에 오염된다.

[체결 모델] 트레이더가 실시간 호가로 정한 주문가에 **즉시 전량 체결**로 가정하고
수수료·거래세만 차감한다. 미체결·부분체결은 기본적으로 재현하지 않는다. 주문 상태기계를
시험할 때만 '시험 모델'(config.PAPER_FAULT_MODEL, 기본 꺼짐 — 아래 절)을 켜 거절·지연·
부분체결·취소 경합을 시드로 재현한다. 시험 모델은 현실 시장의 재현이 아니다.
백테스트가 종가 기준이라 청산일 하락갭(실측 평균 -1.44%)만큼 낙관 편향이 있는 반면,
이 모드는 실제 장중 판단 시점 가격으로 체결되므로 그 편향이 없다.
"""
import json
import logging
import random
import threading
from datetime import datetime

import config
from core import utils
from core import trading_cost

logger = logging.getLogger(__name__)

_lock = threading.RLock()

# 가상 주문번호 일련번호. place_order 는 _lock 안에서만 돌므로 보호가 따로 필요 없다.
_odno_seq = 0


def _new_odno(code):
    """가상 주문번호를 만든다. **같은 프로세스 안에서 절대 겹치지 않아야 한다.**

    [왜] 종전 형식은 `P{초단위시각}{코드끝2자리}` 였다. 매도 워커는 4스레드 병렬이라,
    급락으로 손절이 한꺼번에 나가면 같은 초에 두 주문이 생긴다. 코드 끝 2자리까지 같으면
    (44종목 유니버스에서 드물지 않다) 주문번호가 충돌한다. 그 뒤가 나쁘다 —
    get_fill_by_odno 도 get_trade_by_odno 도 주문번호 하나로만 찾으므로,
    _apply_paper_fill 이 **다른 종목의 체결(수량·단가·손익)** 을 이 종목에 반영한다.

    초 단위 시각에 마이크로초와 일련번호를 붙여 없앤다. 마이크로초는 재기동으로 일련번호가
    0으로 돌아가도 겹치지 않게 하고, 일련번호는 같은 마이크로초에 두 건이 들어오는 경우를
    막는다. 코드 끝 2자리는 로그에서 눈으로 짚기 위해 남긴다.
    """
    global _odno_seq
    _odno_seq = (_odno_seq + 1) % 1000
    return (f"P{datetime.now().strftime('%y%m%d%H%M%S%f')}"
            f"{str(code)[-2:]}{_odno_seq:03d}")

# 체결 비용·슬리피지는 config가 단일 소스다(modules/trading_cost 참조).
# 종전에는 이 파일이 요율을 따로 들고 있었고 슬리피지는 아예 없어서, 같은 전략이라도
# 백테스트와 관찰모드의 성과를 직접 비교할 수 없었다(2026-08-10 정합화).
# 아래 두 이름은 기존 호출부·테스트 호환을 위한 별칭이다.
BUY_FEE_RATE = config.BUY_FEE_RATE
SELL_FEE_RATE = config.SELL_FEE_RATE


def is_active():
    """현재 세션이 관찰 모드인가."""
    return bool(getattr(config.session, 'is_paper', False))


def _db():
    from modules import db_manager
    return db_manager.db


def init_tables():
    """가상 포트폴리오 테이블 생성. 세션이 페이퍼 DB로 전환된 뒤 호출한다."""
    conn = _db().get_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS paper_state (
                       key TEXT PRIMARY KEY, value TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS paper_positions (
                       code TEXT PRIMARY KEY, name TEXT, qty INTEGER,
                       avg_price REAL, first_buy_at TEXT, last_buy_at TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS paper_fills (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, type TEXT,
                       code TEXT, name TEXT, qty INTEGER, price REAL,
                       amount REAL, fee REAL, profit_amt REAL, profit_rate REAL,
                       odno TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS paper_equity (
                       date TEXT PRIMARY KEY, cash REAL, stock_value REAL, total REAL)''')
    # [시험 모델] 지연·부분체결 주문의 상태. 즉시 체결(기본)에서는 한 행도 생기지 않는다.
    cur.execute('''CREATE TABLE IF NOT EXISTS paper_orders (
                       odno TEXT PRIMARY KEY, code TEXT, name TEXT, side TEXT,
                       qty INTEGER, price REAL, filled_qty INTEGER DEFAULT 0,
                       fill_amount REAL DEFAULT 0, status TEXT, created_at TEXT,
                       updated_at TEXT, last_fill_at TEXT, schedule TEXT,
                       next_stage INTEGER DEFAULT 0, race INTEGER DEFAULT 0, note TEXT)''')
    # [마이그레이션] 스냅샷 시점의 시드(누적 투입원금). 누적 수익률의 분모다.
    #  현재 시드로 과거 행을 나누면 입출금 전 구간의 수익률이 왜곡된다 — 같은 계열의 사고를
    #  daily_asset_history 에서 겪었다([[daily-asset-baseline-transfers]]). 분모는 그 시점 값을
    #  함께 굳혀야 한다. 옛 행은 NULL로 남고, 표시부가 '현재 시드로 근사'임을 밝힌다.
    cols = {r[1] for r in cur.execute("PRAGMA table_info(paper_equity)")}
    if "seed" not in cols:
        cur.execute("ALTER TABLE paper_equity ADD COLUMN seed REAL")
        conn.commit()
        logger.info("[PAPER] paper_equity.seed 컬럼 추가")
    conn.commit()

    seed = int(getattr(config, 'PAPER_SEED_CAPITAL', 10_000_000))
    #  strict — '못 읽었다'가 '계좌가 아직 없다'로 읽히면 운용 중인 계좌를 다시 연다.
    #  여기서 던지면 관찰 모드 기동이 멈춘다. 그게 맞다 — 기동 실패는 외부 감시자가
    #  알리지만([[process-death-watchdog]]), 조용히 초기화된 원장은 아무도 모른다.
    if _get_state('seed', strict=True) is None:
        _set_state('seed', seed)
        _set_state('cash', seed)
        _set_state('started_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        logger.info(f"[PAPER] 가상 계좌 개설: 시드 {seed:,}원")


def _get_state(key, default=None, strict=False):
    """가상 계좌 상태값 하나를 읽는다.

    [왜 strict 가 필요한가 · 2026-09-06] 종전에는 예외를 통째로 삼키고 default 를
     돌려줬다. 그래서 **조회 실패가 '그 값이 없다'로 읽혔고**, 그 값이 곧바로 원장에
     다시 써졌다([[unknown-vs-empty]]).
       · 매도 : new_cash = get_cash() + 매도대금 → 읽기가 실패하면 0 + 매도대금.
                예수금 잔액 전체가 이 한 건의 매도대금으로 **덮인다**.
                실측(50주 매수 후 매도, DB 잠금 1회): 6,499,381 → 3,841,619
                (정상 10,349,381, 차이 -6,507,762). 그런데 주문 응답은 rt_cd '0' 이다.
       · 개설 : init_tables 의 `_get_state('seed') is None` 이 참이 되어 **계좌를 다시
                연다** — 포지션은 그대로 남은 채 현금만 시드로 복구된다.
                실측(재기동 시 잠금 1회): 예수금 6,499,381 → 10,000,000,
                50주 포지션 유지 = 없던 자산 3.5백만이 생긴다.
       · 입출금 : adjust_seed 도 cash·seed 를 읽어 더한 값을 쓴다(같은 자리).

     가상 계좌의 자산곡선은 드로다운 → 리스크 한도 → 매매 강도로 이어진다. 어긋난
     원장은 관찰 결과 자체를 못 쓰게 만든다([[daily-asset-baseline-transfers]] 와 같은 계열).

     표시 경로(성과 화면·상태 요약)는 못 읽어도 화면이 죽으면 안 되므로 종전대로
     default 로 넘어간다 — 다만 이제는 **조용하지 않다**. 원장을 고쳐 쓰는 자리만
     strict=True 로 받아 실패를 실패로 다룬다.
    """
    try:
        row = _db().execute_query("SELECT value FROM paper_state WHERE key=?", (key,), fetch='one')
        return json.loads(row[0]) if row else default
    except Exception as e:      # noqa: BLE001
        logger.error(f"[PAPER] 가상 계좌 상태 '{key}' 조회 실패 — "
                     f"{'주문/기록을 보류합니다' if strict else f'표시에 기본값({default!r})을 씁니다'}: "
                     f"{type(e).__name__}: {e}")
        if strict:
            raise
        return default


def _set_state(key, value):
    _db().execute_query("INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)",
                        (key, json.dumps(value)))


def get_cash(strict=False):
    """가상 예수금. strict=True 면 조회 실패를 0원으로 답하지 않고 던진다."""
    return float(_get_state('cash', 0, strict=strict) or 0)


def get_seed(strict=False):
    """가상 시드(누적 투입원금). strict=True 면 조회 실패를 설정 기본값으로 덮지 않는다."""
    return float(_get_state('seed', getattr(config, 'PAPER_SEED_CAPITAL', 10_000_000),
                            strict=strict))


def get_positions():
    """[{code, name, qty, avg_price, first_buy_at, last_buy_at}, ...]"""
    rows = _db().execute_query(
        "SELECT code, name, qty, avg_price, first_buy_at, last_buy_at "
        "FROM paper_positions WHERE qty > 0 ORDER BY code", fetch='all') or []
    return [{"code": r[0], "name": r[1], "qty": int(r[2]), "avg_price": float(r[3]),
             "first_buy_at": r[4], "last_buy_at": r[5]} for r in rows]


def _current_price(code, fallback=0.0):
    """현재가 조회. 실패 시 fallback(보통 평단)을 써서 평가금이 0으로 무너지지 않게 한다."""
    try:
        import api
        price = api.get_current_price(code, False)
        if price and float(price) > 0:
            return float(price)
    except Exception as e:
        logger.debug(f"[PAPER] 현재가 조회 실패({code}): {e}")
    return float(fallback or 0.0)


# 마지막으로 조회에 성공한 현재가 {code: price}. 조회 실패 시 평단으로 되돌아가면
# 하락분이 장부에서 사라져 자산곡선·MDD가 실제보다 좋아 보이므로, 직전 정상가를 쓴다.
_last_prices = {}

# KRX 확정 종가 {code: (확정일 YYYYMMDD, 종가)}. 확정된 봉은 불변이라 하루 한 번만 구하면 된다.
_krx_closes = {}


def reset_price_cache():
    """테스트·계좌 전환용 — 마지막 정상가 캐시를 비운다."""
    with _lock:
        _last_prices.clear()
        _krx_closes.clear()


def _krx_settled_close(code):
    """평가에 쓸 KRX 확정 종가. KRX 정규장 중이거나 못 구하면 0.0(호출부가 실시간가로 폴백).

    [규칙] 자산 평가는 **정규장 중에는 현재가, 장 종료 후에는 KRX 확정 종가**다.
     정규장 밖의 실시간가는 NXT(대체거래소) 체결가인데, NXT 거래량은 정규장의 수백분의
     1이라 소수 체결이 계좌 전체의 평가액을 정한다. 그 값이 자산곡선·MDD·수익률에
     그대로 들어가면 '오늘 얼마를 벌었나'가 몇 건의 장외 체결로 흔들린다.
     (2026-08-30 20:00 실측: 페이퍼 주식평가 3,703,700 = NXT 최종가 / KRX 종가 3,744,000)

    [왜 여기서 거르나] api.get_current_price 는 ats_prpr(NXT)이 있으면 무조건 그것을
     돌려준다 — 주문가·손절 트리거는 언제나 실시간가여야 하므로 그 함수는 그대로가 맞다.
     걸러야 할 것은 **평가액**뿐이다.

    [게이트] chart_overlay_enabled(False) == KRX 정규장(09:00~15:30). 그 밖은 전부 확정
     종가를 쓴다 — NXT 프리(08:00~09:00)·KRX 애프터(16:00~20:00)도 포함하며,
     USE_KRX_CLOSE_AFTER_HOURS(표시 설정)와 무관하다. 평가 기준은 선택지가 아니다.

    [방어] 마지막 봉이 '확정된 세션'보다 오래됐으면(당일 봉 미수신) 쓰지 않는다.
     그대로 쓰면 지난 거래일 종가가 오늘 평가액이 된다(analysis 의 표시 경로와 같은 방어).
     15:30~15:40(종가 확정 여유) 사이가 이 구간이라, 그때는 종전대로 실시간가로 평가한다.
    """
    try:
        import api
        if api.chart_overlay_enabled(False):
            return 0.0          # KRX 정규장 — 현재가로 평가한다
        settled = api.krx_last_settled_day()
    except Exception as e:      # noqa: BLE001 - 판정 실패는 '고정하지 않음'(실시간가 유지)
        logger.debug(f"[PAPER] KRX 확정일 판정 실패({code}): {e}")
        return 0.0

    with _lock:
        hit = _krx_closes.get(code)
    if hit and hit[0] == settled:
        return hit[1]

    try:
        import api
        # realtime=False: 캐시 적중 시 현재가 오버레이를 생략한다(확정 종가를 덮지 않게).
        df = api.get_chart_data(code, False, 'daily', realtime=False)
        if df is None or df.empty:
            return 0.0
        last = df.iloc[-1]
        if str(last['date']).replace('-', '')[:8] < settled:
            return 0.0
        close = float(last['close'])
    except Exception as e:      # noqa: BLE001
        logger.debug(f"[PAPER] KRX 확정 종가 조회 실패({code}): {e}")
        return 0.0

    if close <= 0:
        return 0.0
    with _lock:
        _krx_closes[code] = (settled, close)
    return close


def _price_with_status(code, cost=0.0):
    """(가격, stale여부)를 돌려준다. stale이면 **매매 판정에 쓰면 안 되는 값**이다.

    조회에 실패했을 때 평단으로 폴백하면 prpr == pchs_avg_pric 이 되어 수익률이
    정확히 0.00%로 계산된다. 트레이더의 방어선은 `current_price <= 0` 하나뿐이라
    이 값이 그대로 통과하고, 실제로는 크게 하락한 포지션이 '본전에서 쉬는 정상
    포지션'으로 위장된다 — 손절도 트레일링도 발동하지 않고 로그에도 남지 않는다.
    KIS 레이트리밋(EGW00201)·토큰 만료 같은 일시적 실패는 실제로 관측된다.

    폴백 순서: 직전 정상가 → 평단(직전 정상가가 없을 때만). 어느 쪽이든 stale로 표시해
    판정에서 배제하되, 평가금이 0으로 무너지지는 않게 한다(자산 스냅샷 보존).

    [평가 기준] 정규장 밖에서는 KRX 확정 종가로 평가한다(_krx_settled_close).
     확정된 종가는 '판정 불가'가 아니므로 stale 이 아니다 — 마감 후 청산 신호 스캔은
     오히려 이 값으로 판정해야 종가와 어긋나지 않는다.
    """
    krx = _krx_settled_close(code)
    if krx > 0:
        with _lock:
            _last_prices[code] = krx
        return krx, False

    price = _current_price(code)
    if price > 0:
        with _lock:
            _last_prices[code] = price
        return price, False

    with _lock:
        last = _last_prices.get(code)
    if last and last > 0:
        return float(last), True
    return float(cost or 0.0), True


def valuation_price(code, fallback=0.0):
    """화면 표시용 평가가 — 총자산을 만드는 값과 **같은 규칙**으로 구한다.

    [왜 따로 두나] 성과 화면의 포지션 표가 _current_price 를 직접 불러 NXT 최종가를
     쓰고 있었다. 그래서 같은 화면 안에서 총자산(확정 종가)과 표 합계(NXT)가 40,400원
     어긋났고, 표의 수익률·손절 여유·오픈 리스크만 다른 기준이 됐다(2026-08-30 실측).
     표시 경로가 평가 규칙을 다시 쓰지 않도록 여기 한 줄로 모은다.

    판정용이 아니다 — stale 여부가 필요하면 _price_with_status 를 쓸 것.
    """
    price, _stale = _price_with_status(code, fallback)
    return price


# ==========================================================
# api 가로채기 대상 — KIS 응답과 같은 스키마로 반환한다
# ==========================================================
def get_domestic_balance():
    """api.get_domestic_balance 대체. (output1 보유목록, output2 요약) 형태."""
    with _lock:
        positions = get_positions()
        #  strict — 예수금을 못 읽으면 현금이 통째로 빠진 총자산이 '정상 숫자'로 나간다.
        #  실측(주식 3,500,000 · 현금 6,499,381, 잠금 1회): 총자산 9,999,381 → 3,500,000(-65.0%).
        #  실계좌 경로는 구간별 예외를 잡아 degraded 로 표시한다(account.get_asset_status_data).
        #  가상 계좌도 같은 기계를 타야 한다 — 그러려면 실패가 **예외로** 올라가야 한다.
        cash = get_cash(strict=True)
        open_sell = _open_sell_qty()   # [시험 모델] 미체결 매도 잔량(즉시 체결에서는 빈 dict)
        output1, total_eval, total_pchs = [], 0.0, 0.0
        for p in positions:
            price, price_stale = _price_with_status(p['code'], p['avg_price'])
            evlu = price * p['qty']
            pchs = p['avg_price'] * p['qty']
            pfls = evlu - pchs
            total_eval += evlu
            total_pchs += pchs
            output1.append({
                'pdno': p['code'],
                'prdt_name': p['name'] or p['code'],
                'hldg_qty': str(p['qty']),
                'ord_psbl_qty': str(max(0, p['qty'] - open_sell.get(p['code'], 0))),
                'pchs_avg_pric': f"{p['avg_price']:.4f}",
                'pchs_amt': str(int(pchs)),
                'prpr': str(int(price)),
                'evlu_amt': str(int(evlu)),
                'evlu_pfls_amt': str(int(pfls)),
                'evlu_pfls_rt': f"{(pfls / pchs * 100) if pchs else 0:.2f}",
                'fltt_rt': "0.00",
                '_paper': True,
                # 시세 조회 실패로 폴백한 값. 트레이더는 이 행을 '판정 불가'로 다룬다.
                '_price_stale': price_stale,
            })
        output2 = [{
            'dnca_tot_amt': str(int(cash)),
            'prvs_rcdl_excc_amt': str(int(cash)),
            'scts_evlu_amt': str(int(total_eval)),
            'tot_evlu_amt': str(int(cash + total_eval)),
            'pchs_amt_smtl_amt': str(int(total_pchs)),
            'pchs_amt_smtl': str(int(total_pchs)),
            'evlu_pfls_smtl_amt': str(int(total_eval - total_pchs)),
            'nass_amt': str(int(cash + total_eval)),
        }]
        return output1, output2


def get_deposit_balance():
    """api.get_deposit_balance 대체. 가상 현금은 즉시 결제로 본다(D+2 구분 없음).

    조회 실패는 '예수금 0원'이 아니다 — 던져서 호출부가 구간 실패로 다루게 한다.
    """
    cash = int(get_cash(strict=True))
    #  [시험 모델] 미체결 매수 잔량은 주문가능금액에서 뺀다(실계좌와 같다). 즉시 체결에서는 0.
    reserved = int(_reserved_cash())
    return {"deposit": cash, "foreign_deposit": 0, "withdraw": cash,
            "d2_deposit": cash, "order_possible": cash - reserved, "d2_real": cash}


def fill_price(price, action, market=False, is_etf=False):
    """가상 체결가 산출의 단일 지점. 지정가는 주문가 그대로, 시장가만 슬리피지+호가정렬.

    [왜 지정가에 얹지 않나] 지정가로 들어온 price는 호출부가 이미 현재가에 슬리피지를
     더해 만든 값이다(trader/예약주문/텔레그램 모두 adjust_to_tick(현재가 × (1 ±
     SLIPPAGE_RATE)) — main.py 도움말의 '체결 확률 확보 (현재가 + 슬리피지)'가 그 버퍼다).
     여기서 한 번 더 얹으면 편도 0.4%가 되어 백테스트(편도 0.2%)의 두 배를 문다.
     실측(2026-08-20): 현재가 1,188,000 → 주문 1,190,000 → 가상체결 1,192,380(+0.37%),
     백테스트 모델은 1,190,000(+0.17%). 왕복 0.4%p 초과 부담이라 관찰모드가 전략이
     아니라 비용 모델 때문에 뒤처진다 — mode 1의 존재 이유를 깨뜨린다.
    [2026-08-10 도입 당시의 오판] '지정가 그대로 체결이면 백테스트보다 유리하다'고 봤으나
     그 지정가가 이미 현재가×(1±0.002)라 백테스트 체결가와 같은 자리다.
    [호가 정렬] 시장가 체결가는 호가 단위에 맞춘다. 종전에는 맞추지 않아 101,796·
     1,192,380처럼 **실재할 수 없는 가격**이 원장에 남았다(백테스트는 정렬한다).
     ETF·ETN 은 격자가 다르다(2,000원 이상 5원 단일) — 주권 표로 맞추면 이번엔 반대로
     **실재하는 호가를 실재하지 않는 값으로 옮긴다**.
    """
    price = float(price)
    if not market:
        return price
    adj = trading_cost.apply_slippage(price, action)
    return float(utils.adjust_to_tick(adj, is_overseas=False, is_etf=is_etf) or adj)


def _market_open():
    """국내에 열린 시장이 있는가(api.domestic_trading_session_open). 테스트는 이 이름을 고정한다."""
    import api as _api_mod
    return bool(_api_mod.domestic_trading_session_open())


def _etf_untraded(code, name):
    """ETF/ETN 이 거래되지 않는 창(NXT 프리·휴게·KRX 애프터)에 ETF/ETN 을 주문했는가."""
    import api as _api_mod
    return bool(_api_mod.domestic_etf_untraded_window() and _api_mod.is_domestic_etf_etn(code, name))


def place_order(action, code, qty, price, name=None):
    """api.place_order 대체. 즉시 전량 체결로 처리하고 KIS 형식 응답을 만든다.

    시험 모델(config.PAPER_FAULT_MODEL)이 켜져 있으면 거절·지연·부분체결 시나리오를 따른다.
    """
    with _lock:
        try:
            qty = int(qty)
            price = float(price)
        except (TypeError, ValueError):
            return _fail("주문 수량/가격이 올바르지 않습니다")
        if qty <= 0:
            return _fail("주문 수량이 0입니다")

        if name is None:
            name = _lookup_name(code)

        #  [2026-09-16 · 애프터마켓 정합] 실계좌라면 거부될 주문을 가상계좌가 체결하면 관찰 결과가
        #   실전과 갈린다. 시장이 없는 시간(15:30~16:00 휴게·야간·휴장)과 KRX 애프터마켓의 ETF/ETN
        #   (애프터 미거래·값이 정규장 종가에 멈춰 있어 '그 값에 체결'은 가짜다)은 거절한다.
        #   판정 실패는 종전대로 체결한다(관찰 모드가 시계 오류로 멈추면 그것대로 사고다).
        try:
            if not _market_open():
                return _fail("지금은 열린 시장이 없어 체결하지 않습니다(휴게·야간·휴장)")
            if _etf_untraded(code, name):
                return _fail("ETF/ETN 은 NXT·KRX 애프터마켓에서 거래되지 않습니다")
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[PAPER] 세션 판정 실패({code}) — 종전대로 체결: {e}")

        is_market = price <= 0
        if is_market:  # 시장가 주문 → 현재가로 체결
            price = _current_price(code)
            if price <= 0:
                return _fail("현재가를 확인할 수 없어 체결 불가")

        if fault_model_enabled():
            return _place_with_faults(action, code, name, qty, price, is_market)

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        odno = _new_odno(code)

        #  ETF·ETN 은 호가 격자가 다르다. 판정 실패는 주권으로 본다(종전 동작).
        try:
            import api as _api_mod
            _is_etf = bool(_api_mod.is_domestic_etf_etn(code, name))
        except Exception as e:      # noqa: BLE001
            logger.debug(f"[PAPER] ETF 판정 실패({code}): {e}")
            _is_etf = False
        price = fill_price(price, action, market=is_market, is_etf=_is_etf)

        _booked = _book_fill(action, code, name, qty, price, odno, now)
        if _booked is not None:
            return _booked

        logger.info(f"[PAPER] {action.upper()} 체결 {code} {qty}주 @{price:,.0f} (No.{odno})")
        return {"rt_cd": "0", "msg_cd": "PAPER", "msg1": "가상투자 체결",
                "output": {"ODNO": odno, "ORD_TMD": datetime.now().strftime('%H%M%S')}}


def _book_fill(action, code, name, qty, price, odno, now):
    """체결 한 건을 가상 원장(예수금·포지션·체결 기록)에 한 트랜잭션으로 반영한다.

    성공이면 None, 실패면 _fail() 응답을 돌려준다. 즉시 체결(place_order)과 시험 모델의
    지연·분할 체결(_advance_orders)이 같은 원장 코드를 쓴다 — 두 벌이면 비용·손익 계산이
    갈라진다. 호출부가 _lock 을 잡고 부른다.
    """
    pos = _db().execute_query(
        "SELECT name, qty, avg_price, first_buy_at FROM paper_positions WHERE code=?",
        (code,), fetch='one')
    #  [원자성 · 2026-09-05] 예수금·포지션·체결 기록은 **함께** 맞아야 한다. 종전에는
    #   세 번의 execute_query 로 각각 커밋했다. 중간에 하나가 실패하면(잠금 5회 소진 →
    #   execute_query 는 raise 한다) 절반만 반영된 원장이 남는다:
    #     · 매수 — 예수금은 빠졌는데 포지션이 없다(가상 자산이 조용히 증발)
    #     · 매도 — 예수금은 늘었는데 포지션이 그대로다(자산이 공짜로 불어난다)
    #   가상 계좌의 자산곡선은 드로다운 → 리스크 한도로 이어지므로, 어긋난 원장이
    #   그대로 매매 강도를 바꾼다([[daily-asset-baseline-transfers]] 와 같은 계열).
    #   읽기(get_cash·pos)는 트랜잭션 **밖**에서 이미 끝냈다 — 같은 스레드에서
    #   execute_query 를 부르면 먼저 커밋해 버려 트랜잭션이 깨진다.
    _FILL_SQL = ("INSERT INTO paper_fills (time, type, code, name, qty, price, amount, fee, "
                 "profit_amt, profit_rate, odno) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
    _CASH_SQL = "INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)"

    if action.lower() == 'buy':
        amount = price * qty
        fee = trading_cost.buy_fee(amount)
        try:
            cash = get_cash(strict=True)
        except Exception as e:      # noqa: BLE001
            return _fail(f"가상 예수금을 읽지 못했습니다 — 주문하지 않습니다: {e}")
        if cash < amount + fee:
            return _fail(f"가상 예수금 부족 (필요 {int(amount+fee):,} / 보유 {int(cash):,})")
        if pos and int(pos[1]) > 0:
            new_qty = int(pos[1]) + qty
            new_avg = (float(pos[2]) * int(pos[1]) + amount) / new_qty
            pos_sql = ("UPDATE paper_positions SET qty=?, avg_price=?, last_buy_at=?, "
                       "name=? WHERE code=?")
            pos_args = (new_qty, new_avg, now, name or pos[0], code)
        else:
            pos_sql = ("INSERT OR REPLACE INTO paper_positions "
                       "(code, name, qty, avg_price, first_buy_at, last_buy_at) "
                       "VALUES (?,?,?,?,?,?)")
            pos_args = (code, name, qty, price, now, now)
        try:
            with _db().transaction() as cur:
                cur.execute(_CASH_SQL, ('cash', json.dumps(cash - amount - fee)))
                cur.execute(pos_sql, pos_args)
                cur.execute(_FILL_SQL, (now, '매수', code, name, qty, price,
                                        amount, fee, 0.0, 0.0, odno))
        except Exception as e:      # noqa: BLE001
            logger.error(f"[PAPER] 매수 원장 기록 실패({code} {qty}주) — "
                         f"되돌렸습니다(주문 없음): {e}", exc_info=True)
            return _fail(f"가상 원장 기록 실패 — 주문이 접수되지 않았습니다: {e}")

    elif action.lower() == 'sell':
        if not pos or int(pos[1]) < qty:
            return _fail(f"가상 보유수량 부족 (요청 {qty} / 보유 {int(pos[1]) if pos else 0})")
        amount = price * qty
        fee = trading_cost.sell_fee(amount)
        avg = float(pos[2])
        # 보고 손익은 왕복 비용을 모두 뺀다(매수 수수료는 진입 시 현금에서 이미 나갔지만,
        # '이 거래로 얼마를 벌었나'는 양쪽을 다 뺀 값이어야 한다 — trading_cost 주석 참조).
        profit_amt, profit_rate = trading_cost.net_realized_profit(avg, price, qty)
        #  strict — 여기서 0으로 떨어지면 매도대금이 예수금 잔액을 **덮는다**.
        try:
            new_cash = get_cash(strict=True) + amount - fee
        except Exception as e:      # noqa: BLE001
            return _fail(f"가상 예수금을 읽지 못했습니다 — 주문하지 않습니다: {e}")
        remain = int(pos[1]) - qty
        if remain > 0:
            pos_sql, pos_args = "UPDATE paper_positions SET qty=? WHERE code=?", (remain, code)
        else:
            pos_sql, pos_args = "DELETE FROM paper_positions WHERE code=?", (code,)
        try:
            with _db().transaction() as cur:
                cur.execute(_CASH_SQL, ('cash', json.dumps(new_cash)))
                cur.execute(pos_sql, pos_args)
                cur.execute(_FILL_SQL, (now, '매도', code, name or pos[0], qty, price,
                                        amount, fee, profit_amt, profit_rate, odno))
        except Exception as e:      # noqa: BLE001
            logger.error(f"[PAPER] 매도 원장 기록 실패({code} {qty}주) — "
                         f"되돌렸습니다(주문 없음): {e}", exc_info=True)
            return _fail(f"가상 원장 기록 실패 — 주문이 접수되지 않았습니다: {e}")
    else:
        return _fail(f"알 수 없는 주문 유형: {action}")
    return None


def _fail(msg):
    logger.warning(f"[PAPER] 주문 거부: {msg}")
    return {"rt_cd": "1", "msg_cd": "PAPER_REJECT", "msg1": f"[가상투자] {msg}", "output": {}}


def _lookup_name(code):
    """주문 종목의 이름 — 관심목록, 가상 원장의 보유 종목명, 코드 순으로 찾는다.

    [2026-10-25] 종전에는 관심목록에 없으면 곧장 코드를 이름으로 돌려줬다. 관심목록 밖
     보유(유니버스 재선정으로 빠진 종목·HTS 직접 매수)는 원장에 이름이 있는데도, 추가
     매수하면 그 이름이 코드로 덮였고(화면 '100140(100140)') 매도 체결 기록의 이름도
     코드로 남았다. ETF/ETN 판정(호가 격자)도 이름 대신 코드를 받았다.
    """
    try:
        for key in ("stocks_kr", "etfs_kr"):
            for item in config.session.stock_data.get(key, []):
                if item.get('code') == code and item.get('name'):
                    return item['name']
    except Exception:
        pass
    try:
        row = _db().execute_query("SELECT name FROM paper_positions WHERE code=?",
                                  (code,), fetch='one')
        if row and row[0]:
            return row[0]
    except Exception as e:      # noqa: BLE001
        logger.debug(f"[PAPER] 보유 종목명 조회 실패({code}): {e}")
    return code


def _record_fill(time_str, type_str, code, name, qty, price, amount, fee, profit_amt, profit_rate, odno):
    _db().execute_query(
        "INSERT INTO paper_fills (time, type, code, name, qty, price, amount, fee, "
        "profit_amt, profit_rate, odno) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (time_str, type_str, code, name, qty, price, amount, fee, profit_amt, profit_rate, odno))


# ==========================================================
# 스냅샷 / 리포트용 집계
# ==========================================================
def snapshot_equity():
    """일별 자산 스냅샷 기록(자산곡선·MDD 산출용). 같은 날 재호출 시 덮어쓴다.

    반환: 이 스냅샷이 **KRX 확정 종가로만** 평가됐는가. 마감 스냅샷을 찍는 쪽이 이 값을
     보고 재시도를 정한다 — 한 종목이라도 일봉을 못 받아 실시간가로 폴백했다면 그날 행은
     아직 종가 기준이 아니다(다음 주기에 덮으면 된다).
    """
    try:
        _, output2 = get_domestic_balance()
        s = output2[0]
        cash = float(s['dnca_tot_amt'])
        stock = float(s['scts_evlu_amt'])
        # 시드를 함께 굳힌다 — 누적 수익률의 분모는 '그 시점' 투입원금이어야 한다.
        _db().execute_query(
            "INSERT OR REPLACE INTO paper_equity (date, cash, stock_value, total, seed) "
            "VALUES (?,?,?,?,?)",
            (datetime.now().strftime('%Y-%m-%d'), cash, stock, cash + stock,
             get_seed(strict=True)))
        # 값 조회는 확정일 단위로 캐시돼 있어 추가 조회가 아니다.
        return all(_krx_settled_close(p['code']) > 0 for p in get_positions())
    except Exception as e:      # noqa: BLE001
        #  [2026-09-06] 종전에는 debug 였다. 이 표는 자산곡선·MDD의 원본이고 MDD 는
        #   리스크 한도로 이어진다 — 행이 빈 것도, 틀린 채 굳는 것도 조용히 지나가면 안 된다.
        #   INSERT 인자를 만드는 조회가 strict 라, 값을 못 만들면 **쓰지 않고** 여기로 온다
        #   (같은 날 행은 INSERT OR REPLACE 라 덮어썼을 값이다).
        logger.error(f"[PAPER] 자산 스냅샷 실패 — 오늘 행을 남기지 못했습니다: "
                     f"{type(e).__name__}: {e}")
        return False


_FILL_COLS = ("time, type, code, name, qty, price, amount, fee, profit_amt, profit_rate, odno")


def _fill_row(r):
    return {"time": r[0], "type": r[1], "code": r[2], "name": r[3], "qty": int(r[4]),
            "price": float(r[5]), "amount": float(r[6]), "fee": float(r[7]),
            "profit_amt": float(r[8]), "profit_rate": float(r[9]), "odno": r[10]}


def get_fills(limit=None):
    q = f"SELECT {_FILL_COLS} FROM paper_fills ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = _db().execute_query(q, fetch='all') or []
    return [_fill_row(r) for r in rows]


def get_fill_by_odno(odno):
    """주문번호로 가상 체결 1건을 찾는다(없으면 None).

    체결 감시가 '이 주문이 실제로 체결됐는가'를 확인하는 유일한 근거다 —
    가상 주문은 증권사 미체결 목록에 뜨지 않으므로 원장 대사 말고는 확인 경로가 없다.
    """
    if not odno:
        return None
    #  [시험 모델] 지연·분할 주문은 **종결된 뒤에만** 누적 체결 한 건으로 답한다. 체결 감시는
    #   주문번호당 한 번 반영하므로(conclusion._handle_simulation_fill), 부분을 먼저 넘기면
    #   잔량 체결이 영영 반영되지 않는다. 즉시 체결 주문은 이 표에 없다 — 종전 경로 그대로.
    order = _order_row(odno)
    if order is not None:
        _advance_orders()
        order = _order_row(odno)
        return _settled_fill(order)
    row = _db().execute_query(
        f"SELECT {_FILL_COLS} FROM paper_fills WHERE odno=? ORDER BY id DESC",
        (str(odno),), fetch='one')
    return _fill_row(row) if row else None


def get_equity_curve():
    """일별 자산 스냅샷. seed는 그 시점 투입원금(옛 행은 None — 표시부가 근사임을 밝힌다)."""
    rows = _db().execute_query(
        "SELECT date, cash, stock_value, total, seed FROM paper_equity ORDER BY date",
        fetch='all') or []
    return [{"date": r[0], "cash": float(r[1]), "stock_value": float(r[2]), "total": float(r[3]),
             "seed": (float(r[4]) if r[4] is not None else None)} for r in rows]


def daily_ledger():
    """날짜별 체결 요약 — 보유 종목 수·실현손익·매매 이벤트를 한 번에 되짚는다.

    [왜 원장에서 세는가] paper_positions 는 '지금'만 안다. 자산 곡선은 과거 행도 보여주므로
    그 시점의 슬롯 사용률을 알려면 체결을 되감는 수밖에 없다. reset()이 paper_fills 와
    paper_equity 를 함께 지우므로 두 표는 항상 같은 계좌를 가리킨다.

    [왜 한 함수인가] 셋 다 같은 원장을 같은 순서로 훑는다. 나눠 두면 같은 루프가 셋이 되고,
    '매도 시 포지션에서 빼는' 규칙 같은 것이 한쪽에서만 바뀔 수 있다.

    반환: {'YYYY-MM-DD': {'holdings': 그날 마감 보유 종목 수,
                          'realized': 그날 실현손익 합(원, 비용 반영됨),
                          'events': ['+한국콜마', '-SK이노베이션', ...]}}
    매매가 없던 날은 키가 없다 — 보유 수는 호출부가 직전 값을 이어 쓴다(매매가 없으면
    보유는 변하지 않는다). 실현손익·이벤트는 없는 날이 곧 0/빈 목록이다.
    """
    rows = _db().execute_query(
        "SELECT time, type, code, name, qty, profit_amt FROM paper_fills ORDER BY id",
        fetch='all') or []
    held, by_date = {}, {}
    for t, type_str, code, name, qty, profit in rows:
        d = str(t)[:10]
        e = by_date.setdefault(d, {"holdings": 0, "realized": 0.0, "events": []})
        is_buy = type_str == '매수'
        held[code] = held.get(code, 0) + (int(qty) if is_buy else -int(qty))
        if held[code] <= 0:
            held.pop(code, None)
        if not is_buy:
            e["realized"] += float(profit or 0.0)
        e["events"].append(("+" if is_buy else "-") + (name or code))
        e["holdings"] = len(held)
    return by_date


def equity_index(rows, live_total=None, live_seed=None):
    """자산 곡선을 **입출금 중립** 지수로 바꾼다(첫날 = 1.0).

    [왜 필요한가 · 2026-09-04] 낙폭은 총자산 곡선에서 그대로 재고 있었다. 그런데 가상
     입출금(adjust_seed)은 현금을 계단처럼 움직이므로, **매매 성과가 조금도 나빠지지
     않아도 출금액이 그대로 드로다운으로 찍힌다** — 1,000만 계좌에서 300만을 빼면 MDD
     0.00% → -27.45%(실측). 반대로 입금은 고점을 올려 뒤이은 진짜 낙폭을 부풀린다.
     실계좌 쪽은 2026-08-30에 같은 문제를 net_transfer 환산으로 정리했는데
     (daily_asset_history), 5-8 성과 화면이 보는 paper_equity 만 남아 있었다.

    스냅샷 행마다 그 시점 시드가 함께 굳어 있으므로(seed 컬럼), 전날 대비 시드 증감이
    곧 그 사이의 입출금이다. 그만큼을 뺀 뒤 수익률을 재 이어 붙인다(시간가중수익률).
    시드가 없는 옛 행은 입출금이 없었던 것으로 본다 — 그 시절엔 입출금 기능이 곡선에
    반영되지 않았으므로 추정하는 것보다 낫다.
    """
    series = [(float(r["total"]), r.get("seed")) for r in rows]
    if live_total is not None:
        series.append((float(live_total), live_seed))
    if not series:
        return []

    index = [1.0]
    prev_total, prev_seed = series[0]
    for total, seed in series[1:]:
        transfer = 0.0
        if seed is not None and prev_seed is not None:
            transfer = float(seed) - float(prev_seed)
        if prev_total > 0:
            index.append(index[-1] * ((total - transfer) / prev_total))
        else:
            index.append(index[-1])
        prev_total, prev_seed = total, (seed if seed is not None else prev_seed)
    return index


def max_drawdown(index):
    """지수 계열의 최대 낙폭(%). 낙폭이 없으면 0.0."""
    if not index:
        return 0.0
    peak, worst = index[0], 0.0
    for v in index:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak * 100)
    return worst


def get_performance():
    """누적 성과 지표. 백테스트 리포트와 같은 정의(PF·승률·MDD·연속손실)를 쓴다."""
    seed = get_seed()
    _, output2 = get_domestic_balance()
    total = float(output2[0]['tot_evlu_amt'])
    sells = [f for f in get_fills() if f['type'] == '매도']
    wins = [f for f in sells if f['profit_amt'] > 0]
    losses = [f for f in sells if f['profit_amt'] <= 0]
    gross_profit = sum(f['profit_amt'] for f in wins)
    gross_loss = abs(sum(f['profit_amt'] for f in losses))

    # [기준] 곡선(일별 스냅샷) **뒤에 현재값을 붙여** 낙폭을 잰다. 스냅샷만 쓰면 오늘의
    #  하락은 다음 스냅샷이 찍히기 전까지 MDD에 절대 들어가지 않아, 같은 화면의 총자산은
    #  실시간인데 MDD만 어제 기준인 상태가 된다(2026-08-30 실측).
    #  곡선은 입출금 중립 지수로 환산해서 잰다 — 출금액이 그대로 낙폭으로 찍히던 자리다
    #  (equity_index 참조).
    rows = get_equity_curve() or [{"total": seed, "seed": seed}]
    mdd = max_drawdown(equity_index(rows, live_total=total, live_seed=seed))

    streak = best_streak = 0
    for f in sells:
        streak = streak + 1 if f['profit_amt'] <= 0 else 0
        best_streak = max(best_streak, streak)

    return {
        "seed": seed, "total": total,
        "total_return": (total - seed) / seed * 100 if seed else 0.0,
        "cash": get_cash(), "positions": len(get_positions()),
        "sell_count": len(sells), "win": len(wins), "loss": len(losses),
        "win_rate": len(wins) / len(sells) * 100 if sells else 0.0,
        "pf": (gross_profit / gross_loss) if gross_loss else (float('inf') if gross_profit else 0.0),
        "gross_profit": gross_profit, "gross_loss": gross_loss,
        "mdd": mdd, "max_loss_streak": best_streak,
        "started_at": _get_state('started_at', '-'),
    }


def adjust_seed(amount):
    """가상 계좌 입출금. 실계좌에 돈을 넣고 빼는 것과 같게 다룬다.

    시드(=누적 투입원금)와 현금을 함께 움직여야 수익률 분모가 맞는다.
    초기화와 달리 포지션·체결 이력은 그대로 유지된다.
    반환: (성공여부, 메시지)
    """
    with _lock:
        amount = int(amount)
        try:
            cash = get_cash(strict=True)
            new_seed = get_seed(strict=True) + amount
        except Exception as e:      # noqa: BLE001
            return False, f"가상 계좌 잔액을 읽지 못했습니다 — 반영하지 않았습니다: {e}"
        if amount < 0 and cash + amount < 0:
            return False, f"출금액이 가상 현금({int(cash):,}원)을 초과합니다"
        if new_seed <= 0:
            return False, "시드가 0 이하가 됩니다"
        #  [원자성 · 2026-09-05] 위 독스트링이 불변식을 적어 뒀다 — "시드와 현금을
        #   **함께** 움직여야 수익률 분모가 맞는다". 따로 쓰면 그 불변식이 깨진다:
        #   현금만 반영되고 시드가 안 움직이면 입금액이 그대로 '수익'으로 보인다.
        try:
            with _db().transaction() as cur:
                sql = "INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)"
                cur.execute(sql, ('cash', json.dumps(cash + amount)))
                cur.execute(sql, ('seed', json.dumps(new_seed)))
        except Exception as e:      # noqa: BLE001
            logger.error(f"[PAPER] 입출금 기록 실패({amount:+,}원) — 되돌렸습니다: {e}",
                         exc_info=True)
            return False, f"가상 원장 기록 실패 — 반영되지 않았습니다: {e}"
        logger.info(f"[PAPER] 가상 {'입금' if amount >= 0 else '출금'} {abs(amount):,}원 "
                    f"(시드 {new_seed:,.0f} / 현금 {cash+amount:,.0f})")
    # 락 밖에서 — 기준선 보정은 DB·JSON·트레이더 메모리를 함께 만진다.
    _shift_daily_baseline(amount)
    return True, f"{'입금' if amount >= 0 else '출금'} {abs(amount):,}원 반영 완료"


def _account_key():
    """일일 자산 기준선이 쓰는 계좌 키. common._get_trade_account()와 같은 규칙."""
    s = config.session
    cano = getattr(s, 'auto_cano', None) or getattr(s, 'cano', '') or ''
    acnt = getattr(s, 'auto_acnt_prdt_cd', None) or getattr(s, 'acnt_prdt_cd', '') or ''
    return f"{cano}-{acnt}"


def _clear_daily_baseline():
    """초기화 후 남는 '오늘 시작 자산' 기준선을 지운다.

    일일 손실 한도(check_loss_limit)와 드로다운 기반 리스크 스케일링(HWM)이 이
    기준선을 본다. 시드를 바꿔 초기화했는데 기준선이 남아 있으면 500만 → 100만
    축소가 -80% 손실로 읽혀 방어 모드가 즉시 걸리고, 반대로 키우면 드로다운이
    과소평가된다.

    [주의] daily_asset_state.json은 실계좌 기준선과 **한 파일을 공유**한다. 파일을
    통째로 지우면 실전 인스턴스가 같은 날 재기동할 때 당일 손실 기준을 잃고 현재
    자산으로 다시 잡는다(그날의 낙폭이 조용히 사라진다). 가상 계좌 키만 지운다.
    daily_asset_history는 페이퍼 DB에 따로 있지만 대칭성을 위해 같은 키로 지운다.
    """
    key = _account_key()

    try:
        from core import jsonio
        from modules.auto_trade.common import DAILY_STATE_FILE
        data = jsonio.load_json(DAILY_STATE_FILE, default={}) or {}
        accounts = data.get("accounts") or {}
        if key in accounts:
            accounts.pop(key, None)
            data["accounts"] = accounts
            jsonio.save_json(DAILY_STATE_FILE, data)
            logger.info(f"[PAPER] 일일 시작 자산 기준선 삭제: {key}")
    except Exception as e:
        logger.warning(f"[PAPER] 일일 자산 기준선 정리 실패(무시): {e}")

    try:
        _db().execute_query("DELETE FROM daily_asset_history WHERE account=?", (key,))
    except Exception as e:
        logger.warning(f"[PAPER] 일일 자산 이력 정리 실패(무시): {e}")

    # 실행 중인 트레이더의 메모리 기준선도 함께 내린다(재기동 없이 반영).
    #  0은 '미설정'이라 손실 한도 판정이 건너뛰어지고, 다음 초기화 때 새 시드로 다시 잡힌다.
    try:
        import modules.auto_trade as _at
        inst = getattr(_at.AutoTrader, "_instance", None)
        if inst is not None:
            inst.initial_asset = 0
            inst._hwm_cache = 0.0
            inst._hwm_cache_date = None
    except Exception as e:
        logger.warning(f"[PAPER] 트레이더 기준선 초기화 실패(무시): {e}")


def _shift_daily_baseline(amount):
    """가상 입출금 뒤 드로다운 기준을 다시 재게 만든다. **아무것도 옮기지 않는다.**

    [왜 옮기지 않게 됐나] 종전에는 자산 이력(daily_asset_history)을 통째로 평행이동하고
    실행 중 트레이더의 initial_asset·baseline_principal 까지 함께 옮겼다. 실계좌 쪽은
    2026-08-30에 그 방식을 폐기했다 — 되돌릴 수 없고, 추정이 틀리면 고점이 낮아져
    드로다운을 **과소**평가한다(= 리스크 한도가 조용히 열린다). 원본을 두고 읽을 때
    환산하는 쪽(net_transfer)으로 통일됐는데 가상 계좌만 옛 방식이 남아 있었다.

    [남겨 두면 이중 보정이 된다] 옛 코드는 메모리의 baseline_principal 만 옮기고 DB의
    대조점(daily_asset_history.principal)은 두었다. 그래서 다음 기동의 오프라인 보정
    (trader._reconcile_offline_transfer)이 같은 입출금을 한 번 더 잡아 이력에 net_transfer
    까지 적었다 — 이동 + 환산 = 두 번.
    (게다가 오늘 기준선 보정 코드는 daily_asset_state.json 의 계좌 항목이 dict 로 바뀐
     뒤로 `dict + int` 예외를 내고 조용히 실패하고 있었다. 고칠 것이 아니라 없앨 코드였다.)

    [그래서 지금은 무엇이 처리하나] 가상 입출금은 현금을 바꾸므로 원금 불변량
    (현금+매입원가-실현손익)이 그만큼 움직인다. 실계좌와 **같은 경로**가 자동으로 잡는다.
      · 트레이더 가동 중  → net_transfer_today 가 매 주기 다시 재 기준선을 보정하고,
                            그날 행의 net_transfer 로 드로다운 기준까지 환산된다.
      · 트레이더 정지 중  → 다음 기동 때 _reconcile_offline_transfer 가 되찾는다.
    여기서는 하루 1회만 갱신되는 HWM 캐시만 풀어 준다 — 안 풀면 그 갱신이 오늘 하루
    옛 고점을 그대로 쓴다.
    """
    if not amount:
        return
    try:
        import modules.auto_trade as _at
        inst = getattr(_at.AutoTrader, "_instance", None)
        if inst is not None:
            inst._hwm_cache_date = None
    except Exception as e:
        logger.warning(f"[PAPER] 드로다운 기준 재측정 유도 실패(무시): {e}")


# 실계좌 DB와 **이름·스키마가 같은** 공용 테이블. 페이퍼 DB 파일에서만 지운다.
#  trades       : 5-4 트레이딩 평가·보유 분석이 보는 매매 기록. 남겨 두면 초기화 후
#                 성과 화면이 지워진 계좌의 과거 청산을 계속 집계한다.
#  journal_outbox: 매매일지 전송 대기열. 지운 매매가 뒤늦게 웹서버로 나가지 않게 한다.
#  trailing_stops / half_tp_status : code가 PK인 포지션 파생 상태. 포지션만 지우고
#                 남기면 같은 종목 재진입 시 옛 최고가·반익절 이력이 그대로 붙는다.
#  reserved_orders: 초기화 뒤에도 살아남아 발동하는 가상 예약 주문을 없앤다.
_SHARED_TABLES = ("trades", "journal_outbox", "trailing_stops",
                  "half_tp_status", "reserved_orders")


def _is_paper_db():
    """현재 열려 있는 DB가 가상투자 전용 파일인가.

    _SHARED_TABLES는 실계좌 DB에도 같은 이름으로 있다. 세션이 페이퍼 DB로 전환되지
    않은 상태에서 초기화가 불리면 **실계좌 매매 기록을 통째로 지우게 되므로**,
    파일 경로를 확인한 뒤에만 손댄다(fail-closed: 확인 못 하면 지우지 않는다).
    """
    import os
    try:
        return os.path.abspath(_db().db_path) == os.path.abspath(config.PAPER_DB_FILE_PATH)
    except Exception as e:
        logger.warning(f"[PAPER] DB 경로 확인 실패 — 공용 테이블은 건드리지 않는다: {e}")
        return False


def _clear_trade_history():
    """가상 계좌의 매매 기록·포지션 파생 상태를 지운다. 실계좌 DB면 아무것도 안 한다."""
    if not _is_paper_db():
        logger.warning("[PAPER] 가상투자 DB가 아니어서 매매 기록 삭제를 건너뛴다 "
                       f"(현재 DB: {getattr(_db(), 'db_path', '?')})")
        return False

    for tbl in _SHARED_TABLES:
        try:
            _db().execute_query(f"DELETE FROM {tbl}")
        except Exception as e:
            # 테이블이 아직 없을 수 있다(구버전 페이퍼 DB). 초기화 자체는 계속 진행한다.
            logger.warning(f"[PAPER] {tbl} 정리 실패(무시): {e}")

    # 실행 중인 트레이더의 메모리 캐시도 함께 내린다(재기동 없이 반영).
    #  DB만 지우면 트레일링 최고가·반익절 이력이 메모리에 살아남아 다음 주기에 다시 쓰인다.
    try:
        import modules.auto_trade as _at
        inst = getattr(_at.AutoTrader, "_instance", None)
        if inst is not None:
            if isinstance(getattr(inst, 'trailing_stop_cache', None), dict):
                inst.trailing_stop_cache.clear()
            half = getattr(inst, 'half_tp_cache', None)
            if isinstance(half, (set, dict)):
                half.clear()
            om = getattr(inst, 'order_manager', None)
            if om is not None:
                with om._lock:
                    om.pending_orders.clear()
    except Exception as e:
        logger.warning(f"[PAPER] 트레이더 메모리 캐시 정리 실패(무시): {e}")
    return True


def _clear_restricted_stocks():
    """가상 계좌 앞으로 걸린 트레이딩 제한 종목을 푼다.

    [주의] restricted_stocks.json은 실계좌와 **한 파일을 공유**한다(계좌별 키로 구분).
    통째로 지우면 실계좌의 수동매매 보호가 사라지므로 가상 계좌 키만 제거하고,
    전 계좌 공통(global memo) 항목은 사용자가 직접 건 것이므로 손대지 않는다.
    """
    if not is_active():
        # 관찰 모드가 아니면 _get_trade_account()가 실계좌를 가리킨다 — 손대지 않는다.
        logger.warning("[PAPER] 관찰 모드가 아니어서 제한 종목 해제를 건너뛴다")
        return []
    try:
        import modules.auto_trade as _at
        # 등록 경로(add_restricted_stock)와 같은 계좌를 써야 키가 어긋나지 않는다.
        cano, acnt = _at._get_trade_account()
        key = f"{cano}-{acnt or ''}"
        data = _at.load_restricted_stocks()
        targets = [c for c, info in data.items() if key in (info.get('accounts') or {})]
        freed = [data[c].get('name', c) for c in targets]
        for code in targets:
            _at.remove_restricted_stock(code, cano=cano, acnt=acnt)
        if freed:
            logger.info(f"[PAPER] 가상 계좌 트레이딩 제한 해제 ({len(freed)}종목): {', '.join(freed)}")
        return freed
    except Exception as e:
        logger.warning(f"[PAPER] 제한 종목 정리 실패(무시): {e}")
        return []


def reset(seed=None):
    """가상 계좌 초기화. 포지션·체결·자산곡선·매매 기록을 모두 지우고 시드를 다시 넣는다."""
    with _lock:
        cleared = _clear_trade_history()
        seed = int(seed if seed is not None else getattr(config, 'PAPER_SEED_CAPITAL', 10_000_000))
        #  비우기와 새 시드 넣기를 한 트랜잭션으로 묶는다. 중간에 끊기면 paper_state 만
        #  비워진 계좌(현금 0·시드 없음)가 남는다 — 다시 초기화하면 복구되지만, 그 사이
        #  기동한 자동매매가 예수금 0 으로 판단한다.
        try:
            with _db().transaction() as cur:
                for tbl in ("paper_positions", "paper_fills", "paper_equity", "paper_state"):
                    cur.execute(f"DELETE FROM {tbl}")
                sql = "INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)"
                cur.execute(sql, ('seed', json.dumps(seed)))
                cur.execute(sql, ('cash', json.dumps(seed)))
                cur.execute(sql, ('started_at',
                                  json.dumps(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))))
        except Exception as e:      # noqa: BLE001
            logger.error(f"[PAPER] 계좌 초기화 실패 — 되돌렸습니다: {e}", exc_info=True)
            raise
        logger.info(f"[PAPER] 가상 계좌 초기화 (시드 {seed:,}원, 매매 기록 삭제 "
                    f"{'완료' if cleared else '건너뜀'})")
    _clear_daily_baseline()
    _clear_restricted_stocks()
    return cleared


# ==========================================================
# [시험 모델 · 2026-10-16] 체결 장애 시나리오 — config.PAPER_FAULT_MODEL
# ==========================================================
#  목적은 시장 재현이 아니라 **주문 상태기계 시험**이다: 거절, 지연 체결, 부분체결과 잔량,
#  취소와 체결의 경합, 재시작 뒤 이어지는 미체결. 주문마다 (SEED, 일련번호)로 시나리오를
#  미리 정해 paper_orders 에 적어 두고, 시간이 된 체결은 누가 조회할 때(미체결 목록·체결
#  확인·취소·잔고) 진행한다. 같은 SEED·같은 주문 순서면 결과가 같다.
#  체결 자체는 _book_fill(즉시 체결과 같은 원장 코드)로 남긴다.

_ORDER_COLS = ("odno, code, name, side, qty, price, filled_qty, fill_amount, status, created_at, "
               "updated_at, last_fill_at, schedule, next_stage, race, note")
_OPEN = "open"
_FINAL = ("filled", "cancelled", "failed")


def _now():
    """시험 모델의 시계(테스트가 바꿔 끼운다)."""
    return datetime.now()


def fault_config():
    cfg = dict(getattr(config, "PAPER_FAULT_MODEL", None) or {})
    cfg.setdefault("ENABLED", False)
    return cfg


def fault_model_enabled():
    return bool(fault_config().get("ENABLED"))


def _order_dict(r):
    keys = [k.strip() for k in _ORDER_COLS.split(",")]
    d = dict(zip(keys, r))
    d["schedule"] = json.loads(d["schedule"] or "[]")
    return d


def _order_row(odno):
    try:
        r = _db().execute_query(f"SELECT {_ORDER_COLS} FROM paper_orders WHERE odno=?",
                                (str(odno),), fetch='one')
    except Exception as e:      # noqa: BLE001  (표가 없으면 시험 모델을 쓴 적이 없다)
        logger.debug(f"[PAPER] 주문 상태 조회 실패({odno}): {e}")
        return None
    return _order_dict(r) if r else None


def _open_orders_rows():
    try:
        rows = _db().execute_query(
            f"SELECT {_ORDER_COLS} FROM paper_orders WHERE status=? ORDER BY created_at, odno",
            (_OPEN,), fetch='all') or []
    except Exception as e:      # noqa: BLE001
        logger.debug(f"[PAPER] 미체결 조회 실패: {e}")
        return []
    return [_order_dict(r) for r in rows]


def _next_fault_seq():
    seq = int(_get_state("fault_seq", 0) or 0) + 1
    _set_state("fault_seq", seq)
    return seq


def _settled_fill(order):
    """종결된 주문의 누적 체결 한 건(체결 0이거나 아직 미체결이면 None)."""
    if not order or order["status"] not in _FINAL or int(order["filled_qty"] or 0) <= 0:
        return None
    qty = int(order["filled_qty"])
    return {"time": order["last_fill_at"] or order["updated_at"], "type": "매수" if order["side"] == "buy" else "매도",
            "code": order["code"], "name": order["name"], "qty": qty,
            "price": float(order["fill_amount"]) / qty, "amount": float(order["fill_amount"]),
            "fee": 0.0, "profit_amt": 0.0, "profit_rate": 0.0, "odno": order["odno"],
            "partial": qty < int(order["qty"]), "status": order["status"]}


def _update_order(odno, **kw):
    kw["updated_at"] = _now().strftime('%Y-%m-%d %H:%M:%S')
    cols = ", ".join(f"{k}=?" for k in kw)
    _db().execute_query(f"UPDATE paper_orders SET {cols} WHERE odno=?", (*kw.values(), str(odno)))


def _execute_stage(order, qty, when):
    """주문 한 건의 체결 단계 하나를 원장에 남긴다. 실패면 주문을 'failed' 로 닫는다."""
    stamp = when.strftime('%Y-%m-%d %H:%M:%S')
    booked = _book_fill(order["side"], order["code"], order["name"], qty, order["price"],
                        order["odno"], stamp)
    if booked is not None:
        _update_order(order["odno"], status="failed", note=str(booked.get("msg1")))
        order["status"] = "failed"
        return False
    order["filled_qty"] = int(order["filled_qty"] or 0) + qty
    order["fill_amount"] = float(order["fill_amount"] or 0) + qty * float(order["price"])
    done = order["filled_qty"] >= int(order["qty"])
    order["status"] = "filled" if done else _OPEN
    _update_order(order["odno"], filled_qty=order["filled_qty"], fill_amount=order["fill_amount"],
                  status=order["status"], last_fill_at=stamp, next_stage=order["next_stage"])
    return True


def _advance_orders(now=None):
    """시간이 된 체결 단계를 진행한다. 호출부가 아니어도 _lock 을 잡는다(재진입 가능)."""
    now = now or _now()
    with _lock:
        for order in _open_orders_rows():
            stages = order["schedule"]
            while order["status"] == _OPEN and order["next_stage"] < len(stages):
                at, qty = stages[order["next_stage"]]
                due = datetime.strptime(at, '%Y-%m-%d %H:%M:%S')
                if due > now:
                    break
                order["next_stage"] += 1
                #  체결 시각은 '예정 시각'으로 남긴다 — 조회 시각을 쓰면 같은 시드라도 기록이 달라진다
                if not _execute_stage(order, int(qty), due):
                    break


def _place_with_faults(action, code, name, qty, price, is_market):
    """시험 모델의 주문 접수 — 시나리오를 정해 적고, 지금 도래한 체결은 바로 진행한다."""
    cfg = fault_config()
    side = action.lower()
    if side not in ("buy", "sell"):
        return _fail(f"알 수 없는 주문 유형: {action}")
    seq = _next_fault_seq()
    rng = random.Random(f"{cfg.get('SEED', 0)}:{seq}")
    rejected = rng.random() < float(cfg.get("REJECT_PROB", 0) or 0)
    delayed = rng.random() < float(cfg.get("DELAY_PROB", 0) or 0)
    partial = rng.random() < float(cfg.get("PARTIAL_PROB", 0) or 0) and qty >= 2
    race = rng.random() < float(cfg.get("CANCEL_RACE_PROB", 0) or 0)
    if rejected:
        return _fail(f"[시험 모델] 주문 거절(시드 {cfg.get('SEED', 0)} · {seq}번째 주문)")

    now = _now()
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    odno = _new_odno(code)
    try:
        import api as _api_mod
        _is_etf = bool(_api_mod.is_domestic_etf_etn(code, name))
    except Exception:           # noqa: BLE001
        _is_etf = False
    price = fill_price(price, action, market=is_market, is_etf=_is_etf)

    # 접수 시점 확인 — 실계좌처럼 주문가능금액·주문가능수량을 넘는 주문은 받지 않는다
    if side == "buy":
        need = price * qty + trading_cost.buy_fee(price * qty)
        try:
            avail = get_cash(strict=True) - _reserved_cash()
        except Exception as e:  # noqa: BLE001
            return _fail(f"가상 예수금을 읽지 못했습니다 — 주문하지 않습니다: {e}")
        if avail < need:
            return _fail(f"가상 주문가능금액 부족 (필요 {int(need):,} / 가능 {int(avail):,})")
    else:
        pos = _db().execute_query("SELECT qty FROM paper_positions WHERE code=?", (code,), fetch='one')
        held = int(pos[0]) if pos else 0
        if held - _open_sell_qty().get(code, 0) < qty:
            return _fail(f"가상 주문가능수량 부족 (요청 {qty} / 가능 {held - _open_sell_qty().get(code, 0)})")

    delay = int(cfg.get("DELAY_SECONDS", 30) or 0)
    first_at = now if not delayed else now.replace(microsecond=0) + _seconds(delay)
    stages = []
    if partial:
        q1 = max(1, min(qty - 1, int(qty * float(cfg.get("PARTIAL_RATIO", 0.5) or 0.5))))
        stages.append((first_at.strftime('%Y-%m-%d %H:%M:%S'), q1))
        rem_sec = int(cfg.get("REMAINDER_FILL_SECONDS", 0) or 0)
        if rem_sec > 0:
            stages.append(((first_at + _seconds(rem_sec)).strftime('%Y-%m-%d %H:%M:%S'), qty - q1))
    else:
        stages.append((first_at.strftime('%Y-%m-%d %H:%M:%S'), qty))

    with _lock:
        _db().execute_query(
            f"INSERT INTO paper_orders ({_ORDER_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (odno, code, name, side, qty, price, 0, 0.0, _OPEN, stamp, stamp, None,
             json.dumps(stages), 0, 1 if race else 0,
             f"seq={seq} delayed={int(delayed)} partial={int(partial)}"))
        _advance_orders(now)
    logger.info(f"[PAPER·시험] {side.upper()} 접수 {code} {qty}주 @{price:,.0f} (No.{odno}) "
                f"지연={delayed} 부분={partial} 경합={race}")
    return {"rt_cd": "0", "msg_cd": "PAPER", "msg1": "가상투자 접수(시험 모델)",
            "output": {"ODNO": odno, "ORD_TMD": now.strftime('%H%M%S')}}


def _seconds(n):
    from datetime import timedelta
    return timedelta(seconds=int(n))


def _reserved_cash():
    """미체결 매수 잔량에 묶인 현금(주문가 기준 + 수수료). 즉시 체결에서는 0."""
    total = 0.0
    for o in _open_orders_rows():
        if o["side"] == "buy":
            rem = int(o["qty"]) - int(o["filled_qty"] or 0)
            amt = rem * float(o["price"])
            total += amt + trading_cost.buy_fee(amt)
    return total


def _open_sell_qty():
    out = {}
    for o in _open_orders_rows():
        if o["side"] == "sell":
            out[o["code"]] = out.get(o["code"], 0) + int(o["qty"]) - int(o["filled_qty"] or 0)
    return out


def open_orders():
    """api.get_domestic_open_orders 대체(KIS 정정취소가능주문 형식). 즉시 체결에서는 빈 목록."""
    _advance_orders()
    out = []
    for o in _open_orders_rows():
        created = datetime.strptime(o["created_at"], '%Y-%m-%d %H:%M:%S')
        rem = int(o["qty"]) - int(o["filled_qty"] or 0)
        out.append({"odno": o["odno"], "pdno": o["code"], "prdt_name": o["name"] or o["code"],
                    "ord_qty": str(o["qty"]), "tot_ccld_qty": str(o["filled_qty"] or 0),
                    "rmn_qty": str(rem), "psbl_qty": str(rem), "ord_unpr": str(o["price"]),
                    "ord_tmd": created.strftime('%H%M%S'), "ord_dt": created.strftime('%Y%m%d'),
                    "sll_buy_dvsn_cd": "02" if o["side"] == "buy" else "01",
                    "sll_buy_dvsn_cd_name": "매수" if o["side"] == "buy" else "매도", "_paper": True})
    return out


def cancel_order(org_no):
    """api.revise_cancel_order(취소) 대체. 시험 모델 주문이 아니면 종전 응답(대상 없음)."""
    with _lock:
        order = _order_row(org_no)
        if order is None:
            return {"rt_cd": "1", "msg_cd": "PAPER_REJECT",
                    "msg1": "[가상투자] 즉시 체결되어 정정/취소할 주문이 없습니다", "output": {}}
        _advance_orders()                       # 취소 시각 이전에 도래한 체결이 먼저다
        order = _order_row(org_no)
        if order["status"] != _OPEN:
            return {"rt_cd": "1", "msg_cd": "PAPER_REJECT",
                    "msg1": f"[가상투자] 이미 종결된 주문입니다({order['status']})", "output": {}}
        if order["race"]:
            # 경합: 취소가 닿기 전에 잔량이 체결된다 — 취소는 실패한다
            rem = int(order["qty"]) - int(order["filled_qty"] or 0)
            order["next_stage"] = len(order["schedule"])
            _execute_stage(order, rem, _now())
            return {"rt_cd": "1", "msg_cd": "PAPER_RACE",
                    "msg1": "[가상투자·시험] 취소 전에 잔량이 체결되었습니다(경합)", "output": {}}
        _update_order(org_no, status="cancelled", note=(order.get("note") or "") + " cancelled")
        return {"rt_cd": "0", "msg_cd": "PAPER", "msg1": "가상투자 취소(시험 모델)",
                "output": {"ODNO": f"C{org_no}"}}


def get_order_state(odno):
    """시험 모델 주문의 현재 상태 dict(시험 모델 주문이 아니면 None)."""
    if _order_row(odno) is None:
        return None
    _advance_orders()
    return _order_row(odno)


def get_settled_fills():
    """체결 감시 재기동 복구용 — 주문번호당 한 건(즉시 체결 행은 그대로, 시험 모델 주문은 종결 누적).

    시험 모델을 쓴 적이 없으면 get_fills() 와 같다.
    """
    fills = get_fills()
    try:
        model = {r[0] for r in (_db().execute_query("SELECT odno FROM paper_orders", fetch='all') or [])}
    except Exception:           # noqa: BLE001
        model = set()
    if not model:
        return fills
    _advance_orders()
    out = [f for f in fills if f.get("odno") not in model]
    for odno in sorted(model):
        sf = _settled_fill(_order_row(odno))
        if sf:
            out.append(sf)
    return out
