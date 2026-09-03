"""청산 패리티 — 백테스트 decide_sell vs 실매매 engine.analyze_sell.

[왜 테스트로 고정하나] 청산 판정은 **두 곳에 따로 구현**돼 있다.

    실매매   : engine.DefaultStrategy.analyze_sell   (222줄)
    백테스트 : portfolio_backtest.decide_sell

 두 구현을 대조하는 하네스는 이미 있었다(tools/audit_exit_parity.py). 그러나 그건
 **사람이 기억해서 돌려야 하는 스크립트**이고, FinanceDataReader 로 실데이터를 받으므로
 네트워크 없이는 돌지 않는다. 즉 지금까지 어긋남은 '누군가 생각나서 돌려본 날'에만
 잡혔다. 실제로 이 저장소는 같은 형태의 결함을 반복해서 겪었다 — 시간청산 시계 리셋,
 히트 캡 산식, BEP 토글 무시. 셋 다 두 구현이 갈라진 자리였다.

 재구현은 갈라진다. 선언(주석의 "[동기화]")이 아니라 테스트가 붙들어야 한다.
 그래서 같은 하네스를 **합성 일봉 위에서 네트워크 없이** 돌려 매 테스트 실행마다 건다.

[무엇을 지키는가]
 ① 같은 입력에 두 구현의 청산 판정(청산 여부 + 사유 카테고리)이 완전히 같아야 한다.
 ② 기본값이 OFF인 청산 스위치까지 켜고 잰다. 기본 설정만 돌리면 두 구현이 그 분기를
    아예 타지 않아 '불일치 0'이 무의미해진다 — audit_exit_parity 의 --set 도움말이
    경계하는 그 무의미한 0이다. 실제로 이 테스트를 처음 붙였을 때 시간청산 문턱
    (TIME_STOP_MIN_PROFIT_RATE)을 켠 조합에서 하네스가 키를 빠뜨려 거짓 불일치가 났고,
    그 자리에서 build_sell_cfg SSOT 로 고쳤다.
 ③ 표본이 실제로 여러 청산 사유를 밟았는지 함께 못박는다. 합성 데이터가 언젠가
    '아무도 청산되지 않는' 계열로 바뀌면 ①이 공짜로 통과하기 때문이다.
 ④ decide_sell 이 읽는 cfg 키를 build_sell_cfg 가 전부 채우는지 AST로 대조한다.
    키가 빠지면 decide_sell 은 조용히 자기 기본값으로 돌아가고, 실매매만 새 값으로
    돈다 — 코드가 아니라 하네스 때문에 생기는 불일치의 온상이다.

[이 테스트의 범위 밖 — 알고 남겨둔 것]
 · 익절·반익절·수익보존·RSI과열·방어적 반매도: 실매매에만 있고 decide_sell 에는 없다.
   전부 기본 OFF(추세추종 기조)라 현재 매매에는 영향이 없지만, **켜면 백테스트가
   재현하지 못한다.** 이 테스트는 그 스위치를 켜지 않는다 — 켜면 '알려진 미구현'이
   불일치로 잡혀 회귀 신호가 묻힌다.
 · 관측 차이(백테스트는 그날 고가를 알고 실매매는 주기마다 본 현재가만 안다)는 구조적
   한계이지 결함이 아니다. 여기서는 양쪽에 같은 high 를 주는 '① 로직 대조'만 건다.
   크기를 재려면 tools/audit_exit_parity.py 를 실데이터로 돌린다.
"""
import ast
import copy
import inspect
from collections import Counter

import numpy as np
import pandas as pd
import pytest

import config
from modules import backtest, portfolio_backtest as pbt
from tools import audit_exit_parity as aep

# 합성 일봉 길이. 실매매는 CHART_LOOKBACK_DAYS(≈494 거래일)치를 받아 지표를 계산하므로,
#  하네스가 짧은 프레임을 주면 EMA120 워밍업 부족 때문에 판정이 갈린다 — 코드 차이가
#  아니라 데이터 차이다. MIN_HISTORY 이후부터만 비교해 그 오염을 배제한다.
BARS = 560
MIN_HISTORY = 380
STEP = 17


