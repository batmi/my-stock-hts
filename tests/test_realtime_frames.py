"""실시간 WS 프레임 처리 — 잘못 읽은 한 프레임이 주문가가 되지 않는가.

[왜 이 파일인가] `_on_message` 는 들어온 프레임이 체결통보인지·체결가인지·호가인지
가르는 분기점인데, 전체 스위트에서 **한 줄도 실행되지 않았다**(2026-08-30 커버리지 실측
brokers/realtime.py 52%). 파서(parse_h0stcnt0 등)는 검증돼 있었지만 그 파서를 **무엇으로
부르고 어디에 담는지**는 아무도 확인하지 않았다.

[왜 중요한가] 이 캐시는 장식이 아니다. get_current_price 가 REST보다 **먼저** 이 값을
읽고, 그 값이 곧 트리거 판정과 주문가가 된다. 프레임을 잘못 갈라 담으면 REST는 멀쩡한데
주문만 엉뚱한 가격에 나간다 — 로그에는 아무 오류도 남지 않는다.

[여기서 고정하는 것]
  ① 라우팅: 체결통보 프레임은 절대 시세 캐시에 들어가지 않는다(반대도 마찬가지).
  ② 캐시 키: 전문의 종목코드가 읽기 경로의 조회 키와 정확히 일치한다.
  ③ 폴백: 값이 없거나·묵었거나·0이면 None → REST 경로로 넘어간다(fail-safe).
  ④ 내구성: 망가진 프레임이 예외를 던지거나 캐시를 오염시키지 않는다.
"""
import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import config
from brokers import realtime as rt

KEY, IV = "0" * 32, "1" * 16


def _encrypt(plain, key=KEY, iv=IV):
    enc = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    pad = 16 - len(plain.encode()) % 16
    body = plain.encode() + bytes([pad]) * pad
    return base64.b64encode(enc.update(body) + enc.finalize()).decode()


def _price_rec(code="005930", price="70500", rate="1.23", vol="123456", vs="145.5"):
    """H0STCNT0 레코드(필드 인덱스는 스펙 고정: 0코드 2현재가 5등락률 13거래량 18체결강도)."""
    rec = ["0"] * 20
    rec[rt._P_CODE], rec[rt._P_PRICE] = code, price
    rec[rt._P_CHG_RATE], rec[rt._P_VOLUME], rec[rt._P_VOL_STRENGTH] = rate, vol, vs
    return rec


def _ask_rec(code="005930", ask="1000", bid="2000"):
    rec = ["0"] * 46
    rec[rt._A_CODE], rec[rt._A_TOTAL_ASK], rec[rt._A_TOTAL_BID] = code, ask, bid
    return rec


def _exec_rec(code="005930", odno="0000013727", qty="10", price="70000", filled="2"):
    rec = ["0"] * 14
    rec[rt._E_CUST], rec[rt._E_ACNT] = "CUST", "5012"
    rec[rt._E_ODNO], rec[rt._E_BUYSELL] = odno, "02"
    rec[rt._E_CODE], rec[rt._E_QTY], rec[rt._E_PRICE] = code, qty, price
    rec[rt._E_TIME], rec[rt._E_REJECT], rec[rt._E_FILLED] = "093015", "0", filled
    return rec


def _frame(tr_id, recs, enc="0"):
    return f"{enc}|{tr_id}|{len(recs):03d}|" + "^".join(f for r in recs for f in r)


@pytest.fixture
def feed():
    f = rt.KisRealtimeFeed()
    f._ws = MagicMock()
    return f


def _feed_msg(feed, msg):
    feed._on_message(msg, feed._ws, "APPROVAL")


# ───────────────────── ① 프레임 라우팅 ─────────────────────

def test_a_price_frame_lands_in_the_price_cache(feed):
    _feed_msg(feed, _frame(rt.TR_PRICE, [_price_rec()]))
    assert feed.get_price("005930") == 70500.0
    assert feed.get_vol_strength("005930") == 145.5


def test_one_frame_can_carry_several_symbols(feed):
    _feed_msg(feed, _frame(rt.TR_PRICE, [_price_rec("005930", "70500"),
                                         _price_rec("000660", "120000")]))
    assert feed.get_price("005930") == 70500.0
    assert feed.get_price("000660") == 120000.0


