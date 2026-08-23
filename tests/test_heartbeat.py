"""프로세스 사망 감시(하트비트) 테스트.

고정하려는 성질은 넷이다.
 1) 도장이 살아 있으면 조용하다.
 2) 약속 시각을 넘기면 **한 번만** 알린다(cron 이 5분마다 도는데 매번 보내면 소음이 된다).
 3) 정상 종료 표식·기록 없음은 알리지 않는다(사고사와 구분되어야 한다).
 4) 되살리지 않는다 — 이 모듈에는 재기동 경로가 아예 없어야 한다.
"""
import json
import os
import sys
import time

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import heartbeat


@pytest.fixture
def hb(tmp_path, monkeypatch):
    """하트비트 파일과 알림 상태 파일을 임시 경로로 돌리고, 발신은 가로챈다."""
    sent = []
    monkeypatch.setattr(heartbeat, "ALERT_STATE_PATH", str(tmp_path / "alert.json"))
    monkeypatch.setattr(heartbeat, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    monkeypatch.setattr(heartbeat, "_send_telegram", lambda text: (sent.append(text), (True, ""))[1])
    return {"path": str(tmp_path / "heartbeat.json"), "sent": sent}


def test_missing_file_is_unknown_and_silent(hb):
    """한 번도 뜬 적 없는 기기에서 감시자를 켜도 울리지 않는다."""
    assert heartbeat.evaluate(path=hb["path"])["state"] == "unknown"
    state, notified = heartbeat.check_and_notify(path=hb["path"])
    assert state == "unknown"
    assert notified is False
    assert hb["sent"] == []


def test_fresh_beat_is_ok(hb):
    heartbeat.beat(interval_sec=60, running=True, mode="실전", path=hb["path"])
    assert heartbeat.evaluate(path=hb["path"])["state"] == "ok"
    assert heartbeat.check_and_notify(path=hb["path"]) == ("ok", False)
    assert hb["sent"] == []


def test_deadline_passed_notifies_once(hb):
    """사망 판정은 한 번만 알린다 — 같은 사망 건으로 반복 발신하지 않는다."""
    heartbeat.beat(interval_sec=60, running=True, mode="실전", instance="HTS", holdings=2,
                   path=hb["path"])
    later = time.time() + 60 * 3 + 60 + 10   # 약속 시각(간격×3 + 여유 60초)을 넘긴 시점

    state, notified = heartbeat.check_and_notify(now=later, path=hb["path"])
    assert state == "dead"
    assert notified is True
    assert len(hb["sent"]) == 1
    body = hb["sent"][0]
    assert "시스템 중단 감지" in body
    assert "자동으로 재기동하지 않습니다" in body      # 알림 전용임이 본문에 드러나야 한다
    assert "보유 종목 2개" in body

    # 두 번째 호출(다음 cron 주기)은 조용해야 한다.
    state, notified = heartbeat.check_and_notify(now=later + 300, path=hb["path"])
    assert state == "dead"
    assert notified is False
    assert len(hb["sent"]) == 1


def test_recovery_notifies_once(hb):
    """다시 도장이 찍히면 복구를 한 번 알리고, 알림 상태를 비운다."""
    heartbeat.beat(interval_sec=60, path=hb["path"])
    later = time.time() + 400
    heartbeat.check_and_notify(now=later, path=hb["path"])
    assert len(hb["sent"]) == 1

    heartbeat.beat(interval_sec=60, path=hb["path"])          # 재기동 — 새 타임스탬프
    state, notified = heartbeat.check_and_notify(path=hb["path"])
    assert state == "ok"
    assert notified is True
    assert "시스템 복구" in hb["sent"][1]

    # 복구를 알린 뒤에는 다시 조용해진다.
    assert heartbeat.check_and_notify(path=hb["path"]) == ("ok", False)
    assert len(hb["sent"]) == 2


def test_clean_shutdown_is_silent(hb):
    """정상 종료(메뉴 종료·SIGTERM)는 사고사가 아니다 — 아무리 오래 지나도 울리지 않는다."""
    heartbeat.beat(interval_sec=60, path=hb["path"])
    heartbeat.stopped(reason="스케줄러 종료", path=hb["path"])
    result = heartbeat.evaluate(now=time.time() + 86400, path=hb["path"])
    assert result["state"] == "stopped"
    state, notified = heartbeat.check_and_notify(now=time.time() + 86400, path=hb["path"])
    assert state == "stopped"
    assert notified is False
    assert hb["sent"] == []


def test_beat_never_raises(monkeypatch, tmp_path):
    """도장 찍기가 실패해도 호출부(스케줄러 루프)를 깨뜨리지 않는다."""
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(heartbeat, "_atomic_write", boom)
    heartbeat.beat(interval_sec=60, path=str(tmp_path / "x.json"))
    heartbeat.stopped(path=str(tmp_path / "x.json"))


def test_atomic_write_leaves_no_partial_file(tmp_path):
    """감시자가 반쪽 JSON 을 읽지 않도록 임시 파일 + rename 으로 쓴다."""
    p = str(tmp_path / "hb.json")
    heartbeat.beat(interval_sec=60, path=p)
    assert not os.path.exists(p + ".tmp")
    with open(p, encoding="utf-8") as f:
        assert json.load(f)["state"] == "alive"


def test_module_has_no_restart_path():
    """되살리기 금지 — 재기동에 쓰일 만한 수단이 모듈에 없어야 한다."""
    src = open(heartbeat.__file__, encoding="utf-8").read()
    for banned in ("subprocess", "os.system", "os.execv", "Popen"):
        assert banned not in src, f"하트비트 모듈에 재기동 경로로 쓰일 수 있는 {banned} 가 있다"
