"""공통 청산 판단 계층(modules/exit_rules) — 우선순위·유예·남은 규칙 차이·순수성.

실매매(engine.analyze_sell)와 백테스트(portfolio_backtest.decide_sell)는 이 함수를 같이
부른다. 두 어댑터의 종전 동작 동등성은 tests/test_exit_parity.py 가 지키고, 여기서는
체인 자체의 성질을 고정한다.
"""
import ast
import collections
import dataclasses
import inspect
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from modules import exit_rules as xr  # noqa: E402

BASE = dict(price=100.0, avg=100.0, high=100.0, profit_rate=0.0, holding_days=3,
            sl_rate=-7.0, state="상승", score=6.0, ema60=90.0, atr=0.0,
            roll_high_5=100.0, roll_high_10=100.0)
RULES = xr.ExitRules(use_atr=False, ts_breakeven=False, ts_act=10.0, ts_callback=5.0,
                     time_stop_days=15)


def ev(rules=RULES, **over):
    return xr.evaluate_exit(xr.ExitInputs(**dict(BASE, **over)), rules)


def test_hold_when_nothing_fires():
    d = ev()
    assert not d.sell and d.code is None


def test_price_stop_beats_trailing_and_time_stop():
    # 손절선 아래 + TS 조건 + 시간청산 조건이 동시에 선다 → 손절이 이긴다
    d = ev(price=90.0, profit_rate=-10.0, high=150.0, holding_days=30, roll_high_5=90.0)
    assert d.code == xr.STOP_LOSS


def test_stop_label_priority_lock_over_bep_over_atr():
    r = dataclasses.replace(RULES, bep_use=True, bep_activation=5.0, bep_stop=0.5,
                            profit_lock_use=True, profit_lock_min_mfe=8.0, profit_lock_giveback=0.5)
    # MFE +30% → 이익보호선 +15%가 BEP 0.5% 보다 높다
    d = ev(r, price=110.0, profit_rate=10.0, high=130.0)
    assert d.code == xr.PROFIT_LOCK and d.is_lock and not d.is_bep
    # MFE +6% (이익보호 문턱 미달) → BEP 만 올라온다
    d = ev(r, price=100.0, profit_rate=0.0, high=106.0)
    assert d.code == xr.BEP_STOP and d.is_bep
    # ATR 적용 손절이면 ATR 사유
    d = ev(dataclasses.replace(RULES, use_atr=True), price=90.0, profit_rate=-10.0, atr_applied=True)
    assert d.code == xr.ATR_STOP


def test_time_stop_grace_still_judges_trailing_stop():
    d = ev(price=100.0, profit_rate=-0.5, high=150.0, holding_days=20,
           roll_high_5=150.0, roll_high_10=150.0)
    assert d.code == xr.TRAILING_STOP and d.time_stop_due and d.time_stop_graced


def test_time_stop_grace_requires_history_and_buyish_state():
    kw = dict(profit_rate=-0.5, holding_days=20)
    assert ev(**kw).code is None                                     # 유예
    assert ev(roll_high_5=None, roll_high_10=None, **kw).code == xr.TIME_STOP  # 이력 부족 → 유예 없음
    assert ev(state="관망", **kw).code == xr.TIME_STOP               # 매수 계열 아님
    assert ev(dataclasses.replace(RULES, time_stop_strict=True), **kw).code == xr.TIME_STOP


def test_trend_exit_needs_structure_break_and_judged_state():
    assert ev(score=3.0).code is None                       # 60일선 위 — 점수만으론 안 판다
    assert ev(score=3.0, ema60=110.0).code == xr.TREND_EXIT
    assert ev(score=3.0, ema60=None, state="-").code is None  # 판정 불가 — 팔지 않는다
    lax = dataclasses.replace(RULES, require_judged=False)
    assert ev(lax, score=3.0, ema60=None, state="-").code == xr.TREND_EXIT


def test_state_sell_switch_matters_only_when_limit_is_non_positive():
    off = dataclasses.replace(RULES, sell_score_limit=0.0)
    assert ev(off, state="매도", score=0.0).code == xr.STATE_SELL           # 실매매: 항상
    bt = dataclasses.replace(off, state_sell_always=False)
    assert ev(bt, state="매도", score=0.0).code is None                     # 백테스트: 0 < 0 거짓
    assert ev(dataclasses.replace(RULES, state_sell_always=False), state="매도", score=0.0).code \
        == xr.STATE_SELL


