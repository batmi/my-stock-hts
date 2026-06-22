"""토스증권 클라이언트(toss_api) 단위 테스트.

네트워크를 타지 않도록 requests 및 토큰 캐시를 모킹한다.
"""
import json
from unittest.mock import patch, MagicMock

import config
import toss_api


class FakeResponse:
    def __init__(self, status_code, body=None, text="", headers=None):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_get_access_token_success():
    captured = {}

    def fake_set_token(key, token, expired):
        captured["key"] = key
        captured["token"] = token
        captured["expired"] = expired

    config.session.toss_app_key = "c_test"
    config.session.toss_app_secret = "s_test"

    with patch.object(config.session, "get_valid_token", return_value=None), \
         patch.object(config.session, "set_token", side_effect=fake_set_token), \
         patch("toss_api.requests.post",
               return_value=FakeResponse(200, {"access_token": "TOK123", "token_type": "Bearer", "expires_in": 86400})) as mp:
        token = toss_api.get_access_token(force_refresh=True)

    assert token == "TOK123"
    assert captured["key"] == "TOSS"
    # form-urlencoded 본문으로 전송되는지 확인
    _, kwargs = mp.call_args
    assert kwargs["data"]["grant_type"] == "client_credentials"
    assert kwargs["data"]["client_id"] == "c_test"


def test_get_access_token_uses_cache():
    with patch.object(config.session, "get_valid_token", return_value="CACHED"), \
         patch("toss_api.requests.post") as mp:
        token = toss_api.get_access_token(force_refresh=False)
    assert token == "CACHED"
    mp.assert_not_called()


def test_resolve_account_seq_matches_acc_num():
    config.session.toss_account_seq = None
    config.session.toss_acc_num = "12345678901"
    config.session.cano = ""
    accounts = [
        {"accountNo": "99999999999", "accountSeq": 7, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": 3, "accountType": "BROKERAGE"},
    ]
    with patch("toss_api.get_accounts", return_value=accounts):
        seq = toss_api.resolve_account_seq(force=True)
    assert seq == 3
    assert config.session.toss_account_seq == 3


def test_resolve_account_seq_fallback_first():
    config.session.toss_account_seq = None
    config.session.toss_acc_num = ""
    config.session.cano = ""
    accounts = [{"accountNo": "55555555555", "accountSeq": 1, "accountType": "BROKERAGE"}]
    with patch("toss_api.get_accounts", return_value=accounts):
        seq = toss_api.resolve_account_seq(force=True)
    assert seq == 1


def test_get_price_returns_first_row():
    rows = [{"symbol": "005930", "lastPrice": "72000", "currency": "KRW"}]
    with patch("toss_api._request", return_value=rows) as mp:
        result = toss_api.get_price("005930")
    assert result["lastPrice"] == "72000"
    # prices 엔드포인트로 라우팅되는지 확인
    args, kwargs = mp.call_args
    assert "/api/v1/prices" in args[1]


def test_request_error_envelope_raises():
    config.session.toss_account_seq = 1
    err_body = {"error": {"requestId": "R1", "code": "stock-not-found", "message": "없음"}}
    with patch("toss_api.get_access_token", return_value="TOK"), \
         patch("toss_api.requests.get", return_value=FakeResponse(404, err_body)):
        try:
            toss_api.get_orderbook("000000")
            assert False, "TossApiError가 발생해야 함"
        except toss_api.TossApiError as e:
            assert e.code == "stock-not-found"
            assert e.status == 404


def test_create_order_builds_body():
    config.session.toss_account_seq = 1
    captured = {}

    def fake_request(method, path, group, params=None, json_body=None, account=True, retries=2):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"orderId": "OID", "clientOrderId": None}

    with patch("toss_api._request", side_effect=fake_request):
        res = toss_api.create_order("005930", "BUY", order_type="LIMIT", quantity=10, price=70000)

    assert res["orderId"] == "OID"
    assert captured["method"] == "POST"
    assert captured["body"]["symbol"] == "005930"
    assert captured["body"]["quantity"] == "10"
    assert captured["body"]["price"] == "70000"
