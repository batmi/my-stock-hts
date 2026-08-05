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
수수료·거래세만 차감한다. 미체결·부분체결은 재현하지 않는다(1단계 범위).
백테스트가 종가 기준이라 청산일 하락갭(실측 평균 -1.44%)만큼 낙관 편향이 있는 반면,
이 모드는 실제 장중 판단 시점 가격으로 체결되므로 그 편향이 없다.
"""
import json
import logging
import threading
from datetime import datetime

import config

logger = logging.getLogger(__name__)

_lock = threading.RLock()

# 체결 비용. 매수는 위탁수수료만, 매도는 수수료 + 증권거래세.
# (포트폴리오 백테스트 simulate와 동일 기준: 매도 0.23%)
BUY_FEE_RATE = 0.00015
SELL_FEE_RATE = 0.0023


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
    conn.commit()

    seed = int(getattr(config, 'PAPER_SEED_CAPITAL', 5_000_000))
    if _get_state('seed') is None:
        _set_state('seed', seed)
        _set_state('cash', seed)
        _set_state('started_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        logger.info(f"[PAPER] 가상 계좌 개설: 시드 {seed:,}원")


def _get_state(key, default=None):
    try:
        row = _db().execute_query("SELECT value FROM paper_state WHERE key=?", (key,), fetch='one')
        return json.loads(row[0]) if row else default
    except Exception:
        return default


def _set_state(key, value):
    _db().execute_query("INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)",
                        (key, json.dumps(value)))


def get_cash():
    return float(_get_state('cash', 0) or 0)


def get_seed():
    return float(_get_state('seed', getattr(config, 'PAPER_SEED_CAPITAL', 5_000_000)))


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


def reset_price_cache():
    """테스트·계좌 전환용 — 마지막 정상가 캐시를 비운다."""
    with _lock:
        _last_prices.clear()


def _price_with_status(code, cost=0.0):
    """(가격, stale여부)를 돌려준다. stale이면 **매매 판정에 쓰면 안 되는 값**이다.

    조회에 실패했을 때 평단으로 폴백하면 prpr == pchs_avg_pric 이 되어 수익률이
    정확히 0.00%로 계산된다. 트레이더의 방어선은 `current_price <= 0` 하나뿐이라
    이 값이 그대로 통과하고, 실제로는 크게 하락한 포지션이 '본전에서 쉬는 정상
    포지션'으로 위장된다 — 손절도 트레일링도 발동하지 않고 로그에도 남지 않는다.
    KIS 레이트리밋(EGW00201)·토큰 만료 같은 일시적 실패는 실제로 관측된다.

    폴백 순서: 직전 정상가 → 평단(직전 정상가가 없을 때만). 어느 쪽이든 stale로 표시해
    판정에서 배제하되, 평가금이 0으로 무너지지는 않게 한다(자산 스냅샷 보존).
    """
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


# ==========================================================
# api 가로채기 대상 — KIS 응답과 같은 스키마로 반환한다
# ==========================================================
def get_domestic_balance():
    """api.get_domestic_balance 대체. (output1 보유목록, output2 요약) 형태."""
    with _lock:
        positions = get_positions()
        cash = get_cash()
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
                'ord_psbl_qty': str(p['qty']),
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
    """api.get_deposit_balance 대체. 가상 현금은 즉시 결제로 본다(D+2 구분 없음)."""
    cash = int(get_cash())
    return {"deposit": cash, "foreign_deposit": 0, "withdraw": cash,
            "d2_deposit": cash, "order_possible": cash, "d2_real": cash}


def place_order(action, code, qty, price, name=None):
    """api.place_order 대체. 즉시 전량 체결로 처리하고 KIS 형식 응답을 만든다."""
    with _lock:
        try:
            qty = int(qty)
            price = float(price)
        except (TypeError, ValueError):
            return _fail("주문 수량/가격이 올바르지 않습니다")
        if qty <= 0:
            return _fail("주문 수량이 0입니다")

        if price <= 0:  # 시장가 주문 → 현재가로 체결
            price = _current_price(code)
            if price <= 0:
                return _fail("현재가를 확인할 수 없어 체결 불가")

        if name is None:
            name = _lookup_name(code)

        pos = _db().execute_query(
            "SELECT name, qty, avg_price, first_buy_at FROM paper_positions WHERE code=?",
            (code,), fetch='one')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        odno = f"P{datetime.now().strftime('%y%m%d%H%M%S')}{code[-2:]}"

        if action.lower() == 'buy':
            amount = price * qty
            fee = amount * BUY_FEE_RATE
            cash = get_cash()
            if cash < amount + fee:
                return _fail(f"가상 예수금 부족 (필요 {int(amount+fee):,} / 보유 {int(cash):,})")
            _set_state('cash', cash - amount - fee)
            if pos and int(pos[1]) > 0:
                new_qty = int(pos[1]) + qty
                new_avg = (float(pos[2]) * int(pos[1]) + amount) / new_qty
                _db().execute_query(
                    "UPDATE paper_positions SET qty=?, avg_price=?, last_buy_at=?, name=? WHERE code=?",
                    (new_qty, new_avg, now, name or pos[0], code))
            else:
                _db().execute_query(
                    "INSERT OR REPLACE INTO paper_positions "
                    "(code, name, qty, avg_price, first_buy_at, last_buy_at) VALUES (?,?,?,?,?,?)",
                    (code, name, qty, price, now, now))
            _record_fill(now, '매수', code, name, qty, price, amount, fee, 0.0, 0.0, odno)

        elif action.lower() == 'sell':
            if not pos or int(pos[1]) < qty:
                return _fail(f"가상 보유수량 부족 (요청 {qty} / 보유 {int(pos[1]) if pos else 0})")
            amount = price * qty
            fee = amount * SELL_FEE_RATE
            avg = float(pos[2])
            profit_amt = (price - avg) * qty - fee
            profit_rate = (profit_amt / (avg * qty) * 100) if avg else 0.0
            _set_state('cash', get_cash() + amount - fee)
            remain = int(pos[1]) - qty
            if remain > 0:
                _db().execute_query("UPDATE paper_positions SET qty=? WHERE code=?", (remain, code))
            else:
                _db().execute_query("DELETE FROM paper_positions WHERE code=?", (code,))
            _record_fill(now, '매도', code, name or pos[0], qty, price, amount, fee,
                         profit_amt, profit_rate, odno)
        else:
            return _fail(f"알 수 없는 주문 유형: {action}")

        logger.info(f"[PAPER] {action.upper()} 체결 {code} {qty}주 @{price:,.0f} (No.{odno})")
        return {"rt_cd": "0", "msg_cd": "PAPER", "msg1": "가상투자 체결",
                "output": {"ODNO": odno, "ORD_TMD": datetime.now().strftime('%H%M%S')}}


def _fail(msg):
    logger.warning(f"[PAPER] 주문 거부: {msg}")
    return {"rt_cd": "1", "msg_cd": "PAPER_REJECT", "msg1": f"[가상투자] {msg}", "output": {}}


def _lookup_name(code):
    try:
        for key in ("stocks_kr", "etfs_kr"):
            for item in config.session.stock_data.get(key, []):
                if item.get('code') == code:
                    return item.get('name', code)
    except Exception:
        pass
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
    """일별 자산 스냅샷 기록(자산곡선·MDD 산출용). 같은 날 재호출 시 덮어쓴다."""
    try:
        _, output2 = get_domestic_balance()
        s = output2[0]
        cash = float(s['dnca_tot_amt'])
        stock = float(s['scts_evlu_amt'])
        _db().execute_query(
            "INSERT OR REPLACE INTO paper_equity (date, cash, stock_value, total) VALUES (?,?,?,?)",
            (datetime.now().strftime('%Y-%m-%d'), cash, stock, cash + stock))
    except Exception as e:
        logger.debug(f"[PAPER] 자산 스냅샷 실패: {e}")


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
    row = _db().execute_query(
        f"SELECT {_FILL_COLS} FROM paper_fills WHERE odno=? ORDER BY id DESC",
        (str(odno),), fetch='one')
    return _fill_row(row) if row else None


def get_equity_curve():
    rows = _db().execute_query(
        "SELECT date, cash, stock_value, total FROM paper_equity ORDER BY date", fetch='all') or []
    return [{"date": r[0], "cash": float(r[1]), "stock_value": float(r[2]), "total": float(r[3])}
            for r in rows]


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

    curve = [e['total'] for e in get_equity_curve()] or [seed, total]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v - peak) / peak * 100)

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
        cash = get_cash()
        if amount < 0 and cash + amount < 0:
            return False, f"출금액이 가상 현금({int(cash):,}원)을 초과합니다"
        new_seed = get_seed() + amount
        if new_seed <= 0:
            return False, "시드가 0 이하가 됩니다"
        _set_state('cash', cash + amount)
        _set_state('seed', new_seed)
        logger.info(f"[PAPER] 가상 {'입금' if amount >= 0 else '출금'} {abs(amount):,}원 "
                    f"(시드 {new_seed:,.0f} / 현금 {cash+amount:,.0f})")
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
        import jsonio
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
        for tbl in ("paper_positions", "paper_fills", "paper_equity", "paper_state"):
            _db().execute_query(f"DELETE FROM {tbl}")
        cleared = _clear_trade_history()
        seed = int(seed if seed is not None else getattr(config, 'PAPER_SEED_CAPITAL', 5_000_000))
        _set_state('seed', seed)
        _set_state('cash', seed)
        _set_state('started_at', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        logger.info(f"[PAPER] 가상 계좌 초기화 (시드 {seed:,}원, 매매 기록 삭제 "
                    f"{'완료' if cleared else '건너뜀'})")
    _clear_daily_baseline()
    _clear_restricted_stocks()
    return cleared
