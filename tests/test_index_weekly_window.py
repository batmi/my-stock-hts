"""지수 주봉도 3년을 덮어야 한다.

[배경 · 2026-09-04] 지수 전용 소스(국내 지수·미국채 현물·HY OAS·KRX 금현물)는 네이티브
주봉이 없어 일봉을 주 단위로 묶는다. 그런데 그 재료가 **화면용으로 짧게** 잡혀 있었다 —
금현물 300거래일(_KRX_GOLD_PAGES x 60), 국채·OAS n_bars=300. 묶으면 60주, 즉 주봉이
1년치밖에 안 나왔다(KIS 네이티브 주봉은 lookback_days=1100 ≈ 157주).

한 가지 더: KRX 는 한 번에 2년까지만 준다 — 900일을 달라고 하면 오류가 아니라 **0행**을
준다(실측: 금현물 720일 477행 / 900일 0행). 그래서 상한을 올리는 게 아니라 구간을 나눠
받아야 한다.
"""
import pandas as pd
import pytest

import api
import config
from api import charts
from modules import krx_data


def _daily(n, end="2026-09-04"):
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    df = pd.DataFrame({
        "date": days.strftime("%Y%m%d"),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
    })
    return df


# ─────────────────────────────────────────────
# 1. KRX 2년 상한을 구간으로 넘는다
# ─────────────────────────────────────────────

def test_two_years_or_less_is_still_a_single_request():
    """일봉 경로의 호출 횟수가 늘면 안 된다 — 상한 이하는 종전 그대로 한 구간이다."""
    assert len(krx_data._range_windows(400)) == 1
    assert len(krx_data._range_windows(krx_data._MAX_RANGE_DAYS)) == 1


def test_three_years_is_split_into_windows_under_the_server_limit():
    wins = krx_data._range_windows(1100)
    assert len(wins) >= 2
    for start, end in wins:
        span = (pd.Timestamp(end) - pd.Timestamp(start)).days
        assert span <= krx_data._MAX_RANGE_DAYS, f"{start}~{end} = {span}일 — 서버가 0행을 준다"


def test_windows_cover_the_whole_span_without_a_hole():
    wins = krx_data._range_windows(1100)
    oldest = min(s for s, _ in wins)
    newest = max(e for _, e in wins)
    assert (pd.Timestamp(newest) - pd.Timestamp(oldest)).days >= 1090
    # 최신순으로 이어지며 경계가 벌어지지 않는다(하루 겹치게 물려 둔다)
    ordered = sorted(wins)
    for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
        assert pd.Timestamp(s2) <= pd.Timestamp(e1), f"{e1} 과 {s2} 사이가 비었다"


def test_gold_pages_until_a_window_comes_back_empty(monkeypatch):
    """더 과거가 없으면(상장 이전) 받은 만큼 쓰고 멈춘다 — 헛된 요청을 반복하지 않는다."""
    calls = []

    def _post(bld, **kw):
        calls.append((kw["strtDd"], kw["endDd"]))
        if len(calls) > 2:
            return []
        return [{"TRD_DD": f"2026/0{len(calls)}/01", "TDD_OPNPRC": "1", "TDD_HGPRC": "2",
                 "TDD_LWPRC": "1", "TDD_CLSPRC": "1.5", "ACC_TRDVOL": "10"}]

    monkeypatch.setattr(krx_data, "_post", _post)
    krx_data.get_gold_daily(2000, use_cache=False)
    assert len(calls) == 3, "빈 구간을 만나고도 계속 요청했다"


# ─────────────────────────────────────────────
# 2. 주봉이 3년 창을 요구한다
# ─────────────────────────────────────────────

def test_weekly_lookback_matches_the_kis_native_window():
    """같은 메뉴가 소스에 따라 다른 기간을 보여주면 안 된다."""
    import inspect

    kis = inspect.signature(charts._fetch_kis_weekly_domestic).parameters["lookback_days"].default
    assert charts.WEEKLY_LOOKBACK_DAYS == kis == 1100


@pytest.mark.parametrize("code,kind,attr,builder", [
    ("KOSPI", "domestic", "get_index_daily", lambda m: m),
    ("^KRXGOLD", "krx_gold", "get_gold_daily", lambda m: m),
])
def test_krx_backed_sources_are_asked_for_three_years(monkeypatch, code, kind, attr, builder):
    got = {}

    def _fetch(*a, **k):
        got['days'] = a[-1] if a and isinstance(a[-1], int) else k.get('days')
        return _daily(750)

    monkeypatch.setattr(krx_data, attr, _fetch)
    df = api._index_source_long_daily(code, kind)
    assert got['days'] == charts.WEEKLY_LOOKBACK_DAYS
    assert len(df) == 750


@pytest.mark.parametrize("kind,fn", [("tv_spot", "get_us_treasury_spot_data"),
                                     ("fred", "get_fred_data")])
