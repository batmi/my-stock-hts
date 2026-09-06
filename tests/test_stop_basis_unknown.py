"""매수 기록을 **못 읽었을 때** 손절선이 조용히 넓어지지 않는가.

[왜 중요한가] 포지션 크기는 진입 시점의 손절폭을 전제로 정해졌다. 사후에 손절을
넓히면 그 포지션의 실제 손실이 사이징이 가정한 상한을 넘는다 — build_sell_thresholds
독스트링이 그것을 명시적으로 금지한다. 그런데 배치 조회가 실패하면 `{code: []}` 를
돌려줘, 호출부가 "매수 기록이 전혀 없다"로 읽었다. 실측:

    전역 고정 손절률          : -7.0%
    기록으로 계산된 손절선    : -3.2%
    조회 실패(빈 목록) 손절선 : 전역값
    → 손절폭이 2.19배로 벌어진다

매도 엔진 자체는 일봉 ATR 로 복원해 버틴다(entry_atr_stop_rate). 그러나 **안전망 두
곳** — 미체결 매수 취소, 미관리 포지션 이탈 경보 — 은 주기 앞머리에서 돌아 일봉이 없다.
그 둘이 전역값을 보면 손절선을 이미 지났는데 마지막 안전망이 울지 않는다.
"""
import pytest

import config
from modules import db_manager
from modules.auto_trade import engine


# ─────────────────────── ① 산식 자체 ───────────────────────

def test_기록이_있으면_그_값이_손절선이다():
    """대조군 — 이 경로가 깨지면 아래 판정이 전부 무의미하다."""
    t = engine.build_sell_thresholds(
        rule=None, buy_trades=[{'qty': '50', 'stop_loss_rate': -3.2}])
    assert t.get("STOP_LOSS_RATE") == pytest.approx(-3.2)


def test_기록이_없으면_전역_고정폭으로_떨어진다():
    """전제를 못 박는다 — 이 사실 때문에 '못 읽음'과 '없음'을 갈라야 한다."""
    t = engine.build_sell_thresholds(rule=None, buy_trades=[])
    fell_back = float(t.get("STOP_LOSS_RATE",
                            config.SELL_STRATEGY["STOP_LOSS_RATE"]))
    assert fell_back == pytest.approx(config.SELL_STRATEGY["STOP_LOSS_RATE"])
    assert abs(fell_back) > 3.2, "전역폭이 기록값보다 좁으면 이 축의 전제가 다르다"


def test_직전_실측값이_있으면_전역폭으로_떨어지지_않는다():
    """fallback_atr_rate 는 기록이 없을 때만 쓰이고, 기록이 있으면 무시된다."""
    t = engine.build_sell_thresholds(rule=None, buy_trades=[], fallback_atr_rate=-3.2)
    assert t.get("STOP_LOSS_RATE") == pytest.approx(-3.2)

    t2 = engine.build_sell_thresholds(
        rule=None, buy_trades=[{'qty': '50', 'stop_loss_rate': -2.0}],
        fallback_atr_rate=-9.9)
    assert t2.get("STOP_LOSS_RATE") == pytest.approx(-2.0), \
        "기록이 있는데 복원값이 이겼다 — 우선순위가 뒤집혔다"


# ─────────────────────── ② 조회 실패의 답 ───────────────────────

def test_조회_실패는_기록_없음으로_답하지_않는다(monkeypatch):
    """`{code: []}` 는 '없다'이고, 실패는 '모른다'다([[unknown-vs-empty]])."""
    db = db_manager.db

    class _Boom:
        def cursor(self):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(type(db), '_get_conn', lambda self: _Boom())
    assert db.get_buy_trades_for_current_holdings(["005930"]) is None
    assert db.get_latest_buy_trades(["005930"]) is None


def test_보유가_없으면_실패가_아니다():
    """빈 종목 목록은 정상이다 — 이것까지 None 이면 호출부가 매번 경보한다."""
    assert db_manager.db.get_buy_trades_for_current_holdings([]) == {}
    assert db_manager.db.get_latest_buy_trades([]) == {}


# ─────────────────────── ③ 안전망이 같은 선을 본다 ───────────────────────

@pytest.fixture
def trader():
    from modules.auto_trade.trader import AutoTrader
    AutoTrader._instance = None
    t = AutoTrader()
    t.holding_risk_cache = {}
    yield t
    AutoTrader._instance = None


def test_안전망은_직전_주기의_실측_손절선을_이어받는다(trader):
    """이 파일의 요점 — 두 선이 갈리면 경보가 조용히 늦는다."""
    trader.holding_risk_cache = {"005930": {'sl_rate': -3.2, 'atr': 1000.0}}

    without = trader._effective_stop_loss_rate([], rule=None)
    with_cache = trader._effective_stop_loss_rate(
        [], rule=None, fallback_atr_rate=trader._last_known_sl_rate("005930"))

    assert without == pytest.approx(config.SELL_STRATEGY["STOP_LOSS_RATE"])
    assert with_cache == pytest.approx(-3.2), \
        "안전망이 엔진과 다른 선을 본다 — 손절선을 지나도 울지 않는다"


def test_캐시가_없으면_종전_경로로_돌아간다(trader):
    """모르면 안 건드린다 — 없는 값을 지어내지 않는다."""
    assert trader._last_known_sl_rate("000660") is None


def test_양수_캐시값은_손절선이_아니다(trader):
    """이익 잠김(TS 무장 등)으로 선이 매수가 위로 올라간 경우. 손절률로 쓰면 안 된다."""
    trader.holding_risk_cache = {"005930": {'sl_rate': 1.5}}
    assert trader._last_known_sl_rate("005930") is None


def test_경보_경로가_실제로_그_값을_넘긴다():
    """[가드] 인자를 안 넘기면 위 함수가 있어도 없는 것과 같다."""
    import inspect
    from modules.auto_trade.trader import AutoTrader

    for fn in (AutoTrader._alert_unmanaged_stop,
               AutoTrader._cancel_pending_buy_on_stop_loss):
        src = inspect.getsource(fn)
        assert "_last_known_sl_rate" in src, \
            f"{fn.__name__} 이 직전 실측 손절선을 넘기지 않는다 — 전역 고정폭으로 떨어진다"
