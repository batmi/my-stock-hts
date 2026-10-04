"""감사 도구의 데이터 경로 — FDR 패키지 제거 뒤의 계약 (2026-10-04).

· 지수(load_index, 도구 7개 공유): KRX Open API 저장소. 종전 FDR 지수 캐시(FinanceData GitHub)는
  2026-09-17 에서 멈춰 그 뒤 감사가 지수 이력을 조용히 09-17 에서 잘라 썼다.
· 종목 일봉(stock_daily): 네이버 일봉 직접 조회 — 종전 fdr.DataReader 와 같은 원천·같은 모양.
· 어떤 모듈·도구도 FinanceDataReader 를 import 하지 않는다(requirements 에서 뺐다).
"""
import ast
import glob
import os

import pandas as pd
import pytest

from tools import audit_market_axes as ax
from tools import audit_common


def _oa_frame(dates):
    return pd.DataFrame({"date": dates, "open": [1.0] * len(dates), "high": [2.0] * len(dates),
                         "low": [0.5] * len(dates), "close": [1.5 + i for i in range(len(dates))],
                         "volume": [9.0] * len(dates)})


def test_load_index_reads_krx_openapi(monkeypatch, tmp_path):
    from modules import krx_openapi as oa
    asked = []
    monkeypatch.setattr(oa, "is_configured", lambda: True)
    monkeypatch.setattr(oa, "index_daily",
                        lambda mt, days, max_calls=None, now=None: (asked.append((mt, max_calls)),
                                                                    _oa_frame(["20091230", "20100104", "20100105"]))[1])
    dates, close = ax.load_index("KQ11", "2010-01-01", use_cache=False)
    assert asked == [("KOSDAQ", ax._INDEX_MAX_CALLS)], "max_calls 를 명시해야 백그라운드 대량 적재가 안 뜬다"
    assert [d.strftime("%Y%m%d") for d in dates] == ["20100104", "20100105"]       # start 이전은 자른다
    assert list(close) == [2.5, 3.5]


def test_load_index_fails_loudly_with_backfill_hint(monkeypatch):
    from modules import krx_openapi as oa
    monkeypatch.setattr(oa, "is_configured", lambda: True)
    monkeypatch.setattr(oa, "index_daily", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="krx_openapi_backfill"):
        ax.load_index("KS11", "2010-01-01", use_cache=False)


def test_unknown_index_is_not_guessed():
    with pytest.raises(RuntimeError, match="지원하지 않는"):
        ax.load_index("^GSPC", "2020-01-01", use_cache=False)


def test_stock_daily_keeps_the_old_fdr_shape(monkeypatch):
    from modules import krx_daily
    monkeypatch.setattr(krx_daily, "_fetch_fdr", lambda code, s, e: pd.DataFrame(
        {"date": ["20250102", "20250103"], "open": [1, 2], "high": [2, 3], "low": [0, 1],
         "close": [1.5, 2.5], "volume": [10, 20]}))
    df = audit_common.stock_daily("005930", "2025-01-01")
    assert isinstance(df.index, pd.DatetimeIndex)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert float(df["Close"].iloc[-1]) == 2.5


def test_nothing_imports_financedatareader():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for pattern in ("modules/**/*.py", "api/**/*.py", "core/**/*.py", "brokers/**/*.py", "tools/**/*.py", "main.py"):
        for path in glob.glob(os.path.join(root, pattern), recursive=True):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(n.split(".")[0] == "FinanceDataReader" for n in names):
                    offenders.append(os.path.relpath(path, root))
    assert not offenders, f"FinanceDataReader 는 requirements 에서 뺐다 — {offenders}"


def test_stale_index_cache_is_not_reused(monkeypatch, tmp_path):
    """시작일만 키인 캐시가 처음 만든 날 파일을 영영 재사용하던 자리."""
    from modules import krx_openapi as oa
    monkeypatch.setattr(ax, "INDEX_CACHE_DIR", str(tmp_path))
    pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2026-08-20"])).to_csv(tmp_path / "KS11_2010-01-01.csv")
    monkeypatch.setattr(oa, "latest_available_dd", lambda now=None: "20261002")
    monkeypatch.setattr(oa, "is_configured", lambda: True)
    monkeypatch.setattr(oa, "index_daily", lambda *a, **k: _oa_frame(["20261001", "20261002"]))
    dates, _ = ax.load_index("KS11", "2010-01-01")
    assert dates[-1].strftime("%Y%m%d") == "20261002"
    #  다시 부르면 이번엔 갱신된 캐시를 쓴다(저장소를 다시 읽지 않는다)
    monkeypatch.setattr(oa, "index_daily", lambda *a, **k: pytest.fail("신선한 캐시를 두고 다시 읽었다"))
    ax.load_index("KS11", "2010-01-01")
