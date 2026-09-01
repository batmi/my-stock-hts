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
