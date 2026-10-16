"""화면·백테스트 기능이 전역 설정을 '임시로' 바꾸지 않는다 — 백그라운드 자동매매로 새기 때문.

[무엇이 있었나 · 2026-10-16] 자동매매(AutoTrader)는 같은 프로세스의 백그라운드 스레드이고,
 매 주기 build_buy_thresholds 가 config.ANALYSIS_THRESHOLDS 를 다시 읽는다. 그런데
   · 메뉴 2-7 전체 종목 분석이 화면 문턱(BUY_SCORE·BUY_RSI_MAX·RISE_SCORE·BUY_VOL_STRENGTH)을
     분석 수 분 동안 그 딕셔너리에 써 두었다가 되돌렸다 — 체결강도 옵션을 끄면 0.0 까지.
   · 백테스트 메뉴가 USE_MARKET_FILTER·CHART_LOOKBACK_DAYS 를 호출 동안 바꿨다 되돌렸다.
 그 창 안에서 실매매는 화면용으로 낮춘 문턱·꺼진 게이트로 판단할 수 있었다. 둘 다 화면
 결과에는 기여하지 않았다(값은 이미 인자로도 넘어가고 있었다). 이제 인자로만 넘긴다.

[가드] tools/ 는 별도 프로세스라 자동매매와 상태를 공유하지 않으므로 대상이 아니다.
 설정을 **영구히** 바꾸는 정식 경로(modules/settings.py 메뉴·저장)도 대상이 아니다.
"""
import os
import re
import sys

import pandas as pd
import pytest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import analysis, backtest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("modules", "core", "api", "brokers")
EXEMPT_FILES = {os.path.join("modules", "settings.py")}   # 설정을 영구히 바꾸는 정식 경로
# 의도된 런타임 상태 플래그(판단 설정이 아니다). 새로 넣을 때는 이유를 함께 적을 것.
ALLOWED = {("main.py", "config.WEBCHART_ACTIVE = True")}

_WRITE = re.compile(
    r"config\.[A-Z][A-Z0-9_]*\[[^]]+\]\s*=[^=]"                       # 그룹 딕셔너리 항목 쓰기
    r"|config\.[A-Z][A-Z0-9_]*\.(update|pop|clear|setdefault)\("      # 그룹 딕셔너리 변형
    r"|(^|[^.\w])config\.[A-Z][A-Z0-9_]*\s*=[^=]"                     # 필드 대입
    r"|setattr\(config,")


def _sources():
    for d in SCAN_DIRS:
        for base, _dirs, files in os.walk(os.path.join(ROOT, d)):
            for f in files:
                if f.endswith(".py"):
                    rel = os.path.relpath(os.path.join(base, f), ROOT)
                    if rel not in EXEMPT_FILES:
                        yield rel
    yield "main.py"


def test_runtime_code_does_not_write_global_config():
    offenders = []
    for rel in _sources():
        for i, line in enumerate(open(os.path.join(ROOT, rel), encoding="utf-8"), 1):
            code = line.split("#", 1)[0]
            if _WRITE.search(code) and (rel, code.strip()) not in ALLOWED:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "전역 설정을 런타임에 바꾸는 코드 — 인자로 넘길 것:\n" + "\n".join(offenders)


def test_guard_pattern_catches_the_old_forms():
    for src in ('config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = params["BUY_SCORE"]',
                'config.ANALYSIS_THRESHOLDS.clear()',
                'config.USE_MARKET_FILTER = use_market_filter',
                "setattr(config, 'X', 1)"):
        assert _WRITE.search(src), src
    assert not _WRITE.search('x = config.ANALYSIS_THRESHOLDS["BUY_SCORE"] == 7')


def test_market_analysis_worker_takes_thresholds_from_params(monkeypatch):
    """2-7 워커는 화면 문턱을 params 로만 받는다 — 전역 설정이 바뀌지 않아도 결과가 같다."""
    n = 300
    df = pd.DataFrame({"date": pd.bdate_range("2025-01-02", periods=n).strftime("%Y%m%d"),
                       "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0})
    monkeypatch.setattr(analysis.api, "get_chart_data", lambda *a, **k: df.copy())
    monkeypatch.setattr(analysis.api, "chart_overlay_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(analysis.api, "display_price_krx_fixed", lambda *_a, **_k: True)
    monkeypatch.setattr(analysis.api, "chart_overlay_price", lambda *_a, **_k: 0)
    monkeypatch.setattr(analysis, "check_smart_money_turnaround", lambda *_a, **_k: (False, ""))
    seen = {}
    real = analysis.classify_stock_state

    def spy(**kw):
        seen["thresholds"] = kw.get("thresholds")
        seen["config_buy"] = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        return real(**kw)
    monkeypatch.setattr(analysis, "classify_stock_state", spy)
    before = dict(config.ANALYSIS_THRESHOLDS)
    params = {"BUY_SCORE": 1.5, "BUY_RSI_MAX": 99, "RISE_SCORE": 1.0, "BUY_VOL_STRENGTH": 0.0,
              "MR_VOL_STRENGTH": 0.0, "OUTPUT_FILTER": "ALL", "WEIGHTS": config.SCORING_WEIGHTS,
              "USE_VOL": False}
    analysis._analyze_stock_worker({"code": "005930", "name": "x"}, params)
    assert seen["thresholds"] is params
    assert seen["config_buy"] == before["BUY_SCORE"]
    assert dict(config.ANALYSIS_THRESHOLDS) == before


def test_prepare_market_filter_follows_argument_not_global(monkeypatch):
    monkeypatch.setattr(config, "USE_MARKET_FILTER", True, raising=False)
    monkeypatch.setattr(backtest, "_MARKET_FILTER_STATE", {"dates": None, "desc": "", "key": None})
    with patch("api.fetch_yfinance_data") as mock_fetch:
        assert backtest.prepare_market_filter("005930", is_overseas=False, days=30,
                                              use_filter=False) is None
    mock_fetch.assert_not_called()
    assert config.USE_MARKET_FILTER is True
