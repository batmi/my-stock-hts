"""[카오스] 재기동 후 트레일링 스탑 최고가가 살아남는가.

주청산 수단이 샹들리에 TS인데, 최고가는 프로세스 메모리(trailing_stop_cache)와 DB
(trailing_stops)에 이중으로 산다. 재기동 시 DB에서 복원되지 않으면 TS가 리셋되어,
이미 벌어 둔 이익을 방어선 없이 반납한다. 반대로 청산한 종목의 최고가가 남아 있으면
재진입 직후 TS가 오발동해 신규 포지션이 즉시 잘린다. 양방향 모두 실계좌에서 위험하다.

여기서는 DB 왕복과 그 결과가 실제 청산 판정(engine.compute_trailing_stop)을 어떻게
바꾸는지까지 확인한다. '값이 저장된다'가 아니라 '판정이 달라진다'를 봐야 의미가 있다.
"""
import pytest

import config
from modules import db_manager
from modules.auto_trade import AutoTrader
from modules.auto_trade.engine import compute_trailing_stop

CODE = "005930"
BUY = 100_000.0
PEAK = 130_000.0        # 최고 수익률 +30%
NOW = 124_000.0         # 고점 대비 -4.6% (기본 콜백 5% 미만 → 아직 청산 아님)


@pytest.fixture
def db():
    d = db_manager.db
    d.delete_trailing_stop(CODE)
    yield d
    d.delete_trailing_stop(CODE)


def _ts(highest, current=NOW):
    """ATR 동적 콜백을 배제하고 기본 콜백만으로 판정한다(재현성 확보)."""
    return compute_trailing_stop(highest, BUY, current, ind=None,
                                 ts_activation=10.0, ts_callback=5.0, use_atr_stop=False)


# ---------------------------------------------------------------------------
# 1. 재기동 복원 왕복
# ---------------------------------------------------------------------------
def test_highest_price_survives_restart(db):
    """재기동 시 캐시 로드 경로(get_all_trailing_stops)가 최고가를 그대로 돌려준다."""
    db.update_highest_price(CODE, PEAK)

    restored = db.get_all_trailing_stops()
    assert CODE in restored, "재기동 캐시 로드에 종목이 없다 — 최고가가 유실된다"
    assert restored[CODE] == pytest.approx(PEAK)
    assert db.get_highest_price(CODE) == pytest.approx(PEAK)


def test_fresh_instance_starts_empty_and_is_filled_from_db(db):
    """새 프로세스의 캐시는 비어 있고, DB 값으로 채워져야 한다(복원 계약)."""
    db.update_highest_price(CODE, PEAK)

    AutoTrader._instance = None
    trader = AutoTrader()
    assert not trader.trailing_stop_cache, "새 인스턴스인데 캐시가 비어 있지 않다"

    trader.trailing_stop_cache = db.get_all_trailing_stops()   # startup의 복원 동작
    assert trader.trailing_stop_cache.get(CODE) == pytest.approx(PEAK)


def test_highest_price_is_monotonic(db):
    """최고가는 단조 증가여야 한다 — 낮은 값이 들어와도 방어선이 내려가면 안 된다."""
    db.update_highest_price(CODE, PEAK)
    db.update_highest_price(CODE, BUY * 1.05)      # 하락 중 잘못된 갱신 시도
    assert db.get_highest_price(CODE) == pytest.approx(PEAK), \
        "최고가가 내려갔다 — TS 방어선이 후퇴한다"


# ---------------------------------------------------------------------------
# 2. 복원 실패가 실제 판정을 바꾸는가 (값이 아니라 결과로 확인)
# ---------------------------------------------------------------------------
def test_lost_highest_retreats_the_stop_line():
    """얕은 하락 — 무장은 유지되지만 방어선이 뒤로 물러난다.

    복원 실패 시 최고가는 0이 되고, 다음 주기에 현재가로 다시 잡힌다
    (trader._check_sell_conditions: highest_price == 0.0 → current_price).
    """
    restored, lost = _ts(PEAK), _ts(NOW)
    assert restored['armed'] and lost['armed'], "이 시나리오는 양쪽 다 무장 상태여야 한다"

    giveback = (restored['stop_price'] - lost['stop_price']) / BUY * 100
    assert giveback > 5.0, \
        f"최고가 유실로 청산선이 {giveback:.1f}%p만 후퇴한다 — 시나리오를 재검토할 것"


