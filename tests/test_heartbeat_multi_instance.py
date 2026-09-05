"""한 기기에서 두 모드를 돌려도 감시자가 각각을 본다.

[배경 · 2026-09-04 실측] 하트비트는 모드를 가리지 않고 logs/heartbeat.json 하나를 썼다.
기기가 다르면(파이=가상투자 / 맥북=실전) 파일도 달라 문제가 없지만, 모드 잠금은
**다른 모드끼리는 동시 실행을 허용**한다. 한 기기에서 실전과 토스를 함께 띄우면:
  · 두 스케줄러가 같은 파일에 번갈아 도장을 찍는다 → 한쪽이 죽어도 다른 쪽 도장이 계속
    갱신돼 감시자는 영원히 'ok'. 프로세스 사망 감시가 통째로 무력해진다.
  · 텔레그램을 끈 채 띄운 인스턴스가 시작하며 남기는 '정상 종료' 표식(main.py)이 살아
    있는 다른 인스턴스의 도장을 덮어, 감시자가 아예 침묵한다.
둘 다 실측으로 재현됐다. 인스턴스마다 파일을 갈라 각각 감시한다.

프로세스 사망은 알리기만 하고 되살리지 않는다는 규약은 그대로다.
"""
import json
import os
import time

import pytest
from unittest.mock import patch

from modules import heartbeat as hb


@pytest.fixture
def hb_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", str(tmp_path / "heartbeat.json"))
    monkeypatch.setattr(hb, "ALERT_STATE_PATH", str(tmp_path / "alert.json"))
    sent = []
    monkeypatch.setattr(hb, "_send_telegram", lambda t: (sent.append(t), (True, ""))[1])
    return {"dir": tmp_path, "sent": sent}


def _stale(mode, instance, now, age=400):
    """이미 죽은 인스턴스의 마지막 도장."""
    hb._atomic_write(hb.path_for(mode), {
        "state": "alive", "ts": now - age, "iso": "old", "deadline": now - age + 240,
        "pid": 1, "host": "mac", "mode": mode, "instance": instance})


# ─────────────────────────────────────────────
# 1. 파일이 갈리는가
# ─────────────────────────────────────────────

def test_each_mode_stamps_its_own_file(hb_dir):
    hb.beat(interval_sec=60, mode="실전", instance="MAC-REAL")
    hb.beat(interval_sec=60, mode="토스", instance="MAC-TOSS")

    names = {os.path.basename(p) for p in hb.instance_paths()}
    assert len(names) == 2, f"도장이 한 파일에 겹쳤다: {names}"
    assert hb.read(hb.path_for("실전"))["instance"] == "MAC-REAL"
    assert hb.read(hb.path_for("토스"))["instance"] == "MAC-TOSS"


def test_unknown_mode_keeps_the_legacy_path(hb_dir):
    """모드를 모르면 종전 경로 그대로 — 옛 파일·기존 cron 설정과 호환."""
    assert hb.path_for(None) == hb.HEARTBEAT_PATH
    assert hb.path_for("") == hb.HEARTBEAT_PATH


def test_temp_files_are_not_watched(hb_dir):
    hb.beat(interval_sec=60, mode="실전")
    open(hb.path_for("실전") + ".999.tmp", "w").write("{}")
    assert all(not p.endswith(".tmp") for p in hb.instance_paths())


def test_temp_name_carries_the_pid(hb_dir):
    """이름이 고정이면 두 프로세스가 상대의 반쪽 내용을 rename 으로 공표할 수 있다."""
    import inspect

    assert "os.getpid()" in inspect.getsource(hb._atomic_write)


# ─────────────────────────────────────────────
# 2. 한쪽이 죽으면 그것만 잡히는가
# ─────────────────────────────────────────────

def test_a_dead_instance_is_detected_while_the_other_lives(hb_dir):
    """[핵심] 살아 있는 쪽 도장에 가려 죽은 쪽을 놓치던 자리."""
    now = time.time()
    _stale("토스", "MAC-TOSS", now)
    hb.beat(interval_sec=60, running=True, mode="실전", instance="MAC-REAL")

    states = {os.path.basename(p): st for p, st, _, _ in hb.check_all()}
    assert states[os.path.basename(hb.path_for("토스"))] == "dead"
    assert states[os.path.basename(hb.path_for("실전"))] == "ok"
    assert any("MAC-TOSS" in t for t in hb_dir["sent"]), "죽은 인스턴스를 알리지 않았다"
    assert not any("MAC-REAL" in t for t in hb_dir["sent"])


def test_a_stopped_marker_does_not_silence_the_other_instance(hb_dir):
    """텔레그램 없이 띄운 인스턴스의 '정상 종료' 표식이 남의 감시를 끄면 안 된다."""
    now = time.time()
    hb.beat(interval_sec=60, running=True, mode="실전", instance="MAC-REAL")
    hb.stopped(reason="하트비트 미가동(텔레그램 알림 비활성)", mode="토스")

    states = {os.path.basename(p): st for p, st, _, _ in hb.check_all(notify=False)}
    assert states[os.path.basename(hb.path_for("토스"))] == "stopped"
    assert states[os.path.basename(hb.path_for("실전"))] == "ok"

    # 실전이 그 뒤 죽으면 정상적으로 알린다
    _stale("실전", "MAC-REAL", time.time())
    assert any(st == "dead" for _, st, _, _ in hb.check_all())
    assert any("MAC-REAL" in t for t in hb_dir["sent"])


