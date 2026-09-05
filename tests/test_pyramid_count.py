"""피라미딩 증액 횟수가 상한을 지키는가.

[종전 구조] 증액 횟수를 최근 매수의 **사유 문자열**에서 정규식(`피라미딩 N차`)으로 뽑았다.
못 찾으면 0이다. 즉 기록이 유실되면(DB 쓰기 실패·수동 정리) 횟수가 0으로 읽혀 상한을
넘겨 계속 증액된다. 리스크를 키우는 동작에 fail-open 이었다.

증액은 보유수량의 50%씩이라 1 → 1.5 → 2.25 → 3.375 로 커진다. 횟수가 계속 0이면
여기서 멈추지 않고 한 종목이 계좌를 삼킨다(보유 종목 수 게이트는 같은 종목이라 무력).

운영 환경이 라즈베리파이 24시간 + 패키지 적용을 위한 잦은 재시작이라, 횟수는 재시작을
넘겨 살아남아야 한다 — 그래서 메모리가 아니라 trailing_stops.pyramid_count 에 둔다.
신규 진입 시 delete_trailing_stop 으로 함께 지워져 자동으로 0이 된다.
"""
import sqlite3

import pytest
from unittest.mock import patch

import config
from modules import db_manager
from modules.auto_trade import AutoTrader

CODE = "005930"
NAME = "삼성전자"


@pytest.fixture
def clean_db():
    db_manager.db.delete_trailing_stop(CODE)
    yield
    db_manager.db.delete_trailing_stop(CODE)


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.buy_halted = False
    t.initial_asset = 10_000_000
    t.portfolio_heat_amt = 0.0
    yield t


# ───────────────────────── 저장소 ─────────────────────────

def test_count_starts_at_zero_and_increments(clean_db):
    assert db_manager.db.get_pyramid_count(CODE) == 0
    assert db_manager.db.bump_pyramid_count(CODE, 0) is True
    assert db_manager.db.get_pyramid_count(CODE) == 1
    assert db_manager.db.bump_pyramid_count(CODE, 1) is True
    assert db_manager.db.get_pyramid_count(CODE) == 2


def test_count_survives_a_restart(clean_db):
    """[핵심] 라즈베리파이는 패키지 적용으로 수시로 재시작한다 — 메모리면 안 된다."""
    db_manager.db.bump_pyramid_count(CODE, 0)
    db_manager.db.bump_pyramid_count(CODE, 1)
    db_manager.db.close_all_connections()          # 재기동을 흉내 낸다
    assert db_manager.db.get_pyramid_count(CODE) == 2, "재시작에 증액 횟수가 사라졌다"


def test_new_position_resets_the_count(clean_db):
    """신규 진입은 delete_trailing_stop 을 거치므로 횟수도 함께 0이 되어야 한다."""
    db_manager.db.bump_pyramid_count(CODE, 0)
    db_manager.db.delete_trailing_stop(CODE)
    assert db_manager.db.get_pyramid_count(CODE) == 0, "이전 포지션의 횟수가 남았다"


def test_read_failure_is_not_reported_as_zero(clean_db):
    """[핵심] 조회 실패는 '0회'와 구분돼야 한다 — 0으로 읽으면 상한이 무력해진다."""
    with patch.object(db_manager.db, '_get_conn', side_effect=sqlite3.Error("boom")):
        assert db_manager.db.get_pyramid_count(CODE) == -1


def test_write_failure_is_reported(clean_db):
    with patch.object(db_manager.db, '_get_conn', side_effect=sqlite3.Error("disk full")):
        assert db_manager.db.bump_pyramid_count(CODE, 0) is False


# ───────────────────────── 증액 경로 ─────────────────────────

