"""응답이 유실된 주문을 재전송하지 않는가 — 중복 주문 방지.

[배경] ThrottledSession.request 는 예외가 나면 메서드를 가리지 않고 백오프 후 재시도했다
(MAX_RETRIES=4, DEFAULT_TIMEOUT=10초). 조회라면 몇 번을 다시 보내도 무해하지만, 주문은
다르다. 요청이 거래소에 닿아 체결된 뒤 응답만 유실돼도 똑같이 타임아웃으로 보이고,
그때 재전송하면 **같은 주문이 두 번 나간다**.

포지션이 두 배가 되면 손절폭·변동성 한도·포트폴리오 히트 캡이 한꺼번에 무의미해진다.
이 시스템의 1차 리스크 통제는 자본 대비 리스크 한도이고 그건 전부 수량 산정 하나에
실려 있다 — 손실 자체보다 **통제 수단이 조용히 사라지는 것**이 문제다.

코드는 이 위험을 부분적으로 알고 있었다. EGW00215(주문 계열 오류)에는
'주문과 같이 상태 변화가 있는 API는 중복 방지를 위해 재시도하지 않음' 이라는 분기가
이미 있다. 다만 그건 **응답을 받은** 경우뿐이고, 정작 위험한 무응답은 덮이지 않았다.

여기서 고정하는 계약:
  1. 응답을 받은 거부(EGW00201 등)는 종전대로 재시도한다 — 주문이 안 들어간 것이 확정이다
  2. 연결 자체가 안 된 경우(ConnectTimeout)도 재시도한다 — 주문이 나갔을 수 없다
  3. 요청을 보낸 뒤 응답을 못 받은 경우(ReadTimeout 등)는 재전송하지 않는다
  4. 그 경우 **조회로 확인**한다. 재전송이 아니라 대사(reconcile)다
  5. 애매하면(후보 2건 이상) 자동으로 정하지 않고 운용자에게 넘긴다
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

import api


# ─────────────────────────────────────────────
# 1. 무엇을 '상태 변화'로 보는가
# ─────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "/uapi/domestic-stock/v1/trading/order-cash",
    "/uapi/domestic-stock/v1/trading/order-rvsecncl",
    "/uapi/overseas-stock/v1/trading/order",
    "/uapi/overseas-stock/v1/trading/order-rvsecncl",
])
def test_order_endpoints_are_state_changing(url):
    assert api._is_state_changing("POST", url) is True


@pytest.mark.parametrize("method,url", [
    ("GET", "/uapi/domestic-stock/v1/trading/order-cash"),
    ("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order"),
    ("GET", "/uapi/domestic-stock/v1/quotations/inquire-price"),
    ("POST", "/oauth2/tokenP"),
])
def test_reads_and_token_issue_are_not_state_changing(method, url):
    """조회는 몇 번 다시 보내도 무해하다 — 여기까지 막으면 장애 내성이 떨어진다."""
    assert api._is_state_changing(method, url) is False


# ─────────────────────────────────────────────
# 2. 무엇을 '결과 모름'으로 보는가
# ─────────────────────────────────────────────

def test_connect_timeout_is_safe_to_retry():
    """연결이 안 된 것이므로 주문이 나갔을 수 없다. 이것까지 막으면 과잉이다."""
    assert api._is_response_unknown(requests.exceptions.ConnectTimeout()) is False


@pytest.mark.parametrize("exc", [
    requests.exceptions.ReadTimeout(),
    requests.exceptions.ConnectionError(),
    requests.exceptions.ChunkedEncodingError(),
])
def test_send_without_response_is_unknown(exc):
    """요청은 나갔는데 응답을 못 받았다 — 체결됐을 수 있다."""
    assert api._is_response_unknown(exc) is True


def test_application_level_rejection_is_not_unknown():
    """EGW00201 같은 거부는 응답을 받은 것이다 — 주문 미접수가 확정이라 재시도해야 한다."""
    assert api._is_response_unknown(Exception("Rate Limit Exceeded (EGW00201)")) is False


# ─────────────────────────────────────────────
# 3. 대사(reconcile) — 재전송이 아니라 조회로 확인한다
# ─────────────────────────────────────────────

def _row(code="005930", side="02", qty=10, odno="0000123456", minutes_ago=0):
    t = datetime.now() - timedelta(minutes=minutes_ago)
    return {"pdno": code, "sll_buy_dvsn_cd": side, "ord_qty": str(qty),
            "odno": odno, "ord_tmd": t.strftime("%H%M%S")}


@pytest.fixture
def no_db_records(monkeypatch):
    """DB에 아무 주문도 없는 상태 — 응답을 못 받았으니 기록도 없는 게 정상이다."""
    monkeypatch.setattr(api, '_odno_known_to_db', lambda odno: False)


def test_a_landed_order_is_adopted_not_resent(monkeypatch, no_db_records):
    """거래소에 있으면 그 주문을 이어받는다 — 두 번째 주문을 내지 않는다."""
    monkeypatch.setattr(api, 'get_today_history', lambda *a, **k: {"rt_cd": "0", "output1": [_row()]})
    res = api._reconcile_unknown_order("buy", "005930", 10, "ReadTimeout")
    assert res['rt_cd'] == '0'
    assert res['msg_cd'] == 'ORDER_RECOVERED'
    assert res['output']['ODNO'] == "0000123456"


def test_no_trace_means_the_order_never_landed(monkeypatch, no_db_records):
    monkeypatch.setattr(api, 'get_today_history', lambda *a, **k: {"rt_cd": "0", "output1": []})
    res = api._reconcile_unknown_order("buy", "005930", 10, "ReadTimeout")
    assert res['rt_cd'] == '1'
    assert res['msg_cd'] == 'ORDER_NOT_PLACED'


def test_ambiguity_is_handed_to_the_operator(monkeypatch, no_db_records):
    """후보가 둘이면 어느 것이 이번 주문인지 단정할 수 없다 — 잘못 고르면 남의 주문을
    '내 것'으로 알고 관리하게 된다. 자동으로 정하지 않는다."""
    rows = [_row(odno="0000111111"), _row(odno="0000222222")]
    monkeypatch.setattr(api, 'get_today_history', lambda *a, **k: {"rt_cd": "0", "output1": rows})
    with patch.object(api, 'send_telegram_message') as tg:
        res = api._reconcile_unknown_order("buy", "005930", 10, "ReadTimeout")
    assert res['msg_cd'] == 'ORDER_UNKNOWN'
    assert tg.called, "판단을 못 하는 상태를 운용자에게 알리지 않았다"
    assert "0000111111" in tg.call_args[0][0]


def test_orders_already_in_db_are_not_adopted(monkeypatch):
    """응답을 받아 기록된 주문은 이번 건이 아니다 — 그걸 이어받으면 남의 주문을 훔친다."""
    monkeypatch.setattr(api, 'get_today_history', lambda *a, **k: {"rt_cd": "0", "output1": [_row()]})
    monkeypatch.setattr(api, '_odno_known_to_db', lambda odno: True)
    res = api._reconcile_unknown_order("buy", "005930", 10, "ReadTimeout")
    assert res['msg_cd'] == 'ORDER_NOT_PLACED'


@pytest.mark.parametrize("row,label", [
    (_row(code="000660"), "다른 종목"),
    (_row(side="01"), "매도/매수 반대"),
    (_row(qty=7), "수량 불일치"),
    (_row(minutes_ago=30), "시간 창 밖(직전 주기의 주문)"),
])
def test_only_a_matching_order_counts(monkeypatch, no_db_records, row, label):
    monkeypatch.setattr(api, 'get_today_history', lambda *a, **k: {"rt_cd": "0", "output1": [row]})
    res = api._reconcile_unknown_order("buy", "005930", 10, "ReadTimeout")
    assert res['msg_cd'] == 'ORDER_NOT_PLACED', f"{label}인데 이번 주문으로 봤다"


def test_대사_조회가_실패하면_미접수로_단정하지_않는다(monkeypatch, no_db_records):
    """조회 **실패**와 '주문 없음'은 다르다.

    [무엇을 지키는가] 종전에는 `(get_today_history() or {}).get('output1') or []` 라,
     rt_cd != '0' 인 실패 응답이 그대로 빈 목록이 됐다. 그러면 대사는
     "접수 흔적 없음 → 미접수로 봅니다"(ORDER_NOT_PLACED)로 끝난다 — 이 계층이
     존재하는 이유와 정반대 결론이다. 다음 주기가 같은 주문을 다시 내고, 게다가
     ORDER_NOT_PLACED 는 운용자에게 알리지도 않아 이중 주문이 조용히 난다.
     실측(2026-09-06): rt_cd='9999' 응답 → msg_cd=ORDER_NOT_PLACED, '대사 확인'이라 적힌 채.
    """
    monkeypatch.setattr(api, 'get_today_history',
                        lambda *a, **k: {"rt_cd": "9999", "msg_cd": "NETERR", "msg1": "타임아웃"})
    sent = []
    monkeypatch.setattr(api, 'send_telegram_message', lambda m, **k: sent.append(m))

    res = api._reconcile_unknown_order("buy", "005930", 10, "응답 유실")

    assert res['msg_cd'] == 'ORDER_UNKNOWN', (
        f"조회 실패를 '{res['msg_cd']}' 로 단정했다 — 재전송 금지 규칙이 뒤집힌다")


def test_토스_미체결_조회가_실패해도_미접수로_단정하지_않는다(monkeypatch, no_db_records):
    """토스 당일 이력은 CLOSED 만 준다 — 미체결을 합쳐야 KIS 와 같은 범위가 된다.
    그 조회가 실패하면 범위가 반쪽이고, 반쪽으로 '미접수'를 결론지을 수 없다."""
    import config
    monkeypatch.setattr(config.session, 'is_toss', True, raising=False)
    monkeypatch.setattr(api, 'get_today_history', lambda *a, **k: {"rt_cd": "0", "output1": []})
    monkeypatch.setattr(api, '_toss_open_orders', lambda market: None)   # 조회 실패
    monkeypatch.setattr(api, 'send_telegram_message', lambda m, **k: None)

    res = api._reconcile_unknown_order("buy", "005930", 10, "응답 유실")
    assert res['msg_cd'] == 'ORDER_UNKNOWN'


def test_a_failed_lookup_leaves_it_unknown(monkeypatch, no_db_records):
    """확인을 못 했으면 '미접수'가 아니라 '모름'이다 — 없다고 단정하면 재주문으로 이어진다."""
    def boom(*a, **k):
        raise RuntimeError("조회 실패")
    monkeypatch.setattr(api, 'get_today_history', boom)
    res = api._reconcile_unknown_order("buy", "005930", 10, "ReadTimeout")
    assert res['msg_cd'] == 'ORDER_UNKNOWN'


def test_db_lookup_failure_is_conservative(monkeypatch):
    """DB를 못 읽으면 '이미 아는 주문'으로 본다 — 모르는 주문을 함부로 이어받지 않는다."""
    with patch('modules.db_manager.db.check_trade_exists', side_effect=RuntimeError("db down")):
        assert api._odno_known_to_db("0000123456") is True


# ─────────────────────────────────────────────
# 4. 실제 전송 경로 — 재전송이 일어나지 않는가
# ─────────────────────────────────────────────

def test_place_order_does_not_resend_on_lost_response(monkeypatch):
    """[핵심] 이 테스트가 이번 수정의 전부다 — POST가 두 번 나가면 안 된다."""
    calls = []

    def fake_call_api(*a, **k):
        calls.append(k.get('method'))
        raise api.OrderOutcomeUnknown("ReadTimeout")

    monkeypatch.setattr(api, 'call_api', fake_call_api)
    monkeypatch.setattr(api, '_paper_active', lambda: False)
    monkeypatch.setattr(api, '_prepare_account_params', lambda a, b: ("12345678", "01"))
    monkeypatch.setattr(api, 'get_today_history', lambda *a, **k: {"rt_cd": "0", "output1": [_row()]})
    monkeypatch.setattr(api, '_odno_known_to_db', lambda odno: False)
    monkeypatch.setattr(api.config.session, 'is_toss', False, raising=False)

    res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")

    assert len(calls) == 1, f"주문이 {len(calls)}번 전송됐다 — 중복 주문이 나간다"
    assert res['msg_cd'] == 'ORDER_RECOVERED'


def test_overseas_has_no_reconcile_path_and_says_so(monkeypatch):
    """해외는 당일 주문 대사 경로가 없다. 확인 못 하면 '결과 불명'으로 남긴다."""
    monkeypatch.setattr(api, 'call_api',
                        lambda *a, **k: (_ for _ in ()).throw(api.OrderOutcomeUnknown("ReadTimeout")))
    monkeypatch.setattr(api, '_paper_active', lambda: False)
    monkeypatch.setattr(api, '_prepare_account_params', lambda a, b: ("12345678", "01"))
    monkeypatch.setattr(api.config.session, 'is_toss', False, raising=False)

    res = api.place_order("overseas", "buy", "AAPL", 1, 200, "00", exchange_code="NAS")
    assert res['msg_cd'] == 'ORDER_UNKNOWN'


def test_cancel_failure_is_left_to_the_next_cycle(monkeypatch):
    """정정·취소는 결과가 애매해도 손해가 누적되지 않는다 — 다음 주기가 미체결을 다시 잡는다."""
    monkeypatch.setattr(api, 'call_api',
                        lambda *a, **k: (_ for _ in ()).throw(api.OrderOutcomeUnknown("ReadTimeout")))
    monkeypatch.setattr(api, '_paper_active', lambda: False)
    monkeypatch.setattr(api, '_prepare_account_params', lambda a, b: ("12345678", "01"))
    monkeypatch.setattr(api.config.session, 'is_toss', False, raising=False)
    # NXT 미지원 종목(=KRX 직접 라우팅) 경로를 잰다. SOR 경로는 거래소 폴백이 따로 있어
    #  예외를 그대로 올리며, 그건 _order_with_exchange_fallback 쪽 계약이다.
    monkeypatch.setattr(api, 'is_nxt_tradeable', lambda *a, **k: False)

    res = api.revise_cancel_order("domestic", "cancel", "0000111111", "005930", 10, 0, "02", "00")
    assert res['rt_cd'] == '1' and res['msg_cd'] == 'ORDER_UNKNOWN'


# ─────────────────────────────────────────────
# 5. 세션 계층 — 재시도 루프에서 실제로 빠져나오는가
# ─────────────────────────────────────────────

def _session_request(method, url, exc):
    """ThrottledSession.request 를 태우되 실제 소켓은 쓰지 않는다."""
    with patch('requests.Session.request', side_effect=exc) as sent:
        with patch.object(api.ThrottledSession, '_is_screen_output_allowed', create=True,
                          return_value=False):
            try:
                api.session.request(method, url, retries=3)
            except Exception as e:
                return sent.call_count, e
    return sent.call_count, None


def test_lost_order_response_escapes_the_retry_loop_immediately():
    n, exc = _session_request("POST", "https://openapi.koreainvestment.com:9443"
                                      "/uapi/domestic-stock/v1/trading/order-cash",
                              requests.exceptions.ReadTimeout("timeout"))
    assert isinstance(exc, api.OrderOutcomeUnknown)
    assert n == 1, f"주문이 {n}번 전송됐다"


def test_a_read_timeout_on_a_query_is_still_retried():
    """조회까지 막으면 일시적 네트워크 흔들림에 시스템이 약해진다."""
    n, exc = _session_request("GET", "https://openapi.koreainvestment.com:9443"
                                     "/uapi/domestic-stock/v1/quotations/inquire-price",
                              requests.exceptions.ReadTimeout("timeout"))
    assert not isinstance(exc, api.OrderOutcomeUnknown)
    assert n > 1, "조회 재시도가 사라졌다"
