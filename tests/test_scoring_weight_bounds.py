"""스코어링 가중치는 합계만이 아니라 **개별 값**도 검사한다.

[왜] modify_scoring_weights 의 검사는 `합계 == 10.0` 하나뿐이었다. 그런데 각 가중치는
 그 팩터의 **배수**가 된다 — analysis.calculate_score 의
     r_trend = weights["TREND"] / 4.0
 음수면 가점이 감점이 되어, 추세가 강한 종목일수록 점수가 낮아진다.
 이 시스템의 핵심은 추세추종이고([[strategy-trend-following-core]]) 점수가 진입 1순위이므로
 ([[entry-rank-score-first]]) 그대로 진입 순위가 뒤집힌다.

 실측(합계는 똑같이 10.0, TREND -4.0 / MOMENTUM 10.5):
     추세가 강한 A   5.50 → 4.30
     모멘텀만 강한 B 5.00 → 6.90      → 1순위가 A 에서 B 로 바뀐다.

 이 경로는 _edit_config_table 을 타지 않아 중앙 규칙표(_range_error)를 통째로 지나치고
 있었다. 검증 규칙이 두 갈래로 갈리는 자리를 없앤다.
"""
import pytest

from modules import analysis
from modules import settings as S

NORMAL = {"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}
INVERTED = {"TREND": -4.0, "MOMENTUM": 10.5, "STRENGTH": 1.5, "SYNERGY": 2.0}

#  둘 다 상승이지만 성격이 다르다: A = 추세가 강함, B = 모멘텀만 강함.
A = dict(price=12000, ema20=11500, ema60=11000, ema120=10000, sar=11000,
         rsi=55, adx=32, cci=60, obv_trend=True, macd=120, macd_signal=80,
         plus_di=30, minus_di=12)
B = dict(price=10200, ema20=10100, ema60=10150, ema120=10000, sar=10150,
         rsi=68, adx=22, cci=180, obv_trend=True, macd=20, macd_signal=25,
         plus_di=22, minus_di=18)


def _score(kw, w):
    r = analysis.calculate_score(weights=w, **kw)
    return r[0] if isinstance(r, tuple) else r


def test_음수_가중치는_진입_순위를_뒤집는다():
    """고치기 전 상태를 못 박아 둔다 — 왜 막아야 하는지가 여기 있다."""
    assert sum(INVERTED.values()) == pytest.approx(10.0), "합계 검사만으로는 걸리지 않는다"
    assert _score(A, NORMAL) > _score(B, NORMAL), "정상 가중치에서는 추세가 이긴다"
    assert _score(A, INVERTED) < _score(B, INVERTED), "음수 가중치가 순위를 뒤집는다"


@pytest.mark.parametrize("key", ["TREND", "MOMENTUM", "STRENGTH", "SYNERGY"])
def test_음수_가중치는_거부된다(key):
    assert S._range_error(key, -0.1), f"{key} 음수가 통과한다"


@pytest.mark.parametrize("key,val", [("TREND", 4.0), ("MOMENTUM", 2.5),
                                     ("STRENGTH", 1.5), ("SYNERGY", 2.0)])
def test_현재_기본값은_막히지_않는다(key, val):
    assert S._range_error(key, val) is None


@pytest.mark.parametrize("key", ["TREND", "MOMENTUM", "STRENGTH", "SYNERGY"])
def test_0은_허용한다(key):
    """0 은 '그 팩터를 끈다'는 뜻이라 정상적인 조정이다."""
    assert S._range_error(key, 0) is None


def test_가중치_입력_경로가_규칙표를_실제로_부른다():
    """규칙표에 넣기만 하고 이 경로에서 부르지 않으면 아무 일도 일어나지 않는다."""
    import ast
    import inspect

    src = inspect.getsource(S.modify_scoring_weights)
    tree = ast.parse(src.lstrip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_range_error"]
    assert calls, "합계만 보고 개별 값은 중앙 규칙표에 묻지 않는다"


def test_합계_검사는_그대로_남아_있다():
    """개별 검사를 더하면서 합계 규칙을 잃으면 안 된다(총점 10점 모델의 전제)."""
    import inspect
    src = inspect.getsource(S.modify_scoring_weights)
    assert "10.0" in src and "new_total" in src


# ══════════════════════════════════════════════════════════════════
# 두 번째 입구 — 종목별 개별 룰
# ══════════════════════════════════════════════════════════════════
#  가중치를 넣는 자리는 둘이다. 전역 설정(메뉴 0)과 **종목별 개별 룰**(자동매매 메뉴).
#  개별 룰은 그 종목에만 적용돼 화면 어디에도 크게 드러나지 않으므로 오히려 더 조용하다.
#  둘이 다른 규칙을 쓰면 한쪽만 고쳐지고 다른 쪽은 그대로 남는다 — 이 감사가 반복해서
#  본 '목록이 갈라지는 자리'다.


def test_개별_룰_가중치도_같은_규칙표를_거친다():
    import ast
    import inspect

    from modules.auto_trade import menu

    src = inspect.getsource(menu.manage_stock_strategies) \
        if hasattr(menu, 'manage_stock_strategies') else inspect.getsource(menu)
    tree = ast.parse(src.lstrip() if src.startswith(' ') else src)
    #  WEIGHT_FACTORS 로 받은 값들을 _range_error 에 물어보는 호출이 있어야 한다.
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_range_error"]
    assert calls, "개별 룰 가중치가 중앙 규칙표를 지나치지 않는다(합계만 본다)"


def test_두_입구가_같은_키를_쓴다():
    """규칙표의 키와 개별 룰 입력의 키가 갈리면 검사가 조용히 빗나간다."""
    import inspect

    from modules.auto_trade import menu

    src = inspect.getsource(menu)
    for key in ("TREND", "MOMENTUM", "STRENGTH", "SYNERGY"):
        assert f'("{key}",' in src, f"개별 룰 입력에 {key} 가 없다"
        assert key in S._RANGE_RULES, f"규칙표에 {key} 가 없다"
