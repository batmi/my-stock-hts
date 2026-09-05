"""히트 패리티 — 백테스트의 오픈 리스크가 실매매와 같은 자를 쓰는가.

[왜 고정하나] 포트폴리오 히트 캡(`SYSTEM_MAX_PORTFOLIO_RISK`)은 백테스트 모듈이
 '재현 대상'으로 선언한 항목인데, 두 구현의 **산식이 달랐다.**

   실매매 engine.RiskManager.compute_portfolio_heat
     : 수량 × max(0, 매수가 − 유효손절선)          [유효손절선 = 손절 / BEP / TS 무장 상향]
   백테스트 run_portfolio 인라인 (2026-09-01 이전)
     : 수량 × 종가 × |진입 손절률|/100

 세 군데가 갈렸다 — ① 청산선이 아니라 **진입 손절률만** 봤다(TS 무장 상향·본전청산·
 이익보호를 반영하지 않아, 이미 이익이 잠긴 포지션도 최초 손절폭만큼 예산을 계속 먹었다),
 ② 기준이 **종가**라 손절선이 고정된 채 값만 올라도 히트가 부풀었다, ③ 그래서 백테스트
 히트가 실매매식의 **2.3배**였고, 캡이 배분액을 깎은 매수가 8장 합 799건이었다 —
 무동작 다이얼이 아니라 **모든 감사가 다른 세기의 캡 아래에서 돌고 있었다.**

 이 저장소는 같은 형태의 결함을 이미 여러 번 겪었다(진입 순위·사이징·거래비용).
 재구현은 갈라진다. 선언이 아니라 테스트가 붙들어야 한다.

[이 파일이 지키는 것]
 ① 같은 포지션에 두 구현이 **원 단위까지 같은 리스크**를 낸다 — 손절 대기·TS 무장·
    본전 상향 세 상태 모두에서.
 ② 백테스트의 손절선이 매도 경로와 같은 SSOT(_intraday_stop_level)에서 온다.
 ③ 기준이 매수가다 — 현재가로 되돌아가면 '추세가 잘 될수록 증액이 막히는' 성질이
    함께 돌아온다(2026-08-30 실운영에서 한국콜마 증액 206주기 차단).
"""
import inspect
import threading

import pytest

import config
from modules import portfolio_backtest as pbt
from modules.auto_trade import RiskManager, engine


class _Trader:
    def __init__(self):
        self._lock = threading.RLock()
        self.trailing_stop_cache = {}

    def log(self, *a, **k):
        pass


@pytest.fixture
def rm():
    t = _Trader()
    return RiskManager(t), t


def _live_heat(rm_pair, qty, buy, cur, high, sl_rate, atr):
    r, t = rm_pair
    t.trailing_stop_cache["X"] = high
    return r.compute_portfolio_heat(
        [{'pdno': "X", 'hldg_qty': str(qty), 'pchs_avg_pric': str(buy), 'prpr': str(cur)}],
        {"X": [{'qty': qty, 'stop_loss_rate': sl_rate}]},
        live_map={"X": {'sl_rate': sl_rate, 'atr': atr}})


def _bt_stop(buy, high, cur, sl_rate, atr):
    """백테스트가 그 포지션에 쓰는 청산선 — 매도 경로와 같은 산식으로 재현한다.

    (_intraday_stop_level 은 run_portfolio 안의 클로저라 직접 부를 수 없으므로,
     같은 조립을 여기서 세우고 ②의 소스 검사로 그 동일성을 따로 건다.)
    """
    s = config.SELL_STRATEGY
    cands = [buy * (1 + sl_rate / 100.0)]
    max_profit = (high - buy) / buy * 100
    if s.get("PROFIT_LOCK_USE", False):
        lock = engine.profit_lock_stop_rate(max_profit)
        if lock is not None:
            cands[0] = max(cands[0], buy * (1 + lock / 100.0))
    callback = engine.effective_callback(
        s["TRAILING_STOP_CALLBACK_RATE"],
        (atr * s["TRAILING_ATR_MULTIPLIER"] / high) * 100, max_profit)
    armed = max_profit >= engine.breakeven_activation_rate(
        atr, buy, s["TRAILING_STOP_CALLBACK_RATE"], engine.ts_activation_atr_mult(), True)
    if armed:
        cands.append(high * (1 - callback / 100.0))
    return max(cands)


@pytest.mark.parametrize("label,buy,cur,high,atr", [
    ("손절 대기",   10000.0, 9800.0,  10000.0, 600.0),
    ("이익 중·미무장", 10000.0, 11400.0, 11500.0, 600.0),
    ("TS 무장",     10000.0, 13000.0, 14000.0, 600.0),
])
def test_the_two_implementations_agree_to_the_won(rm, label, buy, cur, high, atr):
    """① 같은 포지션에 같은 리스크. 세 상태 모두에서."""
    qty = 10
    sl_rate = -(atr * config.SELL_STRATEGY["ATR_STOP_MULTIPLIER"] / buy) * 100
    live = _live_heat(rm, qty, buy, cur, high, sl_rate, atr)
    bt = qty * max(0.0, buy - _bt_stop(buy, high, cur, sl_rate, atr))
    assert live == pytest.approx(bt, abs=1.0), f"{label}: 실매매 {live:,.0f} vs 백테스트 {bt:,.0f}"


