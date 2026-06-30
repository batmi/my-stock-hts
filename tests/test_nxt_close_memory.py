import os
import sys
from datetime import datetime, timedelta

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


def test_remember_then_recall(tmp_path):
    _reset(tmp_path)
    api._nxt_remember_close("005930", 70000)
    assert api._nxt_recalled_close("005930") == 70000


def test_remember_ignores_nonpositive(tmp_path):
    _reset(tmp_path)
    api._nxt_remember_close("005930", 0)
    api._nxt_remember_close("000660", -5)
    assert api._nxt_recalled_close("005930") == 0
    assert api._nxt_recalled_close("000660") == 0


def test_recall_rejects_stale(tmp_path):
    _reset(tmp_path)
    old = (datetime.now() - timedelta(days=api._NXT_RECALL_MAX_AGE_DAYS + 1)).strftime("%Y%m%d")
    with api._nxt_last_close_lock:
        api._nxt_last_close["000660"] = {"price": 50000, "date": old}
    assert api._nxt_recalled_close("000660") == 0


def test_recall_accepts_within_window(tmp_path):
    _reset(tmp_path)
    recent = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")  # 주말 가정
    with api._nxt_last_close_lock:
        api._nxt_last_close["000660"] = {"price": 51000, "date": recent}
    assert api._nxt_recalled_close("000660") == 51000


def test_recall_none_when_absent(tmp_path):
    _reset(tmp_path)
    assert api._nxt_recalled_close("999999") == 0


def test_persistence_roundtrip(tmp_path):
    _reset(tmp_path)
    api._nxt_remember_close("005930", 70000)
    api._nxt_save_last_close(force=True)           # 즉시 저장
    assert os.path.exists(api._nxt_close_file())
    # 새 세션처럼 메모리 비우고 디스크에서 재로딩
    api._nxt_last_close.clear()
    api._nxt_last_close_loaded = False
    assert api._nxt_recalled_close("005930") == 70000
