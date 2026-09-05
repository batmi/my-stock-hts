"""'전체 검색'에서 프리셋 하나가 실패해도 나머지는 끝까지 돈다.

[배경 · 2026-09-04 감사] 프리셋 실행 루프는 타임아웃만 개별 처리하고, 그 밖의 예외는
`raise e` 로 그대로 올려보냈다. 바깥 except 가 함수를 통째로 끝내므로 — 시장마다 없는
필드 하나면 충분하다 — 9개 프리셋 중 하나가 실패하면 남은 프리셋이 전부 취소되고
검색 결과 연동(개별 종목 심층 분석)까지 건너뛰었다. 실패한 프리셋만 건너뛴다.
"""
import inspect

import pytest

from modules import theme_analysis as ta


def _loop_source():
    src = inspect.getsource(ta._run_tradingview_screener)
    start = src.index("for attempt in range(3):")
    return src[start:src.index("if df is not None", start)]


def test_a_failing_preset_does_not_reraise():
    """[핵심] 이 한 줄이 나머지 프리셋을 통째로 취소시키던 자리다."""
    body = _loop_source()
    assert "raise e" not in body, "프리셋 실패가 여전히 바깥으로 나간다"
    assert "break" in body


def test_the_failure_is_visible():
    """조용히 건너뛰면 '검색 결과가 없다'와 구분되지 않는다."""
    body = _loop_source()
    assert "검색 실패" in body
    assert "logger.warning" in body


def test_timeout_still_retries_before_giving_up():
    """일시적 응답 지연은 재시도해야 한다 — 실패 처리와 섞으면 안 된다."""
    body = _loop_source()
    assert "time.sleep(1.5)" in body
    assert "attempt < 2" in body


# ─────────────────────────────────────────────
# 쿼리 생성은 화면·텔레그램이 공유하는 단일 지점이다
# ─────────────────────────────────────────────

@pytest.mark.parametrize("market", ["korea", "america"])
def test_every_preset_builds_a_query(market):
    for pid in ta.SCREENER_PRESETS:
        q, post = ta.build_screener_query(market, pid)
        assert q is not None, f"{market}/{pid} 쿼리 생성 실패"
        assert callable(post)


def test_menu_numbers_all_map_to_real_presets():
    for mk, pids in ta.SCREENER_MENU_TO_ID.items():
        for pid in pids:
            assert pid in ta.SCREENER_PRESETS, f"메뉴 {mk} 가 없는 프리셋 {pid} 을 가리킨다"
            assert ta.screener_condition_str("korea", pid), f"{pid} 조건 설명이 비었다"


def test_noise_filter_keeps_ordinary_stocks_and_drops_vehicles():
    """이름 기반 노이즈 제거 — TradingView type 필터가 못 거르는 리츠·인프라펀드용.

    (2026-09-04 실측: 맨 Fund/Trust 규칙은 미국 REIT 약 35건을 실제로 걸러내고 오배제는
     한국자산신탁 1건뿐이라, 규칙을 좁히면 오히려 나빠진다 — 현행 유지 결론의 근거를 고정한다)
    """
    import pandas as pd

    df = pd.DataFrame({"description": [
        "Samsung Electronics Co., Ltd.",
        "KB Balhae Infrastructure Fund",
        "SK REIT Co. Ltd.",
        "Vornado Realty Trust",
        "NICE Infra Co., Ltd",
    ]})
    kept = set(ta._screener_noise_filter(df)["description"])
    assert "Samsung Electronics Co., Ltd." in kept
    assert "NICE Infra Co., Ltd" in kept, "영업회사를 이름만 보고 배제했다"
    assert not (kept & {"KB Balhae Infrastructure Fund", "SK REIT Co. Ltd.",
                        "Vornado Realty Trust"})


def test_noise_filter_is_safe_on_missing_columns():
    import pandas as pd

    assert ta._screener_noise_filter(None) is None
    assert ta._screener_noise_filter(pd.DataFrame()).empty
    df = pd.DataFrame({"close": [1.0]})
    assert len(ta._screener_noise_filter(df)) == 1


