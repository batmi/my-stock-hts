"""시장 구분(KOSPI/KOSDAQ) 판정 — 모르면 모른다고 해야 한다.

2026-09-04 감사: 판정이 다섯 벌로 흩어져 있었고 전부 실패 시 'KOSPI'로 폴백했다.
 - 네 벌은 현재가 응답의 rprs_mrkt_kor_name 을 읽었는데, 그 필드는 토스 모드 응답에
   아예 없다(api/toss.py `_toss_current_price_data`) → 토스에서 전 종목이 KOSPI.
 - 다섯째(_get_market_type_by_master)는 마스터 다운로드가 실패하면 두 집합이 비어
   모든 종목을 KOSPI로 판정했고, main.py 가 그것을 stock.json 에 저장했다.
시장 구분은 시장 필터(80일선)와 적응형 임계값이 볼 지수를 고르는 값이다.
"""
from unittest.mock import patch

import pytest

from modules import analysis


@pytest.fixture(autouse=True)
def _clear_master_cache():
    analysis._MASTER_KOSDAQ_CODES = None
    analysis._MASTER_KOSPI_CODES = None
    yield
    analysis._MASTER_KOSDAQ_CODES = None
    analysis._MASTER_KOSPI_CODES = None


def _masters(kospi=(), kosdaq=()):
    def _load(market):
        codes = kospi if market == 'KOSPI' else kosdaq
        return [{'code': c, 'name': c, 'grp': 'ST'} for c in codes]
    return patch.object(analysis, '_get_master_stock_list', side_effect=_load)


def test_master_failure_is_not_kospi():
    """마스터를 못 읽으면 KOSPI 가 아니라 None 이다(종전 결함의 핵심)."""
    with patch.object(analysis, '_get_master_stock_list', side_effect=Exception("boom")):
        assert analysis._get_market_type_by_master("247540") is None
        assert analysis._get_market_type_by_master("005930") is None


def test_master_resolves_both_markets():
    with _masters(kospi=("005930",), kosdaq=("247540",)):
        assert analysis._get_market_type_by_master("005930") == "KOSPI"
        assert analysis._get_market_type_by_master("247540") == "KOSDAQ"


def test_code_absent_from_loaded_masters_is_unknown():
    """마스터는 읽혔지만 종목이 없으면(신규상장·KONEX) 판정 불가다."""
    with _masters(kospi=("005930",), kosdaq=("247540",)):
        assert analysis._get_market_type_by_master("000000") is None


def test_krx_listing_covers_master_failure():
    """마스터가 죽어도 KRX 상장 목록으로 판정이 이어진다(원천 두 개)."""
    with patch.object(analysis, '_get_master_stock_list', side_effect=Exception("boom")), \
         patch('modules.krx_daily.get_market', return_value="KOSDAQ"):
        assert analysis.get_market_type("247540") == "KOSDAQ"


def test_both_sources_down_is_none():
    with patch.object(analysis, '_get_market_type_by_master', return_value=None), \
         patch('modules.krx_daily.get_market', return_value=None):
        assert analysis.get_market_type("247540") is None


def test_konex_is_not_forced_into_kospi():
    """KONEX 는 대응 지수가 없다 — KOSPI 로 밀어 넣지 않는다."""
    with patch.object(analysis, '_get_market_type_by_master', return_value=None), \
         patch('modules.krx_daily.get_market', return_value="KONEX"):
        assert analysis.get_market_type("123456") is None


@pytest.mark.parametrize("code", ["", None, "AAPL", "00593", "0059300"])
def test_non_domestic_codes_are_unknown(code):
    assert analysis.get_market_type(code) is None


def test_unresolved_is_not_cached_by_trader_resolver():
    """판정 실패를 캐시에 넣으면 프로세스가 사는 내내 그 종목이 코스피로 굳는다."""
    from modules.auto_trade import common

    cache = {}
    with patch.object(analysis, 'get_market_type', return_value=None):
        assert common.resolve_market_type("247540", cache=cache) == "KOSPI"
    assert cache == {}, "판정 불가는 캐시에 남으면 안 된다"

    with patch.object(analysis, 'get_market_type', return_value="KOSDAQ"):
        assert common.resolve_market_type("247540", cache=cache) == "KOSDAQ"
    assert cache["247540"] == "KOSDAQ"


def test_trader_resolver_prefers_watchlist_exchange(monkeypatch):
    """관심목록에 확정값이 있으면 네트워크를 타지 않는다."""
    from modules.auto_trade import common
    import config

    monkeypatch.setattr(config.session, 'stock_data',
                        {"stocks_kr": [{"code": "247540", "exchange": "KOSDAQ"}], "etfs_kr": []},
                        raising=False)
    with patch.object(analysis, 'get_market_type',
                      side_effect=AssertionError("호출되면 안 된다")):
        assert common.resolve_market_type("247540", cache={}) == "KOSDAQ"


def test_krx_daily_get_market_reads_listing():
    from modules import krx_daily

    listing = {"005930": {"name": "삼성전자", "marcap": 1, "market": "KOSPI"},
               "247540": {"name": "에코프로비엠", "marcap": 1, "market": "KOSDAQ"},
               "111111": {"name": "이름만", "marcap": 1}}
    with patch.object(krx_daily, 'get_listing_map', return_value=listing):
        assert krx_daily.get_market("005930") == "KOSPI"
        assert krx_daily.get_market("247540") == "KOSDAQ"
        assert krx_daily.get_market("111111") is None   # market 없는 옛 캐시
        assert krx_daily.get_market("999999") is None
    with patch.object(krx_daily, 'get_listing_map', return_value=None):
        assert krx_daily.get_market("005930") is None


def test_no_copies_of_the_market_classification_remain():
    """시세 응답의 시장구분 필드로 코스닥을 판정하는 사본이 다시 생기지 않게 막는다.

    그 필드는 토스 모드 응답에 없다 — 사본이 하나라도 살아나면 그 화면만 조용히
    전 종목 KOSPI 가 된다.
    """
    import io
    import pathlib
    import re
    import tokenize

    root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r"(코스닥|KOSDAQ)[^\n]{0,40}rprs_mrkt|rprs_mrkt[^\n]{0,60}(코스닥|KOSDAQ)")
    offenders = []
    for path in list((root / "modules").rglob("*.py")) + [root / "main.py"]:
        src = path.read_text(encoding='utf-8')
        #  주석·문자열(설명문)은 뺀다 — 결함을 서술한 주석까지 잡으면 가드가 못 쓰게 된다.
        lines = src.splitlines()
        code_lines = {}
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NL):
                    continue
                lineno = tok.start[0]
                if 1 <= lineno <= len(lines):
                    code_lines.setdefault(lineno, lines[lineno - 1])
        except (tokenize.TokenError, IndentationError):
            code_lines = dict(enumerate(lines, 1))
        for lineno, line in code_lines.items():
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{lineno}")
    assert not offenders, (
        "시장 구분은 analysis.get_market_type 하나만 쓴다: " + ", ".join(sorted(offenders)))
