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


# ── [2026-10-23] 메뉴 4 화면 대조군 · 지수 구간 덮음 가드 ──────────────────────────
def _index_db(tmp_path, monkeypatch, rows):
    db = tmp_path / "oa.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE index_daily (bas_dd TEXT, name TEXT, close REAL)")
    c.executemany("INSERT INTO index_daily VALUES (?,?,?)", rows)
    c.commit(); c.close()
    from modules import krx_openapi
    monkeypatch.setattr(krx_openapi, "_db_path", lambda: str(db))


def test_감사_도구와_화면이_같은_산식을_쓴다():
    from modules import benchmark
    assert A.buy_hold is benchmark.buy_hold and A.index_return is benchmark.index_return


def test_지수_저장분이_구간을_덮지_못하면_None(tmp_path, monkeypatch):
    """파이처럼 저장소가 1년뿐인데 3년 백테스트를 돌리면, 있는 날만으로 계산한 1년 수익을 내면 안 된다."""
    _index_db(tmp_path, monkeypatch, [("20250813", "코스피", 100.0), ("20261020", "코스피", 150.0)])
    from modules import benchmark
    assert benchmark.index_return("코스피", "20231020", "20261020") == (None, None)
    #  끝도 마찬가지 — 저장분이 한참 전에 멈췄으면 None
    assert benchmark.index_return("코스피", "20250813", "20261231") == (None, None)
    #  연휴·게시 지연 정도(14일 이내)는 같은 구간으로 본다
    ret, _ = benchmark.index_return("코스피", "20250806", "20261023")
    assert ret == pytest.approx(50.0)


def test_해외_단순_보유는_해외_비용으로_뺀다():
    from modules import benchmark
    a = _df([("20260101", 100.0), ("20260102", 120.0)])
    ret_kr, _ = benchmark.buy_hold([a], "20260101", "20260102")
    ret_us, _ = benchmark.buy_hold([a], "20260101", "20260102", is_overseas=True)
    assert ret_us == pytest.approx((1.2 - trading_cost.round_trip_cost(100, 120, 1, True) / 100) * 100 - 100)
    assert ret_kr != ret_us


def test_포트폴리오_화면_대조군_행과_생존편향_안내(tmp_path, monkeypatch):
    from unittest.mock import patch
    from modules import portfolio_backtest as pb
    _index_db(tmp_path, monkeypatch, [("20260101", "코스피", 100.0), ("20260103", "코스피", 110.0)])
    dfs = {"A": _df([("20260101", 100.0), ("20260102", 90.0), ("20260103", 120.0)])}
    dates = ["20260101", "20260102", "20260103"]
    rows = pb.benchmark_rows(dfs, dates)
    assert [r[0] for r in rows] == ["같은 종목 단순 보유", "코스피", "코스닥"]
    assert rows[1][1] == pytest.approx(10.0) and rows[2][1] is None      # 코스닥 저장분 없음
    assert pb.benchmark_rows({}, []) == []
    with patch("config.console.print") as out:
        pb.print_benchmark(dfs, dates)
    args = [c.args[0] for c in out.call_args_list if c.args]
    tables = [a for a in args if hasattr(a, "columns")]
    assert len(tables) == 1
    cells = [str(x) for col in tables[0].columns for x in col._cells]
    assert "같은 종목 단순 보유" in cells and any("자료 없음" in x for x in cells)
    texts = " ".join(str(a) for a in args if not hasattr(a, "columns"))
    assert "생존 편향" in texts and "--only index" in texts
