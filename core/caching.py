# caching.py
"""공용 인메모리 TTL 캐시 유틸리티.

모듈마다 dict + RLock + TTL 검사 + eviction을 각자 재구현하던 패턴을 일원화한다.
내부 모듈 의존성이 없는 최하위 유틸리티로 모든 계층에서 import할 수 있다.

메모리 보호: max_size를 지정하면 상한 초과 시 가장 오래된 항목부터 제거하여
라즈베리파이(1GB) 등 제약 환경에서도 무한 증가하지 않는다.
"""
import threading
import time


class TTLCache:
    """스레드 안전 TTL 캐시.

    - get(key, ttl): ttl(초) 이내의 항목만 반환, 그 외 None
    - set(key, value): 현재 시각으로 저장 (상한 초과 시 오래된 항목 자동 제거)
    - 레거시 dict 호환: cache[key] = value, len(cache), key in cache, clear()
    """

    def __init__(self, max_size=0):
        self._store = {}  # key -> (timestamp, value)
        self._lock = threading.RLock()
        self._max_size = max_size

    def get(self, key, ttl):
        """ttl(초) 이내에 저장된 값을 반환한다. 없거나 만료 시 None."""
        with self._lock:
            item = self._store.get(key)
            if item is not None and (time.time() - item[0]) < ttl:
                return item[1]
        return None

    def set(self, key, value):
        with self._lock:
            self._store[key] = (time.time(), value)
            self._evict_locked()

    def _evict_locked(self):
        """상한 초과 시 가장 오래된 항목부터 제거해 90% 수준으로 낮춘다.
        (eviction 빈도를 줄이기 위해 한 번에 여유분까지 비운다. 락 보유 상태에서 호출)"""
        if not self._max_size or len(self._store) <= self._max_size:
            return
        drop = len(self._store) - int(self._max_size * 0.9)
        for k in sorted(self._store, key=lambda k: self._store[k][0])[:drop]:
            self._store.pop(k, None)

    def clear(self):
        with self._lock:
            self._store.clear()

    def pop(self, key, default=None):
        with self._lock:
            item = self._store.pop(key, None)
        return item[1] if item is not None else default

    # ── 레거시 dict 스타일 호환 ──
    def __setitem__(self, key, value):
        self.set(key, value)

    def __contains__(self, key):
        with self._lock:
            return key in self._store

    def __len__(self):
        with self._lock:
            return len(self._store)