def test_mr_grace_holds_trend_exit_inside_window():
    kw = dict(score=3.0, ema60=110.0, is_mr_holding=True, holding_days=5, profit_rate=-3.0)
    d = ev(**kw)
    assert not d.sell and d.mr_grace_applied
    # 유예 손실 한도(-7%) 밖 — 손절선(-20%)은 아직 멀어 추세 이탈이 걸린다
    assert ev(**dict(kw, profit_rate=-8.0, sl_rate=-20.0)).code == xr.TREND_EXIT


def test_no_bars_skips_bar_based_exits_but_keeps_price_exits():
    assert ev(score=3.0, ema60=110.0, has_bars=False).code is None
    assert ev(price=90.0, profit_rate=-10.0, has_bars=False).code == xr.STOP_LOSS


def test_take_profit_chain_and_half_sale_state_suggestion():
    r = dataclasses.replace(RULES, tp_rate=10.0, half_tp_use=True)
    d = ev(r, profit_rate=6.0)
    assert d.code == xr.HALF_TAKE_PROFIT and d.sell_ratio == 0.5 and d.marks_half_sold
    # 반익절 뒤 잔여는 익절로 체인을 끝내지 않는다 — 아래 손절·TS 가 계속 흐른다
    d = ev(r, price=112.0, profit_rate=12.0, already_half_sold=True, high=112.0)
    assert d.code is None
    d = ev(r, price=95.0, profit_rate=-8.0, already_half_sold=True, high=120.0)
    assert d.code in (xr.PROFIT_PRESERVE, xr.STOP_LOSS)


def test_rsi_take_profit_uses_super_momentum_threshold():
    r = dataclasses.replace(RULES, tp_rsi=75.0, super_use=True, super_score=8.0, super_w52=90.0,
                            super_tp_rsi=90.0)
    assert ev(r, rsi=80.0).code == xr.RSI_TAKE_PROFIT
    d = ev(r, rsi=80.0, score=8.5, w52_pos=95.0)
    assert d.code is None and d.is_super and d.actual_tp_rsi == 90.0


def test_exit_rules_is_pure():
    """[구조 가드] API·DB·알림·주문 모듈을 import 하지 않는다(산식 헬퍼는 engine 에서만)."""
    tree = ast.parse(inspect.getsource(xr))
    banned = {"api", "modules.db_manager", "modules.telegram_notify", "modules.journal_sync",
              "requests", "sqlite3"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
        else:
            continue
        assert not (set(names) & banned), names


def test_remaining_rule_differences_do_not_bite_on_default_backtest(monkeypatch):
    """독스트링의 주장: 기본 설정에서 남은 두 규칙 차이(require_judged·state_sell_always)는
    백테스트 결과를 바꾸지 않는다. 합성 유니버스의 모든 보유 판정에서 두 스위치를 실매매
    값으로 바꿔 다시 판정해 본다. (실데이터 측정은 아니다 — 크기를 재려면 감사 도구로.)"""
    import test_exit_parity as T
    from modules import portfolio_backtest as pbt

    dfs = {f"{k}{s}": T._synth(s, k) for k in ("trend", "down", "chop") for s in (1, 2, 3)}
    th = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
          "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
          "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "WEIGHTS": config.SCORING_WEIGHTS}
    status = pbt.precompute_status(dfs, th)
    dates = sorted({str(d) for df in dfs.values() for d in df["date"]})
    orig = xr.evaluate_exit
    cnt = collections.Counter()

    def spy(inp, rules):
        d = orig(inp, rules)
        cnt["calls"] += 1
        alt = orig(inp, dataclasses.replace(rules, require_judged=True, state_sell_always=True))
        if (alt.sell, alt.code) != (d.sell, d.code):
            cnt["differ"] += 1
        return d

    monkeypatch.setattr(xr, "evaluate_exit", spy)
    pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=4)
    assert cnt["calls"] > 200, "보유 판정이 거의 없다 — 표본이 무의미해졌다"
    assert cnt["differ"] == 0, cnt
