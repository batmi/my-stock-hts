"""예약 주문 행에서 **없는 컬럼**을 읽지 않는다 (AST 가드).

[왜 이 가드가 있는가 · 2026-09-06] `order.get('stock_name')` 이 세 곳에 있었다.
reserved_orders 테이블의 컬럼은 `name` 이라, 그 셋은 **항상 None** 을 받았다.

None 이 조용한 이유는 `.get()` 이 예외를 내지 않기 때문이다. 그런데 그 None 은
ETF 판정의 유일한 입력이었다:

  · reserved_order_monitor 발주   → ETF 를 주권 호가표로 반올림해 주문가가 어긋난다
  · trading 예약 수정(목표가·단가) → _rsv_parse_price 독스트링이 적어 둔 그대로,
    "사용자가 입력한 유효한 ETF 호가를 다른 값으로 옮긴다(23,070 → 23,050)"

등록 경로 네 곳은 처음부터 올바른 키를 쓰고 있었다 — 두 화면만 갈라져 있었다.
컬럼 목록은 스키마에서 직접 읽는다. 손으로 적으면 컬럼이 늘 때 갈라진다.
"""
import ast
import re

import pytest


_SCANNED = ('modules/reserved_order_monitor.py', 'modules/trading.py')

# 스키마에 없지만 코드가 행에 얹어 쓰는 파생 키(조회 후 붙이는 값).
_DERIVED = {
    '_curr_price', '_gap_str', '_expire_soon', '_sort_key', '_state', '_score', '_sm',
    '_origin', '_account', '_is_db_fallback', 'stock_name',   # stock_name 은 '금지 대상'
}


def _reserved_columns():
    """db_manager 의 CREATE TABLE 에서 reserved_orders 컬럼을 읽는다."""
    with open('modules/db_manager.py', encoding='utf-8') as fh:
        src = fh.read()
    m = re.search(r"CREATE TABLE IF NOT EXISTS reserved_orders\s*\((.*?)\)\s*'''", src, re.S)
    assert m, "reserved_orders 스키마를 찾지 못했다 — 가드가 무의미해진다"
    cols = set()
    for line in m.group(1).split('\n'):
        line = line.strip().rstrip(',')
        if not line or line.upper().startswith(('PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK')):
            continue
        # 한 줄에 여러 컬럼이 올 수 있다: "cano TEXT, acnt TEXT, market TEXT,"
        for part in line.split(','):
            tok = part.strip().split(' ')[0] if part.strip() else ''
            if tok.isidentifier():
                cols.add(tok)
    return cols


def test_스키마를_실제로_읽어낸다():
    cols = _reserved_columns()
    for expected in ('id', 'cano', 'acnt', 'market', 'code', 'name',
                     'qty', 'order_price', 'condition_type', 'target_price', 'target_time'):
        assert expected in cols, f"스키마 파서가 '{expected}' 를 놓쳤다: {sorted(cols)}"


def test_stock_name_은_예약주문_컬럼이_아니다():
    assert 'stock_name' not in _reserved_columns()


@pytest.mark.parametrize("path", _SCANNED)
def test_없는_컬럼을_읽지_않는다(path):
    """`order`/`o` 로 이름 붙은 예약 주문 행에서 stock_name 을 읽으면 항상 None 이다."""
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    tree = ast.parse(src, path)
    lines = src.splitlines()

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and node.args
                and isinstance(node.args[0], ast.Constant)):
            continue
        base = node.func.value
        if not (isinstance(base, ast.Name) and base.id in ('order', 'o')):
            continue
        if node.args[0].value == 'stock_name':
            offenders.append(f"{path}:{node.lineno}\n      {lines[node.lineno - 1].strip()[:100]}")

    assert not offenders, (
        "예약 주문 행에서 존재하지 않는 컬럼 'stock_name' 을 읽는다 — 컬럼명은 'name' 이다.\n"
        ".get() 은 예외를 내지 않으므로 항상 None 이 조용히 흐르고, 그 None 은 ETF 호가 격자\n"
        "판정의 유일한 입력이다(주문가가 어긋난다).\n" + "\n".join(offenders))
