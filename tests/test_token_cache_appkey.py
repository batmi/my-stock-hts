"""토큰 캐시가 '발급한 앱키'까지 확인하는가.

json/token_cache.json 은 파일 하나이고 슬롯 이름이 REAL/AUTO/SIMULATION/TOSS 뿐이다.
그런데 관찰모드(mode 1)는 real_app_key·auto_app_key 를 VIRT_APP_KEY 로 덮어쓴다
(session.load_config). 즉 mode 1 가 VIRT 키로 받은 토큰이 'REAL' 슬롯에 남는다.

만료 시각만 검사하면 그 토큰이 최대 24시간 동안 '유효함'으로 통과한다. 같은 기계에서
mode 2 로 바꾸는 순간 남의 앱키 토큰을 그대로 집어 KIS 인증이 실패하고, 재발급은
앱키당 1분 1회 제한이라 복구도 느리다. 최종 운용(라즈베리파이에서 전 모드 실행)에서
바로 드러나는 구성이다.

앱키 지문(sha256 앞 16자)을 함께 저장·검사해 슬롯이 겹쳐도 서로의 토큰을 집지 않게 한다.
앱키를 교체했을 때 캐시가 조용히 낡는 문제도 같은 검사로 함께 막힌다.
"""
import json

import pytest

import config
from core.session import SessionManager


@pytest.fixture
def store(tmp_path, monkeypatch):
    """디스크 캐시를 임시 파일로 돌린 세션."""
    monkeypatch.setattr(config, "TOKEN_CACHE_FILE", str(tmp_path / "token_cache.json"))
    s = SessionManager()
    s.app_key = "SIM-KEY"
    s.real_app_key = "REAL-KEY"
    s.auto_app_key = "AUTO-KEY"
    return s


