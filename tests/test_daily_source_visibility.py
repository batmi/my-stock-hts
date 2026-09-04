"""감사가 **어느 일봉 위에서** 돌았는지 표에 남는가.

[배경] 국내 일봉은 KRX 공식(pykrx/FDR)이 1순위이고, 실패하면 종목 단위로 조용히
yfinance 로 넘어간다. yfinance 종가는 237거래일 중 2~4일이 KRX 와 어긋나며(최대 1.59%),
손절·익절 판정이 종가 비교라 그 며칠이 거래를 바꾼다. 감사를 병렬로 돌리면 KRX
레이트리밋에 걸려 폴백이 무더기로 나는데([[audit-parallel-data-integrity]]),
폴백은 종목당 WARNING 한 줄로만 남고 감사 CLI 는 로그를 콘솔에 띄우지 않는다 —
**표만 보면 두 감사가 같은 데이터 위에서 잰 것인지 알 수 없었다.**

수급 축은 2026-09-01 에 같은 이유로 announce_smart_money_source 를 얻었다.
일봉은 그보다 근본적인 입력인데 빠져 있었다.
"""
import pytest

from modules import backtest
from modules import portfolio_backtest as pb


@pytest.fixture(autouse=True)
def clean():
    backtest.reset_daily_source()
    yield
    backtest.reset_daily_source()


def _seed(sources):
    for i, src in enumerate(sources):
        backtest._DAILY_SOURCE[f"{i:06d}"] = src


def test_an_all_krx_run_is_summarised_quietly(caplog):
    _seed(["KRX/pykrx"] * 5)
    with caplog.at_level("INFO", logger=pb.logger.name):
        dist = pb.announce_daily_source()
    assert dist == {"KRX/pykrx": 5}
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_a_single_fallback_is_made_loud(caplog):
    """한 종목만 넘어가도 그 실행은 '섞인 데이터' 위에 있다."""
    _seed(["KRX/pykrx"] * 9 + ["yfinance"])
    with caplog.at_level("WARNING", logger=pb.logger.name):
        pb.announce_daily_source()
    warns = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert warns, "폴백이 있는데 조용하다"
    assert "yfinance 1종목" in warns[0], warns[0]
    assert "손절" in warns[0], "무엇이 달라지는지 말하지 않으면 경고가 아니다"


def test_the_chart_api_fallback_counts_too():
    """250봉 상한 경로 — 절단 경고와는 별개로 '출처가 다르다'는 사실이 남아야 한다."""
    _seed(["KRX/fdr", "차트API", "차트API"])
    assert pb.announce_daily_source() == {"KRX/fdr": 1, "차트API": 2}


def test_failures_are_counted_not_dropped():
    """받지 못한 종목이 분포에서 사라지면 '전부 KRX' 처럼 보인다."""
    backtest._DAILY_SOURCE["000000"] = "KRX/pykrx"
    backtest._DAILY_SOURCE["000001"] = None
    assert pb.announce_daily_source() == {"KRX/pykrx": 1, "실패": 1}


def test_nothing_is_said_when_nothing_was_prepared():
    assert pb.announce_daily_source() == {}


def test_the_gate_resets_between_runs():
    """앞 실행의 출처가 남으면 다음 감사의 표가 틀린 전제를 단다."""
    _seed(["yfinance"] * 3)
    backtest.reset_daily_source()
    assert backtest.daily_source_summary() == {}


def test_prepare_universe_announces_both_sources():
    """수급과 일봉 둘 다 같은 문에서 알려야 한다 — 한쪽만 있으면 나머지는 안 보인다."""
    import inspect
    src = inspect.getsource(pb.prepare_universe)
    assert "announce_smart_money_source()" in src
    assert "announce_daily_source()" in src
    assert "reset_daily_source()" in src, "리셋이 없으면 앞 실행 값이 섞인다"


# ─────────── 표본이 줄어든 채 도는 실행 ───────────

def _prepare(monkeypatch, n_targets, n_fail, caplog_level="WARNING"):
    """prepare_universe 를 얇은 대역 위에서 돌린다 — 네트워크 없이 실패 비율만 만든다."""
    import pandas as pd

    calls = {"i": 0}

    def fake_get(code, is_overseas, days):
        calls["i"] += 1
        if calls["i"] <= n_fail:
            raise RuntimeError("KRX 레이트리밋")
        n = 500
        return pd.DataFrame({
            "date": [f"2024{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
            "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
            "close": [100.0] * n, "volume": [1000] * n,
        })

    monkeypatch.setattr(backtest, "get_backtest_data", fake_get)
    monkeypatch.setattr(backtest, "_append_smart_money_signal", lambda df, *a, **k: df)
    monkeypatch.setattr(backtest, "compute_price_indicators", lambda df: df)
    monkeypatch.setattr(backtest, "prepare_market_filter", lambda *a, **k: None)
    monkeypatch.setattr(backtest, "prepare_vol_regime", lambda *a, **k: None)
    monkeypatch.setattr(pb, "announce_smart_money_source", lambda *a, **k: {})
    monkeypatch.setattr(pb, "announce_daily_source", lambda *a, **k: {})
    monkeypatch.setattr(pb, "warn_if_unmodeled", lambda *a, **k: [])

    targets = [(f"{i:06d}", f"종목{i}") for i in range(n_targets)]
    return pb.prepare_universe(targets, days=365)


def test_a_shrunken_universe_is_announced(monkeypatch, caplog):
    """요청의 절반이 빠졌는데 표는 평소처럼 나온다 — 그 사실이 어딘가에는 남아야 한다."""
    with caplog.at_level("WARNING", logger=pb.logger.name):
        dfs, _, _, failed = _prepare(monkeypatch, n_targets=10, n_fail=5)
    assert len(dfs) == 5 and len(failed) == 5
    warns = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("표본이 줄어든" in m for m in warns), warns


def test_a_couple_of_delistings_stay_quiet(monkeypatch, caplog):
    """상장폐지 몇 건은 정상이다 — 매번 경고하면 아무도 안 읽는다."""
    with caplog.at_level("WARNING", logger=pb.logger.name):
        dfs, _, _, failed = _prepare(monkeypatch, n_targets=20, n_fail=1)
    assert len(failed) == 1
    assert not [r for r in caplog.records
                if r.levelname == "WARNING" and "표본이 줄어든" in r.message]


def test_the_drop_reason_is_recorded(monkeypatch, caplog):
    """종전에는 예외를 통째로 삼켜 '왜 빠졌는지'가 어디에도 없었다."""
    with caplog.at_level("DEBUG", logger=pb.logger.name):
        _prepare(monkeypatch, n_targets=4, n_fail=1)
    assert any("레이트리밋" in r.message for r in caplog.records), \
        "제외 사유가 남지 않는다 — 레이트리밋인지 상장폐지인지 구분할 수 없다"
