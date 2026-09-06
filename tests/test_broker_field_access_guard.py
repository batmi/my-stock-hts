"""증권사 응답 숫자 필드는 안전 변환으로만 읽는다 (AST 가드).

[왜] KIS 는 값이 없을 때 키를 **주고 빈 문자열**을 담는다. 두 관용구가 모두 무력하다:

    int(item['evlu_amt'])          ← 빈 문자열이면 ValueError
    int(item.get('evlu_amt', 0))   ← dict.get 의 기본값은 **키가 없을 때만** 쓰인다.
                                     키는 있고 값이 '' 이므로 그대로 int('') 다.

두 번째가 더 오래 살아남는다 — 겉보기에는 방어된 것처럼 보이기 때문이다.

이 예외가 어디서 터지느냐에 따라 대가가 다르다 — 전부 실제로 있었던 일이다:
  · 루프 안(표·메시지 조립)  → 행 단위 try 가 없어 **표/메시지 전체**가 사라진다
    (format_holdings_block, 잔고 표, 보유 현황 알림)
  · 집계 안                  → 주식 평가액이 통째로 빠진 **총자산**이 만들어진다
  · 매도 판정 안             → 그 종목의 **손절·트레일링이 그 주기에 돌지 않는다**
  · 주문 발주 뒤             → 반익절 캐시·예약 일괄취소·거래기록이 통째로 건너뛰어지고,
                               "판정을 받지 못했습니다"라는 사실과 다른 경보까지 나간다

api.safe_int / api.safe_float 가 이미 정본이다. 그것을 쓰면 위 넷이 한 번에 사라진다.

[예외를 두는 법] 정말 하드 서브스크립트가 옳은 자리라면 _ALLOWED 에 사유와 함께 적는다.
"""
import ast
import os

import pytest

# "파일::함수" — 사유를 반드시 함께 적는다.
_ALLOWED: dict[str, str] = {
    'modules/auto_trade/common.py::format_holdings_block':
        "이 함수는 **행 단위 try** 로 감싸 읽을 수 없는 종목만 건너뛰고 그 사실을 밝힌다"
        "(test_holdings_block_row_failure.py 가 그 동작을 고정한다). 여기서는 필드가 비면"
        " 그 줄을 빼는 것이 옳고, 0 으로 메우면 '평가 0원'이라는 거짓말이 된다.",
}

# 증권사 응답에서 '값이 없으면 빈 문자열'로 오는 숫자 필드.
_BROKER_NUMERIC_FIELDS = {
    'hldg_qty', 'prpr', 'pchs_avg_pric', 'evlu_amt', 'evlu_pfls_amt', 'evlu_pfls_rt',
    'pchs_amt', 'ord_qty', 'rmn_qty', 'nccs_qty', 'ord_unpr', 'ord_psbl_qty',
    'ovrs_cblc_qty', 'dnca_tot_amt', 'prvs_rcdl_excc_amt',
}

_SCANNED = (
    'modules/account.py',
    'modules/telegram_bot.py',
    'modules/trading.py',
    'modules/scheduler.py',
    'modules/auto_trade/trader.py',
    'modules/auto_trade/engine.py',
    'modules/auto_trade/common.py',
    'modules/auto_trade/conclusion.py',
    'modules/reserved_order_monitor.py',
)


class _HardSubscript(ast.NodeVisitor):
    """`int(x['<필드>'])` / `float(x['<필드>'])` 를 함수 단위로 찾는다."""

    def __init__(self):
        self.hits = []
        self._fn = []

    def visit_FunctionDef(self, node):
        self._fn.append(node.name)
        self.generic_visit(node)
        self._fn.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and node.func.id in ('int', 'float')
                and node.args):
            arg = node.args[0]
            field = None
            #  ① item['evlu_amt']
            if (isinstance(arg, ast.Subscript) and isinstance(arg.slice, ast.Constant)
                    and arg.slice.value in _BROKER_NUMERIC_FIELDS):
                field = arg.slice.value
            #  ② item.get('evlu_amt', 0) — dict.get 의 기본값은 **키가 없을 때만** 쓰인다.
            #     증권사는 키를 주고 빈 문자열을 담으므로 이 관용구도 똑같이 터진다.
            #     그런데 겉보기에는 방어된 것처럼 보여서 더 오래 살아남는다.
            elif (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == 'get' and len(arg.args) == 2
                    and isinstance(arg.args[0], ast.Constant)
                    and arg.args[0].value in _BROKER_NUMERIC_FIELDS):
                field = arg.args[0].value
            if field is not None:
                self.hits.append((self._fn[-1] if self._fn else '<module>',
                                  node.lineno, field))
        self.generic_visit(node)


