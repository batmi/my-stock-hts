"""로그에 인증키·계좌번호가 평문으로 남지 않는가.

[배경 — 실측 2026-08-10, 30일치 로그] KIS 앱키·시크릿·토큰은 **0건**이었다. call_api 의
헤더 마스킹이 제대로 동작하고 있다. 그런데 다른 것 둘이 새고 있었다.

  · DART 인증키 4건  — DART 는 인증키를 쿼리스트링에 싣는다
  · 계좌번호 11건    — KIS 조회 URL 의 CANO, 그리고 ORDER_FAIL 의 REQ 본문

새어나온 경로는 전부 **예외 메시지**다. requests 의 ConnectionError·Timeout 문자열은
URL 을 통째로 물고 오는데, 그 문자열은 호출부의 마스킹 지점을 지나지 않는다.
마스킹이 있는데도 샜다는 사실 자체가, 호출부마다 가리는 방식이 안 된다는 증거다.
그래서 로깅 계층(Filter)에 둔다 — 어느 호출부에서 오든 파일에 닿기 전에 지나간다.

계좌번호는 뒤 4자리를 남긴다. 실전에서 수동·자동 두 계좌가 도는데 어느 쪽인지
구분하지 못하면 사후 추적이 안 된다.
"""
import logging

import pytest

import config


# ─────────────────────────────────────────────
# 1. 무엇을 가리는가
# ─────────────────────────────────────────────

def test_dart_auth_key_is_masked():
    """실제로 로그에 남아 있던 형태 그대로."""
    line = ("[DART] company.json 호출 오류: HTTPSConnectionPool(host='opendart.fss.or.kr', "
            "port=443): Max retries exceeded with url: /api/company.json?corp_code=00302926"
            "&crtfc_key=0d34ea198258d9e4236b45ac03d8b4483474f485")
    out = config.mask_sensitive(line)
    assert "0d34ea198258d9e4236b45ac03d8b4483474f485" not in out
    assert "***MASKED***" in out
    assert "corp_code=00302926" in out, "진단에 필요한 정보까지 지웠다"


def test_account_number_keeps_only_the_last_four():
    """실전은 수동·자동 두 계좌가 돈다 — 어느 쪽인지 모르면 사후 추적이 안 된다."""
    out = config.mask_sensitive("inquire-balance?CANO=68029263&ACNT_PRDT_CD=01")
    assert "68029263" not in out
    assert "CANO=****9263" in out
    assert "ACNT_PRDT_CD=01" in out


def test_account_number_in_a_json_body_is_masked():
    """ORDER_FAIL 로그는 요청 본문을 통째로 찍는다."""
    out = config.mask_sensitive('REQ: {"CANO": "68029263", "ACNT_PRDT_CD": "01", "PDNO": "102780"}')
    assert "68029263" not in out
    assert '"CANO": "****9263"' in out
    assert '"PDNO": "102780"' in out


def test_bearer_token_is_masked():
    out = config.mask_sensitive("authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.abc")
    assert "eyJ0eXAi" not in out
    assert "Bearer ***MASKED***" in out


@pytest.mark.parametrize("key", ["appkey", "appsecret", "app_key", "app_secret",
                                 "api_key", "secret", "token"])
def test_credential_query_params_are_masked(key):
    out = config.mask_sensitive(f"https://x.com/a?{key}=SUPERSECRETVALUE&b=1")
    assert "SUPERSECRETVALUE" not in out
    assert "b=1" in out, "뒤따르는 파라미터까지 삼켰다"


def test_ordinary_lines_are_untouched():
    """대다수 로그는 민감정보가 없다. 건드리면 진단만 어려워진다."""
    line = "[AutoTrade] 매수 성공 | 005930 | 10주 | No.0000123456"
    assert config.mask_sensitive(line) == line


@pytest.mark.parametrize("bad", [None, "", 12345])
def test_masking_never_raises(bad):
    """로그를 남기다 터지면 그 자체가 사고다."""
    config.mask_sensitive(bad)


# ─────────────────────────────────────────────
# 2. 로깅 계층에 실제로 걸려 있는가
# ─────────────────────────────────────────────

def _record(msg, *args):
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, None)


def test_filter_rewrites_the_record_not_just_the_output():
    """포맷 이후가 아니라 레코드를 고쳐야 파일·콘솔 어느 핸들러로 가든 함께 가려진다."""
    f = config.SensitiveDataFilter()
    rec = _record("url: /api?crtfc_key=%s", "SECRET123")
    assert f.filter(rec) is True
    assert "SECRET123" not in rec.getMessage()
    assert "***MASKED***" in rec.getMessage()


def test_filter_keeps_every_record():
    """마스킹은 검열이 아니다 — 어떤 줄도 사라지면 안 된다."""
    f = config.SensitiveDataFilter()
    assert f.filter(_record("아무 내용")) is True


def test_filter_survives_a_broken_format_string():
    """포맷 인자가 어긋난 레코드에서 필터가 터지면 원래 로그까지 잃는다."""
    f = config.SensitiveDataFilter()
    assert f.filter(_record("인자 부족 %s %s", "하나뿐")) is True


def test_both_log_handlers_carry_the_filter():
    """메인 로그와 자동매매 로그 양쪽에 걸려야 한다 — 한쪽만이면 그쪽으로 샌다."""
    import inspect
    src = inspect.getsource(config)
    assert src.count("SensitiveDataFilter()") >= 2, "핸들러 한 곳에만 걸려 있다"


def test_hint_precheck_matches_every_pattern():
    """값싼 사전 검사가 실제 패턴을 다 덮는가 — 못 덮으면 그 패턴은 영원히 안 걸린다.

    라즈베리파이에서 DEBUG 로그를 켜면 모든 레코드가 이 필터를 지나므로 사전 검사를
    두었는데, 그 목록이 패턴과 어긋나면 마스킹이 조용히 무력화된다.
    """
    samples = [
        "?crtfc_key=AAA", "?appkey=AAA", "?appsecret=AAA", "?app_key=AAA",
        "?app_secret=AAA", "?api_key=AAA", "?secret=AAA", "?token=AAA",
        "Bearer AAA", "CANO=12345678", '"CANO": "12345678"',
    ]
    for s in samples:
        assert config.mask_sensitive(s) != s, f"사전 검사에서 걸러져 마스킹되지 않았다: {s}"