def test_an_orderbook_frame_lands_in_the_orderbook_cache(feed):
    _feed_msg(feed, _frame(rt.TR_ASK, [_ask_rec(ask="1000", bid="2000")]))
    assert feed.get_orderbook("005930") == {"total_ask": 1000.0, "total_bid": 2000.0}
    assert feed.get_price("005930") is None, "호가 프레임이 현재가 캐시를 건드렸다"


def test_an_execution_notice_never_becomes_a_price(feed):
    """[핵심] 체결통보를 시세로 오독하면 REST는 멀쩡한데 주문가만 엉뚱해진다.

    두 전문은 tr_id 로만 갈린다 — 체결통보 body 를 시세 파서에 태우면 필드가 밀려
    체결수량·주문번호 같은 값이 '현재가'로 캐시에 앉는다.
    """
    feed._aes_key, feed._aes_iv = KEY, IV
    got = []
    feed.register_exec_callback(got.append)
    _feed_msg(feed, f"1|{rt.TR_EXEC_REAL}|001|{_encrypt('^'.join(_exec_rec()))}")

    assert len(got) == 1 and got[0]["odno"] == "0000013727"
    assert feed._price == {} and feed._ask == {}


def test_the_subscription_reply_hands_over_the_decryption_keys(feed):
    """[핵심] 이 키를 못 받으면 이후 체결통보는 **전부 조용히 버려진다**.

    복호화 실패는 DEBUG 로그 한 줄이라 눈에 띄지 않는다 — 키 수신이 곧 기능의 스위치다.
    """
    assert feed._aes_key is None
    _feed_msg(feed, json.dumps({"header": {"tr_id": rt.TR_EXEC_REAL},
                                "body": {"output": {"key": KEY, "iv": IV}}}))
    assert (feed._aes_key, feed._aes_iv) == (KEY, IV)

    got = []
    feed.register_exec_callback(got.append)
    _feed_msg(feed, f"1|{rt.TR_EXEC_REAL}|001|{_encrypt('^'.join(_exec_rec()))}")
    assert len(got) == 1, "키를 받고도 체결통보를 처리하지 못했다"


def test_a_pingpong_is_echoed_back(feed):
    """핑퐁을 되받지 않으면 서버가 연결을 끊는다(재연결 루프로 복구되나 수신이 끊긴다)."""
    msg = json.dumps({"header": {"tr_id": "PINGPONG"}})
    with patch.object(rt.asyncio, "ensure_future") as fut:
        _feed_msg(feed, msg)
    feed._ws.send.assert_called_once_with(msg)
    fut.assert_called_once()


# ───────────────────── ② 캐시 키가 조회 키와 같은가 ─────────────────────

def test_the_cache_key_matches_the_lookup_key(feed):
    """전문에 공백이 섞여도 6자리 조회 키로 찾을 수 있어야 한다.

    어긋나면 캐시가 영원히 안 맞아 항상 REST 로 돈다 — 오류 없이 기능만 죽는다.
    """
    _feed_msg(feed, _frame(rt.TR_PRICE, [_price_rec(code=" 005930 ")]))
    _feed_msg(feed, _frame(rt.TR_ASK, [_ask_rec(code=" 005930 ")]))
    assert feed.get_price("005930") == 70500.0
    assert feed.get_orderbook("005930") is not None


# ───────────────────── ③ 모르면 REST 로 넘긴다 ─────────────────────

def test_a_stale_quote_is_not_served(feed):
    """끊긴 연결의 마지막 값이 계속 '현재가'로 나오면 그게 최악이다."""
    _feed_msg(feed, _frame(rt.TR_PRICE, [_price_rec()]))
    feed._price["005930"]["ts"] = time.time() - 100
    assert feed.get_price("005930") is None
    assert feed.get_vol_strength("005930") is None
    assert feed.get_orderbook("005930") is None