def _expiry(hours=+12):
    from datetime import datetime, timedelta
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _raw(store):
    with open(config.TOKEN_CACHE_FILE, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────
# 1. 같은 앱키 — 종전대로 캐시가 살아 있어야 한다
# ─────────────────────────────────────────────

def test_same_app_key_reuses_cached_token(store):
    store.set_token("REAL", "tok-real", _expiry())
    store.real_access_token = ""          # 메모리 캐시를 비워 파일 경로를 강제
    assert store.get_valid_token("REAL", force_disk_reload=True) == "tok-real"


def test_recently_issued_still_detected_for_same_key(store):
    store.set_token("REAL", "tok-real", _expiry())
    assert store.is_token_recently_issued("REAL", seconds=60) is True


# ─────────────────────────────────────────────
# 2. 다른 앱키 — 남의 토큰을 집으면 안 된다
# ─────────────────────────────────────────────

def test_paper_token_is_not_reused_by_real_mode(store):
    """mode 1 가 'REAL' 슬롯에 남긴 VIRT 키 토큰을 mode 2 가 재사용하면 안 된다."""
    store.real_app_key = "VIRT-KEY"                     # 관찰모드: real_app_key ← virt_app_key
    store.set_token("REAL", "tok-from-paper", _expiry())

    store.real_app_key = "REAL-KEY"                     # 실전 모드로 전환
    store.real_access_token = ""
    assert store.get_valid_token("REAL", force_disk_reload=True) is None, (
        "만료 전이라는 이유로 다른 앱키가 발급한 토큰을 집었다 — KIS 인증이 실패한다")


def test_paper_token_does_not_block_real_reissue(store):
    """다른 앱키가 방금 발급했다고 이쪽 재발급을 미루면 안 된다 (EGW00133 은 앱키 단위)."""
    store.real_app_key = "VIRT-KEY"
    store.set_token("REAL", "tok-from-paper", _expiry())

    store.real_app_key = "REAL-KEY"
    assert store.is_token_recently_issued("REAL", seconds=60) is False, (
        "남의 앱키 발급 시각을 보고 대기하면, 정작 이 앱키로는 토큰 없이 멈춘다")


def test_auto_slot_is_independent_of_real_slot(store):
    """수동키/자동매매키가 다르면 두 슬롯이 서로를 오염시키지 않는다."""
    store.set_token("REAL", "tok-real", _expiry())
    store.set_token("AUTO", "tok-auto", _expiry())
    store.real_access_token = store.auto_access_token = ""

    assert store.get_valid_token("REAL", force_disk_reload=True) == "tok-real"
    assert store.get_valid_token("AUTO", force_disk_reload=True) == "tok-auto"


def test_rotating_the_app_key_invalidates_the_cache(store):
    """~/.htsrc 에서 앱키를 교체하면 이전 토큰은 즉시 무효여야 한다."""
    store.set_token("AUTO", "tok-old", _expiry())
    store.auto_app_key = "AUTO-KEY-ROTATED"
    store.auto_access_token = ""
    assert store.get_valid_token("AUTO", force_disk_reload=True) is None


# ─────────────────────────────────────────────
# 3. 하위 호환 · 저장 형식
# ─────────────────────────────────────────────

def test_legacy_entry_without_fingerprint_is_reissued(store):
    """지문이 없는 구버전 캐시는 어느 앱키 것인지 알 수 없으므로 재발급시킨다."""
    from core.jsonio import save_json
    save_json(config.TOKEN_CACHE_FILE, {
        "REAL": {"access_token": "legacy", "token_expired": _expiry(),
                 "issued_at": "2026-01-01 00:00:00"}
    }, indent=2)
    store.real_access_token = ""
    assert store.get_valid_token("REAL", force_disk_reload=True) is None


def test_toss_slot_has_no_app_key_and_still_works(store):
    """토스 토큰은 앱키 개념이 없다 — 지문 검사를 건너뛰고 종전대로 동작해야 한다."""
    store.set_token("TOSS", "tok-toss", _expiry())
    assert store.get_valid_token("TOSS", force_disk_reload=True) == "tok-toss"
    assert store.is_token_recently_issued("TOSS", seconds=60) is True


def test_app_key_itself_is_never_written_to_disk(store):
    """지문만 남기고 앱키 원문은 파일에 쓰지 않는다."""
    store.set_token("REAL", "tok-real", _expiry())
    blob = json.dumps(_raw(store))
    assert "REAL-KEY" not in blob
    assert len(_raw(store)["REAL"]["app_key_fp"]) == 16


def test_expired_token_is_still_rejected(store):
    """지문이 맞아도 만료된 토큰은 무효다 (기존 조건이 약해지지 않았다)."""
    store.set_token("REAL", "tok-real", _expiry(hours=-1))
    store.real_access_token = ""
    assert store.get_valid_token("REAL", force_disk_reload=True) is None


# ─────────────────────────────────────────────
# 3. 메모리 경로도 만료를 본다 (2026-09-05)
#
# get_valid_token 은 파일 캐시를 _check_token_validity 로 꼼꼼히 검사하면서, **메모리
# 경로만 아무것도 안 봤다**:
#     if key == "REAL" and self.real_access_token: return self.real_access_token
#
# 한 번 메모리에 담긴 토큰은 프로세스가 사는 동안 영원히 '유효'다. KIS 토큰 수명은
# 24시간이고 운영은 라즈베리파이 24시간 구동이라 **반드시 도달한다**. 그때 만료 감지는
# 오직 사후적이다 — API 가 EGW00123 을 돌려줘야 TOKEN_EXPIRED 가 서고 예외가 난다.
# 즉 만료 경계에서 나가던 호출이 먼저 한 번 실패하고, 하필 그것이 손절 주문이면
# 그 주문이 실패한다.
def test_메모리에_담긴_만료_토큰을_돌려주지_않는다(store):
    store.set_token("REAL", "OLD-TOKEN", _expiry(hours=-1))
    assert store.real_access_token == "OLD-TOKEN", "하네스 전제: 메모리에 남아 있어야 한다"
    assert store.get_valid_token("REAL") is None, (
        "만료된 메모리 토큰을 유효한 것으로 돌려줬다")


def test_만료가_임박해도_돌려주지_않는다(store):
    """파일 경로와 같은 1분 여유 — 요청이 나가는 사이에 넘어가면 같은 실패다."""
    from datetime import datetime, timedelta
    soon = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    store.set_token("REAL", "ALMOST", soon)
    assert store.get_valid_token("REAL") is None


def test_유효한_메모리_토큰은_그대로_쓴다(store):
    """대조군 — 매번 파일을 읽으면 느려진다(이 경로가 존재하는 이유)."""
    store.set_token("REAL", "GOOD", _expiry(hours=+12))
    assert store.get_valid_token("REAL") == "GOOD"


def test_만료된_메모리_토큰은_파일_캐시로_내려간다(store):
    """다른 프로세스가 이미 갱신해 뒀을 수 있다 — 재발급 전에 파일을 본다."""
    store.set_token("REAL", "OLD", _expiry(hours=-1))
    # 다른 프로세스가 같은 슬롯을 갱신한 상황을 만든다.
    cache = _raw(store)
    cache["REAL"]["access_token"] = "FRESH"
    cache["REAL"]["token_expired"] = _expiry(hours=+12)
    with open(config.TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    assert store.get_valid_token("REAL") == "FRESH"
    assert store.real_access_token == "FRESH", "메모리도 새 토큰으로 갱신돼야 한다"


def test_만료_시각을_모르면_유효하지_않다(store):
    """구버전 캐시·부분 초기화 — 모르는 것을 '유효'로 읽지 않는다."""
    store.set_token("REAL", "NO-EXPIRY", _expiry(hours=+12))
    store.real_token_expired = ""          # 만료 시각만 잃은 상태
    # 파일 캐시가 받쳐 주므로 결과적으로는 같은 토큰이 나오지만, 메모리 경로로 통과하진 않는다.
    assert store._memory_token_alive("") is False
    assert store._memory_token_alive("이건 날짜가 아니다") is False


def test_자동_슬롯도_같은_규칙이다(store):
    store.set_token("AUTO", "AUTO-OLD", _expiry(hours=-1))
    assert store.get_valid_token("AUTO") is None
    store.set_token("AUTO", "AUTO-GOOD", _expiry(hours=+12))
    assert store.get_valid_token("AUTO") == "AUTO-GOOD"
