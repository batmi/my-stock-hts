"""지수 시세를 읽는 두 표면이 같은 규칙을 쓴다.

`market.fetch_index_quote`(텔레그램 등 외부 표면)의 독스트링은 "소스 선택 규칙은 지수
화면(_process_index_worker)과 동일"이라고 선언한다. 선언은 낡는다 — 2026-09-05 감사에서
둘이 **양방향으로** 갈라져 있었다.

① 지수 화면 → 없는 값으로 프록시를 돌렸다
   미국채 금리는 tvDatafeed 현물이 1차이고, 실패하면 5/10/30년만 야후 현물(^FVX/^TNX/^TYX)
   + 아시아장 선물 프록시로 간다. 그런데 프록시 분기의 조건이 `if name in fut_mapping` 뿐이라
   **기준 금리 조회가 실패해도**(current = 0.0) 선물만 받아지면 돌았다:
       est_yield = 0.0 - f_rate/듀레이션
   숫자는 뒤의 일봉 폴백이 덮어써 사라지지만 `is_proxy_yield` 깃발은 남는다 →
   프록시를 쓰지 않은 행에 '(F)' 가 붙고, 지표의 마지막 봉 실시간 패치가 통째로 건너뛰어진다.
   fetch_index_quote 는 같은 프록시를 `if fi and last_price` 안에 중첩해 이미 지키고 있었다.

② 외부 표면 → 전일값이 None 이면 현재값까지 버렸다
   `float(fi.get('regular_market_previous_close', current))` — dict.get 의 기본값은 **키가
   없을 때만** 쓰인다. 키가 있는데 값이 None 이면 float(None) 이 TypeError 를 내고, 바깥
   except 가 삼켜 **멀쩡히 받아 온 현재값까지** 함께 사라졌다(수신 실패로 표시).
   지수 화면은 `is not None and not isnan` 으로 이미 걸러 왔다.

관련: [[unknown-vs-empty]] · [[us-treasury-2y-source]]
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import market


# --------------------------------------------------------- ② fast_info 읽기
def test_전일값이_None이어도_현재값을_버리지_않는다():
    """야후 fast_info 는 키를 주고 값을 None 으로 두는 일이 있다."""
    fi = {'last_price': 4321.0, 'regular_market_previous_close': None}
    cur, prev = market._fast_info_pair(fi)
    assert cur == 4321.0, "전일값 하나 때문에 현재값이 통째로 버려졌다"
    assert prev == 4321.0, "전일값을 모르면 등락 0%(현재값)로 둔다"


def test_전일값_키가_아예_없어도_같다():
    cur, prev = market._fast_info_pair({'last_price': 100.0})
    assert (cur, prev) == (100.0, 100.0)


@pytest.mark.parametrize("fi", [
    None, {}, {'last_price': None},
    {'last_price': float('nan')},
    {'last_price': 'n/a'},
])
def test_현재값을_못_읽으면_모름으로_답한다(fi):
    assert market._fast_info_pair(fi) == (None, None), (
        f"읽을 수 없는 응답을 값으로 포장했다: {fi}")


def test_전일값이_NaN이면_현재값으로_대신한다():
    cur, prev = market._fast_info_pair(
        {'last_price': 10.0, 'regular_market_previous_close': float('nan')})
    assert (cur, prev) == (10.0, 10.0)


def test_정상값은_그대로_통과한다():
    cur, prev = market._fast_info_pair(
        {'last_price': 10.5, 'regular_market_previous_close': 10.0})
    assert (cur, prev) == (10.5, 10.0)


def test_외부_표면이_그_규칙을_실제로_쓴다():
    """헬퍼만 있고 호출부는 옛 코드로 남는 것을 막는다.

    문자열 검색이 아니라 AST 로 본다 — 이 파일과 market.py 의 설명 주석이 옛 코드를
    그대로 인용하고 있어, 문자열로 훑으면 설명 때문에 빨개진다.
    """
    import ast
    src = open(market.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    tree = ast.parse(src)

    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
           and len(n.args) == 2
           and isinstance(n.args[0], ast.Constant)
           and n.args[0].value == "regular_market_previous_close"]
    assert not bad, (
        "dict.get 기본값으로 전일값을 메운다 — 키가 있고 값이 None 이면 기본값은 안 쓰인다: "
        + ", ".join(f"market.py:{ln}" for ln in bad))

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "fetch_index_quote")
    used = [n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_fast_info_pair"]
    assert len(used) >= 2, (
        f"fetch_index_quote 의 fast_info 읽기가 헬퍼를 지나지 않는다({len(used)}곳)")


# ------------------------------------------------- ① 프록시는 기준값을 요구한다
def test_기준_금리를_못_받으면_선물_프록시를_돌리지_않는다():
    """current 가 0.0 인 채로 프록시가 돌면 '(F)' 만 남는 가짜 행이 된다."""
    import ast
    src = open(market.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_process_index_worker")
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If) and "fut_mapping" in ast.dump(n.test)]
    assert guards, "선물 프록시 분기를 찾지 못했다 — 검사기가 낡았다"
    for g in guards:
        test_src = ast.unparse(g.test)
        assert "use_fast_info" in test_src, (
            "기준값을 받았는지 보지 않고 프록시를 돌린다: " + test_src)
