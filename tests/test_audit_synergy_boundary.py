"""시너지 경계 감사(tools/audit_synergy_boundary) — 팔(arm) 정의와 기준선 무결성.

실험 결과가 의미를 가지려면 ① 기준선 팔이 원본 채점과 **완전히 같아야** 하고 ② 후보 팔은
시너지 슬롯(2점) 안에서만 점수를 바꿔야 한다(만점·문턱 혼입 없음).
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from modules import analysis  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools import audit_synergy_boundary as asb  # noqa: E402

BASE = dict(price=120, ema20=100, ema60=95, ema120=90, ema_5=110, sar=80, rsi=65, adx=30, cci=-50,
            prev_cci=-50, obv_trend=True, smart_money=False, macd=2, macd_signal=1.2, macd_hist=0.8,
            prev_macd_hist=1, plus_di=20, minus_di=20, vol_spike=False, vol_trend=False, w52_pos=95,
            mom_ret=40, mom_ret_1m=5, mom_ret_3m=15, trend_persist=None)


def _res(hist):
    return analysis.score_breakdown(**dict(BASE, macd_hist=hist))


def test_baseline_factor_is_the_expansion_condition():
    for h in (0.5, 0.7999, 0.8, 1.2, -0.1):
        f, on = asb.synergy_factor("baseline", _res(h), None)
        assert f == (1.0 if _res(h).diagnostics["macd_expanding"] else 0.0) and on == (f == 1.0)


def test_graded_is_linear_between_0_7_and_0_9():
    assert asb.synergy_factor("graded", _res(0.7), None)[0] == 0.0
    assert abs(asb.synergy_factor("graded", _res(0.8), None)[0] - 0.5) < 1e-9
    assert asb.synergy_factor("graded", _res(0.95), None)[0] == 1.0


def test_hysteresis_turns_on_at_0_8_and_off_below_0_7():
    assert asb.synergy_factor("hysteresis", _res(0.75), False) == (0.0, False)   # 꺼진 상태 — 0.8 미만
    assert asb.synergy_factor("hysteresis", _res(0.75), True) == (1.0, True)     # 켜진 상태 — 0.7 이상 유지
    assert asb.synergy_factor("hysteresis", _res(0.69), True) == (0.0, False)
    assert asb.synergy_factor("hysteresis", _res(0.8), False) == (1.0, True)


def test_candidate_arms_only_move_the_synergy_slot():
    """후보 팔의 점수 = 원점수 − 현행 시너지 + stake × 배수 — 다른 항목·만점은 그대로다."""
    _ps_status, ps = asb.make_patches(lambda *a, **k: None, "graded")
    asb._CTX["code"] = "X"
    for h in (0.72, 0.8, 0.88):
        res = _res(h)
        got, _ = ps(**dict(BASE, macd_hist=h))
        cur = sum(res.contribution(i) for i in asb.SYN_IDS)
        f, _ = asb.synergy_factor("graded", res, None)
        assert got == round(max(res.raw_total - cur + res.diagnostics["synergy_stake"] * f, 0.0), 2)
        assert got <= res.raw_total - cur + 2.0 + 1e-9


def test_baseline_arm_through_the_patch_equals_the_original():
    """실험 도구 자체의 무결성 검사와 같은 것 — 합성 유니버스에서 패치 경로 기준선 == 원본."""
    import test_exit_parity as T
    dfs = {f"{k}{s}": T._synth(s, k) for k in ("trend", "down", "chop") for s in (1, 2)}
    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "WEIGHTS": config.SCORING_WEIGHTS}
    assert asb.precompute(dfs, thr, "baseline") == pb.precompute_status(dfs, thr)
    assert analysis.calculate_score.__name__ == "calculate_score"      # 패치가 원복됐다
