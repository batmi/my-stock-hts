"""구조화 채점 결과(analysis.score_breakdown / modules.score_model) 불변식.

calculate_score 는 score_breakdown 의 어댑터다. 여기서는 구현을 다시 적는 대신, 구조화
결과가 지켜야 하는 성질 — 기여값의 합, 슬롯 배타성, EMA 군집 상한 산술, 결측과 미충족의
구분, 상태 분류가 같은 점수를 재사용해도 결과가 같다는 것 — 을 무작위·경계 입력으로 확인한다.
"""
import json
import math
import os
import random
import re

import pytest

from modules import analysis, score_model

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 지시서(2026-10-16) 재현 입력 — 시너지 경계와 결측 비교의 기준점
BASE = dict(price=120, ema20=100, ema60=95, ema120=90, ema_5=110, sar=80,
            rsi=65, adx=30, cci=-50, prev_cci=-50, obv_trend=True,
            smart_money=False, macd=2, macd_signal=1.2, macd_hist=0.8,
            prev_macd_hist=1, plus_di=20, minus_di=20,
            vol_spike=False, vol_trend=False, w52_pos=95, mom_ret=40,
            mom_ret_1m=5, mom_ret_3m=15, trend_persist=None)


def _rand_inputs(rng):
    def maybe(v, p_none=0.08, p_nan=0.03):
        r = rng.random()
        if r < p_none:
            return None
        if r < p_none + p_nan:
            return float("nan")
        return v
    price = rng.uniform(50, 150)
    d = dict(
        price=price,
        ema20=maybe(price * rng.uniform(0.8, 1.2)), ema60=maybe(price * rng.uniform(0.8, 1.2)),
        ema120=maybe(price * rng.uniform(0.75, 1.25)), ema_5=maybe(price * rng.uniform(0.9, 1.1)),
        sar=maybe(price * rng.uniform(0.85, 1.15)), rsi=maybe(rng.uniform(10, 95)),
        adx=maybe(rng.uniform(5, 60)), cci=maybe(rng.uniform(-250, 250)),
        prev_cci=maybe(rng.uniform(-250, 250)),
        obv_trend=rng.choice([True, False, None]), smart_money=rng.choice([True, False]),
        macd=maybe(rng.uniform(-3, 3)), macd_signal=maybe(rng.uniform(-3, 3)),
        macd_hist=maybe(rng.uniform(-2, 2)), prev_macd_hist=maybe(rng.uniform(-2, 2)),
        plus_di=maybe(rng.uniform(5, 45)), minus_di=maybe(rng.uniform(5, 45)),
        vol_spike=rng.choice([True, False]), vol_trend=rng.choice([True, False]),
        w52_pos=maybe(rng.uniform(0, 100)), mom_ret=maybe(rng.uniform(-50, 80)),
        mom_ret_1m=maybe(rng.uniform(-20, 20), 0.2), mom_ret_3m=maybe(rng.uniform(-30, 30), 0.2),
        trend_persist=maybe(rng.uniform(0, 100), 0.2),
    )
    d["weights"] = rng.choice([None, {"TREND": 5.0, "MOMENTUM": 2.0, "STRENGTH": 1.0, "SYNERGY": 2.0}])
    return d


@pytest.fixture(scope="module")
def samples():
    rng = random.Random(20261016)
    return [_rand_inputs(rng) for _ in range(3000)]


def test_adapter_matches_breakdown(samples):
    for d in samples:
        res = analysis.score_breakdown(**d)
        assert analysis.calculate_score(**d) == (res.total, res.details)


def test_contributions_sum_to_raw_total_and_total_is_floored(samples):
    for d in samples:
        res = analysis.score_breakdown(**d)
        s = sum(it.contribution for it in res.items)
        assert math.isclose(s, res.raw_total, abs_tol=1e-9)
        assert res.total == round(max(res.raw_total, 0.0), 2)
        assert math.isclose(res.penalty, sum(it.contribution for it in res.items if it.factor == "PENALTY"),
                            abs_tol=1e-9)


def test_every_item_reported_once_and_cataloged(samples):
    for d in samples[:300]:
        res = analysis.score_breakdown(**d)
        ids = [it.item_id for it in res.items]
        assert sorted(ids) == sorted(score_model.ITEM_SPECS)
    assert set(score_model.ITEM_LABELS) == set(score_model.ITEM_SPECS)


