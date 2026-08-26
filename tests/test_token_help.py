# tests/test_token_help.py
"""토큰 발급 실패 안내(build_token_failure_help) 및 실패 원인 분류 테스트.

한투(KIS)는 개인 계정에 IP 화이트리스트 개념이 없으므로(실측 확인),
IP 관련 안내는 토스 모드에서만 노출되어야 한다.
"""
import json
from unittest.mock import MagicMock, patch

import config
import api


def _joined(lines):
    return "\n".join(lines)


def test_kis_auth_shows_appkey_guidance_without_whitelist(monkeypatch):
    """한투 AUTH: 앱키 확인 안내를 보여주되 화이트리스트/IP 언급은 없어야 한다."""
    monkeypatch.setattr(config, 'LAST_TOKEN_ERROR', 'AUTH')
    lines = config.build_token_failure_help(is_toss=False)

    text = _joined(lines)
    assert "AppKey" in text
    assert "유효기간" in text
    assert "화이트리스트" not in text
    assert "허용 IP" not in text
    assert "고객 IP" not in text


def test_kis_ip_blocked_returns_empty(monkeypatch):
    """IP_BLOCKED는 토스 전용 분류 — 한투 모드에서는 안내를 노출하지 않는다."""
    monkeypatch.setattr(config, 'LAST_TOKEN_ERROR', 'IP_BLOCKED')
    assert config.build_token_failure_help(is_toss=False) == []


def test_toss_ip_blocked_shows_whitelist_guidance(monkeypatch):
    """토스 IP_BLOCKED: 허용 IP 등록 안내를 노출한다."""
    monkeypatch.setattr(config, 'LAST_TOKEN_ERROR', 'IP_BLOCKED')
    with patch('config.get_public_ip', return_value='1.2.3.4'):
        lines = config.build_token_failure_help(is_toss=True)

    text = _joined(lines)
    assert "화이트리스트" in text
    assert "1.2.3.4" in text
    assert "토스" in text


def test_kis_network_has_no_ip_mention(monkeypatch):
    """한투 NETWORK: 서버/네트워크 안내만 있고 고객 IP 언급은 없어야 한다."""
    monkeypatch.setattr(config, 'LAST_TOKEN_ERROR', 'NETWORK')
    with patch('config.get_public_ip', return_value='1.2.3.4'):
        lines = config.build_token_failure_help(is_toss=False)

    text = _joined(lines)
    assert "서버" in text
    assert "고객 IP" not in text
    assert "화이트리스트" not in text


def test_fetch_token_403_egw00103_classified_as_auth(monkeypatch):
    """한투 토큰 발급 403+EGW00103(유효하지 않은 AppKey)은 IP_BLOCKED가 아닌 AUTH로 분류."""
    body = {"error_code": "EGW00103", "error_description": "유효하지 않은 AppKey입니다."}
    res = MagicMock(status_code=403, text=json.dumps(body, ensure_ascii=False))
    res.json.return_value = body

    monkeypatch.setattr(config.session, 'is_token_recently_issued', lambda *a, **k: False, raising=False)
    monkeypatch.setattr(config.session, 'get_valid_token', lambda *a, **k: None, raising=False)

    with patch('api._token_session') as mock_sess:
        mock_sess.post.return_value = res
        token = api._fetch_and_set_token("REAL", force_refresh=True)

    assert token is None
    assert config.LAST_TOKEN_ERROR == 'AUTH'


def test_fetch_token_explicit_ip_message_classified_as_ip_blocked(monkeypatch):
    """응답에 IP 차단 문구가 명시된 경우에만 IP_BLOCKED로 분류(미래 대비)."""
    res = MagicMock(status_code=403, text='{"error": "access_denied", "message": "IP address not allowed"}')
    res.json.return_value = {"error": "access_denied"}

    monkeypatch.setattr(config.session, 'is_token_recently_issued', lambda *a, **k: False, raising=False)
    monkeypatch.setattr(config.session, 'get_valid_token', lambda *a, **k: None, raising=False)

    with patch('api._token_session') as mock_sess:
        mock_sess.post.return_value = res
        token = api._fetch_and_set_token("REAL", force_refresh=True)

    assert token is None
    assert config.LAST_TOKEN_ERROR == 'IP_BLOCKED'
