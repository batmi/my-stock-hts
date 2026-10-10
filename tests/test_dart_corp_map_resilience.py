"""DART 기업코드 맵 — 만료 파일 유지·오류 문구·원자 저장·새로 상장한 종목 재확인 (2026-10-10).

[왜] 맵(json/dart_corp_map.json)은 30일마다 다시 받는다. 다시 받기가 실패하면 종전엔 갖고 있던 파일을
 버리고 실패해, DART 점검(status 800 — 2026-10-09 아침 실측)과 겹치는 날 6-5~6-8·공시 알림·탐색 업종이
 한꺼번에 멈췄다. 또 맵을 받은 뒤 상장한 종목은 12곳 모두 '해당 없음'으로 답했다(조회 못 한 것을 '없음'으로).
"""
import io
import json
import logging
import os
import time
import zipfile
from unittest.mock import MagicMock

import pytest

import api
import config
from modules import dart_api


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DART_API_KEY", "dummy", raising=False)
    monkeypatch.setattr(config, "JSON_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(dart_api, "_dart_corp_map_cache", None)
    return tmp_path


def _write_map(path, data, age_days):
    p = path / "dart_corp_map.json"
    p.write_text(json.dumps(data))
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def _zip_response(pairs):
    xml = "<result>" + "".join(f"<list><corp_code>{c}</corp_code><stock_code>{s}</stock_code></list>"
                               for s, c in pairs) + "</result>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml)
    res = MagicMock()
    res.content = buf.getvalue()
    return res


_MAINT = MagicMock(content='<?xml version="1.0"?><result><status>800</status><message>시스템 점검으로 인한 서비스가 중지 중입니다.</message></result>'.encode())


def test_expired_file_kept_when_redownload_fails(env, monkeypatch, caplog):
    _write_map(env, {"005930": "00126380"}, age_days=40)
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: _MAINT)
    with caplog.at_level(logging.WARNING):
        m = dart_api.get_dart_corp_map()
    assert m == {"005930": "00126380"}                       # 멈추지 않고 옛 맵으로 계속
    assert any("만료된 파일" in r.message and "800" in r.message for r in caplog.records)


def test_no_file_and_maintenance_names_the_real_cause(env, monkeypatch):
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: _MAINT)
    with pytest.raises(dart_api.DartQueryError) as ei:
        dart_api.get_dart_corp_map()
    assert "status 800" in str(ei.value) and "점검" in str(ei.value)   # 'zip 아님' 대신 실제 원인


def test_download_saved_atomically(env, monkeypatch):
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: _zip_response([("005930", "00126380")]))
    assert dart_api.get_dart_corp_map() == {"005930": "00126380"}
    assert json.loads((env / "dart_corp_map.json").read_text()) == {"005930": "00126380"}
    assert not (env / "dart_corp_map.json.tmp").exists()


def test_fresh_file_used_without_network(env, monkeypatch):
    _write_map(env, {"005930": "00126380"}, age_days=3)
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("받지 않는다")))
    assert dart_api.get_dart_corp_map() == {"005930": "00126380"}


# ── 맵에 없는 종목 ─────────────────────────────────────────────────────────────────
def test_missing_code_with_old_map_refreshes_once_and_finds(env, monkeypatch):
    _write_map(env, {"005930": "00126380"}, age_days=10)
    dart_api.get_dart_corp_map()
    sent = []
    monkeypatch.setattr(dart_api, "_send",
                        lambda *a, **k: sent.append(1) or _zip_response([("005930", "00126380"), ("0099A0", "01999999")]))
    assert dart_api.corp_code_for("0099A0") == "01999999"     # 새로 상장한 종목을 찾았다
    assert dart_api.corp_code_for("999999") is None           # 새 맵에도 없다 = 진짜 없음(ETF·비상장)
    assert len(sent) == 1                                      # 하루 한 번만 다시 받는다


def test_missing_code_and_refresh_fails_is_unknown(env, monkeypatch):
    _write_map(env, {"005930": "00126380"}, age_days=10)
    dart_api.get_dart_corp_map()
    sent = []
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: sent.append(1) or _MAINT)
    for _ in range(3):
        with pytest.raises(dart_api.DartQueryError) as ei:
            dart_api.corp_code_for("0099A0")
        assert "점검" in str(ei.value)                         # '공시 없음'이 아니라 '모름'
    assert len(sent) == 1                                      # 실패해도 그날은 다시 두드리지 않는다
    assert dart_api.corp_code_for("005930") == "00126380"      # 옛 맵에 있는 종목은 그대로


def test_missing_code_with_fresh_map_is_none_without_refresh(env, monkeypatch):
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: _zip_response([("005930", "00126380")]))
    dart_api.get_dart_corp_map()                               # 방금 받은 맵
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("다시 받지 않는다")))
    assert dart_api.corp_code_for("069500") is None            # ETF 는 맵에 없다 — 하루 안 맵이면 그대로 없음


def test_getters_report_unknown_not_empty_for_unconfirmable_code(env, monkeypatch):
    _write_map(env, {"005930": "00126380"}, age_days=10)
    dart_api.get_dart_corp_map()
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: _MAINT)
    with pytest.raises(dart_api.DartQueryError):
        dart_api.get_dart_disclosures("0099A0", days=14)


# ── 공시 목록 상한 경고 ───────────────────────────────────────────────────────────
def test_disclosure_page_cap_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(api, "get_dart_corp_map", lambda *a, **k: {"005930": "00126380"})
    n = {"i": 0}

    def page(endpoint, params):
        n["i"] += 1
        return [{"rcept_no": f"{n['i']:04d}{j:03d}", "report_nm": "x", "rcept_dt": "20261001"} for j in range(100)]

    monkeypatch.setattr(api, "call_dart", page)
    with caplog.at_level(logging.WARNING):
        out = dart_api.get_dart_disclosures("005930", days=730)
    assert len(out) == 100 * dart_api._DISCLOSURE_MAX_PAGES
    assert any("상한" in r.message for r in caplog.records)


def test_watchlist_etf_is_not_a_dart_filer(env, monkeypatch):
    """6-6 은 국내 ETF 도 훑는다 — ETF 는 맵에 없는 게 정상이라 다시 받지도, 실패로 보이지도 않는다."""
    _write_map(env, {"005930": "00126380"}, age_days=10)
    dart_api.get_dart_corp_map()
    monkeypatch.setattr(config.session, "stock_data", {"etfs_kr": [{"code": "069500"}]}, raising=False)
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ETF 때문에 받지 않는다")))
    assert dart_api.corp_code_for("069500") is None
