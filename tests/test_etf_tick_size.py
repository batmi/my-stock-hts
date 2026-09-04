"""ETF·ETN 호가 격자는 주권과 다르다.

2026-09-04 감사 · KRX 원본 종가 실측(pykrx, ETF 11종 · 2024-01~2026-09 · 7,161일):

    가격대            5원 배수   10원 배수      판정
    0~2,000             18.8%       8.6%   → 1원 격자(무작위 수준)
    2,000~5,000        100.0%      50.8%   → 5원
    5,000~20,000       100.0%      53.3%   → 5원   (코드는 10원을 썼다)
    20,000~50,000      100.0%      56.6%   → 5원   (코드는 50원)
    50,000~200,000     100.0%      57.1%   → 5원   (코드는 100원)
    200,000~           100.0%      69.2%   → 5원   (코드는 500원)

대조군 삼성전자(주권)는 같은 기간 50·500원 배수 100%로 주권 표와 정확히 일치했다.
주권 표는 ETF 격자의 배수라 주문이 거부되지는 않는다. 대신 주문가가 최대 tick/2 만큼
어긋난다 — 반올림은 양방향이라 손절 매도 지정가가 위로 밀리면 체결이 늦어진다.
"""
import pytest

from core import utils


@pytest.mark.parametrize("price,expect", [
    (100, 1), (1999, 1),        # 2,000원 미만은 1원 — 주권과 같다
    (2000, 5), (4999, 5),
    (5000, 5), (19999, 5),      # 주권은 10원
    (20000, 5), (49999, 5),     # 주권은 50원
    (50000, 5), (199999, 5),    # 주권은 100원
    (200000, 5), (499999, 5),   # 주권은 500원
    (500000, 5), (1200000, 5),  # 주권은 1,000원
])
def test_etf_tick_is_five_above_two_thousand(price, expect):
    assert utils.get_tick_size(price, is_etf=True) == expect


@pytest.mark.parametrize("price,expect", [
    (1999, 1), (2000, 5), (4999, 5), (5000, 10), (19999, 10),
    (20000, 50), (49999, 50), (50000, 100), (199999, 100),
    (200000, 500), (499999, 500), (500000, 1000),
])
def test_stock_table_is_unchanged(price, expect):
    """주권 표는 손대지 않는다 — 대조군 실측(삼성전자)이 이 표와 100% 일치했다."""
    assert utils.get_tick_size(price) == expect


def test_overseas_wins_over_etf():
    """해외는 0.01 그대로 — 해외 ETF에 국내 격자를 씌우면 안 된다."""
    assert utils.get_tick_size(123.45, is_overseas=True, is_etf=True) == 0.01


@pytest.mark.parametrize("raw,etf_expect,stock_expect", [
    (23070, 23070, 23050),        # KODEX 삼성그룹대 — 주권 표는 20원 아래로 민다
    (100339, 100340, 100300),     # 10만원대 ETF — 주권 표는 39원 아래로 민다
    (8245, 8245, 8240),
    (16915, 16915, 16920),
])
def test_etf_prices_survive_rounding(raw, etf_expect, stock_expect):
    """실재하는 ETF 종가가 주권 표에서는 다른 값으로 옮겨진다."""
    assert int(utils.adjust_to_tick(raw, is_etf=True)) == etf_expect
    assert int(utils.adjust_to_tick(raw)) == stock_expect


def test_rounded_price_is_always_on_the_etf_grid():
    """보정 결과는 반드시 ETF 격자 위에 있어야 한다(주문 거부 방지)."""
    for p in range(1, 3000):
        v = utils.adjust_to_tick(p, is_etf=True)
        assert v % (1 if p < 2000 else 5) == 0, p
    for p in range(2000, 400000, 137):
        assert utils.adjust_to_tick(p, is_etf=True) % 5 == 0, p


def test_default_stays_the_stock_table():
    """인자를 안 주면 종전 동작 그대로 — 백테스트 수치가 흔들리면 안 된다."""
    assert utils.get_tick_size(23070) == 50
    assert int(utils.adjust_to_tick(23070)) == 23050


def test_order_paths_pass_the_etf_flag():
    """국내 주문가를 만드는 경로는 ETF 여부를 넘겨야 한다 — 소스로 고정한다."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    #  백테스트·감사 도구는 대상에서 뺀다 — 국내 주식 전용 모델이고(ETF 자동매매 기각),
    #  격자를 바꾸면 과거 수치와의 연속성이 깨진다.
    order_paths = ("modules/auto_trade/trader.py", "modules/trading.py",
                   "modules/reserved_order_monitor.py", "modules/paper_broker.py")
    import io as _io
    import tokenize

    for rel in order_paths:
        src = (root / rel).read_text(encoding='utf-8')
        lines = src.splitlines()
        #  주석·문서화 문자열은 뺀다 — 결함을 설명하는 글까지 잡으면 가드가 못 쓰게 된다.
        code_lines = set()
        for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL):
                code_lines.add(tok.start[0])
        for i, line in enumerate(lines):
            if "adjust_to_tick(" not in line or (i + 1) not in code_lines:
                continue
            stmt = " ".join(lines[i:i + 4])
            if "is_overseas=True" in stmt:
                continue        # 해외는 0.01 고정
            assert "is_etf" in stmt, f"{rel}:{i + 1} ETF 격자를 넘기지 않는다 → {line.strip()}"