def test_backtest_heat_takes_the_stop_from_the_sell_path():
    """② 손절선은 매도 경로와 같은 SSOT에서 온다.

    여기가 갈리면 히트는 실제로 존재하지 않는 손절선을 가정한다 — 그 방향이 과소면
    캡이 느슨해지고, 과대면 추세가 잘 되는 종목의 증액이 막힌다. 둘 다 조용히 일어난다.
    """
    src = inspect.getsource(pbt.run_portfolio)
    block = src.split("heat_budget = None")[1][:1200]
    assert "_intraday_stop_level(" in block, "히트가 매도 경로의 청산선을 쓰지 않는다"
    assert 'pos["avg"]' in block, "기준이 매수가가 아니다"


def test_the_mark_basis_is_reachable_only_as_a_control():
    """③ 폐기된 정의는 대조군으로만 남는다 — 기본값이 되면 안 된다."""
    sig = inspect.signature(pbt.run_portfolio)
    assert sig.parameters["heat_basis"].default == "cost"


def test_profit_lock_is_seen_by_both(rm, monkeypatch):
    """이익 보호선도 양쪽이 함께 봐야 한다 — 토글을 안 보면 BEP 결함의 재판이다.

    이 선은 TS 무장 **전** 구간(MFE ≥ PROFIT_LOCK_MIN_MFE)에서만 걸린다. 히트가
    못 보면 이미 +25% 오른 포지션의 손절선을 실제보다 낮게 가정해 리스크를 과대
    계상하고, 그만큼 **승자의 증액이 캡에 막힌다** — 이번에 없앤 그 성질이다.
    현재 기본값은 OFF라 평상시엔 아무 일도 없지만, 켠 순간 갈라지면 아무도 모른다.
    """
    monkeypatch.setitem(config.SELL_STRATEGY, "PROFIT_LOCK_USE", True)
    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_MODE", "breakeven")
    # MFE +30% — 이익 보호선(≥25%)은 켜지고 TS 발동선(ATR 900이면 37.0%)은 아직 멀다.
    #  그 사이 구간이 정확히 이 선의 존재 이유다.
    buy, high, cur, qty, atr = 10000.0, 13000.0, 12500.0, 10, 900.0
    sl_rate = -(atr * config.SELL_STRATEGY["ATR_STOP_MULTIPLIER"] / buy) * 100
    mfe = (high - buy) / buy * 100

    assert engine.profit_lock_stop_rate(mfe) is not None, \
        "이 표본은 이익 보호선이 켜지는 구간이어야 한다"
    assert mfe < engine.breakeven_activation_rate(
        atr, buy, config.SELL_STRATEGY["TRAILING_STOP_CALLBACK_RATE"],
        engine.ts_activation_atr_mult(), True), "TS가 이미 무장하면 이 선을 재는 것이 아니다"
    on = _live_heat(rm, qty, buy, cur, high, sl_rate, atr)
    bt = qty * max(0.0, buy - _bt_stop(buy, high, cur, sl_rate, atr))
    assert on == pytest.approx(bt, abs=1.0)

    monkeypatch.setitem(config.SELL_STRATEGY, "PROFIT_LOCK_USE", False)
    off = _live_heat(rm, qty, buy, cur, high, sl_rate, atr)
    assert off > on, "토글을 꺼도 값이 같다 — 히트가 이익 보호선을 안 본다"


# ─────────── 재현 못 하는 기능이 켜지면 소리를 낸다 ───────────

def test_unmodeled_sell_toggles_are_detected(monkeypatch):
    """[조용한 실패 차단] 백테스트에 없는 청산이 켜지면 알려야 한다.

    익절·RSI 과열 익절·반익절·방어적 반매도는 decide_sell 에 아예 없다. 전부 기본
    OFF라 지금은 무해하지만, 켜는 순간 백테스트는 그 청산을 **한 번도 밟지 않은 채**
    그럴듯한 수익률을 내놓는다. 이 저장소가 이미 여러 번 겪은 형태다(히트의 BEP 토글·
    이익보호선, audit_exit_parity 의 time_stop_min 누락). 주석이 아니라 경고로 남긴다.
    """
    from modules import portfolio_backtest as pbt2
    # 선언된 기본값을 본다 — 살아 있는 config 는 다른 테스트가 정당하게 흔든다.
    declared = config.GlobalSettings.model_fields["SELL_STRATEGY"].default
    assert pbt2.unmodeled_sell_features(declared) == [], \
        "기본 설정에서 재현 불가 기능이 켜져 있다 — 지금까지의 감사 수치가 의심스럽다"

    for key, name in (("TAKE_PROFIT_RATE", "고정 익절"), ("TAKE_PROFIT_RSI", "RSI 과열 익절"),
                      ("HALF_TAKE_PROFIT_USE", "반익절"),
                      ("DEFENSIVE_HALF_SELL_USE", "방어적 반매도")):
        val = True if key.endswith("_USE") else 8.0
        assert name in pbt2.unmodeled_sell_features({key: val}), f"{name} 을 못 잡는다"


