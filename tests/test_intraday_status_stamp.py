"""장중 시점판정 캐시는 '무엇으로 만들었는지'를 증명해야 쓰인다.

[배경] data/intraday_tv/status_*.pkl 에는 만들 당시의 임계값·가중치·룩백과 그때의
분봉으로 계산한 **진입 가부 판정까지** 구워져 있다. 그런데 표식이 없어서, 임계값을
바꾸거나 분봉을 새로 받아도 빌더는 '이미 있다'며 건너뛰고 감사 도구는 그것을 지금
설정의 판정인 양 썼다. 계측기가 조용히 다른 것을 재면 그 감사 결과는 전부 무효다
(같은 계열: audit_scale_fn 오염, 감사 유니버스 재현성).
"""
import pandas as pd
import pytest

import config
from modules import intraday_bars as ib


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "intraday_tv"
    d.mkdir()
    monkeypatch.setattr(ib, "CACHE_DIR", str(d))
    return d


def _write_bars(code, last="2026-08-14"):
    idx = pd.to_datetime([f"{last} 10:00", f"{last} 11:00"])
    df = pd.DataFrame({"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                       "close": [1.0, 1.0], "volume": [1.0, 1.0]}, index=idx)
    df.to_pickle(ib.cache_path(code, "60m"))
    return df


def test_status_written_with_stamp_is_accepted(cache_dir):
    _write_bars("005930")
    th = ib.current_thresholds()
    ib.save_status("005930", "60m", {"20260814": {"1000": (0,)}}, th, 260)

    expect = ib.status_meta(th, 260, "2026-08-14")
    got = ib.load_status("005930", "60m", expect=expect)
    assert got == {"20260814": {"1000": (0,)}}
    assert ib.STATUS_META_KEY not in got, "표식이 판정 데이터에 섞여 나오면 안 된다"


@pytest.mark.parametrize("mutate", [
    ("thresholds", lambda th: {**th, "BUY_SCORE": th["BUY_SCORE"] + 1}),
    ("weights", lambda th: {**th, "WEIGHTS": {**th["WEIGHTS"], "TREND": 99.0}}),
])
def test_changed_settings_invalidate_the_cache(cache_dir, mutate, monkeypatch):
    """임계값·가중치를 바꾸면 옛 판정은 쓰이지 않는다."""
    _write_bars("005930")
    old_th = ib.current_thresholds()
    ib.save_status("005930", "60m", {"20260814": {"1000": (0,)}}, old_th, 260)

    _, fn = mutate
    new_th = fn(old_th)
    assert ib.load_status("005930", "60m",
                          expect=ib.status_meta(new_th, 260, "2026-08-14")) is None


def test_refreshed_bars_invalidate_the_cache(cache_dir):
    """분봉을 새로 받으면(끝 날짜가 밀리면) 옛 판정은 쓰이지 않는다."""
    _write_bars("005930", last="2026-08-14")
    th = ib.current_thresholds()
    ib.save_status("005930", "60m", {"20260814": {"1000": (0,)}}, th, 260)

    _write_bars("005930", last="2026-09-03")
    assert ib.bars_last_date("005930", "60m") == "2026-09-03"
    assert ib.load_status("005930", "60m",
                          expect=ib.status_meta(th, 260, "2026-09-03")) is None


def test_stampless_legacy_cache_is_rejected(cache_dir):
    """표식이 없는 옛 캐시는 무엇으로 만들었는지 모르므로 신뢰하지 않는다."""
    _write_bars("005930")
    pd.to_pickle({"20260814": {"1000": (0,)}}, ib.status_cache_path("005930", "60m"))

    expect = ib.status_meta(ib.current_thresholds(), 260, "2026-08-14")
    assert ib.load_status("005930", "60m", expect=expect) is None
    assert ib.load_status("005930", "60m") is not None      # 검증을 안 걸면 종전대로 읽힌다


def test_gate_drops_stale_status_with_a_reason(cache_dir):
    """게이트는 낡은 판정을 조용히 넘기지 않고 사유와 함께 제외한다."""
    _write_bars("005930")
    pd.to_pickle({"20260814": {"1000": (0,)}}, ib.status_cache_path("005930", "60m"))

    daily = pd.DataFrame({"date": ["20260814"], "open": [1.0], "high": [1.0],
                          "low": [1.0], "close": [1.0]})
    bars, status, keep, drop = ib.gate_universe({"005930": daily}, "60m")

    assert keep == [] and bars == {}
    assert len(drop) == 1 and "시점판정" in drop[0][1] and "build_intraday_status" in drop[0][1]
