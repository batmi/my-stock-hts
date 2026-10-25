"""판정 핵심 분리(C2 · 2026-10-25)의 구조 계약 — modules/judgment.py 와 modules/analysis.py.

채점(score_breakdown·calculate_score)·종목 상태 분류(classify_stock_state)·지수 국면 분류(classify_regime_from_df)·
52주 위치(_w52_band)를 analysis.py 에서 judgment.py 로 본문 그대로 옮겼다. 저절로 유지되지 않는 세 가지를 여기서 고정한다.

  1) judgment 는 최상단에서 api·rich·modules.analysis 를 import 하지 않는다 — 판정만 따로 읽고 시험하려고 뗀 것이다.
     최상단에 한 줄이 얹히면 판정 모듈을 여는 것만으로 화면·조회 계층이 딸려 온다.
  2) analysis 는 옮긴 이름을 같은 객체로 다시 내보낸다 — analysis.X 로 부르는 엔진·백테스트·화면·감사 도구가 그대로 돈다.
  3) 채점의 patch 지점은 analysis.calculate_score 하나다. classify_stock_state 의 score 없는 대체 채점도 그 이름을 거친다.
     judgment 의 calculate_score 를 바로 부르면, 바꿔치기가 그 길에만 닿지 않는다 — 실측(분리 전 계측): 예약 주문 감시·
     슈퍼 모멘텀·채점 일관성 테스트 다섯이 analysis.calculate_score 를 바꿔친 채 그 길로 점수를 받는다.
"""
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOVED = ("calculate_score", "_LAST_BREAKDOWN", "last_breakdown_for", "_resolve_weights", "score_breakdown",
         "classify_regime_from_df", "classify_stock_state", "_W52_MIN_BARS", "_w52_high_low", "_w52_band")


def test_judgment_does_not_import_screen_or_fetch_layers_at_top():
    with open(os.path.join(ROOT, "modules", "judgment.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    top = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top += [f"{node.module}.{a.name}" for a in node.names] + [node.module or ""]
    bad = [m for m in top if m == "api" or m.startswith(("api.", "rich", "modules.analysis"))]
    assert not bad, f"judgment 최상단이 화면·조회 계층을 import 한다: {bad}"


def test_judgment_imports_without_analysis():
    """별도 프로세스에서 judgment 만 import 하면 analysis·api 가 딸려 오지 않는다."""
    code = ("import sys; from modules import judgment; "
            "print(','.join(m for m in ('modules.analysis', 'api') if m in sys.modules))")
    res = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-800:]
    assert res.stdout.strip() == "", f"judgment 를 열자 딸려 온 모듈: {res.stdout.strip()}"


def test_analysis_reexports_the_same_objects():
    from modules import analysis, judgment
    missing = [n for n in MOVED if getattr(analysis, n, None) is not getattr(judgment, n)]
    assert not missing, f"analysis 가 judgment 의 이름을 같은 객체로 내보내지 않는다: {missing}"


def test_fallback_scoring_goes_through_the_analysis_patch_point(monkeypatch):
    from modules import analysis
    calls, fake = [], {"score": 9.0}

    def _spy(**kw):
        calls.append(kw)
        return fake["score"], []
    monkeypatch.setattr(analysis, "calculate_score", _spy)
    thresholds = {"BUY_SCORE": 7.5, "RISE_SCORE": 6.0, "BUY_RSI_MAX": 65.0, "SUPER_MOMENTUM_USE": False,
                  "USE_MEAN_REVERSION": False}
    args = dict(price=10000, ema20=9000, ema60=8000, ema120=7000, sar=8000, rsi=60.0, prev_rsi=55.0, adx=30,
                cci=50, obv_trend=True, thresholds=thresholds, w52_pos=95.0)
    high, _, _ = analysis.classify_stock_state(**args)
    assert calls, "score 없는 상태 분류가 analysis.calculate_score 를 거치지 않았다 — 바꿔치기가 그 길에 닿지 않는다"
    fake["score"] = 1.0
    low, _, _ = analysis.classify_stock_state(**args)
    #  같은 입력에서 바꿔친 점수만 달리했다 — 판정이 그 점수를 따라 움직여야 한다.
    assert high in ("매수", "강매수") and low not in ("매수", "강매수"), (high, low)

    calls.clear()
    analysis.classify_stock_state(**args, score=9.0)
    assert not calls, "점수를 넘겼는데 다시 채점했다"