def test_lost_highest_disarms_after_deep_pullback():
    """깊은 하락 — 복원되면 즉시 청산되지만, 유실되면 무장조차 되지 않아 방치된다."""
    shallow_profit = BUY * 1.05     # 고점 대비 -19%, 진입가 대비 +5%

    restored = _ts(PEAK, shallow_profit)
    assert restored['triggered'] is True, "복원 상태라면 이 하락에서 청산돼야 한다"

    lost = _ts(shallow_profit, shallow_profit)
    assert lost['armed'] is False, \
        "최고가를 잃으면 수익률이 발동 기준(10%) 미만이라 무장되지 않는다"
    assert lost['triggered'] is False, "무장되지 않았는데 청산이 걸렸다 — 산식 확인 필요"


def test_restored_highest_triggers_exit_when_price_drops():
    """고점 대비 콜백을 넘겨 하락하면, 복원된 최고가에서만 청산이 걸린다."""
    deep = BUY * 1.20   # +20%로 하락 (고점 130,000 대비 -7.7%)
    assert _ts(PEAK, deep)['triggered'] is True, "복원 상태에서 청산이 걸리지 않았다"
    assert _ts(deep, deep)['triggered'] is False, "최고가를 잃으면 청산이 걸리지 않는다(이익 반납)"


# ---------------------------------------------------------------------------
# 3. 반대 방향 위험 — 청산한 종목의 최고가가 남으면 재진입이 즉시 잘린다
# ---------------------------------------------------------------------------
def test_stale_highest_is_removed_on_exit(db):
    db.update_highest_price(CODE, PEAK)
    db.delete_trailing_stop(CODE)

    assert db.get_highest_price(CODE) is None, "청산 후에도 최고가가 남아 있다"
    assert CODE not in db.get_all_trailing_stops(), \
        "재기동 캐시에 청산된 종목의 최고가가 실린다"


def test_stale_highest_would_kill_new_position(db):
    """정리하지 않으면 왜 위험한지 — 잔존 최고가로 신규 포지션이 즉시 청산 판정된다."""
    db.update_highest_price(CODE, PEAK)

    new_entry = BUY * 1.01      # 신규 진입 직후, 거의 무손익
    stale = compute_trailing_stop(db.get_highest_price(CODE), new_entry, new_entry,
                                  ind=None, ts_activation=10.0, ts_callback=5.0,
                                  use_atr_stop=False)
    assert stale['triggered'] is True, \
        "잔존 최고가가 신규 포지션을 즉시 청산시키지 않는다면 이 방어의 근거가 사라진다"

    db.delete_trailing_stop(CODE)
    clean = compute_trailing_stop(db.get_highest_price(CODE) or new_entry, new_entry,
                                  new_entry, ind=None, ts_activation=10.0,
                                  ts_callback=5.0, use_atr_stop=False)
    assert clean['triggered'] is False, "정리 후에도 신규 포지션이 청산된다"


# ---------------------------------------------------------------------------
# 4. 가상투자와 실계좌의 최고가가 섞이지 않는가
# ---------------------------------------------------------------------------
def test_paper_and_real_trailing_stops_are_isolated(tmp_path):
    """trailing_stops는 code가 PK라, 파일을 공유하면 실계좌 최고가가 가상 포지션에 섞인다."""
    d = db_manager.db
    origin = d.db_path
    try:
        d.delete_trailing_stop(CODE)
        d.update_highest_price(CODE, PEAK)          # 실계좌 쪽 기록

        paper = str(tmp_path / "paper_trading.db")
        d.switch_path(paper)
        assert d.get_highest_price(CODE) is None, \
            "가상투자 DB에서 실계좌 최고가가 보인다 — 포지션이 섞인다"

        d.update_highest_price(CODE, BUY * 1.5)     # 가상 쪽 기록

        d.switch_path(origin)
        assert d.get_highest_price(CODE) == pytest.approx(PEAK), \
            "실계좌로 돌아왔는데 가상투자 기록이 덮어썼다"
    finally:
        d.switch_path(origin)
        d.delete_trailing_stop(CODE)