def test_slot_items_never_both_score(samples):
    slots = {}
    for iid, spec in score_model.ITEM_SPECS.items():
        if spec[3]:
            slots.setdefault(spec[3], []).append(iid)
    for d in samples:
        res = analysis.score_breakdown(**d)
        for slot, ids in slots.items():
            scored = [i for i in ids if res.item(i).status == score_model.MET]
            assert len(scored) <= 1, (slot, scored)


def test_ma_cluster_cap_carries_exact_reduction(samples):
    for d in samples:
        res = analysis.score_breakdown(**d)
        ma_sum = sum(res.contribution(i) for i in score_model.MA_CLUSTER)
        cap_c = res.contribution("trend.ma_cluster_cap")
        assert math.isclose(ma_sum, res.ma_cluster["raw"], abs_tol=1e-9)
        assert math.isclose(ma_sum + cap_c, min(ma_sum, res.ma_cluster["cap"]), abs_tol=1e-9)
        assert res.ma_cluster["applied"] == (cap_c < 0)


def test_details_come_from_items_in_order(samples):
    for d in samples[:500]:
        res = analysis.score_breakdown(**d)
        assert res.details == [it.detail for it in res.shown()]


def test_missing_input_is_unknown_not_unmet():
    res = analysis.score_breakdown(**dict(BASE, ema20=None))
    assert res.status("trend.ema20_above") == score_model.UNKNOWN
    assert "ema20" in res.missing_inputs
    res = analysis.score_breakdown(**dict(BASE, ema20=float("nan")))
    assert res.status("trend.ema20_above") == score_model.UNKNOWN
    # 입력이 있고 조건이 안 맞으면 unmet
    res = analysis.score_breakdown(**dict(BASE, ema20=130))
    assert res.status("trend.ema20_above") == score_model.UNMET
    # trend_persist=None 인 재현 입력: 지속 항목은 확인 불가(0점)이지 미충족이 아니다
    assert analysis.score_breakdown(**BASE).status("trend.persist") == score_model.UNKNOWN


def test_price_momentum_fail_open_is_visible_but_score_unchanged():
    """지시서 5-3: 1개월 -1% → 7.0, None → 7.5. 점수는 기존 정책 그대로, 결과에 미확인 표시."""
    held = analysis.score_breakdown(**dict(BASE, mom_ret_1m=-1))
    open_ = analysis.score_breakdown(**dict(BASE, mom_ret_1m=None))
    checked = analysis.score_breakdown(**BASE)
    assert (held.total, open_.total, checked.total) == (7.0, 7.5, 7.5)
    assert held.status("momentum.price_momentum") == score_model.HELD
    assert open_.item("momentum.price_momentum").unverified == ("mom_ret_1m",)
    assert checked.item("momentum.price_momentum").unverified == ()


def test_synergy_boundary_moves_two_items_together():
    """지시서 5-4: 0.8000 → 7.5, 0.7999 → 5.5. 경계 하나가 시너지 두 항목을 함께 뒤집는다."""
    a = analysis.score_breakdown(**BASE)
    b = analysis.score_breakdown(**dict(BASE, macd_hist=0.7999, macd_signal=1.2001))
    assert (a.total, b.total) == (7.5, 5.5)
    flipped = {i for i in score_model.ITEM_SPECS if a.contribution(i) != b.contribution(i)}
    assert flipped == set(score_model.SHARED_CONDITIONS["macd_expanding"])
    assert a.diagnostics["macd_expand_margin"] >= 0 > b.diagnostics["macd_expand_margin"]


def test_classify_with_precomputed_score_is_identical(samples):
    """상태 분류에 호출자의 점수를 넘겨도(채점 1회) 내부 재채점과 결과가 같다."""
    rng = random.Random(3)
    for d in samples[:1500]:
        d = dict(d)
        w = d.pop("weights")
        th = {"WEIGHTS": w} if w else None
        prev_rsi = rng.choice([None, rng.uniform(10, 90)])
        sc, _ = analysis.calculate_score(weights=w, **d)
        assert (analysis.classify_stock_state(prev_rsi=prev_rsi, thresholds=th, **d)
                == analysis.classify_stock_state(prev_rsi=prev_rsi, thresholds=th, score=sc, **d))


def test_as_dict_is_json_and_compact(samples):
    for d in samples[:200]:
        res = analysis.score_breakdown(**d)
        blob = res.as_dict()
        json.dumps(blob)
        assert all(x["st"] != score_model.UNMET for x in blob["items"])
        assert blob["ver"] == score_model.SCORING_RULES_VERSION
        assert len(blob["fp"]) == 12


