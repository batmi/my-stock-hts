"""국내 업종 = DART 기업개황 표준산업분류 코드 (modules/industry.py · 2026-10-07)."""
import json
from datetime import date

import pytest

import api
import config
from modules import dart_api, industry
from modules.manage import discover


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JSON_DIR", str(tmp_path), raising=False)
    return tmp_path


def _dart(monkeypatch, codes, fail=(), blocked_after=None):
    """company.json 대역. codes: {corp_code: induty_code|None(013)}."""
    asked = []
    monkeypatch.setattr(api, "get_dart_corp_map", lambda *a, **k: {s: "C" + s for s in codes})

    def call(endpoint, params):
        corp = params["corp_code"]
        asked.append(corp[1:])
        if blocked_after is not None and len(asked) > blocked_after:
            raise dart_api.DartBlockedError("DART 호출 중단 중(테스트)")
        if corp[1:] in fail:
            raise dart_api.DartQueryError("company.json: 응답 코드 800")
        v = codes[corp[1:]]
        return None if v is None else {"status": "000", "induty_code": v}

    monkeypatch.setattr(api, "call_dart", call)
    return asked


# ── 업종명: KIND 업종명과 같은 표준산업분류 11차 소분류 이름 ───────────────────────
def test_name_matches_kind_names_used_by_rules():
    assert industry.name("61220") == "전기 통신업"
    assert industry.name("64992") == "기타 금융업"           # 지주회사 → 소분류 '기타 금융업'
    assert industry.name("264") == "통신 및 방송 장비 제조업"
    assert industry.name("108") == "기타 식품 제조업"        # CJ제일제당 DART 108 = KIND '기타 식품 제조업'(11차)
    assert industry.name("10901") == "동물용 사료 및 조제식품 제조업"   # 10차 표엔 109 가 없어 중분류로 떨어졌다
    assert industry.name("12") == "담배 제조업"              # 2자리만 오면 중분류 이름
    assert industry.name("") is None and industry.name(None) is None


@pytest.mark.parametrize("code,label", [
    ("61220", "통신"), ("35120", "전기"), ("35200", "가스"), ("12000", "음식료"),
    ("107", "음식료"), ("108", "음식료"), ("109", "음식료"), ("11111", "음식료"), ("11201", "음식료"), ("47111", "필수소비 유통"),
])
def test_defensive_rule_codes(code, label):
    assert discover._defensive_label(industry.name(code)) == (True, label)


@pytest.mark.parametrize("code", ["264", "261", "303", "631", "201", "10", "35300", "611"])
def test_non_defensive_codes(code):
    assert discover._defensive_label(industry.name(code)) == (False, None)


def test_holding_rule_code():
    assert any(k in industry.name("64992") for k in discover.HOLDING_KEYWORDS)
    assert not any(k in industry.name("641") for k in discover.HOLDING_KEYWORDS)   # 은행은 아님


# ── 캐시·조회 예산 ─────────────────────────────────────────────────────────────────
def test_fetches_only_missing_and_caches(cache_dir, monkeypatch):
    asked = _dart(monkeypatch, {"000010": "264", "000020": "61220", "000030": None})
    known, why = industry.lookup(["000010", "000020", "000030"], today=date(2026, 10, 7))
    assert known == {"000010": "264", "000020": "61220", "000030": ""}   # 013 은 알려진 빈 값
    assert why is None and len(asked) == 3
    asked.clear()
    known2, _ = industry.lookup(["000010", "000020", "000030"], today=date(2026, 10, 8))
    assert known2 == known and asked == []                                # 두 번째는 캐시에서 끝난다
    saved = json.loads((cache_dir / industry.CACHE_FILE).read_text())
    assert saved["000020"] == {"ksic": "61220", "at": "2026-10-07"}


def test_stale_entries_are_refreshed_but_kept_on_failure(cache_dir, monkeypatch):
    (cache_dir / industry.CACHE_FILE).write_text(json.dumps(
        {"000010": {"ksic": "264", "at": "2026-01-01"}}))
    asked = _dart(monkeypatch, {"000010": "261"}, fail={"000010"})
    known, why = industry.lookup(["000010"], today=date(2026, 10, 7))
    assert asked == ["000010"]                       # 180일 넘어 다시 묻는다
    assert known == {"000010": "264"} and why       # 실패하면 옛 값을 쓰고 사유를 돌려준다


def test_failure_is_unknown_not_empty(cache_dir, monkeypatch):
    _dart(monkeypatch, {"000010": "264", "000020": "61220"}, fail={"000020"})
    known, why = industry.lookup(["000010", "000020"], today=date(2026, 10, 7))
    assert known == {"000010": "264"}                # 실패한 종목은 '없음'이 아니라 '모름'
    assert "1건 조회 실패" in why


def test_blocked_stops_immediately_and_keeps_progress(cache_dir, monkeypatch):
    codes = {f"0000{i}0": "264" for i in range(1, 6)}
    asked = _dart(monkeypatch, codes, blocked_after=2)
    known, why = industry.lookup(list(codes), today=date(2026, 10, 7))
    assert len(asked) == 3 and len(known) == 2      # 차단기가 열리면 남은 종목을 두드리지 않는다
    assert "중단" in why
    saved = json.loads((cache_dir / industry.CACHE_FILE).read_text())
    assert len(saved) == 2                          # 받은 만큼은 남는다


def test_corp_map_failure_returns_cached_only(cache_dir, monkeypatch):
    (cache_dir / industry.CACHE_FILE).write_text(json.dumps(
        {"000010": {"ksic": "264", "at": "2026-10-01"}}))

    def no_map(*a, **k):
        raise dart_api.DartQueryError("DART 기업코드 맵을 받지 못했습니다")

    monkeypatch.setattr(api, "get_dart_corp_map", no_map)
    known, why = industry.lookup(["000010", "000020"], today=date(2026, 10, 7))
    assert known == {"000010": "264"} and "기업코드" in why


def test_lookup_is_sequential_no_thread_pool():
    """2026-10-07 IP 차단의 원인 — 이 모듈에서 병렬 조회를 들이지 말 것."""
    import inspect
    src = inspect.getsource(industry)
    assert "ThreadPoolExecutor" not in src and "Thread(" not in src
