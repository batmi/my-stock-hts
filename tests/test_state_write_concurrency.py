"""상태 파일 쓰기: **스레드가 둘이어도 옛 내용 아니면 새 내용이다**.

[왜 이 파일이 있나 · 2026-09-07]
core/jsonio 는 2026-09-04 에 '반쪽짜리 JSON 이 남지 않게' 원자적 쓰기로 바꿨다
(tests/test_jsonio_durability.py). 그 구현의 임시 파일 이름이 `<path>.<PID>.tmp`
였다 — 프로세스가 둘이면 갈라지지만 **같은 프로세스의 스레드끼리는 이름이 하나**다.
두 스레드가 같은 경로를 저장하면 한 임시 파일에 번갈아 쓰고 둘 다 승격시킨다.
원자적으로 갈아 끼우는 대상이 '섞인 내용'이라, 막으려던 것이 그대로 남는다.
실측: 두 내용을 네 스레드로 40회 저장하니 25회가 깨졌다(JSONDecodeError: Extra data).

이 시스템은 스레드가 여럿이다 — 매매 루프·스케줄러·텔레그램 봇·저널 동기화.

원자적 쓰기가 못 막는 것이 하나 더 있다: **소실 갱신**. 파일 전체를 읽어 한 항목만
바꾸고 통째로 다시 쓰는 자리(session.set_token)는, 두 스레드가 같은 옛 사본을 읽으면
각자의 항목만 넣고 덮어써 나중 쪽만 남는다. 파일은 멀쩡한데 내용이 사라진다 —
그래서 원시 함수 수정과 호출부 락은 **둘 다** 필요하다.
실측: 세 토큰을 세 스레드로, 인위적 지연 없이 200회 반복하니 200회 모두 소실됐다.
파일 IO 동안 GIL 이 놓이므로 창이 좁지 않다.

잃는 것이 자동매매 계좌 토큰이면 재기동 때 다시 발급해야 하는데, KIS 는 앱키당
1분 1회 제한(EGW00133)이 있어 하필 그 순간에 막힌다([[token-memory-expiry]]).
"""
import json
import os
import threading

import pytest

import config
from core import jsonio


BIG = {"who": "A", "items": [f"A{i:04d}" for i in range(4000)]}
SMALL = {"who": "B", "items": [f"B{i:04d}" for i in range(400)]}


def _race(path, payloads, rounds=40):
    """같은 경로에 서로 다른 내용을 여러 스레드로 저장하고, 깨진 횟수를 센다."""
    broken = 0
    for _ in range(rounds):
        threads = [threading.Thread(target=jsonio.save_json, args=(path, d))
                   for d in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            got = json.load(open(path, encoding="utf-8"))
        except Exception:
            broken += 1
            continue
        if got not in payloads:
            broken += 1
    return broken


def test_여러_스레드가_같은_파일을_저장해도_섞이지_않는다(tmp_path):
    path = str(tmp_path / "state.json")
    assert _race(path, [BIG, SMALL, BIG, SMALL]) == 0


def test_임시_파일_이름이_스레드마다_다르다(tmp_path, monkeypatch):
    """이름이 하나면 위 검사는 타이밍에 기대게 된다 — 원인을 직접 못 박는다."""
    seen = []
    real_open = open

    def spy(p, *a, **k):
        if str(p).endswith(".tmp"):
            seen.append(os.path.basename(str(p)))
        return real_open(p, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    path = str(tmp_path / "state.json")
    threads = [threading.Thread(target=jsonio.save_json, args=(path, {"n": i}))
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 6
    assert len(set(seen)) == 6, f"임시 파일 이름이 겹친다: {sorted(seen)}"


def test_저장에_실패하면_임시_파일을_남기지_않는다(tmp_path):
    """이름이 유일해진 만큼 청소도 자기 몫이어야 한다 — 안 그러면 쓰레기가 쌓인다."""
    path = str(tmp_path / "state.json")
    assert jsonio.save_json(path, {"a": {1, 2}}) is False   # set 은 직렬화 불가
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


# --------------------------------------------------------------------------
# 소실 갱신 — 파일은 멀쩡한데 항목이 사라진다
# --------------------------------------------------------------------------
KEYS = ("REAL_MANUAL", "REAL_AUTO", "TOSS")


@pytest.fixture
def token_cache(tmp_path, monkeypatch):
    path = str(tmp_path / "token.json")
    monkeypatch.setattr(config, "TOKEN_CACHE_FILE", path, raising=False)
    sess = config.session
    monkeypatch.setattr(sess, "_app_key_fingerprint", lambda k: "fp")
    monkeypatch.setattr(sess, "_token_app_key", lambda k: "ak")
    monkeypatch.setattr(sess, "_update_memory_token", lambda *a, **k: None)
    return path


def test_동시에_발급된_토큰이_서로를_지우지_않는다(token_cache):
    sess = config.session
    for _ in range(30):
        jsonio.save_json(token_cache, {})
        threads = [threading.Thread(target=sess.set_token,
                                    args=(k, f"tok-{k}", "2026-09-08 00:00:00"))
                   for k in KEYS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        cache = json.load(open(token_cache, encoding="utf-8"))
        assert set(cache) == set(KEYS), f"사라진 토큰: {sorted(set(KEYS) - set(cache))}"


def test_토큰_내용은_그대로_저장된다(token_cache):
    """락을 거느라 저장 자체가 망가지면 안 된다."""
    sess = config.session
    sess.set_token("REAL_AUTO", "tok-abc", "2026-09-08 00:00:00")

    cache = json.load(open(token_cache, encoding="utf-8"))
    assert cache["REAL_AUTO"]["access_token"] == "tok-abc"
    assert cache["REAL_AUTO"]["token_expired"] == "2026-09-08 00:00:00"
    assert cache["REAL_AUTO"]["app_key_fp"] == "fp"