def test_the_warning_actually_fires(monkeypatch, caplog):
    """탐지만 하고 아무도 안 부르면 소용이 없다 — 유니버스 준비 시점에 울려야 한다."""
    import inspect
    from modules import portfolio_backtest as pbt2
    assert "warn_if_unmodeled()" in inspect.getsource(pbt2.prepare_universe), \
        "감사·메뉴가 모두 지나는 문에서 확인하지 않는다"

    monkeypatch.setitem(config.SELL_STRATEGY, "HALF_TAKE_PROFIT_USE", True)
    with caplog.at_level("WARNING"):
        assert pbt2.warn_if_unmodeled("테스트") == ["반익절"]
    assert any("반익절" in r.message for r in caplog.records)


def test_missing_live_hooks_are_announced_once():
    """실매매에 있는 게이트가 이 실행에 안 넘어왔으면 알린다 — 단, 프로세스당 한 번만.

    감사 도구 대부분이 daily_loss_limit·reentry_block·oversize_limit 를 넘기지 않는다
    (실측: 각각 1개 도구뿐). 셋 다 따로 측정돼 '무해'로 판정됐지만, '측정하고 무해로
    둔 것'과 '아무도 안 준 것'은 다르다. 주기마다 찍으면 로그가 죽으므로 1회로 묶는다.
    """
    from modules import portfolio_backtest as pbt2
    pbt2._HOOK_WARNED.clear()
    first = pbt2._warn_missing_live_hooks(None, False, None)
    assert "손절 후 재진입 차단" in first
    assert any("방어 모드" in m for m in first)
    assert pbt2._HOOK_WARNED, "한 번도 기록하지 않았다 — 다음 호출에서 또 찍는다"
    before = set(pbt2._HOOK_WARNED)
    pbt2._warn_missing_live_hooks(None, False, None)
    assert set(pbt2._HOOK_WARNED) == before, "같은 항목을 다시 등록했다"


def test_passing_the_hooks_silences_the_notice():
    """게이트를 실제로 넘기면 할 말이 없어야 한다 — 늘 시끄러우면 아무도 안 읽는다."""
    from modules import portfolio_backtest as pbt2
    pbt2._HOOK_WARNED.clear()
    assert pbt2._warn_missing_live_hooks(10.0, True, 1.5) == []


def test_인자를_안_넘겨도_설정에서_켜진_게이트는_없다고_하지_않는다(monkeypatch):
    """[2026-09-05] 없는 격차를 알리면 있는 격차까지 함께 안 믿게 된다.

    oversize_limit 은 인자를 안 넘기면 config.MAX_POSITION_OVERSHOOT(정본 1.3)으로
    켜진 채 돈다. 그런데 경고 호출이 그 해결보다 **앞**에 있어, 인자를 안 넘긴 모든
    실행이 늘 '최소 주문 금액 보정 없음'으로 찍혔다 — 실제로는 1.3 으로 동작하는데도.
    감사 도구 대부분이 이 인자를 넘기지 않으므로 사실상 상시 오보였다.
    """
    import inspect

    from modules import portfolio_backtest as pbt2

    src = inspect.getsource(pbt2.run_portfolio)
    warn_at = src.index("_warn_missing_live_hooks(")
    resolve_at = src.index('getattr(config, "MAX_POSITION_OVERSHOOT"')
    assert resolve_at < warn_at, (
        "경고가 oversize_limit 해결보다 먼저 불린다 — 켜져 있는 게이트를 '없다'고 알린다")


def test_설정값이_상한_1_이하면_그때는_알린다():
    """대조군 — 실제로 무동작인 값(≤1.0)일 때는 알려야 한다."""
    from modules import portfolio_backtest as pbt2
    pbt2._HOOK_WARNED.clear()
    assert "최소 주문 금액 보정" in pbt2._warn_missing_live_hooks(10.0, True, 1.0)
    pbt2._HOOK_WARNED.clear()
    assert "최소 주문 금액 보정" not in pbt2._warn_missing_live_hooks(10.0, True, 1.3)


def test_초과집행_폴백이_정본과_같다():
    """폴백 리터럴 1.0 은 MAX_POSITION_OVERSHOOT 이 생기기 전의 값이었다."""
    import inspect

    import config
    from modules import portfolio_backtest as pbt2

    src = inspect.getsource(pbt2.run_portfolio)
    assert 'getattr(config, "MAX_POSITION_OVERSHOOT", 1.3)' in src, (
        "폴백 리터럴이 정본과 다르다 — 키 이름이 바뀌면 폐기된 값이 되살아난다")
    assert config.MAX_POSITION_OVERSHOOT == 1.3