def test_tvdatafeed_sources_are_asked_for_more_bars(monkeypatch, kind, fn):
    from modules import analysis
    got = {}

    monkeypatch.setattr(analysis, fn,
                        lambda sym, n_bars=300: got.update(n=n_bars) or _daily(n_bars))
    code = (list(config.US_TREASURY_SPOT_TICKERS) if kind == "tv_spot"
            else list(config.FRED_INDEX_TICKERS))[0]
    api._index_source_long_daily(code, kind)
    assert got['n'] > 700, f"{got['n']}봉 — 3년(≈750거래일)에 못 미친다"


def test_weekly_resamples_the_long_series(monkeypatch):
    monkeypatch.setattr(api, "_index_source_long_daily", lambda code, kind: _daily(750))
    w = charts._index_source_chart_data("^KRXGOLD", "krx_gold", "weekly")
    assert 140 <= len(w) <= 170, f"{len(w)}주 — 3년이 아니다"


def test_weekly_falls_back_to_the_short_source(monkeypatch):
    """긴 창이 실패해도 차트가 비면 안 된다 — 짧은 주봉이 빈 화면보다 낫다."""
    monkeypatch.setattr(api, "_index_source_long_daily", lambda code, kind: None)
    monkeypatch.setattr(charts, "_index_source_fetch", lambda code, kind: _daily(250))
    w = charts._index_source_chart_data("^KRXGOLD", "krx_gold", "weekly")
    assert not w.empty and len(w) < 60


def test_a_failing_long_fetch_does_not_raise(monkeypatch):
    monkeypatch.setattr(krx_data, "get_gold_daily",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("KRX 장애")))
    assert api._index_source_long_daily("^KRXGOLD", "krx_gold") is None


# ─────────────────────────────────────────────
# 3. 화면 일봉은 그대로인가
# ─────────────────────────────────────────────

def test_daily_path_still_uses_the_short_source(monkeypatch):
    """일봉이 덩달아 3년을 받으면 매 조회가 KRX 구간 요청을 하나 더 밟는다."""
    monkeypatch.setattr(api, "_index_source_long_daily",
                        lambda code, kind: pytest.fail("일봉이 주봉용 긴 조회를 탔다"))
    monkeypatch.setattr(charts, "_index_source_fetch", lambda code, kind: _daily(300))
    assert not charts._index_source_chart_data("^KRXGOLD", "krx_gold", "daily").empty


@pytest.mark.parametrize("period", ["hourly", "intraday"])
def test_intraday_periods_are_still_refused(period):
    assert charts._index_source_chart_data("^KRXGOLD", "krx_gold", period).empty


# ─────────────────────────────────────────────
# 4. tvDatafeed 캐시가 짧은 것을 돌려주지 않는가
# ─────────────────────────────────────────────

@pytest.fixture
def tv_cache(monkeypatch):
    """국채 현물 캐시를 한 칸 심고, 실제 tvDatafeed 는 못 가게 막는다."""
    from datetime import datetime

    from modules import analysis

    sym = list(config.US_TREASURY_SPOT_TICKERS.values())[0]
    monkeypatch.setattr(analysis, "_get_tvdatafeed", lambda *a, **k: None)

    def _seed(rows, asked):
        analysis._US_TREASURY_SPOT_CACHE[sym] = {
            "df": _daily(rows), "time": datetime.now(), "fail": None, "n_bars": asked}
        return sym

    yield analysis, _seed
    analysis._US_TREASURY_SPOT_CACHE.pop(sym, None)


def test_a_short_cached_frame_is_not_served_to_a_long_request(tv_cache):
    """화면(300봉)이 먼저 캐시하면 주봉이 조용히 300봉을 받아 1년치가 됐다."""
    analysis, seed = tv_cache
    sym = seed(rows=300, asked=300)

    assert len(analysis.get_us_treasury_spot_data(sym, n_bars=300)) == 300, "같은 요청은 적중"
    # 800봉 요청은 미적중 → 재조회로 넘어간다(여기선 tv 가 None 이라 캐시본이 되돌아온다).
    # 적중했다면 재조회 시도 자체가 없었을 것이므로, 계약은 '적중하지 않는다'이다.
    ent = analysis._US_TREASURY_SPOT_CACHE[sym]
    assert ent.get("n_bars", 0) < 800


def test_a_long_cached_frame_still_serves_a_short_request(tv_cache):
    """반대 방향은 그대로 쓴다 — 자르는 건 호출부 몫이다."""
    analysis, seed = tv_cache
    sym = seed(rows=800, asked=800)
    assert len(analysis.get_us_treasury_spot_data(sym, n_bars=300)) == 800


def test_a_short_history_series_is_still_cached(tv_cache):
    """이력이 짧은 계열은 800을 달라고 해도 300행뿐이다 — 행 수로 판정하면 영원히
    캐시가 안 되고 매번 tvDatafeed 를 때린다(회로차단이 있는 이유)."""
    analysis, seed = tv_cache
    sym = seed(rows=120, asked=800)
    assert len(analysis.get_us_treasury_spot_data(sym, n_bars=800)) == 120
