"""개별 룰 입력 화면이 받는 값은 전부 저장·조회 왕복에서 살아남아야 한다.

[2026-10-24] 입력 화면(auto_trade/menu._input_and_save_rule)은 매도잔량비 기준(buy_ask_bid_ratio)과
매도잔량비 자동 연동(auto_adjust_ask_bid_ratio)을 받아 확인 표에 보여 주고 "저장되었습니다"라고 했지만,
stock_strategies 에 두 열이 없고 INSERT 에도 빠져 있어 **저장 때마다 버려졌다** — 엔진은 늘 전역값으로
판정했다. 토스는 체결강도가 없어 매도잔량비가 유일한 수급 게이트라, 룰을 저장하는 순간 운용자 지시가
조용히 무시되는 자리였다. 종전 왕복 테스트(test_db_manager)는 buy_score 하나만 읽어 보고 있었다.

열을 더하면 기존 행은 NULL 이 된다 — NULL 은 '지정 안 함 = 전역값'(engine.rule_value)이어야 하고,
rule.get(키, 기본값) 으로 읽는 자리는 None 을 받으므로 함께 막는다.
"""
import ast
import json
import os
import sqlite3

import pytest

import config
from modules import db_manager as dbm
from modules.auto_trade import engine, menu

_MENU_SRC = os.path.join(os.path.dirname(__file__), os.pardir, "modules", "auto_trade", "menu.py")

#  종전 스키마(두 열이 없던 때) — 운영 DB 가 지금 이 모양이다.
_OLD_SCHEMA = """
    CREATE TABLE stock_strategies (
        code TEXT PRIMARY KEY, name TEXT, buy_score REAL, buy_rsi REAL, buy_vol_strength REAL,
        sell_score REAL, stop_loss REAL, take_profit REAL, take_profit_rsi REAL,
        ts_activation REAL, ts_callback REAL, updated_at TEXT, memo TEXT, weights TEXT,
        invest_ratio REAL, time_stop_days INTEGER, use_atr_stop INTEGER,
        atr_stop_multiplier REAL, half_take_profit_use INTEGER)
"""


def _menu_rule_keys():
    """입력 화면이 new_strategy['키'] = ... 로 채우는 키 전부."""
    tree = ast.parse(open(_MENU_SRC, encoding="utf-8").read())
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == "new_strategy"
                    and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)):
                keys.add(t.slice.value)
    return keys


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    def _make(old_rows=None):
        path = str(tmp_path / "rules.sqlite")
        if old_rows is not None:
            conn = sqlite3.connect(path)
            conn.execute(_OLD_SCHEMA)
            for row in old_rows:
                cols = ", ".join(row)
                conn.execute(f"INSERT INTO stock_strategies ({cols}) VALUES ({', '.join('?' * len(row))})",
                             tuple(row.values()))
            conn.commit()
            conn.close()
        monkeypatch.setattr(config, "DB_FILE_PATH", path)
        mgr = dbm.DBManager()
        monkeypatch.setattr(dbm, "db", mgr)
        return mgr
    return _make