def _synth(seed, kind):
    """결정적 합성 일봉. 국면을 나눠 청산 사유가 고루 발현하도록 만든다."""
    rng = np.random.default_rng(seed)
    px, closes = 10000.0, []
    for i in range(BARS):
        if kind == "trend":        # 상승 후 급락 → 트레일링스탑이 발현
            drift = 0.0016 if i < BARS * 0.75 else -0.010
        elif kind == "down":       # 지속 하락 → 손절·점수하락
            drift = -0.0022
        else:                      # 횡보 → 시간청산
            drift = 0.0008 * np.sin(i / 23.0)
        px = max(px * (1 + drift + rng.normal(0, 0.018)), 500.0)
        closes.append(px)

    c = np.array(closes)
    o = c * (1 + rng.normal(0, 0.004, BARS))
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.007, BARS)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.007, BARS)))
    df = pd.DataFrame({
        "date": pd.bdate_range("2022-01-03", periods=BARS).strftime("%Y%m%d"),
        "open": o, "high": h, "low": l, "close": c,
        "volume": rng.integers(50_000, 500_000, BARS),
    })
    # 수급 축은 양쪽 False 로 고정한다. 살려두면 실매매 경로만 KIS 조회를 타고(테스트에서는
    #  차단돼 실패) 축 하나가 조용히 꺼진 채로 '일치'가 나온다.
    df["smart_money"] = False
    df = backtest.compute_price_indicators(df)
    df["roll_high_5"] = df["high"].rolling(5, min_periods=1).max()
    df["roll_high_10"] = df["high"].rolling(10, min_periods=1).max()
    return df


@pytest.fixture(scope="module")
def universe():
    dfs = {"TREND": _synth(1, "trend"), "DOWN": _synth(2, "down"), "CHOP": _synth(3, "chop")}
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    return dfs, pbt.precompute_status(dfs, thresholds), thresholds


@pytest.fixture(autouse=True)
def _no_smart_money(monkeypatch):
    """실매매 경로의 수급 조회를 끈다 (백테스트 쪽은 smart_money=False 컬럼으로 고정)."""
    from modules import analysis
    monkeypatch.setattr(analysis, "check_smart_money_turnaround",
                        lambda code, is_overseas=False: (False, ""))


# 기본 OFF 스위치까지 켜서 각 분기를 실제로 밟게 한다.
#  (익절 계열은 decide_sell 미구현 — 모듈 독스트링의 '범위 밖' 참조)
OVERLAYS = {
    "기본": {},
    "BEP ON": {"USE_BREAK_EVEN_STOP": True},
    "이익보호 ON": {"PROFIT_LOCK_USE": True, "PROFIT_LOCK_MIN_MFE": 8.0,
                    "PROFIT_LOCK_GIVEBACK": 0.3},
    "TS 손익분기 무장": {"TS_ACTIVATION_MODE": "breakeven"},
    "TS ATR 배수 1.2": {"TRAILING_ATR_MULTIPLIER": 1.2},
    "시간청산 문턱 3%": {"TIME_STOP_MIN_PROFIT_RATE": 3.0},
    "시간청산 8일": {"TIME_STOP_DAYS": 8},
}

# 위 중에서 이 표본의 **청산 사유 분포를 실제로 바꾸는** 것들 (실측).
#  나머지 셋(TS 손익분기 무장 / 고정 발동률 / SELL_SCORE 상향)은 판정 경로는 타지만
#  결과를 바꾸지 못한다 — 실효 콜백이 max(고정, ATR×배수)이고 통상 ATR 항이 지배하기
#  때문이다. 그래서 트레일링 축을 실제로 흔드는 손잡이는 고정 콜백·발동률이 아니라
#  TRAILING_ATR_MULTIPLIER 다(config.SELL_STRATEGY 주석·미검증 다이얼 감사와 같은 결론).
#  이 사실을 테스트가 알고 있어야, 나중에 '무동작 다이얼'을 흔들며 검증했다고 착각하지 않는다.
BITING_OVERLAYS = ("BEP ON", "이익보호 ON", "TS ATR 배수 1.2",
                   "시간청산 문턱 3%", "시간청산 8일")


@pytest.fixture
def sell_strategy():
    """SELL_STRATEGY 를 덮어쓰고 테스트가 끝나면 되돌린다 (모듈 전역이라 누출 방지)."""
    saved = copy.deepcopy(config.SELL_STRATEGY)
    yield config.SELL_STRATEGY
    config.SELL_STRATEGY.clear()
    config.SELL_STRATEGY.update(saved)