def _pyramid(trader, *, db_count=0, legacy_reason="", bump=True, analyze_ok=True):
    """증액 1회를 태우고 (주문 호출 Mock, bump 호출 Mock)을 돌려준다."""
    last_buy = {'reason': legacy_reason} if legacy_reason else None
    result = {'state': '보유', 'score': 5.0, 'ind': {'atr': 1000}}
    with patch.object(db_manager.db, 'get_pyramid_count', return_value=db_count), \
         patch.object(db_manager.db, 'bump_pyramid_count', return_value=bump) as bumped, \
         patch.object(trader.strategy, 'analyze_pyramid',
                      return_value=(analyze_ok, "피라미딩 1차")), \
         patch.object(config, 'USE_MARKET_FILTER', False), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=1000), \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value="ODNO1") as order:
        trader._try_pyramid_buy(CODE, NAME, 100, 70000, 12.0, result, last_buy,
                                is_market_open=True)
    return order, bumped


def test_unknown_count_blocks_the_increase(trader):
    """[핵심] 횟수를 모르면 증액하지 않는다 — 모르는 채로 노출을 키우면 안 된다."""
    order, _ = _pyramid(trader, db_count=-1)
    assert not order.called, "증액 횟수를 확인하지 못했는데 주문을 냈다"


def test_known_count_allows_the_increase(trader):
    """대조군 — 정상 상태에서는 증액이 나가야 한다(보류가 상시면 기능이 죽는다)."""
    order, _ = _pyramid(trader, db_count=0)
    assert order.called, "정상 상태인데 증액이 막혔다"


def test_db_count_is_passed_to_the_strategy(trader):
    """DB 횟수가 실제 판정에 쓰여야 한다 — 안 쓰면 상한이 걸리지 않는다."""
    result = {'state': '보유', 'score': 5.0, 'ind': {'atr': 1000}}
    with patch.object(db_manager.db, 'get_pyramid_count', return_value=2), \
         patch.object(db_manager.db, 'bump_pyramid_count', return_value=True), \
         patch.object(trader.strategy, 'analyze_pyramid',
                      return_value=(False, "상한 도달")) as analyze, \
         patch.object(config, 'USE_MARKET_FILTER', False), \
         patch.object(trader.order_manager, 'send_order') as order:
        trader._try_pyramid_buy(CODE, NAME, 100, 70000, 12.0, result, None,
                                is_market_open=True)
    assert analyze.call_args[0][3] == 2, f"판정에 넘긴 횟수가 틀렸다: {analyze.call_args}"
    assert not order.called


def test_legacy_marker_wins_when_larger(trader):
    """구 버전 포지션 호환 — 사유 마커가 더 크면 그쪽을 믿는다(보수적)."""
    result = {'state': '보유', 'score': 5.0, 'ind': {'atr': 1000}}
    with patch.object(db_manager.db, 'get_pyramid_count', return_value=0), \
         patch.object(db_manager.db, 'bump_pyramid_count', return_value=True), \
         patch.object(trader.strategy, 'analyze_pyramid',
                      return_value=(False, "상한")) as analyze, \
         patch.object(config, 'USE_MARKET_FILTER', False), \
         patch.object(trader.order_manager, 'send_order'):
        trader._try_pyramid_buy(CODE, NAME, 100, 70000, 12.0, result,
                                {'reason': "조건 만족 [피라미딩 3차]"}, is_market_open=True)
    assert analyze.call_args[0][3] == 3, "구 마커(3차)를 무시하고 0으로 봤다"


def test_count_is_recorded_before_the_order(trader):
    """[순서] 기록이 주문보다 앞서야 한다.

    반대면 '주문은 나갔는데 횟수는 그대로'가 되어 다음 주기에 같은 증액이 또 나간다.
    """
    order, bumped = _pyramid(trader, db_count=1)
    assert bumped.called and order.called
    assert bumped.call_args[0] == (CODE, 1), "기존 횟수 기준으로 올리지 않았다"


def test_write_failure_blocks_the_order(trader):
    """[핵심] 횟수를 기록하지 못하면 주문도 내지 않는다.

    기록 없이 증액하면 다음 주기에 같은 증액이 반복되어 상한이 무의미해진다.
    """
    order, _ = _pyramid(trader, db_count=0, bump=False)
    assert not order.called, "횟수 기록에 실패했는데 증액 주문을 냈다"