def test_a_zero_price_is_not_served(feed):
    """장 시작 전·거래정지 등으로 0이 오면 REST 로 넘긴다(0원 주문가 방지)."""
    _feed_msg(feed, _frame(rt.TR_PRICE, [_price_rec(price="0")]))
    assert feed.get_price("005930") is None


def test_an_unsubscribed_symbol_is_not_served(feed):
    assert feed.get_price("999999") is None


def test_get_current_price_falls_back_to_rest_when_the_feed_is_silent():
    """[핵심] 폴백 전환 자체를 고정한다 — WS 가 답을 못 주면 REST 가 답해야 한다."""
    from api.quotes import price as qp

    silent = rt.KisRealtimeFeed()                      # 캐시 비어 있음
    live = rt.KisRealtimeFeed()
    live._on_message(_frame(rt.TR_PRICE, [_price_rec(price="70500")]), MagicMock(), "AK")

    with patch.object(config, "USE_WEBSOCKET", True), \
         patch.object(config.session, "is_toss", False), \
         patch.object(qp, "get_current_price_data",
                      return_value={"rt_cd": "0", "output": {"stck_prpr": "69000"}}) as rest:
        with patch.object(rt, "get_feed", return_value=silent):
            assert qp.get_current_price("005930", False) == 69000
        assert rest.called, "WS 가 값이 없는데 REST 도 부르지 않았다"

        rest.reset_mock()
        with patch.object(rt, "get_feed", return_value=live):
            assert qp.get_current_price("005930", False) == 70500.0
        assert not rest.called, "신선한 WS 값이 있는데도 REST 를 불렀다(TPS 낭비)"


def test_toss_feed_serves_nothing_and_takes_no_exec_callback():
    """토스는 공식 WS 가 없다 — 피드가 조용히 아무것도 주지 않아야 REST 로 돈다."""
    f = rt.TossPollingFeed()
    assert f.get_price("005930") is None and f.get_orderbook("005930") is None
    assert not hasattr(f, "register_exec_callback")


# ───────────────────── ④ 망가진 프레임 ─────────────────────

@pytest.mark.parametrize("bad", [
    "", "|||", "0|H0STCNT0|001", "not json {", "{}", "[]",
    "0|H0STCNT0|001|too^short", "0|H0STCNT0|abc|" + "^".join(_price_rec()),
    "0|UNKNOWN_TR|001|" + "^".join(_price_rec()),
])
def test_a_broken_frame_neither_raises_nor_poisons_the_cache(feed, bad):
    _feed_msg(feed, _frame(rt.TR_PRICE, [_price_rec()]))
    _feed_msg(feed, bad)                       # 예외가 새면 수신 루프가 죽는다
    assert feed.get_price("005930") == 70500.0
    assert set(feed._price) == {"005930"}


def test_an_encrypted_notice_without_keys_is_dropped_not_guessed(feed):
    """키가 없으면 조용히 버린다 — 폴링이 그 체결을 잡는다(추측해서 쓰면 안 된다)."""
    feed._aes_key = feed._aes_iv = None
    got = []
    feed.register_exec_callback(got.append)
    _feed_msg(feed, f"1|{rt.TR_EXEC_REAL}|001|{_encrypt('^'.join(_exec_rec()))}")
    assert got == [] and feed._price == {}


def test_a_failing_callback_does_not_stop_the_others(feed):
    """콜백 하나가 터져도 나머지가 체결통보를 받아야 한다(수신 루프도 살아 있어야 한다)."""
    feed._aes_key, feed._aes_iv = KEY, IV
    got = []
    feed.register_exec_callback(lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    feed.register_exec_callback(got.append)
    _feed_msg(feed, f"1|{rt.TR_EXEC_REAL}|001|{_encrypt('^'.join(_exec_rec()))}")
    assert len(got) == 1


def test_a_callback_registers_only_once(feed):
    got = []
    fn = got.append
    feed.register_exec_callback(fn)
    feed.register_exec_callback(fn)
    feed._aes_key, feed._aes_iv = KEY, IV
    _feed_msg(feed, f"1|{rt.TR_EXEC_REAL}|001|{_encrypt('^'.join(_exec_rec()))}")
    assert len(got) == 1, "같은 콜백이 두 번 등록돼 체결 처리가 중복됐다"