def test_증권사_숫자_필드를_하드_서브스크립트로_읽지_않는다():
    offenders = []
    for rel in _SCANNED:
        if not os.path.exists(rel):
            continue
        with open(rel, encoding='utf-8') as fh:
            src = fh.read()
        tree = ast.parse(src, rel)
        lines = src.splitlines()
        v = _HardSubscript()
        v.visit(tree)
        for fname, lineno, field in v.hits:
            if f"{rel}::{fname}" in _ALLOWED:
                continue
            offenders.append(f"{rel}::{fname} (줄 {lineno}, 필드 {field})\n"
                             f"      {lines[lineno - 1].strip()[:110]}")

    assert not offenders, (
        "증권사 숫자 필드를 하드 서브스크립트로 읽는 자리가 있다.\n"
        "KIS 는 값이 없을 때 키를 주고 **빈 문자열**을 담는다 — int('') 는 ValueError 다.\n"
        "api.safe_int / api.safe_float 를 쓰거나, 정말 옳은 자리라면 _ALLOWED 에 사유와 함께 적어라.\n"
        + "\n".join(offenders))


def test_가드가_실제로_탐지할_수_있다():
    """탐지기가 고장 나면 이 가드는 늘 초록이다 — 스스로를 시험한다."""
    bad = "def f():\n    x = int(item['evlu_pfls_amt'])\n"
    v = _HardSubscript(); v.visit(ast.parse(bad))
    assert v.hits == [('f', 2, 'evlu_pfls_amt')]

    ok = "def f():\n    x = api.safe_int(item.get('evlu_pfls_amt'))\n"
    v2 = _HardSubscript(); v2.visit(ast.parse(ok))
    assert v2.hits == []

    # dict.get 의 기본값은 방어가 아니다 — 이 형태도 잡아야 한다.
    fake = "def f():\n    x = int(item.get('hldg_qty', 0))\n"
    v3 = _HardSubscript(); v3.visit(ast.parse(fake))
    assert v3.hits == [('f', 2, 'hldg_qty')], "get(k, 0) 관용구를 놓쳤다"

    # 증권사 필드가 아닌 키는 대상이 아니다(오탐 방지).
    other = "def f():\n    x = int(row['count'])\n"
    v4 = _HardSubscript(); v4.visit(ast.parse(other))
    assert v4.hits == []


def test_예외_목록은_실재하는_자리만_가리킨다():
    live = set()
    for rel in _SCANNED:
        if not os.path.exists(rel):
            continue
        with open(rel, encoding='utf-8') as fh:
            tree = ast.parse(fh.read(), rel)
        v = _HardSubscript(); v.visit(tree)
        live |= {f"{rel}::{fn}" for fn, _, _ in v.hits}
    stale = sorted(set(_ALLOWED) - live)
    assert not stale, f"이미 사라진 자리를 가리키는 예외가 남아 있다: {stale}"


# ══════════════════════════════════════════════════════════════════════
# 실제 동작 — 한 줄이 목록 전체를 죽이지 않는다
# ══════════════════════════════════════════════════════════════════════

def test_빈_수량_한_줄이_보유목록_전체를_지우지_않는다():
    """실측(2026-09-06):
        [h for h in rows if int(h.get('hldg_qty', 0)) > 0]
          → ValueError: invalid literal for int() with base 10: ''
    이 필터는 코드 전반에 ~20곳 있었다. 한 종목의 빈 값 하나로 잔고 화면·텔레그램
    /status·장 시작/마감 알림·자산 집계가 **동시에** 사라진다.
    """
    import api

    rows = [{'pdno': '005930', 'hldg_qty': '10'},
            {'pdno': '000660', 'hldg_qty': ''},      # 키는 있고 값이 빈 문자열
            {'pdno': '035420', 'hldg_qty': '5'}]

    with pytest.raises(ValueError):
        [h for h in rows if int(h.get('hldg_qty', 0)) > 0]

    kept = [h for h in rows if api.safe_int(h.get('hldg_qty')) > 0]
    assert [h['pdno'] for h in kept] == ['005930', '035420']


def test_safe_int_는_빈값과_None과_공백을_0으로_읽는다():
    import api
    for bogus in ('', '   ', None, 'abc', ','):
        assert api.safe_int(bogus) == 0
    assert api.safe_int('1,234') == 1234