# ─────────────────────────────────────────────
# [모름 vs 없음] 못 물어본 것과 해당 없음을 화면에서 가른다 (2026-09-05)
# ─────────────────────────────────────────────
#  ① 쿼리가 실패하면 종전에는 "조건에 맞는 종목이 없습니다" 가 떴다 — 시장이 조용한
#     것인지 우리가 못 물어본 것인지 화면만으로는 구별할 수 없었다.
#  ② 국내 결과는 종목명을 못 얻으면 '상장폐지'로 단정하고 버렸다. 네이버 차단·토큰
#     만료로 이름 조회가 함께 실패하면 국내 결과가 **통째로** 사라지는데 이유가
#     어디에도 남지 않았다.

import pandas as pd
from unittest.mock import MagicMock, patch


def _drive_screener(scanner, *, kor_name):
    """국내 시장 · 프리셋 3번(신고가 돌파)을 한 번 돌리고 찍힌 줄을 모은다."""
    q = MagicMock()
    q.where.return_value = q
    q.order_by.return_value = q
    q.limit.return_value = q
    q.get_scanner_data.side_effect = scanner

    cls = MagicMock()
    cls.return_value.set_markets.return_value.select.return_value = q

    class _Col:
        """비교 연산이 되는 최소 Column 대역 — MagicMock 은 '>' 를 못 받는다."""
        def __init__(self, name): self.name = name
        def _cmp(self, other): return f"{self.name}?"
        __gt__ = __lt__ = __ge__ = __le__ = __eq__ = __ne__ = _cmp
        def between(self, *a): return self.name
        def isin(self, *a): return self.name
        def has(self, *a): return self.name
        def has_none_of(self, *a): return self.name
        __hash__ = None

    printed = []
    with patch.dict('sys.modules',
                    {'tradingview_screener': MagicMock(Query=cls, Column=_Col)}), \
         patch('modules.theme_analysis.screener_liquidity_filters', return_value=([], "필터")), \
         patch('api.get_stock_name_by_code', return_value=kor_name), \
         patch('api.get_current_price_data', return_value={'rt_cd': '1'}), \
         patch('config.console.print', side_effect=lambda *a, **k: printed.append(a[0] if a else "")), \
         patch('rich.prompt.Prompt.ask', side_effect=["1", "3", "n"]):
        ta._run_tradingview_screener()
    return "\n".join(str(x) for x in printed)


def _rows(n=1):
    return pd.DataFrame({
        'name': ['005930'] * n, 'description': ['Samsung Electronics'] * n,
        'close': [60000.0] * n, 'change': [1.5] * n, 'volume': [100000.0] * n,
        'average_volume': [90000.0] * n, 'SMA20': [59000.0] * n,
        'price_52_week_high': [61000.0] * n, 'price_52_week_low': [40000.0] * n,
    })


def test_검색_실패는_해당_없음이_아니다():
    out = _drive_screener(RuntimeError("field not found"), kor_name="삼성전자")
    assert "검색하지 못했습니다" in out
    assert "조건에 맞는 종목이 없습니다" not in out, (
        "못 물어본 것을 '시장에 없다'로 적었다")


def test_이름을_못_구해_뺀_종목은_밝힌다():
    """이름 조회가 실패하면 국내 결과가 통째로 사라진다 — 그 사실을 말해야 한다."""
    out = _drive_screener(lambda: (1, _rows()), kor_name="005930")   # 조회 실패 = 코드 그대로
    assert "종목명을 얻지 못해 1종목을 제외" in out
    assert "이름 조회가 실패한 것일 수도" in out
    assert "검색하지 못했습니다" not in out, "쿼리는 성공했다"


def test_정상_결과는_종전대로_표로_나온다():
    out = _drive_screener(lambda: (1, _rows()), kor_name="삼성전자")
    assert "제외했습니다" not in out
    assert "조건에 맞는 종목이 없습니다" not in out
    assert "검색하지 못했습니다" not in out
