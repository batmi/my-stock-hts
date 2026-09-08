"""생존 편향 표본에 **거래하지 않는 시장**이 섞이지 못하게 한다.

[왜 · 2026-09-08 감사] `dead_targets` 는 `SecuGroup == "주권"` 만 보고 시장을 안 봤다.
그래서 KONEX 폐지 종목이 그대로 들어왔다 — 실측: 폐지 표본 120 종목 중 **30개(25%)**.

이 시스템은 코스피·코스닥만 거래한다(관심종목도, 시장 필터가 아는 지수도 그 둘뿐이다).
애초에 살 수 없는 종목이 망한 것을 생존 편향으로 세면 프리미엄이 그만큼 과대평가된다 —
측정하려는 것이 아닌 것을 재는 셈이라 취향 문제가 아니라 계측 오류다.

함께 지키는 것 하나 더: 이 함수는 고른 종목의 **시장을 백테스트에 등록한다**. 폐지 종목은
현재 상장 목록에도 KIS 마스터에도 없어 시장 판정이 침묵하고, 그러면 시장 필터가 전부
코스피 지수로 걸린다(실측: 폐지 표본의 58%가 코스닥이었다).
→ [[audit-universe-market-filter-bug]] · [[survivorship-premium-2x]]
"""
import pandas as pd
import pytest

from modules import backtest
from tools import audit_universe


_ROWS = [
    # Symbol,   Name,     Market,   SecuGroup, Reason,        DelistingDate
    ("000001", "코스피폐지", "KOSPI",  "주권", "상장폐지",      "2024-01-10"),
    ("000002", "코스닥폐지", "KOSDAQ", "주권", "감사의견거절",  "2024-02-10"),
    ("000003", "코넥스폐지", "KONEX",  "주권", "상장폐지",      "2024-03-10"),
    ("000004", "합병소멸",   "KOSDAQ", "주권", "피흡수합병",    "2024-04-10"),
    ("000005", "무슨스팩",   "KOSDAQ", "주권", "상장폐지",      "2024-05-10"),
    ("000006", "옛날폐지",   "KOSPI",  "주권", "상장폐지",      "2010-01-10"),
]


@pytest.fixture
def listing(monkeypatch):
    df = pd.DataFrame(_ROWS, columns=["Symbol", "Name", "Market", "SecuGroup",
                                      "Reason", "DelistingDate"])
    monkeypatch.setattr(audit_universe, "_listing", lambda kind, **kw: df.copy())
    backtest._MARKET_TYPE_OVERRIDES.clear()
    yield
    backtest._MARKET_TYPE_OVERRIDES.clear()


def _codes(targets):
    return {c for c, _n in targets}


def test_konex_is_excluded_by_default(listing):
    """거래하지 않는 시장은 표본에서 빠진다 — 프리미엄 과대평가의 원인이었다."""
    assert "000003" not in _codes(audit_universe.dead_targets(10))


def test_tradeable_markets_are_kept(listing):
    got = _codes(audit_universe.dead_targets(10))
    assert {"000001", "000002"} <= got


def test_existing_exclusions_still_apply(listing):
    """합병 소멸·스팩·기간 밖은 종전대로 빠진다(이번 변경이 그것을 풀지 않았다)."""
    got = _codes(audit_universe.dead_targets(10))
    assert "000004" not in got, "피흡수합병은 급락으로 끝나지 않아 편향 표본이 아니다"
    assert "000005" not in got, "스팩은 전략과 무관한 소멸이다"
    assert "000006" not in got, "since 이전 폐지는 창 밖이다"


def test_market_scope_is_overridable(listing):
    """분석 목적으로 넓혀 볼 수는 있어야 한다 — 기본값만 좁힌 것이다."""
    got = _codes(audit_universe.dead_targets(10, markets=("KOSPI", "KOSDAQ", "KONEX")))
    assert "000003" in got


def test_markets_of_dead_stocks_are_registered_for_the_filter(listing):
    """폐지 종목의 시장을 백테스트에 알려 준다 — 안 하면 전부 코스피 필터를 받는다."""
    audit_universe.dead_targets(10)
    assert backtest._MARKET_TYPE_OVERRIDES.get("000002") == "KOSDAQ"
    assert backtest._MARKET_TYPE_OVERRIDES.get("000001") == "KOSPI"


def test_time_stop_arms_bracket_the_current_value():
    """시간청산 팔이 현행(15일)을 사이에 두고 놓여 있어야 비교가 성립한다.

    2026-09-08 축 B 에서 **시간청산만 순위가 뒤집혔다** — 현행풀에서 10일이 명확한
    열위인데 폐지를 섞으면 동률로 좁혀졌다. 한 점(10일)만으로는 '옮겨야 하는가'를
    답할 수 없어 12일을 세웠다.
    """
    import config
    arms = {name: dict((k, v) for _t, k, v in ov) for name, ov in audit_universe.DIALS}
    days = {v["TIME_STOP_DAYS"] for v in arms.values() if "TIME_STOP_DAYS" in v}
    current = config.SELL_STRATEGY["TIME_STOP_DAYS"]
    assert days, "시간청산 팔이 하나도 없다"
    assert all(d < current for d in days), \
        f"현행 {current}일보다 짧은 팔만 있다 — 양쪽을 보려면 긴 쪽도 필요하다(의도된 경우 이 검사를 고칠 것)"
    assert len(days) >= 2, "한 점만으로는 방향을 알 수 없다"
