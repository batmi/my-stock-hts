"""ETF 판정은 근거가 있을 때만 기억한다 (감사 2026-09-06, 배치 67).

[무엇이 걸려 있는가] is_domestic_etf_etn 은 **호가 격자**를 정한다. ETF·ETN 은 2,000원
이상에서 5원 단일인데 주권 표로 반올림하면 주문가가 어긋난다([[etf-tick-size]]).
실측: 74,321원 → 주권표 74,300 / ETF표 74,320 (20원). 손절 예약이면 그만큼 체결이 늦다.

두 결함이 겹쳐 있었다:

 1. **이름 없이 얻은 False 를 캐시에 굳혔다.** 이 함수는 name 없이도 불린다
    (api/toss.py 의 'KRX 단독' 판정). 그때 이름 휴리스틱은 성립할 수 없어 무조건
    False 가 나오는데, 그것이 코드 단위로 캐시돼 **이름을 주고 물어도 False 가
    돌아왔다** — 프로세스가 사는 동안 계속.

 2. **예약 주문 발주가 잘못된 키를 읽었다.** `order.get('stock_name')` 인데
    reserved_orders 테이블의 컬럼은 `name` 이다 — 항상 None 이라 이름 없이 판정했고,
    그 False 가 1번 경로로 캐시에 굳어 다른 경로까지 오염시켰다.
"""
import pytest

import api
import config
from api import instruments
from core import utils


ETF_CODE, ETF_NAME = '069500', 'KODEX 200'


@pytest.fixture
def clean(monkeypatch):
    instruments._ETF_ETN_CACHE.clear()
    monkeypatch.setattr(config.session, 'stock_data',
                        {'etfs_kr': [], 'stocks_kr': [], 'stocks_us': [], 'etfs_us': []},
                        raising=False)
    yield
    instruments._ETF_ETN_CACHE.clear()


def test_이름없이_물은_결과는_캐시에_굳지_않는다(clean):
    """이름이 없으면 휴리스틱이 성립하지 않는다 — 그 False 는 판정이 아니다."""
    assert instruments.is_domestic_etf_etn(ETF_CODE) is False
    assert ETF_CODE not in instruments._ETF_ETN_CACHE, "판정 근거 없이 캐시에 들어갔다"

    # 이름을 주면 곧바로 바로잡힌다.
    assert instruments.is_domestic_etf_etn(ETF_CODE, ETF_NAME) is True


def test_이름으로_판정한_결과는_캐시한다(clean):
    """근거가 있는 판정은 기억한다 — 매번 다시 훑으면 파이에서 낭비다."""
    assert instruments.is_domestic_etf_etn('005930', '삼성전자') is False
    assert instruments._ETF_ETN_CACHE.get('005930') is False

    assert instruments.is_domestic_etf_etn(ETF_CODE, ETF_NAME) is True
    assert instruments._ETF_ETN_CACHE.get(ETF_CODE) is True


def test_관심목록_등록은_이름_없이도_판정_근거다(clean, monkeypatch):
    monkeypatch.setattr(config.session, 'stock_data',
                        {'etfs_kr': [{'code': ETF_CODE, 'name': ETF_NAME}],
                         'stocks_kr': [], 'stocks_us': [], 'etfs_us': []}, raising=False)
    assert instruments.is_domestic_etf_etn(ETF_CODE) is True
    assert instruments._ETF_ETN_CACHE.get(ETF_CODE) is True


def test_예약_주문은_name_컬럼을_읽는다():
    """reserved_orders 테이블의 컬럼은 'name' 이다 — 'stock_name' 은 없다."""
    import ast
    import inspect

    from modules import reserved_order_monitor as rom

    src = inspect.getsource(rom)
    assert "order.get('stock_name')" not in src, (
        "예약 주문에서 없는 키('stock_name')를 읽고 있다 — 항상 None 이라 "
        "ETF 판정이 이름 없이 이뤄진다")
    assert "is_domestic_etf_etn(order['code'], order.get('name'))" in src


def test_호가_격자_차이가_실재한다():
    """이 판정이 왜 중요한지를 수치로 못박는다."""
    for price in (12345.0, 74321.0):
        plain = utils.adjust_to_tick(price, is_overseas=False, is_etf=False)
        etf = utils.adjust_to_tick(price, is_overseas=False, is_etf=True)
        assert plain != etf, f"{price}: 두 호가표가 같다면 이 판정은 무의미하다"
