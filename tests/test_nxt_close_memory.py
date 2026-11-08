import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config


def _reset(tmp_path):
    """디스크 로드를 막고 메모리만 사용하도록 초기화."""
    config.DATA_DIR = str(tmp_path)
    api._nxt_last_close.clear()
    api._nxt_last_close_loaded = True   # 디스크 로드 스킵
    api._nxt_last_close_dirty = False
    api._nxt_last_close_saved_at = 0.0


def _settled(monkeypatch, day):
    """'마지막으로 마감된 거래일'을 고정한다 — 회상 유효성의 유일한 기준(벽시계 무관)."""
    monkeypatch.setattr(api, 'krx_last_settled_day', lambda: day)


def _today():
    return datetime.now().strftime("%Y%m%d")


def test_remember_then_recall(tmp_path, monkeypatch):
    """오늘 애프터에 기억한 값은 오늘 밤(마지막 마감 거래일 = 오늘) 회상된다."""
    _reset(tmp_path)
    _settled(monkeypatch, _today())
    api._nxt_remember_close("005930", 70000)
    assert api._nxt_recalled_close("005930") == 70000


def test_remember_ignores_nonpositive(tmp_path, monkeypatch):
    _reset(tmp_path)
    _settled(monkeypatch, _today())
    api._nxt_remember_close("005930", 0)
    api._nxt_remember_close("000660", -5)
    assert api._nxt_recalled_close("005930") == 0
    assert api._nxt_recalled_close("000660") == 0


def test_recall_rejects_value_from_before_last_settled_session(tmp_path, monkeypatch):
    """[실측 2026-11-08 파이 mode 1] 11-04(수) 애프터에 기억한 값을 일요일에 회상하면 안 된다 —
    그 뒤 11-05·11-06 두 세션이 마감됐다. 종전 '5일 이내' 기준은 4일 전이라 통과시켰고,
    국내 ETF 표 20종목이 수요일 종가를 현재가로 보였다(KODEX 200 89,715, 실제 79,005)."""
    _reset(tmp_path)
    _settled(monkeypatch, "20261106")
    with api._nxt_last_close_lock:
        api._nxt_last_close["069500"] = {"price": 89715, "date": "20261104"}
    assert api._nxt_recalled_close("069500") == 0


def test_recall_accepts_last_settled_session_over_weekend(tmp_path, monkeypatch):
    """금요일 애프터에 기억한 값은 주말 내내(마지막 마감 거래일 = 금요일) 회상된다."""
    _reset(tmp_path)
    _settled(monkeypatch, "20261106")
    with api._nxt_last_close_lock:
        api._nxt_last_close["000660"] = {"price": 51000, "date": "20261106"}
    assert api._nxt_recalled_close("000660") == 51000


def test_recall_rejects_when_settled_day_unknown(tmp_path, monkeypatch):
    """기준일 판정이 실패하면 회상하지 않는다(KIS 현재가로 돌아간다)."""
    _reset(tmp_path)

    def _boom():
        raise RuntimeError("calendar down")
    monkeypatch.setattr(api, 'krx_last_settled_day', _boom)
    with api._nxt_last_close_lock:
        api._nxt_last_close["000660"] = {"price": 51000, "date": _today()}
    assert api._nxt_recalled_close("000660") == 0


def test_recall_none_when_absent(tmp_path, monkeypatch):
    _reset(tmp_path)
    _settled(monkeypatch, _today())
    assert api._nxt_recalled_close("999999") == 0


def test_persistence_roundtrip(tmp_path, monkeypatch):
    _reset(tmp_path)
    _settled(monkeypatch, _today())
    api._nxt_remember_close("005930", 70000)
    api._nxt_save_last_close(force=True)           # 즉시 저장
    assert os.path.exists(api._nxt_close_file())
    # 새 세션처럼 메모리 비우고 디스크에서 재로딩
    api._nxt_last_close.clear()
    api._nxt_last_close_loaded = False
    assert api._nxt_recalled_close("005930") == 70000