def _columns(mgr):
    conn = sqlite3.connect(mgr.db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(stock_strategies)")}
    finally:
        conn.close()


def _full_strategy():
    """입력 화면이 만드는 키 전부를 기본값과 다른 값으로 채운다."""
    return {
        "buy_score": 8.0, "buy_rsi": 65.0, "buy_vol_strength": 120.0,
        "buy_ask_bid_ratio": 1.5, "auto_adjust_ask_bid_ratio": 0,
        "sell_score": 3.5, "stop_loss": -6.0, "take_profit": 30.0, "take_profit_rsi": 80.0,
        "ts_activation": 12.0, "ts_callback": 4.0, "memo": "테스트 메모",
        "weights": {"TREND": 4.5, "MOMENTUM": 2.0, "STRENGTH": 1.5, "SYNERGY": 2.0},
        "invest_ratio": 0.2, "time_stop_days": 20, "use_atr_stop": 1,
        "atr_stop_multiplier": 2.5, "half_take_profit_use": 0,
    }


def test_입력_화면이_받는_키는_전부_테이블_열이다(fresh_db):
    keys = _menu_rule_keys()
    #  AST 가 실제로 키를 찾았는지 — 0개면 아래 검사가 공허하게 통과한다.
    assert {"buy_score", "buy_ask_bid_ratio", "auto_adjust_ask_bid_ratio", "memo"} <= keys
    missing = sorted(keys - _columns(fresh_db()))
    assert not missing, f"입력 화면은 받는데 저장할 열이 없다: {missing}"


def test_테스트용_전략은_입력_화면의_키를_전부_덮는다():
    assert _menu_rule_keys() <= set(_full_strategy())


def test_입력_화면이_받는_값은_저장_후_그대로_읽힌다(fresh_db):
    mgr = fresh_db()
    strategy = _full_strategy()
    mgr.save_stock_strategy("005930", "삼성전자", strategy)
    saved = mgr.get_stock_strategy("005930")
    for key, want in strategy.items():
        got = saved.get(key)
        if key == "weights":
            got = json.loads(got)
        assert got == want, f"{key}: 저장 {want!r} → 조회 {got!r}"


def test_예전_DB에_두_열이_더해지고_기존_행은_전역값을_따른다(fresh_db, monkeypatch):
    monkeypatch.setitem(config.ANALYSIS_THRESHOLDS, "BUY_ASK_BID_RATIO", 1.3)
    monkeypatch.setitem(config.ANALYSIS_THRESHOLDS, "AUTO_ADJUST_ASK_BID_RATIO", False)
    mgr = fresh_db(old_rows=[{"code": "005930", "name": "삼성전자", "buy_score": 8.0, "buy_rsi": 70.0,
                              "sell_score": 4.0, "stop_loss": -7.0, "take_profit": 0.0,
                              "take_profit_rsi": 0.0, "ts_activation": 10.0, "ts_callback": 5.0}])
    assert {"buy_ask_bid_ratio", "auto_adjust_ask_bid_ratio"} <= _columns(mgr)

    rule = mgr.get_stock_strategy("005930")
    assert rule["buy_ask_bid_ratio"] is None and rule["auto_adjust_ask_bid_ratio"] is None
    th = engine.build_buy_thresholds(rule=rule)
    assert th["BUY_ASK_BID_RATIO"] == 1.3
    assert th["AUTO_ADJUST_ASK_BID_RATIO"] is False
    assert th["BUY_SCORE"] == 8.0            # 지정한 값은 그대로


def test_예전_룰에서_Enter만_눌러도_입력을_마치고_전역값으로_저장된다(fresh_db, monkeypatch):
    """NULL 열이 '현재: None'으로 떠서 Enter 가 float('None') 으로 거부되면 입력을 빠져나갈 수 없다."""
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)
    mgr = fresh_db(old_rows=[{"code": "005930", "name": "삼성전자", "buy_score": 8.0, "buy_rsi": 70.0,
                              "sell_score": 4.0, "stop_loss": -7.0, "take_profit": 0.0,
                              "take_profit_rsi": 0.0, "ts_activation": 10.0, "ts_callback": 5.0,
                              "time_stop_days": 15, "use_atr_stop": 1, "atr_stop_multiplier": 2.0,
                              "half_take_profit_use": 0}])
    calls = []

    def _enter(*_a, default=None, **_k):
        calls.append(default)
        if len(calls) > 200:
            raise AssertionError("입력이 끝나지 않는다(같은 질문을 계속 다시 묻는다)")
        return default

    monkeypatch.setattr(menu.Prompt, "ask", _enter)
    monkeypatch.setattr(menu.api, "get_current_price", lambda *a, **k: 0)
    monkeypatch.setattr(menu.utils, "print_breadcrumb", lambda *a, **k: None)

    menu._input_and_save_rule("005930", "삼성전자")

    assert "None" not in [str(c) for c in calls]
    saved = mgr.get_stock_strategy("005930")
    assert saved["buy_score"] == 8.0
    assert saved["buy_ask_bid_ratio"] == config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
    assert saved["auto_adjust_ask_bid_ratio"] == (
        1 if config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True) else 0)


def test_매수_문턱을_손으로_다시_조립하는_자리가_없다():
    """rule.get('buy_ask_bid_ratio', 기본값) 은 NULL 열에 None 을 준다 — engine.rule_value/build_buy_thresholds 로."""
    import re
    root = os.path.join(os.path.dirname(__file__), os.pardir)
    pat = re.compile(r"\.get\(\s*['\"](buy_ask_bid_ratio|auto_adjust_ask_bid_ratio)['\"]\s*,")
    hits = []
    for base in ("modules", "api", "core", "brokers"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, base)):
            for f in files:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dirpath, f)
                for i, line in enumerate(open(p, encoding="utf-8"), 1):
                    if pat.search(line) and "new_strategy" not in line:
                        hits.append(f"{os.path.relpath(p, root)}:{i}")
    assert not hits, f"NULL 열에 None 을 넣는 기본값 조회: {hits}"
