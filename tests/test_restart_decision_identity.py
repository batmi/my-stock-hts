"""[카오스] 죽었다 살아난 뒤에도 **같은 판정**을 내리는가.

[왜 이 파일이 따로 필요한가] test_restart_recovery.py 는 트레일링 최고가가 DB 왕복에서
살아남는지를 본다. 그러나 재기동이 실제로 위험한 이유는 최고가 하나가 아니다. 포지션의
청산 판정은 **여러 조각의 상태**가 모두 맞아야 재현된다.

    · trailing_stops.highest_price  — 샹들리에 청산선의 기준점
    · trailing_stops.pyramid_count  — 증액 여력(권위 소스)
    · trades.stop_loss_rate         — 진입 시 굳은 ATR 손절선
    · half_tp                       — 반익절 앵커(원칙상 OFF지만 상태는 남는다)

하나라도 유실되면 판정이 조용히 바뀐다. 특히 stop_loss_rate가 없으면 ATR 손절이
전역 고정 손절(-7%)로 떨어져, 변동성이 큰 종목이 좁은 폭에서 잘려 나간다.
라즈베리파이 1GB 운영에서 OOM 종료는 가정이 아니라 예정된 사건이므로, '값이 남는가'가
아니라 '판정이 같은가'를 직접 확인한다.

[방법] 상태를 만들고 → 인스턴스를 버리고(프로세스 사망 모사) → 새 인스턴스에서
복원 경로를 태운 뒤 → 같은 입력으로 청산 판정을 두 번 내려 **완전 일치**를 요구한다.
"""
import pytest

import config
from modules import db_manager
from modules.auto_trade import AutoTrader
from modules.auto_trade.engine import compute_trailing_stop, atr_stop_rate

CODE, NAME = "005930", "삼성전자"
BUY = 100_000.0
PEAK = 130_000.0
NOW = 124_000.0
ATR = 3_000.0


@pytest.fixture
def db():
    d = db_manager.db
    _clean(d)
    yield d
    _clean(d)


def _clean(d):
    d.delete_trailing_stop(CODE)
    try:
        d.delete_half_tp(CODE)
    except Exception:
        pass
    try:
        with d._get_conn() as con:
            con.execute("DELETE FROM trades WHERE code = ?", (CODE,))
    except Exception:
        pass


def _restart():
    """프로세스 사망 → 재기동. startup의 DB 복원 경로만 태운다."""
    AutoTrader._instance = None
    t = AutoTrader()
    assert not t.trailing_stop_cache, "새 인스턴스인데 캐시가 남아 있다"
    t.trailing_stop_cache = db_manager.db.get_all_trailing_stops()
    t.half_tp_cache = set(db_manager.db.get_all_half_tp() or [])
    return t


def _verdict(trader, sl_rate):
    """복원된 상태로 내리는 청산 판정."""
    high = trader.trailing_stop_cache.get(CODE, 0.0)
    ts = compute_trailing_stop(high, BUY, NOW, ind={'atr': ATR})
    loss_rate = (NOW - BUY) / BUY * 100
    return {
        'armed': ts['armed'],
        'triggered': ts['triggered'],
        'stop_price': round(ts['stop_price'], 4),
        'callback': round(ts['callback'], 6),
        'activation': round(ts['activation'], 6),
        'stop_hit': bool(sl_rate and loss_rate <= sl_rate),
    }


# ---------------------------------------------------------------------------
def test_verdict_is_identical_after_restart(db):
    """[핵심] 재기동 전후의 청산 판정이 완전히 같아야 한다."""
    db.update_highest_price(CODE, PEAK)
    db.insert_trade("현금매수(AUTO)", CODE, NAME, 10, str(BUY), "ODNO1",
                    order_status="체결", stop_loss_rate=atr_stop_rate(ATR, BUY))

    before_trader = AutoTrader()
    before_trader.trailing_stop_cache = db.get_all_trailing_stops()
    sl_before = _sl_from_db(db)
    before = _verdict(before_trader, sl_before)

    after_trader = _restart()
    sl_after = _sl_from_db(db)
    after = _verdict(after_trader, sl_after)

    assert sl_before == sl_after, f"손절률이 재기동에서 바뀌었다: {sl_before} → {sl_after}"
    assert before == after, f"판정이 재기동에서 바뀌었다:\n  전 {before}\n  후 {after}"


def _sl_from_db(d):
    """실매매가 쓰는 복원 경로 — 매수 기록의 수량가중 평균 손절률."""
    trades = d.get_buy_trades_for_current_holding(CODE) or []
    tq, ws = 0, 0.0
    for t in trades:
        try:
            q, s = int(float(t.get('qty') or 0)), float(t.get('stop_loss_rate') or 0.0)
        except (TypeError, ValueError):
            continue
        if q > 0 and s != 0.0:
            tq += q
            ws += q * s
    return round(ws / tq, 6) if tq else None