@pytest.mark.parametrize("label", list(OVERLAYS))
def test_exit_decisions_match_live(universe, sell_strategy, label):
    """같은 입력 → 두 구현이 같은 청산 판정을 내려야 한다."""
    dfs, status, thresholds = universe
    sell_strategy.update(OVERLAYS[label])

    n, mismatches, cross = aep.run(dfs, status, step=STEP, live_high=False,
                                   thresholds=thresholds, min_history=MIN_HISTORY)

    assert n >= 100, f"[{label}] 비교 표본이 {n}건뿐 — 하네스가 사실상 아무것도 재지 않았다"

    if mismatches:
        head = "\n".join(
            f"  {m['code']} {m['date']} {m['day']}일 수익{m['profit']:+.1f}% "
            f"최고{m['maxprofit']:+.1f}% 손절{m['sl']:.1f}% BEP={m['bep']} "
            f"→ 백테스트 '{m['backtest']}' vs 실매매 '{m['live']}'"
            for m in mismatches[:5])
        pytest.fail(f"[{label}] 청산 판정 불일치 {len(mismatches)}/{n}건\n{head}")

    # 표본이 실제로 청산을 밟았는지 — '아무도 안 팔려서' 통과하는 것을 막는다.
    exits = sum(v for (b_cat, _live), v in cross.items() if b_cat != "보유")
    assert exits >= 5, f"[{label}] 청산이 {exits}건뿐 — 표본이 분기를 밟지 못했다"


def test_overlays_actually_reach_distinct_branches(universe, sell_strategy):
    """스위치를 켠 조합이 기본 조합과 **다른 사유 분포**를 만들어야 한다.

    오버레이가 아무 분기도 바꾸지 못하면 위 파라미터 테스트는 같은 것을 여섯 번 재는
    셈이고, 그 축은 검증되지 않은 채 초록으로 남는다.
    """
    dfs, status, thresholds = universe

    def distribution(overlay):
        saved = copy.deepcopy(config.SELL_STRATEGY)
        try:
            config.SELL_STRATEGY.update(overlay)
            _n, _m, cross = aep.run(dfs, status, step=STEP, live_high=False,
                                    thresholds=thresholds, min_history=MIN_HISTORY)
            dist = Counter()
            for (b_cat, _live), v in cross.items():
                dist[b_cat] += v
            return dist
        finally:
            config.SELL_STRATEGY.clear()
            config.SELL_STRATEGY.update(saved)

    base = distribution({})
    for label in BITING_OVERLAYS:
        assert distribution(OVERLAYS[label]) != base, \
            f"'{label}' 오버레이가 청산 분포를 바꾸지 못했다 — 그 축은 실제로 검증되지 않는다"


def test_build_sell_cfg_covers_every_key_decide_sell_reads():
    """decide_sell 이 읽는 cfg 키를 build_sell_cfg 가 전부 채우는가.

    빠진 키는 예외를 내지 않고 decide_sell 의 기본값으로 조용히 대체된다. 그러면
    백테스트만 옛 규칙으로 돌고 실매매는 config 를 따라가, 코드가 멀쩡한데도 불일치가
    난다. 실제로 time_stop_min 이 이 형태로 빠져 있었다.
    """
    tree = ast.parse(inspect.getsource(pbt.decide_sell))
    read = {
        node.args[0].value
        for node in ast.walk(tree)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "c" and node.args
            and isinstance(node.args[0], ast.Constant))
    }
    missing = read - set(pbt.build_sell_cfg())
    assert not missing, f"build_sell_cfg 가 채우지 않는 키: {sorted(missing)}"


def test_trailing_atr_multiplier_fallback_matches_live():
    """폴백 리터럴이 실매매와 같아야 한다.

    키가 있는 한 발현하지 않지만, 발현하는 날에는 백테스트만 다른 콜백으로 돌고
    아무 신호도 나지 않는다. 종전 백테스트 쪽 폴백은 3.0, 실매매는 3.5였다.
    """
    assert pbt.build_sell_cfg({})["ts_atr_mult"] == 3.5
    assert pbt.build_sell_cfg({})["ts_act"] == 10.0
    assert pbt.build_sell_cfg({})["sell_score_limit"] == 4.0


