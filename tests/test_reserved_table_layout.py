"""예약 주문 대기 목록 표 — 한 종목이 한 줄인가, 폭 상한을 지키는가.

[2026-09-06] 종전에는 종목·발동조건·주문 세 칸이 각각 줄바꿈(\\n)을 품고 있어 한 건이
두 줄을 먹었다. 예약이 몇 건만 쌓여도 화면이 두 배로 길어지고, 눈으로 훑을 때
어느 줄이 어느 종목인지 매번 되짚어야 했다.

폭 상한은 메뉴 2-1 표의 실측치인 **135열**이다(그 이상은 터미널에서 접힌다).
접기 순서는 열 삭제가 아니라 collapse_padding → 열 병합 → 요약화다.
"""
from unittest.mock import patch

import config
from rich.console import Console

from modules import trading


MAX_WIDTH = 135


def _order(oid, name, code, ct, tp, qty, op, otype='sell', mkt='KR',
           curr=17100.0, gap='-1.52%', exp='20261006', cano='68029263'):
    return {'id': oid, 'name': name, 'code': code, 'condition_type': ct,
            'target_price': tp, 'target_time': '', 'composite_json': None,
            'order_type': otype, 'order_price': op, 'qty': qty, 'market': mkt,
            '_curr_price': curr, '_gap_str': gap, 'expire_dt': exp,
            '_expire_soon': False, 'cano': cano, 'acnt': '01'}


def _render(orders, width=MAX_WIDTH):
    """표를 문자열로 그린다. 반환은 (본문 줄 목록, 최대 표시폭)."""
    console = Console(width=width, force_terminal=False, no_color=True)
    with console.capture() as cap, patch.object(config, 'console', console):
        trading._print_reserved_orders_table(orders, fetch_price=False)
    lines = cap.get().splitlines()
    return lines, max((console.measure(l).maximum for l in lines), default=0)


_ROWS = [
    _order(6, '코오롱티슈진', '950160', 'STOP', 10000, 150, 10000, 'buy', gap='-41.52%'),
    # 이 표에서 가장 긴 종목명(ETF)과 가장 긴 조건 문장을 한 줄에 같이 세운다.
    _order(7, '미래에셋TIGER미국나스닥100', '133690', 'TRAILING_SELL', 3.5, 1500, 0),
    _order(8, '삼성전자', '005930', 'SCORE_DOWN', 4.0, 40, 71000),
    _order(9, 'APPLE INC', 'AAPL', 'EMA_DOWN', 60, 12, 0, mkt='US', curr=225.35,
           exp='20991231'),
]


def _body(lines):
    """구분선·제목·꼬리주석을 뺀 데이터 줄."""
    return [l for l in lines
            if l.strip() and '─' not in l and '※' not in l
            and '예약 주문 대기 목록' not in l and 'ID' not in l.split()[:1]]


def test_한_종목은_한_줄로_출력된다():
    lines, _ = _render(_ROWS)
    body = _body(lines)
    assert len(body) == len(_ROWS), (
        f"{len(_ROWS)}건인데 데이터 줄이 {len(body)}줄이다 — 줄바꿈이 남아 있다:\n"
        + "\n".join(body))
    for row, line in zip(_ROWS, body):
        assert row['code'] in line, f"{row['code']} 가 자기 줄에 없다: {line!r}"
        assert row['name'] in line, f"{row['name']} 이 자기 줄에 없다: {line!r}"


def test_다계좌_컬럼이_붙어도_한_줄을_지킨다():
    """계좌 열은 여러 계좌에 예약이 걸릴 때만 뜬다 — 가장 넓은 경우다."""
    rows = [dict(r) for r in _ROWS]
    rows[1]['cano'] = '44048158'
    lines, _ = _render(rows)
    assert len(_body(lines)) == len(rows), "계좌 열이 붙자 줄이 접혔다:\n" + "\n".join(lines)


def test_표_폭이_상한_135열을_넘지_않는다():
    rows = [dict(r) for r in _ROWS]
    rows[1]['cano'] = '44048158'          # 계좌 열까지 띄운 최대 폭
    _, width = _render(rows, width=400)   # 넉넉한 화면에서 자연폭을 잰다
    assert width <= MAX_WIDTH, f"표 폭 {width}열 — 상한 {MAX_WIDTH}열을 넘었다"


def test_조건_종류는_값만_나올_때만_앞에_붙는다():
    """STOP 은 '10,000원'이라 방향을 알 수 없어 종류가 필요하지만, 나머지는 이미
    문장이라 앞에 붙이면 'EMA EMA 60일선 하향이탈'처럼 겹친다."""
    lines, _ = _render(_ROWS)
    body = "\n".join(_body(lines))
    assert 'STOP 10,000원' in body
    assert 'EMA EMA' not in body and 'SCORE 점수' not in body
    assert 'TRAILING_SELL 고점' not in body