def test_both_dying_alerts_twice(hb_dir):
    """한 칸짜리 기억을 쓰면 두 번째 사망이 '이미 알린 건'으로 삼켜진다."""
    now = time.time()
    _stale("실전", "MAC-REAL", now)
    _stale("토스", "MAC-TOSS", now, age=401)   # ts 가 겹치지 않게

    hb.check_all()
    assert any("MAC-REAL" in t for t in hb_dir["sent"])
    assert any("MAC-TOSS" in t for t in hb_dir["sent"])


def test_the_same_death_is_reported_only_once(hb_dir):
    """cron 이 5분마다 도는데 매번 보내면 알림이 소음이 된다."""
    _stale("실전", "MAC-REAL", time.time())
    hb.check_all()
    n = len(hb_dir["sent"])
    hb.check_all()
    assert len(hb_dir["sent"]) == n


def test_alert_memory_is_kept_per_instance(hb_dir):
    _stale("실전", "MAC-REAL", time.time())
    hb.check_all()
    keys = set(json.load(open(hb.ALERT_STATE_PATH)))
    assert keys == {os.path.basename(hb.path_for("실전"))}


def test_legacy_flat_alert_state_is_still_readable(hb_dir):
    """감시자 첫 실행에서 옛 형식({'notified_ts': ...})을 만나도 깨지지 않는다."""
    now = time.time()
    _stale("실전", "MAC-REAL", now)
    data = hb.read(hb.path_for("실전"))
    hb._atomic_write(hb.ALERT_STATE_PATH, {"notified_ts": data["ts"], "delivered": True})

    state, sent = hb.check_and_notify(path=hb.path_for("실전"))
    assert state == "dead" and sent is False, "옛 기록을 못 읽어 같은 사망을 다시 알렸다"


# ─────────────────────────────────────────────
# 3. 아무것도 없을 때
# ─────────────────────────────────────────────

def test_no_heartbeat_files_is_silent(hb_dir):
    """한 번도 뜬 적 없는 기기에서 감시자를 켜면 매번 울릴 텐데, 그건 소음이다."""
    assert hb.check_all() == []
    assert hb_dir["sent"] == []


# ==========================================================
# [2026-09-05] 하트비트 파일 이름은 ASCII 만 쓴다
# ==========================================================
def test_파일_이름에_한글이_들어가지_않는다():
    """실제로 `logs/heartbeat.토스.json` 이 만들어지고 있었다.

    이 파일을 읽는 것은 cron 감시자·스크립트·로그 수집기처럼 **우리 코드 밖**이다.
    거기서 비ASCII 파일명은 로케일(LANG 이 비어 있는 cron)·전송·아카이브마다 다르게
    깨지고, 감시 장치가 조용히 대상을 잃는다. 종전 _slug 는 `c.isalnum()` 만 봤는데
    파이썬에서 한글은 alnum 이라 그대로 남았다.
    """
    for mode in ("가상투자", "토스", "실전", "KIS 실전", "새로운모드"):
        #  디렉토리는 테스트 러너가 정한다 — 우리가 만드는 것은 파일 이름뿐이다.
        name = os.path.basename(hb.path_for(mode))
        assert name.isascii(), f"{mode} → {name} 에 비ASCII 문자가 있다"


def test_모드마다_다른_파일로_떨어진다():
    """ASCII 로 바꾸다가 두 모드가 같은 이름이 되면 감시가 통째로 무력해진다."""
    modes = ("가상투자", "토스", "실전", "한글모드하나", "한글모드둘")
    paths = [os.path.basename(hb.path_for(m)) for m in modes]
    assert len(set(paths)) == len(modes), f"파일 이름이 겹친다: {paths}"
    #  전부 비ASCII 인 라벨도 공용 경로(heartbeat.json)로 떨어지면 안 된다 —
    #  다른 모드의 도장을 덮어쓴다.
    assert os.path.basename(hb.path_for("한글모드하나")) != os.path.basename(hb.HEARTBEAT_PATH)


def test_옛_한글_이름_파일은_도장을_찍을_때_정리된다(tmp_path):
    """남겨 두면 아무도 갱신하지 않는 파일이 약속 시각을 넘겨 **가짜 사망 알림**이 된다.

    instance_paths 가 `heartbeat.*.json` 을 전부 감시 대상으로 잡기 때문이다.
    """
    base = str(tmp_path / "heartbeat.json")
    legacy = hb.path_for("토스", base, _slug_fn=hb._legacy_slug)
    current = hb.path_for("토스", base)
    assert legacy != current and "토스" in legacy

    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8") as f:
        f.write('{"state": "alive", "ts": 0, "deadline": 0}')

    with patch.object(hb, "HEARTBEAT_PATH", base):
        hb.beat(interval_sec=60, mode="토스")

    assert not os.path.exists(legacy), "옛 한글 이름 파일이 그대로 남았다"
    assert os.path.exists(current)
    assert hb.instance_paths(base) == [current]