def test_missing_buy_record_degrades_the_stop_and_we_can_see_it(db):
    """[경보] 매수 기록이 없으면 ATR 손절선이 복원되지 않는다.

    이 경우 판정은 전역 고정 손절로 떨어진다. 코드가 이를 막지는 않으므로(막을 수도 없다),
    최소한 '복원 불가'가 관측 가능해야 한다 — 조용히 -7%로 바뀌는 것이 가장 위험하다.
    """
    db.update_highest_price(CODE, PEAK)          # 최고가만 있고 매수 기록은 없음
    trader = _restart()
    assert trader.trailing_stop_cache.get(CODE) == pytest.approx(PEAK)
    assert _sl_from_db(db) is None, "매수 기록이 없는데 손절률이 복원됐다 — 근거 없는 값이다"


def test_pyramid_count_survives_restart(db):
    """증액 여력이 리셋되면 이미 3차까지 태운 포지션에 다시 증액한다."""
    db.update_highest_price(CODE, PEAK)
    for i in range(3):
        assert db.bump_pyramid_count(CODE, i), f"{i+1}차 증액 기록 실패"

    assert db.get_pyramid_count(CODE) == 3
    _restart()
    assert db.get_pyramid_count(CODE) == 3, "재기동에서 피라미딩 차수가 유실됐다"


def test_pyramid_bump_writes_expected_plus_one(db):
    """bump는 expected+1을 **무조건** 기록한다(CAS가 아니다).

    [설계 확인] 인자 이름이 expected라 낙관적 잠금처럼 보이지만 비교 후 갱신은 하지 않는다.
    같은 종목의 증액은 종목당 워커 하나에서만 일어나 경합이 없고, 호출부가 주문 **전에**
    bump하므로 '기록만 되고 주문 실패'(기회 상실 = 안전) 쪽으로만 어긋난다.
    이 성질이 바뀌면(종목 병렬화 등) 중복 증액이 가능해지므로 계약을 고정해 둔다.
    """
    db.update_highest_price(CODE, PEAK)
    assert db.bump_pyramid_count(CODE, 0) is True
    assert db.get_pyramid_count(CODE) == 1
    assert db.bump_pyramid_count(CODE, 1) is True
    assert db.get_pyramid_count(CODE) == 2


def test_unknown_pyramid_count_is_not_zero(db):
    """[핵심 계약] 조회 실패는 -1이어야 한다 — '0회'로 읽히면 상한 없이 증액된다.

    호출부(trader._try_pyramid_buy)는 db_count < 0 이면 증액을 보류한다. 이 반환값
    규약이 깨지면 그 가드가 통째로 무력해지고, 증액이 보유수량의 50%씩 복리로 커져
    한 종목이 계좌를 삼킨다.
    """
    import sqlite3
    from unittest.mock import patch

    db.update_highest_price(CODE, PEAK)
    assert db.get_pyramid_count(CODE) == 0, "정상 조회는 0이어야 한다"

    with patch.object(type(db), '_get_conn', side_effect=sqlite3.OperationalError("boom")):
        assert db.get_pyramid_count(CODE) == -1, "조회 실패가 0으로 읽힌다 — 가드가 무력해진다"


def test_weighted_stop_survives_pyramided_position(db):
    """피라미딩된 포지션은 손절률이 수량가중 평균이다. 재기동에서 그대로여야 한다."""
    db.update_highest_price(CODE, PEAK)
    db.insert_trade("현금매수(AUTO)", CODE, NAME, 10, str(BUY), "O1",
                    order_status="체결", stop_loss_rate=-6.0)
    db.insert_trade("현금매수(AUTO)", CODE, NAME, 20, str(BUY * 1.1), "O2",
                    order_status="체결", reason="피라미딩 1차", stop_loss_rate=-12.0)

    expected = round((10 * -6.0 + 20 * -12.0) / 30, 6)
    assert _sl_from_db(db) == pytest.approx(expected)
    _restart()
    assert _sl_from_db(db) == pytest.approx(expected), "재기동에서 가중 손절률이 바뀌었다"


def test_restored_state_can_still_trigger_an_exit(db):
    """복원된 최고가로 실제 청산이 발동해야 한다 — 값만 살고 판정이 죽으면 무의미하다."""
    db.update_highest_price(CODE, PEAK)
    trader = _restart()
    high = trader.trailing_stop_cache[CODE]
    deep = high * 0.80                                   # 고점 대비 -20%
    ts = compute_trailing_stop(high, BUY, deep, ind={'atr': ATR})
    assert ts['armed'] and ts['triggered'], "복원 후 청산이 발동하지 않는다"
