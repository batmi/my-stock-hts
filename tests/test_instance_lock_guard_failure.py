"""중복 실행 차단 장치 자신이 고장 나면 그 사실이 보여야 한다.

[왜] guard_mode·guard_appkey 는 예외를 잡고 **True(=기동 허용)** 를 돌려준다. "잠금
 장치가 고장 났다고 프로그램을 못 뜨게 하지는 않는다"는 의도된 선택이다. 그런데 그
 사실을 debug 로만 남겼다 — 기본 설정(FILE_DEBUG_LEVEL=INFO)에서는 파일에도 안 남는다.
 즉 locks/ 권한이 틀어지거나 디스크가 가득 차면 중복 실행 차단이 통째로 꺼진 채 돈다.

 instance_lock 머리 주석이 그때 깨지는 것을 이미 적어 뒀다 — 텔레그램 409(명령이
 무작위로 갈린다) · KIS TPS/웹소켓/토큰 제약 공유 · trade_history.db 를 두 프로세스가
 함께 쓰기 · 램 1GB 파이의 OOM. 막지는 않되 검사를 못 했다는 사실은 반드시 보여야 한다.
 (배치 98 의 하트비트와 같은 축 — 감시 장치 자신의 고장이 조용한 자리.)
"""
import logging

import pytest

from modules import instance_lock as il


@pytest.fixture(autouse=True)
def clean():
    il._MODE_LOCKS.clear()
    il.GUARD_FAILURE = ""
    yield
    il._MODE_LOCKS.clear()
    il.GUARD_FAILURE = ""


def _break_lock_dir(monkeypatch):
    def boom():
        raise PermissionError(13, "Permission denied: 'db/locks'")
    monkeypatch.setattr(il, '_lock_dir', boom)


def test_모드_잠금_실패는_기동을_막지_않는다(monkeypatch):
    """의도된 fail-open — 이 선택 자체는 유지한다."""
    _break_lock_dir(monkeypatch)
    assert il.guard_mode("2") is True


def test_모드_잠금_실패가_운영_로그에_남는다(monkeypatch, caplog):
    _break_lock_dir(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="modules.instance_lock"):
        il.guard_mode("2")
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert loud, "debug 로만 남으면 기본 설정에서는 파일에도 안 남는다"
    assert "중복 실행 차단이 꺼진 채로" in loud[0].message, "결과를 말하지 않으면 경고가 아니다"


def test_앱키_잠금_실패도_같은_자리에_남는다(monkeypatch, caplog):
    _break_lock_dir(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="modules.instance_lock"):
        assert il.guard_appkey("SOMEKEY", label="자동매매") is True
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "앱키" in il.guard_failure_note()


def test_검사를_못한_사실이_사유와_함께_남는다(monkeypatch):
    assert il.guard_failure_note() == ""
    _break_lock_dir(monkeypatch)
    il.guard_mode("2")
    note = il.guard_failure_note()
    assert note and "PermissionError" in note and "모드(2)" in note


def test_정상일_때는_아무_사유도_남지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(il, '_lock_dir', lambda: str(tmp_path))
    assert il.guard_mode("9") is True
    assert il.guard_failure_note() == ""
    il.release_mode("9")


def test_기동_안내가_그_사실을_띄운다(monkeypatch, capsys):
    """로그는 사고가 난 뒤에야 열어 본다 — 그때는 이미 두 인스턴스가 돌고 있다."""
    import config
    import main

    _break_lock_dir(monkeypatch)
    monkeypatch.setattr(main, '_detect_appkey_duplicates', lambda: [])
    main._enforce_single_instance("2", allow_duplicate=False)
    out = capsys.readouterr().out
    assert "중복 실행 검사를 하지 못했습니다" in out, out
    assert "OOM" in out, "무엇이 깨지는지 말하지 않으면 안내가 아니다"