def test_blocked_increase_releases_the_heat_reservation(trader):
    """보류 시 선점한 히트 예산을 반납해야 한다 — 안 하면 예산이 새어 매수가 막힌다."""
    trader.portfolio_heat_amt = 0.0
    with patch.object(trader.risk_manager, 'portfolio_risk_budget_left', return_value=10_000_000):
        _pyramid(trader, db_count=0, bump=False)
    assert trader.portfolio_heat_amt == 0.0, "보류했는데 히트 예산이 선점된 채 남았다"


# ──────────────── 잃어버린 갱신(lost update) ────────────────
#
# bump_pyramid_count(code, expected) 는 이름과 달리 비교-교환이 아니었다. SQL 이
#     ON CONFLICT(code) DO UPDATE SET pyramid_count = excluded.pyramid_count
# 였고 expected 는 아무 데도 쓰이지 않았다.
#
# 호출부는 get_pyramid_count 로 읽고 → 시장 필터·히트 캡·수량 산정 등 여러 게이트를
# 지난 뒤 → 여기서 쓴다. 그 창 사이에 값이 올라가 있으면 이 쓰기가 그것을 **덮어 내린다**.
# 횟수가 뒤로 가면 상한이 한 칸 늘고, 증액은 보유수량의 50%씩 커지므로 그 한 칸이
# 그대로 노출로 남는다.
def test_이미_더_큰_기록을_덮어_내리지_않는다(clean_db):
    """읽은 뒤 다른 경로가 먼저 올린 상황 — 뒤늦은 쓰기는 거부되어야 한다."""
    assert db_manager.db.bump_pyramid_count(CODE, 0) is True      # → 1
    assert db_manager.db.bump_pyramid_count(CODE, 1) is True      # → 2

    # expected=0 을 들고 뒤늦게 도착한 쓰기(창 사이에 값이 2가 됐다).
    assert db_manager.db.bump_pyramid_count(CODE, 0) is False, (
        "낡은 expected 로 온 쓰기가 통과했다 — 횟수가 뒤로 간다")
    assert db_manager.db.get_pyramid_count(CODE) == 2, "기록이 덮여 내려갔다"


def test_같은_값으로_다시_쓰는_것도_거부한다(clean_db):
    """두 경로가 같은 expected 를 읽은 경우 — 한쪽만 성공해야 한다."""
    assert db_manager.db.bump_pyramid_count(CODE, 0) is True      # → 1
    assert db_manager.db.bump_pyramid_count(CODE, 0) is False     # 같은 1차를 또
    assert db_manager.db.get_pyramid_count(CODE) == 1


def test_거부되면_증액도_하지_않는다(clean_db, trader):
    """'기록 못 하면 주문 안 낸다'는 계약이 거부(False)에도 그대로 걸린다."""
    order, bumped = _pyramid(trader, db_count=0, bump=False)
    assert bumped.called, "기록 시도 자체가 없었다"
    assert not order.called, "횟수 기록이 거부됐는데 증액 주문이 나갔다"


def test_구버전_포지션의_따라잡기는_허용한다(clean_db):
    """사유 문자열에서 읽은 legacy_count 로 0인 행을 한 번에 올리는 정당한 경로.

    엄격한 CAS(`= expected`)를 쓰면 이 경로가 막힌다 — 그래서 단조 조건을 쓴다.
    """
    db_manager.db.update_highest_price(CODE, 70000)      # pyramid_count 없이 행만 생성
    assert db_manager.db.get_pyramid_count(CODE) == 0
    assert db_manager.db.bump_pyramid_count(CODE, 2) is True, "구버전 따라잡기가 막혔다"
    assert db_manager.db.get_pyramid_count(CODE) == 3


def test_행이_없으면_그대로_만든다(clean_db):
    assert db_manager.db.bump_pyramid_count(CODE, 0) is True
    assert db_manager.db.get_pyramid_count(CODE) == 1
