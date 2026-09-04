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