# ==========================================================
# 시간청산 '유예'가 트레일링 스탑을 삼키지 않는다 (2026-09-01)
#
# [배경] 두 구현 모두 청산 판정이 if/elif 체인이었다. 시간청산 조건이 참인데 유예되면
# (매수 계열 상태 + 상방 모멘텀) reason 이 빈 채로 체인이 끝나, 그날 트레일링 스탑 판정이
# 통째로 건너뛰어졌다 — 승자가 무너지는 순간 청산을 미루는 방향이다. 바로 위 반익절 절이
# 같은 형태로 이미 한 번 고쳐졌는데 여기만 남아 있었다.
#
# [왜 지금까지 안 드러났나] 유예 조건('최근 5일 고점 ≥ 10일 고점' = 고점을 최근에 찍었다)과
# TS 발동 조건('고점 대비 콜백만큼 하락')이 논리적으로 거의 배타적이다. 백테스트 10년에서
# 삼킨 사례 0건이고, TIME_STOP_MIN_PROFIT_RATE 를 5·10 으로 올려 유예를 146·417건까지
# 늘려도 0이었다. 그래서 이 수정은 수치를 바꾸지 않는다 — 함정만 없앤다.
# 그리고 백테스트·실매매가 **같은 결함을 공유**했으므로 청산 패리티 감사로는 못 잡혔다.
# ==========================================================

def test_time_stop_grace_does_not_swallow_the_trailing_stop():
    """[핵심] 유예된 날에도 TS 는 판정된다."""
    from modules import portfolio_backtest as pb

    cfg = pb.build_sell_cfg()
    cfg.update({"use_time_stop": True, "time_stop_days": 15, "time_stop_min": 0.0,
                "ts_breakeven": False, "ts_act": 10.0, "ts_callback": 5.0,
                "use_atr": False})
    # 유예가 서는 상태: 보유 20일 · 손실 중 · 매수 계열 · 상방 모멘텀(5일고 = 10일고)
    # 동시에 TS 도 서는 상태: MFE +50% 인데 고점 대비 33% 하락
    sell, reason = pb.decide_sell(
        price=100.0, high=150.0, avg=100.5, sl_rate=-30.0, atr_applied=False,
        is_bep=False, holding_days=20, state="상승", state_reason="", raw_score=6.0,
        sell_check=6.0, ema60=90.0, atr=0.0,
        roll_high_5=150.0, roll_high_10=150.0, cfg=cfg)

    assert sell and reason == "트레일링스탑", \
        f"시간청산 유예가 트레일링 스탑을 삼켰다 (sell={sell}, reason={reason!r})"


def test_the_grace_itself_still_works():
    """유예는 살아 있어야 한다 — TS 가 안 걸리면 그날은 팔지 않는다."""
    from modules import portfolio_backtest as pb

    cfg = pb.build_sell_cfg()
    cfg.update({"use_time_stop": True, "time_stop_days": 15, "time_stop_min": 0.0,
                "ts_breakeven": False, "ts_act": 10.0, "ts_callback": 5.0,
                "use_atr": False})
    sell, reason = pb.decide_sell(
        price=100.0, high=100.5, avg=100.5, sl_rate=-30.0, atr_applied=False,
        is_bep=False, holding_days=20, state="상승", state_reason="", raw_score=6.0,
        sell_check=6.0, ema60=90.0, atr=0.0,
        roll_high_5=100.5, roll_high_10=100.5, cfg=cfg)

    assert not sell, f"유예가 사라졌다 — 상방 모멘텀이 살아있는데 팔았다 ({reason!r})"


def test_time_stop_still_fires_without_grace():
    """상방 모멘텀이 없으면 종전대로 시간청산된다."""
    from modules import portfolio_backtest as pb

    cfg = pb.build_sell_cfg()
    cfg.update({"use_time_stop": True, "time_stop_days": 15, "time_stop_min": 0.0,
                "ts_breakeven": False, "ts_act": 10.0, "ts_callback": 5.0,
                "use_atr": False})
    sell, reason = pb.decide_sell(
        price=100.0, high=100.5, avg=100.5, sl_rate=-30.0, atr_applied=False,
        is_bep=False, holding_days=20, state="상승", state_reason="", raw_score=6.0,
        sell_check=6.0, ema60=90.0, atr=0.0,
        roll_high_5=95.0, roll_high_10=100.5, cfg=cfg)

    assert sell and reason == "시간청산", f"시간청산이 안 걸렸다 ({sell}, {reason!r})"


def test_the_live_path_no_longer_uses_elif_for_the_trailing_stop():
    """실매매 쪽도 같은 형태였다 — 두 구현이 같은 결함을 공유해 패리티로는 못 잡혔다."""
    import inspect
    from modules.auto_trade import engine

    src = inspect.getsource(engine.DefaultStrategy.analyze_sell)
    # 주석에는 옛 형태가 설명으로 남아 있다 — 코드 줄만 본다.
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    assert "elif ts_msg:" not in code, "실매매 트레일링 스탑이 여전히 elif 체인에 묶여 있다"
    assert "if not reason and ts_msg:" in code
