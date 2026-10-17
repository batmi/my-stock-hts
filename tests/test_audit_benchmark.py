"""감사 대조군(tools/audit_common.buy_hold·index_return) — 단순 보유와 지수를 같은 규약으로 잰다.

[왜 · 2026-10-17] 감사는 팔끼리 상대 비교만 했고 '그냥 들고 있기'·지수와 견준 적이 없었다.
 대조군이 틀리면 '전략이 낫다/못하다'가 통째로 틀리므로 산식을 손으로 계산한 값과 맞춘다.
"""
import sqlite3

import pandas as pd
import pytest

from core import trading_cost
from tools import audit_common as A


def _df(pairs):
    return pd.DataFrame({"date": [d for d, _ in pairs], "close": [c for _, c in pairs]})


def test_동일가중_보유는_비용을_빼고_평균한다():
    a = _df([("20260101", 100.0), ("20260102", 110.0), ("20260103", 120.0)])
    b = _df([("20260101", 200.0), ("20260102", 180.0), ("20260103", 160.0)])
    ret, mdd = A.buy_hold([a, b], "20260101", "20260103")
    ca = trading_cost.round_trip_cost(100, 120, 1) / 100
    cb = trading_cost.round_trip_cost(200, 160, 1) / 200
    expect = ((1.2 - ca) + (0.8 - cb)) / 2 * 100 - 100
    assert ret == pytest.approx(expect)
    #  자산곡선: 1.0 → (1.1+0.9)/2=1.0 → (1.2+0.8)/2=1.0 — 낙폭 없음
    assert mdd == pytest.approx(0.0)


def test_중도에_끊긴_종목은_마지막_봉으로_남고_낙폭에_잡힌다():
    up = _df([("20260101", 100.0), ("20260102", 100.0), ("20260103", 100.0)])
    dead = _df([("20260101", 100.0), ("20260102", 50.0)])            # 이틀째 뒤 상장폐지
    ret, mdd = A.buy_hold([up, dead], "20260101", "20260103")
    assert ret == pytest.approx((1.0 + 0.5) / 2 * 100 - 100, abs=0.5)
    assert mdd == pytest.approx(-25.0)


def test_데이터가_없으면_None():
    assert A.buy_hold([None, _df([("20260101", 1.0)])], "20260101", "20260105") == (None, None)


def test_지수_구간_수익과_낙폭(tmp_path, monkeypatch):
    db = tmp_path / "oa.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE index_daily (bas_dd TEXT, name TEXT, close REAL)")
    c.executemany("INSERT INTO index_daily VALUES (?,?,?)",
                  [("20260101", "코스피", 100.0), ("20260102", "코스피", 120.0),
                   ("20260103", "코스피", 90.0), ("20260104", "코스피", 110.0)])
    c.commit(); c.close()
    from modules import krx_openapi
    monkeypatch.setattr(krx_openapi, "_db_path", lambda: str(db))
    ret, mdd = A.index_return("코스피", "20260101", "20260104")
    assert ret == pytest.approx(10.0)
    assert mdd == pytest.approx(-25.0)
    assert A.index_return("코스닥", "20260101", "20260104") == (None, None)