def test_params_fingerprint_tracks_weights():
    a = analysis.score_breakdown(**BASE)
    b = analysis.score_breakdown(**dict(BASE, weights={"TREND": 5.0, "MOMENTUM": 2.0, "STRENGTH": 1.0, "SYNERGY": 2.0}))
    assert a.params_fingerprint != b.params_fingerprint
    assert a.params_fingerprint == analysis.score_breakdown(**BASE).params_fingerprint


def test_score_audit_tools_do_not_parse_display_text():
    """[가드] 감사 도구(tools/ 전체)가 채점 설명 문구의 숫자·접두사를 다시 해석하지 않는다.

    문구는 화면용이다. 항목·배점은 score_breakdown 의 항목 ID·기여값으로 읽어야 한다.
    """
    offenders = [name for name, src in _score_tool_sources() if _parses_display_text(src)]
    assert not offenders, offenders


# 화면 문구의 접두사가 따옴표 리터럴로 들어 있으면 문자열 해석의 흔적이다(접두사 표·startswith 모두).
_DISPLAY_LITERAL = re.compile(
    r"[\"'](EMA: |\[상한\] EMA|CCI: |VOL: |가격 모멘텀(:| 보류)|추세 지속:|MACD: |RSI: |SAR: |ADX: |DMI: "
    r"|수급: |감점: |추세 시작:|모멘텀 폭발:)")
_POINTS_REGEX = re.compile(r"re\.compile\(r\"\\\(\(\[\+-\]")


def _parses_display_text(src):
    return bool(_DISPLAY_LITERAL.search(src) or _POINTS_REGEX.search(src))


def _score_tool_sources():
    tools_dir = os.path.join(ROOT, "tools")
    for name in sorted(os.listdir(tools_dir)):
        if name.endswith(".py"):
            yield name, open(os.path.join(tools_dir, name), encoding="utf-8").read()


def test_display_text_guard_detects_legacy_parsing():
    """가드 자체 검증 — 종전 도구들의 해석 방식(접두사 표, 점수 정규식)을 실제로 잡는다."""
    assert _parses_display_text('LABELS = [("EMA: 현재가 > 20일선", "추세 EMA20위")]')
    assert _parses_display_text('CAP_PREFIX = "[상한] EMA 군집"')
    assert _parses_display_text('_PTS = re.compile(r"\\(([+-]?\\d+\\.\\d+)\\)")')
    assert not _parses_display_text('MA_BREAK = "trend.ma60_breakout_early"')


def _synth_daily(rng, n):
    import pandas as pd
    p, rows = 100.0, []
    drift = rng.uniform(-0.002, 0.003)
    for i in range(n):
        o = p * (1 + rng.gauss(0, 0.01))
        c = o * (1 + rng.gauss(drift, 0.02))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.008)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.008)))
        rows.append(dict(date=pd.Timestamp("2024-01-01") + pd.Timedelta(days=i), open=o, high=h, low=lo,
                         close=c, volume=float(rng.randint(10000, 500000))))
        p = c
    return pd.DataFrame(rows)


def test_live_and_backtest_paths_agree_per_item_on_same_history():
    """[5-5] 같은 시점까지 같은 이력이면 실매매 경로(df→지표→채점)와 백테스트 경로(사전계산
    행→backtest.score_inputs→채점)가 **항목별 상태·기여까지** 같다. 총점만 같으면 서로 다른
    항목이 상쇄했을 수 있다. (제한 이력 260봉의 워밍업 괴리는 로직 오류가 아니므로 여기서
    다루지 않는다 — tools/audit_live_backtest_parity.py 의 B-live 가 따로 잰다.)"""
    from core import indicators
    from modules import backtest
    rng = random.Random(8)
    for _ in range(3):
        df = _synth_daily(rng, 330)
        rows = backtest.compute_price_indicators(df.copy()).to_dict("records")
        for t in range(260, 330, 7):
            b = analysis.score_breakdown(**backtest.score_inputs(rows[t], rows[t - 1]))
            sub = df.iloc[:t + 1].reset_index(drop=True)
            ind = indicators.calculate_indicators(sub)
            w52 = indicators.w52_position(sub, float(sub.iloc[-1]["close"]))
            live = analysis.score_breakdown(df=sub, ind=ind, w52_pos=w52)
            for it in b.items:
                li = live.item(it.item_id)
                assert (it.status, round(it.contribution, 6)) == (li.status, round(li.contribution, 6)), \
                    (t, it.item_id, it.inputs, li.inputs)
