"""TTL 캐시가 **만료된 값을 유효한 것처럼** 답하지 못하게 한다.

[왜 · 2026-09-08 감사] `TTLCache` 는 dict 호환용으로 `__contains__` 를 갖고 있었는데,
`key in cache` 는 ttl 을 받을 수 없어 **만료된 항목에도 True** 를 돌려줬다. 이 클래스를
쓰는 자리는 전부 '유효한 값이 있는가'를 묻는 곳이라 그 True 는 낡은 값을 유효한 것으로
읽게 만든다 — 이 저장소가 반복해서 고쳐 온 실패 유형이다([[unknown-vs-empty]]).

지금 `in` 을 쓰는 곳은 없다. 그래서 메서드를 없앴다 — 앞으로 그렇게 쓰는 코드가 조용히
낡은 값을 읽는 대신 그 자리에서 멈춘다. 유효성 검사는 `get(key, ttl) is not None` 이다.
"""
import time

import pytest

from core.caching import TTLCache


def test_get_honours_ttl():
    c = TTLCache()
    c.set("k", "v")
    assert c.get("k", ttl=10) == "v"
    assert c.get("k", ttl=0) is None, "ttl 0 이면 유효한 값이 없다"


def test_expired_value_is_not_served():
    c = TTLCache()
    c.set("k", "v")
    time.sleep(0.02)
    assert c.get("k", ttl=0.01) is None


def test_membership_check_is_refused_not_silently_stale():
    """`in` 은 ttl 을 못 받는다 — 조용히 참을 돌려주느니 막는다."""
    c = TTLCache()
    c.set("k", "v")
    with pytest.raises(TypeError):
        "k" in c          # noqa: B015 - 이 표현식 자체가 검사 대상이다


def test_eviction_keeps_newest():
    c = TTLCache(max_size=3)
    for i in range(10):
        c.set(f"k{i}", i)
    assert len(c) <= 3
    assert c.get("k9", ttl=10) == 9


def test_pop_returns_value_and_removes():
    c = TTLCache()
    c.set("k", "v")
    assert c.pop("k") == "v"
    assert c.get("k", ttl=10) is None
    assert c.pop("k", "기본값") == "기본값"
